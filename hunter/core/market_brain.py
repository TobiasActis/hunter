"""
"Cerebro" de mercado para criptos establecidas (BTC, ETH, SOL) en MODO SOMBRA: aprende de las velas de 1 hora, predice
cada hora la probabilidad de que el precio suba en las proximas 4 y 24 horas, y mide EN VIVO, con datos que nunca vio, si
acierta y si alcanza para pagar las comisiones. NO opera ni decide nada.

Por que sombra: en la investigacion (walk-forward 2020-2026, entrenando solo con el pasado y probando el mes siguiente) el
modelo mostro estructura real pero chica: AUC 0.52-0.55 (0.5 = azar), consistente entre mitades, marcos y activos; pero la
ganancia bruta por operacion (0 a +10 bps) no cubre el costo (taker 14 bps ida y vuelta, maker 4 bps). En una simulacion
realista (una posicion a la vez, entrada en la apertura siguiente) BTC y ETH dieron negativo todos los anos; solo una celda
de SOL dio positivo con intervalo [-19,+42] bps: ruido. Este modulo sigue midiendo con datos nuevos.

Variables (solo pasado): retornos a 1..64 velas, volatilidad, ATR, RSI, distancia a EMA20/50, Bollinger, posicion en el rango
de 48 velas, volumen relativo, forma de la vela, hora/dia y retornos de los OTROS dos activos. Modelo: gradient boosting de
profundidad 3, reentrenado cada RETRAIN_DAYS dias con todo el historial disponible (etiqueta = signo del retorno a H velas,
con embargo de H velas). Datos: Binance spot, velas publicas de 1 h, cacheadas en data/brain_1h_*.csv.
"""
import asyncio
import logging
import math
import os
import sys
import time

import httpx
import numpy as np
import pandas as pd

from core.scalper import SCALP_DB, conn_ctx, now_iso

logger = logging.getLogger("hunter.market_brain")

SYMBOLS = ("BTC", "ETH", "SOL")
HORIZONS = (4, 24)
RETRAIN_DAYS = 7
HISTORY_START_MS = 1_598_918_400_000        # 2020-09-01 UTC (arranque de SOL en Binance)
BAR_MS = 3_600_000
COST_TAKER, COST_MAKER = 0.0014, 0.0004
SIGNAL_THRESHOLD = 0.05                      # |p - 0.5| minimo para contar una "senal"
DATA_DIR = os.path.dirname(os.path.abspath(SCALP_DB))
POLL_SECONDS = 60

# Referencia de la investigacion (AUC walk-forward fuera de muestra, 1 h): para comparar con lo que se mide en vivo
RESEARCH_AUC = {("BTC", 4): 0.5465, ("BTC", 24): 0.5188, ("ETH", 4): 0.5396, ("ETH", 24): 0.5193, ("SOL", 4): 0.5194, ("SOL", 24): 0.5223}

SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, horizon INTEGER NOT NULL, candle_ms INTEGER NOT NULL,   -- apertura de la vela sobre la que se predijo
    p_up REAL NOT NULL, made_at TEXT NOT NULL, model_trained_at TEXT,
    realized_ret REAL, scored_at TEXT,
    UNIQUE(symbol, horizon, candle_ms)
);
CREATE TABLE IF NOT EXISTS brain_state (key TEXT PRIMARY KEY, value TEXT);
"""


def init_db():
    with conn_ctx() as c:
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ datos
def cache_path(sym):
    return os.path.join(DATA_DIR, f"brain_1h_{sym}.csv")


def load_cache(sym) -> pd.DataFrame:
    p = cache_path(sym)
    if not os.path.exists(p):
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"]).set_index("t")
    df = pd.read_csv(p, header=None, names=["t", "o", "h", "l", "c", "v"]).drop_duplicates("t").sort_values("t")
    return df.set_index("t")


async def update_cache(client, sym) -> int:
    """Baja de Binance las velas de 1 h CERRADAS que faltan y las agrega al cache. Devuelve cuantas agrego."""
    df = load_cache(sym)
    start = int(df.index[-1]) + BAR_MS if len(df) else HISTORY_START_MS
    now_ms = int(time.time() * 1000)
    added = 0
    rows = []
    while start < now_ms:
        r = await client.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": f"{sym}USDT", "interval": "1h", "limit": 1000, "startTime": start}, timeout=30)
        r.raise_for_status()
        k = r.json()
        if not k:
            break
        for x in k:
            if int(x[6]) < now_ms:                       # solo velas cerradas
                rows.append((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])))
        start = int(k[-1][0]) + BAR_MS
        if len(k) < 1000:
            break
        await asyncio.sleep(0.25)
    if rows:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(cache_path(sym), "a") as f:
            for t, o, h, l, c, v in rows:
                f.write(f"{t},{o},{h},{l},{c},{v}\n")
        added = len(rows)
    return added


def aligned(raw: dict) -> dict:
    idx = None
    for s in SYMBOLS:
        idx = raw[s].index if idx is None else idx.intersection(raw[s].index)
    return {s: raw[s].loc[idx] for s in SYMBOLS}


# ------------------------------------------------------------------ variables (solo pasado)
def build_features(raw: dict, sym: str) -> pd.DataFrame:
    d = raw[sym]
    c, h, l, o, v = d.c, d.h, d.l, d.o, d.v
    f = pd.DataFrame(index=d.index)
    lr = np.log(c).diff()
    for k in (1, 2, 4, 8, 16, 32, 64):
        f[f"ret{k}"] = np.log(c).diff(k)
    f["vol16"] = lr.rolling(16).std(); f["vol64"] = lr.rolling(64).std(); f["volratio"] = f.vol16 / f.vol64
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean(); f["atrp"] = atr / c
    dlt = c.diff(); up = dlt.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-dlt.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    f["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    e20 = c.ewm(span=20, adjust=False).mean(); e50 = c.ewm(span=50, adjust=False).mean()
    f["d_e20"] = (c - e20) / atr; f["d_e50"] = (c - e50) / atr
    ma = c.rolling(20).mean(); sd = c.rolling(20).std(); f["bb"] = (c - ma) / (2 * sd)
    hi, lo = h.rolling(48).max(), l.rolling(48).min(); f["rngpos"] = (c - lo) / (hi - lo)
    f["volz"] = np.log((v + 1) / (v.rolling(96).mean() + 1))
    rng = (h - l).replace(0, np.nan)
    f["body"] = (c - o) / rng; f["upw"] = (h - np.maximum(c, o)) / rng; f["loww"] = (np.minimum(c, o) - l) / rng
    ts = pd.to_datetime(d.index, unit="ms", utc=True)
    f["hs"] = np.sin(2 * np.pi * ts.hour / 24); f["hc"] = np.cos(2 * np.pi * ts.hour / 24)
    f["ds"] = np.sin(2 * np.pi * ts.dayofweek / 7); f["dc"] = np.cos(2 * np.pi * ts.dayofweek / 7)
    for other in SYMBOLS:
        if other != sym:
            oc = np.log(raw[other].c)
            f[f"x_{other}_r4"] = oc.diff(4); f[f"x_{other}_r16"] = oc.diff(16)
    return f


def _new_model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=200,
                                          l2_regularization=1.0, random_state=0)


def train_models(raw: dict, until_ms: int | None = None) -> dict:
    """Entrena un modelo por (activo, horizonte) con TODO el historial (o hasta `until_ms`), con embargo de H velas."""
    raw = aligned(raw)
    models = {}
    for sym in SYMBOLS:
        F = build_features(raw, sym)
        c = raw[sym].c.values
        for H in HORIZONS:
            fwd = np.full(len(c), np.nan)
            fwd[:-H] = np.log(c[H:] / c[:-H])
            X = F.values
            ok = ~np.isnan(X).any(axis=1) & ~np.isnan(fwd)
            if until_ms is not None:
                ok &= raw[sym].index.values < until_ms
            ok[max(0, len(ok) - H):] = False
            if ok.sum() < 3000:
                continue
            m = _new_model()
            m.fit(X[ok], (fwd[ok] > 0).astype(int))
            models[(sym, H)] = m
    return models


def predict_last(models: dict, raw: dict, n_last: int = 1):
    """Probabilidad de que suba para las ultimas `n_last` velas cerradas. Devuelve [(sym, H, candle_ms, p)]."""
    raw = aligned(raw)
    out = []
    for sym in SYMBOLS:
        F = build_features(raw, sym)
        X = F.values
        for H in HORIZONS:
            m = models.get((sym, H))
            if m is None:
                continue
            for i in range(len(X) - n_last, len(X)):
                if i >= 0 and not np.isnan(X[i]).any():
                    out.append((sym, H, int(F.index[i]), float(m.predict_proba(X[i:i + 1])[0, 1])))
    return out


# ------------------------------------------------------------------ registro y medicion en vivo
def log_predictions(preds, trained_at):
    with conn_ctx() as c:
        for sym, H, ms, p in preds:
            c.execute("INSERT OR IGNORE INTO brain_predictions (symbol, horizon, candle_ms, p_up, made_at, model_trained_at) VALUES (?,?,?,?,?,?)",
                      (sym, H, ms, p, now_iso(), trained_at))


def score_predictions(raw: dict) -> int:
    """Completa el retorno REAL de las predicciones cuyo horizonte ya paso (cierre de la vela + H contra cierre de la vela)."""
    raw = aligned(raw)
    n = 0
    with conn_ctx() as c:
        for r in c.execute("SELECT id, symbol, horizon, candle_ms FROM brain_predictions WHERE realized_ret IS NULL").fetchall():
            df = raw[r["symbol"]]
            t1 = r["candle_ms"] + r["horizon"] * BAR_MS
            if r["candle_ms"] in df.index and t1 in df.index:
                ret = math.log(df.c[t1] / df.c[r["candle_ms"]])
                c.execute("UPDATE brain_predictions SET realized_ret=?, scored_at=? WHERE id=?", (ret, now_iso(), r["id"]))
                n += 1
    return n


def get_snapshot() -> dict:
    """Estadisticas en vivo por (activo, horizonte), sobre predicciones ya evaluadas con datos posteriores al entrenamiento."""
    init_db()
    from sklearn.metrics import roc_auc_score
    rows = []
    with conn_ctx() as c:
        state = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM brain_state")}
        for sym in SYMBOLS:
            for H in HORIZONS:
                last = c.execute("SELECT p_up, candle_ms, made_at FROM brain_predictions WHERE symbol=? AND horizon=? ORDER BY candle_ms DESC LIMIT 1", (sym, H)).fetchone()
                sc = c.execute("SELECT p_up, realized_ret FROM brain_predictions WHERE symbol=? AND horizon=? AND realized_ret IS NOT NULL ORDER BY candle_ms", (sym, H)).fetchall()
                d = {"symbol": sym, "horizon": H, "research_auc": RESEARCH_AUC.get((sym, H)), "n_scored": len(sc),
                     "last_p": last["p_up"] if last else None, "last_candle_ms": last["candle_ms"] if last else None}
                if len(sc) >= 30:
                    p = np.array([x["p_up"] for x in sc]); r = np.array([x["realized_ret"] for x in sc])
                    y = (r > 0).astype(int)
                    d["auc"] = float(roc_auc_score(y, p)) if len(np.unique(y)) > 1 else None
                    d["dir_acc"] = float(((p > 0.5) == (r > 0)).mean())
                    sel = np.abs(p - 0.5) >= SIGNAL_THRESHOLD
                    d["n_signals"] = int(sel.sum())
                    if sel.sum() >= 10:
                        g = np.where(p[sel] > 0.5, 1, -1) * r[sel]
                        d["gross_bps"] = float(g.mean() * 1e4)
                        d["net_taker_bps"] = float((g.mean() - COST_TAKER) * 1e4)
                        d["net_maker_bps"] = float((g.mean() - COST_MAKER) * 1e4)
                rows.append(d)
    return {"rows": rows, "trained_at": state.get("trained_at"), "n_train_rows": state.get("n_train_rows"),
            "retrain_days": RETRAIN_DAYS, "threshold": SIGNAL_THRESHOLD, "cost_taker_bps": COST_TAKER * 1e4, "cost_maker_bps": COST_MAKER * 1e4}


# ------------------------------------------------------------------ bucle principal
def _set_state(k, v):
    with conn_ctx() as c:
        c.execute("INSERT OR REPLACE INTO brain_state VALUES (?,?)", (k, str(v)))


def _get_state(k):
    with conn_ctx() as c:
        r = c.execute("SELECT value FROM brain_state WHERE key=?", (k,)).fetchone()
        return r["value"] if r else None


MODEL_PATH = os.path.join(DATA_DIR, "brain_models.joblib")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _train_in_subprocess():
    """El entrenamiento corre en un proceso hijo (1 hilo, baja prioridad): en una maquina de 1 GB sin swap un pico de memoria
    dentro del proceso principal podria tumbar todo el sistema; el hijo libera la memoria al terminar."""
    env = dict(os.environ, OMP_NUM_THREADS="1")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "core.market_brain", "train", cwd=BASE_DIR, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("entrenamiento fallo: " + err.decode(errors="replace")[-400:])


def _load_models():
    import joblib
    d = joblib.load(MODEL_PATH)
    return d["models"], d["trained_at"], d["n_rows"]


async def run():
    init_db()
    logger.info("Cerebro de mercado (SOMBRA) arrancando: predice cada hora, no opera")
    models, trained_ts, last_pred_ms = {}, None, 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                for s in SYMBOLS:
                    await update_cache(client, s)
                raw = {s: load_cache(s) for s in SYMBOLS}
                enough = all(len(raw[s]) > 5000 for s in SYMBOLS)
                # modelos: reutiliza los del disco si son recientes; si no, reentrena en un proceso hijo
                if enough and (not models or (time.time() - trained_ts) > RETRAIN_DAYS * 86400):
                    fresh = os.path.exists(MODEL_PATH) and (time.time() - os.path.getmtime(MODEL_PATH)) < RETRAIN_DAYS * 86400
                    if not fresh:
                        t0 = time.time()
                        await _train_in_subprocess()
                        logger.info(f"Cerebro: modelos reentrenados en {time.time() - t0:.0f}s")
                    models, trained_at_iso, n_rows = await asyncio.to_thread(_load_models)
                    trained_ts = os.path.getmtime(MODEL_PATH)
                    _set_state("trained_at", trained_at_iso); _set_state("n_train_rows", n_rows)
                if models:
                    al = aligned(raw)
                    newest = int(al["BTC"].index[-1])
                    if newest > last_pred_ms:
                        preds = await asyncio.to_thread(predict_last, models, raw, 1)
                        log_predictions(preds, _get_state("trained_at"))
                        last_pred_ms = newest
                    n = score_predictions(raw)
                    if n:
                        logger.info(f"Cerebro: {n} predicciones evaluadas con el resultado real")
            except Exception:
                logger.exception("Cerebro: error en el ciclo, reintenta")
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "train":
    # Proceso hijo de entrenamiento (ver _train_in_subprocess)
    import joblib
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    _raw = {s: load_cache(s) for s in SYMBOLS}
    _models = train_models(_raw)
    joblib.dump({"models": _models, "trained_at": now_iso(), "n_rows": len(aligned(_raw)["BTC"])}, MODEL_PATH)
    print("ok", len(_models))
