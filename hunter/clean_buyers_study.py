"""
Estudio de "compradores limpios" (filtros del trader J, 2026-09-20) sobre una COPIA de la base de HUNTER (no correr en el servidor: carga 1M+ transacciones en memoria).

Regla FIJADA DE ANTEMANO (umbrales = percentil 60 de la primera mitad de las alertas, 13-18 sep 2026; no se reajustan):
    LIMPIO = concentracion de compras top1 <= 0.54  Y  compras de tamano igual <= 0.25  Y  ninguna billetera nueva (fresh == 0)
  - top1: fraccion del volumen comprado (USD) antes de la alerta que se llevo la wallet que mas compro (proxy del "mapa de burbujas").
  - same: fraccion de las compras cuyo monto (2 cifras significativas) es el mas repetido (billeteras repartidas con el mismo monto).
  - fresh: fraccion de las wallets de la alerta cuya primera transaccion en nuestros datos ocurrio en los 10 min previos (billetera nueva).
Resultado con datos previos (fuera de muestra, posiciones realistas v2+, n=518): LIMPIO +2.4 USD por posicion (IC95 [-3.3, +8.7], n=206) contra -6.3 las demas; diferencia +8.7 (IC95 [+2.0, +15.6]);
el win-rate mejora +1.8 pp fuera de muestra (+4.7 pp en muestra), IC cruza 0. No es ganancia demostrada.
Criterio para adoptarlo (fijado de antemano): con >= 300 posiciones NUEVAS (posteriores a --since) el grupo LIMPIO promedia > 0 con IC95 que excluye 0 y la diferencia contra el resto es positiva en ambas mitades.

RESULTADO CON DATOS NUEVOS (2026-09-20, 975 posiciones posteriores a los umbrales, copia de la base del 20-sep 15:40 UTC): LIMPIO -3.21 USD (IC95 [-4.74,-1.64], n=432) contra -3.25 el resto
(n=543); diferencia +0.05 (IC95 [-1.9,+2.1]); win-rate de las alertas 9.3% contra 9.4%. SIN EFECTO: el +8 USD de la muestra anterior fue casualidad del periodo (13-19 sep). DESCARTADO como filtro.
Sirve de ejemplo de por que se fijan los umbrales antes y se prueba con datos posteriores.

Uso:  python clean_buyers_study.py RUTA_COPIA.db [--since 2026-09-19T21:54:00]   (--since = inicio de la version a evaluar; por defecto v4)
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

TOP1_MAX = 0.54
SAME_MAX = 0.25
FRESH_MAX = 0.0
V4_SINCE = "2026-09-19T21:54:00"


def _ts(s):
    return pd.to_datetime(s, utc=True, format="ISO8601", errors="coerce").astype("int64") / 1e9


def compute_features(c) -> pd.DataFrame:
    al = pd.read_sql("SELECT id, token_address, triggered_at, price_at_alert, price_after_30m FROM stampede_alerts WHERE chain='robinhood' AND price_at_alert>0", c)
    al["t"] = _ts(al["triggered_at"])
    al["y"] = al["price_after_30m"] / al["price_at_alert"]
    tx = pd.read_sql("SELECT wallet, token_address, side, amount_usd, detected_at FROM transactions WHERE chain='robinhood' AND amount_usd IS NOT NULL", c)
    tx["t"] = _ts(tx["detected_at"])
    tx = tx.sort_values("t")
    first_seen = tx.groupby("wallet")["t"].min().to_dict()
    by_tok = {k: g for k, g in tx.groupby("token_address")}
    aw = pd.read_sql("SELECT alert_id, wallet FROM alert_wallets", c)
    aw_by = {k: list(g["wallet"]) for k, g in aw.groupby("alert_id")}
    rows = []
    for r in al.itertuples():
        g = by_tok.get(r.token_address)
        if g is None:
            continue
        b = g[(g["t"] <= r.t) & (g["side"] == "buy")]
        if len(b) < 2:
            continue
        amt = b["amount_usd"].values
        tot = amt.sum()
        if tot <= 0:
            continue
        per = b.groupby("wallet")["amount_usd"].sum().sort_values(ascending=False).values
        mag = 10.0 ** (np.floor(np.log10(np.maximum(amt, 1e-9))) - 1)
        same = float(pd.Series(np.round(amt / mag) * mag).value_counts().iloc[0] / len(amt))
        ws = aw_by.get(r.id, [])
        fresh = float(np.mean([(r.t - first_seen[w]) <= 600 for w in ws if w in first_seen])) if ws else np.nan
        rows.append(dict(id=r.id, t=r.t, y=r.y, top1=per[0] / tot, same=same, fresh=fresh))
    F = pd.DataFrame(rows)
    F["win"] = (F["y"] >= 1.3).astype(float)
    F.loc[F["y"].isna(), "win"] = np.nan
    F["clean"] = (F["top1"] <= TOP1_MAX) & (F["same"] <= SAME_MAX) & (F["fresh"] <= FRESH_MAX)
    return F


def load_positions(c, since: str) -> pd.DataFrame:
    p = pd.read_sql(f"""SELECT p.alert_id id, p.pnl_usd, p.opened_at FROM paper_positions p WHERE p.status='closed' AND p.opened_at >= '{since}' AND p.alert_id IS NOT NULL
                        AND p.id NOT IN (SELECT position_id FROM paper_position_exits WHERE reason LIKE '%anomaly%')""", c)
    return p.groupby("id").agg(pnl=("pnl_usd", "sum"), opened_at=("opened_at", "min")).reset_index()


def _boot(x, n=4000, rng=np.random.default_rng(3)):
    x = np.asarray(x, float)
    return np.percentile([rng.choice(x, len(x)).mean() for _ in range(n)], [2.5, 97.5])


def _diff(a, b, n=4000, rng=np.random.default_rng(4)):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return np.percentile([rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean() for _ in range(n)], [2.5, 97.5])


def report(F: pd.DataFrame, pos: pd.DataFrame, since: str):
    D = F.merge(pos, on="id", how="inner").sort_values("opened_at").reset_index(drop=True)
    print(f"Posiciones cerradas desde {since}: {len(D)} | PnL medio {D.pnl.mean():+.2f} USD | ganadoras {(D.pnl > 0).mean():.1%}")
    if len(D) < 30:
        print("Muestra insuficiente (criterio: >= 300).")
        return
    for nm, S in (("TODAS", D), ("1ra mitad", D.iloc[: len(D) // 2]), ("2da mitad", D.iloc[len(D) // 2:])):
        p, q = S[S.clean], S[~S.clean]
        line = f"  {nm:10s} LIMPIO n={len(p):4d} media {p.pnl.mean() if len(p) else float('nan'):+6.2f}"
        if len(p) > 10:
            ci = _boot(p.pnl); line += f" IC95 [{ci[0]:+.2f},{ci[1]:+.2f}]"
        line += f" | resto n={len(q):4d} media {q.pnl.mean() if len(q) else float('nan'):+6.2f}"
        if len(p) > 10 and len(q) > 10:
            d = _diff(p.pnl, q.pnl); line += f" | dif {p.pnl.mean() - q.pnl.mean():+.2f} IC95 [{d[0]:+.2f},{d[1]:+.2f}]"
        print(line)
    p = D[D.clean]
    ok = len(D) >= 300 and len(p) > 10 and _boot(p.pnl)[0] > 0
    print("Criterio de adopcion (>=300 posiciones nuevas y LIMPIO con IC95 > 0):", "CUMPLE" if ok else "NO se cumple todavia")
    A = F[F.t >= pd.Timestamp(since, tz="UTC").timestamp()].dropna(subset=["win"])
    if len(A) > 50:
        p, q = A[A.clean], A[~A.clean]
        d = _diff(p.win, q.win)
        print(f"Todas las alertas resueltas desde {since}: n={len(A)} | win LIMPIO {p.win.mean():.1%} (n={len(p)}) vs resto {q.win.mean():.1%} (n={len(q)}) | dif {100 * (p.win.mean() - q.win.mean()):+.1f} pp IC95 [{100 * d[0]:+.1f},{100 * d[1]:+.1f}]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", default=V4_SINCE)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    feats = compute_features(conn)
    print(f"Alertas con features: {len(feats)} | LIMPIO: {feats.clean.mean():.1%}")
    report(feats, load_positions(conn, a.since), a.since)
