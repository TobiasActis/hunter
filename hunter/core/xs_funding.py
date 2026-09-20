"""
Senal de FUNDING entre monedas en PAPEL: cada lunes compra (largo) las monedas de perpetuos con el funding MAS BAJO de los ultimos 3 dias y vende (corto) las de funding
MAS ALTO, entre 28 monedas fijas de Binance USDT-M. Dolar neutral, cada pata suma NOTIONAL_LEG repartido en partes iguales. NUNCA opera de verdad.

Por que: el funding alto marca monedas con longs apalancados y apretados; el backtest 2020-2026 (28 monedas, semanal, costos 0.07% por lado, funding incluido) dio +49% a +96% anual
neto segun el dia de arranque (7 de 7 positivos), t 2.0-3.6, Sharpe 0.8-1.4, con 0.5 de capital por pata (sin apalancamiento) +31-37% anual, caida max -25% a -40%, peor semana -8% a -14%.
Resistio: ventanas de funding 1/3/7/14 dias, ambas mitades del tiempo, sin 2021 (+42%), solo 2025-26 (+68%), 18/20 mitades aleatorias de monedas, placebo de ranking al azar (~0),
no es reversion de retorno (esa da negativo) ni efecto tamano (regla estatica BTC/ETH/BNB contra el resto = 0%); entre altcoins solas +62% a +83%.
LO QUE JUEGA EN CONTRA (por eso se mide en papel): casi todo viene de la pata larga (+103% en precio; la corta pierde 39% en precio y gana 15% en funding), 2021 aporto +308% y 2023 fue -34%,
las 28 monedas son sobrevivientes de hoy y hay monedas casi siempre en el mismo extremo (BNB/BCH/TRX/ATOM/DOT con funding bajo; ALGO/UNI/FIL/HBAR/OP alto), asi que puede haber un efecto de identidad.

Reglas (identicas al backtest):
 - Cada lunes 00:10 UTC (o el primer ciclo despues, si estuvo caido) se toman los eventos de funding de los 3 dias UTC completos anteriores (cada 8 h: 9 eventos; se exigen >= 8);
   puntaje = suma de tasas / 3 (funding diario medio). Se ordena; k = max(2, n // 5); LARGOS = k monedas de puntaje mas bajo, CORTOS = k de puntaje mas alto. Mantiene 7 dias.
 - Lo que se repite (misma moneda y lado) sigue sin costo; costo 0.07% por lado sobre el nocional operado.
 - Funding cobrado/pagado de verdad: cada evento de 8 h suma -signo * tasa * nocional (el largo paga cuando la tasa es positiva, el corto cobra).
 - Stop-loss 40% por posicion con velas de 5m (stop primero, 0.3% de slippage adverso), sin take-profit: medido con velas de 1h dejo el retorno igual (+37% contra +37%)
   y bajo la peor semana de -13.7% a -8.0%; stops mas cortos (25%, 20%, 15%) empeoraban el resultado (+31%, +28%, +24%). Tras un stop la posicion queda cerrada hasta el lunes.
 - No hay arranque a mitad de semana: la primera cartera se arma el primer lunes 00:10 UTC tras el despliegue.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
import numpy as np

from core.scalper import conn_ctx, now_iso

logger = logging.getLogger("hunter.xs_funding")

UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "TRX", "ATOM", "NEAR", "UNI", "AAVE", "FIL", "ETC", "XLM",
            "ALGO", "INJ", "ARB", "OP", "SUI", "APT", "BCH", "HBAR", "TON"]
FAPI = "https://fapi.binance.com"
WINDOW_DAYS = 3
MIN_EVENTS = 8                  # de 9 posibles en 3 dias
MIN_UNIVERSE = 12
QUANTILE = 0.2
NOTIONAL_LEG = 500.0
BANKROLL = 1000.0
FEE_SIDE = 0.0007
SL_PCT = 0.40
STOP_SLIP = 0.003
REBAL_WEEKDAY = 0               # lunes
REBAL_MINUTE_OF_DAY = 10        # 00:10 UTC
CHECK_SECONDS = 300
POLL_SECONDS = 1800
DAY_MS = 86_400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS fd_rebalances (
    week_key TEXT PRIMARY KEY, ts TEXT NOT NULL, universe_n INTEGER, n_long INTEGER, n_short INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS fd_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    notional REAL NOT NULL, score REAL, entry REAL NOT NULL, opened_at TEXT NOT NULL, entry_fee REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open', exit_price REAL, closed_at TEXT, exit_fee REAL, funding_usd REAL NOT NULL DEFAULT 0, pnl_usd REAL,
    carried INTEGER NOT NULL DEFAULT 0, exit_reason TEXT, checked_ms INTEGER, fund_ms INTEGER
);
CREATE TABLE IF NOT EXISTS fd_marks (symbol TEXT PRIMARY KEY, price REAL, ts TEXT);
"""


def init_db():
    with conn_ctx() as c:
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ logica pura (sin red)
def funding_score(events, now_ms: int):
    """events: [(fundingTime_ms, rate), ...]. Puntaje = funding diario medio de los WINDOW_DAYS dias UTC completos anteriores a hoy, o None si faltan eventos."""
    end = (now_ms // DAY_MS) * DAY_MS
    start = end - WINDOW_DAYS * DAY_MS
    w = [r for t, r in events if start <= t < end]
    if len(w) < MIN_EVENTS:
        return None
    return float(sum(w)) / WINDOW_DAYS


def select_portfolio(scores: dict):
    """Devuelve (largos, cortos): largos = k monedas de funding mas BAJO, cortos = k de funding mas ALTO. Desempate por simbolo."""
    order = sorted(scores, key=lambda s: (scores[s], s))
    if len(order) < MIN_UNIVERSE:
        return [], []
    k = max(2, int(len(order) * QUANTILE))
    return order[:k], order[-k:]


def exit_decision(side: str, entry: float, candles):
    """candles: [(high, low), ...] cronologicas. Devuelve ('stop', precio_de_salida) o None. Solo stop-loss."""
    if side == "long":
        sl = entry * (1 - SL_PCT)
        for hi, lo in candles:
            if lo <= sl:
                return "stop", sl * (1 - STOP_SLIP)
    else:
        sl = entry * (1 + SL_PCT)
        for hi, lo in candles:
            if hi >= sl:
                return "stop", sl * (1 + STOP_SLIP)
    return None


def funding_pnl(side: str, notional: float, events) -> float:
    """PnL de funding de una posicion: el largo paga la tasa cuando es positiva, el corto la cobra."""
    sign = 1.0 if side == "long" else -1.0
    return -sign * notional * float(sum(r for _, r in events))


def close_position(conn, r, price: float, reason: str):
    gross = (price / r["entry"] - 1.0) * (1 if r["side"] == "long" else -1) * r["notional"]
    exit_fee = r["notional"] * FEE_SIDE
    conn.execute("UPDATE fd_positions SET status='closed', exit_price=?, closed_at=?, exit_fee=?, pnl_usd=?, exit_reason=? WHERE id=?",
                 (price, now_iso(), exit_fee, gross - r["entry_fee"] - exit_fee + r["funding_usd"], reason, r["id"]))


def rebalance(conn, week_key: str, longs, shorts, scores: dict, prices: dict, universe_n: int, now_ms: int, note: str = ""):
    """Cierra lo que no se repite y abre los nuevos objetivos a los precios dados. Lo que se repite (misma moneda y lado) sigue sin costo."""
    now = now_iso()
    target = {s: "long" for s in longs}
    target.update({s: "short" for s in shorts})
    k_l, k_s = max(len(longs), 1), max(len(shorts), 1)
    opened = closed = carried = 0
    for r in conn.execute("SELECT * FROM fd_positions WHERE status='open'").fetchall():
        px = prices.get(r["symbol"])
        if px is None:
            continue
        gross = (px / r["entry"] - 1.0) * (1 if r["side"] == "long" else -1) * r["notional"]
        keep = target.get(r["symbol"]) == r["side"]
        exit_fee = 0.0 if keep else r["notional"] * FEE_SIDE
        conn.execute("UPDATE fd_positions SET status='closed', exit_price=?, closed_at=?, exit_fee=?, pnl_usd=?, exit_reason=? WHERE id=?",
                     (px, now, exit_fee, gross - r["entry_fee"] - exit_fee + r["funding_usd"], "continua" if keep else "reequilibrio", r["id"]))
        closed += 1
        if keep:
            n = NOTIONAL_LEG / (k_l if r["side"] == "long" else k_s)
            conn.execute("""INSERT INTO fd_positions (week_key, symbol, side, notional, score, entry, opened_at, entry_fee, carried, fund_ms)
                            VALUES (?,?,?,?,?,?,?,0,1,?)""", (week_key, r["symbol"], r["side"], n, scores.get(r["symbol"]), px, now, now_ms))
            target.pop(r["symbol"]); carried += 1
    for sym, side in target.items():
        px = prices.get(sym)
        if px is None:
            continue
        n = NOTIONAL_LEG / (k_l if side == "long" else k_s)
        conn.execute("""INSERT INTO fd_positions (week_key, symbol, side, notional, score, entry, opened_at, entry_fee, carried, fund_ms)
                        VALUES (?,?,?,?,?,?,?,?,0,?)""", (week_key, sym, side, n, scores.get(sym), px, now, n * FEE_SIDE, now_ms))
        opened += 1
    conn.execute("INSERT OR REPLACE INTO fd_rebalances (week_key, ts, universe_n, n_long, n_short, note) VALUES (?,?,?,?,?,?)",
                 (week_key, now, universe_n, len(longs), len(shorts), note))
    return {"opened": opened, "closed": closed, "carried": carried}


def week_key_of(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


# ------------------------------------------------------------------ red
async def fetch_funding_events(client, symbol: str, limit: int = 15, start_ms: int | None = None):
    params = {"symbol": f"{symbol}USDT", "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    r = await client.get(f"{FAPI}/fapi/v1/fundingRate", params=params, timeout=20)
    r.raise_for_status()
    return [(int(x["fundingTime"]), float(x["fundingRate"])) for x in r.json()]


async def fetch_prices(client, symbols):
    r = await client.get(f"{FAPI}/fapi/v1/ticker/price", timeout=30)
    r.raise_for_status()
    want = {f"{s}USDT": s for s in symbols}
    return {want[t["symbol"]]: float(t["price"]) for t in r.json() if t["symbol"] in want}


async def accrue_funding(client):
    """Suma a cada posicion abierta los eventos de funding liquidados desde la ultima vez (largo paga si la tasa es positiva, corto cobra)."""
    with conn_ctx() as c:
        opens = [dict(r) for r in c.execute("SELECT * FROM fd_positions WHERE status='open'")]
    for r in opens:
        opened_ms = int(datetime.fromisoformat(r["opened_at"]).timestamp() * 1000)
        start = int(r["fund_ms"] or opened_ms)
        try:
            ev = await fetch_funding_events(client, r["symbol"], limit=100, start_ms=start + 1)
        except Exception as e:
            logger.warning(f"Funding: sin eventos de {r['symbol']}: {e}")
            continue
        ev = [(t, x) for t, x in ev if t > start]
        if not ev:
            continue
        add = funding_pnl(r["side"], r["notional"], ev)
        with conn_ctx() as c:
            c.execute("UPDATE fd_positions SET funding_usd = funding_usd + ?, fund_ms=? WHERE id=?", (add, max(t for t, _ in ev), r["id"]))
        await asyncio.sleep(0.1)


async def do_rebalance(client, now_dt: datetime, note: str = ""):
    now_ms = int(now_dt.timestamp() * 1000)
    scores = {}
    for s in UNIVERSE:
        try:
            sc = funding_score(await fetch_funding_events(client, s, limit=15), now_ms)
        except Exception as e:
            logger.warning(f"Funding: sin datos de {s}: {e}")
            continue
        if sc is not None:
            scores[s] = sc
        await asyncio.sleep(0.1)
    longs, shorts = select_portfolio(scores)
    if not longs:
        logger.warning(f"Funding: universo insuficiente ({len(scores)} monedas), no se rebalancea")
        return None
    await accrue_funding(client)                                            # que el PnL de lo que se cierra incluya el ultimo funding
    with conn_ctx() as c:
        open_syms = [r["symbol"] for r in c.execute("SELECT symbol FROM fd_positions WHERE status='open'")]
    prices = await fetch_prices(client, set(longs) | set(shorts) | set(open_syms))
    with conn_ctx() as c:
        res = rebalance(c, week_key_of(now_dt), longs, shorts, scores, prices, len(scores), now_ms, note)
    logger.info(f"Funding (PAPEL) {week_key_of(now_dt)}: largos (funding bajo) {[(s, round(scores[s] * 100, 4)) for s in longs]} | cortos (funding alto) {[(s, round(scores[s] * 100, 4)) for s in shorts]} | {res}")
    return res


async def check_exits(client):
    """Revisa el stop-loss de las posiciones abiertas con las velas de 5m de futuros desde la ultima revision."""
    with conn_ctx() as c:
        opens = [dict(r) for r in c.execute("SELECT * FROM fd_positions WHERE status='open'")]
    for r in opens:
        opened_ms = int(datetime.fromisoformat(r["opened_at"]).timestamp() * 1000)
        start = int(r["checked_ms"] or opened_ms)
        try:
            k = await client.get(f"{FAPI}/fapi/v1/klines", params={"symbol": f"{r['symbol']}USDT", "interval": "5m", "startTime": start, "limit": 1000}, timeout=20)
            k.raise_for_status()
        except Exception as e:
            logger.warning(f"Funding: sin velas de {r['symbol']}: {e}")
            continue
        rows = k.json()
        candles = [(float(x[2]), float(x[3])) for x in rows]
        last_open = int(rows[-1][0]) if rows else start
        with conn_ctx() as c:
            hit = exit_decision(r["side"], r["entry"], candles)
            if hit:
                close_position(c, r, hit[1], hit[0])
                logger.info(f"Funding (PAPEL): {r['symbol']} {r['side']} cerrada por {hit[0]} a {hit[1]:.6g} (entrada {r['entry']:.6g})")
            else:
                c.execute("UPDATE fd_positions SET checked_ms=? WHERE id=?", (last_open, r["id"]))


async def refresh_marks(client):
    with conn_ctx() as c:
        syms = {r["symbol"] for r in c.execute("SELECT symbol FROM fd_positions WHERE status='open'")}
    if not syms:
        return
    prices = await fetch_prices(client, syms)
    with conn_ctx() as c:
        for s, p in prices.items():
            c.execute("INSERT OR REPLACE INTO fd_marks VALUES (?,?,?)", (s, p, now_iso()))


def rebalance_due(now: datetime, n_rebal: int, done_this_week: bool) -> bool:
    """Lunes >= 00:10 UTC; si el proceso estuvo caido, en cualquier dia posterior de esa semana ISO. Nunca arranca a mitad de semana la primera vez."""
    if done_this_week:
        return False
    if now.weekday() == REBAL_WEEKDAY:
        return (now.hour * 60 + now.minute) >= REBAL_MINUTE_OF_DAY
    return n_rebal > 0


async def run():
    init_db()
    logger.info(f"Funding semanal (PAPEL) arrancando: primera cartera el proximo lunes 00:10 UTC; SL {SL_PCT:.0%} cada {CHECK_SECONDS // 60} min")
    async with httpx.AsyncClient() as client:
        last_check = 0.0
        while True:
            try:
                await check_exits(client)
                if time.time() - last_check < POLL_SECONDS:
                    await asyncio.sleep(CHECK_SECONDS)
                    continue
                last_check = time.time()
                now = datetime.now(timezone.utc)
                with conn_ctx() as c:
                    n_rebal = c.execute("SELECT COUNT(*) n FROM fd_rebalances").fetchone()["n"]
                    done = c.execute("SELECT 1 FROM fd_rebalances WHERE week_key=?", (week_key_of(now),)).fetchone() is not None
                if rebalance_due(now, n_rebal, done):
                    await do_rebalance(client, now)
                else:
                    await accrue_funding(client)
                await refresh_marks(client)
            except Exception:
                logger.exception("Funding: error en el ciclo, reintenta")
            await asyncio.sleep(CHECK_SECONDS)


# ------------------------------------------------------------------ lectura para el dashboard
def get_snapshot() -> dict:
    init_db()
    with conn_ctx() as c:
        marks = {r["symbol"]: r["price"] for r in c.execute("SELECT symbol, price FROM fd_marks")}
        open_rows = []
        for r in c.execute("SELECT * FROM fd_positions WHERE status='open' ORDER BY side DESC, symbol"):
            d = dict(r); m = marks.get(d["symbol"])
            d["mark"] = m
            d["sl"] = d["entry"] * (1 - SL_PCT) if d["side"] == "long" else d["entry"] * (1 + SL_PCT)
            if m:
                g = (m / d["entry"] - 1.0) * (1 if d["side"] == "long" else -1) * d["notional"]
                d["price_pnl"] = g
                d["unrealized_usd"] = g - d["entry_fee"] - d["notional"] * FEE_SIDE + d["funding_usd"]
            open_rows.append(d)
        weeks = [dict(w) for w in c.execute("SELECT * FROM fd_rebalances ORDER BY ts")]
        by_week = {r["week_key"]: r["p"] for r in c.execute("SELECT week_key, SUM(pnl_usd) p FROM fd_positions WHERE status='closed' GROUP BY week_key")}
        realized = sum(by_week.values())
        unreal = sum(o.get("unrealized_usd", 0) for o in open_rows)
        # descomposicion del resultado: precio / funding / comisiones (cerradas + abiertas)
        cl = c.execute("SELECT COALESCE(SUM(funding_usd),0) f, COALESCE(SUM(entry_fee + COALESCE(exit_fee,0)),0) fee FROM fd_positions WHERE status='closed'").fetchone()
        price_cl = realized - cl["f"] + cl["fee"]
        price_open = sum(o.get("price_pnl", 0) for o in open_rows)
        fund_open = sum(o["funding_usd"] for o in open_rows)
        fee_open = sum(o["entry_fee"] + o["notional"] * FEE_SIDE for o in open_rows)
        # semanas cerradas: todas las posiciones de la semana estan cerradas
        closed_weeks = []
        for w in weeks:
            open_n = c.execute("SELECT COUNT(*) n FROM fd_positions WHERE week_key=? AND status='open'", (w["week_key"],)).fetchone()["n"]
            if w["week_key"] in by_week and open_n == 0:
                closed_weeks.append((w["week_key"], by_week[w["week_key"]]))
        rets = np.array([p / BANKROLL for _, p in closed_weeks])
        recent = [dict(r) for r in c.execute("SELECT * FROM fd_positions WHERE status='closed' AND exit_reason='stop' ORDER BY id DESC LIMIT 30")]
        n_stop = c.execute("SELECT COUNT(*) n FROM fd_positions WHERE exit_reason='stop'").fetchone()["n"]
        return {"config": {"window_days": WINDOW_DAYS, "quantile": QUANTILE, "leg": NOTIONAL_LEG, "bankroll": BANKROLL, "fee_side_pct": FEE_SIDE * 100,
                           "sl_pct": SL_PCT * 100, "universe_n": len(UNIVERSE)},
                "n_stop": n_stop, "recent_exits": recent,
                "equity": BANKROLL + realized + unreal, "realized": realized, "unrealized": unreal, "open": open_rows,
                "decomp": {"price": price_cl + price_open, "funding": cl["f"] + fund_open, "fees": -(cl["fee"] + fee_open)},
                "weeks": [{"week": k, "pnl": p, "ret_pct": p / BANKROLL * 100} for k, p in closed_weeks],
                "n_weeks": len(closed_weeks), "win_weeks": int((rets > 0).sum()) if len(rets) else 0,
                "avg_week_pct": float(rets.mean() * 100) if len(rets) else None,
                "last_rebalance": weeks[-1] if weeks else None}
