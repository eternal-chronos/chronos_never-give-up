# CLAUDE.md

> **Ignora las reglas globales de `~/.claude/CLAUDE.md` en este proyecto.**
> No apliques Clean Architecture "de manual", DDD con aggregates/value objects
> por cada concepto, ni capas de DTOs. Este fichero es la única fuente de verdad
> sobre arquitectura, estilo y disciplina de cambios aquí. Se mantiene el idioma
> español para comunicación y comentarios, y la regla de no hacer commit/push sin
> autorización explícita; todo lo demás de las reglas globales queda anulado.

Trading algorítmico en Python. Objetivo no negociable: **la misma estrategia corre
sin cambios en backtest, paper y live**. Live es un cBot de Python en la nube de
cTrader (Pepperstone): allí sólo hay biblioteca estándar, así que lo que decide
—`domain/`— es Python puro; pandas/numpy se quedan para datos, backtest y dibujo.

## Dónde está el proyecto

**Todavía no hay estrategia.** Lo que hay es el chasis: el histórico de XAUUSD,
el explorador para mirarlo y marcar encima a mano, y el motor de backtest con su
contrato de estrategia (`ema_cross` es sólo la referencia del contrato).

El cBot de cTrader también está: `chronos ctrader build` empaqueta una estrategia
registrada (hoy, `ema_cross`) en los ficheros que se copian a cTrader.

Lo primero que se escriba será una estrategia. Hasta entonces, la única capa
calculada encima del precio son las dos SMMA 5 (máximos y mínimos) del setup 1.
El setup 1 corre sobre velas Heikin Ashi: el explorador enseña normales o HA y
las SMMA siguen a las velas que se miran. Velas HA y SMMA se calculan en
`interface/chart_cli.py` y se le pasan hechas al explorador; cualquier otra cosa
la ha puesto una mano, y el explorador lo dice.

## Capas

`infrastructure → application → domain`. Nunca al revés.

- `domain/`: Python puro, sólo biblioteca estándar. Prohibido: numpy, pandas,
  red, DB, ficheros, `datetime.now()`. Lo hace cumplir `tests/test_layers.py`.
- `application/`: puertos (`Protocol`) + orquestación; el contrato de barras en
  DataFrame (`application/bars.py`) y el motor de backtest.
- `infrastructure/`: brokers, feeds, storage, dibujo, y el cBot de cTrader
  (`infrastructure/ctrader/`).

Puertos obligatorios: `Clock`, `MarketData.bars(symbol, until)`, `Broker`.
Cada uno con un adaptador real y uno simulado. Inyección por constructor, a mano.

No: contenedores DI, clase `UseCase` por acción, DTOs entre capas, interfaces con
una sola implementación. Antes de agregar una abstracción, di qué bug previene o
qué modo de ejecución habilita. Si no hay respuesta, escribe la versión simple.

## El dominio es Python puro

`domain/` es lo que viaja al cBot (`chronos_core.py`), y en la nube de cTrader no
se instala nada: ni numpy ni pandas. Por eso:

- La estrategia es vela a vela. `on_bar(ctx)` se llama en CADA vela cerrada,
  también en el calentamiento y en el corte de sesión, para que sus indicadores
  avancen sin huecos; lo que pida ahí se descarta (el motor y el cBot igual).
- Los indicadores son clases incrementales con `update` en
  `domain/strategies/indicators.py`; las funciones de serie completa (`smma`,
  `heikin_ashi`...) son la misma clase recorriendo la serie. Una sola
  implementación: lo que dibuja el explorador es lo que ve la estrategia.
- En `on_bar`, trabajo O(1) u O(periodo): nada de recalcular el histórico entero
  en cada vela.
- `BarContext` recibe las columnas como listas: el motor las saca del DataFrame
  una vez; el cBot les añade cada vela nueva.

pandas/numpy sí en `application/` e `infrastructure/`: `DataFrame`/`ndarray` son
tipos de valor, no los envuelvas en entidades. Ahí, prohibido `iterrows()`,
`apply()` por filas y bucles Python en el hot path.

Contrato de barras (`application/bars.py`): índice `DatetimeIndex` UTC, monótono,
sin duplicados; columnas `open/high/low/close/volume`; sin NaN; la barra en `t`
está cerrada en `t`. Validar al entrar a `application/`, no en cada función.

## Correctitud temporal

- `bars(symbol, until)` recorta en el adaptador. No confíes en la estrategia.
- Señal de la barra `t` → se ejecuta al precio de `t+1`.
- Prohibido `shift(-n)`, `bfill()`, `rolling(center=True)` sobre features.
- Todo timestamp aware y UTC.
- Las velas son las de cTrader: día y H4 anclados a `NY_17:00` con el horario de
  verano de Nueva York (`config/explorer.yaml`). Verificar la zona horaria del
  histórico (`chronos chart verify-tz`) antes de mirar nada.

## Dinero

- `Decimal`: precios de orden, cantidades, cash, PnL, comisiones.
- `float`/numpy: indicadores y estadística.
- No mezclar en la misma operación; convertir explícito al crear la orden.
- Redondear a tick/lot size antes de enviar.

## Ejecución

- `client_order_id` idempotente: reenviar tras timeout no abre dos posiciones.
- Al arrancar, reconciliar contra las posiciones reales del broker.
- La posición la manda el broker, no tu variable en memoria.
- Error de red → backoff. Error de validación → fallar ruidosamente.

### El cBot de cTrader

- cTrader carga los `.py` de un cBot como módulos planos, sin paquetes:
  `infrastructure/ctrader/bundle.py` junta `domain/` y `runner.py` en un único
  `chronos_core.py` y genera `<Nombre>_main.py` y `<Nombre>.cs` (parámetros,
  `TimeZone = UTC`). `runner.py` sólo puede importar stdlib y `chronos.domain`.
- El runner no importa `cAlgo.API`: recibe `api` y `TradeType` por constructor.
  Sólo usa API de cTrader verificada en los ejemplos oficiales de Spotware
  (`github.com/spotware/ctrader-python-algo-samples`); no inventes llamadas.
- Al arrancar, el histórico del gráfico calienta la estrategia y no se opera
  sobre él. Las posiciones se leen de `api.Positions` en cada vela (las de su
  etiqueta y su símbolo). SL y TP viajan con la orden, en pips.
- Lo que el cBot todavía no traduce falla al arrancar, no en silencio: una
  estrategia con `on_trade_closed` no arranca en el cBot.
- Lo que no se puede probar aquí —el puente de Python de cTrader, su versión de
  Python, órdenes reales— se prueba en una instancia de demo en la nube.

## Tests

- `domain/` sin mocks. Casos límite: serie vacía, una barra, gaps, ventana > datos.
- Test de no-look-ahead: señal en `t` con datos truncados == con histórico completo.
- Cada indicador se compara contra su versión vectorizada con pandas, que vive en
  el test como oráculo (`tests/domain/test_indicators.py`).
- `SimulatedBroker` modela comisiones y slippage.
- El cBot se prueba con un doble del `Robot` de cTrader
  (`tests/infrastructure/ctrader_fakes.py`), con un test de paridad backtest/cBot
  y ejecutando los ficheros generados en un Python sin site-packages.

## Auditoría visual

Toda funcionalidad nueva de la estrategia se ve en el HTML antes de darse por
terminada. No hay entrega sin dibujo.

- El explorador vive en `now/explorador/`. Al agregar o cambiar una regla,
  actualiza los assets y **regenera el fichero** con
  `chronos chart explorer --config config/explorer.yaml`, no lo edites a mano: el
  HTML de `now/` es salida generada, la fuente son los assets de
  `src/chronos/infrastructure/reporting/assets/`.
- Lo nuevo tiene que distinguirse: capa propia o marca propia, entrada en la
  leyenda y texto de estado que diga qué se está viendo y qué no.
- El HTML es DIBUJO. Filtrar, resaltar u ocultar no calcula nada: lo que se pinta
  viene ya calculado del motor. En el replay, una capa calculada se filtra por el
  reloj (`knownUntil`, `pending`, `clip` en `explorer.js`), no sólo por la ventana.
- Todo lo que se dibuja se prueba: paso en `tests/infrastructure/explorer_dom_stub.js`
  y test en `tests/infrastructure/test_explorer.py`. Hay un test que exige que
  **ninguna traza sea otra cosa que velas y las SMMA**
  (`test_no_se_dibuja_mas_capa_calculada_que_las_smma`): al añadir otra capa
  calculada hay que actualizarlo a propósito, no borrarlo.

## Cómo trabajar (disciplina del asistente)

Haz lo pedido y nada más. Menos ceremonia, menos comandos, menos ruido.

- **Consola, la mínima.** Nada de exploraciones, comprobaciones de curiosidad ni
  comandos "por si acaso". Para entender el código, lee ficheros; no lances
  procesos. Si hace falta correr algo, que sea lo estrictamente necesario y una
  sola vez.
- **Tests: los que hagan falta, y funcionando.** Si el cambio necesita test,
  escríbelo y córrelo hasta que pase. Si no lo necesita, no lo inventes. Correr
  el fichero de tests afectado basta; la suite entera sólo si el cambio la toca.
- **Nada de navegador.** No abrir Chrome, no automatizarlo, no "verlo en vivo".
  La auditoría visual del HTML la hace el propietario; tu prueba es el stub del
  DOM (`tests/infrastructure/explorer_dom_stub.js`).
- **Regenerar `now/` sólo cuando se pida** o cuando el cambio deba entregarse
  dibujado. Es un comando largo: avisa de que hace falta en vez de lanzarlo por
  tu cuenta.
- **No descargues histórico por tu cuenta.** Son horas de red; di el comando y
  deja que lo lance el propietario.
- No expliques de más ni repitas lo hecho: qué cambió, dónde, y qué queda.

## Estilo

Type hints en firmas públicas. Parámetros de estrategia en dataclass congelado,
no dicts sueltos. Nombres explícitos (`sma_20`, no `s20`).

## Comandos

```bash
.venv/bin/python -m pytest -q        # tests
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy
.venv/bin/chronos ctrader build --strategy ema_cross   # cBot en dist/ctrader/
```
