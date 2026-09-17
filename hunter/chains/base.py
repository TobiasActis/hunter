"""
Contrato que toda cadena soportada debe cumplir.

La idea: el motor de detección (core/stampede.py) no sabe ni le importa
si el dato viene de Solana, Robinhood chain o Base. Solo pide un stream
de eventos normalizados. Así agregamos cadenas nuevas escribiendo un
solo archivo, sin tocar nada más.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class SwapEvent:
    """Evento normalizado de compra/venta, sea cual sea la cadena de origen."""
    chain: str
    wallet: str
    token_address: str
    side: str          # "buy" | "sell"
    amount_usd: float
    price: float
    tx_hash: str


class ChainListener(ABC):
    """Cada cadena (Solana, Robinhood, Base, BNB...) implementa esto."""

    name: str

    @abstractmethod
    async def listen(self, wallets: list[str]) -> AsyncIterator[SwapEvent]:
        """
        Debe ser un generador async que va emitiendo SwapEvent a medida
        que detecta actividad de las wallets rastreadas (o de todo el
        mercado, si wallets está vacío -- útil para el modo "descubrir
        smart money" que armamos más adelante).
        """
        raise NotImplementedError
