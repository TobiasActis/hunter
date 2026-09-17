"""
Precio de SOL/USD en tiempo (casi) real.

PumpPortal manda los montos de trade en SOL, no en USD (ver
chains/solana_pumpportal.py), así que necesitamos este precio para
poder calcular `amount_usd` y aplicar el filtro `MIN_TRADE_USD`.

Fuente: API pública de CoinGecko (gratis, sin API key, rate limit
generoso para una sola consulta cada N segundos). Si en el futuro esto
rate-limitea en producción, la alternativa es el endpoint de Binance
(https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT).

Se cachea en memoria y se refresca con una tarea de fondo -- el parser
del WebSocket (`_parse_message`, que es sync) no puede hacer un await
por cada mensaje, así que lee el último valor cacheado.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("hunter.sol_price")

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
REFRESH_INTERVAL_SECONDS = 30

_cached_price: float | None = None


def get_cached_sol_usd() -> float | None:
    """Lectura sync del último precio conocido. None si todavía no se pudo obtener ninguno."""
    return _cached_price


async def fetch_sol_usd() -> float | None:
    """Un solo pedido a CoinGecko. Devuelve None si falla (nunca inventa un precio)."""
    global _cached_price
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                COINGECKO_URL,
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=10,
            )
            resp.raise_for_status()
            price = resp.json()["solana"]["usd"]
            _cached_price = float(price)
            return _cached_price
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            logger.warning(f"No se pudo obtener el precio de SOL/USD: {e}")
            return None


async def refresh_loop(interval_seconds: int = REFRESH_INTERVAL_SECONDS):
    """Tarea de fondo: refresca el precio cacheado cada `interval_seconds`."""
    while True:
        await fetch_sol_usd()
        await asyncio.sleep(interval_seconds)
