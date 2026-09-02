"""
Wires everything together: options.txt -> Roblox cookie -> Discord OAuth ->
Gateway loop. Called from run.py at the repo root.
"""

import asyncio
import getpass
import sys

from src.core.logging_setup import setup_logging, apply_options, get_logger
from src.core.secure_store import SecureStore
from src.core.options import load_options
from src.core.human_webhook import HumanWebhookNotifier
from src.discord.oauth import get_access_token, DEFAULT_SCOPES
from src.discord.gateway import run_gateway_with_reconnect
from src.roblox.client import RobloxClient, RobloxAuthError
from src.roblox.presence_builder import PresenceBuilder

setup_logging()
log = get_logger("app")

ROBLOX_COOKIE_KEY = "roblox_cookie"


def get_roblox_client(store: SecureStore, worker_pool_size: int = 4):
    cookie = store.get(ROBLOX_COOKIE_KEY)
    freshly_entered = False

    if cookie:
        log.info("Roblox cookie loaded from secure store")
    else:
        log.info("no stored Roblox cookie found, prompting for one")
        print(
            "Paste your .ROBLOSECURITY cookie value (input hidden).\n"
            "WARNING: this cookie grants full access to your Roblox account "
            "(trading, purchases, account settings). Never share it or paste "
            "it anywhere else. If you ever suspect it leaked, log out all "
            "sessions in Roblox account settings immediately."
        )
        cookie = getpass.getpass("Cookie: ").strip()
        freshly_entered = True
        if not cookie:
            log.error("no cookie entered, aborting")
            sys.exit(1)

    client = RobloxClient(cookie, user_agent="Mozilla/5.0", max_workers=worker_pool_size)

    try:
        user = client.get_authenticated_user()
    except RobloxAuthError as e:
        log.error("Roblox authentication failed: %s", e)
        if not freshly_entered:
            log.warning("stored cookie appears invalid -- removing it, rerun and paste a fresh one")
            store.delete(ROBLOX_COOKIE_KEY)
        sys.exit(1)

    if freshly_entered:
        store.set(ROBLOX_COOKIE_KEY, cookie)
        log.info("Roblox cookie encrypted and stored for future runs")

    return client, user


def main():
    log.info("starting ropysence")
    options = load_options()
    apply_options(options)

    store = SecureStore()
    client_id = options["script.user.id"]

    roblox_client, roblox_user = get_roblox_client(store)

    def get_token():
        # Cheap to call repeatedly -- returns the cached token when still
        # valid, only does real work (refresh or full re-auth) when needed.
        # Called fresh on every reconnect attempt AND every image-proxy
        # call, so a mid-run token refresh is always picked up automatically.
        return get_access_token(client_id, DEFAULT_SCOPES, options["script.localhost.port"], store)

    human_notifier = HumanWebhookNotifier(
        webhook_urls=options["human.discord.webhook"],
        alias=options["script.dev.alias"],
    )

    builder = PresenceBuilder(
        roblox=roblox_client,
        user=roblox_user,
        options=options,
        get_access_token_fn=get_token,
        client_id=client_id,
        human_notifier=human_notifier,
    )

    poll_interval = options["script.interval"]
    log.info(
        "entering main loop (poll_interval=%ss, status=%s, reconnect=%s)",
        poll_interval, options["rpc.state"], options["script.reconnect.enabled"],
    )

    try:
        asyncio.run(run_gateway_with_reconnect(
            get_access_token_fn=get_token,
            build_activity_fn=builder.build,
            poll_interval=poll_interval,
            status=options["rpc.state"],
            alias=options["script.dev.alias"],
            reconnect_enabled=options["script.reconnect.enabled"],
            base_delay=options["script.reconnect.base_delay"],
            max_delay=options["script.reconnect.max_delay"],
            max_attempts=options["script.reconnect.max_attempts"],
        ))
    except KeyboardInterrupt:
        log.info("interrupted by user, shutting down")
    except Exception as e:
        log.error("fatal error: %s", e)
        sys.exit(1)
    finally:
        roblox_client.close()
        human_notifier.close()
