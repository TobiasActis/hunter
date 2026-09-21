"""
Pruebas de core/forward_eval.py (rastro de billeteras y resultado de las corredoras, en papel). Correr con: python test_forward_eval.py
Base sintetica con resultados calculables a mano: la salida amplia B, el seguimiento con retraso, el registro de compras nuevas (rastro y control), la medicion y los veredictos.
"""
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from core import attention_lab as al
from core import forward_eval as fe

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


print("--- salida amplia B (retorno neto, costo 2%)")
check("stop 25%: 1.0 -> 0.7 => -32%", abs(fe.exit_b_return([1.0, 0.7]) - (0.7 - 1 - 0.02)) < 1e-9)
check("corredora: 1.6 (arma) -> 2.2 (50%) -> 3.0 -> 1.9 (cae 37% del maximo) => +103%", abs(fe.exit_b_return([1.0, 1.6, 2.2, 3.0, 1.9]) - (0.5 * 2.2 + 0.5 * 1.9 - 1 - 0.02)) < 1e-9)
check("plana hasta el final de la hora: sale al ultimo precio, solo costo", abs(fe.exit_b_return([1.0, 1.05, 0.95, 1.0]) - (1.0 - 1 - 0.02)) < 1e-9)
check("antes de armarse no hay trailing: 1.4 -> 1.1 no sale, 1.4 -> 0.74 si (stop)", abs(fe.exit_b_return([1.0, 1.4, 1.1]) - (1.1 - 1.02)) < 1e-9 and abs(fe.exit_b_return([1.0, 1.4, 0.74]) - (0.74 - 1.02)) < 1e-9)
t = [0, 3, 6, 10, 20]; p = [1.0, 1.2, 1.5, 3.3, 2.0]
o = fe.follow_outcome(t, p, 0, 5)
check("seguir con retraso: entra en la 1ra operacion con t >= 5 s (precio 1.5), pico 3.3/1.5 = 2.2", o[0] == 1.5 and abs(o[1] - 2.2) < 1e-9)
check("sin operaciones despues del retraso -> None", fe.follow_outcome([0, 1], [1.0, 1.1], 0, 5) is None)
check("la ventana de 1 h corta la trayectoria", fe.follow_outcome([0, 10, 4000], [1.0, 1.0, 9.0], 0, 5)[1] == 1.0)

print("--- rastro: registro de compras nuevas (rastro y control), medicion y veredicto")
T0 = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
iso = lambda s: (T0 + timedelta(seconds=s)).isoformat()
hdb = os.path.join(tempfile.mkdtemp(), "h.db"); c = sqlite3.connect(hdb)
c.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, chain TEXT, token_address TEXT, detected_at TEXT, price REAL, side TEXT, amount_usd REAL, wallet TEXT)")
c.execute("CREATE TABLE tokens (chain TEXT, token_address TEXT, quote_token TEXT)")
c.execute("CREATE TABLE stampede_alerts (id INTEGER PRIMARY KEY, chain TEXT, token_address TEXT, triggered_at TEXT)")
c.execute("INSERT INTO tokens VALUES ('robinhood', '0xbad', '0xerc20')")
def tx(i, s, tok, side, w, px, usd=100.0): c.execute("INSERT INTO transactions VALUES (?, 'robinhood', ?, ?, ?, ?, ?, ?)", (i, tok, iso(s), px, side, usd, w))
# antes de data_until (no cuenta): ids 1-10
for i in range(1, 11): tx(i, -1000 + i, "0xold", "buy", "OLD", 1e-6)
# despues: el rastreado W1 compra 0xa (primera vez), otra vez 0xa (no cuenta), y 0xbad (no-ETH: no cuenta); un ajeno compra con id multiplo de 97 (control)
tx(11, 0, "0xa", "buy", "W1", 1.0e-7)
tx(12, 2, "0xa", "buy", "W1", 1.1e-7)
tx(13, 3, "0xbad", "buy", "W1", 1.0e-7)
tx(14, 5, "0xa", "buy", "X", 1.2e-7)
tx(97, 6, "0xa", "buy", "Y", 1.2e-7)
for i, (s, px) in enumerate([(8, 1.5e-7), (15, 3.4e-7), (30, 2.0e-7)], start=200): tx(i, s, "0xa", "buy", "Z", px)
tx(300, 7000, "0xa", "sell", "Z", 1.0e-7)
c.execute("INSERT INTO stampede_alerts VALUES (1, 'robinhood', '0xa', ?)", (iso(0),))
c.commit()
wl = os.path.join(tempfile.mkdtemp(), "trail.json")
json.dump({"built_at": "2026-09-21", "data_until": iso(-500), "base_rate": 0.078, "wallets": [{"wallet": "W1", "tokens": 30, "hits": 9, "p": 0.3, "lo": 0.16, "lift": 3.8, "med_entry_s": 30, "p25_entry_s": 10, "gap_s": 40}]}, open(wl, "w"))
edb = os.path.join(tempfile.mkdtemp(), "e.db"); al.init_db(edb)
n = fe.track_trail(hdb, edb, wl)
with al.conn_ctx(edb) as e:
    ev = {(r["kind"], r["wallet"], r["token"]): dict(r) for r in e.execute("SELECT * FROM at_trail_events")}
    ptr = e.execute("SELECT v FROM at_state WHERE k='trail_last_tx'").fetchone()[0]
check("arranca despues de data_until y registra solo la 1ra compra del rastreado en un token ETH", ("trail", "W1", "0xa") in ev and ("trail", "W1", "0xbad") not in ev and ("trail", "OLD", "0xold") not in ev and sum(1 for k in ev if k[0] == "trail") == 1)
check("control: 1 de cada 97 compras de otras billeteras (id 97), no las demas", ("control", "Y", "0xa") in ev and ("control", "X", "0xa") not in ev)
check("el punto de partida avanza a la ultima COMPRA procesada (la venta posterior no cuenta)", int(ptr) == 202)
check("una segunda pasada no duplica", fe.track_trail(hdb, edb, wl) == 0)
now = (T0 + timedelta(seconds=5000)).timestamp()
check("no mide antes de 1 h + 2 min", fe.resolve_trail(hdb, edb, now=(T0 + timedelta(seconds=1000)).timestamp()) == 0)
check("mide los eventos vencidos", fe.resolve_trail(hdb, edb, now=now) == 2)
with al.conn_ctx(edb) as e:
    r = dict(e.execute("SELECT * FROM at_trail_events WHERE kind='trail' AND wallet='W1'").fetchone())
check("el rastreado: entrada 5 s despues (1.5e-7... primera operacion >= 5 s), pico 3.4e-7, retorno B con la logica del estudio", r["status"] == "done" and abs(r["entry_price"] - 1.2e-7) < 1e-15 and r["peak"] > 2.5 and r["ret_b"] is not None)
snap = fe.trail_snapshot(edb, wl)
check("snapshot: lista de billeteras, recientes y veredicto FALTA MUESTRA con poca muestra", snap["available"] and snap["verdict"] == "FALTA MUESTRA" and snap["n_wallets"] == 1 and len(snap["recent"]) == 2 and snap["wallets"][0]["f_events"] == 1)
check("el snapshot del rastro se serializa a JSON", len(json.dumps(snap, default=str)) > 100)
with al.conn_ctx(edb) as e:                                                                              # 5 billeteras de la lista compran el MISMO token: cuenta como UN token, no cinco
    for i in range(5):
        e.execute("INSERT INTO at_trail_events (kind, wallet, token, tb, status, entry_price, peak, ret_b, resolved_ms) VALUES ('trail', ?, 'tok-x', ?, 'done', 1.0, 4.0, 2.0, 1)", (f"WW{i}", 1000.0 + i))
    e.execute("INSERT INTO at_trail_events (kind, wallet, token, tb, status, entry_price, peak, ret_b, resolved_ms) VALUES ('trail', 'WW9', 'tok-y', 2000.0, 'done', 1.0, 1.0, -0.3, 1)")
sn2 = fe.trail_snapshot(edb, wl)
check("la unidad es el TOKEN: 6 compras del rastro en 3 tokens (con el de antes) -> n=3, no 7", sn2["stats"]["trail"]["n"] == 3 and sn2["n_events"]["trail"] == 7 and sn2["total"]["trail"] == 3)
check("sin lista -> disponible False", fe.trail_snapshot(edb, os.path.join(tempfile.mkdtemp(), "no.json")) == {"available": False})

print("--- corredoras: medicion de alertas puntuadas")
with al.conn_ctx(edb) as e:
    e.execute("INSERT INTO at_runner_score (alert_id, token, ts_ms, score, top2, top5, feats) VALUES (1, '0xa', 0, 0.5, 1, 1, '{}')")
    e.execute("INSERT INTO at_runner_score (alert_id, token, ts_ms, score, top2, top5, note) VALUES (2, '0xa', 0, NULL, NULL, NULL, 'sin cinta previa')")
check("no mide antes de 1 h + 2 min", fe.resolve_runner(hdb, edb, now=(T0 + timedelta(seconds=1000)).timestamp()) == 0)
check("mide la alerta puntuada (la de sin cinta no)", fe.resolve_runner(hdb, edb, now=now) == 1)
rs = fe.runner_snapshot(edb)
g = {x["key"]: x for x in rs["groups"]}
check("snapshot: grupo top2 con 1 medida, acierto y retorno", g["top2"]["n"] == 1 and g["top2"]["hit"] in (0.0, 1.0) and g["top2"]["ret"] is not None and rs["no_tape"] == 1 and rs["verdict"] == "FALTA MUESTRA")
check("el snapshot de corredoras se serializa a JSON", len(json.dumps(rs, default=str)) > 100)
with al.conn_ctx(edb) as e:                                                                              # veredictos con muestra suficiente
    for i in range(100, 220):
        e.execute("INSERT INTO at_runner_score (alert_id, token, ts_ms, score, top2, top5, feats, peak, ret_b, resolved_ms) VALUES (?, 't', 0, 0.9, 1, 1, '{}', ?, ?, 1)", (i, 3.5 if i % 3 == 0 else 1.0, 1.0 if i % 3 == 0 else -0.05))
rs2 = fe.runner_snapshot(edb)
check("con >= 100 medidas, acierto 33% y retorno positivo con IC95 > 0 -> CUMPLE", rs2["verdict"] == "CUMPLE")
with al.conn_ctx(edb) as e:
    e.execute("UPDATE at_runner_score SET ret_b = -0.1, peak = 1.0 WHERE alert_id >= 100")
check("con >= 100 medidas y perdiendo -> NO CUMPLE", fe.runner_snapshot(edb)["verdict"] == "NO CUMPLE")

import dashboard as _d
check("dashboard: la pagina de Memes incluye las pestanas Corredoras y Rastro con su script", all(x in _d.HTML_PAGE for x in ('id="mtab-rc"', 'id="mtab-tr"', "loadRc()", "loadTr()", 'data-tab="rc"', 'data-tab="tr"')) and "<!--RC_TAB-->" not in _d.HTML_PAGE and "<!--TR_TAB-->" not in _d.HTML_PAGE)

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
