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

from datetime import datetime, timezone

from core.db import get_conn, get_last_transaction, get_last_transaction_price

logger = logging.getLogger("hunter.token_price")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{token_address}"

# Si el último precio on-chain propio de un token de Robinhood tiene menos de
# esto, se usa directo sin llamar a DexScreener (2026-09-19): es el precio más
# fresco que hay y evita una llamada de red por posición en cada revisión, lo
# que permite revisar las posiciones cada pocos segundos en vez de cada minuto.
FRESH_OWN_PRICE_SECONDS = 90

MAX_PRICE_DEVIATION = 5.0  # DexScreener vs último precio on-chain propio, ver fetch_current_price


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
    with get_conn() as conn:
        last_tx = get_last_transaction(conn, chain, token_address)
    fallback = last_tx["price"] if last_tx else None

    if chain == "robinhood" and last_tx is not None and fallback:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_tx["detected_at"])).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is not None and age <= FRESH_OWN_PRICE_SECONDS:
            return fallback

    price = await fetch_token_price_sol(token_address)

    if price is not None:
        # BUG REAL (2026-09-18): DexScreener empezó a indexar tokens ya
        # graduados de Robinhood Chain, pero su priceNative NO viene en
        # la misma unidad que nuestro precio on-chain propio (que es el
        # que se usó para la ENTRADA). Mezclar las dos fuentes daba
        # multiplicadores de 100x a 6,937x en ~16 posiciones (0.5% del
        # total) que inflaban el PnL en +$95k sobre datos falsos. Se
        # verificó que el 97.5% de las salidas normales SÍ coincide con
        # el precio propio (+-40%), así que el problema es puntual: si
        # DexScreener se desvía más de MAX_PRICE_DEVIATION del último
        # precio propio, no se le cree.
        if fallback is not None and fallback > 0:
            ratio = price / fallback
            if ratio > MAX_PRICE_DEVIATION or ratio < 1 / MAX_PRICE_DEVIATION:
                logger.warning(
                    f"DexScreener ({price:.4g}) se desvía {ratio:.1f}x del precio propio "
                    f"({fallback:.4g}) para {token_address} ({chain}) -- se ignora, "
                    f"se usa el precio propio."
                )
                return fallback
        return price

    if fallback is not None:
        logger.info(
            f"DexScreener sin datos para {token_address} ({chain}) -- "
            f"usando último precio propio registrado: {fallback}"
        )
    return fallback
