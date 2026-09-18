"""
Punto de entrada para (re)entrenar el modelo de core/brain.py.

Por qué este archivo existe y no se corre core/brain.py directamente
(descubierto en producción, 2026-09-16): cuando un módulo se ejecuta
como script principal (`python core/brain.py` o `python -m core.brain`,
da igual), Python le pone __module__ = "__main__" a las clases
definidas ahí -- incluyendo TrainedModel. joblib.dump() graba ese
"__main__" en el pickle. Pero main.py es OTRO proceso, con SU PROPIO
"__main__" (main.py, no brain.py) -- al tratar de cargar el modelo
ahí, joblib/pickle busca "TrainedModel" en el __main__ de main.py, no
lo encuentra, y explota con AttributeError. Esto rompía la detección
de alertas en vivo: el error pasaba ANTES de insert_stampede_alert()
en main.py, así que cada alerta nueva se perdía y el listener de
robinhood entraba en loop de reinicio (ver journalctl del 2026-09-16).

La solución real es que el módulo que DEFINE TrainedModel nunca se
ejecute como "__main__" -- que siempre se IMPORTE. Por eso este
archivo, que solo importa y llama a train(), es el que hay que correr
(y el que usa hunter-brain.service) en vez de core/brain.py directo.
"""
import logging

from core.brain import train
from core.entry_filter import train as train_entry_filter

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Filtro de entrada aprendido de los datos propios (2026-09-18) --
    # se reentrena a diario junto con brain.py, con todo lo acumulado.
    train_entry_filter()
    result = train()
    if result is None:
        print("\n⏳ Todavía no hay suficientes datos. Esto es esperable el primer día --")
        print("   volvé a correr esto después de que el listener junte datos reales.")
