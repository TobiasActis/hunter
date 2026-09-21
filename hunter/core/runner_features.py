"""
Rasgos para anticipar "corredoras" (tokens que llegan a >= 3x en 1 h), calculados SOLO con lo que se sabia al momento de la alerta:
  - rasgos de la alerta (stampede_alerts): edad, win-rate de las billeteras, tamano de compra, historial del creador, puntaje de ML, "chase"...
  - rasgos de la CINTA del token: solo operaciones con detected_at < triggered_at (cuantas, de cuantas billeteras, si una concentra las compras, cuanto subio el precio, aceleracion...).
Estudio del 2026-09-21 (3.421 posiciones, v2-v5, solo curvas en ETH; entrenar en un periodo y evaluar en el otro, ambos sentidos): el 2% mejor por puntaje (~24 alertas/dia) acierta >= 3x el 27% de las veces contra 10.6% de base
(lift 2.5x) y con salida amplia rindio +$9/op (IC95 [-0.0, +18.7]) en replay. NO esta probado hacia adelante: por eso el puntaje solo se GUARDA (modo sombra) y se mide despues; no decide nada.
El mismo codigo lo usan el estudio y el puntaje en vivo (test_runner_features.py comprueba que da lo mismo que el estudio).
"""
import json
import os
from datetime import datetime, timezone

import numpy as np

ALERT_FEATURES = ["age", "wr_avg", "wr_min", "buy_avg", "buy_min", "cr_n", "cr_rate", "conc", "entry_score", "chase", "rep", "prior_avg", "prior_min"]
TAPE_FEATURES = ["n_tx", "n_buy", "n_sell", "vol_buy", "vol_sell", "buy_share", "n_wallets", "n_buyers", "top1_share", "top3_share", "max_buy", "med_buy", "tape_age_s", "rate_min", "prog", "dd",
                 "near_high", "last10_buy", "last10_sell", "acc", "wl_hits", "repeat_share", "hour"]
FEATURES = ALERT_FEATURES + TAPE_FEATURES
# "chase" (llenado vs precio de la alerta) solo existe si se abrio una posicion; con el filtro v6 las descartadas no tienen: se quita del modelo para que el puntaje sea comparable en todas las alertas
MODEL_FEATURES = [f for f in FEATURES if f != "chase"]
USD_MAX = 20000.0
_WL = None


def watch_wallets(path="data/watch_wallets.json"):
    global _WL
    if _WL is None:
        try:
            _WL = {w["wallet"] for w in json.load(open(path, encoding="utf-8"))["wallets"]}
        except Exception:
            _WL = set()
    return _WL


def _ts(s):
    return datetime.fromisoformat(s).timestamp()


def alert_features(conn, alert_id):
    """Rasgos de la alerta (sin peak_wallet_count, que se actualiza DESPUES de la alerta)."""
    cur = conn.execute("""SELECT token_age_seconds age, avg_wallet_win_rate wr_avg, min_wallet_win_rate wr_min, avg_wallet_buy_usd buy_avg, min_wallet_buy_usd buy_min, creator_tokens_created cr_n,
        creator_migration_rate cr_rate, early_buy_concentration conc, entry_score, chase_ratio chase, wallet_rep_score rep, avg_wallet_prior_trades prior_avg, min_wallet_prior_trades prior_min
        FROM stampede_alerts WHERE id = ?""", (alert_id,))
    row = cur.fetchone()
    if row is None:
        return None
    r = dict(zip([d[0] for d in cur.description], tuple(row)))               # funciona con o sin row_factory
    return {k: (float(r[k]) if r[k] is not None else None) for k in ALERT_FEATURES}


def tape_features(conn, token, triggered_at, watch=None):
    """Rasgos de la cinta del token ANTES de la alerta, o None si hay menos de 2 operaciones."""
    watch = watch_wallets() if watch is None else watch
    tx = conn.execute("SELECT detected_at, price, side, amount_usd, wallet FROM transactions WHERE chain='robinhood' AND token_address=? AND detected_at < ? AND price > 0 AND amount_usd > 0 AND amount_usd <= ? ORDER BY id",
                      (token, triggered_at, USD_MAX)).fetchall()
    if len(tx) < 2:
        return None
    ta = _ts(triggered_at)
    t = np.array([_ts(x[0]) for x in tx]); px = np.array([x[1] for x in tx]); us = np.array([x[3] for x in tx]); buy = np.array([x[2] == "buy" for x in tx]); w = [x[4] for x in tx]
    bu = us[buy]; span = max(ta - t[0], 1.0)
    bw, bcount = {}, {}
    for wi, ui, bi in zip(w, us, buy):
        if bi:
            bw[wi] = bw.get(wi, 0.0) + ui
            bcount[wi] = bcount.get(wi, 0) + 1
    tot = sum(bw.values()) or 1.0
    srt = sorted(bw.values(), reverse=True)
    cm = np.maximum.accumulate(px)
    last10 = t >= ta - 10
    prev30 = (t >= ta - 40) & (t < ta - 10)
    f = {"n_tx": len(tx), "n_buy": int(buy.sum()), "n_sell": int((~buy).sum()), "vol_buy": float(bu.sum()), "vol_sell": float(us[~buy].sum()), "buy_share": float(bu.sum() / max(us.sum(), 1e-9)),
         "n_wallets": len(set(w)), "n_buyers": len(bw), "top1_share": srt[0] / tot if srt else None, "top3_share": sum(srt[:3]) / tot if srt else None, "max_buy": float(bu.max()) if len(bu) else 0.0,
         "med_buy": float(np.median(bu)) if len(bu) else 0.0, "tape_age_s": span, "rate_min": len(tx) / max(span / 60, 0.1), "prog": float(px[-1] / px[0]), "dd": float(np.min(px / cm)), "near_high": float(px[-1] / cm[-1]),
         "last10_buy": float(us[last10 & buy].sum()), "last10_sell": float(us[last10 & ~buy].sum()), "acc": float(us[last10 & buy].sum() / max(us[prev30 & buy].sum() / 3, 1.0)),
         "wl_hits": len(set(bw) & watch), "repeat_share": (sum(1 for k in bw if bcount[k] > 1) / len(bw)) if bw else None,
         "hour": datetime.fromtimestamp(ta, tz=timezone.utc).hour}
    return f


def feature_vector(alert_feats, tape_feats):
    """Vector en el orden de FEATURES (NaN donde falta), o None si no hay cinta."""
    if alert_feats is None or tape_feats is None:
        return None
    a = {**alert_feats, **tape_feats}
    return np.array([[float(a[k]) if a.get(k) is not None else np.nan for k in MODEL_FEATURES]])
