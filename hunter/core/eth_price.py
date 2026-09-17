"""
Precio de ETH/USD en tiempo (casi) real -- ídem core/sol_price.py, pero
para Robinhood Chain, donde el gas y el "quote token" de los swaps de
Pons es ETH (ver chains/robinhood.py).
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("hunter.eth_price")

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
REFRESH_INTERVAL_SECONDS = 30

_cached_price: float | None = None


def get_cached_eth_usd() -> float | None:
    return _cached_price


async def fetch_eth_usd() -> float | None:
    global _cached_price
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                COINGECKO_URL,
                params={"ids": "ethereum", "vs_currencies": "usd"},
                timeout=10,
            )
            resp.raise_for_status()
            price = resp.json()["ethereum"]["usd"]
            _cached_price = float(price)
            return _cached_price
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            logger.warning(f"No se pudo obtener el precio de ETH/USD: {e}")
            return None


async def refresh_loop(interval_seconds: int = REFRESH_INTERVAL_SECONDS):
    while True:
        await fetch_eth_usd()
        await asyncio.sleep(interval_seconds)
