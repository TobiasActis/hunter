"""
Lista de billeteras "top por PnL realizado" de Robinhood Chain (idea del articulo "The Complete Robinhood Chain Strategy"), para medirla HACIA ADELANTE como filtro en modo sombra (seccion 8 de daily_review.py).
Correr sobre una COPIA de la base (no en el servidor: carga 1.4M transacciones en memoria):
    python watchlist_tool.py RUTA_COPIA.db data/watch_wallets.json [--n 50] [--min-slices 10]
Regla (fijada con el estudio del 2026-09-20, ranking con datos 13-18 sep y evaluacion 18-20 sep): PnL realizado por FIFO por billetera (unidades = usd/precio; una venta sin compra previa vista no cuenta
como ganancia), minimo 10 rebanadas cerradas, las N de mayor PnL.
(Como filtro sobre nuestras posiciones el estudio dio -2.65 USD (+-2.1) con >=1 billetera de la lista contra -4.72 (+-1.4) sin ninguna: sin significancia.)
SOLO CURVAS EN ETH (2026-09-20): ~20% de los lanzamientos cotizan en otro ERC20 (no en ETH) y sus montos/precios estan mal medidos; se excluyen con tokens.quote_token
(completar el historico con quote_token_tool.py). Con solo curvas en ETH la persistencia de las top-PnL sigue (74% con PnL>0 contra 18-24% del azar) y copiar sus compras sigue dando neto negativo.
LIMPIEZA OBLIGATORIA (2026-09-20): transactions.amount_usd es NO confiable en la cola: el 0.5% de las operaciones sumaba el 98.6% del volumen en USD (una venta de $364 millones; una billetera con $25.000 millones
de volumen en 5 dias). Antes de cualquier suma en USD se descartan las operaciones con precio > 1e-5 ETH/token o monto > $20.000 (1.2% de las operaciones). Con datos limpios: las 50 con mayor PnL siguieron ganando
en el 78% de los casos contra 27-29% del azar (persistencia real), pero copiar sus compras (retraso 15 s) da bruto ~1.00x y NETO -3.6% a -5.8% contra -31% comprando lo de las demas; solo 1 de las 30 de arriba es humana estricta.
El JSON guarda `data_until`: el criterio de evaluacion solo cuenta alertas POSTERIORES.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd


PRICE_MAX = 1e-5
USD_MAX = 20000.0


def build(db, n=50, min_slices=10):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tx = pd.read_sql("SELECT wallet, token_address token, side, amount_usd usd, price, detected_at FROM transactions WHERE chain='robinhood' AND amount_usd>0 AND price>0", c)
    tx["t"] = pd.to_datetime(tx["detected_at"], utc=True, format="ISO8601").astype("int64") / 1e9
    tx = tx[(tx.price <= PRICE_MAX) & (tx.usd <= USD_MAX)].sort_values("t")           # limpieza: ver el docstring
    # Curvas que NO cotizan en ETH (~20% de los lanzamientos): sus montos y precios estan mal medidos -> fuera (usa tokens.quote_token; ver quote_token_tool.py)
    cols = [r[1] for r in c.execute("PRAGMA table_info(tokens)")]
    if "quote_token" in cols:
        bad = {r[0] for r in c.execute("SELECT token_address FROM tokens WHERE chain='robinhood' AND quote_token IS NOT NULL AND quote_token != ?", ("0x" + "0" * 40,))}
        tx = tx[~tx.token.isin(bad)]
    lots, pnl, slices = {}, {}, {}
    for w, tok, side, usd, px in zip(tx.wallet.values, tx.token.values, tx.side.values, tx.usd.values, tx.price.values):
        qty = usd / px
        if side == "buy":
            lots.setdefault((w, tok), []).append([qty, usd / qty])
            continue
        q = lots.get((w, tok)); rem = qty; unit = usd / qty
        while rem > 1e-12 and q:
            lot = q[0]; take = min(lot[0], rem)
            pnl[w] = pnl.get(w, 0.0) + take * (unit - lot[1]); slices[w] = slices.get(w, 0) + 1
            lot[0] -= take; rem -= take
            if lot[0] <= 1e-12:
                q.pop(0)
    rows = sorted(((pnl[w], w, slices[w]) for w in pnl if slices[w] >= min_slices), reverse=True)[:n]
    data_until = datetime.fromtimestamp(float(tx.t.max()), timezone.utc).isoformat()
    return {"built_at": datetime.now(timezone.utc).isoformat(), "data_until": data_until, "n": n, "min_slices": min_slices, "source": "top por PnL realizado FIFO con datos limpios (estudio 2026-09-20)",
            "wallets": [{"wallet": w, "train_pnl_usd": round(p, 2), "slices": s} for p, w, s in rows]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db"); ap.add_argument("out"); ap.add_argument("--n", type=int, default=50); ap.add_argument("--min-slices", type=int, default=10)
    a = ap.parse_args()
    res = build(a.db, a.n, a.min_slices)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print(f"{len(res['wallets'])} billeteras, datos hasta {res['data_until']} -> {a.out}")
    for w in res["wallets"][:5]:
        print("  ", w["wallet"][:12], "PnL ${:,.0f}".format(w["train_pnl_usd"]), "rebanadas", w["slices"])
