"""
Busca billeteras candidatas para el rastreo manual (core/watch_wallets.py) a partir de las que ya vimos comprando como KOL o smart money de GMGN (core/attention_lab.py, tablas at_events fuentes GMGN_KOL/GMGN_SMARTBUY).
Dos etapas para no gastar de mas (GMGN /v1/user/wallet_stats NO procesa lote pese a la documentacion: con varias direcciones solo devuelve la primera; /v1/user/wallet_profits si procesa lote, hasta 100 por llamada,
pero no trae winrate ni etiquetas):
  1) CRIBADO barato: wallet_profits (30d) en lotes de 100 para TODAS las candidatas -> rendimiento = ganancia realizada / costo del periodo, filtra por actividad minima.
  2) DETALLE de las mejores: wallet_stats de a una (1 req/s) para las que pasaron el cribado -> acierto, distribucion de resultados, etiquetas de GMGN, Twitter; excluye "arbitrager" (son bots de velocidad, no
     traders que se puedan copiar) y avisa si crea muchos tokens (podria ser un deployer, no solo un trader).
Solo lee y muestra un reporte (y lo guarda en data/wallet_scout_last.json): no agrega nada a watch_manual.json por si solo. Correr en el servidor (necesita GMGN_API_KEY):
    ./venv/bin/python wallet_scout.py [--min-trades 15] [--min-pnl 0.30] [--min-winrate 0.45] [--top 20] [--chain sol]
"""
import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx

from core import attention_lab as al


async def _with_retry(fn, tries=5, wait=3.0):
    for i in range(tries):
        try:
            return await fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429 or i == tries - 1:
                raise
            await asyncio.sleep(wait)

GMGN_HOST = "https://openapi.gmgn.ai"
EXCLUDE_TAGS = {"arbitrager"}


def candidate_wallets(chain="solana", path=None):
    """Billeteras distintas vistas comprando (KOL o smart money de GMGN) en la cadena dada."""
    seen = set()
    with al.conn_ctx(path) as c:
        for r in c.execute("SELECT meta FROM at_events WHERE source IN ('GMGN_KOL','GMGN_SMARTBUY') AND chain=?", (chain,)):
            try:
                w = json.loads(r["meta"] or "{}").get("maker")
            except (TypeError, ValueError):
                w = None
            if w:
                seen.add(w)
    return sorted(seen)


def _q(extra=None):
    return {**(extra or {}), "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}


def _unwrap(js, key):
    d = (js or {}).get("data")
    while isinstance(d, dict) and key not in d and isinstance(d.get("data"), (dict, list)):
        d = d["data"]
    return d


async def screen_profits(client, key, chain_code, wallets, period="30d"):
    """Etapa 1: wallet_profits en lotes de 100. Devuelve {wallet: {pnl, profit_usd, cost_usd, buy, sell}}."""
    out = {}
    for i in range(0, len(wallets), 100):
        chunk = wallets[i:i + 100]
        r = await _with_retry(lambda: client.post(GMGN_HOST + "/v1/user/wallet_profits", params=_q(), json={"chain": chain_code, "period": period, "wallet_addresses": chunk},
                                                    headers={"X-APIKEY": key, "User-Agent": "hunter-paper-lab", "Content-Type": "application/json"}, timeout=30))
        r.raise_for_status()
        lst = _unwrap(r.json(), "list")
        for row in (lst.get("list") if isinstance(lst, dict) else lst) or []:
            try:
                w, cost, profit, buy, sell = row["wallet_address"], float(row.get("realized_profit_cost") or 0), float(row.get("realized_profit") or 0), row.get("buy") or 0, row.get("sell") or 0
            except (KeyError, TypeError, ValueError):
                continue
            out[w] = {"pnl": (profit / cost) if cost else None, "profit_usd": profit, "cost_usd": cost, "buy": buy, "sell": sell}
        if i + 100 < len(wallets):
            await asyncio.sleep(1.3)
    return out


async def detail_stats(client, key, chain_code, wallet, period="30d"):
    """Etapa 2: wallet_stats de UNA billetera (winrate, distribucion, etiquetas, Twitter)."""
    r = await _with_retry(lambda: client.get(GMGN_HOST + "/v1/user/wallet_stats", params=_q({"chain": chain_code, "wallet_address": wallet, "period": period}), headers={"X-APIKEY": key, "User-Agent": "hunter-paper-lab"}, timeout=20))
    r.raise_for_status()
    d = _unwrap(r.json(), "pnl_stat")
    if not isinstance(d, dict) or not d.get("wallet_address"):
        return None
    ps, cm = d.get("pnl_stat") or {}, d.get("common") or {}
    return {"wallet": wallet, "winrate": ps.get("winrate"), "tokens": ps.get("token_num"), "gt_5x": ps.get("pnl_gt_5x_num"), "big_loss": ps.get("pnl_lt_nd5_num"),
            "avg_hold_h": (ps.get("avg_holding_period") or 0) / 3600, "tags": cm.get("tags") or [], "created_tokens": cm.get("created_token_count"),
            "twitter": cm.get("twitter_username"), "twitter_verified": cm.get("is_blue_verified"), "account_age_d": max((time.time() - (cm.get("created_at") or time.time())) / 86400, 0)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="sol", choices=["sol", "base"])
    ap.add_argument("--min-trades", type=int, default=15)
    ap.add_argument("--min-pnl", type=float, default=0.30)
    ap.add_argument("--min-winrate", type=float, default=0.45)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--detail-n", type=int, default=40, help="cuantas de la etapa 1 (por pnl) pasan a la etapa 2 de detalle")
    a = ap.parse_args()
    key = os.environ.get("GMGN_API_KEY")
    if not key:
        for line in open(os.path.expanduser("~/hunter/.gmgn_env")):
            if line.startswith("GMGN_API_KEY="):
                key = line.strip().split("=", 1)[1]
    chain_str = al.GMGN_CHAINS.get(a.chain, a.chain)
    wallets = candidate_wallets(chain_str)
    print(f"candidatas (vistas como KOL o smart money en {chain_str}): {len(wallets)}")
    if not wallets:
        return
    async with httpx.AsyncClient() as client:
        screened = await screen_profits(client, key, a.chain, wallets)
        pre = sorted(((w, s) for w, s in screened.items() if s["pnl"] is not None and s["buy"] + s["sell"] >= a.min_trades and s["pnl"] >= a.min_pnl), key=lambda x: -x[1]["pnl"])
        print(f"pasan el cribado (>= {a.min_trades} operaciones, rendimiento 30d >= {a.min_pnl:.0%}): {len(pre)} de {len(screened)} con datos")
        details = {}
        for w, _ in pre[:a.detail_n]:
            try:
                d = await detail_stats(client, key, a.chain, w)
                if d:
                    details[w] = d
            except Exception as e:
                print(f"  (detalle de {w[:10]} fallo: {e})")
            await asyncio.sleep(2.5)
    ranked = []
    for w, s in pre:
        d = details.get(w)
        if not d or (d["winrate"] or 0) < a.min_winrate or (EXCLUDE_TAGS & set(d["tags"])):
            continue
        ranked.append({**s, **d})
    ranked.sort(key=lambda x: -x["pnl"])
    print(f"\ncumplen todo (+ acierto >= {a.min_winrate:.0%}, sin etiqueta de bot de arbitraje): {len(ranked)}\n")
    for s in ranked[:a.top]:
        flag = " [crea tokens: revisar si es deployer]" if (s["created_tokens"] or 0) > 50 else ""
        tw = f" @{s['twitter']}" if s["twitter"] else ""
        print(f"  {s['wallet']}  30d {s['pnl']*100:+6.1f}% (${s['profit_usd']:+,.0f}/${s['cost_usd']:,.0f})  acierto {s['winrate']*100:4.0f}%  "
              f"tokens {s['tokens']:3}  op {s['buy']+s['sell']:4}  tenencia {s['avg_hold_h']:.1f}h  cuenta {s['account_age_d']:.0f}d  tags {s['tags']}{tw}{flag}")
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "chain": chain_str, "candidates_seen": len(wallets), "screened": len(pre), "detailed": len(details), "ranked": ranked}
    json.dump(out, open("data/wallet_scout_last.json", "w", encoding="utf-8"), indent=1, default=str)
    print(f"\nguardado data/wallet_scout_last.json ({len(ranked)} rankeadas)")


if __name__ == "__main__":
    asyncio.run(main())
