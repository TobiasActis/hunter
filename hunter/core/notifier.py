"""
Envío de alertas por Telegram. Simple a propósito: un solo lugar para
cambiar de canal (Discord, email, lo que sea) sin tocar el resto del
sistema.
"""
import logging

import httpx

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("hunter.notifier")


async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram no configurado -- imprimiendo alerta en consola:")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            }, timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Error enviando a Telegram: {e}")
            print(message)  # fallback: al menos lo vemos en consola


def format_stampede_alert(alert: dict) -> str:
    return (
        f"🐎 *MANADA DETECTADA* ({alert['chain']})\n"
        f"Token: `{alert['token_address']}`\n"
        f"Wallets comprando: *{alert['wallet_count']}* "
        f"en {alert['window_seconds']}s\n"
        f"Wallets: {', '.join(w[:6] + '...' for w in alert['wallets'][:5])}"
        f"{'...' if len(alert['wallets']) > 5 else ''}"
    )
