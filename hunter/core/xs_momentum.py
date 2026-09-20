"""
Momentum semanal entre monedas (cross-sectional) en PAPEL: cada semana compra (largo) las monedas que mas subieron en los ultimos
28 dias y vende (corto) las que mas cayeron, entre las ~45 mas liquidas de Binance. NUNCA opera de verdad.

Reglas (fijadas con el backtest 2019-2026, 70 monedas; ver memoria del proyecto): universo en cada fecha = monedas con >= 120 dias de
historia, volumen medio de 30 dias >= $10M y desvio diario >= 0.4% (saca estables), top TOPN por volumen; se ordena por retorno de LOOKBACK dias medido al cierre del LUNES;
largo quintil superior / corto quintil inferior, pesos iguales (cada pata suma NOTIONAL_LEG); se opera el miercoles 00:10 UTC (~ cierre del
martes: retraso de 1 dia, como en el backtest) y se mantiene 7 dias. Las posiciones que se repiten (misma moneda y lado) siguen sin costo.
Costo 0.07% por lado sobre el nocional operado. Funding de los cortos NO incluido.

Resultado del backtest: L=28 quintil 20%: +50-60% anual neto de costos, Sharpe 0.6-0.9, t~1.7, caida maxima -64% a -90%; de 16 variantes 12 dieron
positivo pero de +12% a +66%; 2024-2026 mas debil (2da mitad negativa). Sesgo conocido del backtest: universo = monedas liquidas HOY (sobrevivientes),
funding excluido. Esta operacion en papel mide sin ese sesgo. Semana de arranque: la primera posicion se abre al desplegar (senal del ultimo lunes).
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd

from core.scalper import conn_ctx, now_iso

logger = logging.getLogger("hunter.xs_momentum")

LOOKBACK = 28
QUANTILE = 0.2
TOPN = 45
UNIVERSE_FETCH = 60            # monedas a bajar (se filtra despues por elegibilidad y se recorta a TOPN)
MIN_HIST_DAYS = 120
MIN_VOL30 = 10e6
MIN_DAILY_STD = 0.004          # excluye monedas estables sin lista: desvio diario de 30 dias < 0.4% (RLUSD, U, etc.)
NOTIONAL_LEG = 500.0
BANKROLL = 1000.0
FEE_SIDE = 0.0007
REBAL_WEEKDAY = 2              # miercoles
REBAL_MINUTE_OF_DAY = 10       # 00:10 UTC
POLL_SECONDS = 1800
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "EUR", "EURI", "AEUR", "BUSD", "XUSD", "USD1", "PAXG", "WBTC", "WBETH", "BFUSD"}
BAD_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")

SCHEMA = """
CREATE TABLE IF NOT EXISTS xs_rebalances (
    week_key TEXT PRIMARY KEY, ts TEXT NOT NULL, signal_date TEXT, universe_n INTEGER, n_long INTEGER, n_short INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS xs_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,        -- 'long' | 'short'
    notional REAL NOT NULL, mom REAL, entry REAL NOT NULL, opened_at TEXT NOT NULL, entry_fee REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open', exit_price REAL, closed_at TEXT, exit_fee REAL, pnl_usd REAL,
    carried INTEGER NOT NULL DEFAULT 0                                      -- 1 si continua de la semana anterior (sin costo de entrada)
);
CREATE TABLE IF NOT EXISTS xs_marks (symbol TEXT PRIMARY KEY, price REAL, ts TEXT);
"""


def init_db():
    with conn_ctx() as c:
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ logica pura (sin red)
def select_portfolio(closes: pd.DataFrame, qvols: pd.DataFrame, signal_date: pd.Timestamp):
    """closes/qvols: filas = dias (indice datetime), columnas = monedas. Devuelve (largos, cortos, universo, mom)
    con la informacion HASTA signal_date inclusive. Mismo criterio que el backtest."""
    c = closes.loc[:signal_date]
    q = qvols.loc[:signal_date]
    if len(c) <= LOOKBACK or signal_date not in c.index:
        return [], [], [], {}
    hist = c.notna().sum()
    vol30 = q.tail(30).mean()
    dstd = c.pct_change(fill_method=None).tail(30).std()
    elig = [s for s in c.columns if hist[s] >= MIN_HIST_DAYS and vol30[s] >= MIN_VOL30 and dstd[s] >= MIN_DAILY_STD and not np.isnan(c[s].iloc[-1])
            and not np.isnan(c[s].iloc[-1 - LOOKBACK])]
    elig = sorted(elig, key=lambda s: -vol30[s])[:TOPN]
    if len(elig) < 12:
        return [], [], elig, {}
    mom = pd.Series({s: c[s].iloc[-1] / c[s].iloc[-1 - LOOKBACK] - 1 for s in elig}).sort_values()
    k = max(3, int(len(mom) * QUANTILE))
    return list(mom.index[-k:]), list(mom.index[:k]), elig, mom.to_dict()


def rebalance(conn, week_key: str, longs, shorts, mom: dict, prices: dict, signal_date: str, universe_n: int, note: str = ""):
    """Cierra lo que no se repite y abre los nuevos objetivos a los precios dados. Lo que se repite (misma moneda y lado) sigue sin costo."""
    now = now_iso()
    target = {s: "long" for s in longs}
    target.update({s: "short" for s in shorts})
    k_l, k_s = max(len(longs), 1), max(len(shorts), 1)
    opened = closed = carried = 0
    for r in conn.execute("SELECT * FROM xs_positions WHERE status='open'").fetchall():
        px = prices.get(r["symbol"])
        if px is None:
            continue
        gross = (px / r["entry"] - 1.0) * (1 if r["side"] == "long" else -1) * r["notional"]
        keep = target.get(r["symbol"]) == r["side"]
        exit_fee = 0.0 if keep else r["notional"] * FEE_SIDE
        conn.execute("UPDATE xs_positions SET status='closed', exit_price=?, closed_at=?, exit_fee=?, pnl_usd=? WHERE id=?",
                     (px, now, exit_fee, gross - r["entry_fee"] - exit_fee, r["id"]))
        closed += 1
        if keep:
            n = NOTIONAL_LEG / (k_l if r["side"] == "long" else k_s)
            conn.execute("""INSERT INTO xs_positions (week_key, symbol, side, notional, mom, entry, opened_at, entry_fee, carried)
                            VALUES (?,?,?,?,?,?,?,0,1)""", (week_key, r["symbol"], r["side"], n, mom.get(r["symbol"]), px, now))
            target.pop(r["symbol"]); carried += 1
    for sym, side in target.items():
        px = prices.get(sym)
        if px is None:
            continue
        n = NOTIONAL_LEG / (k_l if side == "long" else k_s)
        conn.execute("""INSERT INTO xs_positions (week_key, symbol, side, notional, mom, entry, opened_at, entry_fee, carried)
                        VALUES (?,?,?,?,?,?,?,?,0)""", (week_key, sym, side, n, mom.get(sym), px, now, n * FEE_SIDE))
        opened += 1
    conn.execute("INSERT OR REPLACE INTO xs_rebalances VALUES (?,?,?,?,?,?,?)", (week_key, now, signal_date, universe_n, len(longs), len(shorts), note))
    return {"opened": opened, "closed": closed, "carried": carried}


# ------------------------------------------------------------------ red
def _ok_symbol(s: str) -> bool:
    return s.isascii() and s.isalnum() and s.endswith("USDT") and s[:-4] not in STABLE and not s[:-4].endswith(BAD_SUFFIX)


async def fetch_universe(client):
    r = await client.get("https://api.binance.com/api/v3/ticker/24hr", timeout=30)
    r.raise_for_status()
    syms = sorted([t for t in r.json() if _ok_symbol(t["symbol"])], key=lambda t: -float(t["quoteVolume"]))[:UNIVERSE_FETCH]
    closes, qvols = {}, {}
    now_ms = int(time.time() * 1000)
    for t in syms:
        k = await client.get("https://api.binance.com/api/v3/klines", params={"symbol": t["symbol"], "interval": "1d", "limit": 200}, timeout=30)
        k.raise_for_status()
        rows = [x for x in k.json() if int(x[6]) < now_ms]                    # solo velas diarias cerradas
        idx = pd.to_datetime([int(x[0]) for x in rows], unit="ms")
        closes[t["symbol"][:-4]] = pd.Series([float(x[4]) for x in rows], index=idx)
        qvols[t["symbol"][:-4]] = pd.Series([float(x[7]) for x in rows], index=idx)
        await asyncio.sleep(0.12)
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(qvols).sort_index()


async def fetch_prices(client, symbols):
    r = await client.get("https://api.binance.com/api/v3/ticker/price", timeout=30)
    r.raise_for_status()
    want = {f"{s}USDT": s for s in symbols}
    return {want[t["symbol"]]: float(t["price"]) for t in r.json() if t["symbol"] in want}


def week_key_of(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


async def do_rebalance(client, now_dt: datetime, note: str = ""):
    closes, qvols = await fetch_universe(client)
    # ultimo LUNES cuya vela diaria ya cerro
    d = pd.Timestamp(now_dt.date())
    while d.weekday() != 0 or d not in closes.index:
        d -= pd.Timedelta(days=1)
        if (pd.Timestamp(now_dt.date()) - d).days > 10:
            raise RuntimeError("no se encontro un lunes con vela cerrada")
    longs, shorts, elig, mom = select_portfolio(closes, qvols, d)
    if not longs:
        logger.warning("Momentum: universo insuficiente, no se rebalancea")
        return None
    with conn_ctx() as c:
        open_syms = [r["symbol"] for r in c.execute("SELECT symbol FROM xs_positions WHERE status='open'")]
    prices = await fetch_prices(client, set(longs) | set(shorts) | set(open_syms))
    with conn_ctx() as c:
        res = rebalance(c, week_key_of(now_dt), longs, shorts, mom, prices, str(d.date()), len(elig), note)
    logger.info(f"Momentum (PAPEL) {week_key_of(now_dt)}: senal {d.date()} | largos {longs} | cortos {shorts} | {res}")
    return res


async def refresh_marks(client):
    with conn_ctx() as c:
        syms = {r["symbol"] for r in c.execute("SELECT symbol FROM xs_positions WHERE status='open'")}
    if not syms:
        return
    prices = await fetch_prices(client, syms)
    with conn_ctx() as c:
        for s, p in prices.items():
            c.execute("INSERT OR REPLACE INTO xs_marks VALUES (?,?,?)", (s, p, now_iso()))


async def run():
    init_db()
    logger.info("Momentum semanal (PAPEL) arrancando: reequilibra los miercoles 00:10 UTC")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                now = datetime.now(timezone.utc)
                with conn_ctx() as c:
                    n_rebal = c.execute("SELECT COUNT(*) n FROM xs_rebalances").fetchone()["n"]
                    done = c.execute("SELECT 1 FROM xs_rebalances WHERE week_key=?", (week_key_of(now),)).fetchone()
                due_wed = now.weekday() == REBAL_WEEKDAY and (now.hour * 60 + now.minute) >= REBAL_MINUTE_OF_DAY and not done
                if n_rebal == 0:
                    await do_rebalance(client, now, "arranque")          # primera posicion al desplegar
                elif due_wed:
                    await do_rebalance(client, now)
                await refresh_marks(client)
            except Exception:
                logger.exception("Momentum: error en el ciclo, reintenta")
            await asyncio.sleep(POLL_SECONDS)


# ------------------------------------------------------------------ lectura para el dashboard
def get_snapshot() -> dict:
    init_db()
    with conn_ctx() as c:
        marks = {r["symbol"]: r["price"] for r in c.execute("SELECT symbol, price FROM xs_marks")}
        open_rows = []
        for r in c.execute("SELECT * FROM xs_positions WHERE status='open' ORDER BY side DESC, symbol"):
            d = dict(r); m = marks.get(d["symbol"])
            d["mark"] = m
            if m:
                g = (m / d["entry"] - 1.0) * (1 if d["side"] == "long" else -1) * d["notional"]
                d["unrealized_usd"] = g - d["entry_fee"] - d["notional"] * FEE_SIDE
            open_rows.append(d)
        weeks = []
        for w in c.execute("SELECT * FROM xs_rebalances ORDER BY ts"):
            pos = c.execute("SELECT COALESCE(SUM(pnl_usd),0) p, COUNT(*) n, SUM(carried) k FROM xs_positions WHERE week_key=? AND status='closed'", (w["week_key"],)).fetchone()
            weeks.append(dict(w) | {"closed_n": pos["n"]})
        # PnL por semana = suma de las posiciones abiertas EN esa semana y cerradas en el siguiente reequilibrio
        by_week = {r["week_key"]: r["p"] for r in c.execute("SELECT week_key, SUM(pnl_usd) p FROM xs_positions WHERE status='closed' GROUP BY week_key")}
        realized = sum(by_week.values())
        unreal = sum(o.get("unrealized_usd", 0) for o in open_rows)
        closed_weeks = [(w["week_key"], by_week[w["week_key"]]) for w in weeks if w["week_key"] in by_week]
        rets = np.array([p / BANKROLL for _, p in closed_weeks])
        return {"config": {"lookback": LOOKBACK, "quantile": QUANTILE, "topn": TOPN, "leg": NOTIONAL_LEG, "bankroll": BANKROLL, "fee_side_pct": FEE_SIDE * 100},
                "equity": BANKROLL + realized + unreal, "realized": realized, "unrealized": unreal, "open": open_rows,
                "weeks": [{"week": k, "pnl": p, "ret_pct": p / BANKROLL * 100} for k, p in closed_weeks],
                "n_weeks": len(closed_weeks), "win_weeks": int((rets > 0).sum()) if len(rets) else 0,
                "avg_week_pct": float(rets.mean() * 100) if len(rets) else None,
                "last_rebalance": weeks[-1] if weeks else None}
