"""
Calculadora de expectativa (EV) para la estrategia:
"N apuestas chicas, la mayoría pierde, una gana Xx y paga todo".

Esto NO te dice si vas a ganar. Te dice, con la estructura de comisiones
real, qué tiene que ser CIERTO en el mundo (probabilidad de encontrar un
ganador, tamaño del multiplicador) para que la estrategia tenga
expectativa positiva. Después comparamos eso contra probabilidades
reales del mercado (lo que sabemos por los informes anteriores) para
ver si es realista o wishful thinking.
"""
from dataclasses import dataclass


@dataclass
class FeeStructure:
    entry_fee_pct: float = 0.01       # 1% comisión del bot en la compra
    entry_fee_min_usd: float = 0.95   # piso de Solana
    exit_fee_pct: float = 0.01        # 1% comisión del bot en la venta
    exit_fee_min_usd: float = 0.95
    slippage_pct: float = 0.03        # estimación conservadora para tokens chicos (3%)


def cost_of_trade(size_usd: float, fees: FeeStructure) -> float:
    """Comisión efectiva de ENTRAR con size_usd, en USD."""
    return max(size_usd * fees.entry_fee_pct, fees.entry_fee_min_usd) + (size_usd * fees.slippage_pct)


def exit_cost(gross_value_usd: float, fees: FeeStructure) -> float:
    """Comisión efectiva de SALIR con gross_value_usd, en USD -- separada
    de net_result_of_position para poder cobrarla por TRAMO cuando una
    posición se vende en partes (toma de ganancias escalonada), sin
    recobrar la comisión de ENTRADA en cada tramo (esa se paga una sola
    vez, al abrir -- ver core/paper_trading.py)."""
    return max(gross_value_usd * fees.exit_fee_pct, fees.exit_fee_min_usd) + \
        (gross_value_usd * fees.slippage_pct)


def net_result_of_position(entry_usd: float, multiplier: float, fees: FeeStructure) -> float:
    """
    Devuelve el resultado NETO en USD de una posición, después de
    comisión de entrada, comisión de salida y slippage en ambos lados.
    multiplier = 0 significa pérdida total (rug / va a cero).
    multiplier = 20 significa 20x sobre lo efectivamente invertido.
    """
    entry_cost = cost_of_trade(entry_usd, fees)
    effective_entry = entry_usd - entry_cost  # lo que realmente queda "trabajando"
    if effective_entry <= 0:
        return -entry_usd  # la comisión sola ya se comió todo

    gross_exit_value = effective_entry * multiplier
    net_exit_value = gross_exit_value - exit_cost(gross_exit_value, fees)

    return net_exit_value - entry_usd  # ganancia/pérdida neta vs. lo que pusiste


def simulate_strategy(
    capital_usd: float,
    num_positions: int,
    losers: int,
    winner_multiplier: float,
    fees: FeeStructure,
):
    """
    Simula exactamente tu escenario: capital dividido en N posiciones
    iguales, K de ellas pierden todo (van a 0), y las restantes ganan
    winner_multiplier.
    """
    size_per_position = capital_usd / num_positions
    winners = num_positions - losers

    total_result = 0.0
    breakdown = []

    for i in range(num_positions):
        if i < losers:
            result = net_result_of_position(size_per_position, multiplier=0, fees=fees)
            breakdown.append((f"Posición {i+1} (pierde todo)", result))
        else:
            result = net_result_of_position(size_per_position, multiplier=winner_multiplier, fees=fees)
            breakdown.append((f"Posición {i+1} (gana {winner_multiplier}x)", result))
        total_result += result

    final_capital = capital_usd + total_result
    return final_capital, total_result, breakdown


def required_multiplier_for_breakeven(
    capital_usd: float, num_positions: int, losers: int, fees: FeeStructure
) -> float:
    """Busca (por bisección simple) qué multiplicador necesita el ganador
    para que el resultado total sea exactamente $0 (breakeven)."""
    lo, hi = 0.1, 1000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        _, total, _ = simulate_strategy(capital_usd, num_positions, losers, mid, fees)
        if total > 0:
            hi = mid
        else:
            lo = mid
    return hi


if __name__ == "__main__":
    fees = FeeStructure()
    CAPITAL = 50.0
    POSITIONS = 5
    LOSERS = 4

    print("=" * 70)
    print(f"ESCENARIO: ${CAPITAL} dividido en {POSITIONS} posiciones de "
          f"${CAPITAL/POSITIONS:.2f} c/u, {LOSERS} pierden todo, 1 gana")
    print("=" * 70)

    for mult in [5, 10, 20, 30, 50]:
        final_capital, total_result, breakdown = simulate_strategy(
            CAPITAL, POSITIONS, LOSERS, mult, fees
        )
        signo = "✅" if total_result > 0 else "❌"
        print(f"\n{signo} Si el ganador hace {mult}x:")
        print(f"   Capital final: ${final_capital:.2f}  (resultado neto: ${total_result:+.2f})")

    breakeven = required_multiplier_for_breakeven(CAPITAL, POSITIONS, LOSERS, fees)
    print(f"\n{'='*70}")
    print(f"🎯 MULTIPLICADOR MÍNIMO PARA NO PERDER PLATA: {breakeven:.1f}x")
    print(f"   (con comisiones ~1% + slippage ~3% en cada punta)")
    print("=" * 70)

    print("\n--- Detalle del escenario de 20x ---")
    _, _, breakdown = simulate_strategy(CAPITAL, POSITIONS, LOSERS, 20, fees)
    for label, result in breakdown:
        print(f"  {label}: ${result:+.2f}")
