"""
Motor de detección de "manadas": N wallets distintas comprando el mismo
token en una ventana de tiempo corta. Esta es la lógica central que
viste descripta en el tweet de STAMPEDE -- acá la implementamos de
verdad, de forma auditable (vos podés ver exactamente por qué disparó
cada alerta, no es una caja negra).

IMPORTANTE -- por qué STAMPEDE_MIN_WALLETS = 5 en config/settings.py:
ese número NUNCA se validó con datos propios, se heredó de la
conversación/tweet original que inspiró el proyecto. Para poder
comparar algún día "manadas de 5" contra "manadas de 8" con datos
reales, el detector ahora también reporta el "pico" real de wallets
que compraron DENTRO de toda la ventana de 8 min -- no solo el número
en el instante exacto del disparo, que hoy queda siempre fijo en 5
(dispara apenas cruza el umbral, así que wallet_count nunca varía).
"""
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from chains.base import SwapEvent
from config.settings import (
    STAMPEDE_MIN_WALLETS,
    STAMPEDE_WINDOW_SECONDS,
    MIN_TRADE_USD,
    IGNORE_SINGLE_WALLET_REPEATS,
)

logger = logging.getLogger("hunter.stampede")


@dataclass
class TokenActivity:
    # wallet -> timestamp de su compra más reciente de este token
    buyers: dict = field(default_factory=dict)
    already_alerted: bool = False
    peak_wallet_count: int = 0
    alerted_at: float = 0.0
    # id de la alerta en la DB -- lo setea main.py después de insertarla
    # (acá no tenemos acceso a la DB), para poder mandar actualizaciones
    # de peak_wallet_count a la fila correcta mientras siga la ventana.
    alert_id: int | None = None


class StampedeDetector:
    def __init__(self):
        # token_address -> TokenActivity
        self._activity: dict[str, TokenActivity] = defaultdict(TokenActivity)

    def process(self, event: SwapEvent) -> dict | None:
        """
        Procesa un evento y devuelve un dict con los datos de la alerta
        si se detecta una manada NUEVA (type='new_alert'), un dict de
        actualización si el token ya había alertado pero sigue sumando
        compradores dentro de la misma ventana (type='update'), o None
        si no hay nada que reportar.
        """
        if event.side != "buy":
            return None
        if event.amount_usd is not None and event.amount_usd < MIN_TRADE_USD:
            return None  # filtra órdenes de prueba chiquitas

        activity = self._activity[event.token_address]
        now = time.time()

        # Si ignoramos repeticiones de la misma wallet, solo actualizamos
        # el timestamp si es su primera compra reciente de este token.
        is_new_buyer = event.wallet not in activity.buyers
        if IGNORE_SINGLE_WALLET_REPEATS and not is_new_buyer:
            return None

        activity.buyers[event.wallet] = now
        self._prune_old(activity, now)

        wallet_count = len(activity.buyers)
        if wallet_count > activity.peak_wallet_count:
            activity.peak_wallet_count = wallet_count

        if wallet_count >= STAMPEDE_MIN_WALLETS and not activity.already_alerted:
            activity.already_alerted = True
            activity.alerted_at = now
            return {
                "type": "new_alert",
                "chain": event.chain,
                "token_address": event.token_address,
                "wallet_count": wallet_count,
                "window_seconds": STAMPEDE_WINDOW_SECONDS,
                "wallets": list(activity.buyers.keys()),
            }

        if (
            activity.already_alerted
            and activity.alert_id is not None
            and (now - activity.alerted_at) <= STAMPEDE_WINDOW_SECONDS
        ):
            return {
                "type": "update",
                "alert_id": activity.alert_id,
                "token_address": event.token_address,
                "peak_wallet_count": activity.peak_wallet_count,
            }

        return None

    def set_alert_id(self, token_address: str, alert_id: int):
        """main.py llama esto justo después de insertar la alerta en la
        DB, para que las próximas compras del mismo token dentro de la
        ventana puedan actualizar el peak_wallet_count de esa fila."""
        self._activity[token_address].alert_id = alert_id

    def _prune_old(self, activity: TokenActivity, now: float):
        cutoff = now - STAMPEDE_WINDOW_SECONDS
        stale = [w for w, ts in activity.buyers.items() if ts < cutoff]
        for w in stale:
            del activity.buyers[w]
        # Si se cayó por debajo del umbral tras podar, permitimos que
        # pueda volver a alertar en el futuro.
        if len(activity.buyers) < STAMPEDE_MIN_WALLETS:
            activity.already_alerted = False
