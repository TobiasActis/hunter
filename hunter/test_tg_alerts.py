"""
Pruebas de tg_alerts.py: el analizador de alertas y la red de seguridad de reconciliacion (rescata mensajes que el evento en vivo de Telethon se perdio). Correr con: python test_tg_alerts.py
"""
import asyncio
from datetime import datetime, timezone

from tg_alerts import make_handler, parse_alert, reconcile_once

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'MAL'} {name}")


SLOW = """🐌 SLOW GRADUATION

$SD · sd
⏱ Took 15h+ to graduate (most coins: ~20 min)

CA: 6arQWGxhTLuxS42G6k2THZD8yD9NuiRwcSqVbP79pump
📈 4,998 trades on the curve before it filled
"""
NEW_LAUNCH = """🚀 NEW LAUNCH
A dev from our watchlist just launched a coin

$ABLE · Achieving a Better Life Experien
CA: D6Rbcz77a54qzSzwQJZfjY2qNUSsR6UvY6STEcBFzBKd
"""

print("--- parse_alert")
r = parse_alert(SLOW)
check("SLOW GRADUATION: extrae mint, simbolo y horas ('15h+')", r == {"kind": "slow", "mint": "6arQWGxhTLuxS42G6k2THZD8yD9NuiRwcSqVbP79pump", "symbol": "SD", "hours": 15.0})
check("NEW LAUNCH: no es simulable (se ignora a proposito)", parse_alert(NEW_LAUNCH) is None)
check("texto vacio o sin alerta -> None", parse_alert("") is None and parse_alert("hola") is None)
check("'Took 45min+' -> 0.75 h", parse_alert(SLOW.replace("15h+", "45min+"))["hours"] == 0.75)
check("'Took 2d+' -> 48 h", parse_alert(SLOW.replace("15h+", "2d+"))["hours"] == 48.0)

print("--- make_handler: deduplica por id, distingue vivo de rescatado")
sent = []


def fake_post(base, user, pw, payload):
    sent.append(payload)
    return True


seen = set()
handler = make_handler(fake_post, "http://x", "u", "p", seen)
d = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
was_new1 = asyncio.run(handler(101, d, SLOW, True))
check("primera vez: procesa y manda la alerta", was_new1 is True and len(sent) == 1 and sent[0]["mint"].endswith("pump"))
was_new2 = asyncio.run(handler(101, d, SLOW, True))
check("mismo id de nuevo: no lo vuelve a mandar (deduplicado)", was_new2 is False and len(sent) == 1)
was_new3 = asyncio.run(handler(102, d, NEW_LAUNCH, False))
check("id nuevo sin alerta simulable: se marca visto pero no manda nada", was_new3 is True and len(sent) == 1 and 102 in seen)

print("--- reconcile_once: rescata lo que el evento en vivo no vio, sin duplicar lo que si vio")


class FakeMsg:
    def __init__(self, id_, date, text):
        self.id, self.date, self.raw_text = id_, date, text


async def fake_iter():
    for m in (FakeMsg(201, d, SLOW), FakeMsg(202, d, NEW_LAUNCH), FakeMsg(203, d, SLOW.replace("SD", "OTRO").replace("6arQWGxhTLuxS42G6k2THZD8yD9NuiRwcSqVbP79pump", "9arQWGxhTLuxS42G6k2THZD8yD9NuiRwcSqVbP79pump"))):
        yield m


seen2 = set(); sent2 = []
handler2 = make_handler(lambda *a: (sent2.append(a[-1]), True)[1], "http://x", "u", "p", seen2)
asyncio.run(handler2(201, d, SLOW, True))                                           # el evento en vivo YA proceso el 201
n = asyncio.run(reconcile_once(fake_iter(), handler2))
check("no reprocesa el que el evento en vivo ya vio (201)", len(sent2) == 2)          # 201 (vivo) + 203 (rescatado); 202 no es simulable
check("rescata los 2 que el evento en vivo se habia perdido (202 sin alerta, 203 con alerta)", n == 2)
check("el mensaje rescatado se manda igual que el del evento en vivo", any(p["symbol"] == "OTRO" for p in sent2))
n2 = asyncio.run(reconcile_once(fake_iter(), handler2))
check("una segunda reconciliacion sobre los mismos mensajes no encuentra nada nuevo", n2 == 0)

print("\nRESULTADO:", "TODO OK" if ok else "HAY ERRORES")
raise SystemExit(0 if ok else 1)
