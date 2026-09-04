#!/usr/bin/env python3
"""
Entry point. Run this from the repo root:

    python run.py

First run:
  - creates config.json under ~/.config/ropysence/ and exits so
    you can set discord_client_id
  - second run prompts for your Roblox .ROBLOSECURITY cookie (hidden
    input), verifies it, and stores it encrypted for future runs
  - opens a browser for Discord authorization (PKCE, no password/token
    ever touches this script) and stores the resulting tokens encrypted too

Every run after that reuses both, silently, until you delete them --
see src/core/secure_store.py (SecureStore().delete("roblox_cookie") /
.delete("discord_tokens")), or just delete the config dir.
"""

import os
import sys

# Add the repo root to sys.path so `src` resolves as a package regardless of
# the current working directory this is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.ropysence.app import main

if __name__ == "__main__":
    main()
