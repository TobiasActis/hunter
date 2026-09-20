"""
HUNTER - punto de entrada.

Conecta: listener de cadena -> detector de manadas -> DB (para
backtesting futuro) -> notificador.

Uso:
    python main.py
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from chains.robinhood import RobinhoodListener
from chains.solana import SolanaListener
from chains.solana_pumpportal import PumpPortalListener
from config.settings import TRACKED_WALLETS, MODE, DASHBOARD_HOST, ENTRY_FILTER_ENFORCE
from core.auto_trader import auto_open_on_alert, refresh_loop as auto_trader_loop
from core.brain import get_cached_model, predict_probability
from core.db import (
    init_db, get_conn, insert_transaction, insert_stampede_alert, get_token_created_at,
    get_wallet_prior_trade_count, update_alert_peak_wallet_count,
    insert_alert_wallets, get_wallet_win_rates, get_wallet_buy_amounts_for_token,
    get_token_lifecycle, get_creator_track_record, get_early_buy_concentration,
    update_alert_entry_decision, get_wallet_rep_score, update_alert_wallet_rep,
)
from core.entry_filter import decide as decide_entry
from core.eth_price import fetch_eth_usd, refresh_loop as eth_price_refresh_loop
from core.notifier import send_telegram, format_stampede_alert
from core.outcome_tracker import refresh_loop as outcome_tracker_loop
from core.scalper import run as scalper_run
from core.market_brain import run as market_brain_run
from core.xs_momentum import run as xs_momentum_run
from core.sol_price import fetch_sol_usd, refresh_loop as sol_price_refresh_loop
from core.stampede import StampedeDetector
from core.token_lifecycle_solana import run as token_lifecycle_solana_run
from dashboard import run_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hunter.main")

# Acá vive la lista de listeners activos. Agregar una cadena nueva es
# literalmente: escribir chains/<cadena>.py implementando ChainListener,
# importarlo, y agregarlo a esta lista.
#
# PumpPortalListener() da tokens nuevos gratis pero bloquea los swaps
# (buy/sell) detrás de una API key paga -- ver chains/solana_pumpportal.py.
# SolanaListener() es el plan B 100% gratis (RPC público + getTransaction)
# y es el que hoy puede detectar manadas reales sin invertir nada.
# RobinhoodListener() cubre Pons (launchpad de memecoins en Robinhood
# Chain) -- validado con datos reales el 2026-09-13, ver chains/robinhood.py.
ACTIVE_LISTENERS = [
    SolanaListener(),
    RobinhoodListener(),
    # PumpPortalListener(),  # swaps bloqueados sin API key paga (0.02 SOL)
    # BaseListener(),       # próximo paso
]


_bg_tasks: set = set()


def spawn(coro):
    """Lanza una corrutina en segundo plano guardando la referencia (si
    no, asyncio puede recolectarla a mitad de camino) y logueando
    cualquier error en vez de perderlo en silencio."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Tarea en segundo plano falló", exc_info=t.exception())

    task.add_done_callback(_done)
    return task


async def supervise(name: str, coro_factory, restart_delay: int = 10):
    """
    CONFIRMADO EN PRODUCCIÓN (2026-09-16): con asyncio.gather() plano, un
    error no manejado en CUALQUIER tarea (ej: un bug interno de la
    librería websockets durante una reconexión de Solana) cancela TODAS
    las demás tareas y tumba el proceso entero -- pasó 22 veces en un
    día, aunque systemd lo reiniciaba solo 10s después cada vez.

    Esto envuelve cada tarea de fondo para que un fallo ahí SOLO
    reinicie ESA tarea (logueando el error completo para poder
    investigar), sin afectar a las demás -- así un bug en un listener no
    corta el dashboard, el auto-trader, ni los otros listeners.
    """
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Tarea '{name}' se cayó inesperadamente -- reintentando en {restart_delay}s")
            await asyncio.sleep(restart_delay)


async def run_listener(listener, detector: StampedeDetector):
    logger.info(f"Arrancando listener de {listener.name}...")
    async for event in listener.listen(TRACKED_WALLETS):
        with get_conn() as conn:
            insert_transaction(
                conn, event.chain, event.wallet, event.token_address,
                event.side, event.amount_usd, event.price, event.tx_hash,
            )

        alert = detector.process(event)
        if alert is None:
            continue

        if alert["type"] == "update":
            # El token ya había disparado una alerta, pero siguen
            # sumándose compradores dentro de la misma ventana de 8 min
            # -- actualizamos el pico real (ver core/stampede.py, por
            # qué esto importa para poder revisar si 5 es el umbral
            # correcto más adelante).
            with get_conn() as conn:
                update_alert_peak_wallet_count(conn, alert["alert_id"], alert["peak_wallet_count"])
            continue

        # type == "new_alert"
        logger.info(f"Manada detectada: {alert}")
        with get_conn() as conn:
            # Edad del token al momento de la alerta -- señal tomada de
            # un análisis externo (tiempo de curva como indicador de
            # actividad orgánica vs bot), pero medida con NUESTROS
            # propios datos en vez de creerle al análisis ajeno (ver
            # core/token_lifecycle_solana.py y chains/robinhood.py).
            # None si todavía no vimos la creación de este token.
            token_age_seconds = None
            created_at = get_token_created_at(conn, alert["chain"], alert["token_address"])
            if created_at:
                created_dt = datetime.fromisoformat(created_at)
                token_age_seconds = (datetime.now(timezone.utc) - created_dt).total_seconds()

            # Historial del CREADOR del token (no de las wallets
            # compradoras) -- NUEVO 2026-09-17, inspirado en ver a un
            # trader real chequear cuántos tokens previos de un dev
            # llegaron a graduar antes de comprar ("3 de 1300" como red
            # flag). A diferencia del win-rate de wallets, acá "éxito"
            # es objetivo y verificable on-chain (¿graduó o no?), no
            # depende de si nosotros ganamos plata. Puramente
            # informativo todavía -- ver MIN_TOKENS_FOR_CREATOR_TRACK_RECORD.
            creator_tokens_created = None
            creator_migration_rate = None
            token_row = get_token_lifecycle(conn, alert["chain"], alert["token_address"])
            if token_row and token_row["creator"]:
                track_record = get_creator_track_record(conn, alert["chain"], token_row["creator"])
                if track_record:
                    creator_tokens_created = track_record["tokens_created"]
                    creator_migration_rate = track_record["migration_rate"]
                    logger.info(
                        f"Historial del creador (informativo): {track_record['tokens_created']} "
                        f"tokens lanzados, {track_record['migration_rate']:.1%} graduaron."
                    )

            # Concentración de compra temprana ("bundled") -- NUEVO
            # 2026-09-17, la segunda señal sacada de estudiar el mismo
            # trader real. Verificado con datos propios antes de usarlo:
            # tokens con >70% del volumen de los primeros 60s en una
            # sola wallet tuvieron win-rate de 6.7% contra 16.2% de los
            # no concentrados (ver core/db.py::get_early_buy_concentration
            # para la muestra exacta -- chica para el caso de alta
            # concentración, por eso informativo, no filtro todavía).
            early_buy_concentration = get_early_buy_concentration(
                conn, alert["chain"], alert["token_address"],
                token_row["created_at"] if token_row else None,
            )

            # Cuántas veces vimos operar antes a cada una de las 5
            # wallets de la manada -- proxy de "wallet conocida" vs
            # "wallet nueva" mientras no tengamos el historial completo
            # que pediría wallet_scorer.py (ver README, 2026-09-16).
            #
            # OJO: el corte NO puede ser "ahora" -- para cuando la
            # alerta dispara, las 5 compras que la causaron YA están
            # insertadas en `transactions`, así que cada wallet se
            # contaría a sí misma como "trade previo". Usamos el
            # inicio de la ventana de detección (window_seconds atrás)
            # como corte, así quedan afuera las compras de ESTA manada.
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=alert["window_seconds"])
            cutoff_str = cutoff.isoformat()
            prior_counts = [
                get_wallet_prior_trade_count(conn, alert["chain"], w, cutoff_str)
                for w in alert["wallets"]
            ]
            avg_prior = sum(prior_counts) / len(prior_counts) if prior_counts else None
            min_prior = min(prior_counts) if prior_counts else None

            # Win-rate histórico de las wallets de esta manada -- NUEVO
            # 2026-09-16, pedido explícito para "empezar a rastrear
            # wallets ganadoras". PURAMENTE INFORMATIVO por ahora, mismo
            # motivo que brain_predicted_prob: con máximo 3 apariciones
            # por wallet hoy (ver análisis real del mismo día), cualquier
            # win-rate todavía es ruido estadístico -- ver
            # MIN_ALERTS_FOR_WALLET_WIN_RATE en core/db.py. Se guarda
            # igual para poder comparar más adelante contra el resultado
            # real, y para no perder el historial mientras se acumula
            # muestra suficiente.
            win_rates = get_wallet_win_rates(conn, alert["chain"], alert["wallets"])
            avg_wallet_win_rate = sum(win_rates.values()) / len(win_rates) if win_rates else None
            min_wallet_win_rate = min(win_rates.values()) if win_rates else None
            if win_rates:
                logger.info(
                    f"Win-rate histórico de wallets de esta manada (informativo, "
                    f"{len(win_rates)}/{len(alert['wallets'])} con muestra mínima): "
                    f"promedio={avg_wallet_win_rate:.0%} mínimo={min_wallet_win_rate:.0%}"
                )

            # Tamaño de compra de las wallets de esta manada -- NUEVO
            # 2026-09-16, a partir de una pregunta directa ("¿importa
            # cuánto meten?"). Con datos reales de 669 alertas: las
            # GANADORAS tuvieron una compra mediana de $115 contra $165
            # de las PERDEDORAS, y por wallet la correlación entre
            # win-rate y tamaño de compra fue -0.15 (el tercio de
            # wallets con mejor win-rate compra ~6x MENOS que el tercio
            # con peor win-rate). Contraintuitivo, y con muestra chica
            # (191 wallets, la mayoría justo en el mínimo de 5) -- se
            # guarda informativo, NO filtra la compra todavía.
            buy_amounts = get_wallet_buy_amounts_for_token(
                conn, alert["chain"], alert["token_address"], alert["wallets"],
                datetime.now(timezone.utc).isoformat(),
            )
            avg_wallet_buy_usd = sum(buy_amounts) / len(buy_amounts) if buy_amounts else None
            min_wallet_buy_usd = min(buy_amounts) if buy_amounts else None

            # Predicción de brain.py -- PURAMENTE INFORMATIVA todavía
            # (ver core/brain.py y el README: el modelo no tiene señal
            # validada hoy). Se guarda para poder comparar más adelante
            # "qué hubiera dicho el modelo" contra el resultado real.
            #
            # try/except a propósito (2026-09-16): un bug real en
            # producción (modelo pickleado con __module__="__main__",
            # ver train_brain.py) hacía que esto reventara ACÁ, antes
            # de insert_stampede_alert() -- perdiendo la alerta entera
            # y tumbando este listener en loop. Esto es solo un dato
            # informativo, nunca puede costar perder el registro real
            # de una manada detectada.
            brain_prob = None
            try:
                model = get_cached_model()
                if model is not None:
                    brain_prob = predict_probability(
                        model, alert["wallet_count"], alert["window_seconds"],
                        token_age_seconds, avg_prior, min_prior,
                    )
                    logger.info(
                        f"brain.py predice {brain_prob:.1%} de probabilidad de llegar a "
                        f"6.5x+ (informativo -- todavía NO filtra la compra, ver README)."
                    )
            except Exception:
                logger.exception(
                    "Error prediciendo con brain.py -- se sigue sin la predicción "
                    "(queda None), la alerta se guarda igual."
                )

            alert_id = insert_stampede_alert(
                conn, alert["chain"], alert["token_address"],
                alert["wallet_count"], alert["window_seconds"],
                price_at_alert=event.price,
                token_age_seconds=token_age_seconds,
                avg_wallet_prior_trades=avg_prior,
                min_wallet_prior_trades=min_prior,
                brain_predicted_prob=brain_prob,
                avg_wallet_win_rate=avg_wallet_win_rate,
                min_wallet_win_rate=min_wallet_win_rate,
                avg_wallet_buy_usd=avg_wallet_buy_usd,
                min_wallet_buy_usd=min_wallet_buy_usd,
                creator_tokens_created=creator_tokens_created,
                creator_migration_rate=creator_migration_rate,
                early_buy_concentration=early_buy_concentration,
            )
            # Wallets EXACTAS que causaron esta alerta -- se guardan acá
            # (2026-09-16) para poder actualizar wallet_stats con
            # precisión cuando la posición se cierre (ver
            # core/db.py::record_partial_exit), en vez de reconstruirlas
            # a ojo por ventana de tiempo como había que hacer antes.
            insert_alert_wallets(conn, alert_id, alert["wallets"])
            # Solo métrica informativa (modo sombra); un fallo acá nunca debe
            # frenar la alerta.
            try:
                update_alert_wallet_rep(conn, alert_id, get_wallet_rep_score(
                    conn, alert["chain"], list(alert["wallets"]), alert_id))
            except Exception as e:
                logger.warning(f"wallet_rep_score falló para alerta #{alert_id}: {e}")
        detector.set_alert_id(alert["token_address"], alert_id)

        # Filtro de entrada aprendido de los datos propios (2026-09-18,
        # ver core/entry_filter.py): la alerta SIEMPRE queda registrada,
        # pero solo se notifica y se opera si el modelo la considera de
        # las mejores ('pass') o entra por exploración ('explore', para
        # poder medir el filtro en vivo). Ante cualquier fallo, se opera
        # como antes ('nofilter').
        with get_conn() as conn:
            alert_row = conn.execute("SELECT * FROM stampede_alerts WHERE id = ?", (alert_id,)).fetchone()
            entry_score, entry_decision = decide_entry(conn, alert_row)
            if not ENTRY_FILTER_ENFORCE and entry_decision in ("pass", "skip", "explore"):
                # Modo sombra: el score se guarda y se sigue evaluando, pero
                # NO decide -- en vivo no mostró ventaja (ver settings.py).
                entry_decision = "shadow_pass" if entry_decision == "pass" else "shadow_skip"
            update_alert_entry_decision(conn, alert_id, entry_score, entry_decision)
        if entry_decision == "skip":
            logger.info(
                f"Filtro de entrada: alerta #{alert_id} DESCARTADA (score {entry_score:.3f}) -- "
                f"registrada pero sin notificar ni operar."
            )
            continue
        if entry_score is not None:
            logger.info(f"Filtro de entrada: alerta #{alert_id} {entry_decision.upper()} (score {entry_score:.3f})")

        await send_telegram(format_stampede_alert(alert))
        # Auto-trader: abre una posición de PAPER TRADING (simulada,
        # nunca plata real) apenas se detecta la alerta, para no
        # perder el timing si no hay nadie mirando el dashboard.
        # En una tarea aparte: abrir la posición espera la latencia de
        # ejecución simulada (SIM_ENTRY_LATENCY_S) y no debe frenar a
        # este listener mientras tanto.
        spawn(auto_open_on_alert(alert["chain"], alert["token_address"], alert_id=alert_id))


async def main():
    init_db()
    logger.info(f"HUNTER arrancando en modo: {MODE}")
    detector = StampedeDetector()

    initial_sol_price = await fetch_sol_usd()
    if initial_sol_price is None:
        logger.warning(
            "No se pudo obtener el precio inicial de SOL/USD -- "
            "amount_usd va a quedar None hasta que el refresh en segundo "
            "plano lo consiga."
        )
    else:
        logger.info(f"Precio SOL/USD inicial: ${initial_sol_price}")

    initial_eth_price = await fetch_eth_usd()
    if initial_eth_price is None:
        logger.warning(
            "No se pudo obtener el precio inicial de ETH/USD -- "
            "amount_usd va a quedar None (Robinhood Chain) hasta que el "
            "refresh en segundo plano lo consiga."
        )
    else:
        logger.info(f"Precio ETH/USD inicial: ${initial_eth_price}")

    tasks = [
        supervise("sol_price", sol_price_refresh_loop),
        supervise("eth_price", eth_price_refresh_loop),
        supervise("outcome_tracker", outcome_tracker_loop),
        supervise("auto_trader", auto_trader_loop),
        supervise("dashboard", lambda: run_dashboard(host=DASHBOARD_HOST)),
        supervise("scalper", scalper_run),
        supervise("market_brain", market_brain_run),
        supervise("xs_momentum", xs_momentum_run),
        supervise("token_lifecycle_solana", token_lifecycle_solana_run),
    ]
    tasks += [
        supervise(f"listener_{listener.name}", lambda l=listener: run_listener(l, detector))
        for listener in ACTIVE_LISTENERS
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
