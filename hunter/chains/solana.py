"""
Listener de Solana -- plan B (gratis) para cuando PumpPortal no sirve.

VALIDADO CONTRA EL SERVIDOR REAL el 2026-09-13. Contexto: PumpPortal
(chains/solana_pumpportal.py) bloqueó subscribeTokenTrade/
subscribeAccountTrade detrás de una API key que exige 0.02 SOL
cargados -- esto es la alternativa 100% gratis usando el RPC público
de Solana directamente.

Cómo funciona (confirmado con transacciones reales, no supuesto):
1. logsSubscribe con {"mentions": [PUMPFUN_PROGRAM]} manda, para CADA
   transacción que toca ese programa, sus logMessages + signature.
   Gratis, sin API key, funciona con wss://api.mainnet-beta.solana.com.
2. Se descartan la mayoría (create de token, migraciones, etc.) mirando
   si el log contiene "Instruction: Buy" o "Instruction: Sell" -- así
   no gastamos rate limit del RPC público llamando getTransaction para
   cosas que no son swaps.
3. Para las que sí son swaps, se llama getTransaction (HTTP, jsonParsed)
   con esa signature. Ahí NO se parsea el texto del log (frágil) sino
   los balances estructurados:
   - La wallet que ejecuta el swap es la que tiene "signer": true en
     transaction.message.accountKeys.
   - meta.preTokenBalances / meta.postTokenBalances de esa wallet dan
     el delta de tokens (negativo = vendió, positivo = compró).
   - meta.preBalances / meta.postBalances (lamports) de esa misma
     wallet dan el monto neto de SOL movido, ya neto de fees de red.
   Confirmado contra una venta real: el delta de tokens dio negativo
   exactamente en la transacción que logueaba "Instruction: Sell".

Limitación conocida: el RPC público rate-limitea. Para uso serio en
VPS, poné HELIUS_API_KEY en config/settings.py y cambiá SOLANA_RPC_WS/
SOLANA_RPC_HTTP por los endpoints de Helius -- el free tier de Helius
NO da WebSockets enhanced (esos son de pago, confirmado por su propia
documentación), pero SU RPC estándar (logsSubscribe + getTransaction,
que es justo lo que usamos acá) sí está en el free tier y es mucho
menos restrictivo que el público.
"""
import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional

import httpx
import websockets

from chains.base import ChainListener, SwapEvent
from config.settings import SOLANA_RPC_WS, SOLANA_RPC_HTTP
from core.sol_price import get_cached_sol_usd

logger = logging.getLogger("hunter.solana")

# Program IDs públicos de los DEX/launchpads más usados para memecoins.
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

BUY_LOG_MARKER = "Program log: Instruction: Buy"
SELL_LOG_MARKER = "Program log: Instruction: Sell"


class SolanaListener(ChainListener):
    name = "solana"

    def __init__(self, program_ids: Optional[list[str]] = None):
        self.program_ids = program_ids or [RAYDIUM_AMM_PROGRAM, PUMPFUN_PROGRAM]
        # El RPC público rate-limitea rápido en modo descubrimiento
        # (confirmado: ~10 requests en 10s ya tira 429). Este backoff
        # evita seguir bombardeándolo -- mientras esté activo, se
        # descartan swaps en vez de seguir pegándole al RPC.
        self._backoff_until = 0.0

    async def listen(self, wallets: list[str]) -> AsyncIterator[SwapEvent]:
        while True:  # reconexión automática si el WS se cae
            try:
                async with websockets.connect(SOLANA_RPC_WS) as ws:
                    await self._subscribe(ws, wallets)
                    async for raw_msg in ws:
                        event = await self._parse_message(raw_msg)
                        if event:
                            yield event
            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning(f"WS de Solana caído ({e}), reconectando en 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                # CONFIRMADO EN PRODUCCIÓN (2026-09-16): un cierre anormal
                # del WS ("keepalive ping timeout; no close frame
                # received") puede disparar un bug interno de la librería
                # websockets durante la reconexión (AttributeError:
                # 'NoneType' object has no attribute 'resume_reading') en
                # vez de ConnectionClosed -- no entraba en el except de
                # arriba, se escapaba, y tumbaba TODO el proceso (22
                # reinicios en un día). Logueamos completo para poder
                # investigar, pero nunca dejamos que esto mate el proceso.
                logger.exception(f"Error inesperado en el WS de Solana, reconectando en 5s: {e}")
                await asyncio.sleep(5)

    async def _subscribe(self, ws, wallets: list[str]):
        if wallets:
            # Modo dirigido: solo estas wallets (más liviano, menos falsos positivos)
            for wallet in wallets:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}],
                }))
        else:
            # Modo descubrimiento: todo el flujo de los programas DEX
            for program_id in self.program_ids:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [program_id]}, {"commitment": "confirmed"}],
                }))

    async def _parse_message(self, raw_msg: str) -> Optional[SwapEvent]:
        try:
            data = json.loads(raw_msg)
            value = data.get("params", {}).get("result", {}).get("value", {})
            logs = value.get("logs", [])
            signature = value.get("signature")
            err = value.get("err")
        except (json.JSONDecodeError, AttributeError):
            return None

        if err is not None or not logs or not signature:
            return None  # transacción fallida o mensaje que no es una notificación de log

        # Filtro barato antes de gastar una llamada HTTP: la gran mayoría
        # de las transacciones que mencionan al programa son creación de
        # tokens, migraciones, etc. -- no swaps.
        if any(BUY_LOG_MARKER in line for line in logs):
            side = "buy"
        elif any(SELL_LOG_MARKER in line for line in logs):
            side = "sell"
        else:
            return None

        return await self._fetch_and_build_swap(signature, side)

    async def _fetch_and_build_swap(self, signature: str, side: str) -> Optional[SwapEvent]:
        """
        Trae la transacción completa (jsonParsed) y arma el SwapEvent a
        partir de los balances pre/post -- no del texto del log, que es
        frágil y no da los montos exactos.
        """
        if time.monotonic() < self._backoff_until:
            return None  # en backoff por 429 reciente, no vale la pena reintentar todavía

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    SOLANA_RPC_HTTP,
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                    },
                    timeout=10,
                )
                if resp.status_code == 429:
                    self._backoff_until = time.monotonic() + 5
                    logger.warning(
                        "RPC de Solana rate-limiteó (429) -- pausando llamadas "
                        "getTransaction por 5s. Si esto pasa seguido en modo "
                        "descubrimiento, hace falta un RPC propio (ej. Helius "
                        "free tier) o pasar a modo dirigido con menos wallets."
                    )
                    return None
                resp.raise_for_status()
                result = resp.json().get("result")
            except httpx.HTTPError as e:
                logger.warning(f"Error consultando getTransaction para {signature}: {e}")
                return None

        if not result:
            return None  # todavía no confirmada / el RPC no la tiene

        return self._build_swap_event(result, signature, side)

    def _build_swap_event(self, tx: dict, signature: str, side: str) -> Optional[SwapEvent]:
        meta = tx.get("meta") or {}
        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])

        # La wallet que ejecuta el swap es la firmante (fee payer).
        wallet = next((k["pubkey"] for k in account_keys if k.get("signer")), None)
        wallet_index = next((i for i, k in enumerate(account_keys) if k.get("pubkey") == wallet), None)
        if wallet is None or wallet_index is None:
            return None

        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []
        if wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
            return None

        sol_amount = abs(post_balances[wallet_index] - pre_balances[wallet_index]) / 1e9
        if sol_amount == 0:
            return None

        pre_by_owner = {b["owner"]: b for b in (meta.get("preTokenBalances") or []) if b.get("owner") == wallet}
        post_by_owner = {b["owner"]: b for b in (meta.get("postTokenBalances") or []) if b.get("owner") == wallet}

        token_mint = None
        token_delta = 0.0
        for owner, post_bal in post_by_owner.items():
            pre_bal = pre_by_owner.get(owner)
            pre_amount = float((pre_bal or {}).get("uiTokenAmount", {}).get("uiAmountString") or 0)
            post_amount = float(post_bal.get("uiTokenAmount", {}).get("uiAmountString") or 0)
            delta = post_amount - pre_amount
            if delta != 0:
                token_mint = post_bal["mint"]
                token_delta = delta
                break

        if token_mint is None or token_delta == 0:
            return None

        price = sol_amount / abs(token_delta)  # SOL por token
        sol_usd = get_cached_sol_usd()
        amount_usd = sol_amount * sol_usd if sol_usd is not None else None

        return SwapEvent(
            chain=self.name,
            wallet=wallet,
            token_address=token_mint,
            side=side,
            amount_usd=amount_usd,
            price=price,
            tx_hash=signature,
        )
