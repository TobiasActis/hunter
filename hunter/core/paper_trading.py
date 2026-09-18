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
import asyncio
import logging

from config.settings import (
    SIM_BANKROLL_USD, SIM_ENFORCE_CAPITAL, SIM_POSITION_USD, SIM_ENTRY_LATENCY_S, SIM_EXIT_LATENCY_S,
)
from core.db import (
    get_conn, open_paper_position, get_paper_position, record_partial_exit, get_setting,
)
from core.ev_calculator import FeeStructure, cost_of_trade, exit_cost
from core.slippage import curve_depth_usd, price_factor
from core.token_price import fetch_current_price

logger = logging.getLogger("hunter.paper_trading")

DEFAULT_POSITION_USD = SIM_POSITION_USD
FEES = FeeStructure()  # Solana y demás: comisión + slippage plano, como antes

# Robinhood Chain (2026-09-18): comisión del protocolo Pons ~1% por lado
# (visible en el campo fee de los eventos CurveBuy/CurveSell) + gas de
# L2 de centavos (piso $0.05, NO el piso de $0.95 de bots de Solana, que
# solo aplicaba a Solana). El slippage ya NO va acá: se calcula por
# orden con el modelo de curva medido (core/slippage.py), así que
# slippage_pct queda en 0 para no cobrarlo dos veces.
FEES_ROBINHOOD = FeeStructure(
    entry_fee_pct=0.01, entry_fee_min_usd=0.05,
    exit_fee_pct=0.01, exit_fee_min_usd=0.05,
    slippage_pct=0.0,
)


def fees_for(chain: str) -> FeeStructure:
    return FEES_ROBINHOOD if chain == "robinhood" else FEES


def free_cash_usd(conn) -> float:
    """Capital libre de la cuenta simulada: capital inicial + PnL
    realizado - costo de lo que sigue invertido. Solo cuenta posiciones
    abiertas desde sim_v2_since (cuando arrancó la simulación realista;
    las anteriores usaban otro modelo de costos y otro tamaño)."""
    since = get_setting(conn, "sim_v2_since") or "0000"
    row = conn.execute(
        """SELECT COALESCE(SUM(pnl_usd), 0) pnl,
                  COALESCE(SUM(amount_usd * remaining_fraction), 0) invested
           FROM paper_positions WHERE opened_at >= ?""", (since,),
    ).fetchone()
    return SIM_BANKROLL_USD + row["pnl"] - row["invested"]


async def open_position(chain: str, token_address: str, amount_usd: float = DEFAULT_POSITION_USD,
                         alert_id: int | None = None, latency: bool = False) -> dict | None:
    """Abre una posición simulada como lo haría una cuenta real: espera
    la latencia de ejecución, llena al precio spot + impacto de la curva
    (core/slippage.py) y solo si hay capital libre (free_cash_usd).
    None si no se pudo obtener precio; {"skipped": ...} si no hay capital.

    alert_id (2026-09-16) liga la posición a la manada que la causó --
    se usa al cerrarla para actualizar el win-rate de cada wallet que
    participó (ver core/db.py::record_partial_exit y wallet_stats)."""
    if latency and SIM_ENTRY_LATENCY_S > 0:
        await asyncio.sleep(SIM_ENTRY_LATENCY_S)

    spot = await fetch_current_price(chain, token_address)
    if spot is None:
        logger.warning(f"No se pudo abrir posición paper para {token_address}: sin precio disponible.")
        return None

    # Chequeo de capital + alta en el MISMO bloque sin await en el medio:
    # con varias aperturas concurrentes no se puede gastar dos veces lo mismo.
    with get_conn() as conn:
        cash = free_cash_usd(conn)
        if SIM_ENFORCE_CAPITAL and cash < amount_usd:
            logger.info(f"Sin capital libre (${cash:.2f} < ${amount_usd:.2f}) -- no se abre {token_address}.")
            return {"skipped": "sin_capital"}
        depth = curve_depth_usd(conn, chain, token_address)
        entry_price = spot * price_factor(amount_usd, depth, "buy") if depth else spot
        effective_entry_usd = amount_usd - cost_of_trade(amount_usd, fees_for(chain))
        position_id = open_paper_position(
            conn, chain, token_address, amount_usd, entry_price, effective_entry_usd, alert_id=alert_id
        )

    logger.info(
        f"Posición paper #{position_id} abierta: {token_address} spot {spot:.4g} -> "
        f"llenado {entry_price:.4g} (+{(entry_price / spot - 1):.2%} slippage), ${amount_usd}"
    )
    return {"id": position_id, "token_address": token_address, "entry_price": entry_price, "amount_usd": amount_usd}


async def sell_partial(position_id: int, fraction: float, reason: str, exit_price: float | None = None,
                        latency: bool = False) -> dict | None:
    """
    Vende `fraction` del tamaño ORIGINAL de la posición (0 < fraction <= 1,
    y <= lo que quede abierto). Si fraction cubre todo lo que quedaba, la
    posición queda 'closed' sola (ver core/db.py::record_partial_exit).
    None si no se pudo obtener precio o si no hay nada que vender.

    `exit_price` (2026-09-17): si el caller YA consultó el precio actual
    para decidir vender (auto_trader.py -- stop-loss/take-profit/trailing
    stop deciden mirando el precio), hay que pasarlo acá en vez de dejar
    que esta función pida uno NUEVO. Bug real encontrado en producción:
    dos fetches independientes (uno para decidir, otro acá adentro para
    ejecutar) podían devolver precios distintos si el feed tenía un
    glitch entre medio -- se vio un caso con multiplier=3237x etiquetado
    "stop_loss" (imposible: stop-loss solo dispara si el precio CAYÓ),
    que infló el PnL total en +$8154 sobre datos falsos. Si no se pasa
    (ej. cierre manual del dashboard, o la red de seguridad de 1h que no
    mira precio para decidir), se pide uno fresco como antes.

    latency (2026-09-18, simulación realista): entre decidir vender y
    que la orden se llene pasan SIM_EXIT_LATENCY_S segundos -- se espera
    y se toma el precio de ESE momento (en una caída, el llenado real es
    peor que el precio que disparó el stop). El precio spot después se
    ajusta por el impacto de la curva de la venta (core/slippage.py)."""
    with get_conn() as conn:
        position = get_paper_position(conn, position_id)

    if position is None or position["status"] != "open":
        return None

    if latency and SIM_EXIT_LATENCY_S > 0:
        await asyncio.sleep(SIM_EXIT_LATENCY_S)
        fresh = await fetch_current_price(position["chain"], position["token_address"])
        if fresh is not None:
            exit_price = fresh
        with get_conn() as conn:  # puede haberse cerrado/vendido durante la espera
            position = get_paper_position(conn, position_id)
        if position is None or position["status"] != "open":
            return None

    fraction = min(fraction, position["remaining_fraction"])
    if fraction <= 1e-9:
        return None

    if exit_price is None:
        exit_price = await fetch_current_price(position["chain"], position["token_address"])
    if exit_price is None:
        logger.warning(f"No se pudo vender posición paper #{position_id}: sin precio disponible.")
        return None

    fees = fees_for(position["chain"])
    entry_price = position["entry_price"]
    # Impacto de la venta sobre la curva (valor aproximado a vender, en USD)
    if entry_price:
        approx_sale_usd = position["amount_usd"] * fraction * (exit_price / entry_price)
        with get_conn() as conn:
            depth = curve_depth_usd(conn, position["chain"], position["token_address"])
        if depth:
            exit_price = exit_price * price_factor(approx_sale_usd, depth, "sell")

    multiplier = exit_price / position["entry_price"] if position["entry_price"] else 0

    # effective_entry_usd puede faltar en posiciones abiertas ANTES de
    # este cambio (migración solo agrega la columna, no la calcula
    # retroactivamente) -- se aproxima al vuelo en vez de romper.
    effective_entry_total = position["effective_entry_usd"]
    if effective_entry_total is None:
        effective_entry_total = position["amount_usd"] - cost_of_trade(position["amount_usd"], fees)

    slice_original_usd = position["amount_usd"] * fraction
    slice_effective_entry = effective_entry_total * fraction
    gross_exit_value = slice_effective_entry * multiplier
    pnl_usd = (gross_exit_value - exit_cost(gross_exit_value, fees)) - slice_original_usd

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


async def close_position(position_id: int, reason: str = "manual_close", latency: bool = False) -> dict | None:
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

    return await sell_partial(position_id, position["remaining_fraction"], reason=reason, latency=latency)
