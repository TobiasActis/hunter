"""
Paper trading: simula compras/ventas SIN plata real, para poder probar
la estrategia con precios de mercado reales antes de arriesgar algo.

Regla del proyecto (ver README): MODE debe quedarse en "alert_only"
hasta tener un modelo entrenado y validado con backtesting real -- este
módulo es precisamente esa validación, nunca ejecuta una orden de
verdad. No firma transacciones, no toca ninguna wallet real.

Reutiliza el modelo de fees/slippage de core/ev_calculator.py (mismo
que ya usamos para calcular el breakeven real de 6.5x) en vez de
inventar un cálculo de PnL ingenuo -- así el resultado simulado es
comparable con lo que ya sabemos del proyecto.

Soporta VENTAS PARCIALES (2026-09-16, pedido explícito después de ver
en el dashboard que dejar todo a un cierre de golpe a la hora perdía
ganancias que ya se habían visto -- ver core/auto_trader.py para la
lógica de "cuándo" tomar ganancias). La comisión de ENTRADA se cobra
UNA sola vez, al abrir (effective_entry_usd), no se recobra en cada
venta parcial -- eso sería cobrar de más comparado con una entrada real
de $10 seguida de varias salidas chicas.
"""
import logging

from core.db import (
    get_conn, open_paper_position, get_paper_position, record_partial_exit,
)
from core.ev_calculator import FeeStructure, cost_of_trade, exit_cost
from core.token_price import fetch_current_price

logger = logging.getLogger("hunter.paper_trading")

DEFAULT_POSITION_USD = 10.0  # tamaño de posición usado en el escenario del README ($50 / 5)
FEES = FeeStructure()


async def open_position(chain: str, token_address: str, amount_usd: float = DEFAULT_POSITION_USD,
                         alert_id: int | None = None) -> dict | None:
    """Abre una posición simulada al precio de mercado actual. None si no se pudo obtener precio.

    `alert_id` (2026-09-16) liga la posición a la manada que la causó --
    se usa al cerrarla para actualizar el win-rate de cada wallet que
    participó (ver core/db.py::record_partial_exit y wallet_stats)."""
    entry_price = await fetch_current_price(chain, token_address)
    if entry_price is None:
        logger.warning(f"No se pudo abrir posición paper para {token_address}: sin precio disponible.")
        return None

    effective_entry_usd = amount_usd - cost_of_trade(amount_usd, FEES)

    with get_conn() as conn:
        position_id = open_paper_position(
            conn, chain, token_address, amount_usd, entry_price, effective_entry_usd, alert_id=alert_id
        )

    logger.info(f"Posición paper #{position_id} abierta: {token_address} @ {entry_price} SOL, ${amount_usd}")
    return {"id": position_id, "token_address": token_address, "entry_price": entry_price, "amount_usd": amount_usd}


async def sell_partial(position_id: int, fraction: float, reason: str) -> dict | None:
    """
    Vende `fraction` del tamaño ORIGINAL de la posición (0 < fraction <= 1,
    y <= lo que quede abierto) al precio de mercado actual. Si fraction
    cubre todo lo que quedaba, la posición queda 'closed' sola (ver
    core/db.py::record_partial_exit). None si no se pudo obtener precio
    o si no hay nada que vender.
    """
    with get_conn() as conn:
        position = get_paper_position(conn, position_id)

    if position is None or position["status"] != "open":
        return None

    fraction = min(fraction, position["remaining_fraction"])
    if fraction <= 1e-9:
        return None

    exit_price = await fetch_current_price(position["chain"], position["token_address"])
    if exit_price is None:
        logger.warning(f"No se pudo vender posición paper #{position_id}: sin precio disponible.")
        return None

    multiplier = exit_price / position["entry_price"] if position["entry_price"] else 0

    # effective_entry_usd puede faltar en posiciones abiertas ANTES de
    # este cambio (migración solo agrega la columna, no la calcula
    # retroactivamente) -- se aproxima al vuelo en vez de romper.
    effective_entry_total = position["effective_entry_usd"]
    if effective_entry_total is None:
        effective_entry_total = position["amount_usd"] - cost_of_trade(position["amount_usd"], FEES)

    slice_original_usd = position["amount_usd"] * fraction
    slice_effective_entry = effective_entry_total * fraction
    gross_exit_value = slice_effective_entry * multiplier
    pnl_usd = (gross_exit_value - exit_cost(gross_exit_value, FEES)) - slice_original_usd

    with get_conn() as conn:
        record_partial_exit(conn, position_id, fraction, exit_price, multiplier, pnl_usd, reason)

    logger.info(
        f"Posición paper #{position_id}: vendido {fraction:.0%} del original ({reason}) -- "
        f"{multiplier:.2f}x, PnL de este tramo ${pnl_usd:+.2f}"
    )
    return {
        "id": position_id, "fraction": fraction, "exit_price": exit_price,
        "multiplier": multiplier, "pnl_usd": pnl_usd, "reason": reason,
    }


async def close_position(position_id: int, reason: str = "manual_close") -> dict | None:
    """Cierra lo que quede ABIERTO de la posición (100% si nunca se tomó
    ganancia parcial, o el resto si ya se vendieron tramos antes).

    `reason` default 'manual_close' (el botón "Cerrar" del dashboard).
    core/auto_trader.py pasa 'time_exit' explícitamente para la red de
    seguridad de 1h -- ANTES los dos casos quedaban mezclados bajo
    'manual_close', lo que hacía parecer que hubo 431 cierres manuales
    cuando en realidad eran casi todos cierres forzados por la hora
    (encontrado analizando los datos el 2026-09-16)."""
    with get_conn() as conn:
        position = get_paper_position(conn, position_id)

    if position is None or position["status"] != "open":
        return None

    return await sell_partial(position_id, position["remaining_fraction"], reason=reason)
