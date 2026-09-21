"""
Prueba de la v7: la salida por presion de venta solo actua despues de SELL_PRESSURE_MIN_AGE_S segundos desde el llenado. Correr con: python test_sell_pressure_age.py
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import settings
from core import auto_trader as at

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


now = datetime.now(timezone.utc)
check("la espera de la v7 es de 45 s", settings.SELL_PRESSURE_MIN_AGE_S == 45 and at.SELL_PRESSURE_MIN_AGE_S == 45)
check("segundos desde un instante con zona", abs(at._seconds_since((now - timedelta(seconds=30)).isoformat()) - 30) < 2)
check("sin zona se asume UTC", abs(at._seconds_since((now - timedelta(seconds=30)).replace(tzinfo=None).isoformat()) - 30) < 2)
check("dato invalido -> valor enorme (la regla actua como antes, nunca queda bloqueada)", at._seconds_since("no-es-fecha") > 1e8 and at._seconds_since(None) > 1e8)

calls = []


@contextmanager
def fake_conn():
    yield object()


async def fake_price(chain, token):
    return 1.0


async def fake_sell(pid, frac, reason, exit_price=None, latency=False):
    calls.append((pid, reason))
    return {"pnl_usd": -1.0, "multiplier": 1.0}


at.get_conn = fake_conn
at.fetch_current_price = fake_price
at.count_distinct_sellers_since = lambda conn, chain, token, since: 5                # 5 vendedores distintos: superaria K=3
at.get_paper_position = lambda conn, pid: {"id": pid, "status": "open", "remaining_fraction": 1.0}
at.sell_partial = fake_sell
at.update_peak_price = lambda *a, **k: None


def pos(age_s):
    return {"id": 7, "chain": "robinhood", "token_address": "0xt", "entry_price": 1.0, "peak_price": 1.0, "remaining_fraction": 1.0, "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()}


asyncio.run(at._manage_open_position(pos(10)))
check("a los 10 s con 5 vendedores NO vende por presion de venta", calls == [])
asyncio.run(at._manage_open_position(pos(60)))
check("a los 60 s con 5 vendedores SI vende por presion de venta", calls == [(7, "sell_pressure")])

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
