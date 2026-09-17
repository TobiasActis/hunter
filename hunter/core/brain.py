"""
El "cerebro" del sistema: un clasificador de machine learning clásico
que aprende, a partir de nuestros propios datos históricos, qué
combinación de señales predice mejor un ganador de 6.5x+ (nuestro
breakeven real, calculado en ev_calculator.py).

Por qué esto y no un LLM/ChatGPT:
- Es 100% gratis, corre en tu máquina, sin costo por llamada a una API
- Está hecho específicamente para este tipo de problema (clasificación
  a partir de features numéricas), a diferencia de un LLM de propósito
  general
- Es interpretable: podemos ver EXACTAMENTE qué features pesan más en
  la decisión, en vez de una caja negra

Requiere: pip install scikit-learn pandas --break-system-packages

IMPORTANTE: este archivo no sirve de nada hasta que tengamos datos
reales acumulados en data/hunter.db (mínimo sugerido: 100-200 alertas
de "manada" con su resultado a 1h ya verificado). Por eso el próximo
paso work es dejar el listener de PumpPortal corriendo en tu VPS
durante 2-3 semanas ANTES de entrenar nada.

CONECTADO A main.py DESDE 2026-09-16, PERO SOLO DE FORMA INFORMATIVA:
cada vez que se entrena acá (el timer systemd `hunter-brain.timer`
corre esto una vez por día) se guarda el modelo en MODEL_PATH.
`main.py` lo carga y calcula una predicción para cada alerta NUEVA,
que queda guardada en `stampede_alerts.brain_predicted_prob` -- pero
esa predicción NO decide si se compra o no. Deliberado: hoy el modelo
no tiene ninguna señal validada (dio AUC 0.500, puro azar, la primera
vez que lo corrimos con datos reales -- ver README). Conectarlo a
decisiones reales de compra con un modelo sin validar sería la misma
"corazonada disfrazada de dato" que este proyecto existe para evitar.
Primero hay que acumular semanas de `brain_predicted_prob` guardado
junto al resultado real de cada alerta, y RECIÉN si se confirma que
predice algo, usarlo para filtrar compras.
"""
import logging
import os
import sqlite3
from dataclasses import dataclass

import joblib
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score

from config.settings import DB_PATH

logger = logging.getLogger("hunter.brain")

# Definimos "éxito" como: el precio 1h después de la alerta es >= 6.5x
# el precio al momento de la alerta (nuestro breakeven real calculado
# con comisiones incluidas, no un número inventado).
SUCCESS_MULTIPLIER = 6.5

MODEL_PATH = "data/brain_model.joblib"


@dataclass
class TrainedModel:
    model: GradientBoostingClassifier
    feature_names: list[str]
    train_auc: float
    test_auc: float
    n_samples: int


def load_training_data() -> pd.DataFrame:
    """
    Arma el dataset de entrenamiento a partir de las alertas de manada
    ya registradas y sus resultados de precio verificados.
    """
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT * FROM stampede_alerts
        WHERE outcome_checked = 1
          AND price_at_alert IS NOT NULL
          AND price_after_1h IS NOT NULL
        """,
        conn,
    )
    conn.close()

    if df.empty:
        return df

    df["success"] = (df["price_after_1h"] / df["price_at_alert"]) >= SUCCESS_MULTIPLIER
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    IMPORTANTE (2026-09-16): wallet_count y window_seconds son
    CONSTANTES para toda alerta que generamos (siempre disparan en
    exactamente STAMPEDE_MIN_WALLETS wallets, dentro de la misma
    ventana) -- lo confirmamos corriendo esto con datos reales: el
    modelo daba AUC 0.500 (puro azar) porque no había NADA de
    varianza para aprender. Las columnas de abajo son el primer
    intento de darle al modelo algo que sí varía de alerta a alerta.

    token_age_seconds: edad del token al momento de la alerta (ver
    core/token_lifecycle_solana.py y chains/robinhood.py). -1 si no
    lo sabíamos todavía (sentinel explícito, no lo confundimos con "0
    segundos" que sería un dato real).

    avg/min_wallet_prior_trades: cuántas transacciones habíamos visto
    NOSOTROS MISMOS de las wallets compradoras, antes de esta alerta --
    proxy de wallet "conocida" vs "nueva". Ojo: es un piso, no el
    historial real de la wallet (solo mide desde que este sistema
    empezó a correr) -- el reemplazo completo sería wallet_scorer.py
    con datos de cash/PnL/días activo, que hoy no tenemos sin pagar
    una fuente externa (ver README).
    """
    features = pd.DataFrame()
    features["wallet_count"] = df["wallet_count"]
    features["window_seconds"] = df["window_seconds"]
    for col in ("token_age_seconds", "avg_wallet_prior_trades", "min_wallet_prior_trades"):
        features[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(-1)
    return features


def train(min_samples: int = 100) -> TrainedModel | None:
    df = load_training_data()

    if len(df) < min_samples:
        logger.warning(
            f"Solo {len(df)} alertas con resultado verificado -- "
            f"necesitamos al menos {min_samples} para que entrenar tenga sentido "
            f"estadístico. Seguí corriendo el listener y volvé a intentar en unos días."
        )
        return None

    X = build_features(df)
    y = df["success"].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y
    )

    model = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.05, random_state=42
    )
    model.fit(X_train, y_train)

    train_auc = roc_auc_score(y_train, model.predict_proba(X_train)[:, 1])
    test_auc = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])

    logger.info(f"Entrenado con {len(df)} muestras. AUC train={train_auc:.3f} test={test_auc:.3f}")
    logger.info("\n" + classification_report(y_test, model.predict(X_test)))

    # Importancia de cada feature -- esto es la parte "interpretable"
    # que un LLM no te da gratis
    importances = dict(zip(X.columns, model.feature_importances_))
    logger.info(f"Importancia de features: {importances}")

    result = TrainedModel(
        model=model,
        feature_names=list(X.columns),
        train_auc=train_auc,
        test_auc=test_auc,
        n_samples=len(df),
    )

    joblib.dump(result, MODEL_PATH)
    logger.info(f"Modelo guardado en {MODEL_PATH} -- main.py lo va a usar para predicciones informativas.")

    return result


_cached_model: TrainedModel | None = None
_cached_model_mtime: float | None = None


def get_cached_model() -> TrainedModel | None:
    """
    Carga el último modelo guardado por train() -- con cache en memoria
    que se invalida solo si el archivo cambió (comparando mtime), así
    main.py (un proceso Python separado y de larga duración) recoge
    automáticamente el resultado del reentrenamiento diario
    (hunter-brain.timer, un proceso DISTINTO) sin necesitar reiniciarse.
    None si todavía no se entrenó nunca (no hay archivo).
    """
    global _cached_model, _cached_model_mtime
    if not os.path.exists(MODEL_PATH):
        return None
    mtime = os.path.getmtime(MODEL_PATH)
    if _cached_model is None or mtime != _cached_model_mtime:
        _cached_model = joblib.load(MODEL_PATH)
        _cached_model_mtime = mtime
        logger.info(f"Modelo recargado desde {MODEL_PATH} (entrenado con {_cached_model.n_samples} muestras).")
    return _cached_model


def predict_probability(
    model: TrainedModel, wallet_count: int, window_seconds: int,
    token_age_seconds: float | None, avg_wallet_prior_trades: float | None,
    min_wallet_prior_trades: float | None,
) -> float:
    """
    Probabilidad (0-1) de que ESTA alerta llegue a SUCCESS_MULTIPLIER
    según el modelo -- mismo criterio de sentinel (-1 para "no sabemos")
    que build_features(), para que una predicción en vivo sea
    consistente con cómo se entrenó el modelo.
    """
    row = {
        "wallet_count": wallet_count,
        "window_seconds": window_seconds,
        "token_age_seconds": token_age_seconds if token_age_seconds is not None else -1,
        "avg_wallet_prior_trades": avg_wallet_prior_trades if avg_wallet_prior_trades is not None else -1,
        "min_wallet_prior_trades": min_wallet_prior_trades if min_wallet_prior_trades is not None else -1,
    }
    X = pd.DataFrame([row])[model.feature_names]  # mismo orden de columnas que en el entrenamiento
    return float(model.model.predict_proba(X)[0, 1])


# NO agregar un "if __name__ == '__main__': train()" acá -- correr
# este archivo directamente hace que TrainedModel se pickle con
# __module__="__main__", que rompe al cargarlo desde main.py (bug real
# de producción, 2026-09-16, ver train_brain.py en la raíz del
# proyecto). Para (re)entrenar, correr train_brain.py.
