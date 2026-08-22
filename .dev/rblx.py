#!/usr/bin/env python3
"""
Step 1 of the rebuild: Roblox presence, completely in isolation.
No Discord, no OAuth, no Gateway -- just: paste cookie, verify it, ask
Roblox what your presence is, print the raw response.

Run this while you're actually sitting in a Roblox game, and paste the
full output back. This tells us definitively whether the Roblox side of
things works at all before we wire it into anything else.

Usage:
    pip install requests --break-system-packages
    python roblox_check.py
"""

import getpass
import json
import logging
import sys

import requests

logging.addLevelName(logging.DEBUG, "DBG")
logging.addLevelName(logging.INFO, "INF")
logging.addLevelName(logging.WARNING, "WRN")
logging.addLevelName(logging.ERROR, "ERR")
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger()

AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"


def main():
    print("Paste your .ROBLOSECURITY cookie (input hidden):")
    cookie = getpass.getpass("Cookie: ").strip()
    if not cookie:
        log.error("no cookie entered")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    log.info("verifying cookie...")
    resp = session.get(AUTH_URL, timeout=10)
    log.info("auth check -> HTTP %d", resp.status_code)
    log.debug("auth check body: %s", resp.text)

    if resp.status_code != 200:
        log.error("cookie rejected, stopping here")
        sys.exit(1)

    user = resp.json()
    uid = user["id"]
    print(f"\nAuthenticated as {user.get('name')} (uid={uid})\n")

    log.info("fetching presence for uid=%s ...", uid)
    presence_resp = session.post(PRESENCE_URL, json={"userIds": [uid]}, timeout=10)
    log.info("presence check -> HTTP %d", presence_resp.status_code)

    print("\n--- RAW PRESENCE RESPONSE ---")
    try:
        body = presence_resp.json()
        print(json.dumps(body, indent=2))
    except Exception:
        print("NOT JSON:", presence_resp.text)
        sys.exit(1)
    print("--- END RAW RESPONSE ---\n")

    presences = body.get("userPresences", [])
    if not presences:
        print(">> userPresences array is EMPTY.")
    else:
        ptype = presences[0].get("userPresenceType")
        labels = {0: "Offline", 1: "Online (website)", 2: "In Game", 3: "In Studio", 4: "Invisible"}
        print(f">> Parsed status: {labels.get(ptype, f'unknown type {ptype}')}")


if __name__ == "__main__":
    main()
