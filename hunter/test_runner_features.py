"""
Pruebas de core/runner_features.py y del puntaje de corredoras en modo sombra (core/attention_lab.score_runner). Correr con: python test_runner_features.py
Base sintetica: comprueba que los rasgos usan SOLO lo anterior a la alerta (sin mirar el futuro), la aritmetica de la cinta y el flujo de puntuar alertas nuevas.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from core import attention_lab as al
from core import runner_features as rf

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


T0 = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
iso = lambda s: (T0 + timedelta(seconds=s)).isoformat()
hdb = os.path.join(tempfile.mkdtemp(), "h.db")
c = sqlite3.connect(hdb)
c.execute("""CREATE TABLE stampede_alerts (id INTEGER PRIMARY KEY, chain TEXT, token_address TEXT, triggered_at TEXT, token_age_seconds REAL, avg_wallet_win_rate REAL, min_wallet_win_rate REAL,
    avg_wallet_buy_usd REAL, min_wallet_buy_usd REAL, creator_tokens_created REAL, creator_migration_rate REAL, early_buy_concentration REAL, entry_score REAL, chase_ratio REAL, wallet_rep_score REAL,
    avg_wallet_prior_trades REAL, min_wallet_prior_trades REAL)""")
c.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, chain TEXT, token_address TEXT, detected_at TEXT, price REAL, side TEXT, amount_usd REAL, wallet TEXT)")
rows = [("0xt", 0, 1.0, "buy", 100, "A"), ("0xt", 5, 1.2, "buy", 50, "B"), ("0xt", 9, 1.5, "buy", 50, "C"), ("0xt", 12, 1.4, "sell", 30, "B"), ("0xt", 25, 9.9, "buy", 500, "Z")]       # la ultima es POSTERIOR a la alerta
for tok, s, px, side, usd, w in rows:
    c.execute("INSERT INTO transactions (chain, token_address, detected_at, price, side, amount_usd, wallet) VALUES ('robinhood', ?, ?, ?, ?, ?, ?)", (tok, iso(s), px, side, usd, w))
c.execute("INSERT INTO transactions (chain, token_address, detected_at, price, side, amount_usd, wallet) VALUES ('robinhood', '0xsolo', ?, 1.0, 'buy', 10, 'A')", (iso(0),))
def alert(i, tok, s): c.execute("INSERT INTO stampede_alerts VALUES (?, 'robinhood', ?, ?, 30, 0.4, 0.3, 200, 80, 2, 0.5, 0.6, 0.7, NULL, 0.1, 10, 4)", (i, tok, iso(s)))
alert(1, "0xt", 20); alert(2, "0xsolo", 20)
c.commit()

print("--- rasgos de la cinta")
tf = rf.tape_features(c, "0xt", iso(20), watch={"A"})
check("usa solo operaciones ANTERIORES a la alerta (la de 25 s queda afuera)", tf["n_tx"] == 4 and tf["vol_buy"] == 200.0)
check("compradores, concentracion y ventas", tf["n_buyers"] == 3 and abs(tf["top1_share"] - 0.5) < 1e-9 and abs(tf["top3_share"] - 1.0) < 1e-9 and tf["n_sell"] == 1 and tf["max_buy"] == 100.0)
check("progreso del precio, caida maxima y cerca del maximo", abs(tf["prog"] - 1.4) < 1e-9 and abs(tf["dd"] - 1.4 / 1.5) < 1e-9 and abs(tf["near_high"] - 1.4 / 1.5) < 1e-9)
check("billeteras de la lista de vigilancia entre los compradores", tf["wl_hits"] == 1)
check("ultimos 10 s: compra de 50 (a 9 s... = 11 s antes) queda fuera, la venta a 12 s dentro", tf["last10_buy"] == 0.0 and tf["last10_sell"] == 30.0)
check("menos de 2 operaciones previas -> None", rf.tape_features(c, "0xsolo", iso(20), watch=set()) is None)
af = rf.alert_features(c, 1)
check("rasgos de la alerta (sin peak_wallet_count)", af["wr_avg"] == 0.4 and af["cr_rate"] == 0.5 and af["chase"] is None and "peak_wallet_count" not in af)
vec = rf.feature_vector(af, tf)
check("vector en el orden del modelo, sin 'chase' y con NaN donde falta", vec.shape == (1, len(rf.MODEL_FEATURES)) and "chase" not in rf.MODEL_FEATURES and np.isnan(vec).any() == False)

print("--- puntaje de alertas nuevas (modo sombra)")
X = np.random.default_rng(0).random((300, len(rf.MODEL_FEATURES))); y = (X[:, 0] + np.random.default_rng(1).random(300) > 1.1).astype(int)
mp = os.path.join(tempfile.mkdtemp(), "m.joblib")
joblib.dump({"model": HistGradientBoostingClassifier(max_iter=20).fit(X, y), "features": rf.MODEL_FEATURES, "thr_top2": 0.9, "thr_top5": 0.5}, mp)
edb = os.path.join(tempfile.mkdtemp(), "e.db"); al.init_db(edb)
now = T0 + timedelta(seconds=120)
check("sin modelo no hace nada", al.score_runner(hdb, edb, model_path=os.path.join(tempfile.mkdtemp(), "no.joblib"), now=now) == 0)
check("la primera pasada solo fija el punto de partida (no rellena el pasado)", al.score_runner(hdb, edb, model_path=mp, now=now) == 0)
alert(3, "0xt", 20); alert(4, "0xsolo", 21); alert(5, "0xt", 119)                                    # la 5 es de hace 1 s: todavia no se puntua
c.commit()
n = al.score_runner(hdb, edb, model_path=mp, now=now)
with al.conn_ctx(edb) as e:
    sc = {r["alert_id"]: dict(r) for r in e.execute("SELECT * FROM at_runner_score")}
check("puntua las alertas de mas de 10 s y deja para despues la muy reciente", n == 2 and set(sc) == {3, 4})
check("guarda un puntaje entre 0 y 1 con los rasgos usados, y marca top2/top5", 0 <= sc[3]["score"] <= 1 and '"n_tx": 4' in sc[3]["feats"] and sc[3]["top2"] in (0, 1) and sc[3]["top5"] in (0, 1))
check("sin cinta previa: fila con nota y sin puntaje (no se pierde la alerta)", sc[4]["score"] is None and "sin cinta" in sc[4]["note"])
n2 = al.score_runner(hdb, edb, model_path=mp, now=T0 + timedelta(seconds=200))
check("la alerta pendiente se puntua despues y no se repite lo ya hecho", n2 == 1)

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
