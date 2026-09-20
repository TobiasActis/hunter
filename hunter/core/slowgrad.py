"""
GRADUACIONES LENTAS de pump.fun (Solana) EN PAPEL: replica la senal del bot de Telegram @kotte_memescan_bot ("SLOW GRADUATION", codigo abierto: github.com/gustaffsonKotte/qlo) y mide, con precios
reales de DexScreener y costos reales de pool, si comprar tras la graduacion rinde. NUNCA opera de verdad.

Idea del bot (segun su repo): un token que tarda HORAS en llenar la bonding curve (en vez de ~20 min) rinde mejor despues de graduar; su backtest de 97.146 graduaciones dice "1.5x mas probable de duplicar y 2.4x de
hacer 5x" -- una probabilidad, no un retorno neto de costos y de entrar tarde. Sus filtros: migracion verdadera, liquidez real >= 10 SOL, edad >= 6 h, alerta a <= 180 s. Sus alertas llegan por mensaje directo del bot
(el canal publico t.me/kotte_writes NO las trae: se verifico), asi que aca se DETECTAN las mismas graduaciones con nuestro rastreador de PumpPortal (tabla tokens) y ademas se pueden pegar CAs a mano (MANUAL).

Variantes:
  SG6     graduo >= 6 h despues de la creacion (nuestra hora de creacion conocida). Es la senal del bot.
  SGOLD   graduo pero nunca vimos su creacion (token anterior al rastreador, o sea mas viejo que ~5 dias): el escalon "muy lento" ("Took 51d+").
  SGCTL   CONTROL: graduo en menos de 1 h (la mayoria, ~20 min); se muestrea 1 de cada 8 para no gastar llamadas.
  BOT     alertas REALES del bot recibidas por el oyente tg_alerts.py (lee solo a @kotte_memescan_bot en la cuenta de Telegram del dueno): entrada al primer precio tras recibirla; se descartan alertas de hace
          mas de 5 min (si el oyente estuvo caido, simular ahora no reflejaria la alerta). El retraso de entrada se mide contra la HORA DEL MENSAJE de Telegram.
  MANUAL  CAs de alertas reales del bot que pegas en el dashboard: se simulan al precio del momento en que las pegas.
Reglas de entrada (iguales para todas): se busca el par en DexScreener cada ~10 s hasta 5 min; entrada al primer precio disponible (se mide el retraso contra la graduacion); se rechaza si la reserva real de SOL del pool < 10 SOL
(USDC < $1000). Tamano fijo $100.
Costos por lado: comision de PumpSwap 0.30%, deslizamiento = tamano / reserva real de cotizacion del pool en USD (pool constante-producto), costo de transaccion (prioridad + propina) $0.5. La salida usa la liquidez del
MOMENTO de salir; si el par desaparece no se cuenta cero: se marca SIN DATO (como hace el propio escaner).
Mediciones: precio del pool a 1, 3, 5, 10, 15, 30 min y 1, 2, 4, 6, 12, 24 h desde la entrada. Lo que importa: el multiplo neto medio por horizonte contra el CONTROL. Regla para creerle: >= 100 senales medidas por
horizonte con neto medio > 0 (IC95% excluye 0) y mejor que el control; una salida fija a UN horizonte elegido de antemano (1 h) para no escoger el mejor mirando.
"""
import asyncio
import hashlib
import logging
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone

import httpx

from config.settings import DB_PATH
from core.scalper import conn_ctx, now_iso

logger = logging.getLogger("hunter.slowgrad")

DEX = "https://api.dexscreener.com/tokens/v1/solana/"
SIZE_USD = 100.0
FEE_SIDE = 0.003
TX_USD = 0.5
MIN_QUOTE_SOL = 10.0
MIN_QUOTE_USDC = 1000.0
SLOW_HOURS = 6.0
FAST_HOURS = 1.0
CTL_SAMPLE = 8
ENTRY_TIMEOUT_S = 300
MISS_GRACE_S = 180
POLL_SECONDS = 10
HORIZONS_S = [60, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
STAT_HORIZONS_S = [300, 900, 3600, 21600, 86400]
DECISION_HORIZON_S = 3600
VARIANTS = {
    "SG6": "Graduacion lenta (>= 6 h): la senal del bot",
    "SGOLD": "Graduacion muy lenta (token anterior a nuestros datos)",
    "SGCTL": "Control: graduacion rapida (< 1 h)",
    "BOT": "Alertas reales del bot (automaticas)",
    "MANUAL": "Alertas del bot que pegas vos",
}
B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sg_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL, mint TEXT NOT NULL, source TEXT NOT NULL, hours_to_grad REAL, signal_ms INTEGER NOT NULL,
    status TEXT NOT NULL,                       -- 'pending' | 'active' | 'done' | 'rejected' | 'no_price'
    symbol TEXT, entry_ms INTEGER, entry_lag_s REAL, entry_price REAL, entry_liq_usd REAL, entry_quote_sol REAL, entry_quote_usd REAL, sol_usd REAL, pair TEXT, dex TEXT, note TEXT,
    UNIQUE(variant, mint)
);
CREATE INDEX IF NOT EXISTS ix_sg_status ON sg_signals(status);
CREATE TABLE IF NOT EXISTS sg_marks (signal_id INTEGER NOT NULL, horizon_s INTEGER NOT NULL, ts_ms INTEGER NOT NULL, price REAL, quote_usd REAL, missed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(signal_id, horizon_s));
CREATE TABLE IF NOT EXISTS sg_state (k TEXT PRIMARY KEY, v TEXT);
"""


def init_db():
    with conn_ctx() as c:
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ logica pura (sin red)
def ctl_sample(mint: str) -> bool:
    return int(hashlib.sha1(mint.encode()).hexdigest()[:8], 16) % CTL_SAMPLE == 0


def classify(created_at, graduated_at, mint):
    """Variante de una graduacion, o None si no entra en ninguna. created_at None = no vimos la creacion (token mas viejo que el rastreador)."""
    if not created_at:
        return "SGOLD", None
    try:
        hours = (datetime.fromisoformat(graduated_at) - datetime.fromisoformat(created_at)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None, None
    if hours >= SLOW_HOURS:
        return "SG6", hours
    if hours < FAST_HOURS and ctl_sample(mint):
        return "SGCTL", hours
    return None, hours


def choose_pair(pairs, mint):
    """Mejor par del token en la respuesta de DexScreener: PumpSwap primero, luego el de mayor liquidez. Devuelve dict o None."""
    cands = []
    for p in pairs:
        try:
            if p["baseToken"]["address"] != mint or not p.get("priceUsd"):
                continue
            liq = p.get("liquidity") or {}
            cands.append((p.get("dexId") == "pumpswap", float(liq.get("usd") or 0), p))
        except (KeyError, TypeError, ValueError):
            continue
    if not cands:
        return None
    _, liq_usd, p = max(cands, key=lambda x: (x[0], x[1]))
    price = float(p["priceUsd"])
    liq = p.get("liquidity") or {}
    q = p.get("quoteToken") or {}
    quote_amt = liq.get("quote")
    sol_usd = quote_sol = None
    if q.get("symbol") in ("SOL", "WSOL") and p.get("priceNative"):
        try:
            sol_usd = price / float(p["priceNative"])
        except (ValueError, ZeroDivisionError):
            sol_usd = None
        if quote_amt is not None:
            quote_sol = float(quote_amt)
    quote_usd = (quote_sol * sol_usd) if (quote_sol is not None and sol_usd) else (float(quote_amt) if (quote_amt is not None and q.get("symbol") in ("USDC", "USDT")) else liq_usd / 2)
    return {"price": price, "liq_usd": liq_usd, "quote_sol": quote_sol, "quote_usd": quote_usd, "sol_usd": sol_usd, "pair": p.get("pairAddress"), "dex": p.get("dexId"),
            "symbol": (p.get("baseToken") or {}).get("symbol")}


def accept_liquidity(info) -> bool:
    if info["quote_sol"] is not None:
        return info["quote_sol"] >= MIN_QUOTE_SOL
    return info["quote_usd"] >= MIN_QUOTE_USDC


def net_multiple(entry_price, entry_quote_usd, mark_price, mark_quote_usd, size=SIZE_USD, fee=FEE_SIDE, tx=TX_USD):
    """Multiplo NETO del capital ($size) tras comprar al precio de entrada y vender al del marcado, con comision, deslizamiento del pool y costo de transaccion en cada lado."""
    if not (entry_price and mark_price and entry_quote_usd and mark_quote_usd and entry_price > 0 and mark_price > 0):
        return None
    swap_in = (size - tx) * (1 - fee)
    fill = entry_price * (1 + swap_in / entry_quote_usd)
    tokens = swap_in / fill
    value = tokens * mark_price
    out_slip = min(value / mark_quote_usd, 0.99)
    proceeds = value * (1 - out_slip) * (1 - fee) - tx
    return max(proceeds, 0.0) / size


def summarize(rows):
    """rows: [(bruto, neto)] -> dict con n, mediana bruta, neto medio +-IC95, % neto>1, % bruto>=2x, >=5x."""
    n = len(rows)
    if not n:
        return {"n": 0}
    g = [r[0] for r in rows]
    nt = [r[1] for r in rows]
    mean = sum(nt) / n
    sd = statistics.pstdev(nt) * (n / (n - 1)) ** 0.5 if n > 1 else None
    return {"n": n, "median_gross": statistics.median(g), "mean_net": mean, "ci95": (1.96 * sd / n ** 0.5 if sd is not None else None), "median_net": statistics.median(nt),
            "pct_net_pos": sum(1 for x in nt if x > 1) / n, "pct_2x": sum(1 for x in g if x >= 2) / n, "pct_5x": sum(1 for x in g if x >= 5) / n}


def extract_mints(text: str):
    seen, out = set(), []
    for m in B58.findall(text or ""):
        if m not in seen:
            seen.add(m); out.append(m)
    return out


# ------------------------------------------------------------------ base de datos
def _state(c, k, default=None):
    r = c.execute("SELECT v FROM sg_state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _set_state(c, k, v):
    c.execute("INSERT OR REPLACE INTO sg_state (k, v) VALUES (?,?)", (k, str(v)))


def add_manual(mints):
    """Agrega CAs pegados a mano (variante MANUAL, entrada al precio actual). Devuelve cuantos se agregaron."""
    init_db()
    n = 0
    now_ms = int(time.time() * 1000)
    with conn_ctx() as c:
        for m in mints:
            if not B58.fullmatch(m):
                continue
            cur = c.execute("INSERT OR IGNORE INTO sg_signals (variant, mint, source, hours_to_grad, signal_ms, status) VALUES ('MANUAL', ?, 'manual', NULL, ?, 'pending')", (m, now_ms))
            n += cur.rowcount
    return n


BOT_MAX_AGE_S = 300


def add_bot_alert(mint, ts_ms, hours=None, symbol=None):
    """Alerta real del bot recibida por el oyente. Devuelve (agregada, motivo). Se descartan las de hace mas de 5 min."""
    init_db()
    if not B58.fullmatch(mint or ""):
        return False, "CA invalido"
    now_ms = int(time.time() * 1000)
    if not ts_ms or now_ms - int(ts_ms) > BOT_MAX_AGE_S * 1000:
        return False, "alerta vieja (mas de 5 min)"
    with conn_ctx() as c:
        cur = c.execute("INSERT OR IGNORE INTO sg_signals (variant, mint, source, hours_to_grad, signal_ms, status, symbol) VALUES ('BOT', ?, 'telegram', ?, ?, 'pending', ?)",
                        (mint, hours, int(ts_ms), symbol))
        return (cur.rowcount == 1), ("ya estaba" if cur.rowcount == 0 else "ok")


def detect_graduations(now_ms):
    """Lee de la base principal las graduaciones nuevas de Solana (tabla tokens, la llena el rastreador de PumpPortal) y crea senales."""
    import os
    path = os.path.abspath(DB_PATH)
    with conn_ctx() as c:
        last = _state(c, "last_grad")
        if last is None:
            last = datetime.now(timezone.utc).isoformat()
            _set_state(c, "last_grad", last)
            return 0
    rows = []
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = src.execute("SELECT token_address, created_at, graduated_at FROM tokens WHERE chain='solana' AND graduated_at > ? ORDER BY graduated_at LIMIT 400", (last,)).fetchall()
        finally:
            src.close()
    except sqlite3.Error as e:
        logger.warning(f"Graduaciones: no se pudo leer la base principal: {e}")
        return 0
    n = 0
    with conn_ctx() as c:
        for mint, created_at, grad_at in rows:
            variant, hours = classify(created_at, grad_at, mint)
            if variant:
                try:
                    sig_ms = int(datetime.fromisoformat(grad_at).timestamp() * 1000)
                except (TypeError, ValueError):
                    sig_ms = now_ms
                cur = c.execute("INSERT OR IGNORE INTO sg_signals (variant, mint, source, hours_to_grad, signal_ms, status) VALUES (?,?, 'own', ?, ?, 'pending')", (variant, mint, hours, sig_ms))
                n += cur.rowcount
        if rows:
            _set_state(c, "last_grad", rows[-1][2])
    return n


# ------------------------------------------------------------------ red
async def dex_pairs(client, mints):
    """{mint: [pares]} para hasta N mints (de a 30 por llamada)."""
    out = {m: [] for m in mints}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            r = await client.get(DEX + ",".join(chunk), timeout=20)
            r.raise_for_status()
            for p in r.json():
                m = (p.get("baseToken") or {}).get("address")
                if m in out:
                    out[m].append(p)
        except Exception as e:
            logger.warning(f"DexScreener: error consultando {len(chunk)} tokens: {e}")
            for m in chunk:
                out[m] = None                                        # None = no se pudo consultar (distinto de "sin par")
    return out


async def process_entries(client, now_ms):
    with conn_ctx() as c:
        pend = [dict(r) for r in c.execute("SELECT * FROM sg_signals WHERE status='pending' ORDER BY id LIMIT 200")]
    if not pend:
        return
    pairs = await dex_pairs(client, [p["mint"] for p in pend])
    with conn_ctx() as c:
        for p in pend:
            prs = pairs.get(p["mint"])
            info = choose_pair(prs, p["mint"]) if prs else None
            if info:
                if not accept_liquidity(info):
                    c.execute("UPDATE sg_signals SET status='rejected', symbol=?, entry_liq_usd=?, entry_quote_sol=?, note='liquidez real < minimo' WHERE id=?", (info["symbol"], info["liq_usd"], info["quote_sol"], p["id"]))
                    continue
                lag = (now_ms - p["signal_ms"]) / 1000
                c.execute("""UPDATE sg_signals SET status='active', symbol=?, entry_ms=?, entry_lag_s=?, entry_price=?, entry_liq_usd=?, entry_quote_sol=?, entry_quote_usd=?, sol_usd=?, pair=?, dex=? WHERE id=?""",
                          (info["symbol"], now_ms, lag, info["price"], info["liq_usd"], info["quote_sol"], info["quote_usd"], info["sol_usd"], info["pair"], info["dex"], p["id"]))
            elif prs is not None and (now_ms - p["signal_ms"]) / 1000 > ENTRY_TIMEOUT_S:
                c.execute("UPDATE sg_signals SET status='no_price', note='sin par en DexScreener tras 5 min' WHERE id=?", (p["id"],))


async def process_marks(client, now_ms):
    with conn_ctx() as c:
        act = [dict(r) for r in c.execute("SELECT * FROM sg_signals WHERE status='active'")]
        done = {}
        for r in c.execute("SELECT signal_id, horizon_s FROM sg_marks WHERE signal_id IN (SELECT id FROM sg_signals WHERE status='active')"):
            done.setdefault(r["signal_id"], set()).add(r["horizon_s"])
    due = []
    for s in act:
        for h in HORIZONS_S:
            if h not in done.get(s["id"], set()) and now_ms >= s["entry_ms"] + h * 1000:
                due.append((s, h))
    if not due:
        return
    mints = sorted({s["mint"] for s, _ in due})
    pairs = await dex_pairs(client, mints)
    with conn_ctx() as c:
        for s, h in due:
            prs = pairs.get(s["mint"])
            if prs is None:
                continue                                              # fallo de red: reintenta
            info = choose_pair(prs, s["mint"])
            if info:
                c.execute("INSERT OR IGNORE INTO sg_marks (signal_id, horizon_s, ts_ms, price, quote_usd, missed) VALUES (?,?,?,?,?,0)", (s["id"], h, now_ms, info["price"], info["quote_usd"]))
            elif now_ms - (s["entry_ms"] + h * 1000) > MISS_GRACE_S * 1000:
                c.execute("INSERT OR IGNORE INTO sg_marks (signal_id, horizon_s, ts_ms, price, quote_usd, missed) VALUES (?,?,?,NULL,NULL,1)", (s["id"], h, now_ms))
        for s in act:
            n = c.execute("SELECT COUNT(*) n FROM sg_marks WHERE signal_id=?", (s["id"],)).fetchone()["n"]
            if n >= len(HORIZONS_S):
                c.execute("UPDATE sg_signals SET status='done' WHERE id=?", (s["id"],))


async def run():
    init_db()
    logger.info("Graduaciones lentas (PAPEL) arrancando: detecta graduaciones de pump.fun, mide el precio del pool a 1 min - 24 h contra un control")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                now_ms = int(time.time() * 1000)
                n = detect_graduations(now_ms)
                if n:
                    logger.info(f"Graduaciones: {n} senales nuevas")
                await process_entries(client, now_ms)
                await process_marks(client, now_ms)
            except Exception:
                logger.exception("Graduaciones: error en el ciclo, reintenta")
            await asyncio.sleep(POLL_SECONDS)


# ------------------------------------------------------------------ lectura para el dashboard
def get_snapshot() -> dict:
    init_db()
    with conn_ctx() as c:
        sigs = [dict(r) for r in c.execute("SELECT * FROM sg_signals ORDER BY id DESC LIMIT 5000")]
        marks = {}
        for r in c.execute("SELECT * FROM sg_marks"):
            marks.setdefault(r["signal_id"], {})[r["horizon_s"]] = dict(r)
    variants = []
    recent = []
    for v, name in VARIANTS.items():
        vs = [s for s in sigs if s["variant"] == v]
        entered = [s for s in vs if s["status"] in ("active", "done")]
        lags = [s["entry_lag_s"] for s in entered if s["entry_lag_s"] is not None and v != "MANUAL"]
        horizons = []
        for h in STAT_HORIZONS_S:
            rows, missed = [], 0
            for s in entered:
                m = marks.get(s["id"], {}).get(h)
                if not m:
                    continue
                if m["missed"] or not m["price"]:
                    missed += 1
                    continue
                nm = net_multiple(s["entry_price"], s["entry_quote_usd"], m["price"], m["quote_usd"])
                if nm is None:
                    continue
                rows.append((m["price"] / s["entry_price"], nm))
            d = summarize(rows); d["horizon_s"] = h; d["missed"] = missed
            horizons.append(d)
        variants.append({"variant": v, "name": name, "signals": len(vs), "entered": len(entered), "rejected": sum(1 for s in vs if s["status"] == "rejected"),
                         "no_price": sum(1 for s in vs if s["status"] == "no_price"), "pending": sum(1 for s in vs if s["status"] == "pending"),
                         "median_lag_s": (statistics.median(lags) if lags else None), "horizons": horizons})
    for s in sigs[:200]:
        ms = marks.get(s["id"], {})
        last = None
        for h in sorted(ms):
            if ms[h]["price"]:
                last = (h, ms[h])
        d = {k: s[k] for k in ("id", "variant", "mint", "symbol", "hours_to_grad", "signal_ms", "status", "entry_lag_s", "entry_price", "entry_quote_sol", "entry_liq_usd", "note")}
        if last and s["entry_price"]:
            d["last_h"] = last[0]
            d["last_gross"] = last[1]["price"] / s["entry_price"]
            d["last_net"] = net_multiple(s["entry_price"], s["entry_quote_usd"], last[1]["price"], last[1]["quote_usd"])
        recent.append(d)
    return {"config": {"size_usd": SIZE_USD, "fee_side_pct": FEE_SIDE * 100, "tx_usd": TX_USD, "min_quote_sol": MIN_QUOTE_SOL, "slow_hours": SLOW_HOURS, "decision_horizon_s": DECISION_HORIZON_S},
            "variants": variants, "recent": recent, "bot_last_ms": max([s["signal_ms"] for s in sigs if s["variant"] == "BOT"], default=None)}
