"""
Listener de Robinhood Chain (L2 tipo Arbitrum Orbit, lanzada julio
2026) -- monitorea Pons, uno de los launchpads de memecoins que corren
ahí (junto a Memecoin.fun, Flap, Noxa, etc.). Se eligió Pons porque
tiene un factory contract confirmado y activo (ver validación abajo).

VALIDADO CONTRA EL SERVIDOR REAL el 2026-09-13, mismo rigor que
chains/solana.py -- nada de esto es "según la documentación", todo se
verificó con RPC calls reales:
- Factory de Pons (0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e):
  contrato real con bytecode desplegado, 1603 eventos en ~50k bloques.
- Evento TokenLaunched: confirmado con logs reales, 95 lanzamientos en
  solo 2000 bloques -- hay MUCHA actividad de creación de tokens acá.
- Eventos CurveBuy/CurveSell: decodificados contra transacciones
  reales (ej: compra real de 0.1496 ETH por 2.18M tokens, fee ~1%;
  venta real de 8.6M tokens por 0.74 ETH) -- la estructura de campos
  (quoteIn/tokensOut/fee/tax para buy, tokensIn/quoteOut/fee/tax para
  sell) coincide exactamente con lo documentado por Bitquery.

IMPORTANTE -- por qué esto usa POLLING y no WebSocket: el "feed"
WebSocket público de Robinhood Chain (wss://feed.mainnet.chain.
robinhood.com) NO es un eth_subscribe estándar -- se probó en vivo y
resultó ser el sequencer feed crudo de Arbitrum (mensajes L2 en
formato binario propio, para sincronizar nodos, no logs decodificados).
Decodificar eso sería reimplementar buena parte de un cliente de nodo
Arbitrum, fuera de escope. La alternativa gratis que sí funciona es
pedir eth_getLogs por HTTP cada POLL_INTERVAL_SECONDS.

Diseño clave: a diferencia de Solana (donde hay que pedir
getTransaction por cada señal), acá se puede filtrar eth_getLogs por
el topic0 del evento SIN especificar una dirección de contrato --
así se ven TODOS los buys/sells de TODAS las bonding curves del
launchpad en una sola consulta, sin necesitar una llamada extra por
transacción. La única llamada extra es por TOKEN nuevo (no por trade):
un eth_call a decimals() cuando vemos su TokenLaunched, para poder
mostrar el precio normalizado en vez de la razón cruda quote/token.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx

from chains.base import ChainListener, SwapEvent
from config.settings import ROBINHOOD_RPC_HTTP
from core.db import get_conn, upsert_token_created, upsert_token_graduated
from core.eth_price import get_cached_eth_usd

logger = logging.getLogger("hunter.robinhood")

PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
ZERO_ADDRESS = "0x" + "0" * 40


def launch_quote_token(log: dict) -> Optional[str]:
    """Activo de COTIZACION de la curva segun el evento TokenLaunched: primera palabra de los datos (address).
    ZERO_ADDRESS = ETH nativo. Verificado en cadena el 2026-09-20 con 56.465 lanzamientos: ~80% cotizan en ETH y ~20% en otros
    ERC20 (mas de 10 activos distintos). El listener asumia ETH para todos (quote_raw / 1e18): en las curvas con otro activo
    los montos y precios salian en 'ETH' inventados (una venta de $364 millones eran 139.241 unidades de un ERC20 que valian ~0.65 ETH)."""
    data = (log.get("data") or "")[2:]
    if len(data) < 64:
        return None
    return "0x" + data[24:64].lower()


def is_eth_quoted(quote_token: Optional[str]) -> bool:
    """True si la curva cotiza en ETH. Si el evento no trae el dato (None) se asume ETH, como antes del arreglo."""
    return quote_token is None or quote_token == ZERO_ADDRESS
TOKEN_LAUNCHED_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
CURVE_BUY_TOPIC = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
CURVE_SELL_TOPIC = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
# PoolGraduated(address indexed token, uint256 positionId, uint256 tokenAmount,
# uint256 pairTokenAmount) -- calculado con keccak256 y verificado contra un
# evento real en cadena el 2026-09-15 (ver conversación, 1 graduación real
# encontrada en 20000 bloques con este topic0 exacto).
POOL_GRADUATED_TOPIC = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"


def _decode_timestamp_hex(ts_hex) -> Optional[datetime]:
    if not ts_hex:
        return None
    try:
        ts = int(ts_hex, 16)
    except (ValueError, TypeError):
        return None
    # BUG REAL encontrado el 2026-09-16 analizando por qué el 87.5% de
    # los tokens de Robinhood Chain tenían created_at="1970-01-01"
    # (época 0): el campo `blockTimestamp` que devuelve este RPC en los
    # logs viene en "0x0" para la enorme mayoría de los eventos -- NO es
    # que falte (pasaría el "if ts_hex:"), es que está ahí pero vacío de
    # verdad. ts=0 nunca es un timestamp real para un evento on-chain de
    # esta cadena (lanzada en 2026) -- se trata como dato ausente, no
    # como "el token nació en 1970".
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)

POLL_INTERVAL_SECONDS = 5
# Tope de bloques por consulta -- el RPC público rechaza queries con más
# de 10000 logs matcheados (lo vimos en vivo). Si nos atrasamos mucho
# (reconexión, downtime), preferimos saltar historia vieja y avisar en
# vez de reventar la query.
MAX_BLOCK_RANGE = 500


class RobinhoodListener(ChainListener):
    name = "robinhood"

    def __init__(self):
        # curva (address, lowercase) -> token address -- se completa a
        # medida que vemos TokenLaunched. Si una curva empezó a operar
        # ANTES de que arrancáramos a escuchar, sus trades se descartan
        # (no sabemos a qué token pertenecen) -- ver _parse_trade_log.
        self._curve_to_token: dict[str, str] = {}
        # token address -> decimales reales (ERC20 estándar), para mostrar
        # un precio legible en vez de la razón cruda quote/token. Default
        # 18 (lo más común en EVM) si la llamada a decimals() falla.
        self._token_decimals: dict[str, int] = {}
        self._last_block: Optional[int] = None
        self._backoff_until = 0.0
        self._skipped_non_eth = 0

    async def listen(self, wallets: list[str]) -> AsyncIterator[SwapEvent]:
        async with httpx.AsyncClient() as client:
            self._last_block = await self._get_latest_block(client)
            logger.info(f"Robinhood Chain: arrancando desde el bloque {self._last_block}")

            while True:
                if time.monotonic() < self._backoff_until:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                try:
                    events = await self._poll_once(client)
                except httpx.HTTPError as e:
                    logger.warning(f"Error consultando RPC de Robinhood Chain: {e}")
                    events = []

                for event in events:
                    yield event

                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _rpc_call(self, client: httpx.AsyncClient, method: str, params: list):
        resp = await client.post(
            ROBINHOOD_RPC_HTTP,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=15,
        )
        if resp.status_code == 429:
            self._backoff_until = time.monotonic() + 10
            logger.warning("RPC de Robinhood Chain rate-limiteó (429) -- pausando 10s.")
            return None
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            # "logs matched by query exceeds limit" cae acá -- lo tratamos
            # como error recuperable en _poll_once (se resuelve solo en
            # el próximo poll, ya con last_block actualizado).
            logger.warning(f"RPC de Robinhood Chain devolvió error: {data['error']}")
            return None
        return data.get("result")

    async def _get_latest_block(self, client: httpx.AsyncClient) -> int:
        result = await self._rpc_call(client, "eth_blockNumber", [])
        return int(result, 16) if result else 0

    async def _get_logs(self, client, topics, address=None, from_block=0, to_block=0):
        filt = {
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        if address:
            filt["address"] = address
        result = await self._rpc_call(client, "eth_getLogs", [filt])
        return result or []

    async def _get_block_timestamp_iso(self, client: httpx.AsyncClient, log: dict) -> str:
        """Momento real on-chain de un evento -- preferimos esto a "cuándo
        lo vimos nosotros" para no depender del timing del polling.

        `blockTimestamp` en el log es un campo NO estándar (lo agregan
        algunos clientes tipo Erigon) y en este RPC viene en "0x0" para
        el 87.5% de los eventos reales (bug encontrado el 2026-09-16,
        ver _decode_timestamp_hex) -- no se puede confiar en él solo.
        `eth_getBlockByNumber` sí es JSON-RPC estándar y siempre trae un
        `timestamp` real; se usa como fuente confiable, con el campo del
        log como atajo rápido solo cuando de verdad viene poblado."""
        dt = _decode_timestamp_hex(log.get("blockTimestamp"))
        if dt is not None:
            return dt.isoformat()

        block_number = log.get("blockNumber")
        if block_number:
            result = await self._rpc_call(client, "eth_getBlockByNumber", [block_number, False])
            if result:
                dt = _decode_timestamp_hex(result.get("timestamp"))
                if dt is not None:
                    return dt.isoformat()

        return datetime.now(timezone.utc).isoformat()

    async def _fetch_decimals(self, client: httpx.AsyncClient, token_address: str) -> int:
        """decimals() de ERC20 (selector 0x313ce567) -- default 18 si falla."""
        result = await self._rpc_call(
            client, "eth_call", [{"to": token_address, "data": "0x313ce567"}, "latest"]
        )
        if not result or result == "0x":
            return 18
        try:
            return int(result, 16)
        except ValueError:
            return 18

    async def _poll_once(self, client: httpx.AsyncClient) -> list[SwapEvent]:
        latest = await self._get_latest_block(client)
        if latest <= self._last_block:
            return []

        from_block = self._last_block + 1
        if latest - from_block > MAX_BLOCK_RANGE:
            logger.warning(
                f"Robinhood Chain: nos atrasamos {latest - from_block} bloques -- "
                f"saltando a los últimos {MAX_BLOCK_RANGE} en vez de pedir todo "
                f"(evita el límite de 10000 logs del RPC público)."
            )
            from_block = latest - MAX_BLOCK_RANGE
        to_block = latest

        # 1. Actualizar el mapeo curva -> token con los lanzamientos nuevos,
        # y registrar la fecha de creación (tabla `tokens`, ver core/db.py).
        launch_logs = await self._get_logs(
            client, topics=[TOKEN_LAUNCHED_TOPIC], address=PONS_FACTORY,
            from_block=from_block, to_block=to_block,
        )
        for log in launch_logs:
            token = "0x" + log["topics"][1][-40:]
            curve = "0x" + log["topics"][2][-40:]
            # topics[3]: tercer address indexado del evento, no
            # documentado -- confirmado el 2026-09-17 con datos reales
            # (una misma wallet apareciendo acá en 5 TokenLaunched
            # distintos dentro de una ventana chica de bloques, algo que
            # no pasaría si fuera un valor por-token). Es el creador/
            # deployer, no el token ni la curva.
            creator = "0x" + log["topics"][3][-40:] if len(log["topics"]) > 3 else None
            quote = launch_quote_token(log)
            eth_quoted = is_eth_quoted(quote)
            if eth_quoted:
                self._curve_to_token[curve.lower()] = token.lower()
                self._token_decimals[token.lower()] = await self._fetch_decimals(client, token)
            else:
                # Curva cotizada en otro ERC20: sus montos/precios NO estan en ETH y el simulador no puede comprarla con ETH.
                # No se registra la curva, asi que sus trades se descartan en _parse_trade_log (curva desconocida).
                self._skipped_non_eth += 1
                if self._skipped_non_eth % 100 == 1:
                    logger.info(f"Curvas que no cotizan en ETH ignoradas: {self._skipped_non_eth} (ultima: {token.lower()} cotiza en {quote})")
            created_iso = await self._get_block_timestamp_iso(client, log)
            with get_conn() as conn:
                upsert_token_created(conn, self.name, token.lower(), created_iso, creator=creator.lower() if creator else None, quote_token=quote)

        # 2. Graduaciones (bonding curve -> pool líquido) -- para medir
        # "tiempo hasta graduación" con datos propios, no ajenos.
        graduation_logs = await self._get_logs(
            client, topics=[POOL_GRADUATED_TOPIC], address=PONS_FACTORY,
            from_block=from_block, to_block=to_block,
        )
        for log in graduation_logs:
            token = "0x" + log["topics"][1][-40:]
            graduated_iso = await self._get_block_timestamp_iso(client, log)
            with get_conn() as conn:
                upsert_token_graduated(conn, self.name, token.lower(), graduated_iso)
            logger.info(f"Token graduado detectado: {token.lower()}")

        # 3. Traer TODOS los buys/sells de TODAS las curvas (sin filtro
        # de address -- esa es la ventaja de EVM sobre Solana acá).
        trade_logs = await self._get_logs(
            client, topics=[[CURVE_BUY_TOPIC, CURVE_SELL_TOPIC]],
            from_block=from_block, to_block=to_block,
        )

        self._last_block = to_block

        events = []
        for log in trade_logs:
            event = self._parse_trade_log(log)
            if event:
                events.append(event)
        return events

    def _parse_trade_log(self, log: dict) -> Optional[SwapEvent]:
        topic0 = log["topics"][0].lower()
        curve_address = log["address"].lower()
        token_address = self._curve_to_token.get(curve_address)
        if token_address is None:
            return None  # curva lanzada antes de que empezáramos a escuchar

        hexdata = log["data"][2:]
        words = [int(hexdata[i * 64:(i + 1) * 64], 16) for i in range(len(hexdata) // 64)]
        if len(words) < 4:
            return None

        if topic0 == CURVE_BUY_TOPIC:
            side = "buy"
            wallet = "0x" + log["topics"][1][-40:]
            quote_raw, token_raw = words[0], words[1]
        elif topic0 == CURVE_SELL_TOPIC:
            side = "sell"
            wallet = "0x" + log["topics"][1][-40:]
            token_raw, quote_raw = words[0], words[1]
        else:
            return None

        if quote_raw == 0 or token_raw == 0:
            return None

        quote_eth = quote_raw / 1e18  # ETH tiene 18 decimales, siempre
        decimals = self._token_decimals.get(token_address, 18)
        token_amount = token_raw / (10 ** decimals)  # normalizado, legible
        price = quote_eth / token_amount

        eth_usd = get_cached_eth_usd()
        amount_usd = quote_eth * eth_usd if eth_usd is not None else None

        return SwapEvent(
            chain=self.name,
            wallet=wallet,
            token_address=token_address,
            side=side,
            amount_usd=amount_usd,
            price=price,
            tx_hash=log["transactionHash"],
        )
