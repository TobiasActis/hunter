"""
Filtro de ENTRADA aprendido de los datos propios (2026-09-18): decide si
una alerta de manada se opera (paper) o se descarta, para mandar menos
señales pero con mayor win-rate.

Qué se encontró estudiando 3,400+ posiciones reales limpias (sin
anomalías de precio), validado FUERA DE MUESTRA con 3 tramos temporales
(entrenar con lo anterior, probar en lo siguiente -- nunca en lo mismo
que se aprendió):
  - AUC 0.65 estable en los 3 tramos (un modelo más profundo llegaba a
    0.92 en entrenamiento y 0.60-0.66 afuera: memorizaba, se descartó).
  - El 20% mejor por score tuvo win-rate 23-29% contra 15-19% general.
  - Ganan: tokens muy jóvenes, compras chicas, manada que se forma
    rápido, sin ventas previas, wallets con buen historial, precio bajo
    (temprano en la curva). Pierden: plata grande (ballenas), ventas ya
    en curso, tokens avanzados en la curva, wallets de mal historial.
  - OJO, límite honesto: con posiciones de $10 el win-rate de equilibrio
    es ~36% (ganadora +$8 vs perdedora -$4.5, por el piso de comisión
    de $0.95). El filtro mejora la pérdida por trade pero NO garantiza
    ganancia -- por eso todo sigue en paper trading.

Diseño anti-autoengaño:
  - Umbral fijado con predicciones fuera de muestra (walk-forward), no
    con las del propio entrenamiento.
  - EXPLORATION_RATE: una fracción de las alertas descartadas se opera
    igual ('explore'), así el modelo sigue viendo toda la distribución
    al reentrenar y se puede medir en vivo si el filtro sirve.
  - Solo Robinhood Chain (ahí están los datos); otras cadenas pasan sin
    filtrar. Sin modelo entrenado, todo pasa (comportamiento anterior).
  - El modelo se guarda como dict de sklearn + metadatos (no una clase
    propia), para no repetir el bug de pickle con __main__ de brain.py.
"""
import logging
import math
import os
import random
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from core.db import get_conn

logger = logging.getLogger("hunter.entry_filter")

MODEL_PATH = "data/entry_filter.joblib"
TRAIN_SINCE = "2026-09-17T02:53:31"   # desde la escalera de salida corregida
PASS_FRACTION = 0.20                  # se opera ~el 20% mejor por score
EXPLORATION_RATE = 0.10               # de las descartadas, se opera igual este %
MIN_TRAIN_ROWS = 800
FILTERED_CHAINS = {"robinhood"}

FEATURES = [
    "wr_avg", "wr_min", "buy_avg", "buy_min", "conc", "age", "prior_avg", "prior_min",
    "n_buys", "n_sells", "n_buyers", "sell_share", "buy_usd", "span", "log_price",
    "top_buy_share", "hour",
]
LOG_FEATURES = {"buy_avg", "buy_min", "buy_usd", "age", "prior_avg", "prior_min", "span"}


def build_features(conn, a) -> dict | None:
    """Variables de una alerta, todas conocidas AL MOMENTO de alertar
    (ventana de 8 min previa en `transactions`). El mismo código sirve
    para entrenar y para puntuar en vivo -- sin duplicar lógica."""
    try:
        t_alert = datetime.fromisoformat(a["triggered_at"])
    except (TypeError, ValueError):
        return None
    t0 = (t_alert - timedelta(seconds=480)).isoformat()
    txs = conn.execute(
        """SELECT wallet, side, amount_usd, detected_at FROM transactions
           WHERE chain=? AND token_address=? AND detected_at >= ? AND detected_at <= ?
           ORDER BY detected_at""",
        (a["chain"], a["token_address"], t0, a["triggered_at"]),
    ).fetchall()
    buys = [t for t in txs if t["side"] == "buy" and t["amount_usd"]]
    sells = [t for t in txs if t["side"] == "sell" and t["amount_usd"]]
    if len(buys) < 2:
        return None
    buy_usd = sum(b["amount_usd"] for b in buys)
    sell_usd = sum(s["amount_usd"] for s in sells)
    per_w: dict = {}
    for b in buys:
        per_w[b["wallet"]] = per_w.get(b["wallet"], 0) + b["amount_usd"]
    span = (datetime.fromisoformat(buys[-1]["detected_at"]) - datetime.fromisoformat(buys[0]["detected_at"])).total_seconds()
    lp = a["price_at_alert"]
    return {
        "wr_avg": a["avg_wallet_win_rate"], "wr_min": a["min_wallet_win_rate"],
        "buy_avg": a["avg_wallet_buy_usd"], "buy_min": a["min_wallet_buy_usd"],
        "conc": a["early_buy_concentration"], "age": a["token_age_seconds"],
        "prior_avg": a["avg_wallet_prior_trades"], "prior_min": a["min_wallet_prior_trades"],
        "n_buys": len(buys), "n_sells": len(sells), "n_buyers": len(per_w),
        "sell_share": sell_usd / (buy_usd + sell_usd) if (buy_usd + sell_usd) else 0.0,
        "buy_usd": buy_usd, "span": span,
        "log_price": math.log10(lp) if lp and lp > 0 else None,
        "top_buy_share": max(per_w.values()) / buy_usd if buy_usd else None,
        "hour": t_alert.hour,
    }


def _transform(feats: dict) -> list:
    """dict -> vector (NaN donde falta). Logs para variables de cola larga."""
    vec = []
    for f in FEATURES:
        v = feats.get(f)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            vec.append(float("nan"))
        elif f in LOG_FEATURES:
            vec.append(math.log1p(max(v, 0)))
        else:
            vec.append(float(v))
    vec.append(1.0 if feats.get("wr_avg") is not None else 0.0)  # wr_known
    return vec


def _fill(mat: np.ndarray, medians: np.ndarray) -> np.ndarray:
    out = mat.copy()
    idx = np.where(np.isnan(out))
    out[idx] = np.take(medians, idx[1])
    return out


def _new_model():
    return GradientBoostingClassifier(
        n_estimators=120, max_depth=2, learning_rate=0.05, subsample=0.7,
        min_samples_leaf=40, random_state=1,
    )


def train() -> dict | None:
    with get_conn() as conn:
        bad = {r["position_id"] for r in conn.execute(
            "SELECT DISTINCT position_id FROM paper_position_exits WHERE reason LIKE '%_price_anomaly'")}
        rows = conn.execute(
            """SELECT p.id pid, p.pnl_usd, a.* FROM paper_positions p
               JOIN stampede_alerts a ON a.id = p.alert_id
               WHERE p.status='closed' AND p.opened_at > ? AND a.chain IN ('robinhood')
               ORDER BY a.triggered_at""", (TRAIN_SINCE,)).fetchall()
        X, y, pnl = [], [], []
        for r in rows:
            if r["pid"] in bad:
                continue
            f = build_features(conn, r)
            if f is None:
                continue
            X.append(_transform(f)); y.append(1 if (r["pnl_usd"] or 0) > 0 else 0); pnl.append(r["pnl_usd"] or 0)
    n = len(X)
    if n < MIN_TRAIN_ROWS:
        logger.warning(f"Solo {n} filas de entrenamiento (mínimo {MIN_TRAIN_ROWS}) -- sin filtro todavía.")
        return None
    X = np.array(X); y = np.array(y); pnl = np.array(pnl)
    medians = np.nanmedian(X, axis=0)
    medians = np.where(np.isnan(medians), 0.0, medians)
    Xf = _fill(X, medians)

    # walk-forward: cada pliegue se predice con un modelo entrenado SOLO con lo anterior
    oof_scores, oof_idx = [], []
    cuts = [int(n * 0.4), int(n * 0.6), int(n * 0.8), n]
    aucs = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        m = _new_model().fit(Xf[:a], y[:a])
        p = m.predict_proba(Xf[a:b])[:, 1]
        if len(set(y[a:b])) > 1:
            aucs.append(roc_auc_score(y[a:b], p))
        oof_scores.append(p); oof_idx.extend(range(a, b))
    oof = np.concatenate(oof_scores)
    idx = np.array(oof_idx)
    threshold = float(np.quantile(oof, 1 - PASS_FRACTION))
    sel = oof >= threshold
    stats = {
        "n_train": n, "oof_auc_mean": float(np.mean(aucs)) if aucs else None,
        "oof_pass_n": int(sel.sum()),
        "oof_pass_winrate": float(y[idx][sel].mean()), "oof_base_winrate": float(y[idx].mean()),
        "oof_pass_pnl_avg": float(pnl[idx][sel].mean()), "oof_base_pnl_avg": float(pnl[idx].mean()),
    }
    final = _new_model().fit(Xf, y)
    bundle = {
        "model": final, "features": FEATURES, "medians": medians, "threshold": threshold,
        "trained_at": datetime.now(timezone.utc).isoformat(), **stats,
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(
        f"entry_filter entrenado: n={n}, AUC fuera de muestra={stats['oof_auc_mean']:.3f}, "
        f"pasa {PASS_FRACTION:.0%}: win-rate {stats['oof_pass_winrate']:.1%} vs base {stats['oof_base_winrate']:.1%}, "
        f"PnL/trade {stats['oof_pass_pnl_avg']:+.2f} vs {stats['oof_base_pnl_avg']:+.2f}"
    )
    return bundle


_cached = None
_cached_mtime = None


def get_cached_bundle() -> dict | None:
    global _cached, _cached_mtime
    if not os.path.exists(MODEL_PATH):
        return None
    mtime = os.path.getmtime(MODEL_PATH)
    if _cached is None or mtime != _cached_mtime:
        _cached = joblib.load(MODEL_PATH)
        _cached_mtime = mtime
        logger.info(f"entry_filter recargado (entrenado con {_cached['n_train']} filas, umbral {_cached['threshold']:.3f}).")
    return _cached


def decide(conn, alert_row) -> tuple[float | None, str]:
    """(score, decisión) con decisión en 'pass' | 'explore' | 'skip' | 'nofilter'.
    Cualquier problema -> 'nofilter' (se opera como antes, nunca se pierde
    una alerta por un fallo del filtro)."""
    try:
        if alert_row["chain"] not in FILTERED_CHAINS:
            return None, "nofilter"
        bundle = get_cached_bundle()
        if bundle is None:
            return None, "nofilter"
        feats = build_features(conn, alert_row)
        if feats is None:
            return None, "nofilter"
        vec = _fill(np.array([_transform(feats)]), bundle["medians"])
        score = float(bundle["model"].predict_proba(vec)[0, 1])
        if score >= bundle["threshold"]:
            return score, "pass"
        return score, ("explore" if random.random() < EXPLORATION_RATE else "skip")
    except Exception:
        logger.exception("Error en entry_filter.decide -- se opera sin filtrar")
        return None, "nofilter"
