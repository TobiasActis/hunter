"""
Configuración central de HUNTER.
Ajustá estos valores según tu estrategia y las cadenas que quieras monitorear.
"""
import os

# --- RPC / Data providers ---
# Solana: Helius tiene un free tier decente (100k requests/día) y soporta
# WebSocket enhanced transactions, que es justo lo que necesitamos.
# Registrate gratis en https://helius.dev y pegá tu API key acá.
SOLANA_RPC_HTTP = "https://api.mainnet-beta.solana.com"  # público, para arrancar sin API key
SOLANA_RPC_WS = "wss://api.mainnet-beta.solana.com"
HELIUS_API_KEY = ""  # opcional pero MUY recomendado para producción (el público rate-limitea rápido)

# Robinhood Chain (L2 tipo Arbitrum Orbit, lanzada julio 2026). Ahí vive
# Pons, uno de varios launchpads de memecoins -- confirmado con datos
# reales el 2026-09-13 (ver chains/robinhood.py). El RPC público NO
# soporta WebSocket real (el "feed" es el sequencer feed crudo de
# Arbitrum, no eth_subscribe), así que el listener hace polling HTTP.
ROBINHOOD_RPC_HTTP = "https://rpc.mainnet.chain.robinhood.com"  # público, rate-limitado

# --- Telegram para alertas ---
TELEGRAM_BOT_TOKEN = ""  # creá un bot con @BotFather en Telegram
TELEGRAM_CHAT_ID = ""    # tu chat id (te lo puede dar @userinfobot)

# --- Wallets a rastrear (smart money) ---
# Arrancamos vacío a propósito: en el próximo paso construimos el módulo
# que las descubre automáticamente por win rate on-chain, en vez de
# copiar una lista de Twitter a ciegas.
TRACKED_WALLETS = [
    # "AbCdEf123...",  # ejemplo: dirección de wallet en Solana
]

# --- Parámetros de detección de "manada" ---
STAMPEDE_MIN_WALLETS = 5       # mínimo de wallets distintas comprando el mismo token
STAMPEDE_WINDOW_SECONDS = 480  # ventana de tiempo (8 min, como en el tweet que citaste)
MIN_TRADE_USD = 50             # ignorar operaciones menores a esto (filtra "ruido" de wallets de prueba)
IGNORE_SINGLE_WALLET_REPEATS = True  # una wallet comprando 9 veces no cuenta como "manada"

# --- Modo de operación ---
# "alert_only": solo notifica (recomendado para empezar)
# "paper": simula compras/ventas sin plata real, para backtesting en vivo
# "live": ejecuta operaciones reales -- NO ACTIVAR hasta validar con paper trading por semanas
MODE = "alert_only"

# --- Base de datos local (SQLite, simple y suficiente para empezar) ---
DB_PATH = "data/hunter.db"

# --- Dashboard (ver dashboard.py) ---
# Usuario/clave para acceder cuando el dashboard queda expuesto a
# internet (ej. corriendo en un VPS) -- ver deploy/README.md. Si el
# dashboard corre solo en localhost (127.0.0.1, default), esto no hace
# falta, pero igual queda activo por si algún día se cambia el bind.
# Se leen de variables de entorno (NUNCA hardcodeadas acá) -- este
# archivo va a un repo git, y una vez que un secreto queda en el
# historial de git, sigue ahí aunque lo borres del archivo actual.
# En el servidor, el valor real vive en /home/ubuntu/hunter/.env (fuera
# de git, ver deploy/hunter.service.template) -- en tu PC, si corrés el
# dashboard localmente, seteala en tu entorno o en un .env local
# (también fuera de git, ver .env.example).
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "tobias")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme-set-DASHBOARD_PASSWORD-env-var")
# "127.0.0.1" = solo accesible por túnel SSH (más seguro, default para
# correr en tu PC). "0.0.0.0" = accesible desde internet en la IP
# pública del servidor -- solo poné esto si YA tenés el usuario/clave
# de arriba activos, si no cualquiera puede tocar los botones.
DASHBOARD_HOST = "0.0.0.0"

# --- Simulación "como si fuera real" (paper trading, 2026-09-18) ---
# Todo sigue siendo 100% simulado (MODE = "alert_only", nunca se firma
# nada), pero ahora se comporta como una cuenta real: capital limitado,
# tamaño de posición realista, slippage medido de la curva (ver
# core/slippage.py), comisiones de la cadena y latencia de ejecución.
SIM_BANKROLL_USD = 2000.0       # capital simulado total
SIM_POSITION_USD = 50.0         # tamaño por posición (2.5% del capital)
SIM_ENTRY_LATENCY_S = 2.0       # segundos entre la alerta y el llenado de la compra
SIM_EXIT_LATENCY_S = 1.5        # segundos entre la decisión de vender y el llenado
