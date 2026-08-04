"""Notify proxy server to reload keys after admin changes."""

from __future__ import annotations

# Reload target configured by AdminServer at startup.
# Defaults reflect the most common VPS setup (TLS proxy on 8443).
_RELOAD_PORT: int = 8443
_RELOAD_USE_TLS: bool = True


def configure_reload_target(port: int, use_tls: bool) -> None:
    """Set the proxy reload endpoint target. Called once by AdminServer.app()."""
    global _RELOAD_PORT, _RELOAD_USE_TLS
    _RELOAD_PORT = port
    _RELOAD_USE_TLS = use_tls


async def notify_proxy_reload() -> None:
    """Notify the proxy server to reload its keys/groups from the store.

    Tries the configured scheme first, then falls back to the other so it
    works whether the proxy is HTTP or HTTPS.
    """
    import aiohttp
    from loguru import logger

    port = _RELOAD_PORT
    schemes = ["https", "http"] if _RELOAD_USE_TLS else ["http", "https"]
    last_err: Exception | None = None
    for scheme in schemes:
        url = f"{scheme}://127.0.0.1:{port}/api/reload"
        ssl_arg = False if scheme == "https" else None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    ssl=ssl_arg,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        logger.debug(f"proxy reloaded via {url}")
                        return
                    logger.warning(f"proxy reload via {url} returned {resp.status}")
                    return
        except Exception as e:
            last_err = e
            continue
    logger.warning(f"failed to notify proxy reload on port {port}: {last_err}")
