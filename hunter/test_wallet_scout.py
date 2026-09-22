"""
Pruebas de wallet_scout.py: la parte que agrega candidatas a watch_manual.json sin supervision (--auto-add). No llama a la red. Correr con: python test_wallet_scout.py
"""
import json
import os
import tempfile

import wallet_scout as ws

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


def s(wallet, pnl=0.5, winrate=0.6, created_tokens=0):
    return {"wallet": wallet, "pnl": pnl, "profit_usd": 100.0, "cost_usd": 200.0, "buy": 5, "sell": 5, "winrate": winrate, "tokens": 10, "gt_5x": 1, "big_loss": 0,
            "avg_hold_h": 2.0, "tags": [], "created_tokens": created_tokens, "twitter": None, "twitter_verified": False, "account_age_d": 30}


wl = os.path.join(tempfile.mkdtemp(), "w.json")
json.dump({"wallets": [{"wallet": "YA_ESTABA", "gmgn_chain": "sol", "label": "x", "added_at": "2026-01-01", "note": "n"}]}, open(wl, "w"))
check("already_watched lee el set de billeteras existentes", ws.already_watched(wl) == {"YA_ESTABA"})
check("sin archivo -> vacio (no rompe)", ws.already_watched(os.path.join(tempfile.mkdtemp(), "no.json")) == set())

ranked = [s("YA_ESTABA"), s("NUEVA1"), s("NUEVA2"), s("NUEVA3"), s("NUEVA4", created_tokens=100)]
n = ws.auto_add(ranked, "sol", max_add=2, path=wl)
check("agrega hasta max_add, saltando la que ya estaba", n == 2)
data = json.load(open(wl, encoding="utf-8"))
added = [w["wallet"] for w in data["wallets"][1:]]
check("agrego exactamente NUEVA1 y NUEVA2 (las mejores por orden, sin repetir la que ya estaba)", added == ["NUEVA1", "NUEVA2"])
check("no borro la que ya estaba", data["wallets"][0]["wallet"] == "YA_ESTABA")
check("la nota queda con los numeros reales", "50.0%" in data["wallets"][1]["note"] and "60%" in data["wallets"][1]["note"])

n2 = ws.auto_add(ranked, "sol", max_add=5, path=wl)
check("una segunda pasada no repite las ya agregadas, agrega las que faltaban", n2 == 2 and {w["wallet"] for w in json.load(open(wl, encoding="utf-8"))["wallets"]} == {"YA_ESTABA", "NUEVA1", "NUEVA2", "NUEVA3", "NUEVA4"})
n3 = ws.auto_add(ranked, "sol", max_add=5, path=wl)
check("con todas ya agregadas, la siguiente pasada no agrega nada", n3 == 0)
data2 = json.load(open(wl, encoding="utf-8"))
check("la que crea muchos tokens queda marcada en la nota", "OJO" in [w for w in data2["wallets"] if w["wallet"] == "NUEVA4"][0]["note"])

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
