"""Notify proxy server to reload keys after admin changes."""
from __future__ import annotations


async def notify_proxy_reload(proxy_port: int = 8088) -> None:
    """Notify the proxy server to reload its keys from the store."""
    import aiohttp
    from loguru import logger
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{proxy_port}/api/reload") as resp:
                if resp.status == 200:
                    logger.debug("proxy reloaded keys successfully")
                else:
                    logger.warning(f"proxy reload returned {resp.status}")
    except Exception as e:
        logger.warning(f"failed to notify proxy reload: {e}")