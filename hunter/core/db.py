"""
Capa de persistencia. Todo lo que el sistema detecta se guarda acá,
sin excepción -- incluso las alertas que "no sirvieron" -- porque eso
es exactamente lo que necesitamos para el backtesting de la Fase 3.

Sin este registro, terminamos como los tuits que viste: solo se recuerda
el trade ganador, nunca las 9 alertas que no valieron nada.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config.settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    wallet TEXT NOT NULL,
    token_address TEXT NOT NULL,
    side TEXT NOT NULL,              -- 'buy' o 'sell'
    amount_usd REAL,
    price REAL,
    tx_hash TEXT UNIQUE,
    detected_at TEXT NOT NULL        -- ISO timestamp, UTC
);

CREATE TABLE IF NOT EXISTS stampede_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    wallet_count INTEGER NOT NULL,
    window_seconds INTEGER NOT NULL,
    triggered_at TEXT NOT NULL,
    -- Resultado post-hoc, se completa después para el backtest:
    price_at_alert REAL,
    price_after_5m REAL,
    price_after_30m REAL,
    price_after_1h REAL,
    outcome_checked INTEGER DEFAULT 0,
    token_age_seconds REAL,  -- edad del token al momento de la alerta, ver tabla `tokens`
    -- Cuántas transacciones habíamos visto NOSOTROS MISMOS, antes de esta
    -- alerta, de cada una de las wallets que compraron -- proxy honesto de
    -- "wallet conocida/repetida" vs "wallet nueva", mientras no tengamos
    -- el historial completo que pediría wallet_scorer.py (cash, PnL, días
    -- activo -- eso requeriría una fuente externa o mucho más trabajo).
    avg_wallet_prior_trades REAL,
    min_wallet_prior_trades REAL,
    -- Pico real de wallets distintas que compraron DENTRO de toda la
    -- ventana de 8 min, no solo en el instante del disparo (que hoy
    -- siempre da igual a STAMPEDE_MIN_WALLETS). Sirve para poder
    -- comparar "manadas de 5" vs "de 8" con datos propios más adelante
    -- y decidir si 5 es el número correcto -- ver core/stampede.py.
    peak_wallet_count INTEGER,
    -- Probabilidad que predijo brain.py para esta alerta AL MOMENTO DE
    -- CREARSE (si ya había un modelo entrenado) -- puramente
    -- informativo por ahora, NO filtra ni afecta la compra automática
    -- (ver core/auto_trader.py y el README: el modelo hoy no tiene
    -- señal validada, conectarlo a decisiones reales sería prematuro).
    brain_predicted_prob REAL
);

-- Ciclo de vida de cada token: cuándo se creó y cuándo (si acaso) se
-- graduó de bonding curve a pool líquido. Idea tomada de un análisis
-- externo (tiempo de curva como señal de actividad orgánica vs bot) --
-- no confiamos en SUS números, pero medir esto con nuestros propios
-- datos es gratis y nos deja verificarlo o descartarlo nosotros mismos.
CREATE TABLE IF NOT EXISTS tokens (
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    created_at TEXT,
    graduated_at TEXT,
    -- Wallet que DEPLOYÓ el token -- agregado 2026-09-17 después de ver
    -- (en un stream real de un trader de memecoins) que chequea cuántos
    -- tokens previos de un dev llegaron a graduar antes de comprar
    -- ("3 de 1300 migrados" como red flag inmediata). A diferencia del
    -- win-rate de wallets COMPRADORAS (wallet_stats, depende de si
    -- NOSOTROS ganamos plata), esto es una señal más objetiva: ¿el
    -- token del creador llegó a graduar, sí o no? Verificado con datos
    -- reales de Robinhood Chain (topics[3] de TokenLaunched, no
    -- documentado, confirmado viendo una wallet lanzar 5 tokens en la
    -- misma ventana de bloques) y de Solana (traderPublicKey en el
    -- mensaje "create" de PumpPortal, confirmado en vivo).
    creator TEXT,
    PRIMARY KEY (chain, token_address)
);
-- El índice sobre `creator` NO puede ir acá: en una DB que ya existía
-- antes de este cambio, la tabla `tokens` de arriba (CREATE TABLE IF
-- NOT EXISTS) no toca la tabla real, y la columna `creator` recién se
-- agrega en _migrate() -- crear el índice ACÁ rompía con
-- "no such column: creator" en cualquier DB previa (bug real,
-- encontrado desplegando el 2026-09-17). Se crea al final de
-- _migrate(), después del ALTER TABLE.

CREATE TABLE IF NOT EXISTS wallet_stats (
    wallet TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    trades_closed INTEGER DEFAULT 0,
    trades_won INTEGER DEFAULT 0,
    realized_pnl_usd REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    last_updated TEXT
);

-- Qué wallets EXACTAS causaron cada alerta, guardado en el momento
-- (2026-09-16) -- antes no existía esto, y para el análisis de
-- win-rate por wallet había que reconstruirlo a ojo desde
-- `transactions` buscando compras cerca de `triggered_at` (funciona,
-- pero es una aproximación). Guardarlo directo es preciso y además
-- mucho más rápido de consultar.
CREATE TABLE IF NOT EXISTS alert_wallets (
    alert_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    PRIMARY KEY (alert_id, wallet)
);

-- Posiciones de paper trading: NUNCA plata real, solo simulación para
-- validar la estrategia antes de arriesgar algo (ver core/paper_trading.py).
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    amount_usd REAL NOT NULL,        -- tamaño ORIGINAL simulado de la posición
    entry_price REAL NOT NULL,       -- SOL/ETH por token al "comprar"
    effective_entry_usd REAL,        -- amount_usd ya neto de la comisión de entrada
                                      -- (se cobra UNA sola vez acá, no en cada venta parcial)
    remaining_fraction REAL NOT NULL DEFAULT 1.0,  -- 1.0 = nada vendido, 0 = cerrada del todo
    peak_price REAL,                 -- precio más alto visto desde que se abrió -- para el trailing stop
    opened_at TEXT NOT NULL,
    exit_price REAL,                 -- precio de la última venta (parcial o final)
    closed_at TEXT,                  -- se completa cuando remaining_fraction llega a 0
    pnl_usd REAL,                    -- ACUMULADO de todas las ventas parciales + la final
    status TEXT NOT NULL DEFAULT 'open'  -- 'open' | 'closed'
);

-- Cada venta parcial (toma de ganancias escalonada) o el cierre final
-- quedan acá, uno por evento -- permite auditar CUÁNDO y POR QUÉ se
-- vendió cada tramo, y evita que el auto-trader dispare el mismo
-- nivel de toma de ganancias dos veces (ver get_triggered_exit_reasons).
CREATE TABLE IF NOT EXISTS paper_position_exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    fraction REAL NOT NULL,       -- fracción del tamaño ORIGINAL vendida en este evento
    exit_price REAL NOT NULL,
    multiplier REAL NOT NULL,     -- exit_price / entry_price al momento de esta venta
    pnl_usd REAL NOT NULL,        -- neto de ESTE tramo únicamente
    reason TEXT NOT NULL,         -- 'take_profit_50' | 'take_profit_100' | 'time_exit' | 'manual_close'
    exited_at TEXT NOT NULL
);

-- Configuración chica del dashboard (ej. "hasta cuándo ocultar" al
-- limpiar la vista) -- NUNCA borra ni toca los datos reales, solo
-- guarda desde qué momento mostrar en el navegador. brain.py sigue
-- entrenando con TODO lo que hay en las tablas de arriba, sin importar
-- este filtro visual.
CREATE TABLE IF NOT EXISTS dashboard_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- `transactions` ya tiene 100k+ filas y crece sin parar -- sin estos
-- índices, get_wallet_prior_trade_count() (5 veces por CADA alerta
-- nueva, antes de guardarla) y get_last_transaction_price() (fallback
-- de precio para Robinhood Chain, que DexScreener no indexa -- se
-- llama en CADA apertura y CADA chequeo de 60s de una posición de esa
-- cadena) hacían un table scan completo cada vez. Encontrado el
-- 2026-09-16 analizando por qué las operaciones se sentían lentas --
-- esto solo empeora con el tiempo a medida que la tabla crece, sin un
-- índice. Puramente aditivo, no cambia ningún comportamiento.
CREATE INDEX IF NOT EXISTS idx_transactions_chain_wallet_detected
    ON transactions(chain, wallet, detected_at);
CREATE INDEX IF NOT EXISTS idx_transactions_chain_token_detected
    ON transactions(chain, token_address, detected_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM dashboard_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO dashboard_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@contextmanager
def get_conn():
    # timeout=30: si otra conexión está escribiendo, esperar hasta 30s en vez
    # de fallar a los 5s con "database is locked" (bug real, 2026-09-19: al
    # revisar posiciones cada 5s + dashboard + listeners escribiendo, se
    # perdían aperturas/eventos). synchronous=NORMAL es seguro con WAL.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        # Modo WAL (persistente en el archivo): lectores y un escritor pueden
        # trabajar a la vez sin bloquearse -- el modo por defecto bloquea a
        # todos durante cada escritura.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Migraciones idempotentes -- para DBs que ya existían antes de un
    cambio de esquema (CREATE TABLE IF NOT EXISTS no altera tablas ya
    creadas). Cada ALTER se intenta una vez; si la columna ya existe,
    se ignora el error."""
    migrations = [
        "ALTER TABLE stampede_alerts ADD COLUMN token_age_seconds REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN avg_wallet_prior_trades REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN min_wallet_prior_trades REAL",
        "ALTER TABLE paper_positions ADD COLUMN effective_entry_usd REAL",
        "ALTER TABLE paper_positions ADD COLUMN remaining_fraction REAL NOT NULL DEFAULT 1.0",
        "ALTER TABLE paper_positions ADD COLUMN peak_price REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN peak_wallet_count INTEGER",
        "ALTER TABLE stampede_alerts ADD COLUMN brain_predicted_prob REAL",
        "ALTER TABLE paper_positions ADD COLUMN alert_id INTEGER",
        "ALTER TABLE stampede_alerts ADD COLUMN avg_wallet_win_rate REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN min_wallet_win_rate REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN avg_wallet_buy_usd REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN min_wallet_buy_usd REAL",
        "ALTER TABLE tokens ADD COLUMN creator TEXT",
        "ALTER TABLE stampede_alerts ADD COLUMN creator_tokens_created INTEGER",
        "ALTER TABLE stampede_alerts ADD COLUMN creator_migration_rate REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN early_buy_concentration REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN entry_score REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN entry_decision TEXT",
        "ALTER TABLE stampede_alerts ADD COLUMN chase_ratio REAL",
        "ALTER TABLE stampede_alerts ADD COLUMN wallet_rep_score REAL",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Recién ACÁ existe con certeza la columna `creator` (ya sea porque
    # la tabla se creó de cero arriba, o por el ALTER TABLE de arriba).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_creator ON tokens(chain, creator)")
    # Para buscar el historial de una wallet en alertas pasadas (wallet_rep_score).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_wallets_wallet ON alert_wallets(wallet)")


def insert_transaction(conn, chain, wallet, token_address, side, amount_usd, price, tx_hash):
    conn.execute(
        """INSERT OR IGNORE INTO transactions
           (chain, wallet, token_address, side, amount_usd, price, tx_hash, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chain, wallet, token_address, side, amount_usd, price, tx_hash, now_iso()),
    )


def insert_stampede_alert(conn, chain, token_address, wallet_count, window_seconds,
                           price_at_alert, token_age_seconds=None,
                           avg_wallet_prior_trades=None, min_wallet_prior_trades=None,
                           brain_predicted_prob=None,
                           avg_wallet_win_rate=None, min_wallet_win_rate=None,
                           avg_wallet_buy_usd=None, min_wallet_buy_usd=None,
                           creator_tokens_created=None, creator_migration_rate=None,
                           early_buy_concentration=None) -> int:
    cur = conn.execute(
        """INSERT INTO stampede_alerts
           (chain, token_address, wallet_count, window_seconds, triggered_at,
            price_at_alert, token_age_seconds, avg_wallet_prior_trades, min_wallet_prior_trades,
            peak_wallet_count, brain_predicted_prob, avg_wallet_win_rate, min_wallet_win_rate,
            avg_wallet_buy_usd, min_wallet_buy_usd, creator_tokens_created, creator_migration_rate,
            early_buy_concentration)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chain, token_address, wallet_count, window_seconds, now_iso(),
         price_at_alert, token_age_seconds, avg_wallet_prior_trades, min_wallet_prior_trades,
         wallet_count, brain_predicted_prob, avg_wallet_win_rate, min_wallet_win_rate,
         avg_wallet_buy_usd, min_wallet_buy_usd, creator_tokens_created, creator_migration_rate,
         early_buy_concentration),
    )
    return cur.lastrowid


EARLY_BUY_WINDOW_SECONDS = 60


def get_early_buy_concentration(conn, chain: str, token_address: str, created_at: str | None) -> float | None:
    """Qué fracción del volumen comprado (USD) en los primeros
    EARLY_BUY_WINDOW_SECONDS de vida del token se llevó la wallet que
    MÁS compró -- proxy de "bundled" (dev/insiders acumulando con varias
    wallets propias antes de que entren compradores orgánicos), señal
    real de un trader que estudiamos (2026-09-17). Verificado con datos
    propios antes de usarlo: tokens con >70% de concentración temprana
    tuvieron win-rate de 6.7% contra 16.2% de los no concentrados
    (n=15 vs 173 -- muestra chica para el caso de alta concentración,
    por eso queda informativo, no como filtro). None si no hay
    `created_at` conocido o hay menos de 2 compras tempranas registradas."""
    if not created_at:
        return None
    try:
        created_dt = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return None
    window_end = (created_dt + timedelta(seconds=EARLY_BUY_WINDOW_SECONDS)).isoformat()

    buys = conn.execute(
        """SELECT wallet, amount_usd FROM transactions
           WHERE chain = ? AND token_address = ? AND side = 'buy'
             AND detected_at >= ? AND detected_at <= ?
             AND amount_usd IS NOT NULL""",
        (chain, token_address, created_at, window_end),
    ).fetchall()
    if len(buys) < 2:
        return None

    total_volume = sum(b["amount_usd"] for b in buys)
    if total_volume <= 0:
        return None
    per_wallet: dict[str, float] = {}
    for b in buys:
        per_wallet[b["wallet"]] = per_wallet.get(b["wallet"], 0) + b["amount_usd"]
    return max(per_wallet.values()) / total_volume


def get_wallet_buy_amounts_for_token(conn, chain: str, token_address: str,
                                      wallets: list[str], before_iso: str) -> list[float]:
    """Cuánto (amount_usd) puso cada una de estas wallets al comprar ESTE
    token, hasta el momento de la alerta -- para ver si el TAMAÑO de la
    compra dice algo del resultado (pedido explícito 2026-09-16: se
    encontró con datos reales que compras más grandes correlacionan con
    PEOR resultado, tanto por alerta como por wallet -- ver análisis del
    mismo día). Puramente informativo, ver avg/min_wallet_buy_usd."""
    if not wallets:
        return []
    placeholders = ",".join("?" * len(wallets))
    rows = conn.execute(
        f"""SELECT amount_usd FROM transactions
            WHERE chain = ? AND token_address = ? AND side = 'buy'
              AND wallet IN ({placeholders}) AND detected_at <= ?
              AND amount_usd IS NOT NULL""",
        (chain, token_address, *wallets, before_iso),
    ).fetchall()
    return [r["amount_usd"] for r in rows]


def insert_alert_wallets(conn, alert_id: int, wallets: list[str]):
    conn.executemany(
        "INSERT OR IGNORE INTO alert_wallets (alert_id, wallet) VALUES (?, ?)",
        [(alert_id, w) for w in wallets],
    )


def get_alert_wallets(conn, alert_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT wallet FROM alert_wallets WHERE alert_id = ?", (alert_id,)
    ).fetchall()
    return [r["wallet"] for r in rows]


# Mínimo de manadas en las que vimos a una wallet antes de confiar en su
# win-rate -- mismo criterio de fondo que MIN_TRADES_FOR_EVALUATION en
# wallet_scorer.py (menos muestra = ruido, no señal), pero un piso más
# bajo porque acá el universo de "veces que aparece" es chico incluso
# para wallets activas (una wallet puede estar en pocas manadas de 5,
# aunque opere seguido en general). Ajustar cuando haya más datos
# acumulados para ver dónde se estabiliza de verdad.
MIN_ALERTS_FOR_WALLET_WIN_RATE = 5


def get_wallet_win_rates(conn, chain: str, wallets: list[str]) -> dict[str, float]:
    """Win-rate actual (wallet_stats) de cada wallet dada, SOLO para las
    que ya tienen sample mínimo -- el resto queda afuera del dict
    (tratado como 'no sabemos', no como 0%)."""
    if not wallets:
        return {}
    placeholders = ",".join("?" * len(wallets))
    rows = conn.execute(
        f"""SELECT wallet, win_rate FROM wallet_stats
            WHERE chain = ? AND wallet IN ({placeholders})
              AND trades_closed >= ?""",
        (chain, *wallets, MIN_ALERTS_FOR_WALLET_WIN_RATE),
    ).fetchall()
    return {r["wallet"]: r["win_rate"] for r in rows}


def record_wallet_outcomes(conn, chain: str, wallets: list[str], won: bool, pnl_usd: float):
    """Se llama UNA vez por posición cerrada (ver paper_trading.py), para
    cada wallet que participó en la manada que la causó. `pnl_usd` es el
    PnL TOTAL de la posición (no dividido entre wallets) -- no es "cuánto
    ganó esa wallet con su propia plata" (no tenemos esa info, ver
    wallet_scorer.py), es "el resultado del trade que hicimos siguiendo a
    esta wallet", que es lo que de verdad nos importa para decidir si
    vale la pena seguir a alguien."""
    for wallet in wallets:
        conn.execute(
            """INSERT INTO wallet_stats (wallet, chain, trades_closed, trades_won, realized_pnl_usd, win_rate, last_updated)
               VALUES (?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(wallet) DO UPDATE SET
                   trades_closed = wallet_stats.trades_closed + 1,
                   trades_won = wallet_stats.trades_won + excluded.trades_won,
                   realized_pnl_usd = wallet_stats.realized_pnl_usd + excluded.realized_pnl_usd,
                   win_rate = CAST(wallet_stats.trades_won + excluded.trades_won AS REAL) / (wallet_stats.trades_closed + 1),
                   last_updated = excluded.last_updated""",
            (wallet, chain, 1 if won else 0, pnl_usd, 1.0 if won else 0.0, now_iso()),
        )


def update_alert_entry_decision(conn, alert_id: int, score, decision: str):
    """Guarda qué decidió core/entry_filter.py para esta alerta ('pass' |
    'explore' | 'skip' | 'nofilter') -- se registra TAMBIÉN lo descartado,
    para poder medir en vivo si el filtro realmente sirve."""
    conn.execute(
        "UPDATE stampede_alerts SET entry_score = ?, entry_decision = ? WHERE id = ?",
        (score, decision, alert_id),
    )


def update_alert_chase_ratio(conn, alert_id: int, ratio: float):
    """Precio de llenado / precio de la alerta: cuánto ya había subido el
    precio cuando pudimos comprar. Se guarda también para las entradas que
    se descartan por tardías, así se puede seguir evaluando el umbral."""
    conn.execute("UPDATE stampede_alerts SET chase_ratio = ? WHERE id = ?", (ratio, alert_id))


WALLET_REP_BASE_RATE = 0.094   # % de alertas que suben >=30% a los 30 min (medido 2026-09-19, n=7787)
WALLET_REP_PRIOR_N = 4.0
WALLET_REP_WIN_MULT = 1.3


def get_wallet_rep_score(conn, chain: str, wallets: list[str], alert_id: int) -> float | None:
    """Reputación propia de las wallets de una alerta (idea tomada de FOMO
    Radar, que puntúa wallets por historial, pero calculada con NUESTRAS
    alertas): para cada wallet, (ganadas + base*prior) / (alertas + prior),
    donde "ganada" = el token subió >=30% a los 30 min de esa alerta. Se usan
    solo alertas pasadas con resultado ya medido. Devuelve el promedio entre
    las wallets de la alerta (wallet sin historial = tasa base).

    Validado en walk-forward sobre 7787 alertas (2026-09-19): AUC 0.65 contra
    0.59 del win-rate de wallets y 0.55 del filtro ML; con el score >=0.14 la
    simulación realista dio PnL/trade $0.00 contra -$6.0 del resto (n=143),
    o sea SIN ganancia demostrada. Por eso queda solo como métrica en modo
    sombra (no filtra nada) hasta confirmar con datos v3. Solo Robinhood."""
    if chain != "robinhood" or not wallets:
        return None
    placeholders = ",".join("?" * len(wallets))
    rows = conn.execute(
        f"""SELECT aw.wallet AS wallet, COUNT(*) AS n,
                   SUM(CASE WHEN a.price_after_30m >= ? * a.price_at_alert THEN 1 ELSE 0 END) AS g
            FROM alert_wallets aw JOIN stampede_alerts a ON a.id = aw.alert_id
            WHERE aw.wallet IN ({placeholders}) AND a.chain = ? AND a.id <> ?
              AND a.price_after_30m IS NOT NULL AND a.price_at_alert > 0
            GROUP BY aw.wallet""",
        (WALLET_REP_WIN_MULT, *wallets, chain, alert_id),
    ).fetchall()
    hist = {r["wallet"]: (r["n"], r["g"] or 0) for r in rows}
    scores = []
    for w in wallets:
        n, g = hist.get(w, (0, 0))
        scores.append((g + WALLET_REP_BASE_RATE * WALLET_REP_PRIOR_N) / (n + WALLET_REP_PRIOR_N))
    return sum(scores) / len(scores)


def update_alert_wallet_rep(conn, alert_id: int, score: float | None):
    conn.execute("UPDATE stampede_alerts SET wallet_rep_score = ? WHERE id = ?", (score, alert_id))


def update_alert_peak_wallet_count(conn, alert_id: int, peak_wallet_count: int):
    """Solo sube el pico -- nunca lo baja (el detector poda compradores
    viejos de su propio estado, pero el pico histórico de la alerta ya
    guardada no debería retroceder)."""
    conn.execute(
        """UPDATE stampede_alerts SET peak_wallet_count = ?
           WHERE id = ? AND (peak_wallet_count IS NULL OR peak_wallet_count < ?)""",
        (peak_wallet_count, alert_id, peak_wallet_count),
    )


def get_wallet_prior_trade_count(conn, chain: str, wallet: str, before_iso: str) -> int:
    """Cuántas transacciones vimos de esta wallet ANTES del momento dado --
    ojo, es un piso, no el historial real de la wallet: solo cuenta desde
    que este sistema empezó a mirar la cadena, no desde que la wallet
    existe. Aun así es un dato real medido, no inventado."""
    row = conn.execute(
        """SELECT COUNT(*) c FROM transactions
           WHERE chain = ? AND wallet = ? AND detected_at < ?""",
        (chain, wallet, before_iso),
    ).fetchone()
    return row["c"]


def upsert_token_created(conn, chain: str, token_address: str, created_at: str, creator: str | None = None):
    """Registra cuándo se creó un token -- solo la PRIMERA vez que lo vemos
    (COALESCE preserva el valor existente si ya lo habíamos registrado).
    `creator` (2026-09-17): wallet que deployó el token -- ver comentario
    en SCHEMA sobre por qué importa (historial del dev, no del comprador)."""
    conn.execute(
        """INSERT INTO tokens (chain, token_address, created_at, creator)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chain, token_address) DO UPDATE SET
             created_at = COALESCE(tokens.created_at, excluded.created_at),
             creator = COALESCE(tokens.creator, excluded.creator)""",
        (chain, token_address, created_at, creator),
    )


# Mínimo de tokens lanzados antes de confiar en la tasa de graduación de
# un creador -- mismo criterio que MIN_ALERTS_FOR_WALLET_WIN_RATE (con
# 1 o 2 tokens no hay muestra, es ruido). El streamer que inspiró esto
# miraba "3 de 1300" como red flag; acá el piso es más bajo porque
# recién estamos empezando a acumular este dato (2026-09-17).
MIN_TOKENS_FOR_CREATOR_TRACK_RECORD = 3


def get_creator_track_record(conn, chain: str, creator: str) -> dict | None:
    """Cuántos tokens lanzó este creador y qué fracción llegó a graduar
    (bonding curve -> pool líquido) -- señal objetiva y verificable
    on-chain, a diferencia del win-rate de wallets COMPRADORAS (que
    depende de si NOSOTROS ganamos plata). None si no hay muestra
    mínima todavía (no confundir "no sabemos" con "mal historial")."""
    if not creator:
        return None
    row = conn.execute(
        """SELECT COUNT(*) tokens_created,
                  SUM(CASE WHEN graduated_at IS NOT NULL THEN 1 ELSE 0 END) tokens_migrated
           FROM tokens WHERE chain = ? AND creator = ?""",
        (chain, creator),
    ).fetchone()
    if row is None or row["tokens_created"] < MIN_TOKENS_FOR_CREATOR_TRACK_RECORD:
        return None
    return {
        "tokens_created": row["tokens_created"],
        "tokens_migrated": row["tokens_migrated"] or 0,
        "migration_rate": (row["tokens_migrated"] or 0) / row["tokens_created"],
    }


def upsert_token_graduated(conn, chain: str, token_address: str, graduated_at: str):
    conn.execute(
        """INSERT INTO tokens (chain, token_address, graduated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(chain, token_address) DO UPDATE SET
             graduated_at = excluded.graduated_at""",
        (chain, token_address, graduated_at),
    )


def get_token_created_at(conn, chain: str, token_address: str) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM tokens WHERE chain = ? AND token_address = ?",
        (chain, token_address),
    ).fetchone()
    return row["created_at"] if row else None


def get_token_lifecycle(conn, chain: str, token_address: str):
    return conn.execute(
        "SELECT * FROM tokens WHERE chain = ? AND token_address = ?",
        (chain, token_address),
    ).fetchone()


def get_pending_alerts(conn):
    """Alertas todavía no verificadas del todo (para el job de outcome-tracking de brain.py)."""
    return conn.execute(
        "SELECT * FROM stampede_alerts WHERE outcome_checked = 0"
    ).fetchall()


# Columnas válidas para completar el resultado de una alerta -- whitelist
# explícita porque el nombre de columna se interpola en el SQL de abajo.
_OUTCOME_COLUMNS = {"price_after_5m", "price_after_30m", "price_after_1h"}


def update_alert_price(conn, alert_id: int, column: str, price: float):
    if column not in _OUTCOME_COLUMNS:
        raise ValueError(f"Columna no permitida: {column}")
    conn.execute(
        f"UPDATE stampede_alerts SET {column} = ? WHERE id = ?",
        (price, alert_id),
    )


def mark_outcome_checked(conn, alert_id: int):
    conn.execute(
        "UPDATE stampede_alerts SET outcome_checked = 1 WHERE id = ?",
        (alert_id,),
    )


def open_paper_position(conn, chain, token_address, amount_usd, entry_price,
                         effective_entry_usd=None, alert_id=None) -> int:
    cur = conn.execute(
        """INSERT INTO paper_positions
           (chain, token_address, amount_usd, entry_price, effective_entry_usd,
            remaining_fraction, peak_price, opened_at, status, alert_id)
           VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, 'open', ?)""",
        (chain, token_address, amount_usd, entry_price, effective_entry_usd, entry_price, now_iso(), alert_id),
    )
    return cur.lastrowid


def update_peak_price(conn, position_id: int, current_price: float):
    """Actualiza el máximo histórico de precio de la posición SOLO si
    current_price es más alto -- usado por el trailing stop para saber
    cuánto cayó desde el pico (ver core/auto_trader.py)."""
    conn.execute(
        """UPDATE paper_positions
           SET peak_price = MAX(COALESCE(peak_price, 0), ?)
           WHERE id = ?""",
        (current_price, position_id),
    )


def get_triggered_exit_reasons(conn, position_id: int) -> set:
    """Qué niveles de toma de ganancias (o el cierre final) ya se
    ejecutaron para esta posición -- evita que el auto-trader dispare
    el mismo nivel dos veces mientras el precio se queda arriba del
    umbral por varios ciclos seguidos."""
    rows = conn.execute(
        "SELECT reason FROM paper_position_exits WHERE position_id = ?", (position_id,)
    ).fetchall()
    return {r["reason"] for r in rows}


def record_partial_exit(conn, position_id: int, fraction: float, exit_price: float,
                         multiplier: float, pnl_usd: float, reason: str):
    """Vende una FRACCIÓN del tamaño original de la posición. Si con esta
    venta no queda nada (remaining_fraction <= 0), la posición pasa a
    'closed' automáticamente -- así 'vender todo' y 'vender una parte'
    son el mismo camino de código, no dos casos separados."""
    conn.execute(
        """INSERT INTO paper_position_exits
           (position_id, fraction, exit_price, multiplier, pnl_usd, reason, exited_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (position_id, fraction, exit_price, multiplier, pnl_usd, reason, now_iso()),
    )
    conn.execute(
        """UPDATE paper_positions
           SET remaining_fraction = remaining_fraction - ?,
               pnl_usd = COALESCE(pnl_usd, 0) + ?,
               exit_price = ?
           WHERE id = ?""",
        (fraction, pnl_usd, exit_price, position_id),
    )
    row = conn.execute(
        "SELECT remaining_fraction, chain, alert_id, pnl_usd FROM paper_positions WHERE id = ?", (position_id,)
    ).fetchone()
    if row and row["remaining_fraction"] <= 1e-9:
        conn.execute(
            "UPDATE paper_positions SET status = 'closed', closed_at = ? WHERE id = ?",
            (now_iso(), position_id),
        )
        # Recién ACÁ se sabe el PnL final de la posición -- momento
        # correcto para actualizar el win-rate de cada wallet que
        # participó en la manada que la causó (ver record_wallet_outcomes
        # y core/wallet_scorer.py para el porqué de rastrear esto).
        # alert_id puede ser None en posiciones abiertas ANTES de este
        # cambio (2026-09-16) -- se saltea, no se rompe.
        if row["alert_id"] is not None:
            wallets = get_alert_wallets(conn, row["alert_id"])
            if wallets:
                final_pnl = row["pnl_usd"] if row["pnl_usd"] is not None else 0.0
                record_wallet_outcomes(conn, row["chain"], wallets, won=final_pnl > 0, pnl_usd=final_pnl)


def get_position_exits(conn, position_id: int):
    return conn.execute(
        "SELECT * FROM paper_position_exits WHERE position_id = ? ORDER BY exited_at",
        (position_id,),
    ).fetchall()


def has_recent_or_open_position(conn, chain: str, token_address: str, since_iso: str) -> bool:
    """True si ya tenemos una posición abierta de este token, o si
    cerramos una hace menos de `since_iso` -- para no comprar el mismo
    token de nuevo apenas se dispara otra alerta sobre él (visto en
    datos reales: la misma manada repitiéndose varias veces sobre el
    mismo token desperdiciaba posiciones nuevas en algo que ya sabíamos
    cómo venía)."""
    row = conn.execute(
        """SELECT 1 FROM paper_positions
           WHERE chain = ? AND token_address = ?
             AND (status = 'open' OR closed_at > ?)
           LIMIT 1""",
        (chain, token_address, since_iso),
    ).fetchone()
    return row is not None


def get_paper_position(conn, position_id: int):
    return conn.execute(
        "SELECT * FROM paper_positions WHERE id = ?", (position_id,)
    ).fetchone()


def get_paper_positions(conn, limit: int = 100):
    return conn.execute(
        "SELECT * FROM paper_positions ORDER BY opened_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_open_paper_positions(conn):
    """Todas las posiciones abiertas, SIN límite -- a diferencia de
    get_paper_positions() (pensada para mostrar en el dashboard, corta
    en 100), el auto-trader necesita ver TODAS las abiertas para poder
    cerrarlas a tiempo, sin importar cuántas se acumulen con el tiempo."""
    return conn.execute(
        "SELECT * FROM paper_positions WHERE status = 'open'"
    ).fetchall()


def get_last_transaction(conn, chain: str, token_address: str):
    """Última transacción (con precio y timestamp) que vimos on-chain de este token.

    Fallback para cuando una fuente externa (DexScreener) no tiene la
    cadena indexada todavía -- ej. Robinhood Chain, lanzada hace solo
    2 meses. Es un precio real, medido por el propio sistema, no
    inventado -- pero el timestamp importa: si es de ANTES de que
    abriéramos una posición, no es una "actualización" real, es el
    mismo dato viejo (ver dashboard.py::_with_unrealized_pnl).
    """
    return conn.execute(
        """SELECT price, detected_at FROM transactions
           WHERE chain = ? AND token_address = ? AND price IS NOT NULL
           ORDER BY detected_at DESC LIMIT 1""",
        (chain, token_address),
    ).fetchone()


def get_last_transaction_price(conn, chain: str, token_address: str) -> float | None:
    """Atajo sobre get_last_transaction() para cuando solo hace falta el precio."""
    row = get_last_transaction(conn, chain, token_address)
    return row["price"] if row else None
