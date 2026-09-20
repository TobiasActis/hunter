"""
Replay contrafactual de reglas de salida sobre la trayectoria REAL de cada posicion
(transacciones propias), con el mismo modelo de costos que la simulacion (comision de
Robinhood, slippage de curva, latencia de salida). Solo lectura.

Es la version reutilizable de la logica de backtest_study.py; la usa daily_review.py para
medir cada noche, sobre posiciones NUEVAS, ideas que en el backtest historico se vieron
prometedoras (ver salida por presion de venta abajo).

Los niveles absolutos del backtest son optimistas (~7pp, precios congelados tras la
graduacion de la curva): solo se reportan DIFERENCIAS contra las reglas actuales.
"""
from datetime import datetime, timedelta

import numpy as np

from core.ev_calculator import cost_of_trade, exit_cost
from core.paper_trading import fees_for
from core.slippage import ROBINHOOD_VIRTUAL_RESERVE_USD, MIN_DEPTH_FRACTION, price_factor

FEES = fees_for("robinhood")
EXIT_LAT = 1.5


def load_positions(conn, since):
    bad = {r[0] for r in conn.execute(
        "SELECT DISTINCT position_id FROM paper_position_exits WHERE reason LIKE '%_price_anomaly'")}
    rows = conn.execute("""SELECT id, pnl_usd, amount_usd, entry_price, opened_at, token_address, chain
        FROM paper_positions WHERE status='closed' AND opened_at >= ? AND chain='robinhood'
        ORDER BY opened_at""", (since,)).fetchall()
    out = []
    for r in rows:
        if r["id"] in bad or not r["entry_price"]:
            continue
        t0 = datetime.fromisoformat(r["opened_at"])
        txs = conn.execute("""SELECT detected_at, price, side, amount_usd, wallet FROM transactions
            WHERE chain=? AND token_address=? AND detected_at >= ? AND detected_at <= ? AND price IS NOT NULL
            ORDER BY id""", (r["chain"], r["token_address"], r["opened_at"],
                             (t0 + timedelta(seconds=3700)).isoformat())).fetchall()
        net0 = 0.0
        for x in conn.execute("""SELECT side, SUM(amount_usd) s FROM transactions
                                 WHERE chain=? AND token_address=? AND detected_at < ? AND amount_usd IS NOT NULL
                                 GROUP BY side""", (r["chain"], r["token_address"], r["opened_at"])):
            net0 += (x["s"] or 0) if x["side"] == "buy" else -(x["s"] or 0)
        ts = np.array([(datetime.fromisoformat(x["detected_at"]) - t0).total_seconds() for x in txs])
        px = np.array([x["price"] for x in txs])
        vol = np.array([(x["amount_usd"] or 0) * (1 if x["side"] == "buy" else -1) for x in txs])
        sells = [(t, x["wallet"]) for t, x in zip(ts, txs) if x["side"] == "sell"]
        out.append({"id": r["id"], "pnl": r["pnl_usd"] or 0, "amount": r["amount_usd"], "entry": r["entry_price"],
                    "ts": ts, "px": px, "cumnet": net0 + np.cumsum(vol) if len(vol) else np.array([]),
                    "net0": net0, "sells": sells})
    return out


def _price_at(P, t):
    i = np.searchsorted(P["ts"], t, side="right") - 1
    return (P["px"][i], i) if i >= 0 else (None, -1)


def _sell_value(P, amount, frac, t_decide, spot_hint):
    fill, i = _price_at(P, t_decide + EXIT_LAT)
    spot = fill if fill is not None else spot_hint
    net = P["cumnet"][i] if i >= 0 and len(P["cumnet"]) else P["net0"]
    depth = max(ROBINHOOD_VIRTUAL_RESERVE_USD + net, ROBINHOOD_VIRTUAL_RESERVE_USD * MIN_DEPTH_FRACTION)
    eff = amount - cost_of_trade(amount, FEES)
    sale_usd = amount * frac * (spot / P["entry"])
    mult = spot * price_factor(sale_usd, depth, "sell") / P["entry"]
    gross = eff * frac * mult
    return (gross - exit_cost(gross, FEES)) - amount * frac


def simulate(P, interval=5.0, stop=0.15, tps=((1.60, 0.40), (2.20, 0.5)), trail=0.30, hold=600.0, force_t=None):        # v5: stop 15%, tenencia 10 min
    """PnL de la posicion con las reglas de salida vigentes; `force_t` agrega una salida
    total forzada a partir de ese segundo (contrafactual)."""
    remaining = 1.0; pnl = 0.0; peak = P["entry"]; tiers_done = 0
    t = interval
    while t <= hold + 1e-9 and remaining > 1e-9:
        spot, _ = _price_at(P, t)
        if spot is None:
            t += interval; continue
        peak = max(peak, spot)
        mult = spot / P["entry"]
        if t >= hold - 1e-9:
            pnl += _sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        if force_t is not None and t >= force_t:
            pnl += _sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        if 1 - mult >= stop:
            pnl += _sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        if tiers_done < len(tps) and mult >= tps[tiers_done][0]:
            frac = tps[tiers_done][1] * remaining
            pnl += _sell_value(P, P["amount"], frac, t, spot); remaining -= frac; tiers_done += 1
        elif tiers_done == len(tps) and (peak - spot) / peak >= trail:
            pnl += _sell_value(P, P["amount"], remaining, t, spot); remaining = 0; break
        t += interval
    if remaining > 1e-9:
        spot, _ = _price_at(P, hold)
        pnl += _sell_value(P, P["amount"], remaining, hold, spot or P["entry"])
    return pnl


def kth_distinct_seller_time(P, k):
    """Segundo (desde que abrimos) en que vende el k-esimo comprador distinto, o None."""
    seen = set()
    for t, w in P["sells"]:
        seen.add(w)
        if len(seen) >= k:
            return t
    return None
