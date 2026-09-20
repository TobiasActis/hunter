"""
Scalping en PAPEL de criptos establecidas (BTC, ETH, SOL) con el metodo CRT (Candle Range Theory).
NUNCA opera de verdad: no hay claves ni firma de transacciones; solo lee precios publicos de Binance y simula
posiciones (con palanca) en su propia base data/scalp.db, separada de hunter.db para no competir por el lock.

Regla CRT (mecanica, fijada antes de mirar resultados; ver core/scalper.py::detect_crt y el backtest):
  C1 = penultima vela cerrada [L1, H1]; C2 = ultima vela cerrada.
  CORTO si C2 barre el maximo de C1 (high > H1) y cierra de vuelta dentro (L1 <= close <= H1); LARGO si barre el minimo.
  Entrada: apertura de la vela siguiente. Stop: extremo barrido por C2. Objetivo: extremo opuesto de C1.
  Salida por tiempo a las N_MAX velas. Si una vela toca stop y objetivo a la vez se asume el STOP (conservador).
Tamano por riesgo fijo: RISK_PCT del capital por operacion; apalancamiento = notional / capital (tope MAX_LEVERAGE).

RESULTADO DEL BACKTEST (2020-2026, BTC/ETH/SOL, 15m/1h/4h, costo 0.14% ida y vuelta): -0.2R a -1.3R por operacion; la
operacion INVERSA rinde casi igual, o sea que el patron no mostro ventaja. Ninguna variante (tendencia, sesiones,
rango, comision maker) fue robusta. Esta vista existe para medirlo EN VIVO con datos nuevos, no porque funcione.
"""
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import contextmanager

import httpx

from config.settings import DB_PATH

logger = logging.getLogger("hunter.scalper")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAMES = ["15m", "1h"]
BANKROLL_USD = 1000.0
RISK_PCT = 0.01
MAX_LEVERAGE = 10.0
FEE_SIDE = 0.0007          # 0.05% comision taker + 0.02% slippage por lado, sobre el notional
MAINT_MARGIN = 0.005
N_MAX = 12
MIN_STOP_PCT = 0.0005
POLL_SECONDS = 15
KLINES_LIMIT = 40
FRESH_MS = 90_000
SCALP_DB = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "scalp.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scalp_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, tf TEXT NOT NULL, side TEXT NOT NULL,          -- 'long' | 'short'
    opened_at TEXT NOT NULL, entry_candle_ms INTEGER NOT NULL,
    entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL,
    equity_usd REAL NOT NULL, risk_usd REAL NOT NULL, notional REAL NOT NULL, leverage REAL NOT NULL, liq_price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',                                  -- 'open' | 'closed'
    exit_price REAL, exit_reason TEXT, closed_at TEXT, pnl_usd REAL, r_multiple REAL
);
CREATE TABLE IF NOT EXISTS scalp_state (symbol TEXT, tf TEXT, last_signal_ms INTEGER, PRIMARY KEY(symbol, tf));
CREATE TABLE IF NOT EXISTS scalp_marks (symbol TEXT PRIMARY KEY, price REAL, ts TEXT);
"""


@contextmanager
def conn_ctx():
    conn = sqlite3.connect(SCALP_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with conn_ctx() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


# ------------------------------------------------------------------ logica pura (testeable sin red)
def detect_crt(c1, c2):
    """c = (open_ms, o, h, l, c, close_ms). Devuelve (lado, stop) o None."""
    H1, L1 = c1[2], c1[3]
    if not (L1 <= c2[4] <= H1):
        return None
    if c2[2] > H1:
        return "short", c2[2]
    if c2[3] < L1:
        return "long", c2[3]
    return None


def plan_trade(side, stop, c1, entry):
    """Objetivo = extremo opuesto de C1. None si la geometria no es valida (mismos filtros que el backtest)."""
    target = c1[3] if side == "short" else c1[2]
    if abs(entry - stop) / entry < MIN_STOP_PCT:
        return None
    if side == "short" and not (entry < stop and target < entry):
        return None
    if side == "long" and not (entry > stop and target > entry):
        return None
    return {"side": side, "stop": stop, "target": target}


def evaluate_position(pos, candles, now_ms):
    """Devuelve (precio_salida, motivo, fin_ms) o None. `candles` ordenadas; se miran las de open_ms >= vela de entrada,
    incluida la vela en formacion (su high/low parcial ya cuenta). Stop antes que objetivo dentro de una misma vela."""
    idx = 0
    for c in candles:
        if c[0] < pos["entry_candle_ms"]:
            continue
        o, h, l, cl, close_ms = c[1], c[2], c[3], c[4], c[5]
        if pos["side"] == "long":
            hit_stop, hit_tgt = l <= pos["stop"], h >= pos["target"]
        else:
            hit_stop, hit_tgt = h >= pos["stop"], l <= pos["target"]
        if hit_stop:
            return pos["stop"], "stop", close_ms
        if hit_tgt:
            return pos["target"], "objetivo", close_ms
        idx += 1
        if close_ms < now_ms and idx >= N_MAX:
            return cl, "tiempo", close_ms
    return None


def size_position(equity, entry, stop):
    stop_pct = abs(entry - stop) / entry
    risk_usd = equity * RISK_PCT
    notional = risk_usd / stop_pct
    lev = notional / equity
    if lev > MAX_LEVERAGE:
        notional = equity * MAX_LEVERAGE
        risk_usd = notional * stop_pct
        lev = MAX_LEVERAGE
    return risk_usd, notional, lev


def liquidation_price(side, entry, lev):
    dist = max(1.0 / lev - MAINT_MARGIN, 0.0)
    return entry * (1 - dist) if side == "long" else entry * (1 + dist)


def result_of(side, entry, exit_price, notional, risk_usd):
    gross = (exit_price - entry) / entry * notional * (1 if side == "long" else -1)
    pnl = gross - 2 * FEE_SIDE * notional
    return pnl, pnl / risk_usd


# ------------------------------------------------------------------ persistencia
def current_equity(c):
    r = c.execute("SELECT COALESCE(SUM(pnl_usd), 0) s FROM scalp_positions WHERE status='closed'").fetchone()
    return BANKROLL_USD + r["s"]


def open_paper_position(c, symbol, tf, plan, entry, entry_candle_ms):
    eq = current_equity(c)
    risk_usd, notional, lev = size_position(eq, entry, plan["stop"])
    c.execute(
        """INSERT INTO scalp_positions (symbol, tf, side, opened_at, entry_candle_ms, entry, stop, target,
           equity_usd, risk_usd, notional, leverage, liq_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (symbol, tf, plan["side"], now_iso(), entry_candle_ms, entry, plan["stop"], plan["target"], eq, risk_usd,
         notional, lev, liquidation_price(plan["side"], entry, lev)),
    )


def close_paper_position(c, pos, exit_price, reason):
    pnl, r = result_of(pos["side"], pos["entry"], exit_price, pos["notional"], pos["risk_usd"])
    c.execute(
        "UPDATE scalp_positions SET status='closed', exit_price=?, exit_reason=?, closed_at=?, pnl_usd=?, r_multiple=? WHERE id=?",
        (exit_price, reason, now_iso(), pnl, r, pos["id"]),
    )
    logger.info(f"Scalper: #{pos['id']} {pos['symbol']} {pos['tf']} {pos['side']} cerrada por {reason}: ${pnl:+.2f} ({r:+.2f}R)")


# ------------------------------------------------------------------ red
async def fetch_klines(client, symbol, tf):
    resp = await client.get("https://api.binance.com/api/v3/klines",
                            params={"symbol": symbol, "interval": tf, "limit": KLINES_LIMIT}, timeout=15)
    resp.raise_for_status()
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])) for k in resp.json()]


async def poll_once(client):
    now_ms = int(time.time() * 1000)
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                kl = await fetch_klines(client, symbol, tf)
            except Exception as e:
                logger.warning(f"Scalper: sin velas de {symbol} {tf}: {e}")
                continue
            if len(kl) < 5:
                continue
            with conn_ctx() as c:
                c.execute("INSERT OR REPLACE INTO scalp_marks VALUES (?,?,?)", (symbol, kl[-1][4], now_iso()))
                # 1) gestionar posiciones abiertas de este simbolo y marco
                for pos in c.execute("SELECT * FROM scalp_positions WHERE status='open' AND symbol=? AND tf=?", (symbol, tf)).fetchall():
                    res = evaluate_position(dict(pos), kl, now_ms)
                    if res:
                        close_paper_position(c, pos, res[0], res[1])
                # 2) senal nueva: ultimas dos velas CERRADAS = kl[-3], kl[-2]; kl[-1] esta en formacion
                if kl[-2][5] >= now_ms:
                    continue
                st = c.execute("SELECT last_signal_ms FROM scalp_state WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
                if st and st["last_signal_ms"] >= kl[-2][0]:
                    continue
                c.execute("INSERT OR REPLACE INTO scalp_state VALUES (?,?,?)", (symbol, tf, kl[-2][0]))
                sig = detect_crt(kl[-3], kl[-2])
                if not sig:
                    continue
                # Señal vieja (ej. recien arrancado el proceso): el precio de apertura de la vela en formacion ya
                # no es el precio actual, entrar ahi seria inventar el llenado. Solo se opera si la vela abrio hace poco.
                if now_ms - kl[-1][0] > FRESH_MS:
                    logger.info(f"Scalper: senal CRT {sig[0]} en {symbol} {tf} IGNORADA por vieja ({(now_ms - kl[-1][0]) / 1000:.0f}s desde la apertura)")
                    continue
                if c.execute("SELECT 1 FROM scalp_positions WHERE status='open' AND symbol=? AND tf=?", (symbol, tf)).fetchone():
                    continue
                entry = kl[-1][1]        # apertura de la vela en formacion = "apertura siguiente" del backtest
                plan = plan_trade(sig[0], sig[1], kl[-3], entry)
                if plan:
                    open_paper_position(c, symbol, tf, plan, entry, kl[-1][0])
                    logger.info(f"Scalper: CRT {sig[0]} en {symbol} {tf} entrada {entry:.6g} stop {plan['stop']:.6g} objetivo {plan['target']:.6g}")


async def run():
    init_db()
    logger.info(f"Scalper (PAPEL) arrancando: {SYMBOLS} {TIMEFRAMES}, riesgo {RISK_PCT:.0%}/operacion, palanca max {MAX_LEVERAGE:.0f}x")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await poll_once(client)
            except Exception:
                logger.exception("Scalper: error en el ciclo, sigue")
            await asyncio.sleep(POLL_SECONDS)


# ------------------------------------------------------------------ lectura para el dashboard
BASELINE = {"nota": "Backtest 2020-2026 (BTC/ETH/SOL), costo 0.14% ida y vuelta: CRT dio entre -0.2R y -1.3R por operacion; la inversa rindio casi igual.",
            "r_por_operacion": {"BTC 1h": -0.62, "ETH 1h": -0.50, "SOL 1h": -0.34, "BTC 15m": -1.09, "ETH 15m": -0.93, "SOL 15m": -0.69}}


def get_snapshot():
    init_db()
    with conn_ctx() as c:
        marks = {r["symbol"]: r["price"] for r in c.execute("SELECT symbol, price FROM scalp_marks")}
        equity = current_equity(c)
        open_rows = []
        for r in c.execute("SELECT * FROM scalp_positions WHERE status='open' ORDER BY id DESC"):
            d = dict(r)
            m = marks.get(d["symbol"])
            d["mark"] = m
            if m:
                d["unrealized_usd"], d["unrealized_r"] = result_of(d["side"], d["entry"], m, d["notional"], d["risk_usd"])
            open_rows.append(d)
        closed = [dict(r) for r in c.execute("SELECT * FROM scalp_positions WHERE status='closed' ORDER BY id DESC LIMIT 100")]

        def stats(where="", args=()):
            r = c.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(pnl_usd>0),0) w, COALESCE(SUM(pnl_usd),0) pnl, COALESCE(AVG(r_multiple),0) ar
                              FROM scalp_positions WHERE status='closed' {where}""", args).fetchone()
            return {"n": r["n"], "wins": r["w"], "pnl": r["pnl"], "avg_r": r["ar"]}
        by_combo = {f"{s.replace('USDT', '')} {tf}": stats("AND symbol=? AND tf=?", (s, tf)) for tf in TIMEFRAMES for s in SYMBOLS}
        return {"config": {"symbols": SYMBOLS, "timeframes": TIMEFRAMES, "bankroll": BANKROLL_USD, "risk_pct": RISK_PCT,
                           "max_leverage": MAX_LEVERAGE, "fee_side_pct": FEE_SIDE * 100, "n_max": N_MAX},
                "equity": equity, "total": stats(), "by_combo": by_combo, "marks": marks,
                "open": open_rows, "closed": closed, "baseline": BASELINE}
