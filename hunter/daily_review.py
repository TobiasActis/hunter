"""
Revisión diaria de HUNTER (solo lectura). Uso en el servidor:
    ./venv/bin/python daily_review.py            # desde que arrancó la simulación realista
    ./venv/bin/python daily_review.py 2026-09-19 # desde una fecha (UTC)

Secciones: estado del sistema, resultados por día y por decisión del
filtro, razones de salida, y CÓMO SE COMPORTÓ EL PRECIO de las ganadoras
vs las perdedoras después de entrar (trayectoria, presión compradora/
vendedora, tiempo hasta el pico) para entender por qué ganan o pierden.
Siempre excluye posiciones con anomalías de precio.
"""
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np

DB = "data/hunter.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def setting(key):
    r = conn.execute("SELECT value FROM dashboard_settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


since = (sys.argv[1] + "T00:00:00") if len(sys.argv) > 1 else (setting("sim_v3_since") or setting("sim_v2_since") or "2026-09-18T22:37:45")
print(f"Revisión desde {since} (UTC)\n")

bad = {r[0] for r in conn.execute(
    "SELECT DISTINCT position_id FROM paper_position_exits WHERE reason LIKE '%_price_anomaly'")}

# ---------------------------------------------------------------- 1) estado
print("=" * 72, "\n1) ESTADO DEL SISTEMA\n", "=" * 72, sep="")
last_tx = conn.execute("SELECT MAX(detected_at) FROM transactions WHERE chain='robinhood'").fetchone()[0]
last_al = conn.execute("SELECT MAX(triggered_at) FROM stampede_alerts").fetchone()[0]
print(f"última tx robinhood: {last_tx}\núltima alerta:       {last_al}\nahora (UTC):         {datetime.utcnow().isoformat()}")
rows = conn.execute("""SELECT substr(triggered_at,1,13) h, COUNT(*) n,
    SUM(entry_decision IN ('pass','shadow_pass')) p, SUM(entry_decision='explore') e, SUM(entry_decision IN ('skip','shadow_skip')) s
    FROM stampede_alerts WHERE triggered_at > ? GROUP BY h ORDER BY h DESC LIMIT 8""", (since,)).fetchall()
print("alertas por hora (pass/explore/skip):")
for r in rows:
    print(f"  {r['h']}  total={r['n']:4d}  pass={r['p'] or 0:3d} explore={r['e'] or 0:3d} skip={r['s'] or 0:3d}")

# ------------------------------------------------------------- 2) resultados
print("\n", "=" * 72, "\n2) RESULTADOS (posiciones cerradas, sin anomalías de precio)\n", "=" * 72, sep="")
pos = conn.execute("""
    SELECT p.id, p.pnl_usd, p.amount_usd, p.opened_at, p.closed_at, p.entry_price, p.peak_price, p.chain,
           p.token_address, a.entry_decision, a.entry_score, a.triggered_at
    FROM paper_positions p LEFT JOIN stampede_alerts a ON a.id = p.alert_id
    WHERE p.status='closed' AND p.opened_at >= ?""", (since,)).fetchall()
pos = [p for p in pos if p["id"] not in bad]
n = len(pos)
if n == 0:
    print("Todavía no hay posiciones cerradas en este período.")
    sys.exit(0)
wins = [p for p in pos if (p["pnl_usd"] or 0) > 0]
tot = sum(p["pnl_usd"] or 0 for p in pos)
inv = sum(p["amount_usd"] for p in pos)
print(f"cerradas: {n}   ganadoras: {len(wins)} ({len(wins)/n:.1%})   PnL total: ${tot:+.2f}   por trade: ${tot/n:+.2f} ({tot/inv*100:+.2f}% de lo invertido)")

print("\npor día (UTC, según cierre):")
byday = {}
for p in pos:
    d = (p["closed_at"] or "")[:10]
    byday.setdefault(d, []).append(p["pnl_usd"] or 0)
for d in sorted(byday):
    v = byday[d]
    print(f"  {d}: {len(v):4d} trades  win {sum(1 for x in v if x>0)/len(v):5.1%}  PnL ${sum(v):+8.2f}  prom ${sum(v)/len(v):+.2f}")

print("\npor decisión del filtro:")
for dec in ("shadow_pass", "shadow_skip", "pass", "explore", "nofilter", None):
    g = [p for p in pos if p["entry_decision"] == dec]
    if not g:
        continue
    s = sum(p["pnl_usd"] or 0 for p in g)
    w = sum(1 for p in g if (p["pnl_usd"] or 0) > 0)
    print(f"  {str(dec):9s} n={len(g):4d} win {w/len(g):5.1%}  PnL ${s:+8.2f}  prom ${s/len(g):+.2f}")

ids = [p["id"] for p in pos]
ph = ",".join("?" * len(ids))
print("\nrazones de salida:")
for r in conn.execute(f"""SELECT reason, COUNT(*) n, SUM(pnl_usd) s, AVG(pnl_usd) a, AVG(multiplier) m
    FROM paper_position_exits WHERE position_id IN ({ph}) GROUP BY reason ORDER BY n DESC""", ids):
    print(f"  {r['reason']:16s} {r['n']:5d} veces  PnL ${r['s']:+9.2f}  prom ${r['a']:+7.2f}  mult prom {r['m']:.2f}x")

# ------------------------------------- 3) filtro en vivo: pass vs skip por resultado del precio
print("\n", "=" * 72, "\n3) FILTRO EN VIVO -- ¿lo descartado era peor? (mismo criterio para todas las alertas)\n", "=" * 72, sep="")
print("criterio: precio a 30 min vs precio de la alerta (independiente de nuestras salidas)")
for dec in ("shadow_pass", "shadow_skip", "pass", "explore", "skip"):
    al = conn.execute("""SELECT price_at_alert p0, price_after_30m p30, price_after_1h p60 FROM stampede_alerts
        WHERE triggered_at > ? AND entry_decision = ? AND price_at_alert > 0 AND price_after_30m IS NOT NULL""", (since, dec)).fetchall()
    if len(al) < 20:
        print(f"  {dec:8s} muestra chica (n={len(al)})")
        continue
    m = np.array([r["p30"] / r["p0"] for r in al])
    print(f"  {dec:8s} n={len(al):4d}  mediana {np.median(m):.3f}x  +30% o más: {(m>=1.3).mean():5.1%}  cae -20% o peor: {(m<=0.8).mean():5.1%}")

# ---------------------- 4) comportamiento del precio: ganadoras vs perdedoras tras entrar
print("\n", "=" * 72, "\n4) CÓMO SE COMPORTÓ LA MONEDA DESPUÉS DE ENTRAR (ganadoras vs perdedoras)\n", "=" * 72, sep="")


def last_price(txs, t):
    """último precio propio conocido a tiempo <= t"""
    lo = None
    for x in txs:
        if x[0] <= t:
            lo = x[1]
        else:
            break
    return lo


feat = []
for p in pos:
    t0 = datetime.fromisoformat(p["opened_at"])
    txs = conn.execute("""SELECT detected_at, price, side, amount_usd, wallet FROM transactions
        WHERE chain=? AND token_address=? AND detected_at >= ? AND detected_at <= ? AND price IS NOT NULL
        ORDER BY id""", (p["chain"], p["token_address"], p["opened_at"],
                         (t0 + timedelta(seconds=900)).isoformat())).fetchall()
    if len(txs) < 2 or not p["entry_price"]:
        continue
    series = [(datetime.fromisoformat(x["detected_at"]), x["price"]) for x in txs]
    e = p["entry_price"]

    def mult_at(sec):
        pr = None
        for t, px in series:
            if (t - t0).total_seconds() <= sec:
                pr = px
            else:
                break
        return (pr / e) if pr else None

    def flow(a, b):
        buy = sell = 0.0; buyers = set()
        for x in txs:
            dt = (datetime.fromisoformat(x["detected_at"]) - t0).total_seconds()
            if a <= dt < b and x["amount_usd"]:
                if x["side"] == "buy":
                    buy += x["amount_usd"]; buyers.add(x["wallet"])
                else:
                    sell += x["amount_usd"]
        return buy, sell, len(buyers)

    b1, s1, u1 = flow(0, 60)
    peak_mult = (p["peak_price"] / e) if p["peak_price"] else None
    feat.append({
        "win": (p["pnl_usd"] or 0) > 0, "pnl": p["pnl_usd"] or 0,
        "m15": mult_at(15), "m30": mult_at(30), "m60": mult_at(60), "m120": mult_at(120),
        "buy60": b1, "sell60": s1, "buyers60": u1, "sellshare60": s1 / (b1 + s1) if (b1 + s1) else 0,
        "peak": peak_mult,
        "hold_min": ((datetime.fromisoformat(p["closed_at"]) - t0).total_seconds() / 60) if p["closed_at"] else None,
    })

W = [f for f in feat if f["win"]]; L = [f for f in feat if not f["win"]]
print(f"con trayectoria disponible: ganadoras {len(W)}, perdedoras {len(L)}\n")


def med(g, k):
    v = [x[k] for x in g if x[k] is not None]
    return np.median(v) if v else float("nan")


print(f"{'métrica (mediana)':40s} {'GANADORAS':>12s} {'PERDEDORAS':>12s}")
for k, lab, fmt in [("m15", "precio a los 15s vs entrada", "{:.3f}x"), ("m30", "precio a los 30s", "{:.3f}x"),
                    ("m60", "precio a los 60s", "{:.3f}x"), ("m120", "precio a los 2 min", "{:.3f}x"),
                    ("buy60", "compras en el 1er minuto ($)", "${:.0f}"), ("sell60", "ventas en el 1er minuto ($)", "${:.0f}"),
                    ("buyers60", "compradores distintos 1er min", "{:.0f}"), ("sellshare60", "% ventas del volumen 1er min", "{:.1%}"),
                    ("peak", "pico máximo alcanzado", "{:.2f}x"), ("hold_min", "minutos hasta cerrar", "{:.1f}")]:
    print(f"{lab:40s} {fmt.format(med(W, k)):>12s} {fmt.format(med(L, k)):>12s}")

print("\n¿el primer minuto anticipa el resultado? (win-rate según el precio a los 60s vs entrada)")
for lo, hi, lab in [(0, 0.90, "cayó >10%"), (0.90, 0.98, "cayó 2-10%"), (0.98, 1.05, "casi igual"), (1.05, 1.20, "subió 5-20%"), (1.20, 99, "subió >20%")]:
    g = [f for f in feat if f["m60"] is not None and lo <= f["m60"] < hi]
    if len(g) >= 15:
        print(f"  {lab:14s} n={len(g):4d}  win {sum(1 for f in g if f['win'])/len(g):5.1%}  PnL prom ${np.mean([f['pnl'] for f in g]):+.2f}")
conn.close()
