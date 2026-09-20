"""
Tablero de estrategias cripto en PAPEL (solo lectura): una fila por estrategia con lo medido hasta hoy, el criterio FIJADO DE ANTEMANO para creerle y su estado.
Se imprime al final del reporte nocturno (daily_review.py) y se puede correr solo:   ./venv/bin/python crypto_review.py

Criterios (no se cambian mirando resultados; ver memoria del proyecto):
  Momentum semanal y Funding semanal : >= 12 semanas cerradas con media semanal > 0 y >= 50% de semanas ganadoras -> CUMPLE (candidata a una prueba real chica hecha por el dueno);
                                       >= 12 semanas con media <= 0 -> DESCARTAR.
  Laboratorio de scalping (por fila)  : >= 100 operaciones cerradas; R neto con IC95 > 0 y mayor que el del CONTROL -> CUMPLE; R neto con IC95 < 0 -> DESCARTAR (el bruto no cubre costos).
  Cerebro (ML)                        : >= 500 senales por (activo, horizonte) con neto a costo taker > 0 -> CUMPLE; >= 500 senales con neto <= 0 -> DESCARTAR.
  Motor CRT                           : >= 100 operaciones; R medio < 0 -> DESCARTAR (el backtest ya decia negativo).
"""
import sys
from datetime import datetime, timezone

WEEKS_GATE = 12
LAB_GATE = 100
BRAIN_GATE = 500
CRT_GATE = 100


# ------------------------------------------------------------------ criterios (puros, testeables)
def weekly_gate(n_weeks, avg_week_pct, win_weeks):
    if n_weeks < WEEKS_GATE:
        return f"RECOLECTANDO ({n_weeks} de {WEEKS_GATE} semanas)"
    if avg_week_pct is not None and avg_week_pct > 0 and win_weeks / n_weeks >= 0.5:
        return "CUMPLE: candidata a prueba real chica"
    return "DESCARTAR: media semanal <= 0 o menos de 50% de semanas ganadoras"


def lab_gate(v, control):
    n = v["n"]
    if n < LAB_GATE:
        return f"RECOLECTANDO ({n} de {LAB_GATE} operaciones)"
    lo, hi = v["avg_r_net"] - v["ci95"], v["avg_r_net"] + v["ci95"]
    ctl = control["avg_r_net"] if control and control["n"] >= 30 else None
    if lo > 0 and (ctl is None or v["avg_r_net"] > ctl):
        return "CUMPLE: R neto > 0 y mejor que el control"
    if hi < 0:
        return "DESCARTAR: el R bruto no cubre los costos"
    return "SIN DIFERENCIA con cero: seguir midiendo"


def brain_gate(row):
    n = row.get("n_signals") or 0
    net = row.get("net_taker_bps")
    if n < BRAIN_GATE or net is None:
        return f"RECOLECTANDO ({n} de {BRAIN_GATE} senales)"
    return "CUMPLE: neto taker > 0" if net > 0 else "DESCARTAR: neto taker <= 0"


def crt_gate(total):
    if total["n"] < CRT_GATE:
        return f"RECOLECTANDO ({total['n']} de {CRT_GATE} operaciones)"
    return "DESCARTAR: R medio < 0" if total["avg_r"] < 0 else "SIN DIFERENCIA con cero: seguir midiendo"


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def rf(x):
    return "--" if x is None else f"{x:+.2f}R"


# ------------------------------------------------------------------ reporte
def main():
    print("=" * 72, "\n7) TABLERO DE ESTRATEGIAS CRIPTO (PAPEL)  ", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "\n", "=" * 72, sep="")
    lines = []
    try:
        from core.xs_momentum import get_snapshot as xs_snap
        x = xs_snap()
        avg = x["avg_week_pct"]
        avg_s = "--" if avg is None else "%+.2f%%" % avg
        sc = x.get("scale")
        sc_s = "--" if sc is None else "%.0f%%" % (sc * 100)
        lines.append(("Momentum semanal", "capital $%.2f (inicial $%.0f) | PnL %s | semanas %d (%d ganadoras) | media %s/sem | exposicion %s | stops %d TP %d"
                      % (x["equity"], x["config"]["bankroll"], money(x["realized"] + x["unrealized"]), x["n_weeks"], x["win_weeks"], avg_s, sc_s, x["n_stop"], x["n_tp"]),
                      weekly_gate(x["n_weeks"], avg, x["win_weeks"])))
    except Exception as e:
        lines.append(("Momentum semanal", f"(sin datos: {e})", "?"))
    try:
        from core.xs_funding import get_snapshot as fd_snap
        f = fd_snap()
        avg = f["avg_week_pct"]; d = f["decomp"]
        avg_s = "--" if avg is None else "%+.2f%%" % avg
        lines.append(("Funding semanal", "capital $%.2f | PnL %s (precio %s, funding %s, comisiones %s) | semanas %d (%d ganadoras) | media %s/sem | stops %d%s"
                      % (f["equity"], money(f["realized"] + f["unrealized"]), money(d["price"]), money(d["funding"]), money(d["fees"]), f["n_weeks"], f["win_weeks"], avg_s, f["n_stop"],
                         "" if f["last_rebalance"] else " | primera cartera: proximo lunes 00:10 UTC"),
                      weekly_gate(f["n_weeks"], avg, f["win_weeks"])))
    except Exception as e:
        lines.append(("Funding semanal", f"(sin datos: {e})", "?"))
    try:
        from core.scalp_lab import get_snapshot as lab_snap
        lab = lab_snap(); ctl = next((v for v in lab["variants"] if v["variant"] == "CONTROL"), None)
        for v in lab["variants"]:
            if v["n"] == 0:
                res = f"0 cerradas | {v['open']} abiertas, {v['pending']} pendientes"
            else:
                ci_s = "" if v["ci95"] is None else " +-%.2f" % v["ci95"]
                res = ("%d cerradas | win %.0f%% | R bruto %s | costo -%.2fR | R neto %s%s | PnL %s | %d abiertas, %d pendientes"
                       % (v["n"], v["wins"] / v["n"] * 100, rf(v["avg_r_gross"]), v["cost_r"], rf(v["avg_r_net"]), ci_s, money(v["pnl"]), v["open"], v["pending"]))
            lines.append((f"Lab scalping: {v['name']}", res, "(referencia)" if v["variant"] == "CONTROL" else lab_gate(v, ctl) if v["avg_r_net"] is not None and v["ci95"] is not None else f"RECOLECTANDO ({v['n']} de {LAB_GATE} operaciones)"))
    except Exception as e:
        lines.append(("Lab scalping", f"(sin datos: {e})", "?"))
    try:
        from core.market_brain import get_snapshot as br_snap
        b = br_snap()
        for r in b["rows"]:
            if r.get("n_signals") is None:
                res = f"{r['n_scored']} predicciones evaluadas"
            else:
                auc_s = "--" if r.get("auc") is None else "%.3f" % r["auc"]
                extra = "" if r.get("gross_bps") is None else " | bruto %+.1f bps, neto taker %+.1f, neto maker %+.1f" % (r["gross_bps"], r["net_taker_bps"], r["net_maker_bps"])
                res = "%d evaluadas | AUC %s | acierta %.0f%% | %d senales%s" % (r["n_scored"], auc_s, r["dir_acc"] * 100, r["n_signals"], extra)
            lines.append((f"Cerebro {r['symbol']} {r['horizon']}h", res, brain_gate(r)))
    except Exception as e:
        lines.append(("Cerebro", f"(sin datos: {e})", "?"))
    try:
        from core.scalper import get_snapshot as crt_snap
        s = crt_snap(); t = s["total"]
        lines.append(("Motor CRT", "%d cerradas | win %.0f%% | R medio %+.2fR | PnL %s | %d abiertas" % (t["n"], (t["wins"] / t["n"] * 100 if t["n"] else 0), t["avg_r"], money(t["pnl"]), len(s["open"])), crt_gate(t)))
    except Exception as e:
        lines.append(("Motor CRT", f"(sin datos: {e})", "?"))
    for name, res, gate in lines:
        print(f"- {name}\n    {res}\n    -> {gate}")
    print("\n(Memecoins: ver secciones 1-6 de este reporte; v4 medida el 2026-09-20: -3.9% por posicion con IC95 negativo, se deja como control.)")
    print("Regla: nada pasa a una prueba real hasta cumplir su criterio; la prueba real la hace el dueno, sin apalancamiento y con dinero que pueda perder.")


if __name__ == "__main__":
    main()
