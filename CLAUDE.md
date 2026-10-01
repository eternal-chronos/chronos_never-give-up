# CLAUDE.md

> **Ignora las reglas globales de `~/.claude/CLAUDE.md` en este proyecto.**
> No apliques Clean Architecture "de manual", DDD con aggregates/value objects
> por cada concepto, ni capas de DTOs. Este fichero es la única fuente de verdad
> sobre arquitectura, estilo y disciplina de cambios aquí. Se mantiene el idioma
> español para comunicación y comentarios, y la regla de no hacer commit/push sin
> autorización explícita; todo lo demás de las reglas globales queda anulado.

Trading algorítmico en Python (pandas/numpy). Objetivo no negociable: **la misma
estrategia corre sin cambios en backtest, paper y live**.

## Dónde está el proyecto

**Todavía no hay estrategia.** Lo que hay es el chasis: el histórico de XAUUSD,
el explorador para mirarlo y marcar encima a mano, y el motor de backtest con su
contrato de estrategia (`ema_cross` es sólo la referencia del contrato).

Lo primero que se escriba será una estrategia. Hasta entonces, cualquier cosa que
se dibuje encima del precio la ha puesto una mano, y el explorador lo dice.

## Capas

`infrastructure → application → domain`. Nunca al revés.

- `domain/`: funciones puras sobre arrays. Prohibido: red, DB, ficheros, `datetime.now()`.
- `application/`: puertos (`Protocol`) + orquestación.
- `infrastructure/`: brokers, feeds, storage, dibujo.

Puertos obligatorios: `Clock`, `MarketData.bars(symbol, until)`, `Broker`.
Cada uno con un adaptador real y uno simulado. Inyección por constructor, a mano.

No: contenedores DI, clase `UseCase` por acción, DTOs entre capas, interfaces con
una sola implementación. Antes de agregar una abstracción, di qué bug previene o
qué modo de ejecución habilita. Si no hay respuesta, escribe la versión simple.

## pandas es del dominio

`DataFrame`/`ndarray` son tipos de valor, no infraestructura. No los envuelvas en
entidades ni itereres fila por fila. Prohibido `iterrows()`, `apply()` por filas y
bucles Python en el hot path.

Contrato de barras: índice `DatetimeIndex` UTC, monótono, sin duplicados; columnas
`open/high/low/close/volume`; sin NaN; la barra en `t` está cerrada en `t`.
Validar al entrar a `application/`, no en cada función.

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

## Tests

- `domain/` sin mocks. Casos límite: df vacío, una barra, gaps, ventana > datos.
- Test de no-look-ahead: señal en `t` con datos truncados == con histórico completo.
- `SimulatedBroker` modela comisiones y slippage.

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
  **ninguna traza sea otra cosa que velas y volumen**
  (`test_no_se_dibuja_ni_una_capa_calculada`): al añadir la primera capa
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
```
