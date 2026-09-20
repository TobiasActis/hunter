"""
Pruebas de la deteccion del activo de cotizacion de las curvas de Pons (Robinhood Chain).
Correr con: python test_quote_token.py

Contexto (2026-09-20): ~20% de los lanzamientos de la fabrica cotizan en un ERC20 y no en ETH; el listener dividia todo por 1e18 como si fuera ETH y
generaba montos/precios inventados (una venta de $364 millones). Ahora el listener lee el activo de cotizacion del evento TokenLaunched y NO registra
las curvas que no cotizan en ETH (sus trades se descartan como curva desconocida); el activo queda guardado en tokens.quote_token.
"""
import os
import tempfile

from chains import robinhood as rh
from core import db

ETH_QUOTE = "0x" + "0" * 40
ERC20_QUOTE = "0xab093def657f15df31b33922a95e047add645b29"          # el de la transaccion real 0x32d003a3... (bloque 66.681.359)


def word(x: int) -> str:
    return f"{x:064x}"


def launch_log(quote_hex: str) -> dict:
    """TokenLaunched: data = [activo de cotizacion (address), 0, 772956762007610064474252] como en el evento real de la cadena."""
    q = quote_hex[2:].rjust(64, "0")
    return {"data": "0x" + q + word(0) + word(772956762007610064474252), "topics": ["0x0", "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + "22" * 20, "0x" + "0" * 24 + "33" * 20]}


def trade_log(curve: str, topic0: str, quote_raw: int, token_raw: int) -> dict:
    wallet = "0x" + "0" * 24 + "aa" * 20
    if topic0 == rh.CURVE_BUY_TOPIC:
        words = [quote_raw, token_raw, 0, 0]
    else:
        words = [token_raw, quote_raw, 0, 0]
    return {"address": curve, "topics": [topic0, wallet], "data": "0x" + "".join(word(w) for w in words), "transactionHash": "0xdead"}


ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


print("--- lectura del activo de cotizacion del evento de lanzamiento")
check("ETH nativo (address 0) -> ZERO_ADDRESS", rh.launch_quote_token(launch_log(ETH_QUOTE)) == rh.ZERO_ADDRESS)
check("ERC20 real de la cadena", rh.launch_quote_token(launch_log(ERC20_QUOTE)) == ERC20_QUOTE)
check("datos cortos -> None", rh.launch_quote_token({"data": "0x1234"}) is None and rh.launch_quote_token({}) is None)
check("ETH: se rastrea", rh.is_eth_quoted(rh.ZERO_ADDRESS))
check("ERC20: NO se rastrea", not rh.is_eth_quoted(ERC20_QUOTE))
check("desconocido (None): se asume ETH como antes", rh.is_eth_quoted(None))

print("--- el parser de trades solo acepta curvas registradas (las de ETH)")
rh.get_cached_eth_usd = lambda: 2600.0
L = rh.RobinhoodListener()
ETH_CURVE, ERC20_CURVE = "0x" + "e1" * 20, "0x" + "e2" * 20
L._curve_to_token[ETH_CURVE] = "0x" + "b1" * 20                     # solo la curva de ETH quedo registrada (lo que hace el bucle de lanzamientos)
ev = L._parse_trade_log(trade_log(ETH_CURVE, rh.CURVE_SELL_TOPIC, quote_raw=10**18 // 2, token_raw=200_000_000 * 10**18))
check("curva de ETH: se parsea (0.5 ETH = $1300)", ev is not None and abs(ev.amount_usd - 1300.0) < 1e-6)
ev2 = L._parse_trade_log(trade_log(ERC20_CURVE, rh.CURVE_SELL_TOPIC, quote_raw=139_241 * 10**18, token_raw=266_460_976 * 10**18))
check("curva de ERC20 (la venta de '$364 millones'): se descarta", ev2 is None)

print("--- base de datos: quote_token en tokens (migracion + COALESCE)")
tmp = tempfile.mkdtemp(); db.DB_PATH = os.path.join(tmp, "t.db")
db.init_db()
with db.get_conn() as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(tokens)")]
    check("la columna quote_token existe tras init_db (migracion)", "quote_token" in cols)
    db.upsert_token_created(c, "robinhood", "0xt1", "2026-09-20T00:00:00+00:00", creator="0xc", quote_token=ERC20_QUOTE)
    db.upsert_token_created(c, "robinhood", "0xt2", "2026-09-20T00:00:00+00:00", creator="0xc", quote_token=ETH_QUOTE)
    db.upsert_token_created(c, "robinhood", "0xt3", "2026-09-20T00:00:00+00:00")                           # sin dato (otra cadena o llamada vieja)
    db.upsert_token_created(c, "robinhood", "0xt1", "2026-09-21T00:00:00+00:00", quote_token=ETH_QUOTE)    # no pisa el valor ya guardado
    rows = {r["token_address"]: r["quote_token"] for r in c.execute("SELECT token_address, quote_token FROM tokens")}
check("guarda el ERC20", rows["0xt1"] == ERC20_QUOTE)
check("guarda ETH", rows["0xt2"] == ETH_QUOTE)
check("sin dato queda NULL", rows["0xt3"] is None)
check("una segunda escritura no pisa el valor original", rows["0xt1"] == ERC20_QUOTE)

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
