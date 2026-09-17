"""
Listener de Solana usando PumpPortal (https://pumpportal.fun).

Por qué esto es mejor que parsear logs crudos de Solana a mano:
PumpPortal ya decodifica las instrucciones del programa de Pump.fun/
Raydium y te da el swap parseado (wallet, token, monto, tipo). Es un
servicio de terceros no oficial, así que lo tratamos como una fuente
más -- si algún día se cae o cambia el formato, la interfaz
ChainListener nos permite reemplazarlo sin tocar el resto del sistema.

COSTOS -- VALIDADO CONTRA EL SERVIDOR REAL el 2026-09-13:
- subscribeNewToken (tokens nuevos creados): GRATIS, confirmado. Llegan
  mensajes reales con txType="create" apenas se conecta, sin API key.
- subscribeMigration (graduaciones a Raydium/PumpSwap): GRATIS (se
  suscribe sin error, pero no llegó ningún evento en la ventana de
  prueba -- son menos frecuentes que tokens nuevos).
- subscribeTokenTrade / subscribeAccountTrade: NO SON GRATIS COMO
  DECÍA ESTE COMENTARIO ANTES. El servidor devuelve este error apenas
  los pedís sin API key:
      "'subscribeTokenTrade' and 'subscribeAccountTrade' methods are
      only available when connecting with an API key funded with at
      least 0.02 SOL."
  O sea: no es "gratis con costo marginal por evento", es un gate de
  entrada -- necesitás una API key de PumpPortal con SOL cargado ANTES
  de poder recibir un solo evento de compra/venta. Sin eso, el modo
  dirigido (TRACKED_WALLETS) y el modo descubrimiento por trades NO
  funcionan. Falta conseguir esa API key y volver a probar.

QUÉ SIGUE SIN VALIDARSE por lo anterior: el formato real de un mensaje
de trade (txType="buy"/"sell", con traderPublicKey/solAmount/
tokenAmount) en `_parse_message()`. Lo que está abajo sigue siendo lo
que dice la documentación pública, no algo confirmado contra el
servidor. Los eventos de txType="create" SÍ se confirmaron y ya los
filtra correctamente el código (no son swaps).
"""
import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import websockets

from chains.base import ChainListener, SwapEvent
from core.sol_price import get_cached_sol_usd

logger = logging.getLogger("hunter.pumpportal")

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"


class PumpPortalListener(ChainListener):
    name = "solana"

    def __init__(self, track_new_tokens: bool = True, track_migrations: bool = True):
        self.track_new_tokens = track_new_tokens
        self.track_migrations = track_migrations

    async def listen(self, wallets: list[str]) -> AsyncIterator[SwapEvent]:
        while True:  # reconexión automática
            try:
                async with websockets.connect(PUMPPORTAL_WS_URL) as ws:
                    await self._subscribe(ws, wallets)
                    async for raw_msg in ws:
                        event = self._parse_message(raw_msg)
                        if event:
                            yield event
            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning(f"WS de PumpPortal caído ({e}), reconectando en 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                # Mismo bug confirmado en chains/solana.py -- un cierre
                # anormal del WS puede disparar un AttributeError interno
                # de la librería websockets en vez de ConnectionClosed.
                logger.exception(f"Error inesperado en el WS de PumpPortal, reconectando en 5s: {e}")
                await asyncio.sleep(5)

    async def _subscribe(self, ws, wallets: list[str]):
        if self.track_new_tokens:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            logger.info("Suscripto a tokens nuevos (gratis)")

        if self.track_migrations:
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            logger.info("Suscripto a graduaciones (gratis)")

        if wallets:
            await ws.send(json.dumps({
                "method": "subscribeAccountTrade",
                "keys": wallets,
            }))
            logger.info(f"Suscripto a trades de {len(wallets)} wallets rastreadas")

    def _parse_message(self, raw_msg: str) -> Optional[SwapEvent]:
        """
        Formato esperado según la documentación pública de PumpPortal
        para eventos de trade (subscribeAccountTrade / subscribeTokenTrade):

            {
                "signature": "...",
                "mint": "TokenMintAddress...",
                "traderPublicKey": "WalletAddress...",
                "txType": "buy" | "sell",
                "tokenAmount": 12345.678,
                "solAmount": 0.5,
                "marketCapSol": 123.45,
                ...
            }

        NOTA: este parsing hay que confirmarlo contra mensajes reales
        (ver aviso arriba). Si algún campo no existe o tiene otro
        nombre, es acá donde se ajusta -- el resto del sistema
        (detector, DB, scorer) no se entera del cambio.
        """
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            return None

        # Los eventos de "nuevo token" y "migración" no son swaps -- los
        # ignoramos acá (se podrían usar para otra cosa, como alimentar
        # el detector de "recién graduado" en el futuro).
        if "txType" not in data or "mint" not in data:
            return None

        tx_type = data.get("txType")
        if tx_type not in ("buy", "sell"):
            return None

        sol_amount = data.get("solAmount")
        token_amount = data.get("tokenAmount")
        if sol_amount is None or token_amount in (None, 0):
            return None

        price = sol_amount / token_amount if token_amount else None

        sol_usd = get_cached_sol_usd()
        amount_usd = sol_amount * sol_usd if sol_usd is not None else None
        if amount_usd is None:
            logger.warning(
                "Precio de SOL/USD todavía no disponible -- amount_usd queda "
                "None para este evento (el filtro MIN_TRADE_USD no lo va a "
                "poder aplicar hasta que el cache tenga un valor)."
            )

        return SwapEvent(
            chain=self.name,
            wallet=data.get("traderPublicKey", ""),
            token_address=data.get("mint", ""),
            side=tx_type,
            amount_usd=amount_usd,
            price=price,
            tx_hash=data.get("signature", ""),
        )
