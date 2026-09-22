"""
Pruebas de core/watch_wallets.py (billeteras puntuales compartidas a mano, medidas hacia adelante con el mismo motor del laboratorio de atencion). Correr con: python test_watch_wallets.py
"""
import asyncio
import json
import os
import tempfile

from core import attention_lab as al
from core import watch_wallets as ww

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


W = "FVZRwUp6E4m9jV4VumF8q7m8q3mF9fpikRrJSCCfFAdP"
payload = {"code": 0, "data": {"activities": [
    {"timestamp": 100, "event_type": "buy", "is_open_or_close": 0, "cost_usd": "500", "price_usd": "0.001", "token": {"address": "SoLtOk1", "symbol": "FOXI"}},
    {"timestamp": 90, "event_type": "sell", "is_open_or_close": 1, "cost_usd": "500", "token": {"address": "SoLtOk2"}},
    {"timestamp": 80, "event_type": "buy", "is_open_or_close": 0, "cost_usd": "5", "token": {"address": "SoLtOk3"}},                       # menos del minimo
    {"timestamp": 50, "event_type": "buy", "is_open_or_close": 0, "cost_usd": "200", "token": {"address": "OLD"}}]}}                        # anterior al since: no cuenta

evs, last_ts = ww.parse_wallet_activity(payload, W, "sol", since_ts=60)
check("compras (buy, abre posicion, >= 30 usd) mas nuevas que 'since', chain mapeada a solana", len(evs) == 1 and evs[0]["chain"] == "solana" and evs[0]["token"] == "SoLtOk1" and evs[0]["meta"]["amount_usd"] == 500.0)
check("el ultimo timestamp visto es el mayor de la respuesta (para el cursor), no solo el filtrado", last_ts == 100)
check("sin since: toma todo (primera vez se usa solo para fijar el cursor, no para filtrar aca)", len(ww.parse_wallet_activity(payload, W, "sol", since_ts=None)[0]) == 2)
check("cadena de GMGN no mapeada -> nada", ww.parse_wallet_activity(payload, W, "arc", since_ts=60)[0] == [])

edb = os.path.join(tempfile.mkdtemp(), "e.db"); al.init_db(edb)
wl = os.path.join(tempfile.mkdtemp(), "w.json")
json.dump({"wallets": [{"wallet": W, "gmgn_chain": "sol", "label": "test"}]}, open(wl, "w"))
check("sin clave no hace nada", asyncio.run(ww.poll(None, "", edb, wl)) == 0)
check("lista vacia -> nada", asyncio.run(ww.poll(None, "k", edb, os.path.join(tempfile.mkdtemp(), "no.json"))) == 0)

calls = []


async def fake_get(client, key, path, params):
    calls.append(params["wallet_address"])
    return payload


al._gmgn_get = fake_get
_sl = al.asyncio.sleep


async def _ns(s):
    return None


al.asyncio.sleep = _ns
n1 = asyncio.run(ww.poll(None, "k", edb, wl))
check("la primera pasada solo fija el punto de partida (no registra eventos)", n1 == 0 and calls == [W])
with al.conn_ctx(edb) as c:
    r = c.execute("SELECT v FROM at_state WHERE k=?", (f"watch_last_{W}",)).fetchone()
check("el cursor queda en el ultimo timestamp de la respuesta", r is not None and r["v"] == "100")
n2 = asyncio.run(ww.poll(None, "k", edb, wl))                                                     # misma respuesta: nada mas nuevo que el cursor (100)
check("una segunda pasada sin novedades no agrega eventos", n2 == 0)
al.asyncio.sleep = _sl

with al.conn_ctx(edb) as c:
    c.execute("UPDATE at_state SET v='0' WHERE k=?", (f"watch_last_{W}",))                        # forzar que la proxima pasada vea las compras como nuevas
al.asyncio.sleep = _ns
n3 = asyncio.run(ww.poll(None, "k", edb, wl))
al.asyncio.sleep = _sl
with al.conn_ctx(edb) as c:
    rows = [dict(r) for r in c.execute("SELECT * FROM at_events WHERE source=?", (f"WATCH_{W[:6]}",))]
check("las compras entran al MISMO pipeline de eventos del laboratorio (at_events, fuente por billetera)", n3 == 2 and len(rows) == 2 and {r["token"] for r in rows} == {"SoLtOk1", "OLD"})

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
