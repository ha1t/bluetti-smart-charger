#!/usr/bin/env python3
"""BLUETTI battery status CLI tool.

Retrieves battery SOC and device status from BLUETTI cloud API.
Based on the protocol used by the official Home Assistant integration.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

import requests
from dotenv import load_dotenv, set_key

# BLUETTI API endpoints (from official HA integration source)
SSO_BASE = "https://sso.bluettipower.com"
GATEWAY_BASE = "https://gw.bluettipower.com"
AUTHORIZE_URL = f"{SSO_BASE}/oauth2/grant"
TOKEN_URL = f"{SSO_BASE}/oauth2/token"
DEVICES_URL = f"{GATEWAY_BASE}/api/bluiotdata/ha/v1/devices"
DEVICE_STATES_URL = f"{GATEWAY_BASE}/api/bluiotdata/ha/v1/deviceStates"

# OAuth client credentials (hardcoded in official HA integration)
CLIENT_ID = "HomeAssistant"
CLIENT_SECRET = "SG9tZUFzc2lzdGFudA=="

ENV_PATH = Path(__file__).parent / ".env"


def load_tokens():
    """Load tokens from .env file."""
    load_dotenv(ENV_PATH, override=True)
    access_token = os.getenv("BLUETTI_ACCESS_TOKEN")
    refresh_token = os.getenv("BLUETTI_REFRESH_TOKEN")
    expires_at = os.getenv("BLUETTI_TOKEN_EXPIRES_AT")
    if not access_token or not refresh_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": float(expires_at) if expires_at else 0,
    }


def save_tokens(access_token, refresh_token, expires_in):
    """Save tokens to .env file."""
    ENV_PATH.touch(exist_ok=True)
    expires_at = str(time.time() + expires_in)
    set_key(str(ENV_PATH), "BLUETTI_ACCESS_TOKEN", access_token)
    set_key(str(ENV_PATH), "BLUETTI_REFRESH_TOKEN", refresh_token)
    set_key(str(ENV_PATH), "BLUETTI_TOKEN_EXPIRES_AT", expires_at)


def refresh_access_token(refresh_token):
    """Refresh the access token using the refresh token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    save_tokens(data["access_token"], data["refresh_token"], data["expires_in"])
    return data["access_token"]


def get_access_token():
    """Get a valid access token, refreshing if necessary."""
    tokens = load_tokens()
    if not tokens:
        print("Error: Not authenticated. Run 'setup' first.", file=sys.stderr)
        sys.exit(1)

    # Refresh if expired (with 60s buffer)
    if time.time() >= tokens["expires_at"] - 60:
        try:
            return refresh_access_token(tokens["refresh_token"])
        except requests.HTTPError as e:
            print(f"Error: Token refresh failed: {e}", file=sys.stderr)
            print("Run 'setup' to re-authenticate.", file=sys.stderr)
            sys.exit(1)

    return tokens["access_token"]


def api_get(url, params=None):
    """Make authenticated GET request to BLUETTI API."""
    token = get_access_token()
    resp = requests.get(
        url,
        headers={"Authorization": token},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("msgCode") != 0:
        print(f"Error: API returned msgCode={data.get('msgCode')}", file=sys.stderr)
        sys.exit(1)
    return data.get("data")


# --- OAuth callback server ---

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler to capture OAuth redirect."""

    authorization_code = None

    def do_GET(self, *_args, **_kwargs):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = params.get("code", [None])[0]

        if code:
            OAuthCallbackHandler.authorization_code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "認証成功! このタブを閉じてください。".encode()
            )
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            error = params.get("error", ["unknown"])[0]
            self.wfile.write(f"認証エラー: {error}".encode())

    def log_message(self, *_args, **_kwargs):
        pass  # Suppress request logs


def find_free_port():
    """Find a free port for the callback server."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# --- Commands ---

def cmd_setup(args):
    """Interactive OAuth setup."""
    # Use a fixed redirect URI that won't actually be served.
    # The user will paste back the redirected URL from their browser.
    redirect_uri = "http://localhost:12345/callback"

    auth_url = (
        f"{AUTHORIZE_URL}?"
        f"client_id={CLIENT_ID}&"
        f"response_type=code&"
        f"redirect_uri={urllib.parse.quote(redirect_uri)}"
    )

    print("1) Open the following URL in your browser:")
    print()
    print(f"   {auth_url}")
    print()
    print("2) Log in with your BLUETTI account.")
    print("3) After login, the browser will redirect to a URL starting with")
    print(f"   {redirect_uri}?code=...")
    print("   (The page won't load - that's expected.)")
    print("4) Copy the ENTIRE URL from the browser address bar and paste it here.")
    print()

    redirected_url = input("Paste URL here: ").strip()

    query = urllib.parse.urlparse(redirected_url).query
    params = urllib.parse.parse_qs(query)
    code = params.get("code", [None])[0]

    if not code:
        print("Error: Could not extract authorization code from URL.", file=sys.stderr)
        sys.exit(1)

    print("Authorization code received. Exchanging for tokens...")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "access_token" not in data:
        print(f"Error: Unexpected token response: {data}", file=sys.stderr)
        sys.exit(1)

    save_tokens(data["access_token"], data["refresh_token"], data["expires_in"])
    print(f"Setup complete! Tokens saved to {ENV_PATH}")


def get_devices():
    """Fetch device list with state from BLUETTI API."""
    return api_get(DEVICES_URL) or []


def cmd_devices(_args):
    """List registered BLUETTI devices."""
    devices = get_devices()
    if not devices:
        print("No devices found.")
        return

    for d in devices:
        model = d.get("model", "Unknown")
        sn = d.get("sn", "N/A")
        name = d.get("name", "")
        online = d.get("online", "0")
        status = "online" if online == "1" else "offline"
        label = f"{name} ({model})" if name else model
        print(f"  {label}  SN: {sn}  [{status}]")


def cmd_status(args):
    """Show battery status for all devices."""
    devices = get_devices()
    if not devices:
        print("No devices found.")
        return

    results = []
    for d in devices:
        sn = d.get("sn", "N/A")
        model = d.get("model", "Unknown")
        name = d.get("name", "")
        online = d.get("online", "0")

        state_list = d.get("stateList", [])
        soc = None
        for state in state_list:
            if state.get("fnCode") == "SOC":
                soc = state.get("fnValue")
                break

        result = {
            "sn": sn,
            "model": model,
            "name": name,
            "online": online == "1",
            "battery_percent": int(soc) if soc is not None else None,
        }
        results.append(result)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            label = f"{r['name']} ({r['model']})" if r["name"] else r["model"]
            soc_str = f"{r['battery_percent']}%" if r["battery_percent"] is not None else "N/A"
            online_str = "online" if r["online"] else "offline"
            print(f"  {label}  SN: {r['sn']}  Battery: {soc_str}  [{online_str}]")


def main():
    parser = argparse.ArgumentParser(description="BLUETTI battery status tool")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Authenticate with BLUETTI account")
    sub.add_parser("devices", help="List registered devices")

    status_parser = sub.add_parser("status", help="Show battery status")
    status_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    commands = {
        "setup": cmd_setup,
        "devices": cmd_devices,
        "status": cmd_status,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
