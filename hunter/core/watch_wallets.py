"""
Billeteras puntuales que el dueno comparte a mano (data/watch_manual.json) porque le parecen interesantes: se registran sus compras REALES via GMGN /v1/user/wallet_activity (dato publico, autenticacion solo
con la clave de API) y se miden con el MISMO motor del laboratorio de atencion (record_events -> apply_entries -> marcas a 5m/15m/1h/4h/24h -> report_text/get_snapshot), como una fuente mas por billetera
("WATCH_<wallet>"). Asi cada billetera puntual se ve en la pestana "Atencion" del dashboard, con su propia fila y su propio veredicto. Solo mide lo que HABRIA pasado siguiendola: nunca opera de verdad.
"""
import json
import logging
import os
from datetime import datetime, timezone

from core import attention_lab as al

logger = logging.getLogger("hunter.watch")

WATCH_JSON = os.path.join("data", "watch_manual.json")
MIN_USD = 30.0
ACTIVITY_LIMIT = 30


def load_watch_wallets(path=WATCH_JSON):
    try:
        return json.load(open(path, encoding="utf-8"))["wallets"]
    except Exception:
        return []


def parse_wallet_activity(payload, wallet, gmgn_chain, since_ts=None, min_usd=MIN_USD):
    """Compras (event_type buy, is_open_or_close == 0) de una billetera desde wallet_activity, mas nuevas que since_ts. Devuelve (eventos, ultimo_timestamp_visto)."""
    chain = al.GMGN_CHAINS.get(gmgn_chain)
    d = al._unwrap(payload, "activities")
    items = d.get("activities") if isinstance(d, dict) else d
    out, last_ts = [], since_ts or 0
    for it in items if isinstance(items, list) else []:
        ts = it.get("timestamp")
        if ts is None:
            continue
        last_ts = max(last_ts, int(ts))
        if since_ts is not None and ts <= since_ts:
            continue
        if it.get("event_type") != "buy" or (it.get("is_open_or_close") not in (0, "0", None)):
            continue
        addr = (it.get("token") or {}).get("address")
        if not chain or not addr:
            continue
        try:
            usd = float(it.get("cost_usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if usd < min_usd:
            continue
        out.append({"source": f"WATCH_{wallet[:6]}", "chain": chain, "token": al.norm_token(chain, addr),
                    "meta": {"wallet": wallet, "amount_usd": usd, "price_usd": it.get("price_usd"), "symbol": (it.get("token") or {}).get("symbol"), "trade_ts": ts}})
    return out, last_ts


async def poll(client, key, path=None, wl_path=WATCH_JSON, now=None):
    """Une compras nuevas de las billeteras vigiladas en el mismo pipeline de eventos del laboratorio. Devuelve cuantos eventos nuevos se registraron."""
    wl = load_watch_wallets(wl_path)
    if not wl or not key:
        return 0
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    events = []
    for w in wl:
        wallet, gch = w["wallet"], w.get("gmgn_chain", "sol")
        state_key = f"watch_last_{wallet}"
        with al.conn_ctx(path) as c:
            r = c.execute("SELECT v FROM at_state WHERE k=?", (state_key,)).fetchone()
        since = int(r["v"]) if r else None
        try:
            js = await al._gmgn_get(client, key, "/v1/user/wallet_activity", {"chain": gch, "wallet_address": wallet, "limit": ACTIVITY_LIMIT})
            if isinstance(js, dict) and js.get("code") not in (None, 0):
                logger.warning(f"WATCH {wallet[:10]}: respuesta {js.get('code')} {str(js.get('msg') or js.get('message'))[:120]}")
                continue
            evs, last_ts = parse_wallet_activity(js, wallet, gch, since)
            if since is None:                                                       # primera vez: solo fija el punto de partida (no reconstruye compras viejas)
                with al.conn_ctx(path) as c:
                    c.execute("INSERT OR REPLACE INTO at_state (k, v) VALUES (?, ?)", (state_key, str(last_ts)))
                continue
            events += evs
            with al.conn_ctx(path) as c:
                c.execute("INSERT OR REPLACE INTO at_state (k, v) VALUES (?, ?)", (state_key, str(last_ts)))
        except Exception as e:
            logger.warning(f"WATCH {wallet[:10]}: {e}")
        await al.asyncio.sleep(1.3)                                                 # limite de GMGN: 1 solicitud por segundo
    return al.record_events(events, now_ms, path) if events else 0
