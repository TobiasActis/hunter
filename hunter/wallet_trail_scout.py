"""
Reconstruye data/trail_wallets.json con datos FRESCOS (no una foto fija): billeteras que entran >= 20 s despues del lanzamiento (se pueden seguir) y compran tokens que corren >= 3x en 1 h mucho mas seguido que
la base. Mismo metodo del estudio del 2026-09-21 (ver memoria del proyecto). Hace falta refrescarla: se midio que al dia siguiente de construida, solo el 1% de las alertas tenia alguna de sus billeteras comprando
(las listas fijas pierden vigencia rapido).

*** NO instalar como timer en el servidor (probado 2026-09-22: con --dias 7 sobre ~2.4M operaciones, el swap subio de 350 MB a 979 MB en 5 minutos y el proceso principal (hunter/main.py) empezo a paginar --
riesgo real para el sistema de trading en vivo en una VM de 956 MB; se aborto a mano). Correr SOLO A MANO, y preferentemente LOCAL contra una copia de la base (como se armo la primera vez), o en el servidor con
--dias 1-2 y vigilando `free -m` mientras corre. Solo lee ~/hunter/data/hunter.db (solo lectura) y escribe data/trail_wallets.json (con reemplazo atomico, asi que un corte a mitad de camino no corrompe nada).
Correr: ./venv/bin/python wallet_trail_scout.py [--dias 2] [--min-tokens 15] [--min-entry-s 20]
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

DB = os.path.join("data", "hunter.db")
OUT = os.path.join("data", "trail_wallets.json")
PRICE_MAX = 1e-5
USD_MAX = 20000.0
FOLLOW_DELAY_S = 5.0
OUTCOME_WINDOW_S = 3600.0
RUNNER_MULT = 3.0
WILSON_LIFT = 1.5
CONTROL_SAMPLE = 4


def wilson_lo(hits, n, z=1.96):
    p = hits / n
    d = 1 + z * z / n
    ce = p + z * z / (2 * n)
    ad = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (ce - ad) / d


def build(db_path, days, min_tokens, min_entry_s, min_p25_s):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    has_q = any(r[1] == "quote_token" for r in c.execute("PRAGMA table_info(tokens)"))
    bad = {r[0] for r in c.execute("SELECT token_address FROM tokens WHERE chain='robinhood' AND quote_token IS NOT NULL AND quote_token != ?", ("0x" + "0" * 40,))} if has_q else set()
    tx = pd.read_sql("SELECT wallet, token_address token, side, amount_usd usd, price, detected_at FROM transactions WHERE chain='robinhood' AND detected_at >= ? AND price>0 AND amount_usd>0", c, params=(since,))
    c.close()
    tx = tx[(tx.price <= PRICE_MAX) & (tx.usd <= USD_MAX) & ~tx.token.isin(bad)].copy()
    tx["t"] = pd.to_datetime(tx.detected_at, utc=True, format="ISO8601").astype("int64") / 1e9
    tx = tx.sort_values("t").reset_index(drop=True)
    n_tx = len(tx)
    if n_tx < 1000:
        return None, {"n_tx": n_tx}
    tok = {k: (g.t.values, g.price.values) for k, g in tx.groupby("token", sort=False)}
    first_t = {k: v[0][0] for k, v in tok.items()}

    def follow(token, tb):
        t, p = tok[token]
        k = np.searchsorted(t, tb + FOLLOW_DELAY_S)
        if k >= len(t):
            return None
        j = np.searchsorted(t, t[k] + OUTCOME_WINDOW_S)
        return (p[k:j].max() / p[k]) if j > k else None

    buys = tx[tx.side == "buy"]
    fb = buys.groupby(["wallet", "token"], sort=False).t.min().reset_index()
    fb["early_s"] = fb.t.values - fb.token.map(first_t).values
    res = []
    for w, token, tb, early in zip(fb.wallet.values, fb.token.values, fb.t.values, fb.early_s.values):
        peak = follow(token, tb)
        if peak is not None:
            res.append((w, token, tb, early, peak))
    E = pd.DataFrame(res, columns=["wallet", "token", "tb", "early_s", "peak"])
    E["run"] = (E.peak >= RUNNER_MULT).astype(int)
    del tx, buys, fb, tok
    if len(E) < 500:
        return None, {"n_tx": n_tx, "n_events": len(E)}
    base_rate = E.run.mean()
    gap = None  # sin datos de bots por ahora (ventana corta); se filtran solo por entrada temprana
    W = E.groupby("wallet").agg(n=("run", "size"), hits=("run", "sum"), med_early=("early_s", "median"), p25_early=("early_s", lambda s: np.percentile(s, 25)))
    W = W[(W.n >= min_tokens) & (W.med_early >= min_entry_s) & (W.p25_early >= min_p25_s)].copy()
    W["p"] = W.hits / W.n
    W["lo"] = wilson_lo(W.hits, W.n)
    W["lift"] = W.p / base_rate
    sel = W[W.lo > WILSON_LIFT * base_rate].sort_values("lo", ascending=False)
    wallets = [{"wallet": w, "tokens": int(r.n), "hits": int(r.hits), "p": round(float(r.p), 4), "lo": round(float(r.lo), 4), "lift": round(float(r.lift), 2),
               "med_entry_s": round(float(r.med_early), 1), "p25_entry_s": round(float(r.p25_early), 1)} for w, r in sel.iterrows()]
    meta = {"n_tx": n_tx, "n_events": len(E), "n_tokens": E.token.nunique(), "n_wallets_screened": len(W), "base_rate": round(float(base_rate), 4)}
    return wallets, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=2, help="ventana de dias (chica a proposito: ver la advertencia de memoria en el docstring antes de subirla)")
    ap.add_argument("--min-tokens", type=int, default=15)
    ap.add_argument("--min-entry-s", type=float, default=20.0)
    ap.add_argument("--min-p25-s", type=float, default=5.0)
    a = ap.parse_args()
    wallets, meta = build(DB, a.dias, a.min_tokens, a.min_entry_s, a.min_p25_s)
    if wallets is None:
        print(f"muy pocos datos todavia para reconstruir la lista ({meta}); no se toca {OUT}")
        return
    now = datetime.now(timezone.utc)
    out = {"built_at": now.isoformat(), "data_until": now.isoformat(), "window_days": a.dias, "source": f"billeteras que entran >= {a.min_entry_s:.0f} s despues del lanzamiento (mediana) y compran tokens que "
           f"corren >= {RUNNER_MULT:.0f}x en 1 h (retraso {FOLLOW_DELAY_S:.0f} s) con cota inferior de Wilson > {WILSON_LIFT}x la base; >= {a.min_tokens} tokens; ventana de {a.dias} dias; solo curvas en ETH",
           "base_rate": meta["base_rate"], "meta": meta, "wallets": wallets}
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, OUT)
    print(f"{meta}: {len(wallets)} billeteras seleccionadas -> {OUT}")
    for w in wallets[:5]:
        print(f"  {w['wallet']}  n={w['tokens']} acierto {w['p']*100:.0f}% (cota {w['lo']*100:.0f}%) lift {w['lift']:.1f}x entra a los {w['med_entry_s']:.0f}s")


if __name__ == "__main__":
    main()
