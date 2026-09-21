"""
Pruebas del laboratorio de atencion (core/attention_lab.py). Correr con: python test_attention_lab.py
Sin red: respuestas de ejemplo con la forma real de DexScreener / GeckoTerminal / GMGN (segun su documentacion) y una base temporal.
"""
import os
import tempfile

from core import attention_lab as al

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


SOL = "CDAC33JvozJ1UjxBMkvZgJcVXoxdH9iGxeBXUJdXpump"
EVM = "0xAbCdEf0000000000000000000000000000000001"

print("--- costo neto (misma logica que el laboratorio de Solana)")
m = al.net_multiple(1.0, 100_000, 1.0, 100_000, "solana")
check("precio igual, pool grande: pierde solo comisiones+deslizamiento (0.95 a 0.99)", 0.95 < m < 0.99)
check("Robinhood cobra mas comision que Solana (1% vs 0.3%)", al.net_multiple(1.0, 1e6, 1.0, 1e6, "robinhood") < al.net_multiple(1.0, 1e6, 1.0, 1e6, "solana"))
check("precio x2 en pool grande: cerca de 2x menos costos", 1.9 < al.net_multiple(1.0, 1e6, 2.0, 1e6, "solana") < 2.0)
check("pool chico (quote $1500): el deslizamiento se come mas", al.net_multiple(1.0, 1500, 1.0, 1500, "solana") < 0.88)
check("dato faltante -> None", al.net_multiple(1.0, 1e6, None, 1e6) is None)

print("--- parsers de fuentes")
ev = al.parse_dex_list([{"chainId": "solana", "tokenAddress": SOL, "amount": 10, "totalAmount": 500}, {"chainId": "robinhood", "tokenAddress": EVM}, {"chainId": "x"}], "DEX_BOOST")
check("boosts: 2 validos, Solana conserva mayusculas y EVM va en minusculas", len(ev) == 2 and ev[0]["token"] == SOL and ev[1]["token"] == EVM.lower())
check("boosts: guarda el monto en meta", ev[0]["meta"] == {"amount": 10, "totalAmount": 500})
gecko = {"data": [{"id": "solana_POOL1", "attributes": {"reserve_in_usd": "12345", "volume_usd": {"h24": "999"}, "pool_created_at": "2026-09-20T10:00:00Z"},
                   "relationships": {"base_token": {"data": {"id": "solana_" + SOL, "type": "token"}}}},
                  {"id": "x", "relationships": {}}]}
gp = al.parse_gecko_pools(gecko, "solana", "GECKO_TREND")
check("gecko: extrae el token base de 'solana_<mint>'", len(gp) == 1 and gp[0]["token"] == SOL and gp[0]["chain"] == "solana")
check("gecko: eth se mapea a la cadena 'ethereum'", al.parse_gecko_pools({"data": [{"relationships": {"base_token": {"data": {"id": "eth_" + EVM}}}, "attributes": {}}]}, "eth", "GECKO_TREND")[0]["chain"] == "ethereum")
n_ctl = sum(len(al.parse_gecko_pools({"data": [{"relationships": {"base_token": {"data": {"id": "solana_TOK%03d" % i}}}, "attributes": {}} for i in range(200)]}, "solana", "CTL")) for _ in range(1))
check("control: muestrea ~1 de cada 4 (entre 30 y 70 de 200)", 30 <= n_ctl <= 70)
gm = {"code": 0, "data": {"rank": [{"address": EVM, "smart_degen_count": 5, "renowned_count": 1, "rank": 1, "rug_ratio": 0.1}, {"address": "0x" + "2" * 40, "smart_degen_count": 0}]}}
check("GMGN tendencias: 2 items, chain robinhood", [e["chain"] for e in al.parse_gmgn_rank(gm, "robinhood", "GMGN_TREND")] == ["robinhood", "robinhood"])
sm = al.parse_gmgn_rank(gm, "robinhood", "GMGN_SMART", al.GMGN_MIN_SMART)
check("GMGN smart money: solo con >= 3 smart_degen", len(sm) == 1 and sm[0]["meta"]["smart_degen_count"] == 5)
check("GMGN: cadena no soportada -> nada", al.parse_gmgn_rank(gm, "arc", "GMGN_TREND") == [])
gm2 = {"code": 0, "data": {"code": 0, "data": {"rank": [{"chain": "robinhood", "address": EVM, "symbol": "FTD", "smart_degen_count": 4, "liquidity": 17468.6}]}}}
gm3 = {"data": {"data": {"rank": [{"address": EVM, "bundler_rate": 0.32, "top_10_holder_rate": 0.41, "dev_team_hold_rate": 0.05, "sniper_count": 7, "rug_ratio": 0.2, "campo_raro": 1}]}}}
mm = al.parse_gmgn_rank(gm3, "robinhood", "GMGN_TREND")[0]["meta"]
check("GMGN: guarda las senales de 'meme mala' (bundles, top 10, equipo del dev, francotiradores) y no campos ajenos", mm.get("bundler_rate") == 0.32 and mm.get("top_10_holder_rate") == 0.41 and mm.get("dev_team_hold_rate") == 0.05 and mm.get("sniper_count") == 7 and "campo_raro" not in mm)
check("GMGN: respuesta REAL con un nivel de anidado de mas (data.data.rank)", len(al.parse_gmgn_rank(gm2, "robinhood", "GMGN_TREND")) == 1 and len(al.parse_gmgn_rank(gm2, "robinhood", "GMGN_SMART", 3)) == 1)

tr = {"code": 0, "data": {"code": 0, "data": {"list": [
    {"maker": "W1", "side": "buy", "base_address": SOL, "amount_usd": 300, "price_usd": 0.001, "timestamp": 1000, "is_open_or_close": 0, "maker_info": {"twitter_username": "kol1", "tags": ["kol"]}},
    {"maker": "W2", "side": "buy", "base_address": SOL, "amount_usd": 900, "price_usd": 0.002, "timestamp": 1010, "is_open_or_close": 0},
    {"maker": "W3", "side": "sell", "base_address": "VENDIDO", "amount_usd": 500, "timestamp": 1001, "is_open_or_close": 1},
    {"maker": "W4", "side": "buy", "base_address": "CHICO", "amount_usd": 10, "timestamp": 1002, "is_open_or_close": 0},
    {"maker": "W5", "side": "buy", "base_address": "CIERRE", "amount_usd": 500, "timestamp": 1003, "is_open_or_close": 1}]}}}
tk = al.parse_gmgn_track(tr, "sol", "GMGN_KOL", now_s=1060)
check("KOL: solo compras que abren posicion y >= $50, un evento por token (la primera)", len(tk) == 1 and tk[0]["token"] == SOL and tk[0]["meta"]["amount_usd"] == 300)
check("KOL: guarda quien compro, a que precio y hace cuantos segundos", tk[0]["meta"]["twitter"] == "kol1" and tk[0]["meta"]["kol_price_usd"] == 0.001 and tk[0]["meta"]["lag_s"] == 60)
check("KOL: cadena no cubierta (robinhood) -> nada", al.parse_gmgn_track(tr, "robinhood", "GMGN_KOL") == [])
check("KOL: una operacion de hace mas de 10 min se descarta (la lista trae horas de antiguedad)", al.parse_gmgn_track(tr, "sol", "GMGN_KOL", now_s=1611) == [] and len(al.parse_gmgn_track(tr, "sol", "GMGN_KOL", now_s=1599)) == 1)

print("--- enriquecimiento de alertas con la estructura del token (GMGN /v1/token/info)")
ti = {"code": 0, "data": {"code": 0, "data": {"holder_count": 41, "liquidity": "8123.5", "price": {"price": "0.0000312"},
      "stat": {"top_10_holder_rate": 0.44, "dev_team_hold_rate": 0.02, "creator_hold_rate": 0.0, "top_bundler_trader_percentage": 0.31, "top_rat_trader_percentage": 0.05, "fresh_wallet_rate": 0.6},
      "wallet_tags_stat": {"sniper_wallets": 3, "bundler_wallets": 9, "rat_trader_wallets": 1}, "dev": {"creator_open_count": 7, "creator_token_status": "sell"}}}}
pf = al.parse_token_info(ti)
check("token info: extrae concentracion, bundles, francotiradores y el historial del creador (respuesta anidada)", pf and pf["top10"] == 0.44 and pf["bundler_pct"] == 0.31 and pf["sniper_w"] == 3 and pf["creator_open_count"] == 7 and pf["holder_count"] == 41 and pf["price"] == "0.0000312")
check("token info: respuesta sin 'stat' -> None", al.parse_token_info({"code": 0, "data": {"x": 1}}) is None and al.parse_token_info(None) is None)
import sqlite3 as _sq, asyncio as _aio
_hdb = os.path.join(tempfile.mkdtemp(), "h.db"); _c = _sq.connect(_hdb)
_c.execute("CREATE TABLE stampede_alerts (id INTEGER PRIMARY KEY, chain TEXT, token_address TEXT, triggered_at TEXT)")
_c.executemany("INSERT INTO stampede_alerts VALUES (?,?,?,?)", [(1, "robinhood", "0xa1", "2026-09-21T10:00:00+00:00"), (2, "robinhood", "0xa2", "2026-09-21T10:01:00+00:00")]); _c.commit()
_edb = os.path.join(tempfile.mkdtemp(), "e.db"); al.init_db(_edb)
_calls = []
async def _fake(client, key, path, params):
    _calls.append(params["address"])
    if params["address"] == "0xa3": raise RuntimeError("fallo de red")
    return ti
al._gmgn_get = _fake
_sl = al.asyncio.sleep
async def _nosleep(s): return None
al.asyncio.sleep = _nosleep
check("enriquecimiento: la primera pasada solo fija el punto de partida (no rellena el pasado)", _aio.run(al.enrich_alerts(None, "k", _hdb, _edb)) == 0 and _calls == [])
_c.executemany("INSERT INTO stampede_alerts VALUES (?,?,?,?)", [(3, "robinhood", "0xa3", "2026-09-21T10:02:00+00:00"), (4, "robinhood", "0xa4", "2026-09-21T10:03:00+00:00"), (5, "base", "0xb5", "2026-09-21T10:04:00+00:00")]); _c.commit()
check("enriquecimiento: procesa las alertas nuevas de Robinhood (ignora otras cadenas)", _aio.run(al.enrich_alerts(None, "k", _hdb, _edb)) == 2 and _calls == ["0xa3", "0xa4"])
with al.conn_ctx(_edb) as _e:
    _rows = {r["alert_id"]: dict(r) for r in _e.execute("SELECT * FROM at_alert_enrich")}
    _last = _e.execute("SELECT v FROM at_state WHERE k='enrich_last_alert'").fetchone()[0]
check("enriquecimiento: guarda los rasgos con el id de la alerta; un fallo queda como ok=0 y no se reintenta", _rows[4]["ok"] == 1 and '"top10": 0.44' in _rows[4]["feats"] and _rows[3]["ok"] == 0 and "fallo de red" in _rows[3]["note"] and _last == "4")
check("enriquecimiento: sin clave no hace nada", _aio.run(al.enrich_alerts(None, "", _hdb, _edb)) == 0)
al.asyncio.sleep = _sl

print("--- eleccion de par")
pairs = [{"baseToken": {"address": SOL, "symbol": "JW"}, "priceUsd": "0.001", "liquidity": {"usd": 5000}, "pairAddress": "a", "dexId": "raydium", "pairCreatedAt": 1_000_000},
         {"baseToken": {"address": SOL, "symbol": "JW"}, "priceUsd": "0.0011", "liquidity": {"usd": 20000}, "pairAddress": "b", "dexId": "pumpswap", "pairCreatedAt": 2_000_000},
         {"baseToken": {"address": "otro"}, "priceUsd": "1", "liquidity": {"usd": 9e9}}]
p = al.choose_pair(pairs, SOL)
check("elige el par de mayor liquidez del token correcto", p["pair"] == "b" and p["quote_usd"] == 10000)
check("sin par valido -> None", al.choose_pair([], SOL) is None and al.choose_pair([{"baseToken": {"address": SOL}}], SOL) is None)
check("EVM: compara sin importar mayusculas", al.choose_pair([{"baseToken": {"address": EVM}, "priceUsd": "1", "liquidity": {"usd": 4000}}], EVM.lower()) is not None)

print("--- ciclo de base de datos (entrada, rechazo, marcas, SIN DATO)")
tmp = os.path.join(tempfile.mkdtemp(), "t.db")
al.init_db(tmp)
NOW = 10_000_000_000
evs = [{"source": "DEX_BOOST", "chain": "solana", "token": SOL, "meta": {}},
       {"source": "DEX_BOOST", "chain": "solana", "token": "POBRE", "meta": {}},
       {"source": "DEX_BOOST", "chain": "solana", "token": "NOHAYPAR", "meta": {}},
       {"source": "DEX_BOOST", "chain": "solana", "token": SOL, "meta": {}}]
check("registra 3 eventos (el duplicado se ignora)", al.record_events(evs, NOW, tmp) == 3)
check("una segunda pasada no duplica", al.record_events(evs, NOW + 1000, tmp) == 0)
with al.conn_ctx(tmp) as c:
    pend = [dict(r) for r in c.execute("SELECT * FROM at_events")]
pairs_by_key = {("solana", SOL): pairs, ("solana", "POBRE"): [{"baseToken": {"address": "POBRE"}, "priceUsd": "1", "liquidity": {"usd": 1000}}], ("solana", "NOHAYPAR"): []}
al.apply_entries(pend, pairs_by_key, NOW + 30_000, tmp)
with al.conn_ctx(tmp) as c:
    st = {r["token"]: dict(r) for r in c.execute("SELECT * FROM at_events")}
check("con par y liquidez: activo, edad calculada", st[SOL]["status"] == "active" and st[SOL]["entry_price"] == 0.0011 and st[SOL]["age_h"] > 0)
check("liquidez chica: rechazado", st["POBRE"]["status"] == "rejected")
check("sin par y antes de 10 min: sigue pendiente", st["NOHAYPAR"]["status"] == "pending")
al.apply_entries([st["NOHAYPAR"]], {("solana", "NOHAYPAR"): []}, NOW + 700_000, tmp)
with al.conn_ctx(tmp) as c:
    check("sin par tras 10 min: no_price", c.execute("SELECT status FROM at_events WHERE token='NOHAYPAR'").fetchone()[0] == "no_price")
act = [st[SOL]]
due = al.due_marks(act, {}, st[SOL]["entry_ms"] + 4000 * 1000)
check("a los 4000 s vencen las marcas de 5 min, 15 min y 1 h", sorted(h for _, h in due) == [300, 900, 3600])
up = [{"baseToken": {"address": SOL}, "priceUsd": "0.0022", "liquidity": {"usd": 20000}, "pairAddress": "b"}]
al.apply_marks(due, {("solana", SOL): up}, st[SOL]["entry_ms"] + 4000 * 1000, tmp)
al.apply_marks([(st[SOL], 14400)], {("solana", SOL): []}, st[SOL]["entry_ms"] + 14400 * 1000 + 700_000, tmp)
al.apply_marks([(st[SOL], 86400)], {("solana", SOL): None}, st[SOL]["entry_ms"] + 86400 * 1000 + 700_000, tmp)
with al.conn_ctx(tmp) as c:
    mk = {r["horizon_s"]: dict(r) for r in c.execute("SELECT * FROM at_marks")}
    stat = c.execute("SELECT status FROM at_events WHERE token=?", (SOL,)).fetchone()[0]
check("marcas de precio guardadas (3)", all(mk[h]["price"] == 0.0022 for h in (300, 900, 3600)))
check("par desaparecido tras la gracia: SIN DATO", mk[14400]["missed"] == 1 and mk[14400]["price"] is None)
check("fallo de red: no guarda nada (reintenta)", 86400 not in mk)
check("con las 5 marcas pasaria a 'done' (aca 4: sigue activo)", stat == "active")

print("--- estadistica y regla de decision")
rows = al.load_events(tmp)
s = al.event_stats([r for r in rows if r["token"] == SOL], 3600)
check("un evento medido a 1 h con neto ~2x", s["n"] == 1 and 1.8 < s["mean"] < 2.0)
s4 = al.event_stats([r for r in rows if r["token"] == SOL], 14400)
check("SIN DATO: no entra en el neto normal, entra como 0 en el peor caso", s4["n"] == 1 and s4["n_seen"] == 0 and s4["mean"] is None and s4["mean_worst"] == 0.0 and s4["missed"] == 1)
check("poca muestra -> FALTA MUESTRA", al.decide([1.5] * 10, [1.0] * 200, "CTL")[0] == "FALTA MUESTRA")
gan = [1.4 + 0.05 * (i % 5) for i in range(150)]
check("150 ganadores claros contra base plana -> CUMPLE", al.decide(gan, [0.9 + 0.01 * (i % 5) for i in range(150)], "CTL")[0] == "CUMPLE")
check("neto medio <= 1 -> NO CUMPLE", al.decide([0.8 + 0.02 * (i % 5) for i in range(150)], [0.7] * 150 + [0.71], "CTL")[0] == "NO CUMPLE")
check("gana pero no supera a la base -> NO CUMPLE", al.decide(gan, gan, "CTL")[0] == "NO CUMPLE")
check("el reporte corre sobre la base de prueba", "DEX_BOOST" in al.report_text(tmp))
snap = al.get_snapshot(tmp, use_cache=False)
r0 = [r for r in snap["rows"] if r["source"] == "DEX_BOOST"][0]
check("dashboard: snapshot con una fila por fuente/cadena, contadores y veredicto", snap["available"] and snap["total_events"] == 3 and r0["events"] == 3 and r0["entered"] == 1 and r0["rejected"] == 1 and r0["no_price"] == 1 and r0["verdict"] in ("FALTA MUESTRA", "NO CUMPLE"))
check("dashboard: neto por horizonte y ultimos eventos con la ultima medicion", r0["horizons"][3600]["n"] == 1 and 1.8 < r0["horizons"][3600]["mean"] < 2.0 and any(x["last_h"] == 3600 and x["last_net"] > 1.8 for x in snap["recent"]))
check("dashboard: sin base -> disponible False (no rompe)", al.get_snapshot(os.path.join(tempfile.mkdtemp(), "no.db"), use_cache=False) == {"available": False})
import json as _json
check("dashboard: el snapshot se serializa a JSON", len(_json.dumps(snap, default=str)) > 100)
import dashboard as _dash
check("dashboard: la pagina de Memes incluye la pestana Atencion y su script", 'id="mtab-at"' in _dash.HTML_PAGE and "loadAt()" in _dash.HTML_PAGE and 'data-tab="at"' in _dash.HTML_PAGE and "<!--AT_TAB-->" not in _dash.HTML_PAGE)

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
