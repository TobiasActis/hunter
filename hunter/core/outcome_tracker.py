"""
Job que completa el resultado de cada alerta de manada (price_after_5m/
30m/1h en stampede_alerts) para que core/brain.py tenga con qué entrenar.

Sin esto, brain.py no tiene nunca datos -- las alertas se guardan pero
`outcome_checked` se queda en 0 para siempre (ver core/db.py::SCHEMA).

Corre como tarea de fondo (ver main.py): cada CHECK_INTERVAL_SECONDS
revisa las alertas pendientes y, para las que ya pasó el tiempo
suficiente, consulta el precio actual del token (core/token_price.py,
gratis vía DexScreener) y completa la columna correspondiente.
"""
import asyncio
import logging
from datetime import datetime, timezone

from core.db import get_conn, get_pending_alerts, update_alert_price, mark_outcome_checked
from core.token_price import fetch_current_price

logger = logging.getLogger("hunter.outcome_tracker")

CHECK_INTERVAL_SECONDS = 60

# (segundos desde la alerta, columna a completar)
INTERVALS = [
    (5 * 60, "price_after_5m"),
    (30 * 60, "price_after_30m"),
    (60 * 60, "price_after_1h"),
]


async def check_pending_alerts():
    with get_conn() as conn:
        alerts = get_pending_alerts(conn)

    now = datetime.now(timezone.utc)

    for alert in alerts:
        triggered_at = datetime.fromisoformat(alert["triggered_at"])
        elapsed = (now - triggered_at).total_seconds()

        updates = {}
        for seconds, column in INTERVALS:
            if elapsed >= seconds and alert[column] is None:
                price = await fetch_current_price(alert["chain"], alert["token_address"])
                if price is not None:
                    updates[column] = price

        if not updates and elapsed < INTERVALS[-1][0]:
            continue  # todavía no hay nada nuevo que guardar para esta alerta

        with get_conn() as conn:
            for column, price in updates.items():
                update_alert_price(conn, alert["id"], column, price)
            # Se da por "chequeada" una vez pasada la hora completa, haya
            # salido bien o no el último intento -- si un token murió y
            # DexScreener ya no lo lista, no tiene sentido reintentar para
            # siempre.
            if elapsed >= INTERVALS[-1][0]:
                mark_outcome_checked(conn, alert["id"])
                logger.info(f"Alerta #{alert['id']} ({alert['token_address']}) verificada.")


async def refresh_loop(interval_seconds: int = CHECK_INTERVAL_SECONDS):
    while True:
        try:
            await check_pending_alerts()
        except Exception as e:
            logger.error(f"Error en el job de outcome-tracking: {e}")
        await asyncio.sleep(interval_seconds)
