"""
LABORATORIO DE ATENCION (PAPEL): mide si los tokens que aparecen en listas publicas de "atencion" (boosts y perfiles nuevos de DexScreener, pools en tendencia de GeckoTerminal y, si hay clave, tendencias y
"smart money" de GMGN) rinden algo DESPUES de aparecer, con costos reales de pool. NUNCA opera de verdad y NO toca la base ni el servicio principal (base propia: data/attention.db, servicio aparte).

Hipotesis a refutar: "estar en una lista de atencion anticipa retorno". Lo esperable es que la atencion ya este en el precio cuando la lista la muestra; se mide igual porque es barato y deja datos propios.
Cada token se registra UNA vez por fuente y cadena (su primera aparicion que vemos) y se entra al primer precio disponible (tamano fijo $100). Precio, liquidez y edad salen SIEMPRE de DexScreener (misma vara para
todas las fuentes); las variables propias de cada fuente (rank, smart_degen_count, monto del boost...) quedan en `meta` para analizar despues.
Costos por lado: comision de la cadena + deslizamiento = tamano / reserva de cotizacion (constante-producto: liquidez USD / 2) + costo de transaccion. Robinhood (curva de Pons) usa 1% por lado.
Si el par desaparece no se cuenta cero: se marca SIN DATO (pero la regla de decision usa el PEOR CASO, con SIN DATO = perdida total, porque un token que desaparece suele ser un rug).
Fuentes: DEX_BOOST (boost pago = atencion comprada), DEX_PERFIL (perfil nuevo; base de comparacion barata, incluye Robinhood), GECKO_TREND (pools en tendencia; solana/base/eth), CTL (CONTROL: pools nuevos de
GeckoTerminal muestreados 1 de cada 4, sin lista de atencion), GMGN_TREND / GMGN_SMART (solo con GMGN_API_KEY; tendencias 1 h y tokens con >= 3 smart money; solana y robinhood).
Mediciones: precio del pool a 5 min, 15 min, 1 h, 4 h y 24 h desde la entrada. Regla para creerle (fijada de antemano, 2026-09-21): por fuente y cadena, >= 100 entradas medidas a 1 h, multiplo neto medio en
peor caso > 1 con IC95 que excluye 1, y mejor que la base de comparacion de esa cadena (CTL si hay >= 30, si no DEX_PERFIL) con IC95 de la diferencia > 0. Un solo horizonte (1 h) elegido antes de mirar.
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import statistics
import time
import uuid
from contextlib import contextmanager

import httpx

logger = logging.getLogger("hunter.attention")

DB = os.path.join("data", "attention.db")
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{addrs}"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
GECKO = "https://api.geckoterminal.com/api/v2/networks/{net}/{kind}"
GMGN_HOST = "https://openapi.gmgn.ai"

SIZE_USD = 100.0
COSTS = {"solana": (0.003, 0.5), "robinhood": (0.01, 0.05), "base": (0.003, 0.05), "ethereum": (0.003, 3.0), "bsc": (0.003, 0.1)}      # (comision por lado, costo de transaccion en USD)
DEFAULT_COST = (0.003, 0.5)
MIN_QUOTE_USD = 1500.0
HORIZONS_S = [300, 900, 3600, 14400, 86400]
DECISION_HORIZON_S = 3600
MIN_DECISION_N = 100
MIN_CTL_N = 30
ENTRY_TIMEOUT_S = 600
MISS_GRACE_S = 600
SOURCE_EVERY_S = 300
WORK_EVERY_S = 20
GECKO_NETS = {"solana": "solana", "base": "base", "eth": "ethereum"}                        # red de GeckoTerminal -> chainId de DexScreener
GMGN_CHAINS = {"sol": "solana", "robinhood": "robinhood"}                                    # cadena de GMGN -> chainId de DexScreener
GMGN_MIN_SMART = 3
GMGN_META_KEYS = ("rank", "hot_level", "smart_degen_count", "renowned_count", "rug_ratio", "volume", "liquidity", "holder_count", "launchpad_platform", "is_wash_trading",       # "senales de meme mala": las que el trader mira a mano
                  "top_10_holder_rate", "bundler_rate", "rat_trader_amount_rate", "sniper_count", "dev_team_hold_rate", "entrapment_ratio", "bot_degen_count", "bot_degen_rate",
                  "top70_sniper_hold_rate", "renounced_mint", "renounced_freeze_account", "is_honeypot", "buy_tax", "sell_tax", "burn_status", "lock_percent",
                  "market_cap", "swaps", "buys", "sells", "price_change_percent1m", "price_change_percent5m", "price_change_percent1h", "creation_timestamp", "open_timestamp", "initial_liquidity")
TRACK_CHAINS = {"sol": "solana", "base": "base"}                                             # /v1/user/kol y /v1/user/smartmoney solo cubren sol/bsc/base/eth (NO Robinhood)
TRACK_MIN_USD = 50.0
TRACK_MAX_LAG_S = 600.0                                                                       # la lista trae las ultimas 100 operaciones (horas de antiguedad): solo cuentan las de los ultimos 10 min
CTL_SAMPLE = 4
SOURCES = ("DEX_BOOST", "DEX_PERFIL", "GECKO_TREND", "CTL", "GMGN_TREND", "GMGN_SMART", "GMGN_KOL", "GMGN_SMARTBUY")

SCHEMA = """
CREATE TABLE IF NOT EXISTS at_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, chain TEXT NOT NULL, token TEXT NOT NULL, first_ms INTEGER NOT NULL,
    status TEXT NOT NULL,                        -- 'pending' | 'active' | 'done' | 'rejected' | 'no_price'
    symbol TEXT, entry_ms INTEGER, entry_lag_s REAL, entry_price REAL, entry_liq_usd REAL, entry_quote_usd REAL, age_h REAL, pair TEXT, dex TEXT, note TEXT, meta TEXT,
    UNIQUE(source, chain, token)
);
CREATE INDEX IF NOT EXISTS ix_at_status ON at_events(status);
CREATE TABLE IF NOT EXISTS at_marks (event_id INTEGER NOT NULL, horizon_s INTEGER NOT NULL, ts_ms INTEGER NOT NULL, price REAL, quote_usd REAL, missed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(event_id, horizon_s));
CREATE TABLE IF NOT EXISTS at_state (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def conn_ctx(path=None):
    p = path or DB
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path=None):
    with conn_ctx(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ logica pura (sin red)
def norm_token(chain, token):
    return token if chain == "solana" else token.lower()


def ctl_sample(token: str) -> bool:
    return int(hashlib.sha1(token.encode()).hexdigest()[:8], 16) % CTL_SAMPLE == 0


def cost_for(chain):
    return COSTS.get(chain, DEFAULT_COST)


def net_multiple(entry_price, entry_quote_usd, mark_price, mark_quote_usd, chain="solana", size=SIZE_USD):
    """Multiplo NETO del capital ($size) tras comprar al precio de entrada y vender al del marcado, con comision, deslizamiento del pool y costo de transaccion en cada lado."""
    if not (entry_price and mark_price and entry_quote_usd and mark_quote_usd and entry_price > 0 and mark_price > 0):
        return None
    fee, tx = cost_for(chain)
    swap_in = (size - tx) * (1 - fee)
    fill = entry_price * (1 + swap_in / entry_quote_usd)
    tokens = swap_in / fill
    value = tokens * mark_price
    out_slip = min(value / mark_quote_usd, 0.99)
    proceeds = value * (1 - out_slip) * (1 - fee) - tx
    return max(proceeds, 0.0) / size


def parse_dex_list(payload, source):
    """Boosts o perfiles de DexScreener: lista de {chainId, tokenAddress, ...}."""
    out = []
    for it in payload if isinstance(payload, list) else []:
        ch, tk = it.get("chainId"), it.get("tokenAddress")
        if not ch or not tk:
            continue
        meta = {k: it[k] for k in ("amount", "totalAmount") if k in it}
        out.append({"source": source, "chain": ch, "token": norm_token(ch, tk), "meta": meta})
    return out


def parse_gecko_pools(payload, net, source):
    """Pools de GeckoTerminal (trending_pools / new_pools): el token base sale de relationships.base_token.data.id = '<red>_<direccion>'."""
    chain = GECKO_NETS.get(net)
    out = []
    for pool in (payload or {}).get("data") or []:
        tid = ((((pool.get("relationships") or {}).get("base_token") or {}).get("data")) or {}).get("id")
        if not chain or not tid or "_" not in tid:
            continue
        addr = tid.split("_", 1)[1]
        a = pool.get("attributes") or {}
        meta = {"reserve_usd": a.get("reserve_in_usd"), "vol24": (a.get("volume_usd") or {}).get("h24"), "pool_created_at": a.get("pool_created_at")}
        out.append({"source": source, "chain": chain, "token": norm_token(chain, addr), "meta": meta})
    if source == "CTL":
        out = [e for e in out if ctl_sample(e["token"])]
    return out


def parse_gmgn_rank(payload, gmgn_chain, source, min_smart=None):
    """Tendencias de GMGN: data.rank es una lista de items con address, smart_degen_count, renowned_count, rug_ratio, hot_level, volume... (segun su documentacion)."""
    chain = GMGN_CHAINS.get(gmgn_chain)
    data = (payload or {}).get("data")
    while isinstance(data, dict) and "rank" not in data and isinstance(data.get("data"), (dict, list)):     # la API real anida un nivel de mas: {"code":0,"data":{"code":0,"data":{"rank":[...]}}}
        data = data["data"]
    items = data.get("rank") if isinstance(data, dict) else data
    out = []
    for it in items if isinstance(items, list) else []:
        addr = it.get("address")
        if not chain or not addr:
            continue
        smart = it.get("smart_degen_count") or 0
        if min_smart is not None and smart < min_smart:
            continue
        meta = {k: it.get(k) for k in GMGN_META_KEYS if k in it}
        out.append({"source": source, "chain": chain, "token": norm_token(chain, addr), "meta": meta})
    return out


def parse_gmgn_track(payload, gmgn_chain, source, now_s=None, min_usd=TRACK_MIN_USD, max_lag_s=TRACK_MAX_LAG_S):
    """Operaciones de KOL o smart money de GMGN (/v1/user/kol, /v1/user/smartmoney): data.list de {maker, side, base_address, amount_usd, price_usd, timestamp, is_open_or_close, maker_info.tags...}.
    Solo COMPRAS que abren o suman posicion (side buy, is_open_or_close == 0) de al menos `min_usd`. Un evento por token (la primera compra que vemos); guarda quien compro, cuanto, a que precio y hace cuanto."""
    chain = TRACK_CHAINS.get(gmgn_chain)
    data = (payload or {}).get("data")
    while isinstance(data, dict) and "list" not in data and isinstance(data.get("data"), (dict, list)):
        data = data["data"]
    items = data.get("list") if isinstance(data, dict) else data
    seen, out = set(), []
    for it in items if isinstance(items, list) else []:
        addr = it.get("base_address")
        if not chain or not addr or it.get("side") != "buy" or (it.get("is_open_or_close") not in (0, "0", None)):
            continue
        try:
            usd = float(it.get("amount_usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if usd < min_usd:
            continue
        tok = norm_token(chain, addr)
        ts = it.get("timestamp")
        if now_s and max_lag_s and (not ts or now_s - float(ts) > max_lag_s):     # operacion vieja: entrar horas despues no mide "seguir al KOL"
            continue
        if tok in seen:
            continue
        seen.add(tok)
        mi = it.get("maker_info") or {}
        meta = {"maker": it.get("maker"), "twitter": mi.get("twitter_username"), "tags": mi.get("tags"), "amount_usd": usd, "kol_price_usd": it.get("price_usd"), "trade_ts": ts,
                "lag_s": (now_s - float(ts)) if (now_s and ts) else None}
        out.append({"source": source, "chain": chain, "token": tok, "meta": meta})
    return out


def choose_pair(pairs, token):
    """Mejor par del token en la respuesta de DexScreener (mayor liquidez). Devuelve dict o None. Cotizacion = mitad de la liquidez (pool constante-producto)."""
    best = None
    for p in pairs or []:
        try:
            if (p["baseToken"]["address"] or "").lower() != token.lower() or not p.get("priceUsd"):
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if best is None or liq > best[0]:
                best = (liq, p)
        except (KeyError, TypeError, ValueError):
            continue
    if best is None:
        return None
    liq, p = best
    created = p.get("pairCreatedAt")
    return {"price": float(p["priceUsd"]), "liq_usd": liq, "quote_usd": liq / 2, "pair": p.get("pairAddress"), "dex": p.get("dexId"), "symbol": (p.get("baseToken") or {}).get("symbol"),
            "created_ms": created}


def mean_ci(x):
    n = len(x)
    if n == 0:
        return None, None
    m = sum(x) / n
    if n < 2:
        return m, None
    return m, 1.96 * statistics.stdev(x) / n ** 0.5


def diff_ci(a, b):
    """Diferencia de medias a - b con IC95 (Welch, aproximacion normal). Devuelve (dif, semiancho) o (None, None)."""
    if len(a) < 2 or len(b) < 2:
        return None, None
    d = sum(a) / len(a) - sum(b) / len(b)
    se = (statistics.variance(a) / len(a) + statistics.variance(b) / len(b)) ** 0.5
    return d, 1.96 * se


def event_stats(rows, horizon_s):
    """rows: filas de eventos con sus marcas [{'chain','entry_price','entry_quote_usd','marks': {h: {'price','quote_usd','missed'}}}].
    Devuelve dict con n medidos, neto medio (sin los SIN DATO), neto medio en PEOR CASO (SIN DATO = 0), % SIN DATO y lista de netos de peor caso."""
    seen, worst, missed = [], [], 0
    for r in rows:
        m = (r["marks"] or {}).get(horizon_s)
        if m is None:
            continue
        if m["missed"]:
            missed += 1
            worst.append(0.0)
            continue
        v = net_multiple(r["entry_price"], r["entry_quote_usd"], m["price"], m["quote_usd"], r["chain"])
        if v is None:
            continue
        seen.append(v)
        worst.append(v)
    n = len(worst)
    mo, ci_o = mean_ci(seen)
    mw, ci_w = mean_ci(worst)
    return {"n": n, "n_seen": len(seen), "missed": missed, "mean": mo, "ci": ci_o, "mean_worst": mw, "ci_worst": ci_w, "worst": worst,
            "win": (sum(1 for v in seen if v > 1) / len(seen)) if seen else None}


def decide(src_worst, base_worst, base_name):
    """Regla fijada de antemano. src_worst / base_worst: netos de peor caso a 1 h. Devuelve (veredicto, detalle)."""
    n = len(src_worst)
    if n < MIN_DECISION_N:
        return "FALTA MUESTRA", f"{n}/{MIN_DECISION_N} medidos"
    m, ci = mean_ci(src_worst)
    if ci is None or m - ci <= 1.0:
        return "NO CUMPLE", f"neto medio peor caso {m:.3f}x (IC95 +-{ci or 0:.3f}) no supera 1"
    if not base_worst or len(base_worst) < 2:
        return "NO CUMPLE", f"sin base de comparacion ({base_name})"
    d, dci = diff_ci(src_worst, base_worst)
    if d is None or d - dci <= 0:
        return "NO CUMPLE", f"no supera a {base_name} (dif {d:+.3f}x, IC95 +-{dci:.3f})"
    return "CUMPLE", f"neto {m:.3f}x y supera a {base_name} por {d:+.3f}x"


# ------------------------------------------------------------------ persistencia
def record_events(events, now_ms, path=None):
    n = 0
    with conn_ctx(path) as c:
        for e in events:
            cur = c.execute("INSERT OR IGNORE INTO at_events (source, chain, token, first_ms, status, meta) VALUES (?,?,?,?, 'pending', ?)",
                            (e["source"], e["chain"], e["token"], now_ms, json.dumps(e.get("meta") or {}, default=str)))
            n += cur.rowcount
    return n


def apply_entries(pending, pairs_by_key, now_ms, path=None):
    """Aplica la respuesta de DexScreener a los eventos pendientes. pairs_by_key: {(chain, token): [pares] | None (no se pudo consultar)}."""
    with conn_ctx(path) as c:
        for p in pending:
            prs = pairs_by_key.get((p["chain"], p["token"]))
            info = choose_pair(prs, p["token"]) if prs else None
            if info:
                age_h = ((now_ms - info["created_ms"]) / 3.6e6) if info["created_ms"] else None
                if info["quote_usd"] < MIN_QUOTE_USD:
                    c.execute("UPDATE at_events SET status='rejected', symbol=?, entry_liq_usd=?, age_h=?, note='liquidez < minimo' WHERE id=?", (info["symbol"], info["liq_usd"], age_h, p["id"]))
                    continue
                c.execute("""UPDATE at_events SET status='active', symbol=?, entry_ms=?, entry_lag_s=?, entry_price=?, entry_liq_usd=?, entry_quote_usd=?, age_h=?, pair=?, dex=? WHERE id=?""",
                          (info["symbol"], now_ms, (now_ms - p["first_ms"]) / 1000, info["price"], info["liq_usd"], info["quote_usd"], age_h, info["pair"], info["dex"], p["id"]))
            elif prs is not None and (now_ms - p["first_ms"]) / 1000 > ENTRY_TIMEOUT_S:
                c.execute("UPDATE at_events SET status='no_price', note='sin par en DexScreener tras 10 min' WHERE id=?", (p["id"],))


def due_marks(active, done, now_ms):
    return [(s, h) for s in active for h in HORIZONS_S if h not in done.get(s["id"], set()) and now_ms >= s["entry_ms"] + h * 1000]


def apply_marks(due, pairs_by_key, now_ms, path=None):
    with conn_ctx(path) as c:
        for s, h in due:
            prs = pairs_by_key.get((s["chain"], s["token"]))
            if prs is None:
                continue                                                # fallo de red: reintenta
            info = choose_pair(prs, s["token"])
            if info:
                c.execute("INSERT OR IGNORE INTO at_marks (event_id, horizon_s, ts_ms, price, quote_usd, missed) VALUES (?,?,?,?,?,0)", (s["id"], h, now_ms, info["price"], info["quote_usd"]))
            elif now_ms - (s["entry_ms"] + h * 1000) > MISS_GRACE_S * 1000:
                c.execute("INSERT OR IGNORE INTO at_marks (event_id, horizon_s, ts_ms, price, quote_usd, missed) VALUES (?,?,?,NULL,NULL,1)", (s["id"], h, now_ms))
        for s in {x[0]["id"]: x[0] for x in due}.values():
            n = c.execute("SELECT COUNT(*) n FROM at_marks WHERE event_id=?", (s["id"],)).fetchone()["n"]
            if n >= len(HORIZONS_S):
                c.execute("UPDATE at_events SET status='done' WHERE id=?", (s["id"],))


# ------------------------------------------------------------------ red
async def _get(client, url, params=None, headers=None):
    r = await client.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


async def dex_pairs(client, items):
    """items: [(chain, token)] -> {(chain, token): [pares] | None si no se pudo consultar}. De a 30 tokens por llamada y por cadena."""
    out = {k: [] for k in items}
    by_chain = {}
    for ch, tk in items:
        by_chain.setdefault(ch, []).append(tk)
    for ch, toks in by_chain.items():
        for i in range(0, len(toks), 30):
            chunk = toks[i:i + 30]
            try:
                data = await _get(client, DEX_TOKENS.format(chain=ch, addrs=",".join(chunk)))
                for p in data if isinstance(data, list) else []:
                    a = ((p.get("baseToken") or {}).get("address") or "")
                    key = (ch, norm_token(ch, a))
                    if key in out:
                        out[key].append(p)
            except Exception as e:
                logger.warning(f"DexScreener: error consultando {len(chunk)} tokens de {ch}: {e}")
                for tk in chunk:
                    out[(ch, tk)] = None
    return out


async def fetch_sources(client, gmgn_key=None):
    events = []
    for source, url in (("DEX_BOOST", DEX_BOOSTS), ("DEX_PERFIL", DEX_PROFILES)):
        try:
            events += parse_dex_list(await _get(client, url), source)
        except Exception as e:
            logger.warning(f"{source}: {e}")
    for net in GECKO_NETS:
        for kind, source in (("trending_pools", "GECKO_TREND"), ("new_pools", "CTL")):
            try:
                events += parse_gecko_pools(await _get(client, GECKO.format(net=net, kind=kind)), net, source)
            except Exception as e:
                logger.warning(f"GeckoTerminal {net}/{kind}: {e}")
            await asyncio.sleep(7)                                     # ~10 llamadas/min sin clave
    if gmgn_key:
        for gch in GMGN_CHAINS:
            for source, extra, min_smart in (("GMGN_TREND", {}, None), ("GMGN_SMART", {"order_by": "smart_degen_count", "direction": "desc"}, GMGN_MIN_SMART)):
                try:
                    q = dict({"chain": gch, "interval": "1h", "limit": 50, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}, **extra)
                    js = await _get(client, GMGN_HOST + "/v1/market/rank", params=q, headers={"X-APIKEY": gmgn_key, "User-Agent": "hunter-paper-lab"})
                    if isinstance(js, dict) and js.get("code") not in (None, 0):
                        logger.warning(f"{source} {gch}: respuesta {js.get('code')} {str(js.get('msg') or js.get('message'))[:120]}")
                        continue
                    got = parse_gmgn_rank(js, gch, source, min_smart)
                    if not got and min_smart is None:                      # tendencia vacia = forma de respuesta distinta a la documentada: dejar la estructura en el log para corregir el parser
                        logger.warning(f"{source} {gch}: 0 items; respuesta: {json.dumps(js, default=str)[:600]}")
                    events += got
                except Exception as e:
                    logger.warning(f"{source} {gch}: {e}")
                await asyncio.sleep(1.3)                               # limite documentado: 1 solicitud por segundo
        for gch in TRACK_CHAINS:
            for source, path in (("GMGN_KOL", "/v1/user/kol"), ("GMGN_SMARTBUY", "/v1/user/smartmoney")):
                try:
                    q = {"chain": gch, "limit": 100, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
                    js = await _get(client, GMGN_HOST + path, params=q, headers={"X-APIKEY": gmgn_key, "User-Agent": "hunter-paper-lab"})
                    if isinstance(js, dict) and js.get("code") not in (None, 0):
                        logger.warning(f"{source} {gch}: respuesta {js.get('code')} {str(js.get('msg') or js.get('message'))[:120]}")
                        continue
                    got = parse_gmgn_track(js, gch, source, now_s=time.time())
                    if not got and source == "GMGN_KOL":
                        logger.warning(f"{source} {gch}: 0 compras; respuesta: {json.dumps(js, default=str)[:500]}")
                    events += got
                except Exception as e:
                    logger.warning(f"{source} {gch}: {e}")
                await asyncio.sleep(1.3)
    return events


async def work_once(client, now_ms):
    with conn_ctx() as c:
        pend = [dict(r) for r in c.execute("SELECT * FROM at_events WHERE status='pending' ORDER BY id LIMIT 300")]
        act = [dict(r) for r in c.execute("SELECT * FROM at_events WHERE status='active'")]
        done = {}
        for r in c.execute("SELECT event_id, horizon_s FROM at_marks WHERE event_id IN (SELECT id FROM at_events WHERE status='active')"):
            done.setdefault(r["event_id"], set()).add(r["horizon_s"])
    if pend:
        apply_entries(pend, await dex_pairs(client, sorted({(p["chain"], p["token"]) for p in pend})), now_ms)
    due = due_marks(act, done, now_ms)
    if due:
        apply_marks(due, await dex_pairs(client, sorted({(s["chain"], s["token"]) for s, _ in due})), now_ms)


async def run():
    init_db()
    key = os.environ.get("GMGN_API_KEY", "").strip() or None
    logger.info(f"Laboratorio de atencion (PAPEL) arrancando: fuentes DexScreener + GeckoTerminal{' + GMGN' if key else ' (sin GMGN: falta GMGN_API_KEY)'}")
    last_src = 0.0
    async with httpx.AsyncClient(headers={"User-Agent": "hunter-paper-lab"}) as client:
        while True:
            try:
                now = time.time()
                if now - last_src >= SOURCE_EVERY_S:
                    last_src = now
                    n = record_events(await fetch_sources(client, key), int(now * 1000))
                    if n:
                        logger.info(f"Atencion: {n} eventos nuevos")
                await work_once(client, int(time.time() * 1000))
            except Exception:
                logger.exception("Atencion: error en el ciclo, reintenta")
            await asyncio.sleep(WORK_EVERY_S)


# ------------------------------------------------------------------ lectura / reporte
def load_events(path=None):
    """Eventos medidos (con sus marcas) para el reporte."""
    with conn_ctx(path) as c:
        ev = [dict(r) for r in c.execute("SELECT * FROM at_events")]
        marks = {}
        for r in c.execute("SELECT * FROM at_marks"):
            marks.setdefault(r["event_id"], {})[r["horizon_s"]] = dict(r)
    for e in ev:
        e["marks"] = marks.get(e["id"], {})
    return ev


def report_text(path=None):
    """Texto para la seccion 9 del reporte nocturno."""
    if not os.path.exists(path or DB):
        return "(el laboratorio de atencion todavia no arranco: no existe data/attention.db)"
    ev = load_events(path)
    if not ev:
        return "Todavia no hay eventos."
    lines = []
    tot = {s: [e for e in ev if e["source"] == s] for s in SOURCES}
    lines.append("Eventos por fuente (total | con entrada | rechazados por liquidez | sin par): " + "; ".join(
        f"{s} {len(v)}|{sum(1 for e in v if e['status'] in ('active', 'done'))}|{sum(1 for e in v if e['status'] == 'rejected')}|{sum(1 for e in v if e['status'] == 'no_price')}" for s, v in tot.items() if v))
    h = DECISION_HORIZON_S
    groups = {}
    for e in ev:
        if e["status"] in ("active", "done"):
            groups.setdefault((e["source"], e["chain"]), []).append(e)
    lines.append(f"\nMultiplo neto del capital a {h // 3600} h (costos reales de pool; peor caso = SIN DATO cuenta como perdida total). >1 = ganancia:")
    for (src, ch), rows in sorted(groups.items()):
        st = event_stats(rows, h)
        if st["n"] == 0:
            continue
        base_name = "CTL" if len(event_stats(groups.get(("CTL", ch), []), h)["worst"]) >= MIN_CTL_N else "DEX_PERFIL"
        base = event_stats(groups.get((base_name, ch), []), h)["worst"] if src != base_name else []
        if src in ("CTL",) or src == base_name:
            verdict = "(base de comparacion)"
        else:
            v, det = decide(st["worst"], base, base_name)
            verdict = f"{v}: {det}"
        ages = sorted(e["age_h"] for e in rows if e["age_h"] is not None)
        age = f"{ages[len(ages) // 2]:.0f} h" if ages else "--"
        drift = []
        if src in ("GMGN_KOL", "GMGN_SMARTBUY"):                                    # cuanto se movio el precio entre la compra del KOL / smart money y nuestra entrada (costo del retraso)
            for e in rows:
                try:
                    kp = float((json.loads(e["meta"] or "{}")).get("kol_price_usd") or 0)
                except (TypeError, ValueError):
                    kp = 0
                if kp > 0 and e["entry_price"]:
                    drift.append(e["entry_price"] / kp)
            drift.sort()
        age += f" | entramos a {drift[len(drift) // 2]:.2f}x del precio del KOL (mediana, n={len(drift)})" if drift else ""
        mean = f"{st['mean']:.3f}x" if st["mean"] is not None else "--"
        worst = f"{st['mean_worst']:.3f}x" if st["mean_worst"] is not None else "--"
        win = f"{st['win'] * 100:.0f}%" if st["win"] is not None else "--"
        lines.append(f"  {src:11s} {ch:9s} n={st['n']:4d} | neto medio {mean} (sin SIN DATO) | peor caso {worst} | >1: {win} | SIN DATO {st['missed']} | edad mediana {age} | {verdict}")
    lines.append(f"\nRegla (fijada 2026-09-21): >= {MIN_DECISION_N} medidos a 1 h, neto peor caso > 1 con IC95 y mejor que la base de comparacion con IC95 de la diferencia > 0.")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run())
