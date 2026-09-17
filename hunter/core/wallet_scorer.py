"""
Scorer de calidad de wallets.

Objetivo: nunca más copiar a ciegas a alguien como teKa088 (cash
$39.90, 15 posiciones, la mayoría en rojo, "tesis" de comprar más caro
cuanto más sube). Este módulo convierte los criterios que discutimos
en reglas duras, auditables, que corren SOLAS antes de que cualquier
wallet entre a nuestra lista de "smart money" a seguir.

Ninguna wallet pasa el filtro por vibra o por un solo trade ganador.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class WalletSnapshot:
    """Lo que necesitamos saber de una wallet para evaluarla."""
    address: str
    label: str = ""                    # ej: "teKa088" / "@LegalRearMouse"
    cash_usd: float = 0.0
    total_position_value_usd: float = 0.0
    total_trades: int = 0
    days_active: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    open_positions_count: int = 0
    open_positions_losing_count: int = 0    # posiciones abiertas en pérdida
    max_drawdown_pct: float | None = None   # si lo tenemos calculado
    largest_single_trade_pct_of_total_pnl: float | None = None


@dataclass
class ScoreResult:
    wallet: WalletSnapshot
    passed: bool
    score: int              # 0-100, informativo, NO es lo que decide pasar/no pasar
    red_flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# --- Umbrales (ajustables, pero documentados el POR QUÉ de cada uno) ---
MIN_TRADES_FOR_EVALUATION = 100        # menos que esto = no hay muestra suficiente
MIN_DAYS_ACTIVE = 60                    # track record mínimo
MIN_CASH_RATIO = 0.10                   # cash / (cash + posiciones) -- señal de gestión de riesgo
MAX_LOSING_POSITIONS_RATIO = 0.40       # si más del 40% de posiciones abiertas están en rojo
MAX_SINGLE_TRADE_PNL_CONCENTRATION = 0.60  # si un solo trade explica >60% del PnL total, es suerte, no skill
MAX_ACCEPTABLE_DRAWDOWN_PCT = 50.0


def evaluate_wallet(w: WalletSnapshot) -> ScoreResult:
    red_flags: list[str] = []
    reasons: list[str] = []
    score = 100

    # --- Regla 1: muestra estadística mínima ---
    if w.total_trades < MIN_TRADES_FOR_EVALUATION:
        red_flags.append(
            f"Solo {w.total_trades} trades registrados (mínimo: {MIN_TRADES_FOR_EVALUATION}). "
            f"Sin esto, cualquier win rate es ruido estadístico, no una señal."
        )
        score -= 40

    if w.days_active < MIN_DAYS_ACTIVE:
        red_flags.append(
            f"Solo {w.days_active:.0f} días de historial (mínimo: {MIN_DAYS_ACTIVE}). "
            f"Un track record corto no distingue suerte de habilidad."
        )
        score -= 25

    # --- Regla 2: gestión de riesgo (el problema exacto de teKa088) ---
    total_capital = w.cash_usd + w.total_position_value_usd
    cash_ratio = (w.cash_usd / total_capital) if total_capital > 0 else 0
    if cash_ratio < MIN_CASH_RATIO:
        red_flags.append(
            f"Cash ratio de {cash_ratio:.1%} (mínimo: {MIN_CASH_RATIO:.0%}). "
            f"Tiene casi todo el capital metido en posiciones -- cero colchón, "
            f"cero gestión de riesgo. Esto es exactamente lo que vimos en teKa088 (2% de cash)."
        )
        score -= 30

    # --- Regla 3: ¿está sangrando en su book actual? ---
    if w.open_positions_count > 0:
        losing_ratio = w.open_positions_losing_count / w.open_positions_count
        if losing_ratio > MAX_LOSING_POSITIONS_RATIO:
            red_flags.append(
                f"{losing_ratio:.0%} de sus posiciones abiertas están en pérdida "
                f"(máximo aceptable: {MAX_LOSING_POSITIONS_RATIO:.0%}). "
                f"El book actual, no el histórico, es lo que importa hoy."
            )
            score -= 25

    # --- Regla 4: ¿el PnL total depende de UN solo trade de suerte? ---
    if w.largest_single_trade_pct_of_total_pnl is not None:
        if w.largest_single_trade_pct_of_total_pnl > MAX_SINGLE_TRADE_PNL_CONCENTRATION:
            red_flags.append(
                f"Un solo trade explica {w.largest_single_trade_pct_of_total_pnl:.0%} "
                f"de su PnL total (máximo: {MAX_SINGLE_TRADE_PNL_CONCENTRATION:.0%}). "
                f"Esto es el patrón '+15.000% una vez, todo lo demás en rojo' -- suerte, no proceso."
            )
            score -= 35

    # --- Regla 5: drawdown ---
    if w.max_drawdown_pct is not None and w.max_drawdown_pct > MAX_ACCEPTABLE_DRAWDOWN_PCT:
        red_flags.append(
            f"Drawdown máximo de {w.max_drawdown_pct:.0f}% "
            f"(máximo aceptable: {MAX_ACCEPTABLE_DRAWDOWN_PCT:.0f}%)."
        )
        score -= 20

    # --- Regla 6: PnL neto real ---
    net_pnl = w.realized_pnl_usd  # el no realizado NO cuenta para aprobar a nadie
    if net_pnl <= 0:
        red_flags.append(
            "PnL REALIZADO (no en papel) es negativo o cero. "
            "Las ganancias no cobradas no son ganancias."
        )
        score -= 40
    else:
        reasons.append(f"PnL realizado positivo: ${net_pnl:,.2f}")

    score = max(0, score)
    passed = len(red_flags) == 0

    return ScoreResult(wallet=w, passed=passed, score=score, red_flags=red_flags, reasons=reasons)


def evaluate_teka088_example():
    """
    Reconstrucción aproximada del perfil que pegaste, con los números
    que compartiste, para que veas el filtro funcionando sobre un caso
    real y no sobre un ejemplo inventado.
    """
    w = WalletSnapshot(
        address="teKa088_approx",
        label="teKa088 / @LegalRearMouse",
        cash_usd=39.90,
        total_position_value_usd=12_307.25 - 39.90,  # aprox, usando "efectivo total" vs valor mostrado
        total_trades=265,
        days_active=1.4,  # "1d 10h de media por posición" y "se unió ago 2026" -- cuenta MUY nueva
        realized_pnl_usd=-3_588.35,  # el "-$3,588.35" que se ve junto al total
        unrealized_pnl_usd=0,  # desconocido, no importa para esta evaluación
        open_positions_count=15,
        open_positions_losing_count=11,  # MOO, MEME, SCHIFFY, CATGPT, SPACEHOOD, INU, CACHE, MOS, aoc, UP, ETH(0.67%) -- contando negativos
        max_drawdown_pct=None,
        largest_single_trade_pct_of_total_pnl=None,  # no tenemos el detalle de cada trade cerrado
    )
    return evaluate_wallet(w)


if __name__ == "__main__":
    result = evaluate_teka088_example()
    print(f"Wallet: {result.wallet.label}")
    print(f"¿Pasa el filtro? {'✅ SÍ' if result.passed else '❌ NO'}")
    print(f"Score: {result.score}/100")
    print("\nBanderas rojas:")
    for flag in result.red_flags:
        print(f"  🚩 {flag}")
    if result.reasons:
        print("\nA favor:")
        for r in result.reasons:
            print(f"  ✅ {r}")
