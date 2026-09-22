"""
Velas (K-line) de tokens ya MIGRADOS / establecidos (capitalizacion >= $100K) para estudiar estrategias de tendencia que nuestro rastreador de la curva no ve: a los ~5 minutos de vida un token "se gradua" a un pool de
mercado abierto y dejamos de ver sus operaciones (NOSH: solo tenemos sus primeros 5 min de una vida de mas de un dia). GMGN da velas de 30 s a 1 dia (/v1/market/token_kline) tambien de Robinhood Chain.
Se guardan velas de 5 min, 15 min y 1 h (las ultimas 100 de cada una: 8 h, 25 h y ~4 dias) en attention.db (at_klines) de los tokens que el laboratorio ya vio con capitalizacion >= 100K; se refrescan cada 6 h. Solo recoleccion de datos: no opera.
"""
import json
import logging
import time

import httpx

from core import attention_lab as al

logger = logging.getLogger("hunter.kline")

RESOLUTIONS = ("5m", "15m", "1h")                                                             # GMGN devuelve las ultimas 100 velas: 8 h, 25 h y ~4 dias de historia
MIN_MCAP = 100000.0
REFRESH_S = 6 * 3600
HISTORY_S = 40 * 3600                      # 480 velas de 5 min: el maximo por solicitud es 500 (con 3 dias GMGN devuelve lista vacia)
MAX_TOKENS_PER_CYCLE = 6
_warned = [0]
# La documentacion pide from/to en segundos, pero con un rango de 3 dias o de 40 h GMGN devolvio lista vacia: se prueban variantes y se recuerda la que trae velas.
RANGE_MODES = [lambda now: {"from": int(now - HISTORY_S), "to": int(now)},               # segundos (documentado)
               lambda now: {},                                                              # sin rango: lo mas reciente que devuelva
               lambda now: {"from": int((now - HISTORY_S) * 1000), "to": int(now * 1000)},  # milisegundos
               lambda now: {"from": int(now - 6 * 3600), "to": int(now)}]                   # solo las ultimas 6 h
RANGE_NAMES = ["segundos", "sin rango", "milisegundos", "6 h"]
_mode = [1, 0]                                                                              # [modo que funciono, tokens donde ningun modo trajo velas]
KLINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS at_klines (chain TEXT NOT NULL, token TEXT NOT NULL, res TEXT NOT NULL, t INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (chain, token, res, t));
CREATE TABLE IF NOT EXISTS at_kline_state (chain TEXT NOT NULL, token TEXT NOT NULL, fetched_ms INTEGER, n INTEGER, note TEXT, PRIMARY KEY (chain, token));
"""


def init_kline_db(path=None):
    with al.conn_ctx(path) as c:
        c.executescript(KLINE_SCHEMA)


def parse_klines(payload):
    """Velas de GMGN (data.list o data.data.list): [{time, open, high, low, close, volume, amount}] -> [(t, o, h, l, c, v_usd)] ordenadas. Ignora velas mal formadas."""
    d = al._unwrap(payload, "list")
    items = d.get("list") if isinstance(d, dict) else d
    out = []
    for k in items if isinstance(items, list) else []:
        try:
            t = int(k["time"])
            if t > 1e11:                                                    # milisegundos -> segundos
                t //= 1000
            out.append((t, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]), float(k.get("volume") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def candidate_tokens(path=None, min_mcap=MIN_MCAP):
    """Tokens (chain, address) que el laboratorio vio con capitalizacion >= min_mcap en alguna fuente de GMGN."""
    seen = {}
    with al.conn_ctx(path) as c:
        for r in c.execute("SELECT chain, token, meta FROM at_events WHERE source LIKE 'GMGN%' ORDER BY id DESC"):
            if (r["chain"], r["token"]) in seen:
                continue
            try:
                mc = float(json.loads(r["meta"] or "{}").get("market_cap") or 0)
            except (TypeError, ValueError):
                mc = 0.0
            if mc >= min_mcap:
                seen[(r["chain"], r["token"])] = mc
    return seen


def pick_due(path=None, now=None, max_n=MAX_TOKENS_PER_CYCLE):
    """Tokens candidatos sin velas o con la ultima descarga de hace mas de REFRESH_S, los de mayor capitalizacion primero."""
    now = now or time.time()
    cands = candidate_tokens(path)
    with al.conn_ctx(path) as c:
        st = {(r["chain"], r["token"]): r["fetched_ms"] for r in c.execute("SELECT chain, token, fetched_ms FROM at_kline_state")}
    due = [(mc, k) for k, mc in cands.items() if st.get(k) is None or now - st[k] / 1000 >= REFRESH_S]
    due.sort(reverse=True)
    return [k for _, k in due[:max_n]]


def store(chain, token, candles, path=None, now=None, note=None, res="5m", final=True):
    """Guarda las velas de una resolucion; con final=True actualiza tambien el estado del token (n = velas guardadas en total)."""
    now = now or time.time()
    with al.conn_ctx(path) as c:
        c.executemany("INSERT OR REPLACE INTO at_klines (chain, token, res, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?,?)", [(chain, token, res, *k) for k in candles])
        if final:
            total = c.execute("SELECT COUNT(*) FROM at_klines WHERE chain=? AND token=?", (chain, token)).fetchone()[0]
            c.execute("INSERT OR REPLACE INTO at_kline_state (chain, token, fetched_ms, n, note) VALUES (?,?,?,?,?)", (chain, token, int(now * 1000), total, note))


async def _fetch_res(client, key, get, gch, token, res, now):
    """Velas de una resolucion; prueba los modos de rango (primero el que ya funciono) hasta que alguno traiga velas."""
    js = None
    for mode in [_mode[0]] + [m for m in range(len(RANGE_MODES)) if m != _mode[0]]:
        js = await get(client, key, "/v1/market/token_kline", {"chain": gch, "address": token, "resolution": res, **RANGE_MODES[mode](now)})
        candles = parse_klines(js)
        if candles:
            if _mode[0] != mode:
                logger.warning(f"GMGN kline: funciona el modo de rango {mode} ({RANGE_NAMES[mode]}); se usa desde ahora")
                _mode[0] = mode
            return candles, js
        if _mode[1] >= len(RANGE_MODES) * 2:                                     # ya probamos todos los modos en varios tokens: no gastar llamadas de mas
            break
        await al.asyncio.sleep(1.3)
    return [], js


async def collect(client, key, path=None, gmgn_get=None, now=None):
    """Descarga las velas (5 min, 15 min y 1 h: las ultimas 100 de cada una) de los tokens pendientes. Devuelve cuantos tokens se procesaron."""
    if not key:
        return 0
    init_kline_db(path)
    get = gmgn_get or al._gmgn_get
    now = now or time.time()
    n = 0
    for chain, token in pick_due(path, now):
        gch = {"solana": "sol"}.get(chain, chain)
        try:
            total = 0
            for res in RESOLUTIONS:
                candles, js = await _fetch_res(client, key, get, gch, token, res, now)
                store(chain, token, candles, path, now, res=res, final=False)
                total += len(candles)
                if not candles:
                    _mode[1] += 1
                    if _warned[0] < 3:
                        _warned[0] += 1
                        logger.warning(f"GMGN kline {chain} {token[:10]} {res}: 0 velas en todos los modos; respuesta: {json.dumps(js, default=str)[:400]}")
                await al.asyncio.sleep(1.3)                                      # limite de GMGN: 1 solicitud por segundo
            store(chain, token, [], path, now, None if total else "sin velas", final=True)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("GMGN kline: 429, reintenta despues")
                break
            store(chain, token, [], path, now, f"HTTP {e.response.status_code}", final=True)
        except Exception as e:
            store(chain, token, [], path, now, str(e)[:120], final=True)
        n += 1
    return n
