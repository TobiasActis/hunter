"""
Entrena el modelo de 'corredoras' (llega a >= 3x en 1 h) con los rasgos de core/runner_features.py y lo guarda en data/runner_model.joblib (el puntaje en vivo lo usa en modo sombra: SOLO guarda, no decide nada).
Correr en el SERVIDOR (para que el modelo se guarde con la misma version de scikit-learn que lo va a leer):
    ./venv/bin/python train_runner_model.py runner_old.jsonl tape_old.jsonl runner_new.jsonl tape_new.jsonl
Cada par (runner_*.jsonl, tape_*.jsonl) sale de _runner_extract.py y _tape_extract.py (estudio del 2026-09-21). Imprime la validacion cruzada entre periodos (entrenar en uno, evaluar en el otro).
"""
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from core.runner_features import MODEL_FEATURES

PARAMS = dict(max_depth=3, learning_rate=0.05, max_iter=150, l2_regularization=1.0, random_state=0)


def load(a, b):
    r = pd.read_json(a, lines=True)
    t = pd.read_json(b, lines=True)
    return r.drop(columns=[c for c in ("pnl_live", "peak1h") if c in r.columns and c in t.columns]).merge(t, on="id", how="inner")


def main(paths):
    old, new = load(paths[0], paths[1]), load(paths[2], paths[3])
    print(f"periodo viejo {len(old)} | nuevo {len(new)} | rasgos {len(MODEL_FEATURES)}")
    oof = []
    for lab, tr, te in (("viejo->nuevo", old, new), ("nuevo->viejo", new, old)):
        m = HistGradientBoostingClassifier(**PARAMS).fit(tr[MODEL_FEATURES].astype(float), (tr.peak1h >= 3).astype(int))
        sc = m.predict_proba(te[MODEL_FEATURES].astype(float))[:, 1]
        y = (te.peak1h >= 3).astype(int).values
        o = np.argsort(-sc)
        line = f"  {lab}: AUC {roc_auc_score(y, sc):.3f} (base {y.mean()*100:.1f}%)"
        for k in (0.02, 0.05):
            top = o[: int(len(te) * k)]
            line += f" | top{k*100:.0f}%: acierta {y[top].mean()*100:.0f}%"
        print(line)
        oof.append(sc)
    allsc = np.concatenate(oof)
    thr2, thr5 = float(np.quantile(allsc, 0.98)), float(np.quantile(allsc, 0.95))
    full = pd.concat([old, new])
    model = HistGradientBoostingClassifier(**PARAMS).fit(full[MODEL_FEATURES].astype(float), (full.peak1h >= 3).astype(int))
    joblib.dump({"model": model, "features": MODEL_FEATURES, "thr_top2": thr2, "thr_top5": thr5, "n_train": len(full), "params": PARAMS}, "data/runner_model.joblib")
    print(f"guardado data/runner_model.joblib (n={len(full)}, umbral top2% {thr2:.3f}, top5% {thr5:.3f})")


if __name__ == "__main__":
    main(sys.argv[1:5])
