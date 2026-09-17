"""
Precio de un token específico (no SOL/USD ni ETH/USD -- ver
core/sol_price.py y core/eth_price.py para eso).

Se usa para: 1) el job de outcome-tracking de core/brain.py (precio
5m/30m/1h después de cada alerta) y 2) core/paper_trading.py (precio
al abrir/cerrar una posición simulada).

Fuente primaria: API pública de DexScreener (gratis, sin key). Probada
contra un token real de Pump.fun el 2026-09-13 -- devuelve priceNative
(precio en la moneda "quote" del par -- SOL en Solana, ETH en cadenas
EVM) y priceUsd.

PROBLEMA REAL ENCONTRADO (2026-09-13): DexScreener todavía NO tiene
indexada Robinhood Chain (lanzada hace solo 2 meses) -- devuelve
`{"pairs": null}` para tokens reales de ahí, confirmado en vivo. Sin
fallback, esto rompía tanto el paper trading como el job de
outcome-tracking para esa cadena. `fetch_current_price()` de abajo
resuelve esto cayendo al último precio que NOSOTROS MISMOS medimos
on-chain (tabla `transactions`) cuando DexScreener no tiene nada --
sigue siendo un dato real, medido, nunca inventado.
"""
import logging

import httpx

from core.db import get_conn, get_last_transaction_price

logger = logging.getLogger("hunter.token_price")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{token_address}"


async def fetch_token_price_sol(token_address: str) -> float | None:
    """
    Precio actual del token según DexScreener, en la unidad "quote" del
    par (SOL para Solana, ETH para cadenas EVM que DexScreener soporte).
    Si el token tiene varios pares, se toma el de mayor volumen 24h.
    Devuelve None si no se pudo obtener o si DexScreener no tiene la
    cadena indexada (nunca inventa un precio).
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                DEXSCREENER_URL.format(token_address=token_address), timeout=10
            )
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"No se pudo obtener precio de {token_address}: {e}")
            return None

    if not pairs:
        return None

    best_pair = max(pairs, key=lambda p: (p.get("volume") or {}).get("h24") or 0)
    try:
        return float(best_pair["priceNative"])
    except (KeyError, TypeError, ValueError):
        return None


async def fetch_current_price(chain: str, token_address: str) -> float | None:
    """
    Punto de entrada recomendado (usado por paper_trading.py y
    outcome_tracker.py): intenta DexScreener primero, y si no hay nada
    (cadena no indexada todavía, como Robinhood Chain hoy), cae al
    último precio que el propio listener de esa cadena registró en
    `transactions`. None solo si ninguna de las dos fuentes tiene dato.
    """
    price = await fetch_token_price_sol(token_address)
    if price is not None:
        return price

    with get_conn() as conn:
        fallback = get_last_transaction_price(conn, chain, token_address)

    if fallback is not None:
        logger.info(
            f"DexScreener sin datos para {token_address} ({chain}) -- "
            f"usando último precio propio registrado: {fallback}"
        )
    return fallback
