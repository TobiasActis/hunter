"""
Seccion 8 del reporte nocturno: la lista de billeteras top por PnL realizado (data/watch_wallets.json, generada con watchlist_tool.py) medida HACIA ADELANTE como filtro en modo sombra.
No cambia nada de lo que opera el sistema. Solo cuenta alertas y posiciones POSTERIORES a `data_until` del JSON.
Criterio fijado de antemano: con >= 300 posiciones posteriores, el grupo "con >= 1 billetera de la lista" promedia > 0 con IC95 que excluye 0 y su diferencia contra "sin ninguna" tiene IC95 > 0.
Estudio previo (2026-09-20): -2.75 USD (+-2.0) con la lista contra -4.79 (+-1.4) sin ella (n=771 vs 1175): direccion correcta, sin significancia, y ambos negativos.
"""
import json
import os

import numpy as np

WL_PATH = "data/watch_wallets.json"
MIN_POS = 300


def _ci(x):
    x = np.asarray(x, float)
    return 1.96 * x.std(ddof=1) / np.sqrt(len(x)) if len(x) > 1 else float("nan")


def main(conn, wl_path=WL_PATH):
    print("=" * 72, "\n8) LISTA DE BILLETERAS TOP POR PnL (medida hacia adelante, modo sombra)\n", "=" * 72, sep="")
    if not os.path.exists(wl_path):
        print(f"(no hay {wl_path}: generarlo con watchlist_tool.py)")
        return
    wl = json.load(open(wl_path, encoding="utf-8"))
    ws = {x["wallet"] for x in wl["wallets"]}
    since = wl["data_until"]
    print(f"Lista de {len(ws)} billeteras armada con datos hasta {since[:19]} UTC. Se cuentan solo alertas posteriores.")
    alerts = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, price_at_alert, price_after_30m FROM stampede_alerts WHERE chain='robinhood' AND triggered_at > ?", (since,))}
    if not alerts:
        print("Todavia no hay alertas posteriores.")
        return
    hits = {}
    for aid, w in conn.execute("SELECT alert_id, wallet FROM alert_wallets WHERE alert_id IN (SELECT id FROM stampede_alerts WHERE chain='robinhood' AND triggered_at > ?)", (since,)):
        if w in ws:
            hits[aid] = hits.get(aid, 0) + 1
    # a) resultado del PRECIO (todas las alertas resueltas): gano = precio a 30 min >= 1.3x
    res = [(aid, (p30 / p0) >= 1.3) for aid, (p0, p30) in alerts.items() if p0 and p30 and p0 > 0 and p30 > 0]
    g1 = [w for aid, w in res if hits.get(aid, 0) >= 1]
    g0 = [w for aid, w in res if hits.get(aid, 0) == 0]
    print(f"Alertas resueltas posteriores: {len(res)} | con >=1 billetera de la lista: {len(g1)} | win (>=1.3x a 30 min): {np.mean(g1) * 100 if g1 else float('nan'):.1f}% contra {np.mean(g0) * 100 if g0 else float('nan'):.1f}% sin ninguna")
    # b) PnL de las posiciones realistas
    pos = {}
    for aid, pnl in conn.execute("""SELECT p.alert_id, SUM(p.pnl_usd) FROM paper_positions p WHERE p.status='closed' AND p.alert_id IN (SELECT id FROM stampede_alerts WHERE chain='robinhood' AND triggered_at > ?)
                                    AND p.id NOT IN (SELECT position_id FROM paper_position_exits WHERE reason LIKE '%anomaly%') GROUP BY p.alert_id""", (since,)):
        pos[aid] = pnl
    a1 = [v for k, v in pos.items() if hits.get(k, 0) >= 1]
    a2 = [v for k, v in pos.items() if hits.get(k, 0) >= 2]
    a0 = [v for k, v in pos.items() if hits.get(k, 0) == 0]
    print(f"Posiciones cerradas posteriores: {len(pos)}")
    for lab, x in (("con >=1 billetera de la lista", a1), ("con >=2 de la lista", a2), ("sin ninguna", a0)):
        if len(x) >= 15:
            print(f"  {lab:32s} n={len(x):4d} | PnL medio {np.mean(x):+6.2f} USD (+-{_ci(x):.2f}) | ganadoras {np.mean(np.array(x) > 0) * 100:.0f}%")
        else:
            print(f"  {lab:32s} n={len(x)} (pocas)")
    if len(a1) >= 15 and len(a0) >= 15:
        diff = np.mean(a1) - np.mean(a0)
        se = (np.var(a1, ddof=1) / len(a1) + np.var(a0, ddof=1) / len(a0)) ** 0.5
        ok = len(pos) >= MIN_POS and (np.mean(a1) - _ci(a1)) > 0 and (diff - 1.96 * se) > 0
        print(f"  diferencia con - sin: {diff:+.2f} USD (IC95 [{diff - 1.96 * se:+.2f}, {diff + 1.96 * se:+.2f}])")
        print("Criterio de adopcion (>=300 posiciones, grupo con lista > 0 con IC95 y diferencia > 0):", "CUMPLE" if ok else "NO se cumple todavia")
    else:
        print("Criterio de adopcion: muestra insuficiente todavia.")
