"""
Backtest de reglas de salida sobre la trayectoria REAL de precios de cada
posición (transacciones propias), con el mismo modelo de costos de la
simulación (comisión de Robinhood, slippage de curva, latencia).

1) Valida el backtester: con las reglas actuales (revisión cada 60s) debe
   reproducir aproximadamente el PnL real.
2) Prueba mejoras con lógica mecánica clara (no ajustadas a mano):
   - revisar el precio cada pocos segundos en vez de cada 60s
   - salida temprana si no hay seguimiento en el primer minuto
   - no entrar cuando el precio ya se disparó antes de llenar
3) Cada variante se reporta en la 1ra y 2da mitad del período por separado,
   para ver si la mejora es consistente y no ruido.

Uso: ./venv/bin/python backtest_study.py
"""
import sqlite3
from datetime import datetime, timedelta

import numpy as np

from core.ev_calculator import cost_of_trade, exit_cost
from core.paper_trading import fees_for
from core.slippage import ROBINHOOD_VIRTUAL_RESERVE_USD, MIN_DEPTH_FRACTION, price_factor

conn = sqlite3.connect("data/hunter.db")
conn.row_factory = sqlite3.Row
SINCE = conn.execute("SELECT value FROM dashboard_settings WHERE key='sim_v2_since'").fetchone()[0]
FEES = fees_for("robinhood")
EXIT_LAT = 1.5


def load_positions():
    bad = {r[0] for r in conn.execute("SELECT DISTINCT position_id FROM paper_position_exits WHERE reason LIKE '%_price_anomaly'")}
    rows = conn.execute("""SELECT p.id, p.pnl_usd, p.amount_usd, p.entry_price, p.opened_at, p.token_address, p.chain, a.price_at_alert
        FROM paper_positions p LEFT JOIN stampede_alerts a ON a.id=p.alert_id
        WHERE p.status='closed' AND p.opened_at >= ? AND p.chain='robinhood'""", (SINCE,)).fetchall()
    out = []
    for r in rows:
        if r["id"] in bad or not r["entry_price"]:
            continue
        t0 = datetime.fromisoformat(r["opened_at"])
        txs = conn.execute("""SELECT detected_at, price, side, amount_usd FROM transactions
            WHERE chain=? AND token_address=? AND detected_at >= ? AND detected_at <= ? AND price IS NOT NULL ORDER BY id""",
            (r["chain"], r["token_address"], r["opened_at"], (t0 + timedelta(seconds=3700)).isoformat())).fetchall()
        net0 = 0.0
        for x in conn.execute("""SELECT side, SUM(amount_usd) s FROM transactions WHERE chain=? AND token_address=? AND detected_at < ?
                                 AND amount_usd IS NOT NULL GROUP BY side""", (r["chain"], r["token_address"], r["opened_at"])):
            net0 += (x["s"] or 0) if x["side"] == "buy" else -(x["s"] or 0)
        ts = np.array([(datetime.fromisoformat(x["detected_at"]) - t0).total_seconds() for x in txs])
        px = np.array([x["price"] for x in txs])
        vol = np.array([(x["amount_usd"] or 0) * (1 if x["side"] == "buy" else -1) for x in txs])
        out.append({"id": r["id"], "pnl": r["pnl_usd"] or 0, "amount": r["amount_usd"], "entry": r["entry_price"],
                    "alert_price": r["price_at_alert"], "ts": ts, "px": px, "cumnet": net0 + np.cumsum(vol) if len(vol) else np.array([]),
                    "net0": net0, "opened_at": r["opened_at"]})
    return out


def price_at(P, t):
    i = np.searchsorted(P["ts"], t, side="right") - 1
    return (P["px"][i], i) if i >= 0 else (None, -1)


def sell_value(P, amount, frac, t_decide, spot_hint):
    """PnL de vender `frac` del original decidiendo en t_decide (llena EXIT_LAT después, con slippage de curva)."""
    fill, i = price_at(P, t_decide + EXIT_LAT)
    spot = fill if fill is not None else spot_hint
    net = P["cumnet"][i] if i >= 0 and len(P["cumnet"]) else P["net0"]
    depth = max(ROBINHOOD_VIRTUAL_RESERVE_USD + net, ROBINHOOD_VIRTUAL_RESERVE_USD * MIN_DEPTH_FRACTION)
    eff = amount - cost_of_trade(amount, FEES)
    sale_usd = amount * frac * (spot / P["entry"])
    mult = spot * price_factor(sale_usd, depth, "sell") / P["entry"]
    gross = eff * frac * mult
    return (gross - exit_cost(gross, FEES)) - amount * frac


def simulate(P, interval=60.0, stop=0.20, tps=((1.60, 0.40), (2.20, 0.5)), trail=0.30, hold=3600.0,
             early_t=None, early_mult=None):
    remaining = 1.0; pnl = 0.0; peak = P["entry"]; tiers_done = 0
    t = interval
    while t <= hold + 1e-9 and remaining > 1e-9:
        spot, _ = price_at(P, t)
        if spot is None:
            t += interval; continue
        peak = max(peak, spot)
        mult = spot / P["entry"]
        if t >= hold - 1e-9:
            pnl += sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        if 1 - mult >= stop:
            pnl += sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        if early_t is not None and tiers_done == 0 and t >= early_t and mult < early_mult and t < early_t + max(interval, 30):
            pnl += sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        if tiers_done < len(tps) and mult >= tps[tiers_done][0]:
            frac = tps[tiers_done][1] * remaining
            pnl += sell_value(P, P["amount"], frac, t, spot); remaining -= frac; tiers_done += 1
        elif tiers_done == len(tps) and (peak - spot) / peak >= trail:
            pnl += sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        t += interval
    if remaining > 1e-9:  # sin precios hasta el final: cierra al último conocido
        spot, _ = price_at(P, hold)
        pnl += sell_value(P, P["amount"], remaining, hold, spot or P["entry"])
    return pnl


print("cargando trayectorias...")
POS = load_positions()
print(f"posiciones de la simulación realista con trayectoria: {len(POS)}\n")
half = len(POS) // 2


def report(name, fn, subset=None):
    S = POS if subset is None else [p for p in POS if subset(p)]
    res = [(p["id"], fn(p)) for p in S]
    allp = np.array([r for _, r in res])
    ids = [i for i, _ in res]
    h1 = np.array([r for (i, r) in res if i in {p["id"] for p in POS[:half]}])
    h2 = np.array([r for (i, r) in res if i in {p["id"] for p in POS[half:]}])
    inv = sum(p["amount"] for p in S)
    print(f"{name:52s} n={len(allp):4d} PnL ${allp.sum():+9.0f} ({allp.sum()/inv*100:+6.2f}%)  win {(allp>0).mean():5.1%} | 1ra mitad ${h1.mean():+6.2f}/trade  2da mitad ${h2.mean():+6.2f}/trade")
    return allp


print("--- 1) VALIDACIÓN del backtester (debe parecerse al PnL real) ---")
real = np.array([p["pnl"] for p in POS])
print(f"{'PnL REAL':52s} n={len(real):4d} PnL ${real.sum():+9.0f} ({real.sum()/sum(p['amount'] for p in POS)*100:+6.2f}%)  win {(real>0).mean():5.1%}")
report("backtest reglas actuales (revisión cada 60s)", lambda p: simulate(p, interval=60))

print("\n--- 2) MEJORAS ---")
for iv in (30, 10, 5, 2):
    report(f"revisar cada {iv}s", lambda p, iv=iv: simulate(p, interval=iv))
print()
report("cada 5s + salida temprana (45s, si <0.92x)", lambda p: simulate(p, interval=5, early_t=45, early_mult=0.92))
report("cada 5s + salida temprana (30s, si <0.95x)", lambda p: simulate(p, interval=5, early_t=30, early_mult=0.95))
report("cada 5s + salida temprana (60s, si <0.90x)", lambda p: simulate(p, interval=5, early_t=60, early_mult=0.90))
report("cada 5s + stop -15%", lambda p: simulate(p, interval=5, stop=0.15))
report("cada 5s + stop -25%", lambda p: simulate(p, interval=5, stop=0.25))
print()
late = lambda p: p["alert_price"] and p["entry"] / p["alert_price"] > 1.15
print(f"posiciones que llenaron >15% arriba del precio de la alerta: {sum(1 for p in POS if late(p))}")
report("cada 5s, SOLO entradas 'no tardías' (llenado <=1.15x alerta)", lambda p: simulate(p, interval=5), subset=lambda p: not late(p))
report("cada 5s, SOLO entradas 'tardías' (llenado >1.15x alerta)", lambda p: simulate(p, interval=5), subset=late)
conn.close()
