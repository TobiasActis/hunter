"""
Prueba rápida del detector de manadas con datos simulados.
Esto nos deja validar la LÓGICA antes de conectarla a datos on-chain
reales (que todavía necesitan el parser de logs de Solana, ver el
comentario en chains/solana.py).

Correr con: python test_stampede.py
"""
from chains.base import SwapEvent
from core.stampede import StampedeDetector

detector = StampedeDetector()

TOKEN = "SoMeMemeCoinAddress111"

# Simulamos 6 wallets distintas comprando el mismo token -> debería
# disparar la alerta en la wallet número 5 (umbral por defecto).
wallets = [f"Wallet{i}" for i in range(1, 7)]

for i, wallet in enumerate(wallets, start=1):
    event = SwapEvent(
        chain="solana",
        wallet=wallet,
        token_address=TOKEN,
        side="buy",
        amount_usd=150.0,
        price=0.00042,
        tx_hash=f"tx_{i}",
    )
    result = detector.process(event)
    status = "🚨 ALERTA" if result else "  (sin alerta todavía)"
    print(f"Compra #{i} ({wallet}): {status}")
    if result:
        print(f"   -> {result}")

print("\n--- Test de filtro MIN_TRADE_USD ---")
small_event = SwapEvent(
    chain="solana", wallet="WalletSmall", token_address="OtroToken",
    side="buy", amount_usd=5.0, price=0.001, tx_hash="tx_small",
)
result = detector.process(small_event)
print(f"Compra de $5 (debería ser ignorada): {'IGNORADA correctamente' if result is None else 'ERROR: no se filtró'}")
