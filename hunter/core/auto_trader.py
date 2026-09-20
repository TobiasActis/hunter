"""
Auto-trader de PAPER TRADING -- abre, toma ganancias, corta pérdidas y
cierra posiciones SIMULADAS automáticamente, sin intervención manual,
para no perder oportunidades mientras nadie está mirando el dashboard.

IMPORTANTE -- esto sigue siendo 100% simulado (ver core/paper_trading.py):
nunca firma una transacción real ni toca una wallet real ni gasta un
centavo de verdad. No cambia la regla del proyecto de que MODE se
queda en "alert_only" -- esto no ejecuta nada real, solo automatiza la
simulación que ya existía con botones manuales.

Regla de ENTRADA: apenas se detecta una alerta de manada, se abre una
posición paper de $DEFAULT_POSITION_USD al precio de ese momento. Si ya
tenemos una posición abierta de ese mismo token, o cerramos una hace
menos de REENTRY_BLOCK_SECONDS (1 h), NO se abre otra.

Regla de SALIDA -- REDISEÑADA el 2026-09-16 analizando 686 alertas
reales, y CORREGIDA ese mismo día unas horas después al ver los
primeros resultados reales en producción (usuario: "no viene muy bien
nuestro sistema" -- correcto, había un bug real, ver punto 1):

  0. STOP-LOSS. Se encontró que de 384 alertas que cayeron -15% o más
     en los primeros 5 minutos, CERO se recuperaron a breakeven en la
     hora siguiente. Cortar temprano cuando cae STOP_LOSS_PCT desde el
     precio de ENTRADA (no desde el pico -- eso es el trailing stop,
     algo distinto) evita quedarse todo el camino hasta la red de
     seguridad de la hora. Vende TODO lo que quede, sin partes.

     VALIDADO en producción unas horas después de desplegarlo: de las
     primeras 155 posiciones cerradas, 118 (76%) cortaron por
     stop-loss -- número alto que en un primer momento pareció un
     stop demasiado ajustado cortando por ruido. Se verificó con datos
     reales (price_after_1h de la alerta, independiente de si ya
     habíamos vendido): 99/100 de esas posiciones seguían por DEBAJO
     del precio de entrada una hora después. El stop-loss está
     funcionando bien -- el problema real estaba en el punto 1.

  1. TOMA DE GANANCIAS EN 2 NIVELES (bug real encontrado el mismo día:
     la versión anterior de 3 niveles a +15%/+50%/+100% con fracciones
     de 25% tenía un error de diseño -- con una posición de
     $DEFAULT_POSITION_USD=$10 y la comisión mínima de $0.95 POR VENTA
     (ver FeeStructure en ev_calculator.py), vender un tramo de 25%
     ($2.50) paga esa comisión mínima como ~38% del tramo solo de
     piso. El breakeven REAL de un tramo de 25% es +62.6%, no +15% ni
     +50% -- verificado con la fórmula exacta de sell_partial().
     Confirmado con datos reales: el nivel de +15% daba PnL promedio
     -$0.12 y el de +50% apenas +$0.70 (positivo solo porque el precio
     ya había subido más que el umbral nominal para cuando el chequeo
     de 60s lo agarraba). Los niveles nuevos (+60%/+120%, con tramos
     más grandes de 40%/30% del original en vez de 25%/25%) SÍ superan
     el breakeven real con margen en el peor caso -- verificado tramo
     por tramo antes de desplegar. Se alcanzan con menos frecuencia que
     antes (23%/13% de los picos históricos vs 43%/27% de los viejos
     umbrales) pero cada vez que disparan es plata real, no una pérdida
     disfrazada de "toma de ganancia".

  2. Lo que quede después del último nivel (30% del original) sigue
     con TRAILING STOP (cae TRAILING_STOP_PCT desde el máximo
     alcanzado -> se vende el resto).

  3. HOLD_SECONDS sigue como red de seguridad final: 1 h hasta la v4; 10 min desde la v5 (2026-09-20).

V5 (2026-09-20, medida con las trayectorias reales de 985 posiciones de v4 y validada en otro periodo, 1145 posiciones de v2+v3): tenencia maxima 10 min
(+$0.31/trade, IC95 [+0.14,+0.49] en v4; +$0.18 [+0.07,+0.30] en la validacion) y stop-loss 15% (+$0.05 y +$0.09 [+0.03,+0.14]); juntas +$0.35 [+0.17,+0.54] y
+$0.25 [+0.12,+0.38]. Mejora chica (~0.5-0.7% del monto): v4 seguia en -3.7% por operacion. El bloqueo de reentrada del mismo token sigue en 1 h.

NO implementado a propósito: "comprar más si el token corrige, como
hacen los traders grandes". No tenemos ninguna señal validada hoy que
distinga una corrección sana de un token que se está muriendo. Ver
README para el detalle de por qué se dejó así.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core.db import (
    get_conn, get_open_paper_positions, get_triggered_exit_reasons,
    has_recent_or_open_position, get_paper_position, update_peak_price, count_distinct_sellers_since,
)
from core.paper_trading import open_position, sell_partial, close_position, DEFAULT_POSITION_USD
from core.token_price import fetch_current_price
from config.settings import SELL_PRESSURE_EXIT_K

logger = logging.getLogger("hunter.auto_trader")

HOLD_SECONDS = 10 * 60  # 10 min desde la v5 (antes 1 h) -- red de seguridad final: lo que sigue abierto se cierra
REENTRY_BLOCK_SECONDS = 60 * 60  # NO se reabre el mismo token si hay una posicion abierta o cerrada hace menos de esto (sigue en 1 h)
CHECK_INTERVAL_SECONDS = 5  # antes 60: con 60s los stop-loss salían a 0.59x en vez de ~0.77x

# Corta TODO lo que quede si el precio cae esto desde la ENTRADA (no
# desde el pico -- eso es TRAILING_STOP_PCT, un concepto distinto).
# v5 (2026-09-20): -15% (antes -20%). Con las trayectorias reales, el stop de 15% rinde +$0.05 a +$0.09 por
# operacion mas que el de 20% (IC95 [+0.00,+0.10] y [+0.03,+0.14] en dos periodos): los stops de v4 salian a 0.65x en
# promedio, o sea que el precio cae muy rapido despues de cruzar el nivel.
STOP_LOSS_PCT = 0.15
STOP_LOSS_REASON = "stop_loss"

# (multiplicador que dispara la venta, fracción de LO QUE QUEDA a
# vender en ese momento, etiqueta). +60%/+120% con tramos de 40%/30%
# del original -- CORREGIDO el 2026-09-16 (ver docstring del módulo):
# la versión anterior (+15%/+50%/+100%, tramos de 25%) no superaba el
# breakeven real de ~$1.90 en comisiones por tramo dado el piso de
# $0.95 por venta. Tramos más grandes diluyen ese piso fijo.
TAKE_PROFIT_LEVELS = [
    (1.60, 0.40, "take_profit_60"),   # 40% del original
    (2.20, 0.5, "take_profit_120"),   # 1/2 de lo que quedó (60%) -> otro 30% del original, queda 30% corriendo
]

TRAILING_STOP_PCT = 0.30  # vender el resto si cae 30% desde el máximo alcanzado
                           # -- punto de partida razonable para la volatilidad de
                           # memecoins, no un número optimizado con datos todavía.
TRAILING_STOP_REASON = "trailing_stop"
SELL_PRESSURE_REASON = "sell_pressure"  # ver SELL_PRESSURE_EXIT_K en config/settings.py


_in_flight: set = set()


async def auto_open_on_alert(chain: str, token_address: str, alert_id: int | None = None):
    with get_conn() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=REENTRY_BLOCK_SECONDS)).isoformat()
        if has_recent_or_open_position(conn, chain, token_address, cutoff):
            logger.info(
                f"Auto-trader: NO se abre posición nueva para {token_address} ({chain}) -- "
                f"ya hay una abierta o cerrada hace menos de 1h de este mismo token."
            )
            return

    alert_price = None
    if alert_id is not None:
        with get_conn() as conn:
            row = conn.execute("SELECT price_at_alert FROM stampede_alerts WHERE id = ?", (alert_id,)).fetchone()
            alert_price = row["price_at_alert"] if row else None

    result = await open_position(
        chain, token_address, DEFAULT_POSITION_USD, alert_id=alert_id, latency=True, alert_price=alert_price,
    )
    if result is not None and result.get("skipped"):
        return  # sin capital libre: ya se logueó en open_position
    if result is None:
        logger.warning(
            f"Auto-trader: no se pudo abrir posición automática para "
            f"{token_address} ({chain}) -- sin precio disponible todavía."
        )
    else:
        logger.info(
            f"Auto-trader: posición #{result['id']} abierta automáticamente "
            f"en {token_address} @ {result['entry_price']} (${result['amount_usd']})"
        )


async def _manage_open_position(position) -> None:
    """Un ciclo de vida completo por posición abierta: actualiza el
    máximo histórico, y decide si toca cortar pérdida, tomar ganancia,
    o vender por trailing stop. La red de seguridad de 1h se maneja
    aparte, en _check_and_close_stale_positions."""
    current_price = await fetch_current_price(position["chain"], position["token_address"])
    if current_price is None or not position["entry_price"]:
        return

    mult_now = current_price / position["entry_price"]

    # Salida por presión de venta (v4, 2026-09-20): si ya vendieron K compradores
    # distintos desde nuestro llenado, se vende todo lo que quede -- ver el
    # razonamiento y los números en config/settings.py::SELL_PRESSURE_EXIT_K.
    if SELL_PRESSURE_EXIT_K > 0 and position["opened_at"]:
        with get_conn() as conn:
            sellers = count_distinct_sellers_since(
                conn, position["chain"], position["token_address"], position["opened_at"])
            if sellers >= SELL_PRESSURE_EXIT_K:
                current = get_paper_position(conn, position["id"])
        if sellers >= SELL_PRESSURE_EXIT_K:
            if current is None or current["status"] != "open" or (current["remaining_fraction"] or 0) <= 1e-9:
                return
            result = await sell_partial(position["id"], current["remaining_fraction"], SELL_PRESSURE_REASON,
                                        exit_price=current_price, latency=True)
            if result:
                logger.info(
                    f"Auto-trader: SALIDA POR PRESIÓN DE VENTA en posición #{position['id']} -- "
                    f"{sellers} compradores distintos ya vendieron ({mult_now:.2f}x), "
                    f"vendido lo que quedaba, PnL ${result['pnl_usd']:+.2f}"
                )
            return

    # Camino rápido: revisando cada pocos segundos, la mayoría de las
    # posiciones no tiene nada que hacer -- sin tocar la base salvo que el
    # precio marque un nuevo máximo.
    if current_price > (position["peak_price"] or 0):
        with get_conn() as conn:
            update_peak_price(conn, position["id"], current_price)
    if (
        (1 - mult_now) < STOP_LOSS_PCT
        and mult_now < TAKE_PROFIT_LEVELS[0][0]
        and position["remaining_fraction"] >= 0.999
    ):
        return

    with get_conn() as conn:
        update_peak_price(conn, position["id"], current_price)
        current = get_paper_position(conn, position["id"])
        already_triggered = get_triggered_exit_reasons(conn, position["id"])

    if current is None or current["status"] != "open":
        return

    multiplier = current_price / position["entry_price"]

    # 0) Stop-loss -- se chequea PRIMERO, antes que nada. Si ya cayó
    # STOP_LOSS_PCT desde la entrada, no tiene sentido evaluar toma de
    # ganancias (obvio que no aplica) -- se vende todo y se termina.
    if STOP_LOSS_REASON not in already_triggered:
        drawdown_from_entry = 1 - multiplier
        if drawdown_from_entry >= STOP_LOSS_PCT:
            result = await sell_partial(position["id"], current["remaining_fraction"], STOP_LOSS_REASON, exit_price=current_price, latency=True)
            if result:
                logger.info(
                    f"Auto-trader: STOP-LOSS en posición #{position['id']} -- "
                    f"cayó {drawdown_from_entry:.0%} desde la entrada ({multiplier:.2f}x), "
                    f"vendido todo lo que quedaba, PnL ${result['pnl_usd']:+.2f}"
                )
            return

    # 1) Toma de ganancias escalonada -- cada nivel se dispara una sola
    # vez, en orden. TAKE_PROFIT_LEVELS está en "fracción de lo que
    # QUEDA" pero sell_partial espera "fracción del tamaño ORIGINAL" --
    # hay que convertir, y releer remaining_fraction fresco por si otro
    # nivel ya vendió algo en este mismo ciclo.
    for threshold, fraction_of_remaining, reason in TAKE_PROFIT_LEVELS:
        if reason in already_triggered:
            continue
        if multiplier >= threshold:
            with get_conn() as conn:
                fresh = get_paper_position(conn, position["id"])
            if fresh is None or fresh["status"] != "open":
                return
            fraction_of_original = fraction_of_remaining * fresh["remaining_fraction"]
            result = await sell_partial(position["id"], fraction_of_original, reason, exit_price=current_price, latency=True)
            if result:
                logger.info(
                    f"Auto-trader: TOMA DE GANANCIA ({reason}) en posición #{position['id']} "
                    f"({multiplier:.2f}x) -- vendido {result['fraction']:.0%} del original, "
                    f"PnL de este tramo ${result['pnl_usd']:+.2f}"
                )
            return  # un solo nivel por ciclo, evaluamos el resto en el próximo

    # 2) Trailing stop -- solo aplica DESPUÉS de haber tomado la última
    # ganancia de la escalera (take_profit_100), así "lo que sigue
    # corriendo" es específicamente el remanente final.
    if TAKE_PROFIT_LEVELS[-1][2] in already_triggered:
        peak = current["peak_price"] or current_price
        drawdown_from_peak = (peak - current_price) / peak if peak else 0
        if drawdown_from_peak >= TRAILING_STOP_PCT:
            result = await sell_partial(
                position["id"], current["remaining_fraction"], TRAILING_STOP_REASON, exit_price=current_price, latency=True
            )
            if result:
                logger.info(
                    f"Auto-trader: TRAILING STOP en posición #{position['id']} -- "
                    f"cayó {drawdown_from_peak:.0%} desde el máximo ({peak:.6g}), "
                    f"vendido el resto ({result['fraction']:.0%} del original), "
                    f"PnL de este tramo ${result['pnl_usd']:+.2f}"
                )


async def _close_stale_position(position) -> None:
    # Red de seguridad final: lo que quede abierto a la hora se
    # cierra sí o sí. reason='time_exit' explícito -- ANTES esto
    # quedaba mezclado con los cierres manuales del botón del
    # dashboard bajo la misma etiqueta 'manual_close', lo que hacía
    # parecer que hubo cientos de cierres manuales cuando en
    # realidad eran casi todos la red de seguridad de la hora
    # (encontrado analizando los datos el 2026-09-16).
    result = await close_position(position["id"], reason="time_exit", latency=True)
    if result is None:
        logger.warning(
            f"Auto-trader: posición #{position['id']} venció (>{HOLD_SECONDS}s) "
            f"pero no se pudo cerrar todavía -- sin precio disponible, reintenta "
            f"en el próximo ciclo."
        )
    else:
        logger.info(
            f"Auto-trader: posición #{position['id']} cerrada automáticamente "
            f"tras {HOLD_SECONDS // 60} min (red de seguridad) -- multiplicador {result['multiplier']:.2f}x, "
            f"PnL neto del tramo final ${result['pnl_usd']:+.2f}"
        )


async def _guarded(position_id: int, coro) -> None:
    """Una sola tarea a la vez por posición: con revisiones cada pocos
    segundos y ventas que esperan la latencia simulada, sin esto la
    siguiente revisión podía intentar vender lo mismo otra vez."""
    if position_id in _in_flight:
        coro.close()
        return
    _in_flight.add(position_id)
    try:
        await coro
    finally:
        _in_flight.discard(position_id)


async def _check_and_close_stale_positions():
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        open_positions = get_open_paper_positions(conn)

    # En memecoins el precio se mueve en segundos -- chequear cada
    # posición UNA POR UNA (como era antes) significa que la última
    # posición de una lista larga se evalúa con un precio ya viejo para
    # cuando le toca el turno (cada chequeo hace una llamada de red a
    # DexScreener/RPC). Con gather() todas se consultan EN PARALELO, así
    # el ciclo entero tarda lo que tarda LA MÁS LENTA, no la suma de
    # todas (encontrado el 2026-09-16 revisando por qué se sentía lento).
    tasks = []
    for position in open_positions:
        opened_at = datetime.fromisoformat(position["opened_at"])
        elapsed = (now - opened_at).total_seconds()
        if elapsed < HOLD_SECONDS:
            tasks.append(_guarded(position["id"], _manage_open_position(position)))
        else:
            tasks.append(_guarded(position["id"], _close_stale_position(position)))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for position, result in zip(open_positions, results):
            if isinstance(result, Exception):
                logger.exception(
                    f"Auto-trader: error manejando posición #{position['id']} "
                    f"-- no afecta a las demás, reintenta en el próximo ciclo.",
                    exc_info=result,
                )


async def refresh_loop(interval_seconds: int = CHECK_INTERVAL_SECONDS):
    while True:
        try:
            await _check_and_close_stale_positions()
        except Exception as e:
            logger.error(f"Error en el auto-trader: {e}")
        await asyncio.sleep(interval_seconds)
