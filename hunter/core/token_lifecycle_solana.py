"""
Rastrea CUÁNDO se crea y CUÁNDO (si acaso) se gradúa cada token de
Pump.fun -- para poder medir "tiempo hasta graduación"/"edad del token
al momento de la alerta" con nuestros propios datos, en vez de creerle
a un análisis de terceros (ver core/db.py::tokens para el porqué).

Usa PumpPortal, NO para trades (eso está bloqueado sin pagar, ver
chains/solana_pumpportal.py) sino para subscribeNewToken y
subscribeMigration -- ambas CONFIRMADAS GRATIS el 2026-09-15:
- subscribeNewToken: ya lo sabíamos gratis (ver chains/solana_pumpportal.py).
- subscribeMigration: se probó en vivo, la suscripción no pidió pago
  (a diferencia de subscribeTokenTrade/subscribeAccountTrade), y llegó
  un evento real de migración con formato:
      {"signature": "...", "mint": "...", "txType": "migrate", "pool": "pump-amm"}

Esto es un listener aparte de chains/solana.py (que sí hace los swaps
reales vía RPC público) porque esta es información distinta -- no
genera SwapEvent, solo actualiza la tabla `tokens`.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from core.db import get_conn, upsert_token_created, upsert_token_graduated

logger = logging.getLogger("hunter.token_lifecycle_solana")

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
CHAIN = "solana"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run():
    while True:  # reconexión automática, mismo patrón que los demás listeners
        try:
            async with websockets.connect(PUMPPORTAL_WS_URL) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                logger.info("Rastreador de ciclo de vida (Solana) conectado.")

                async for raw_msg in ws:
                    _handle_message(raw_msg)
        except (websockets.ConnectionClosed, OSError) as e:
            logger.warning(f"WS de ciclo de vida (Solana) caído ({e}), reconectando en 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            # Mismo bug confirmado que en chains/solana.py::listen() --
            # un cierre anormal del WS puede disparar un AttributeError
            # interno de la librería websockets en vez de ConnectionClosed.
            logger.exception(f"Error inesperado en el WS de ciclo de vida, reconectando en 5s: {e}")
            await asyncio.sleep(5)


def _handle_message(raw_msg: str):
    try:
        data = json.loads(raw_msg)
    except json.JSONDecodeError:
        return

    tx_type = data.get("txType")
    mint = data.get("mint")
    if not mint:
        return  # mensajes de confirmación de suscripción, etc.

    if tx_type == "create":
        with get_conn() as conn:
            upsert_token_created(conn, CHAIN, mint, _now_iso())
    elif tx_type == "migrate":
        with get_conn() as conn:
            upsert_token_graduated(conn, CHAIN, mint, _now_iso())
            logger.info(f"Token graduado detectado: {mint}")
