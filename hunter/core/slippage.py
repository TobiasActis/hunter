"""
Modelo de impacto de precio (slippage) de las curvas de Pons, calibrado
con datos propios el 2026-09-18 -- reemplaza el "3% plano" que se
suponía sin medir.

Calibración (370 tokens reales de Robinhood Chain, transacciones
propias): el precio sigue una curva de producto constante con reserva
virtual, precio ~ (V0 + C)^2, con C = volumen neto acumulado en USD.
sqrt(precio) vs C ajusta con R2 mediano 0.94, y la reserva virtual
inicial V0 da mediana ~$4,600 (rango intercuartil $4.1k-8.3k).

Con eso, una orden de tamaño S sobre una curva con reserva x = V0 + C
paga (en promedio, respecto al precio spot):
    compra: 1 + S/x + S^2/(3x^2)      venta: 1 - S/x + S^2/(3x^2)
Para $50 sobre una curva típica (x ~ $6-10k) es ~0.5-0.8%.

Se usa V0 = $4,100 (percentil 25 de las estimaciones) a propósito: una
reserva menor da MÁS slippage, así que el error va del lado
conservador. Solo Robinhood Chain (donde se calibró); otras cadenas
siguen con el slippage plano de core/ev_calculator.py.
"""
import logging

logger = logging.getLogger("hunter.slippage")

ROBINHOOD_VIRTUAL_RESERVE_USD = 4100.0
MIN_DEPTH_FRACTION = 0.35   # piso de profundidad: x nunca baja de 35% de V0
MAX_IMPACT = 0.60           # tope de seguridad del cálculo


def curve_depth_usd(conn, chain: str, token_address: str) -> float | None:
    """Profundidad actual de la curva en USD (x = V0 + volumen neto
    visto). None si la cadena no está calibrada."""
    if chain != "robinhood":
        return None
    rows = conn.execute(
        """SELECT side, SUM(amount_usd) s FROM transactions
           WHERE chain = ? AND token_address = ? AND amount_usd IS NOT NULL
           GROUP BY side""",
        (chain, token_address),
    ).fetchall()
    net = 0.0
    for r in rows:
        net += (r["s"] or 0) if r["side"] == "buy" else -(r["s"] or 0)
    v0 = ROBINHOOD_VIRTUAL_RESERVE_USD
    return max(v0 + net, v0 * MIN_DEPTH_FRACTION)


def price_factor(size_usd: float, depth_usd: float, side: str) -> float:
    """Factor sobre el precio spot al que se llena una orden de `size_usd`."""
    r = min(size_usd / depth_usd, MAX_IMPACT)
    if side == "buy":
        return 1 + r + (r * r) / 3
    return max(1 - r + (r * r) / 3, 0.05)
