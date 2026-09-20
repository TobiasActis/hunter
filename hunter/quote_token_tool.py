"""
Activo de cotizacion (quote) de cada token de Pons en Robinhood Chain: lo lee del evento TokenLaunched de la fabrica y lo guarda en tokens.quote_token.
El listener nuevo (chains/robinhood.py) ya lo guarda para los lanzamientos NUEVOS y no procesa las curvas que no cotizan en ETH; esta herramienta sirve para
completar el HISTORICO (y para analisis sobre una copia de la base):

    python quote_token_tool.py scan  OUT.json [--blocks 4000000]      # recorre la cadena (RPC publico) y arma {token: activo_de_cotizacion}
    python quote_token_tool.py apply RUTA.db OUT.json                  # UPDATE tokens.quote_token (solo donde esta vacio); NO toca nada mas
    python quote_token_tool.py stats RUTA.db                            # cuantas alertas / posiciones caen en curvas que no cotizan en ETH

Hallazgo (2026-09-20, 56.465 lanzamientos): ~80% cotizan en ETH (address 0) y ~20% en mas de 10 ERC20 distintos. El 100% de las operaciones 'absurdas' (precio > 1e-5 o
monto > $20.000) eran de curvas que no cotizan en ETH, y el 94% de las operaciones de esas curvas parecen normales pero estan mal medidas.
"""
import json
import re
import sqlite3
import sys
import time
import urllib.request

ZERO = "0x" + "0" * 40
RPC = "https://rpc.mainnet.chain.robinhood.com"
FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
TOKEN_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"


def rpc(method, params, tries=4):
    err = None
    for k in range(tries):
        try:
            r = urllib.request.Request(RPC, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                       headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(r, timeout=60).read())
        except Exception as e:                                   # noqa: BLE001
            err = e
            time.sleep(2 * (k + 1))
    raise err


def scan(out, blocks=4_000_000):
    head = int(rpc("eth_blockNumber", [])["result"], 16)
    b, step, res = head - blocks, 20000, {}
    while b <= head:
        to = min(b + step - 1, head)
        d = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to), "address": FACTORY, "topics": [TOKEN_LAUNCHED]}])
        if "error" in d:
            step = max(step // 2, 2000)
            continue
        for l in d["result"]:
            res["0x" + l["topics"][1][-40:]] = "0x" + l["data"][2:66][-40:]
        b = to + 1
        time.sleep(0.15)
    json.dump(res, open(out, "w"))
    n_eth = sum(1 for v in res.values() if v == ZERO)
    print(f"{len(res)} lanzamientos -> {out} | cotizan en ETH: {n_eth} ({n_eth / max(len(res), 1):.1%})")


def apply(db, path):
    m = json.load(open(path))
    c = sqlite3.connect(db, timeout=60)
    cols = [r[1] for r in c.execute("PRAGMA table_info(tokens)")]
    if "quote_token" not in cols:
        c.execute("ALTER TABLE tokens ADD COLUMN quote_token TEXT")
    n = 0
    for tok, q in m.items():
        n += c.execute("UPDATE tokens SET quote_token=? WHERE chain='robinhood' AND token_address=? AND quote_token IS NULL", (q, tok)).rowcount
    c.commit()
    print(f"actualizados {n} tokens de {len(m)} en el mapa")


def stats(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda s: c.execute(s).fetchone()[0]
    print("tokens con activo de cotizacion:", q("SELECT COUNT(*) FROM tokens WHERE chain='robinhood' AND quote_token IS NOT NULL"),
          "| no-ETH:", q(f"SELECT COUNT(*) FROM tokens WHERE chain='robinhood' AND quote_token IS NOT NULL AND quote_token != '{ZERO}'"))
    print("alertas sobre curvas no-ETH:", q(f"SELECT COUNT(*) FROM stampede_alerts a JOIN tokens t ON t.chain=a.chain AND t.token_address=a.token_address WHERE a.chain='robinhood' AND t.quote_token IS NOT NULL AND t.quote_token != '{ZERO}'"),
          "de", q("SELECT COUNT(*) FROM stampede_alerts WHERE chain='robinhood'"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "scan" and len(sys.argv) >= 3:
        scan(sys.argv[2], int(sys.argv[sys.argv.index("--blocks") + 1]) if "--blocks" in sys.argv else 4_000_000)
    elif cmd == "apply" and len(sys.argv) >= 4:
        apply(sys.argv[2], sys.argv[3])
    elif cmd == "stats" and len(sys.argv) >= 3:
        stats(sys.argv[2])
    else:
        print(__doc__)
