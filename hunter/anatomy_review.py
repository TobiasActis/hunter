"""
Seccion 10 del reporte nocturno: ANATOMIA de las operaciones de memes por version (v5, v6, v7...): cuanto gana y cuanto pierde cada operacion, con que frecuencia, cuanto duran y por que razon cierran.
Sirve para aprender que pasa dentro de las operaciones y mejorar de a poco: cada version cambia UNA cosa y aca se ve si la razon ganancia/perdida, el acierto y la duracion se movieron. Solo lectura.
La razon ganancia/perdida necesaria para equilibrar con un acierto p es (1-p)/p (con 33% de aciertos: 2.05; la v5 tenia 1.44).
"""
import numpy as np

VERSIONS = [("v5", "sim_v5_since"), ("v6", "sim_v6_since"), ("v7", "sim_v7_since")]
BUCKETS = [(-1e9, -20), (-20, -5), (-5, 0), (0, 5), (5, 20), (20, 1e9)]


def _ci(x):
    x = np.asarray(x, float)
    return 1.96 * x.std(ddof=1) / len(x) ** 0.5 if len(x) > 1 else float("nan")


def main(conn):
    print("=" * 72, "\n10) ANATOMIA DE LAS OPERACIONES POR VERSION (cuanto ganamos, cuanto perdemos, cuanto duran)\n", "=" * 72, sep="")
    marks = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM dashboard_settings WHERE key LIKE 'sim_v%_since'")}
    bounds = [(v, marks[k]) for v, k in VERSIONS if marks.get(k)]
    if not bounds:
        print("(sin marcadores de version)")
        return
    for i, (v, since) in enumerate(bounds):
        until = bounds[i + 1][1] if i + 1 < len(bounds) else "9999"
        rows = conn.execute("""SELECT p.id, p.pnl_usd, p.amount_usd, (julianday(p.closed_at)-julianday(p.opened_at))*86400 FROM paper_positions p WHERE p.status='closed' AND p.chain='robinhood'
            AND p.opened_at >= ? AND p.opened_at < ? AND p.id NOT IN (SELECT position_id FROM paper_position_exits WHERE reason LIKE '%anomaly%')""", (since, until)).fetchall()
        if len(rows) < 30:
            print(f"\n{v} (desde {since[:16]} UTC): {len(rows)} operaciones cerradas (muestra chica todavia)")
            continue
        pn = np.array([r[1] or 0 for r in rows]); dur = np.array([r[3] or 0 for r in rows]); ids = [r[0] for r in rows]
        w, l = pn[pn > 0], pn[pn <= 0]
        p = len(w) / len(pn)
        ratio = (w.mean() / -l.mean()) if len(w) and len(l) and l.mean() < 0 else float("nan")
        need = (1 - p) / p if p > 0 else float("nan")
        print(f"\n{v} (desde {since[:16]} UTC): {len(pn)} operaciones | por operacion ${pn.mean():+.2f} +-{_ci(pn):.2f} | gana {p*100:.0f}% | ganancia media +${w.mean():.2f} | perdida media -${-l.mean():.2f} | "
              f"razon {ratio:.2f} (equilibrio {need:.2f})")
        print(f"   duracion: mediana {np.median(dur):.0f} s (p25 {np.percentile(dur, 25):.0f}, p75 {np.percentile(dur, 75):.0f}, p90 {np.percentile(dur, 90):.0f}) | ganadoras mediana {np.median(dur[pn > 0]):.0f} s, perdedoras {np.median(dur[pn <= 0]):.0f} s")
        print("   distribucion del PnL por operacion: " + " | ".join(
            f"{('<' + str(hi)) if lo < -1e8 else (('>' + str(lo)) if hi > 1e8 else f'{lo}..{hi}')}: {((pn > lo) & (pn <= hi)).mean() * 100:.0f}% (${pn[(pn > lo) & (pn <= hi)].sum():+.0f})" for lo, hi in BUCKETS))
        ph = ",".join("?" * len(ids)) if len(ids) < 900 else None
        ex = {}
        q = "SELECT e.reason, e.pnl_usd FROM paper_position_exits e JOIN paper_positions p ON p.id=e.position_id WHERE p.opened_at >= ? AND p.opened_at < ? AND e.reason NOT LIKE '%anomaly%'"
        for reason, pnl in conn.execute(q, (since, until)):
            ex.setdefault(reason, []).append(pnl or 0)
        print("   por razon de salida: " + " | ".join(f"{k} n={len(x)} media ${np.mean(x):+.2f} total ${sum(x):+.0f}" for k, x in sorted(ex.items(), key=lambda kv: -len(kv[1]))))
