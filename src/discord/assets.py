"""
Resolves raw Roblox CDN image URLs into Discord-usable Rich Presence image
values.

Per Discord's own docs, a raw https:// URL is NOT directly usable as an
activity image, and neither is a hand-crafted "mp:external/https/..."
string (an earlier, incorrect attempt at this). The real mechanism: POST
the URLs to /applications/{client_id}/external-assets, which returns a
server-signed external_asset_path -- only THAT, prefixed with "mp:", is a
valid image value. The endpoint accepts at most 2 URLs per call, which
conveniently matches our large_image + small_image.
"""

import requests

from src.core.logging_setup import get_logger

log = get_logger("discord_assets")

EXTERNAL_ASSETS_URL = "https://discord.com/api/v10/applications/{client_id}/external-assets"


def proxy_image_urls(access_token: str, client_id: str, urls: list) -> dict:
    """Returns {original_url: 'mp:<external_asset_path>'} for URLs that were
    successfully proxied. URLs that fail are simply absent from the result --
    callers should treat a missing key as 'omit this image'."""
    urls = [u for u in urls if u][:2]
    if not urls:
        return {}

    log.debug("proxying %d image URL(s) through Discord external-assets: %s", len(urls), urls)
    try:
        resp = requests.post(
            EXTERNAL_ASSETS_URL.format(client_id=client_id),
            headers={"Authorization": f"Bearer {access_token}"},
            json={"urls": urls},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("network failure proxying image URLs, images will be omitted this cycle: %s", e)
        return {}

    log.debug("external-assets proxy -> HTTP %d, body=%s", resp.status_code, resp.text[:500])
    if resp.status_code != 200:
        log.warning(
            "Discord rejected the external-assets proxy request (HTTP %d) -- images will be "
            "omitted this cycle. Body: %s", resp.status_code, resp.text[:300],
        )
        return {}

    mapping = {}
    for item in resp.json():
        original = item.get("url")
        path = item.get("external_asset_path")
        if original and path:
            mapping[original] = "mp:" + path
        else:
            log.warning("proxy response missing expected fields for one URL: %s", item)
    return mapping
