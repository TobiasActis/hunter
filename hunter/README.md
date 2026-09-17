# HUNTER — Sistema de detección de "manadas" en memecoins

## Contexto (leer antes de tocar código)

Este proyecto nació de una conversación larga evaluando si se puede
"ganarle" al trading de memecoins copiando perfiles de fomo.family o
usando bots gratis. La conclusión honesta a la que llegamos: no hay
atajo mágico, pero SÍ podemos construir un sistema que reduce las
decisiones estúpidas (copiar wallets sin gestión de riesgo, caer en
marketing de Twitter, operar sin backtesting) usando datos reales
en vez de tuits.

**Filosofía del proyecto: nada se asume, todo se mide.**
- El detector de manadas está probado con `test_stampede.py` (funciona)
- El scorer de wallets fue validado contra un caso real (rechazó
  correctamente a una wallet con $39 de cash y 73% de posiciones en
  rojo — ver `core/wallet_scorer.py::evaluate_teka088_example`)
- La calculadora de EV (`core/ev_calculator.py`) probó que la
  estrategia "$50 en 5 posiciones de $10, 4 pierden todo, 1 gana 20x"
  SÍ da positivo (breakeven real: 6.5x, no 20x) una vez que se
  incluyen comisiones (~1% + slippage ~3% en cada punta)
- El "cerebro" (`core/brain.py`) es un clasificador de scikit-learn
  entrenado con datos propios acumulados — deliberadamente NO es un
  LLM/ChatGPT, porque los papers académicos (StockBench, 2025)
  muestran que los LLMs no le ganan a buy-and-hold en trading

## Estado actual — qué falta

1. **`chains/solana_pumpportal.py` se probó parcialmente contra el
   servidor real (2026-09-13).** La conexión WebSocket funciona,
   `subscribeNewToken` es gratis y confirmado (llegan mensajes reales
   con `txType="create"`, que el código ya filtra bien). PERO:
   `subscribeTokenTrade` y `subscribeAccountTrade` (necesarios para
   ver compras/ventas reales y para el modo dirigido con
   `TRACKED_WALLETS`) devolvieron este error del servidor sin API key:
   *"only available when connecting with an API key funded with at
   least 0.02 SOL"*. Esto NO es lo que decía la documentación original
   que se usó para escribir el código (se asumía costo marginal por
   evento, no un gate de entrada). Falta: conseguir una API key de
   PumpPortal con SOL cargado y volver a probar para validar
   finalmente el formato de `_parse_message()` en mensajes `buy`/`sell`
   reales (ver detalle en el docstring del archivo).

2. ~~`chains/solana.py` incompleto a propósito~~ **IMPLEMENTADO Y
   VALIDADO contra transacciones reales (2026-09-13).** Ahora que
   PumpPortal bloqueó los swaps sin pago (ver punto 1), este pasó a
   ser el listener activo por defecto en `main.py` -- es 100% gratis.
   Usa `logsSubscribe` (RPC público) + `getTransaction` (HTTP,
   jsonParsed) para reconstruir wallet/token/side/monto a partir de
   los balances pre/post, en vez de parsear texto de log a mano.
   Confirmado con una venta real: detectó correctamente
   `wallet=BR8jH6i...`, `token=7ZcEixm...pump`, `side=sell`.

   **Limitación real encontrada, no teórica:** el RPC público
   rate-limitea fuerte -- en modo descubrimiento (`TRACKED_WALLETS`
   vacío, mirando TODO el flujo de Pump.fun) tira `429` a los ~10
   segundos de arrancar. Ya tiene un backoff de 5s para no
   bombardearlo, pero eso significa que en modo descubrimiento se
   van a perder swaps mientras el RPC público esté saturado. Dos
   salidas, ninguna necesita invertir plata:
     - Modo dirigido: completar `TRACKED_WALLETS` con pocas wallets
       específicas en vez de dejar la lista vacía -- baja mucho el
       volumen de calls.
     - Conseguir una API key de Helius gratis (solo registro por
       email, sin SOL) y cambiar `SOLANA_RPC_WS`/`SOLANA_RPC_HTTP`
       en `config/settings.py` -- su RPC estándar (no el WebSocket
       "enhanced", que sí es pago) tiene límites mucho más altos.

3. **`core/brain.py` todavía no tiene datos para entrenar** (eso no
   cambia hasta acumular alertas reales corriendo en producción), pero
   el job que faltaba **ya está implementado y probado (2026-09-13)**:
   `core/outcome_tracker.py` corre en segundo plano junto a los
   listeners (`main.py`), revisa cada 60s las alertas con
   `outcome_checked = 0` y completa `price_after_5m/30m/1h` usando
   `core/token_price.py` (API pública de DexScreener, gratis, sin key).
   Probado end-to-end: insertando una alerta de prueba con
   `triggered_at` de hace 2 horas, completó las 3 columnas con el
   precio real de un token de Pump.fun y marcó `outcome_checked = 1`
   correctamente. Ahora solo falta tiempo: dejarlo corriendo en
   producción para acumular las ~100-200 alertas reales.

4. ~~Falta el precio de SOL/USD en tiempo real~~ **RESUELTO
   (2026-09-13):** `core/sol_price.py` consulta la API pública de
   CoinGecko (gratis, sin key), cachea el valor y lo refresca cada 30s
   en segundo plano (`main.py` lo arranca junto a los listeners).
   `chains/solana_pumpportal.py` ya multiplica `solAmount` por ese
   precio para completar `amount_usd`. Probado contra la API real:
   devolvió $100.28 el día que se implementó. Si CoinGecko rate-limita
   en producción, la alternativa documentada en el propio archivo es
   el endpoint de Binance.

5. **Dashboard + paper trading, agregado y probado (2026-09-13).**
   `dashboard.py` (FastAPI, corre como tarea de fondo dentro de
   `main.py`, mismo proceso) sirve en http://127.0.0.1:8000 una vista
   de solo-refresco-automático (cada 3s) con: precio SOL/USD, alertas
   de manada, transacciones, y posiciones de paper trading. Los
   botones "Comprar"/"Cerrar" abren/cierran posiciones SIMULADAS
   (`core/paper_trading.py`) al precio real de mercado (DexScreener),
   calculando el PnL neto con el mismo modelo de fees/slippage de
   `core/ev_calculator.py` -- nunca firma una transacción real.
   Probado end-to-end: abrir + cerrar una posición del mismo token dio
   PnL negativo esperado (-$2.46 sobre $10) solo por comisiones, igual
   que predice `ev_calculator.py`. Todavía no hay UI para ejecución
   real -- eso queda para después de validar con paper trading, tal
   como pide la regla de MODE de abajo.

6. **`chains/robinhood.py` -- nueva cadena, implementada y validada con
   datos reales (2026-09-13).** Robinhood Chain (L2 tipo Arbitrum
   Orbit, lanzada julio 2026) tiene su propio ecosistema de launchpads
   de memecoins -- se implementó el listener para Pons (uno de varios:
   Memecoin.fun, Flap, Noxa también existen, pero Pons tiene el
   factory contract más fácil de verificar). Validado paso a paso
   contra el RPC real antes de escribir el parser final:
     - Factory de Pons (`0x7ed598bc...`) confirmado con bytecode
       desplegado y 1603 eventos en ~50k bloques.
     - Evento `TokenLaunched`: 95 lanzamientos reales en solo 2000
       bloques -- hay mucha actividad de creación acá.
     - Eventos `CurveBuy`/`CurveSell` decodificados y verificados
       contra transacciones reales (montos, fees, todo cuadra).
   El WebSocket público (`wss://feed.mainnet.chain.robinhood.com`) NO
   sirve -- se probó en vivo y es el sequencer feed crudo de Arbitrum
   (binario, para sincronizar nodos), no `eth_subscribe`. El listener
   usa polling HTTP (`eth_getLogs` cada 5s) en cambio -- sigue siendo
   100% gratis, solo menos inmediato que un push real.
   **Primera alerta de manada real detectada en Robinhood Chain
   corriendo el sistema completo:** 5 wallets distintas comprando
   `0x7cdb1384...` en la ventana de 8 minutos, pipeline completo
   (detección → DB → notificación) funcionando de punta a punta.

7. **Auto-trader de paper trading, agregado y probado (2026-09-13).**
   Antes, abrir/cerrar posiciones simuladas requería un click manual
   en el dashboard -- si nadie estaba mirando, se perdían alertas.
   `core/auto_trader.py` corre como tarea de fondo (`main.py`) y:
     - Abre una posición paper de $10 automáticamente apenas se
       detecta CUALQUIER alerta de manada (sin esperar un click).
     - Cierra cada posición automáticamente 1 hora después de
       abierta -- la misma ventana que ya usa `core/outcome_tracker.py`
       para medir "éxito" en `brain.py` (6.5x), para que los
       resultados sean comparables.
   Sigue siendo 100% simulado -- nunca firma una transacción real.
   Probado end-to-end: se simuló una alerta real (5 wallets) y una
   posición "vieja" forzada a 2 horas -- la primera se auto-abrió
   correctamente, la segunda se auto-cerró con PnL real calculado
   (+$3.20 en el test). Con esto el sistema puede correr sin
   supervisión (por ejemplo, con la compu prendida mientras no estás)
   y no se pierde ninguna oportunidad de juntar datos para `brain.py`.

8. **Edad del token al momento de la alerta, agregado y probado
   (2026-09-15).** Idea que salió de revisar investigación externa
   (un hilo sobre "tiempo de bonding curve" como señal de actividad
   orgánica vs bot) -- el hilo en cuestión terminaba promocionando su
   propia moneda, así que NO nos creemos sus números (1.5x/2.4x), pero
   la idea de medir esto con datos propios es gratis y verificable.
     - Nueva tabla `tokens` (`chain`, `token_address`, `created_at`,
       `graduated_at`) en `core/db.py`, con migración automática para
       no perder las alertas ya guardadas.
     - `core/token_lifecycle_solana.py` (nuevo): usa PumpPortal
       `subscribeNewToken` + `subscribeMigration` -- ambas CONFIRMADAS
       gratis en vivo el 2026-09-15 (a diferencia de subscribeTokenTrade/
       subscribeAccountTrade, que siguen bloqueadas sin pagar). Formato
       real de un mensaje de migración verificado:
       `{"mint": "...", "txType": "migrate", "pool": "pump-amm"}`.
     - `chains/robinhood.py` ahora también registra `TokenLaunched`
       (creación) y el nuevo evento `PoolGraduated` (graduación) --
       topic0 calculado con keccak256 y verificado contra un evento
       real en cadena (`0x0a44ef75...`, 1 graduación real encontrada en
       20000 bloques).
     - `stampede_alerts.token_age_seconds` se completa automáticamente
       si ya vimos la creación del token; `None` si no (nunca se
       inventa un valor). Probado end-to-end con un token simulado
       "creado" 10 minutos antes de la alerta -- dio 600.5s, correcto.
   Esto es solo instrumentación todavía -- no filtra ni afecta ninguna
   alerta ni compra. La idea es acumular semanas de este dato real y
   recién ahí ver si correlaciona con qué alertas ganan, en vez de
   asumir que el hilo de Twitter tenía razón.

9. **Primera revisión completa del sistema en producción (2026-09-16) --
   resultados reales, no proyectados.** Con el sistema corriendo un par
   de días sin supervisión:
     - **24,300 transacciones** detectadas (Robinhood Chain domina por
       lejos: 23,609 vs 691 de Solana -- confirma el gap de RPC público
       que ya sabíamos).
     - **185 alertas de manada**, **177 ya con resultado verificado a
       1h** -- cruzamos el mínimo de 100 que pedía `brain.py`.
     - **Paper trading: -$14.23 neto en 180 posiciones cerradas**
       (~breakeven sobre ~$1800 simulados), win rate 23.9%. Un solo
       trade (+$207) sostiene casi todo el libro -- el patrón de
       "muchas pérdidas chicas, una ganadora rara" que veníamos
       discutiendo desde el principio, confirmado con datos reales.
     - **Solo 2 de 177 alertas (1.1%) llegaron al 6.5x de breakeven
       real.** La señal cruda ("5 wallets compraron") sin ningún
       filtro de calidad no alcanza -- otro dato que confirma por qué
       hace falta conectar wallet_scorer.py o algo parecido.
     - **`brain.py` corrió por primera vez con datos reales** (instalé
       `scikit-learn`/`pandas`, faltaban en `requirements.txt`) -- dio
       **AUC 0.500 exacto (puro azar)**. Causa encontrada, no
       misteriosa: `wallet_count` y `window_seconds` son CONSTANTES en
       todas las alertas (siempre disparan en exactamente 5 wallets/480s),
       cero varianza para que el modelo aprenda algo.
     - En consecuencia, agregué `avg_wallet_prior_trades` y
       `min_wallet_prior_trades` a `stampede_alerts`: cuántas
       transacciones habíamos visto NOSOTROS MISMOS de cada wallet
       compradora antes de esta alerta (proxy de wallet "conocida" vs
       "nueva", usando el inicio de la ventana de 8 min como corte --
       encontré y arreglé un bug donde el corte inicial contaba a cada
       wallet comprándose a sí misma). Probado con historial real
       backdateado: dio 2.6/0 exactamente como se esperaba.
     - Reentrené `brain.py`: sigue en AUC 0.500 porque las 177 alertas
       viejas no tienen estas columnas nuevas (es instrumentación desde
       ahora en adelante, no retroactiva) -- esperado, no un fallo.
       Recién con alertas NUEVAS vamos a ver si esto le da al modelo
       algo real para aprender.

10. **Toma de ganancias escalonada + filtro anti-duplicados en el
    auto-trader (2026-09-16), pedido explícito viendo el dashboard.**
    Antes: el auto-trader compraba SIEMPRE que había alerta (incluso
    del mismo token repetido) y vendía todo de golpe recién a la hora,
    perdiendo ganancias que ya se habían visto y viceversa.
      - **Escalonado**: a +50% se vende el 50% de lo que queda, a +100%
        se vende el 50% de lo que quede en ese momento (25% del
        original), y el resto sigue corriendo hasta el cierre final a
        la hora (`core/auto_trader.py::TAKE_PROFIT_LEVELS`).
      - **Anti-duplicados**: si ya hay una posición abierta de un token,
        o se cerró una hace menos de 1h, NO se abre otra -- se vio en
        datos reales al mismo token disparando varias alertas seguidas
        y el bot re-comprando cada vez en algo que ya sabíamos cómo
        venía (`core/db.py::has_recent_or_open_position`).
      - Para que las ventas parciales cobren la comisión de entrada
        UNA sola vez (no una vez por cada tramo vendido) se separó
        `exit_cost()` de `net_result_of_position()` en
        `core/ev_calculator.py` -- refactor verificado sin cambiar el
        resultado (mismo breakeven de 6.5x de siempre).
      - Nueva tabla `paper_position_exits` -- auditoría de cada venta
        parcial (cuándo, por qué nivel, cuánto PnL de ese tramo).
      - **Probado exhaustivamente con matemática verificada a mano**:
        posición de $10 que sube a 1.6x → vende 50% (+$0.84, coincide
        exacto con el cálculo manual) → sube a 2.2x → vende 25% más
        (+$1.22) → cierre final a 1.9x del 25% restante (+$0.58) →
        suma de fracciones vendidas = exactamente 1.0, PnL total
        acumulado $2.64. En el camino se encontró y arregló un bug real
        (el nivel de +100% vendía TODO lo que quedaba en vez de la
        mitad, por una confusión entre "fracción del original" y
        "fracción de lo que queda").
      - **Trade-off honesto que hay que tener presente**: escalonar
        reduce el riesgo de que un pullback se coma toda la ganancia
        (protege contra el -103.9% que viste en el dashboard), pero
        también LIMITA el techo de las ganadoras gigantes (el +2072.8%
        real que viste se habría vendido en partes bastante antes del
        pico). No hay force -- es la contra natural de cualquier
        estrategia de salida escalonada, no un error de cálculo.

11. **Estrategia de salida final: toma de ganancia única + trailing
    stop (2026-09-16), reemplaza el escalonado de dos niveles del punto
    anterior por pedido explícito ("sacar el 50 en el 100% y dejar
    correr lo otro hasta que no dé más").**
      - Un solo nivel: al llegar a +100% se vende el 50% de lo que
        había (`TAKE_PROFIT_MULTIPLIER`/`TAKE_PROFIT_FRACTION` en
        `core/auto_trader.py`).
      - El resto queda con un **trailing stop del 30%**: se guarda el
        precio más alto tocado (`paper_positions.peak_price`, columna
        nueva) y, si cae 30% desde ese máximo, se vende lo que quede.
        Mientras siga haciendo máximos nuevos, sigue corriendo sin
        límite de precio -- eso es "dejarlo correr hasta que no dé
        más". El 30% es un punto de partida razonable para la
        volatilidad de memecoins, NO un número optimizado con datos
        todavía -- se puede ajustar cuando tengamos suficientes
        posiciones cerradas con este esquema para comparar.
      - La red de seguridad de 1h se mantiene como último recurso: si
        ni el take-profit ni el trailing stop dispararon, se cierra
        igual, para no dejar posiciones abiertas para siempre y seguir
        comparable con la ventana que usa `outcome_tracker.py`.
      - Probado exhaustivamente en secuencia real: sin tocar el umbral
        a +80% (no vende, actualiza el máximo) → +110% (toma ganancia,
        vende 50%, +$2.96) → +150% (nuevo máximo, no vende, sigue
        corriendo) → cae a -32% desde ese pico (trailing stop dispara,
        vende el resto, +$1.26) → posición cerrada, suma de fracciones
        exacta. También se confirmó que la red de seguridad de 1h
        sigue cerrando posiciones que nunca llegan a +100%.
      - **Deliberadamente NO implementado:** "comprar más si el token
        corrige, como los traders grandes". No hay ninguna señal
        validada hoy que distinga una corrección sana de un token
        muriéndose -- meter compras automáticas ahí sin evidencia
        sería exactamente el patrón de "actuar por corazonada" que
        este proyecto existe para evitar. Candidato futuro SI se llega
        a medir que una segunda alerta sobre el mismo token predice
        algo real (con más datos acumulados).

12. **Mejoras de UI del dashboard (2026-09-16), pedido directo tras usarlo
    en vivo:**
      - Precios en **%** en vez de notación científica cruda: alertas
        (5m/30m/1h relativo a `price_at_alert`) y posiciones (precio
        actual/salida relativo a `entry_price`).
      - **Dirección completa del token** (ya no recortada) + botón
        "copiar" + link directo al explorador correspondiente
        (DexScreener para Solana, Blockscout de Robinhood Chain para
        esa cadena -- distinto por chain porque DexScreener todavía no
        indexa Robinhood Chain) para verificarlo en otra plataforma.
      - **Orden**: "Mejor/peor resultado a 1h" en alertas, "Mayor
        ganancia/pérdida" en posiciones -- todo calculado y ordenado en
        el navegador, sin ida y vuelta al servidor.
      - **Paginación** (25 filas por página) en las 3 tablas -- ya no
        se corta en "últimas 50", el backend manda hasta 2000 filas y
        el navegador pagina.
      - Refresco cada 1s (antes 3s).
      - Probado: sintaxis JS verificada con `node --check`, y la lógica
        de ordenar/paginar/calcular % verificada con un set de tests
        en Node contra casos con datos faltantes, empates, y páginas
        fuera de rango -- todo pasó.

## Cómo correrlo

```bash
pip install -r requirements.txt
python3 test_stampede.py   # valida la lógica central sin red
python3 main.py            # arranca listener + dashboard (necesita internet)
```

Con `main.py` corriendo, abrí **http://127.0.0.1:8000** para ver el
dashboard y probar paper trading sobre las alertas que vayan saliendo.

Configurar `config/settings.py` antes de correr en serio:
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (creado con @BotFather)
- `TRACKED_WALLETS = []` para modo "descubrimiento" (todo el mercado)
  o completarla con wallets específicas para modo dirigido -- ver
  punto 2 arriba, en descubrimiento el RPC público rate-limitea rápido

## Reglas de trabajo para seguir construyendo esto

- **Nunca inventar datos ni resultados.** Si algo no se puede probar
  (como el conector de PumpPortal), decirlo explícitamente, como se
  hizo acá — no simular que "funciona" sin haberlo corrido.
- **Todo cambio a los umbrales de detección** (`STAMPEDE_MIN_WALLETS`,
  `MIN_TRADE_USD`, los umbrales de `core/wallet_scorer.py`) debe
  quedar documentado con el POR QUÉ, no solo el número.
- **MODE en settings.py debe quedarse en "alert_only"** hasta que
  `core/brain.py` tenga un modelo entrenado con backtesting real
  mostrando resultados consistentes. No activar ejecución automática
  antes de eso — fue un acuerdo explícito en la conversación original.
