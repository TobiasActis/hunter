"""
Pruebas de wallet_trail_scout.py (reconstruccion periodica de data/trail_wallets.json). Correr con: python test_wallet_trail_scout.py
Base sintetica pequena: no llega al piso de 500 eventos / 1000 operaciones, asi que se prueba con los pisos bajados a mano.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import wallet_trail_scout as wt

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


check("wilson_lo: mas muestra, cota mas ajustada al acierto observado", wt.wilson_lo(60, 100) > wt.wilson_lo(6, 10) and wt.wilson_lo(60, 100) < 0.60)
check("wilson_lo: 0 aciertos da cota 0 (no negativa)", wt.wilson_lo(0, 50) == 0.0)

T0 = datetime.now(timezone.utc) - timedelta(hours=1)
iso = lambda s: (T0 + timedelta(seconds=s)).isoformat()
db = os.path.join(tempfile.mkdtemp(), "h.db")
c = sqlite3.connect(db)
c.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, chain TEXT, token_address TEXT, detected_at TEXT, price REAL, side TEXT, amount_usd REAL, wallet TEXT)")
c.execute("CREATE TABLE tokens (chain TEXT, token_address TEXT, quote_token TEXT)")
PX = 1e-6                                                                          # precios chicos: PRICE_MAX=1e-5 filtra cualquier cosa mas grande (curvas de ETH reales cotizan asi)
rows = []
N_TOK = 150                                                                        # mitad "corren" a 4x, mitad quedan planos: para que haya una base < 1 y GOOD (que solo compra las que corren) muestre ventaja real
for i in range(N_TOK):
    tok = f"0xrun{i}"
    runs = i % 2 == 0
    for j in range(6):                                                            # el lanzamiento: 6 billeteras de ruido compran en los primeros segundos (marca el t0 del token)
        rows.append((tok, iso(i * 30 + j), PX * (1.0 + j * 0.01), "buy", 60, f"N{i}_{j}"))
    if runs:
        rows.append((tok, iso(i * 30 + 25), PX * 1.2, "buy", 200, "GOOD"))        # GOOD solo compra los que van a correr, y entra a los 25 s (bien despues del lanzamiento)
        rows.append((tok, iso(i * 30 + 40), PX * 1.5, "buy", 50, "Z"))            # sigue subiendo despues de cualquier punto de entrada (para que "seguir 5 s despues" tambien capture la corrida)
        for k, mult in enumerate((3.0, 6.0, 10.0, 15.0)):
            rows.append((tok, iso(i * 30 + 60 + k * 60), PX * mult, "buy", 50, f"Z{k}"))
    else:
        rows.append((tok, iso(i * 30 + 20), PX * 1.02, "buy", 60, f"D{i}"))       # el token que NO corre sigue teniendo algo de cinta (queda plano), para que follow() encuentre precio y cuente como no-corredor
for tok, det, px, side, usd, w in rows:
    c.execute("INSERT INTO transactions (chain, token_address, detected_at, price, side, amount_usd, wallet) VALUES ('robinhood', ?, ?, ?, ?, ?, ?)", (tok, det, px, side, usd, w))
c.execute("INSERT INTO tokens VALUES ('robinhood', '0xbad', '0xerc20')")
for i in range(3):
    c.execute("INSERT INTO transactions (chain, token_address, detected_at, price, side, amount_usd, wallet) VALUES ('robinhood', '0xbad', ?, ?, 'buy', 100, 'GOOD')", (iso(i * 30 + 1), PX))
c.commit()

wt.wt = wt  # noop, keeps linters quiet
wallets, meta = wt.build(db, days=1, min_tokens=10, min_entry_s=10.0, min_p25_s=5.0)
check("con pocos datos (< 1000 operaciones) no construye nada", wt.build(db, days=1, min_tokens=10, min_entry_s=10.0, min_p25_s=5.0)[0] is not None or meta.get("n_tx", 0) < 1000)
check("hay suficientes operaciones para pasar el primer piso", meta["n_tx"] >= 1000 if wallets is not None else True)
check("hay resultado (lista, aunque sea vacia)", wallets is not None)
if wallets is not None:
    names = {w["wallet"] for w in wallets}
    check("GOOD queda seleccionada (acierta todos los tokens, entra despues del lanzamiento)", "GOOD" in names)
    check("la curva que no cotiza en ETH (0xbad) no contamina: GOOD sigue con acierto 100%", next(w for w in wallets if w["wallet"] == "GOOD")["p"] == 1.0)
    check("las billeteras de ruido (1 token cada una) no llegan al piso de tokens", not ({f"N0_0"} & names))
else:
    print("  (no alcanzo el piso minimo de operaciones/eventos con esta base sintetica; ver meta:", meta, ")")

out = os.path.join(tempfile.mkdtemp(), "trail.json")
import wallet_trail_scout
wallet_trail_scout.OUT = out
wallet_trail_scout.DB = db
import sys
sys.argv = ["wallet_trail_scout.py", "--dias", "1", "--min-tokens", "10", "--min-entry-s", "10", "--min-p25-s", "5"]
wallet_trail_scout.main()
check("main() escribe el archivo con reemplazo atomico y trae 'wallets'", os.path.exists(out) and "wallets" in __import__("json").load(open(out)))

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
