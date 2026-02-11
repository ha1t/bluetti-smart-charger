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
from dotenv import load_dotenv

import bluetti_battery

ENV_PATH = Path(__file__).parent / ".env"
DB_PATH = Path(__file__).parent / "soc_history.db"

LOOOP_API_URL = "https://looop-denki.com/api/prices"
SWITCHBOT_API_BASE = "https://api.switch-bot.com/v1.1"


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
    }
    if require_switchbot:
        required = ["switchbot_token", "switchbot_secret", "switchbot_device_id"]
        missing = [k for k in required if not config[k]]
        if missing:
            print(f"Error: Missing config: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
    return config


# --- Looop Denki API ---

def fetch_prices(area="01"):
    """Fetch today's and tomorrow's half-hour electricity prices from Looop API.

    Returns dict with "today" (48 floats) and "tomorrow" (48 floats).
    """
    resp = requests.get(LOOOP_API_URL, params={"select_area": area}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "today": data["1"]["price_data"],
        "tomorrow": data["2"]["price_data"],
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
    needed_from_tomorrow = 48 - len(remaining_today)
    window = remaining_today + prices["tomorrow"][:needed_from_tomorrow]
    average_price = sum(window) / len(window)

    return {
        "current_price": current_price,
        "average_price": average_price,
        "slot_index": slot_index,
    }


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
    resp = requests.get(
        f"{SWITCHBOT_API_BASE}/devices", headers=headers, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("statusCode") != 100:
        print(f"Error: SwitchBot API: {data.get('message')}", file=sys.stderr)
        sys.exit(1)
    return data.get("body", {}).get("deviceList", [])


def switchbot_get_status(config):
    """Get Plug Mini current status.

    Returns dict with power ("on"/"off") and weight (watts).
    """
    url = f"{SWITCHBOT_API_BASE}/devices/{config['switchbot_device_id']}/status"
    headers = switchbot_headers(config["switchbot_token"], config["switchbot_secret"])
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("statusCode") != 100:
        print(f"Error: SwitchBot API: {data.get('message')}", file=sys.stderr)
        sys.exit(1)
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
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
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

def decide_charge(soc, current_price, average_price, soc_min, soc_max):
    """Decide whether to charge based on SOC and electricity price.

    Priority:
      1. SOC <= soc_min -> force charge
      2. SOC >= soc_max -> stop charge
      3. price <= average -> charge
      4. price > average -> stop

    Returns dict with charge (bool) and reason (str).
    """
    if soc <= soc_min:
        return {"charge": True, "reason": f"SOC {soc}% <= {soc_min}% (force charge)"}
    if soc >= soc_max:
        return {"charge": False, "reason": f"SOC {soc}% >= {soc_max}% (stop charge)"}
    if current_price <= average_price:
        return {
            "charge": True,
            "reason": f"price {current_price:.2f} <= avg {average_price:.2f} (cheap)",
        }
    return {
        "charge": False,
        "reason": f"price {current_price:.2f} > avg {average_price:.2f} (expensive)",
    }


# --- Commands ---

def cmd_run(args):
    """Main charge control logic."""
    now = datetime.datetime.now(JST)
    print(f"=== charge_controller {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    config = load_config(require_switchbot=not args.dry_run)

    soc = get_battery_soc()
    if soc is None:
        print("Error: Could not get battery SOC", file=sys.stderr)
        sys.exit(1)
    print(f"Battery SOC: {soc}%")

    prices = fetch_prices(config["looop_area"])
    price_info = get_current_price_info(prices)
    print(
        f"Current price: {price_info['current_price']:.2f} yen/kWh"
        f" (slot {price_info['slot_index']}/47)"
    )
    print(f"Average price: {price_info['average_price']:.2f} yen/kWh")

    decision = decide_charge(
        soc,
        price_info["current_price"],
        price_info["average_price"],
        config["soc_min"],
        config["soc_max"],
    )
    print(
        f"Decision: {'CHARGE ON' if decision['charge'] else 'CHARGE OFF'}"
        f" - {decision['reason']}"
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

    print("=== done ===")


def cmd_list_devices(args):
    """List SwitchBot devices to find Plug Mini device ID."""
    load_dotenv(ENV_PATH, override=True)
    token = os.getenv("SWITCHBOT_TOKEN")
    secret = os.getenv("SWITCHBOT_SECRET")
    if not token or not secret:
        print("Error: SWITCHBOT_TOKEN and SWITCHBOT_SECRET required", file=sys.stderr)
        sys.exit(1)

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
