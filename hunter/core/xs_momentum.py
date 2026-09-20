"""
Momentum semanal entre monedas (cross-sectional) en PAPEL: cada semana compra (largo) las monedas que mas subieron en los ultimos
28 dias y vende (corto) las que mas cayeron, entre las ~45 mas liquidas de Binance. NUNCA opera de verdad.

Reglas (fijadas con el backtest 2019-2026, 70 monedas; ver memoria del proyecto): universo en cada fecha = monedas con >= 120 dias de
historia, volumen medio de 30 dias >= $10M y desvio diario >= 0.4% (saca estables), top TOPN por volumen; se ordena por retorno de LOOKBACK dias medido al cierre del LUNES;
largo quintil superior / corto quintil inferior, pesos iguales (cada pata suma NOTIONAL_LEG); se opera el miercoles 00:10 UTC (~ cierre del
martes: retraso de 1 dia, como en el backtest) y se mantiene 7 dias. Las posiciones que se repiten (misma moneda y lado) siguen sin costo.
Costo 0.07% por lado sobre el nocional operado. Funding de los cortos NO incluido.

CONTROL DE RIESGO (2026-09-20, medido en el backtest; el paquete se eligio entre varias combinaciones, asi que su mejora esta algo inflada):
 - Stop-loss 20% y take-profit 40% por posicion, revisados cada 5 min con las velas reales de 5m (minimo/maximo); stop primero si toca ambos;
   el stop sale con 0.3% de slippage adverso. Tras un stop/TP la posicion queda cerrada hasta el proximo reequilibrio.
 - Tamano segun volatilidad: exposicion = min(1, 40% / volatilidad anualizada de las ultimas 8 semanas de la propia cartera); con menos de 8 semanas
   cerradas se usa la mitad (0.5). El tamano de cada pata es NOTIONAL_LEG x exposicion.
 Backtest 2019-2026 (neto de costos): sin control +58% anual, Sharpe 0.73, caida max -72%, peor semana -63%; SL/TP +47%, Sharpe 0.71, -69%, -41%;
 SL/TP + volatilidad objetivo 40%: +38%, Sharpe 0.81, caida max -43%, peor semana -28%. Solo el stop-loss (sin TP) recortaba el retorno a la mitad y el
 take-profit corto (15%) tambien: la caida grande viene de semanas en que toda la cartera se da vuelta a la vez, no de posiciones sueltas.

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
SL_PCT = 0.20
TP_PCT = 0.40
STOP_SLIP = 0.003              # slippage adverso al ejecutar un stop
TARGET_VOL = 0.40              # volatilidad anual objetivo de la cartera
VOL_WINDOW = 8                 # semanas para estimarla
WARMUP_SCALE = 0.5             # exposicion mientras no hay VOL_WINDOW semanas cerradas
CHECK_SECONDS = 300            # revision de stops / take-profit
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
    carried INTEGER NOT NULL DEFAULT 0,                                     -- 1 si continua de la semana anterior (sin costo de entrada)
    exit_reason TEXT, checked_ms INTEGER
);
CREATE TABLE IF NOT EXISTS xs_marks (symbol TEXT PRIMARY KEY, price REAL, ts TEXT);
"""


def init_db():
    with conn_ctx() as c:
        c.executescript(SCHEMA)
        # migraciones para bases creadas antes del control de riesgo
        for table, col, typ in (("xs_positions", "exit_reason", "TEXT"), ("xs_positions", "checked_ms", "INTEGER"), ("xs_rebalances", "scale", "REAL")):
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except Exception:
                pass                                                          # la columna ya existe


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


def exit_decision(side: str, entry: float, candles):
    """candles: [(high, low), ...] en orden cronologico. Devuelve (motivo, precio_de_salida) o None. Stop primero si toca ambos en la misma vela."""
    if side == "long":
        sl, tp = entry * (1 - SL_PCT), entry * (1 + TP_PCT)
    else:
        sl, tp = entry * (1 + SL_PCT), entry * (1 - TP_PCT)
    for hi, lo in candles:
        stop_hit = (lo <= sl) if side == "long" else (hi >= sl)
        tp_hit = (hi >= tp) if side == "long" else (lo <= tp)
        if stop_hit:
            return "stop", (sl * (1 - STOP_SLIP) if side == "long" else sl * (1 + STOP_SLIP))
        if tp_hit:
            return "take_profit", tp
    return None


def exposure_factor(unit_returns) -> float:
    """Exposicion de la proxima semana segun la volatilidad de las ultimas VOL_WINDOW semanas de la cartera (retornos por unidad de nocional)."""
    if len(unit_returns) < VOL_WINDOW:
        return WARMUP_SCALE
    v = float(np.std(unit_returns[-VOL_WINDOW:], ddof=1)) * np.sqrt(52)
    return 1.0 if v <= 0 else min(1.0, TARGET_VOL / v)


def close_position(conn, r, price: float, reason: str):
    gross = (price / r["entry"] - 1.0) * (1 if r["side"] == "long" else -1) * r["notional"]
    exit_fee = r["notional"] * FEE_SIDE
    conn.execute("UPDATE xs_positions SET status='closed', exit_price=?, closed_at=?, exit_fee=?, pnl_usd=?, exit_reason=? WHERE id=?",
                 (price, now_iso(), exit_fee, gross - r["entry_fee"] - exit_fee, reason, r["id"]))


def rebalance(conn, week_key: str, longs, shorts, mom: dict, prices: dict, signal_date: str, universe_n: int, note: str = "", scale: float = 1.0):
    """Cierra lo que no se repite y abre los nuevos objetivos a los precios dados. Lo que se repite (misma moneda y lado) sigue sin costo."""
    now = now_iso()
    target = {s: "long" for s in longs}
    target.update({s: "short" for s in shorts})
    k_l, k_s = max(len(longs), 1), max(len(shorts), 1)
    leg = NOTIONAL_LEG * scale
    opened = closed = carried = 0
    for r in conn.execute("SELECT * FROM xs_positions WHERE status='open'").fetchall():
        px = prices.get(r["symbol"])
        if px is None:
            continue
        gross = (px / r["entry"] - 1.0) * (1 if r["side"] == "long" else -1) * r["notional"]
        keep = target.get(r["symbol"]) == r["side"]
        exit_fee = 0.0 if keep else r["notional"] * FEE_SIDE
        conn.execute("UPDATE xs_positions SET status='closed', exit_price=?, closed_at=?, exit_fee=?, pnl_usd=?, exit_reason=? WHERE id=?",
                     (px, now, exit_fee, gross - r["entry_fee"] - exit_fee, "continua" if keep else "reequilibrio", r["id"]))
        closed += 1
        if keep:
            n = leg / (k_l if r["side"] == "long" else k_s)
            conn.execute("""INSERT INTO xs_positions (week_key, symbol, side, notional, mom, entry, opened_at, entry_fee, carried)
                            VALUES (?,?,?,?,?,?,?,0,1)""", (week_key, r["symbol"], r["side"], n, mom.get(r["symbol"]), px, now))
            target.pop(r["symbol"]); carried += 1
    for sym, side in target.items():
        px = prices.get(sym)
        if px is None:
            continue
        n = leg / (k_l if side == "long" else k_s)
        conn.execute("""INSERT INTO xs_positions (week_key, symbol, side, notional, mom, entry, opened_at, entry_fee, carried)
                        VALUES (?,?,?,?,?,?,?,?,0)""", (week_key, sym, side, n, mom.get(sym), px, now, n * FEE_SIDE))
        opened += 1
    conn.execute("INSERT OR REPLACE INTO xs_rebalances (week_key, ts, signal_date, universe_n, n_long, n_short, note, scale) VALUES (?,?,?,?,?,?,?,?)",
                 (week_key, now, signal_date, universe_n, len(longs), len(shorts), note, scale))
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
        scale = exposure_factor(weekly_unit_returns(c))
        res = rebalance(c, week_key_of(now_dt), longs, shorts, mom, prices, str(d.date()), len(elig), note, scale)
    logger.info(f"Momentum (PAPEL) {week_key_of(now_dt)}: senal {d.date()} | exposicion {scale:.2f} | largos {longs} | cortos {shorts} | {res}")
    return res


def weekly_unit_returns(conn):
    """Retorno semanal de la cartera por unidad de nocional por pata (normalizado por la exposicion de cada semana), en orden cronologico."""
    out = []
    for w in conn.execute("SELECT week_key, COALESCE(scale, 1.0) sc FROM xs_rebalances ORDER BY ts"):
        r = conn.execute("SELECT COUNT(*) n, SUM(pnl_usd) p FROM xs_positions WHERE week_key=?", (w["week_key"],)).fetchone()
        open_n = conn.execute("SELECT COUNT(*) n FROM xs_positions WHERE week_key=? AND status='open'", (w["week_key"],)).fetchone()["n"]
        if r["n"] and open_n == 0 and r["p"] is not None:
            out.append(r["p"] / (NOTIONAL_LEG * w["sc"]))
    return out


async def check_exits(client):
    """Revisa stop-loss / take-profit de las posiciones abiertas con las velas de 5m desde la ultima revision."""
    with conn_ctx() as c:
        opens = [dict(r) for r in c.execute("SELECT * FROM xs_positions WHERE status='open'")]
    for r in opens:
        opened_ms = int(datetime.fromisoformat(r["opened_at"]).timestamp() * 1000)
        start = int(r["checked_ms"] or opened_ms)
        try:
            k = await client.get("https://api.binance.com/api/v3/klines", params={"symbol": f"{r['symbol']}USDT", "interval": "5m", "startTime": start, "limit": 1000}, timeout=20)
            k.raise_for_status()
        except Exception as e:
            logger.warning(f"Momentum: sin velas de {r['symbol']}: {e}")
            continue
        candles = [(float(x[2]), float(x[3])) for x in k.json()]
        last_open = int(k.json()[-1][0]) if candles else start
        with conn_ctx() as c:
            hit = exit_decision(r["side"], r["entry"], candles)
            if hit:
                close_position(c, r, hit[1], hit[0])
                logger.info(f"Momentum (PAPEL): {r['symbol']} {r['side']} cerrada por {hit[0]} a {hit[1]:.6g} (entrada {r['entry']:.6g})")
            else:
                c.execute("UPDATE xs_positions SET checked_ms=? WHERE id=?", (last_open, r["id"]))


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
    logger.info(f"Momentum semanal (PAPEL) arrancando: reequilibra los miercoles 00:10 UTC; SL {SL_PCT:.0%} / TP {TP_PCT:.0%} cada {CHECK_SECONDS // 60} min")
    async with httpx.AsyncClient() as client:
        last_rebal_check = 0.0
        while True:
            try:
                await check_exits(client)
                if time.time() - last_rebal_check < POLL_SECONDS:
                    await asyncio.sleep(CHECK_SECONDS)
                    continue
                last_rebal_check = time.time()
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
            await asyncio.sleep(CHECK_SECONDS)


# ------------------------------------------------------------------ lectura para el dashboard
def get_snapshot() -> dict:
    init_db()
    with conn_ctx() as c:
        marks = {r["symbol"]: r["price"] for r in c.execute("SELECT symbol, price FROM xs_marks")}
        open_rows = []
        for r in c.execute("SELECT * FROM xs_positions WHERE status='open' ORDER BY side DESC, symbol"):
            d = dict(r); m = marks.get(d["symbol"])
            d["mark"] = m
            d["sl"] = d["entry"] * (1 - SL_PCT) if d["side"] == "long" else d["entry"] * (1 + SL_PCT)
            d["tp"] = d["entry"] * (1 + TP_PCT) if d["side"] == "long" else d["entry"] * (1 - TP_PCT)
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
        last_scale = c.execute("SELECT COALESCE(scale,1.0) sc FROM xs_rebalances ORDER BY ts DESC LIMIT 1").fetchone()
        recent = [dict(r) for r in c.execute("SELECT * FROM xs_positions WHERE status='closed' AND exit_reason IN ('stop','take_profit') ORDER BY id DESC LIMIT 30")]
        n_stop = c.execute("SELECT COUNT(*) n FROM xs_positions WHERE exit_reason='stop'").fetchone()["n"]
        n_tp = c.execute("SELECT COUNT(*) n FROM xs_positions WHERE exit_reason='take_profit'").fetchone()["n"]
        return {"config": {"lookback": LOOKBACK, "quantile": QUANTILE, "topn": TOPN, "leg": NOTIONAL_LEG, "bankroll": BANKROLL, "fee_side_pct": FEE_SIDE * 100,
                           "sl_pct": SL_PCT * 100, "tp_pct": TP_PCT * 100, "target_vol": TARGET_VOL * 100, "vol_window": VOL_WINDOW},
                "scale": last_scale["sc"] if last_scale else None, "recent_exits": recent, "n_stop": n_stop, "n_tp": n_tp,
                "equity": BANKROLL + realized + unreal, "realized": realized, "unrealized": unreal, "open": open_rows,
                "weeks": [{"week": k, "pnl": p, "ret_pct": p / BANKROLL * 100} for k, p in closed_weeks],
                "n_weeks": len(closed_weeks), "win_weeks": int((rets > 0).sum()) if len(rets) else 0,
                "avg_week_pct": float(rets.mean() * 100) if len(rets) else None,
                "last_rebalance": weeks[-1] if weeks else None}
