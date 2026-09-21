"""
Medicion HACIA ADELANTE (papel) de dos ideas que en el estudio del 2026-09-21 mostraron una senal debil pero repetida y NO estan probadas:
  1) RASTRO DE BILLETERAS: billeteras que entran >= 20 s despues del lanzamiento (se pueden seguir) y que compran tokens que corren >= 3x en 1 h mucho mas seguido que el resto (data/trail_wallets.json, datos 13-20 sep).
     Se registra cada compra NUEVA de esas billeteras y, como control, 1 de cada 97 compras de otras billeteras; se mide que paso con quien las hubiera seguido 5 s despues.
  2) CORREDORAS: el puntaje por alerta (core/runner_features.py + attention_lab.score_runner); se mide que paso con las alertas puntuadas (entrada 2 s despues de la alerta).
Salida medida (igual para todos los grupos): la 'amplia B' -- stop 25% hasta armar a 1.5x, 50% a 2x, trailing 35%, maximo 1 h, costo de ida y vuelta 2% -- sin deslizamiento de salida (optimista; solo importan las DIFERENCIAS).
NUNCA opera de verdad. Reglas para creerle (fijadas el 2026-09-21 antes de ver datos posteriores):
  RASTRO:    >= 100 TOKENS del rastro medidos (uno por token: la primera billetera de la lista que entro; muchas billeteras compran el mismo token), llegan a >= 3x al menos 1.5 veces mas seguido que el control
             y retorno neto medio B > 0 con IC95 > 0.
  CORREDORAS: >= 100 alertas del top 2% medidas, acierto de >= 3x >= 20% y retorno neto medio B > 0 con IC95 > 0.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from core import attention_lab as al

TRAIL_MIN_N = 100
RUNNER_MIN_N = 100
RUNNER_MIN_HIT = 0.20
TRAIL_LIFT = 1.5


# ------------------------------------------------------------------ medicion (funciones puras + lectura de hunter.db en solo lectura)
def exit_b_return(prices, cost=al.EXIT_COST):
    """Retorno neto de la salida amplia B sobre la trayectoria (prices[0] = entrada)."""
    e = prices[0]
    peak, armed, done, rem, ret = e, False, False, 1.0, 0.0
    for x in prices[1:]:
        peak = max(peak, x)
        m = x / e
        if not armed and 1 - m >= 0.25:
            ret += rem * m
            rem = 0.0
            break
        if not done and m >= 2.0:
            ret += 0.5 * rem * m
            rem *= 0.5
            done = armed = True
        if m >= 1.5:
            armed = True
        if armed and (peak - x) / peak >= 0.35:
            ret += rem * m
            rem = 0.0
            break
    if rem > 0:
        ret += rem * (prices[-1] / e)
    return ret - 1 - cost


def follow_outcome(times, prices, tb, delay, window=al.OUTCOME_WINDOW_S):
    """Entrada en la primera operacion con t >= tb + delay. Devuelve (entrada, pico/entrada, retorno neto B) o None si no hay entrada."""
    k = next((i for i, t in enumerate(times) if t >= tb + delay), None)
    if k is None:
        return None
    seg = []
    for t, p in zip(times[k:], prices[k:]):
        if t > times[k] + window:
            break
        seg.append(p)
    return seg[0], max(seg) / seg[0], exit_b_return(seg)


def token_path(h, token, t_from, t_to):
    """(tiempos, precios) de las operaciones de un token entre dos instantes (epoch), con la limpieza de los estudios (precio en ETH por token <= 1e-5)."""
    iso = lambda x: datetime.fromtimestamp(x, tz=timezone.utc).isoformat()
    rows = h.execute("SELECT detected_at, price FROM transactions WHERE chain='robinhood' AND token_address=? AND detected_at >= ? AND detected_at <= ? AND price > 0 AND price <= ? ORDER BY id",
                     (token, iso(t_from), iso(t_to), al.PRICE_MAX)).fetchall()
    return [datetime.fromisoformat(r[0]).timestamp() for r in rows], [r[1] for r in rows]


def _open_hunter(hunter_db):
    return sqlite3.connect(f"file:{hunter_db}?mode=ro", uri=True, timeout=10)


def load_trail_wallets(path=al.TRAIL_JSON):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ rastro de billeteras
def _first_id_since(h, iso):
    """Primer id de transactions con detected_at >= iso, por busqueda binaria sobre la clave primaria (una consulta por MIN(id) con filtro recorre toda la tabla: ~50 s con 2 millones de filas)."""
    lo, hi = 0, h.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
    while lo < hi:
        mid = (lo + hi) // 2
        r = h.execute("SELECT detected_at FROM transactions WHERE id >= ? ORDER BY id LIMIT 1", (mid,)).fetchone()
        if r is None or r[0] >= iso:
            hi = mid
        else:
            lo = mid + 1
    return lo


def track_trail(hunter_db=al.HUNTER_DB, path=None, wl_path=al.TRAIL_JSON):
    """Registra las compras NUEVAS (primera compra de esa billetera en ese token) de las billeteras con rastro y, de control, 1 de cada 97 compras de otras. Arranca donde terminan los datos con los que se armo la lista
    (data_until del JSON): todo lo posterior es fuera de muestra. Procesa por lotes de TRAIL_BATCH operaciones."""
    wl = load_trail_wallets(wl_path)
    if not wl or not os.path.exists(hunter_db):
        return 0
    wallets = {w["wallet"] for w in wl["wallets"]}
    h = _open_hunter(hunter_db)
    rows_out = []
    try:
        with al.conn_ctx(path) as c:
            r = c.execute("SELECT v FROM at_state WHERE k='trail_last_tx'").fetchone()
        if r is None:
            last = max(_first_id_since(h, wl["data_until"]) - 1, 0)
        else:
            last = int(r["v"])
        has_q = any(col[1] == "quote_token" for col in h.execute("PRAGMA table_info(tokens)"))
        bad = {x[0] for x in h.execute("SELECT token_address FROM tokens WHERE chain='robinhood' AND quote_token IS NOT NULL AND quote_token != ?", (al.NON_ETH_QUOTE,))} if has_q else set()
        # "+chain" impide que SQLite elija el indice por cadena (recorreria toda la tabla): asi recorre solo el rango de ids por clave primaria
        batch = h.execute("SELECT id, wallet, token_address, detected_at FROM transactions WHERE id > ? AND +chain='robinhood' AND side='buy' AND price > 0 AND price <= ? AND amount_usd > 0 AND amount_usd <= ? "
                          "ORDER BY id LIMIT ?", (last, al.PRICE_MAX, al.USD_MAX_TX, al.TRAIL_BATCH)).fetchall()
        max_id = max([b[0] for b in batch], default=last)
        for tid, w, tok, det in batch:
            kind = "trail" if w in wallets else ("control" if tid % al.CONTROL_MOD == 0 else None)
            if kind is None or tok in bad:
                continue
            if h.execute("SELECT 1 FROM transactions WHERE chain='robinhood' AND wallet=? AND token_address=? AND side='buy' AND id < ? LIMIT 1", (w, tok, tid)).fetchone():
                continue                                                              # no es su primera compra de ese token
            rows_out.append((kind, w, tok, datetime.fromisoformat(det).timestamp(), tid))
    finally:
        h.close()
    n = 0
    with al.conn_ctx(path) as c:
        for kind, w, tok, tb, tid in rows_out:
            n += c.execute("INSERT OR IGNORE INTO at_trail_events (kind, wallet, token, tb, tx_id, status) VALUES (?,?,?,?,?, 'pending')", (kind, w, tok, tb, tid)).rowcount
        c.execute("INSERT OR REPLACE INTO at_state (k, v) VALUES ('trail_last_tx', ?)", (str(max_id),))
    return n


def resolve_trail(hunter_db=al.HUNTER_DB, path=None, now=None, max_n=60):
    """Mide el resultado de los eventos pendientes cuando pasaron mas de 1 h + 2 min desde la compra de la billetera."""
    if not os.path.exists(hunter_db):
        return 0
    now = now or time.time()
    with al.conn_ctx(path) as c:
        pend = [dict(r) for r in c.execute("SELECT id, token, tb FROM at_trail_events WHERE status='pending' AND tb <= ? ORDER BY tb LIMIT ?", (now - al.RESOLVE_AFTER_S, max_n))]
    if not pend:
        return 0
    h = _open_hunter(hunter_db)
    outs = []
    try:
        for e in pend:
            t, p = token_path(h, e["token"], e["tb"], e["tb"] + al.FOLLOW_DELAY_S + al.OUTCOME_WINDOW_S + 60)
            outs.append((e["id"], follow_outcome(t, p, e["tb"], al.FOLLOW_DELAY_S)))
    finally:
        h.close()
    with al.conn_ctx(path) as c:
        for eid, o in outs:
            if o is None:
                c.execute("UPDATE at_trail_events SET status='no_entry', resolved_ms=? WHERE id=?", (int(now * 1000), eid))
            else:
                c.execute("UPDATE at_trail_events SET status='done', entry_price=?, peak=?, ret_b=?, resolved_ms=? WHERE id=?", (o[0], o[1], o[2], int(now * 1000), eid))
    return len(outs)


# ------------------------------------------------------------------ resultado de las corredoras (alertas puntuadas)
def resolve_runner(hunter_db=al.HUNTER_DB, path=None, now=None, max_n=60):
    """Mide el resultado de las alertas puntuadas (entrada = primera operacion 2 s despues de la alerta) cuando pasaron mas de 1 h + 2 min."""
    if not os.path.exists(hunter_db):
        return 0
    now_dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    limit_ts = (now_dt - timedelta(seconds=al.RESOLVE_AFTER_S)).timestamp()
    with al.conn_ctx(path) as c:
        pend = [dict(r) for r in c.execute("SELECT alert_id, token FROM at_runner_score WHERE score IS NOT NULL AND resolved_ms IS NULL ORDER BY alert_id LIMIT ?", (max_n * 3,))]
    if not pend:
        return 0
    h = _open_hunter(hunter_db)
    outs = []
    try:
        for e in pend:
            r = h.execute("SELECT triggered_at FROM stampede_alerts WHERE id=?", (e["alert_id"],)).fetchone()
            if not r:
                continue
            tb = datetime.fromisoformat(r[0]).timestamp()
            if tb > limit_ts:
                continue
            t, p = token_path(h, e["token"], tb, tb + al.RUNNER_DELAY_S + al.OUTCOME_WINDOW_S + 60)
            outs.append((e["alert_id"], follow_outcome(t, p, tb, al.RUNNER_DELAY_S)))
            if len(outs) >= max_n:
                break
    finally:
        h.close()
    with al.conn_ctx(path) as c:
        for aid, o in outs:
            c.execute("UPDATE at_runner_score SET peak=?, ret_b=?, resolved_ms=? WHERE alert_id=?", (o[1] if o else None, o[2] if o else None, int(now_dt.timestamp() * 1000), aid))
    return len(outs)


# ------------------------------------------------------------------ lectura para el dashboard
def _stats(vals_hit, vals_ret):
    """n, % que llega a >=3x, retorno medio B con IC95."""
    n = len(vals_ret)
    if n == 0:
        return {"n": 0, "hit": None, "ret": None, "ci": None, "win": None}
    mean, ci = al.mean_ci(vals_ret)
    return {"n": n, "hit": sum(vals_hit) / n, "ret": mean, "ci": ci, "win": sum(1 for x in vals_ret if x > 0) / n}


def _open_ro(path=None):
    c = sqlite3.connect(f"file:{path or al.DB}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def trail_snapshot(path=None, wl_path=al.TRAIL_JSON):
    p = path or al.DB
    wl = load_trail_wallets(wl_path)
    if not os.path.exists(p) or not wl:
        return {"available": False}
    c = _open_ro(p)
    try:
        ev = [dict(r) for r in c.execute("SELECT * FROM at_trail_events ORDER BY tb DESC")]
    finally:
        c.close()
    # UNIDAD = TOKEN: muchas billeteras de la lista compran el MISMO token (en la primera hora medida, 602 compras eran 29 tokens): contarlas todas seria contar un mismo resultado muchas veces. Se toma, por token,
    # la primera compra (la de la billetera que llego primero), que es lo que haria quien sigue el rastro.
    def first_per_token(rows):
        seen = {}
        for e in sorted(rows, key=lambda x: x["tb"]):
            seen.setdefault(e["token"], e)
        return list(seen.values())
    done = {k: first_per_token([e for e in ev if e["kind"] == k and e["status"] == "done"]) for k in ("trail", "control")}
    st = {k: _stats([e["peak"] >= 3 for e in v], [e["ret_b"] for e in v]) for k, v in done.items()}
    total = {k: len({e["token"] for e in ev if e["kind"] == k}) for k in ("trail", "control")}
    n_events = {k: sum(1 for e in ev if e["kind"] == k) for k in ("trail", "control")}
    verdict, detail = "FALTA MUESTRA", f"{st['trail']['n']}/{TRAIL_MIN_N} tokens del rastro medidos"
    if st["trail"]["n"] >= TRAIL_MIN_N and st["control"]["n"] >= 20:
        t, k = st["trail"], st["control"]
        hit_ok = k["hit"] and t["hit"] >= TRAIL_LIFT * k["hit"] and t["hit"] > k["hit"] * 1.0
        ret_ok = t["ret"] is not None and t["ci"] is not None and t["ret"] - t["ci"] > 0
        verdict = "CUMPLE" if (hit_ok and ret_ok) else "NO CUMPLE"
        detail = f"acierta 3x {t['hit']*100:.0f}% vs control {k['hit']*100:.0f}%; retorno {t['ret']*100:+.1f}% (+-{(t['ci'] or 0)*100:.1f}%)"
    per = {}
    for e in ev:
        if e["kind"] == "trail":
            d = per.setdefault(e["wallet"], {"events": 0, "done": 0, "hits": 0, "rets": []})
            d["events"] += 1
            if e["status"] == "done":
                d["done"] += 1
                d["hits"] += 1 if e["peak"] >= 3 else 0
                d["rets"].append(e["ret_b"])
    wallets = []
    for w in wl["wallets"]:
        d = per.get(w["wallet"], {"events": 0, "done": 0, "hits": 0, "rets": []})
        wallets.append({**w, "f_events": d["events"], "f_done": d["done"], "f_hit": (d["hits"] / d["done"]) if d["done"] else None, "f_ret": (sum(d["rets"]) / len(d["rets"])) if d["rets"] else None})
    recent = [{"ms": int(e["tb"] * 1000), "kind": e["kind"], "wallet": e["wallet"], "token": e["token"], "status": e["status"], "peak": e["peak"], "ret": e["ret_b"]} for e in ev[:40]]
    return {"available": True, "built_at": wl.get("built_at"), "data_until": wl.get("data_until"), "base_rate": wl.get("base_rate"), "n_wallets": len(wl["wallets"]), "total": total, "n_events": n_events, "stats": st,
            "verdict": verdict, "detail": detail, "wallets": wallets, "recent": recent, "config": {"min_n": TRAIL_MIN_N, "lift": TRAIL_LIFT, "delay_s": al.FOLLOW_DELAY_S}}


def runner_snapshot(path=None):
    p = path or al.DB
    if not os.path.exists(p):
        return {"available": False}
    c = _open_ro(p)
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM at_runner_score ORDER BY alert_id DESC")]
    finally:
        c.close()
    if not rows:
        return {"available": True, "total": 0, "groups": [], "recent": [], "verdict": "FALTA MUESTRA", "detail": "todavia no hay alertas puntuadas", "config": {"min_n": RUNNER_MIN_N, "min_hit": RUNNER_MIN_HIT}}
    scored = [r for r in rows if r["score"] is not None]
    res = [r for r in scored if r["resolved_ms"] is not None and r["ret_b"] is not None]
    groups = []
    for key, name, f in (("top2", "Top 2% (el filtro estricto)", lambda r: r["top2"] == 1), ("top5", "Top 5%", lambda r: r["top5"] == 1), ("all", "Todas las alertas puntuadas", lambda r: True)):
        sc = [r for r in scored if f(r)]
        rs = [r for r in res if f(r)]
        groups.append({"key": key, "name": name, "scored": len(sc), **_stats([r["peak"] >= 3 for r in rs], [r["ret_b"] for r in rs])})
    t2 = groups[0]
    verdict, detail = "FALTA MUESTRA", f"{t2['n']}/{RUNNER_MIN_N} alertas del top 2% medidas"
    if t2["n"] >= RUNNER_MIN_N:
        ok = t2["hit"] >= RUNNER_MIN_HIT and t2["ret"] is not None and t2["ci"] is not None and t2["ret"] - t2["ci"] > 0
        verdict = "CUMPLE" if ok else "NO CUMPLE"
        detail = f"acierta 3x {t2['hit']*100:.0f}% (piso {RUNNER_MIN_HIT*100:.0f}%); retorno {t2['ret']*100:+.1f}% (+-{(t2['ci'] or 0)*100:.1f}%)"
    recent = [{"alert_id": r["alert_id"], "token": r["token"], "ms": r["ts_ms"], "score": r["score"], "top2": r["top2"], "top5": r["top5"], "peak": r.get("peak"), "ret": r.get("ret_b"),
               "resolved": r.get("resolved_ms") is not None, "note": r["note"]} for r in rows[:40]]
    return {"available": True, "total": len(rows), "no_tape": sum(1 for r in rows if r["score"] is None), "groups": groups, "recent": recent, "verdict": verdict, "detail": detail,
            "config": {"min_n": RUNNER_MIN_N, "min_hit": RUNNER_MIN_HIT}}
