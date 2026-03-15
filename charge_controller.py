#!/usr/bin/env python3
"""BLUETTI smart charge controller.

Charges the battery when electricity is cheap (Looop denki forecast),
stops charging when expensive. Uses SwitchBot Plug Mini to control
the charger power. Forces charging when SOC drops below threshold.
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

import requests
from requests.exceptions import ConnectionError, Timeout
from dotenv import load_dotenv

import bluetti_battery

ENV_PATH = Path(__file__).parent / ".env"
DB_PATH = Path(__file__).parent / "soc_history.db"

LOOOP_API_URL = "https://looop-denki.com/api/prices"
SWITCHBOT_API_BASE = "https://api.switch-bot.com/v1.1"


# --- HTTP retry helper ---

def _request_with_retry(method, url, max_retries=3, **kwargs):
    """HTTP request with exponential backoff retry.

    Retries on: ConnectionError, Timeout, HTTP 5xx, 429.
    Does NOT retry on: HTTP 4xx (except 429).
    """
    kwargs.setdefault("timeout", 30)
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"Retry {attempt+1}/{max_retries}: HTTP {resp.status_code} from {url}, waiting {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
            resp.raise_for_status()
            return resp
        except (ConnectionError, Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"Retry {attempt+1}/{max_retries}: {type(e).__name__} for {url}, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except requests.HTTPError:
            raise
    # Should not reach here, but just in case
    raise last_exc


# --- Configuration ---

def load_config(require_switchbot=True):
    """Load configuration from .env file."""
    load_dotenv(ENV_PATH, override=True)
    config = {
        "switchbot_token": os.getenv("SWITCHBOT_TOKEN"),
        "switchbot_secret": os.getenv("SWITCHBOT_SECRET"),
        "switchbot_device_id": os.getenv("SWITCHBOT_DEVICE_ID"),
        "looop_area": os.getenv("LOOOP_AREA", "01"),
        "soc_min": int(os.getenv("SOC_MIN", "20")),
        "soc_max": int(os.getenv("SOC_MAX", "80")),
        "charge_rate_pct_per_slot": float(os.getenv("CHARGE_RATE_PCT_PER_SLOT", "10")),
        "default_consumption_rate": float(os.getenv("DEFAULT_CONSUMPTION_RATE", "3.0")),
        "pushbullet_token": os.getenv("PUSHBULLET_TOKEN"),
    }
    if require_switchbot:
        required = ["switchbot_token", "switchbot_secret", "switchbot_device_id"]
        missing = [k for k in required if not config[k]]
        if missing:
            raise RuntimeError(f"Missing config: {', '.join(missing)}")
    return config


# --- Pushbullet Notification ---

def notify(config, title, body):
    """Send a push notification via Pushbullet. Fails silently."""
    token = config.get("pushbullet_token")
    if not token:
        return
    try:
        requests.post(
            "https://api.pushbullet.com/v2/pushes",
            headers={"Access-Token": token},
            json={"type": "note", "title": title, "body": body},
            timeout=10,
        )
    except Exception as e:
        print(f"Warning: Pushbullet notification failed: {e}", file=sys.stderr)


def check_token_expiry(config):
    """Warn if BLUETTI token expires within 7 days."""
    load_dotenv(ENV_PATH, override=True)
    expires_at = os.getenv("BLUETTI_TOKEN_EXPIRES_AT")
    if not expires_at:
        return
    remaining = float(expires_at) - time.time()
    days_left = remaining / 86400
    if days_left <= 7:
        notify(
            config,
            "BLUETTI: トークン期限切れ間近",
            f"BLUETTIのAPIトークンが{days_left:.1f}日後に期限切れになります。\n"
            f"'bluetti_battery.py setup' を実行して再認証してください。",
        )


# --- Looop Denki API ---

def fetch_prices(area="01"):
    """Fetch today's and tomorrow's half-hour electricity prices from Looop API.

    Returns dict with "today" (48 floats), "tomorrow" (48 floats or None),
    "today_level" (48 floats), "tomorrow_level" (48 floats or None).

    Level values per slot: -0.5=でんき日和, 0=なし, 0.5=でんき注意報, 1=でんき警報
    """
    resp = _request_with_retry("GET", LOOOP_API_URL, params={"select_area": area})
    data = resp.json()
    tomorrow_data = data["2"].get("price_data", None)
    tomorrow_level = data["2"].get("level", None)
    return {
        "today": data["1"]["price_data"],
        "tomorrow": tomorrow_data,
        "today_level": data["1"]["level"],
        "tomorrow_level": tomorrow_level,
    }


def get_current_price_info(prices):
    """Get current slot price and rolling 24h-ahead average.

    Uses remaining slots today + enough slots from tomorrow to form
    a 48-slot (24h) window starting from the current slot.

    Returns dict with current_price, average_price, slot_index.
    """
    now = datetime.datetime.now(JST)
    slot_index = now.hour * 2 + (1 if now.minute >= 30 else 0)
    current_price = prices["today"][slot_index]

    remaining_today = prices["today"][slot_index:]
    tomorrow = prices["tomorrow"] if prices["tomorrow"] is not None else []
    needed_from_tomorrow = 48 - len(remaining_today)
    window = remaining_today + tomorrow[:needed_from_tomorrow]
    average_price = sum(window) / len(window)

    current_level = prices["today_level"][slot_index]

    return {
        "current_price": current_price,
        "average_price": average_price,
        "slot_index": slot_index,
        "window_slots": len(window),
        "window_prices": window,
        "tomorrow_available": prices["tomorrow"] is not None,
        "current_level": current_level,
        "is_denki_biyori": current_level <= -0.5,
    }


def calculate_slots_needed(soc, soc_max, consumption_rate, charge_rate_per_slot, window_size):
    """Calculate how many charge slots are needed in the next 24h.

    Args:
        soc: Current SOC %.
        soc_max: Target max SOC %.
        consumption_rate: Battery consumption in %/hour.
        charge_rate_per_slot: SOC % gained per 30-min charge slot.
        window_size: Number of slots in the price window.

    Returns number of slots to charge (0..window_size).
    """
    soc_gap = max(0, soc_max - soc)
    window_hours = window_size * 0.5
    total_consumption = consumption_rate * window_hours
    total_charge_needed = soc_gap + total_consumption

    net_charge_per_slot = charge_rate_per_slot - (consumption_rate * 0.5)
    if net_charge_per_slot <= 0:
        return window_size

    slots = int(-(-total_charge_needed // net_charge_per_slot))  # ceil division
    return min(slots, window_size)


def get_cheapest_slots(window_prices, n):
    """Return set of indices of the N cheapest slots in the price window.

    Ties are broken by earlier slot index (prefer charging sooner).
    """
    if n <= 0:
        return set()
    if n >= len(window_prices):
        return set(range(len(window_prices)))
    indexed = [(price, i) for i, price in enumerate(window_prices)]
    indexed.sort(key=lambda x: (x[0], x[1]))
    return {i for _, i in indexed[:n]}


# --- SwitchBot Plug Mini API ---

def switchbot_headers(token, secret):
    """Generate SwitchBot API v1.1 HMAC-SHA256 authentication headers."""
    t = str(int(round(time.time() * 1000)))
    nonce = str(uuid.uuid4())
    string_to_sign = f"{token}{t}{nonce}"
    sign = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            msg=string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8").upper()
    return {
        "Authorization": token,
        "t": t,
        "sign": sign,
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf-8",
    }


def switchbot_list_devices(config):
    """List all SwitchBot devices."""
    headers = switchbot_headers(config["switchbot_token"], config["switchbot_secret"])
    resp = _request_with_retry("GET", f"{SWITCHBOT_API_BASE}/devices", headers=headers)
    data = resp.json()
    if data.get("statusCode") != 100:
        raise RuntimeError(f"SwitchBot API: {data.get('message')}")
    return data.get("body", {}).get("deviceList", [])


def switchbot_get_status(config):
    """Get Plug Mini current status.

    Returns dict with power ("on"/"off") and weight (watts).
    """
    url = f"{SWITCHBOT_API_BASE}/devices/{config['switchbot_device_id']}/status"
    headers = switchbot_headers(config["switchbot_token"], config["switchbot_secret"])
    resp = _request_with_retry("GET", url, headers=headers)
    data = resp.json()
    if data.get("statusCode") != 100:
        raise RuntimeError(f"SwitchBot API: {data.get('message')}")
    body = data["body"]
    return {"power": body["power"], "weight": body.get("weight", 0)}


def switchbot_set_power(config, turn_on):
    """Set Plug Mini power state. Skips if already in desired state.

    Returns action taken: "turned_on", "turned_off", "already_on", "already_off".
    """
    status = switchbot_get_status(config)
    current_on = status["power"] == "on"

    if turn_on and current_on:
        return "already_on"
    if not turn_on and not current_on:
        return "already_off"

    command = "turnOn" if turn_on else "turnOff"
    url = f"{SWITCHBOT_API_BASE}/devices/{config['switchbot_device_id']}/commands"
    headers = switchbot_headers(config["switchbot_token"], config["switchbot_secret"])
    payload = {
        "command": command,
        "parameter": "default",
        "commandType": "command",
    }
    resp = _request_with_retry("POST", url, headers=headers, json=payload)
    return "turned_on" if turn_on else "turned_off"


# --- BLUETTI Battery ---

def get_battery_soc():
    """Get current battery SOC from BLUETTI API.

    Returns int (0-100) or None if unavailable.
    """
    devices = bluetti_battery.get_devices()
    if not devices:
        return None
    for d in devices:
        for state in d.get("stateList", []):
            if state.get("fnCode") == "SOC":
                return int(state["fnValue"])
    return None


# --- SOC History ---

def _get_db():
    """Open SOC history database, creating table if needed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS soc_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            soc INTEGER NOT NULL,
            charging INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_soc_history_timestamp
        ON soc_history(timestamp)
    """)
    conn.commit()
    return conn


def record_soc(soc, charging=False):
    """Record a SOC reading with timestamp. Prunes records older than 30 days."""
    now = datetime.datetime.now(JST)
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO soc_history (timestamp, soc, charging) VALUES (?, ?, ?)",
            (now.isoformat(), soc, int(charging)),
        )
        cutoff = (now - datetime.timedelta(days=30)).isoformat()
        conn.execute("DELETE FROM soc_history WHERE timestamp < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def get_consumption_rate(hours=24):
    """Calculate average battery consumption rate from recent discharge periods.

    Returns rate in %/hour, or None if insufficient data.
    """
    since = (datetime.datetime.now(JST) - datetime.timedelta(hours=hours)).isoformat()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, soc, charging FROM soc_history "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return None

    rates = []
    for i in range(1, len(rows)):
        prev_ts, prev_soc, prev_charging = rows[i - 1]
        curr_ts, curr_soc, curr_charging = rows[i]

        if prev_charging or curr_charging:
            continue

        delta_soc = prev_soc - curr_soc
        if delta_soc <= 0:
            continue

        prev_dt = datetime.datetime.fromisoformat(prev_ts)
        curr_dt = datetime.datetime.fromisoformat(curr_ts)
        delta_hours = (curr_dt - prev_dt).total_seconds() / 3600

        if delta_hours <= 0 or delta_hours > 2:
            continue

        rates.append(delta_soc / delta_hours)

    if not rates:
        return None

    return sum(rates) / len(rates)


def get_soc_history(hours=24):
    """Fetch SOC history records for the given number of hours."""
    since = (datetime.datetime.now(JST) - datetime.timedelta(hours=hours)).isoformat()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, soc, charging FROM soc_history "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {"timestamp": ts, "soc": soc, "charging": bool(charging)}
        for ts, soc, charging in rows
    ]


# --- Charge Decision ---

def decide_charge(soc, price_info, config, consumption_rate=None):
    """Decide whether to charge based on SOC and cheapest-N-slots strategy.

    Priority:
      1. SOC <= soc_min -> force charge
      2. SOC >= soc_max -> stop charge
      3. Calculate needed charge slots from consumption rate and SOC gap,
         then charge only during the cheapest N slots in the 24h window.

    Returns dict with charge (bool), reason (str), and slots_needed (int or None).
    """
    soc_min = config["soc_min"]
    soc_max = config["soc_max"]

    if soc <= soc_min:
        return {"charge": True, "reason": f"SOC {soc}% <= {soc_min}% (force charge)", "slots_needed": None}
    if soc >= 100 and price_info.get("is_denki_biyori"):
        return {"charge": False, "reason": f"SOC {soc}% = 100% (battery full, stop charge even on でんき日和)", "slots_needed": None}
    if soc >= soc_max and not price_info.get("is_denki_biyori"):
        return {"charge": False, "reason": f"SOC {soc}% >= {soc_max}% (stop charge)", "slots_needed": None}
    if soc >= soc_max and price_info.get("is_denki_biyori"):
        return {"charge": True, "reason": f"SOC {soc}% >= {soc_max}% but でんき日和 (charge to 100%)", "slots_needed": None}

    rate = consumption_rate if consumption_rate is not None else config["default_consumption_rate"]
    window_prices = price_info["window_prices"]
    slots_needed = calculate_slots_needed(
        soc, soc_max, rate,
        config["charge_rate_pct_per_slot"],
        len(window_prices),
    )

    if slots_needed == 0:
        return {
            "charge": False,
            "reason": f"SOC {soc}% near target, no charge needed (N=0)",
            "slots_needed": 0,
        }

    cheapest = get_cheapest_slots(window_prices, slots_needed)
    # Slot 0 in the window is the current slot
    is_cheap_slot = 0 in cheapest

    if is_cheap_slot:
        return {
            "charge": True,
            "reason": (
                f"price {price_info['current_price']:.2f} is in cheapest {slots_needed}"
                f"/{len(window_prices)} slots (charge)"
            ),
            "slots_needed": slots_needed,
        }
    return {
        "charge": False,
        "reason": (
            f"price {price_info['current_price']:.2f} is NOT in cheapest {slots_needed}"
            f"/{len(window_prices)} slots (wait)"
        ),
        "slots_needed": slots_needed,
    }


# --- Commands ---

def cmd_run(args):
    """Main charge control logic."""
    now = datetime.datetime.now(JST)
    print(f"=== charge_controller {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    config = load_config(require_switchbot=not args.dry_run)

    try:
        soc = get_battery_soc()
        if soc is None:
            raise RuntimeError("Could not get battery SOC")
        print(f"Battery SOC: {soc}%")

        prices = fetch_prices(config["looop_area"])
        price_info = get_current_price_info(prices)
        print(
            f"Current price: {price_info['current_price']:.2f} yen/kWh"
            f" (slot {price_info['slot_index']}/47)"
        )
        tomorrow_tag = "" if price_info["tomorrow_available"] else " [tomorrow N/A]"
        print(
            f"Average price: {price_info['average_price']:.2f} yen/kWh"
            f" ({price_info['window_slots']}/48 slots){tomorrow_tag}"
        )
        if price_info["is_denki_biyori"]:
            print("でんき予報: でんき日和 (充電上限を100%に拡張)")

        consumption_rate = get_consumption_rate()
        if consumption_rate is not None:
            print(f"Consumption rate: {consumption_rate:.1f} %/h (from history)")
        else:
            print(f"Consumption rate: {config['default_consumption_rate']:.1f} %/h (default)")

        decision = decide_charge(soc, price_info, config, consumption_rate)
        slots_info = f" [N={decision['slots_needed']}]" if decision["slots_needed"] is not None else ""
        print(
            f"Decision: {'CHARGE ON' if decision['charge'] else 'CHARGE OFF'}"
            f" - {decision['reason']}{slots_info}"
        )

        try:
            record_soc(soc, charging=(decision["charge"] and not args.dry_run))
        except Exception as e:
            print(f"Warning: Failed to record SOC history: {e}", file=sys.stderr)

        if args.dry_run:
            print("[DRY RUN] Skipping plug control")
        else:
            action = switchbot_set_power(config, decision["charge"])
            print(f"Plug action: {action}")

        check_token_expiry(config)
        print("=== done ===")

    except Exception as e:
        import traceback
        print(f"FAIL-SAFE: Error occurred: {e}", file=sys.stderr)
        traceback.print_exc()
        notify(config, "BLUETTI: エラー発生", f"フェイルセーフが発動しました。\n{e}")
        if not args.dry_run:
            try:
                action = switchbot_set_power(config, True)
                print(f"FAIL-SAFE: Charger turned ON ({action})")
            except Exception as e2:
                print(f"FAIL-SAFE: Could not control plug: {e2}", file=sys.stderr)
                notify(config, "BLUETTI: プラグ制御失敗", f"フェイルセーフでプラグを制御できませんでした。\n{e2}")
        print("=== done (fail-safe) ===")


def cmd_list_devices(args):
    """List SwitchBot devices to find Plug Mini device ID."""
    load_dotenv(ENV_PATH, override=True)
    token = os.getenv("SWITCHBOT_TOKEN")
    secret = os.getenv("SWITCHBOT_SECRET")
    if not token or not secret:
        raise RuntimeError("SWITCHBOT_TOKEN and SWITCHBOT_SECRET required")

    config = {"switchbot_token": token, "switchbot_secret": secret}
    devices = switchbot_list_devices(config)
    if not devices:
        print("No devices found.")
        return

    for d in devices:
        name = d.get("deviceName", "Unknown")
        dtype = d.get("deviceType", "Unknown")
        did = d.get("deviceId", "N/A")
        print(f"  {name} ({dtype})  ID: {did}")


def cmd_prices(args):
    """Show today's electricity prices."""
    load_dotenv(ENV_PATH, override=True)
    area = os.getenv("LOOOP_AREA", "01")
    prices = fetch_prices(area)
    price_info = get_current_price_info(prices)

    print(f"Area: {area}")
    print(f"Average (24h ahead): {price_info['average_price']:.2f} yen/kWh")
    print(f"Current slot ({price_info['slot_index']}): {price_info['current_price']:.2f} yen/kWh")
    print()
    print("Time             Price   vs Avg")
    print("-" * 40)

    now = datetime.datetime.now(JST)
    current_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
    avg = price_info["average_price"]

    for i, price in enumerate(prices["today"]):
        h = i // 2
        m = "30" if i % 2 else "00"
        marker = " <--" if i == current_slot else ""
        diff = "cheap" if price <= avg else "HIGH"
        print(f"  {h:02d}:{m}  {price:8.2f}  {diff:>5}{marker}")


def cmd_history(args):
    """Show SOC history and consumption stats."""
    hours = args.hours
    records = get_soc_history(hours)

    if not records:
        print(f"No SOC history in the last {hours} hours.")
        return

    rate = get_consumption_rate(hours)

    print(f"SOC history (last {hours}h): {len(records)} records")
    if rate is not None:
        print(f"Avg consumption rate: {rate:.1f} %/hour ({rate * 0.5:.1f} %/slot)")
    else:
        print("Avg consumption rate: insufficient data")
    print()

    print("Time              SOC   Status")
    print("-" * 40)
    for r in records:
        dt = datetime.datetime.fromisoformat(r["timestamp"])
        time_str = dt.strftime("%m-%d %H:%M")
        status = "CHG" if r["charging"] else "   "
        bar = "#" * (r["soc"] // 5)
        print(f"  {time_str}  {r['soc']:3d}%  {status}  |{bar}")


def main():
    parser = argparse.ArgumentParser(description="BLUETTI smart charge controller")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run charge control (default)")
    run_parser.add_argument(
        "--dry-run", action="store_true", help="Show decision without controlling plug"
    )

    sub.add_parser("list-devices", help="List SwitchBot devices")
    sub.add_parser("prices", help="Show today's electricity prices")

    history_parser = sub.add_parser("history", help="Show SOC history and stats")
    history_parser.add_argument(
        "--hours", type=int, default=24, help="Hours of history to show (default: 24)"
    )

    args = parser.parse_args()

    if args.command == "list-devices":
        cmd_list_devices(args)
    elif args.command == "prices":
        cmd_prices(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "history":
        cmd_history(args)
    else:
        # Default to run with dry-run when no command given
        parser.print_help()


if __name__ == "__main__":
    main()
