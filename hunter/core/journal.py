"""
Diario de operaciones MANUALES en demo (para comprobar con numeros propios un metodo discrecional, p. ej. el de un canal
de trading): se anota cada operacion (entrada, stop, objetivo, salida, comision) y se calcula R neto de costos, win-rate
con intervalo de confianza, expectativa, win-rate necesario para no perder y curva acumulada en R.
No opera nada ni toca ninguna cuenta: es solo un cuaderno con calculadora. Vive en data/scalp.db (tabla journal_trades).
"""
import math

from core.scalper import conn_ctx, now_iso

DEFAULT_FEE_SIDE_PCT = 0.07     # 0.05% comision taker + 0.02% slippage por lado (igual que el motor automatico)

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    method TEXT, symbol TEXT NOT NULL, tf TEXT,
    side TEXT NOT NULL,                      -- 'long' | 'short'
    entry REAL NOT NULL, stop REAL NOT NULL, target REAL,
    fee_side_pct REAL NOT NULL, leverage REAL,
    exit_price REAL, closed_at TEXT, note TEXT
);
"""


def init():
    with conn_ctx() as c:
        c.executescript(SCHEMA)


def _num(d, key, required=True, lo=None, hi=None):
    v = d.get(key)
    if v in (None, ""):
        if required:
            raise ValueError(f"falta {key}")
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} no es un numero")
    if not math.isfinite(v):
        raise ValueError(f"{key} no es valido")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise ValueError(f"{key} fuera de rango")
    return v


def _text(d, key, n):
    return str(d.get(key) or "").strip()[:n]


def _check_geometry(side, entry, stop, target):
    if side == "long" and not stop < entry:
        raise ValueError("en un LARGO el stop debe estar por debajo de la entrada")
    if side == "short" and not stop > entry:
        raise ValueError("en un CORTO el stop debe estar por encima de la entrada")
    if target is not None and ((side == "long" and target <= entry) or (side == "short" and target >= entry)):
        raise ValueError("el objetivo debe estar del lado de la ganancia")


def add_trade(d: dict) -> int:
    init()
    side = str(d.get("side") or "").lower()
    if side not in ("long", "short"):
        raise ValueError("lado invalido (long o short)")
    symbol = _text(d, "symbol", 20).upper()
    if not symbol:
        raise ValueError("falta el activo")
    entry = _num(d, "entry", lo=1e-12)
    stop = _num(d, "stop", lo=1e-12)
    target = _num(d, "target", required=False, lo=1e-12)
    fee = _num(d, "fee_side_pct", required=False, lo=0, hi=2)
    fee = DEFAULT_FEE_SIDE_PCT if fee is None else fee
    lev = _num(d, "leverage", required=False, lo=1, hi=1000)
    exit_price = _num(d, "exit_price", required=False, lo=1e-12)
    _check_geometry(side, entry, stop, target)
    with conn_ctx() as c:
        cur = c.execute(
            """INSERT INTO journal_trades (created_at, method, symbol, tf, side, entry, stop, target, fee_side_pct, leverage,
               exit_price, closed_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_iso(), _text(d, "method", 40), symbol, _text(d, "tf", 10), side, entry, stop, target, fee, lev,
             exit_price, now_iso() if exit_price else None, _text(d, "note", 200)),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price) -> None:
    init()
    price = _num({"x": exit_price}, "x", lo=1e-12)
    with conn_ctx() as c:
        r = c.execute("SELECT id, exit_price FROM journal_trades WHERE id=?", (trade_id,)).fetchone()
        if not r:
            raise ValueError("operacion inexistente")
        if r["exit_price"] is not None:
            raise ValueError("la operacion ya estaba cerrada")
        c.execute("UPDATE journal_trades SET exit_price=?, closed_at=? WHERE id=?", (price, now_iso(), trade_id))


def delete_trade(trade_id: int) -> None:
    init()
    with conn_ctx() as c:
        c.execute("DELETE FROM journal_trades WHERE id=?", (trade_id,))


def compute(t: dict) -> dict:
    """Agrega los calculos de una operacion: distancia al stop, ratio planificado y, si esta cerrada, R bruto/costo/neto."""
    sign = 1 if t["side"] == "long" else -1
    dist = abs(t["entry"] - t["stop"])
    out = dict(t)
    out["stop_pct"] = dist / t["entry"] * 100
    out["planned_rr"] = abs(t["target"] - t["entry"]) / dist if t.get("target") else None
    out["cost_r"] = 2 * t["fee_side_pct"] / 100 * t["entry"] / dist
    if t.get("exit_price") is not None:
        out["gross_r"] = sign * (t["exit_price"] - t["entry"]) / dist
        out["net_r"] = out["gross_r"] - out["cost_r"]
    else:
        out["gross_r"] = out["net_r"] = None
    return out


def wilson(k: int, n: int, z: float = 1.96):
    """Intervalo de confianza de Wilson para una proporcion (mejor que el normal con n chico)."""
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def stats_of(trades: list) -> dict:
    closed = sorted([t for t in trades if t["net_r"] is not None], key=lambda t: (t["closed_at"] or "", t["id"]))
    n = len(closed)
    res = {"n_closed": n, "n_open": len(trades) - n}
    if n == 0:
        return res
    r = [t["net_r"] for t in closed]
    wins = [x for x in r if x > 0]
    losses = [-x for x in r if x <= 0]
    lo, hi = wilson(len(wins), n)
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    cum, run, peak, dd = [], 0.0, 0.0, 0.0
    for x in r:
        run += x; cum.append(run); peak = max(peak, run); dd = max(dd, peak - run)
    rrs = [t["planned_rr"] for t in closed if t["planned_rr"] is not None]
    res.update({
        "wins": len(wins), "wr": len(wins) / n, "wr_lo": lo, "wr_hi": hi, "avg_win_r": aw, "avg_loss_r": al,
        "expectancy_r": sum(r) / n, "total_r": run, "breakeven_wr": (al / (aw + al)) if (aw + al) > 0 else None,
        "avg_planned_rr": (sum(rrs) / len(rrs)) if rrs else None,
        "share_rr_ge2": (sum(1 for x in rrs if x >= 2) / len(rrs)) if rrs else None,
        "cum_r": cum, "max_drawdown_r": dd,
        "avg_cost_r": sum(t["cost_r"] for t in closed) / n,
    })
    return res


def list_and_stats() -> dict:
    init()
    with conn_ctx() as c:
        rows = [compute(dict(r)) for r in c.execute("SELECT * FROM journal_trades ORDER BY id DESC")]
    return {"trades": rows, "stats": stats_of(rows), "default_fee_side_pct": DEFAULT_FEE_SIDE_PCT}
