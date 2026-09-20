"""
Oyente de alertas del bot de Telegram @kotte_memescan_bot -> laboratorio de graduaciones lentas de HUNTER (paper).
SOLO LEE los mensajes de ESE bot en tu cuenta de Telegram (nunca envia mensajes, nunca lee otros chats) y manda cada alerta "SLOW GRADUATION" al dashboard (POST /api/sg/bot), que la simula al precio real.

SEGURIDAD (leelo antes de usarlo):
  - Leer tus mensajes exige una SESION de Telegram de tu cuenta: es una credencial COMPLETA (equivale a tener tu Telegram abierto). Se guarda solo en el archivo de sesion, en TU maquina o servidor (permisos 600).
  - Recomendado: usar una CUENTA SECUNDARIA (otro numero, eSIM o virtual) que solo le de /start a ese bot, asi la sesion no expone tu cuenta principal.
  - api_id y api_hash se sacan gratis en https://my.telegram.org (API development tools). NUNCA los pegues en un chat: van en variables de entorno.
  - Este script no usa ninguna clave de exchange y no opera nada.

Uso (una vez, interactivo: te pide telefono y el codigo que te llega a Telegram):
    export TG_API_ID=123456 TG_API_HASH=xxxxxxxx
    ./venv/bin/python tg_alerts.py --login
Despues, corriendo siempre (por ejemplo como servicio systemd, ver hunter-tg.service.example):
    export TG_API_ID=... TG_API_HASH=... HUNTER_USER=... HUNTER_PASSWORD=...        # HUNTER_URL por defecto http://127.0.0.1:8000
    ./venv/bin/python tg_alerts.py
Prueba del analizador sin Telegram:   ./venv/bin/python tg_alerts.py --parse "texto de una alerta"
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.request

BOT_USERNAME = "kotte_memescan_bot"
B58 = r"[1-9A-HJ-NP-Za-km-z]{32,44}"


def parse_alert(text: str):
    """Analiza el texto de una alerta del bot. Devuelve dict {kind, mint, symbol, hours} o None si no es una alerta que sepamos simular.
    kind 'slow' = 'SLOW GRADUATION' (token que tardo horas en graduar; ya esta en PumpSwap: se puede simular)."""
    if not text:
        return None
    if "SLOW GRADUATION" in text.upper():
        m = re.search(r"CA:\s*(" + B58 + ")", text)
        if not m:
            return None
        t = re.search(r"Took\s+(\d+(?:\.\d+)?)\s*(min|m|h|d)\+?", text, re.I)
        hours = None
        if t:
            v, u = float(t.group(1)), t.group(2).lower()
            hours = v / 60 if u in ("min", "m") else (v * 24 if u == "d" else v)
        s = re.search(r"\$([A-Za-z0-9_]{1,20})", text)
        return {"kind": "slow", "mint": m.group(1), "symbol": s.group(1) if s else None, "hours": hours}
    return None


def post_alert(base_url: str, user: str, password: str, payload: dict) -> bool:
    req = urllib.request.Request(base_url.rstrip("/") + "/api/sg/bot", data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200


async def listen(login_only: bool):
    try:
        from telethon import TelegramClient, events
    except ImportError:
        sys.exit("Falta la libreria: ./venv/bin/pip install telethon")
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Faltan las variables de entorno TG_API_ID y TG_API_HASH (se sacan en https://my.telegram.org). No las pegues en ningun chat.")
    session = os.path.expanduser(os.environ.get("TG_SESSION", "~/.hunter_tg/session"))
    os.makedirs(os.path.dirname(session), exist_ok=True)
    try:
        os.chmod(os.path.dirname(session), 0o700)
    except OSError:
        pass
    client = TelegramClient(session, int(api_id), api_hash)
    base = os.environ.get("HUNTER_URL", "http://127.0.0.1:8000")
    user, pw = os.environ.get("HUNTER_USER", ""), os.environ.get("HUNTER_PASSWORD", "")
    await client.start()                                                       # login interactivo la primera vez (telefono + codigo)
    try:
        os.chmod(session + ".session", 0o600)
    except OSError:
        pass
    me = await client.get_me()
    print(f"Sesion lista (cuenta: {getattr(me, 'first_name', '?')}). Escuchando SOLO a @{BOT_USERNAME}.", flush=True)
    if login_only:
        await client.disconnect()
        return

    @client.on(events.NewMessage(from_users=BOT_USERNAME))
    async def on_msg(event):
        a = parse_alert(event.raw_text or "")
        if not a:
            print(f"[{event.date:%H:%M:%S}] mensaje del bot sin alerta simulable (se ignora)", flush=True)
            return
        payload = {"mint": a["mint"], "ts_ms": int(event.date.timestamp() * 1000), "hours": a["hours"], "symbol": a["symbol"]}
        try:
            ok = await asyncio.get_event_loop().run_in_executor(None, post_alert, base, user, pw, payload)
            print(f"[{event.date:%H:%M:%S}] alerta {a['symbol']} {a['mint'][:8]}… ({a['hours']} h) -> {'enviada' if ok else 'rechazada'}", flush=True)
        except Exception as e:
            print(f"[{event.date:%H:%M:%S}] no se pudo enviar al dashboard: {e}", flush=True)

    await client.run_until_disconnected()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="solo iniciar sesion (interactivo) y salir")
    ap.add_argument("--parse", help="prueba el analizador con un texto")
    a = ap.parse_args()
    if a.parse is not None:
        print(json.dumps(parse_alert(a.parse), ensure_ascii=False))
    else:
        asyncio.run(listen(a.login))
