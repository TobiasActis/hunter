"""
LABORATORIO DE SCALPING en PAPEL: simula en vivo, con precios reales de Binance futuros, varios metodos de scalping sobre BTC/ETH/SOL y los compara contra un control al azar.
NUNCA opera de verdad (sin claves ni firma). Comparte la base data/scalp.db con el motor CRT (tablas lab_*).

Metodo base (Order Block SMC del indicador "Scalping PRO", version mecanica; backtest 2023-2026 BTC/ETH/SOL, 15m con OB de 1h, n=3549):
  - OB en 1h: la ultima vela contraria entre las 3 previas a una vela 1h que cierra sobre el maximo (bajo el minimo) de las 10 velas 1h anteriores. Zona = [minimo, maximo] de esa vela;
    nace al cerrar la vela que rompe, vale 48 h, se descarta si una vela de 15m cierra al otro lado de la zona, y se usa UNA vez por variante (primer toque).
  - Toque: vela de 15m cerrada con low <= techo de la zona y close >= piso (largos); espejo para cortos.
  - SL = piso de la zona - 10% de su altura (techo + 10% para cortos); TP = 2R; salida por tiempo a 24 h; riesgo entre 0.1% y 2% del precio; una posicion por variante y moneda.
  - Tamano: riesgo fijo de RISK_USD (1% de $1000); notional = riesgo / distancia al stop (tope 10x el capital).
Variantes:
  OB        entrada a mercado al cerrar la vela del toque (orden taker: 0.05% + 0.02% de deslizamiento por lado).
  OB_RSI    igual pero solo si el RSI(14) de 15m < 35 (largos) / > 65 (cortos) en la vela del toque.
  OB_LIMIT  orden LIMITE (maker, 0.02% por lado) puesta al nacer la zona en el borde de la zona mas cercano al precio; se llena solo si el precio la CRUZA por 0.02% (para no ser optimista con la
            cola de la fila); el TP es limite (maker) y se ejecuta si el precio lo cruza por 0.02%; el stop y la salida por tiempo son taker.
  CONTROL   entrada a mercado al azar (1 de cada 40 velas de 15m por moneda, lado al azar) con riesgo muestreado de las senales reales de OB y TP a 2R: mide cuanto vale el simple azar con esos costos.
Resultados del backtest (bruto por operacion, en R): OB +0.07 (t~3), OB+RSI +0.11, costos taker ~0.26R => neto -0.19R; con maker ~0. No se espera ganar: se mide con ejecucion realista.
Resolucion de intrabarra: velas de 1 minuto; si en una vela tocan stop y objetivo a la vez, cuenta el STOP.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import httpx

from core.scalper import conn_ctx, now_iso

logger = logging.getLogger("hunter.scalp_lab")

FAPI = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
VARIANTS = {
    "OB": "OB a mercado",
    "OB_RSI": "OB + RSI extremo",
    "OB_LIMIT": "OB con orden limite (maker)",
    "CONTROL": "Control al azar",
}
BANKROLL = 1000.0
RISK_USD = 10.0
MAX_NOTIONAL_X = 10.0
FEE_TAKER = 0.0007            # 0.05% comision + 0.02% de deslizamiento, por lado
FEE_MAKER = 0.0002            # 0.02% por lado con orden limite
THROUGH = 0.0002              # el precio debe cruzar el nivel limite por 0.02% para llenarlo
MIN_RISK, MAX_RISK = 0.001, 0.02
ZONE_TTL_MS = 48 * 3_600_000
TIMEOUT_MS = 24 * 3_600_000
FRESH_MS = 150_000
CONTROL_P = 1 / 40
POLL_SECONDS = 30
RSI_LONG, RSI_SHORT = 35.0, 65.0
MS15, MS1H = 900_000, 3_600_000
DEFAULT_RISK_PCT = 0.0055

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    status TEXT NOT NULL,                       -- 'pending' | 'open' | 'closed' | 'cancelled'
    created_at TEXT NOT NULL, created_ms INTEGER NOT NULL, zone_key TEXT,
    entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL, risk_pct REAL NOT NULL, risk_usd REAL NOT NULL, notional REAL NOT NULL,
    filled_at TEXT, filled_ms INTEGER, closed_at TEXT, exit_price REAL, exit_reason TEXT,
    gross_pnl REAL, fees REAL, pnl_usd REAL, r_gross REAL, r_net REAL, checked_ms INTEGER, expires_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_lab_status ON lab_positions(status, variant, symbol);
CREATE TABLE IF NOT EXISTS lab_zone_use (variant TEXT NOT NULL, symbol TEXT NOT NULL, zone_key TEXT NOT NULL, note TEXT, PRIMARY KEY(variant, symbol, zone_key));
CREATE TABLE IF NOT EXISTS lab_state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS lab_marks (symbol TEXT PRIMARY KEY, price REAL, ts TEXT);
"""


def init_db():
    with conn_ctx() as c:
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ logica pura (sin red)
def htf_zones(candles):
    """candles: velas 1h CERRADAS [{t, o, h, l, c}] en orden. Devuelve [(t_cierre_ms, lado, piso, techo)]; lado +1 = OB alcista (compras), -1 = bajista."""
    out = []
    for j in range(11, len(candles)):
        cj = candles[j]
        if cj["c"] > max(x["h"] for x in candles[j - 10:j]):
            for k in (j - 1, j - 2, j - 3):
                if candles[k]["c"] < candles[k]["o"]:
                    out.append((cj["t"] + MS1H, 1, candles[k]["l"], candles[k]["h"])); break
        elif cj["c"] < min(x["l"] for x in candles[j - 10:j]):
            for k in (j - 1, j - 2, j - 3):
                if candles[k]["c"] > candles[k]["o"]:
                    out.append((cj["t"] + MS1H, -1, candles[k]["l"], candles[k]["h"])); break
    return out


def zone_key(t_close_ms, side, lo, hi):
    return f"{t_close_ms}:{side}:{lo:.8g}:{hi:.8g}"


def touch(side, lo, hi, cndl):
    """Vela de 15m cerrada toca la zona sin cerrar del otro lado (largos: low<=techo y close>=piso)."""
    return (cndl["l"] <= hi and cndl["c"] >= lo) if side == 1 else (cndl["h"] >= lo and cndl["c"] <= hi)


def broken(side, lo, hi, cndl):
    return cndl["c"] < lo if side == 1 else cndl["c"] > hi


def plan_from_zone(side, lo, hi, entry):
    """(stop, target, risk_pct) o None si la geometria no sirve (riesgo fuera de [0.1%, 2%] o entrada del otro lado del stop)."""
    h = hi - lo
    stop = lo - 0.1 * h if side == 1 else hi + 0.1 * h
    if (side == 1 and entry <= stop) or (side == -1 and entry >= stop):
        return None
    risk = abs(entry - stop)
    rp = risk / entry
    if not (MIN_RISK <= rp <= MAX_RISK):
        return None
    return stop, entry + side * 2 * risk, rp


def size_notional(entry, stop):
    return min(RISK_USD / (abs(entry - stop) / entry), MAX_NOTIONAL_X * BANKROLL)


def rsi14(closes, n=14):
    if len(closes) <= n:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    au, ad = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        au = (au * (n - 1) + gains[i]) / n
        ad = (ad * (n - 1) + losses[i]) / n
    return 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)


def finish(pos, exit_price, reason, exit_maker: bool):
    """Calcula PnL bruto, comisiones y R neto de una posicion que se cierra. La entrada es maker solo en OB_LIMIT."""
    side = 1 if pos["side"] == "long" else -1
    gross = side * (exit_price / pos["entry"] - 1.0) * pos["notional"]
    entry_fee = FEE_MAKER if pos["variant"] == "OB_LIMIT" else FEE_TAKER
    fees = pos["notional"] * (entry_fee + (FEE_MAKER if exit_maker else FEE_TAKER))
    pnl = gross - fees
    return {"exit_price": exit_price, "exit_reason": reason, "gross_pnl": gross, "fees": fees, "pnl_usd": pnl,
            "r_gross": gross / pos["risk_usd"], "r_net": pnl / pos["risk_usd"]}


def advance_position(pos, candles):
    """Aplica velas de 1 minuto (cronologicas, con t=apertura, cierre en t+60000) a una posicion 'pending' u 'open'.
    Devuelve (nuevo_estado dict con los campos a actualizar, ultimo_ms_revisado). Stop primero si una vela toca stop y objetivo."""
    upd = {}
    p = dict(pos)
    long = p["side"] == "long"
    last_ms = p.get("checked_ms")
    for k in candles:
        if last_ms is not None and k["t"] <= last_ms:
            continue
        last_ms = k["t"]
        if p["status"] == "pending":
            if k["t"] + 60_000 > p["expires_ms"]:
                upd.update({"status": "cancelled", "exit_reason": "vencida", "closed_at": now_iso()}); p["status"] = "cancelled"; break
            level = p["entry"]
            filled = (k["l"] <= level * (1 - THROUGH)) if long else (k["h"] >= level * (1 + THROUGH))
            if not filled:
                continue
            p["status"] = "open"; upd.update({"status": "open", "filled_at": now_iso(), "filled_ms": k["t"]}); p["filled_ms"] = k["t"]
            # en la vela del llenado solo cuenta el stop (no se sabe el orden dentro de la vela)
            if (long and k["l"] <= p["stop"]) or ((not long) and k["h"] >= p["stop"]):
                upd.update(finish(p, p["stop"], "stop", False)); upd.update({"status": "closed", "closed_at": now_iso()}); p["status"] = "closed"; break
            continue
        if p["status"] != "open":
            break
        stop_hit = (k["l"] <= p["stop"]) if long else (k["h"] >= p["stop"])
        if stop_hit:
            upd.update(finish(p, p["stop"], "stop", False)); upd.update({"status": "closed", "closed_at": now_iso()}); p["status"] = "closed"; break
        if p["variant"] == "OB_LIMIT":
            tp_hit = (k["h"] >= p["target"] * (1 + THROUGH)) if long else (k["l"] <= p["target"] * (1 - THROUGH))
            exit_maker = True
        else:
            tp_hit = (k["h"] >= p["target"]) if long else (k["l"] <= p["target"])
            exit_maker = False
        if tp_hit:
            upd.update(finish(p, p["target"], "objetivo", exit_maker)); upd.update({"status": "closed", "closed_at": now_iso()}); p["status"] = "closed"; break
        start = p.get("filled_ms") or p["created_ms"]
        if k["t"] + 60_000 - start >= TIMEOUT_MS:
            upd.update(finish(p, k["c"], "tiempo", False)); upd.update({"status": "closed", "closed_at": now_iso()}); p["status"] = "closed"; break
    upd["checked_ms"] = last_ms
    return upd, last_ms


# ------------------------------------------------------------------ base de datos
def _state(c, k, default=None):
    r = c.execute("SELECT v FROM lab_state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _set_state(c, k, v):
    c.execute("INSERT OR REPLACE INTO lab_state (k, v) VALUES (?,?)", (k, str(v)))


def _zone_used(c, variant, symbol, key):
    return c.execute("SELECT 1 FROM lab_zone_use WHERE variant=? AND symbol=? AND zone_key=?", (variant, symbol, key)).fetchone() is not None


def _use_zone(c, variant, symbol, key, note):
    c.execute("INSERT OR IGNORE INTO lab_zone_use (variant, symbol, zone_key, note) VALUES (?,?,?,?)", (variant, symbol, key, note))


def _busy(c, variant, symbol):
    return c.execute("SELECT 1 FROM lab_positions WHERE variant=? AND symbol=? AND status IN ('pending','open')", (variant, symbol)).fetchone() is not None


def _insert(c, variant, symbol, side, status, entry, stop, target, rp, zkey, now_ms, expires_ms=None):
    notional = size_notional(entry, stop)
    c.execute("""INSERT INTO lab_positions (variant, symbol, side, status, created_at, created_ms, zone_key, entry, stop, target, risk_pct, risk_usd, notional, filled_at, filled_ms, checked_ms, expires_ms)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (variant, symbol, "long" if side == 1 else "short", status, now_iso(), now_ms, zkey, entry, stop, target, rp, RISK_USD, notional,
               now_iso() if status == "open" else None, now_ms if status == "open" else None, now_ms, expires_ms))


def _update(c, pid, upd):
    if not upd:
        return
    cols = ", ".join(f"{k}=?" for k in upd)
    c.execute(f"UPDATE lab_positions SET {cols} WHERE id=?", (*upd.values(), pid))


# ------------------------------------------------------------------ red
async def fetch_klines(client, symbol, interval, limit=100, start_ms=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    r = await client.get(f"{FAPI}/fapi/v1/klines", params=params, timeout=20)
    r.raise_for_status()
    return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "ct": int(x[6])} for x in r.json()]


async def last_price(client, symbol):
    r = await client.get(f"{FAPI}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    return float(r.json()["price"])


async def process_symbol(client, symbol, now_ms):
    k15 = [k for k in await fetch_klines(client, symbol, "15m", 100) if k["ct"] < now_ms]
    k1h = [k for k in await fetch_klines(client, symbol, "1h", 100) if k["ct"] < now_ms]
    if len(k15) < 20 or len(k1h) < 14:
        return
    px = await last_price(client, symbol)
    with conn_ctx() as c:
        start_ms = int(_state(c, "start_ms", now_ms))
        _set_state(c, "start_ms", start_ms)
        last15 = _state(c, f"last15_{symbol}")
        # 1) gestionar posiciones vivas con velas de 1 minuto
        live = [dict(r) for r in c.execute("SELECT * FROM lab_positions WHERE symbol=? AND status IN ('pending','open')", (symbol,))]
    if live:
        since = min((p["checked_ms"] or p["created_ms"]) for p in live)
        k1 = [k for k in await fetch_klines(client, symbol, "1m", 1000, since) if k["ct"] < now_ms]
        with conn_ctx() as c:
            for p in live:
                upd, _ = advance_position(p, k1)
                _update(c, p["id"], upd)
                if upd.get("status") == "closed":
                    logger.info(f"Lab {p['variant']} {symbol} {p['side']} cerrada por {upd.get('exit_reason')}: R neto {upd.get('r_net', 0):+.2f} (bruto {upd.get('r_gross', 0):+.2f})")
    # 2) zonas OB de 1h nacidas despues del arranque
    zones = [z for z in htf_zones(k1h) if z[0] >= start_ms and now_ms - z[0] <= ZONE_TTL_MS]
    new15 = [k for k in k15 if last15 is None or k["t"] > int(last15)]
    with conn_ctx() as c:
        if last15 is None:
            _set_state(c, f"last15_{symbol}", k15[-1]["t"]); new15 = []
        # ordenes limite: se ponen al nacer la zona, en el borde mas cercano al precio, si el precio esta del lado correcto
        for (tc, side, lo, hi) in zones:
            key = zone_key(tc, side, lo, hi)
            if _zone_used(c, "OB_LIMIT", symbol, key):
                continue
            if _busy(c, "OB_LIMIT", symbol):
                continue
            level = hi if side == 1 else lo
            cur = k15[-1]["c"]
            plan = plan_from_zone(side, lo, hi, level)
            ok_side = (cur > level * (1 + THROUGH)) if side == 1 else (cur < level * (1 - THROUGH))
            if plan is None or not ok_side:
                _use_zone(c, "OB_LIMIT", symbol, key, "descartada"); continue
            _insert(c, "OB_LIMIT", symbol, side, "pending", level, plan[0], plan[1], plan[2], key, now_ms, expires_ms=tc + ZONE_TTL_MS)
            _use_zone(c, "OB_LIMIT", symbol, key, "orden puesta")
            logger.info(f"Lab OB_LIMIT {symbol}: orden {'compra' if side == 1 else 'venta'} limite en {level:.6g} (SL {plan[0]:.6g}, TP {plan[1]:.6g})")
        # cancelar ordenes pendientes cuya zona fue rota por un cierre de 15m
        pend = [dict(r) for r in c.execute("SELECT * FROM lab_positions WHERE symbol=? AND status='pending'", (symbol,))]
        # 3) velas de 15m recien cerradas: toques de zona y control al azar
        for cndl in new15:
            _set_state(c, f"last15_{symbol}", cndl["t"])
            fresh = now_ms - cndl["ct"] <= FRESH_MS
            for p in pend:
                zsplit = (p["zone_key"] or "").split(":")
                if len(zsplit) == 4 and p["status"] == "pending":
                    side, lo, hi = int(zsplit[1]), float(zsplit[2]), float(zsplit[3])
                    if broken(side, lo, hi, cndl):
                        c.execute("UPDATE lab_positions SET status='cancelled', exit_reason='zona rota', closed_at=? WHERE id=?", (now_iso(), p["id"])); p["status"] = "cancelled"
            closes = [k["c"] for k in k15 if k["t"] <= cndl["t"]]
            rsi = rsi14(closes[-80:])
            for (tc, side, lo, hi) in zones:
                if tc > cndl["t"]:
                    continue
                key = zone_key(tc, side, lo, hi)
                if broken(side, lo, hi, cndl):
                    for v in ("OB", "OB_RSI"):
                        _use_zone(c, v, symbol, key, "rota")
                    continue
                if not touch(side, lo, hi, cndl):
                    continue
                for v in ("OB", "OB_RSI"):
                    if _zone_used(c, v, symbol, key):
                        continue
                    _use_zone(c, v, symbol, key, "toque")                      # la zona se consume en el primer toque, con o sin confirmacion
                    if v == "OB_RSI" and (rsi is None or (rsi >= RSI_LONG if side == 1 else rsi <= RSI_SHORT)):
                        continue
                    if not fresh or _busy(c, v, symbol):
                        continue
                    entry = px                                                   # apertura de la vela siguiente ~ precio actual (la vela recien cerro)
                    plan = plan_from_zone(side, lo, hi, entry)
                    if plan is None:
                        continue
                    _insert(c, v, symbol, side, "open", entry, plan[0], plan[1], plan[2], key, now_ms)
                    logger.info(f"Lab {v} {symbol}: {'LARGO' if side == 1 else 'CORTO'} en {entry:.6g} (SL {plan[0]:.6g}, TP {plan[1]:.6g}, riesgo {plan[2]*100:.2f}%, RSI {rsi if rsi is None else round(rsi, 1)})")
            # control al azar
            if fresh and random.random() < CONTROL_P and not _busy(c, "CONTROL", symbol):
                rps = [r["risk_pct"] for r in c.execute("SELECT risk_pct FROM lab_positions WHERE variant IN ('OB','OB_RSI') ORDER BY id DESC LIMIT 60")]
                rp = random.choice(rps) if rps else DEFAULT_RISK_PCT
                side = random.choice((1, -1)); entry = px
                stop = entry * (1 - side * rp)
                _insert(c, "CONTROL", symbol, side, "open", entry, stop, entry + side * 2 * rp * entry, rp, None, now_ms)
        c.execute("INSERT OR REPLACE INTO lab_marks VALUES (?,?,?)", (symbol, k15[-1]["c"], now_iso()))


async def poll_once(client):
    now_ms = int(time.time() * 1000)
    for s in SYMBOLS:
        try:
            await process_symbol(client, s, now_ms)
        except Exception as e:
            logger.warning(f"Lab: error con {s}: {e}")
        await asyncio.sleep(0.2)


async def run():
    init_db()
    logger.info(f"Laboratorio de scalping (PAPEL) arrancando: {', '.join(VARIANTS)} sobre {', '.join(SYMBOLS)}, cada {POLL_SECONDS} s")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await poll_once(client)
            except Exception:
                logger.exception("Lab: error en el ciclo, reintenta")
            await asyncio.sleep(POLL_SECONDS)


# ------------------------------------------------------------------ lectura para el dashboard
def get_snapshot() -> dict:
    init_db()
    with conn_ctx() as c:
        marks = {r["symbol"]: r["price"] for r in c.execute("SELECT symbol, price FROM lab_marks")}
        rows = []
        for v, name in VARIANTS.items():
            cl = [dict(r) for r in c.execute("SELECT * FROM lab_positions WHERE variant=? AND status='closed'", (v,))]
            n = len(cl)
            rn = [p["r_net"] for p in cl]; rg = [p["r_gross"] for p in cl]
            avg = sum(rn) / n if n else None
            sd = (sum((x - avg) ** 2 for x in rn) / (n - 1)) ** 0.5 if n > 1 else None
            rows.append({"variant": v, "name": name, "n": n, "wins": sum(1 for p in cl if p["pnl_usd"] > 0), "pnl": sum(p["pnl_usd"] for p in cl),
                         "avg_r_net": avg, "avg_r_gross": (sum(rg) / n if n else None), "ci95": (1.96 * sd / n ** 0.5 if sd is not None else None),
                         "fees": sum(p["fees"] for p in cl), "cost_r": (sum(p["fees"] for p in cl) / (n * RISK_USD) if n else None),
                         "open": c.execute("SELECT COUNT(*) n FROM lab_positions WHERE variant=? AND status='open'", (v,)).fetchone()["n"],
                         "pending": c.execute("SELECT COUNT(*) n FROM lab_positions WHERE variant=? AND status='pending'", (v,)).fetchone()["n"],
                         "cancelled": c.execute("SELECT COUNT(*) n FROM lab_positions WHERE variant=? AND status='cancelled'", (v,)).fetchone()["n"]})
        live = []
        for r in c.execute("SELECT * FROM lab_positions WHERE status IN ('open','pending') ORDER BY created_ms DESC"):
            d = dict(r); m = marks.get(d["symbol"]); d["mark"] = m
            if d["status"] == "open" and m:
                sd = 1 if d["side"] == "long" else -1
                d["unrealized_usd"] = sd * (m / d["entry"] - 1.0) * d["notional"]
            d["variant_name"] = VARIANTS[d["variant"]]
            live.append(d)
        closed = [dict(r) | {"variant_name": VARIANTS[r["variant"]]} for r in c.execute("SELECT * FROM lab_positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 40")]
        return {"config": {"risk_usd": RISK_USD, "bankroll": BANKROLL, "fee_taker_pct": FEE_TAKER * 100, "fee_maker_pct": FEE_MAKER * 100},
                "variants": rows, "live": live, "closed": closed, "started": _state(c, "start_ms"), "marks": marks}
