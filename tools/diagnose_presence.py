#!/usr/bin/env python3
"""
Fast debug loop for the "why does it always say Offline" question -- talks
to Roblox ONLY, skips Discord/OAuth entirely, so you can re-run this in a
couple seconds while you're actually sitting in a game and watch exactly
what Roblox's presence API says in real time.

Run from the repo root:
    python tools/diagnose_presence.py             # uses stored cookie if present
    python tools/diagnose_presence.py --watch      # re-checks every 5s until Ctrl+C
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.logging_setup import setup_logging, get_logger
from src.core.secure_store import SecureStore
from src.roblox.client import RobloxClient, RobloxAuthError

setup_logging()
log = get_logger("diagnose_presence")

PRESENCE_LABELS = {0: "Offline", 1: "Online (website)", 2: "In Game", 3: "In Studio", 4: "Invisible"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="re-check every 5s until Ctrl+C")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    store = SecureStore()
    cookie = store.get("roblox_cookie")
    if not cookie:
        log.error("no stored Roblox cookie found -- run `python run.py` once first, or set one manually via "
                   "SecureStore().set('roblox_cookie', '...')")
        sys.exit(1)

    log.info("using stored cookie (never printed)")
    client = RobloxClient(cookie, user_agent="Mozilla/5.0", max_workers=1)

    try:
        user = client.get_authenticated_user()
    except RobloxAuthError as e:
        log.error("cookie check failed: %s", e)
        sys.exit(1)

    uid = user["id"]
    print(f"\nAuthenticated as {user.get('name')} (uid={uid})\n")

    def check_once():
        raw = client._post_with_csrf("https://presence.roblox.com/v1/presence/users", {"userIds": [uid]})
        print(f"--- {time.strftime('%H:%M:%S')} ---")
        print(f"HTTP status: {raw.status_code}")
        try:
            body = raw.json()
        except Exception:
            print("Response was not JSON:", raw.text[:500])
            return
        print(json.dumps(body, indent=2))

        presences = body.get("userPresences", [])
        if not presences:
            print(">> userPresences array is EMPTY -- Roblox returned no entry for this uid at all.")
            return
        ptype = presences[0].get("userPresenceType")
        label = PRESENCE_LABELS.get(ptype, f"unknown type {ptype}")
        print(f">> Parsed status: {label}")
        if ptype == 0:
            print(
                ">> Roblox itself is reporting Offline for this account right now. If you believe "
                "that's wrong while actively playing, this is the ground truth to compare against -- "
                "the same JSON above is exactly what the main app sees. Things worth checking: "
                "account privacy setting for online visibility, whether this cookie belongs to the "
                "session that's actually in-game, or (rarely) a few seconds of caching lag right "
                "after joining."
            )
        print()

    if args.watch:
        log.info("watch mode -- checking every %.1fs, Ctrl+C to stop", args.interval)
        try:
            while True:
                check_once()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopped")
    else:
        check_once()

    client.close()


if __name__ == "__main__":
    main()
