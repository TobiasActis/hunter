"""
Pruebas de core/kline_lab.py (recoleccion de velas de tokens migrados). Correr con: python test_kline_lab.py
"""
import asyncio
import json
import os
import tempfile

from core import attention_lab as al
from core import kline_lab as kl

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


payload = {"code": 0, "data": {"code": 0, "data": {"list": [
    {"time": 1000, "open": "1.0", "high": "1.2", "low": "0.9", "close": "1.1", "volume": "500", "amount": "9"},
    {"time": 700, "open": "0.9", "high": "1.0", "low": "0.8", "close": "1.0", "volume": "300", "amount": "5"},
    {"time": 1300000000000, "open": 2, "high": 3, "low": 1, "close": 2, "volume": 10},
    {"time": "malo"}]}}}
cs = kl.parse_klines(payload)
check("velas: respuesta anidada, ordenadas por tiempo, milisegundos -> segundos, se ignora la mal formada", [c[0] for c in cs] == [700, 1000, 1300000000] and cs[1] == (1000, 1.0, 1.2, 0.9, 1.1, 500.0))
check("velas: respuesta vacia o rara -> lista vacia", kl.parse_klines({"data": {}}) == [] and kl.parse_klines(None) == [])

db = os.path.join(tempfile.mkdtemp(), "e.db"); al.init_db(db); kl.init_kline_db(db)
with al.conn_ctx(db) as c:
    for src, ch, tok, mc in (("GMGN_TREND", "robinhood", "0xbig", 900000), ("GMGN_SMART", "robinhood", "0xmid", 150000), ("GMGN_TREND", "robinhood", "0xsmall", 30000), ("GMGN_TREND", "solana", "SOLTOK", 400000)):
        c.execute("INSERT INTO at_events (source, chain, token, first_ms, status, meta) VALUES (?,?,?,0,'active',?)", (src, ch, tok, json.dumps({"market_cap": mc})))
cand = kl.candidate_tokens(db)
check("candidatos: solo capitalizacion >= $100K", set(cand) == {("robinhood", "0xbig"), ("robinhood", "0xmid"), ("solana", "SOLTOK")})
check("pendientes: los de mayor capitalizacion primero", kl.pick_due(db, now=10000, max_n=2) == [("robinhood", "0xbig"), ("solana", "SOLTOK")])

calls = []


async def fake_get(client, key, path, params):
    calls.append((path, params["chain"], params["address"], params["resolution"]))
    if params["address"] == "0xmid":
        raise RuntimeError("fallo de red")
    return payload


_sl = al.asyncio.sleep


async def _ns(s):
    return None


al.asyncio.sleep = _ns
n = asyncio.run(kl.collect(None, "k", db, gmgn_get=fake_get, now=10000))
al.asyncio.sleep = _sl
with al.conn_ctx(db) as c:
    got = {(r["chain"], r["token"]): r["n"] for r in c.execute("SELECT chain, token, COUNT(*) n FROM at_klines GROUP BY 1,2")}
    st = {(r["chain"], r["token"]): dict(r) for r in c.execute("SELECT * FROM at_kline_state")}
check("descarga velas de 5 min con la cadena de GMGN (sol) para los pendientes", n == 3 and all(x[0] == "/v1/market/token_kline" for x in calls) and {x[3] for x in calls} == {"5m", "15m", "1h"} and any(x[1] == "sol" for x in calls))
check("guarda las velas y el estado; un fallo queda anotado y no se reintenta de inmediato", got[("robinhood", "0xbig")] == 9 and st[("robinhood", "0xmid")]["n"] == 0 and "fallo" in st[("robinhood", "0xmid")]["note"] and kl.pick_due(db, now=10100) == [])
check("se vuelve a pedir pasadas 6 h", len(kl.pick_due(db, now=10000 + 6 * 3600 + 5)) == 3)
check("sin clave no hace nada", asyncio.run(kl.collect(None, "", db, gmgn_get=fake_get, now=10000)) == 0)

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
