# chronos-never_give_up

Laboratorio de trading algorítmico sobre **XAUUSD CFD** con **Pepperstone / cTrader**.

**Todavía no hay estrategia, y es a propósito.** Lo que hay es el chasis, con
todo lo que hace falta para escribir una encima:

- un **explorador HTML** autocontenido con cinco temporalidades (Diario, H4, H1,
  M15 y M5), volumen, replay paso a paso con la vela en formación, zoom de
  trading y herramientas para marcar a mano encima del precio;
- la **carga y agregación del histórico** M1 → M5 / M15 / H1 / H4 / Diario, con
  la rejilla de cTrader y el horario de verano de Nueva York resuelto de verdad;
- el **motor de backtest** con su contrato de estrategia, su bróker simulado —con
  spread, comisión, swap y slippage—, dimensionamiento de riesgo, métricas y
  panel HTML;
- la **verificación de zona horaria**, obligatoria antes de mirar nada.

Estado: **solo backtest**. El camino previsto es backtest → demo → live, y cada
salto exige trabajo explícito (ver *Hoja de ruta*). La configuración tiene una
puerta que rechaza cualquier `mode` distinto de `backtest`.

## Instalación

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
source .venv/bin/activate
```

## El explorador

Es la herramienta principal mientras no haya estrategia: se abre con doble clic,
funciona sin conexión y lleva todo el histórico dentro del mismo fichero.

```bash
# 1. Traer el histórico. Tiene que ser M1: la verificación horaria mide el rango
#    medio POR MINUTO y con velas de una hora no distingue 13:30 de 13:00.
#    Son horas de descarga; se reanuda si se corta.
chronos data dukascopy -g m1 --from 2018-01-01 --to 2025-12-31 --sides bid

# 2. Verificar la zona horaria del histórico. Obligatorio antes de mirar nada.
chronos chart verify-tz --config config/explorer.yaml

# 3. Qué hay cargado: temporalidades, velas y tramo de cada una.
chronos chart info --config config/explorer.yaml

# 4. Escribir el explorador -> now/explorador/explorador.html
chronos chart explorer --config config/explorer.yaml
```

Si el histórico no da para alguna temporalidad —un histórico H1 no da para M15
ni para M5— no se rompe nada: se avisa, se dibujan las que hay y el propio HTML
lo dice en su estado.

### Qué se puede hacer dentro

| Control | Qué hace | Atajo |
|---|---|---|
| **Temporalidad** | Diario, H4, H1, M15, M5 | `d` `4` `1` `m` `5` |
| **Vista** | Velas o línea de cierres; «Ajustar» suelta el zoom a mano | |
| **Periodo** | Presets de 1 semana a todo, y `◀ ▶` para ir tramo a tramo | `←` `→` |
| **Volumen** | El volumen del histórico al pie del precio, en las últimas 40 velas | |
| **Replay** | Reproduce la historia paso a paso, con la vela en formación | espacio |
| **Simular entrada** | Planta una caja largo/corto, la arrastra y mide pips y R:R | |
| **Rectángulo** | Recuadros a mano, con los nombres de `config/explorer.yaml` | |
| **Cuenta simulada** | Apunta cada caja como ganada / perdida / BE y lleva el saldo | |
| **Auditoría ciega** | Ventana al azar con semilla, para marcar antes de mirar nada | |

Además: rueda para zoom, arrastrar sobre el **eje de precios** comprime o estira
la vertical y sobre el **eje de fechas** abre o cierra el gráfico de lado, como en
cualquier plataforma. «Ajustar» o el doble clic sueltan el encuadre.

Los nombres de los recuadros salen de `config/explorer.yaml` (`marks.rects`):
cámbialos cuando la estrategia tenga vocabulario propio, sin tocar el JavaScript.

## El backtest

```bash
# Datos sintéticos para comprobar que el pipeline funciona de punta a punta
chronos data synth --periods 200000
chronos backtest --config config/backtest.synthetic.yaml

# Sobre el histórico real
chronos backtest --config config/backtest.yaml
chronos backtest --start 2024-01-01 --end 2024-06-30

# Otros
chronos data info data/processed/XAUUSD_M1_bid.parquet
chronos strategy list
```

`ema_cross` es la estrategia **de referencia** que viene con el motor, no la del
proyecto: existe para poder correr el pipeline entero antes de que haya una
propia. Cuando sobre, se borra `src/chronos/domain/strategies/ema_cross.py` y su
sección en los YAML de backtest.

Cada corrida deja una carpeta en `reports/` con `report.html` (el panel),
`trades.csv`, `equity.csv`, `metrics.json` y `run.json`. El panel es un fichero
autocontenido que permite acotar el periodo, filtrar por lado y por motivo de
salida y ordenar la tabla de operaciones:

| | Qué hace |
|---|---|
| Filtrar en el panel | Mira lo que ya ocurrió dentro de ese tramo. La estrategia tomó sus decisiones sobre la corrida completa. Instantáneo. |
| `--start` / `--end` | Vuelve a simular: el calentamiento y el estado arrancan de cero dentro del periodo. Es el resultado real de operar solo ese tramo. |

Las métricas del panel se calculan en JavaScript, así que son una segunda
implementación de las fórmulas de Python. `tests/infrastructure/test_dashboard.py`
ejecuta ese JavaScript con node sobre una corrida real y compara las métricas
contra `compute_performance`: si divergen, el test falla.

### Qué modela el backtest

| Aspecto | Modelo |
|---|---|
| Ejecución | Señal en el cierre de la barra N → fill en la apertura de la N+1 |
| Horquilla | `price_basis: bid` → se compra en ask, se vende en bid |
| Comisión | Por lote y por lado, cargada al balance en apertura y cierre |
| Swap | Puntos por lote y noche, con triple el día configurado |
| Deslizamiento | Puntos en contra en órdenes a mercado y en stops |
| Stop loss | Se dispara con el recorrido de la barra; con hueco, fill en la apertura |
| Take profit | Orden limitada: se llena al nivel, sin deslizamiento |
| Barra que toca SL y TP | `intrabar_priority: worst` → se asume el stop (conservador) |
| Margen | Requerido al abrir; stop out por nivel de margen |
| Riesgo | Pérdida diaria máxima y drawdown máximo cortan la operativa |

Limitaciones conocidas, para no engañarse con los resultados:

- Sin datos intrabar más finos, el orden real de SL/TP dentro de una vela es
  desconocido; por eso la asunción es la pesimista.
- El spread es fijo salvo que el dataset traiga columna `spread`. Los picos de
  spread en noticias y en el cierre diario no se modelan si no están en los datos.
- No hay rechazos del bróker, requotes ni latencia.
- El swap se aplica por día de calendario del servidor, no por sesión exacta.

**Verifica la ficha del símbolo contra tu cuenta real** (cTrader → clic derecho en
el símbolo → *Symbol Information*): comisión, swap, apalancamiento y tamaño de
contrato cambian por entidad regulatoria y tipo de cuenta, y mueven el resultado
del backtest más que la mayoría de parámetros de la estrategia.

## Escribir la estrategia

```python
# src/chronos/domain/strategies/mi_estrategia.py
@register("mi_estrategia")
class MiEstrategia(Strategy):
    def __init__(self, periodo: int = 5) -> None:
        super().__init__(periodo=periodo)
        self._media = Smma(periodo)          # indicador incremental

    @property
    def warmup_bars(self) -> int: ...
    def on_bar(self, ctx):                   # una vez por cada vela cerrada
        media = self._media.update(ctx.close)
        return (EntrySignal(...),) if ... else ()
```

1. Un fichero nuevo en `src/chronos/domain/strategies/`, decorado con
   `@register("nombre")` y heredando de `Strategy`. **Python puro**, sólo
   biblioteca estándar: es el mismo código que corre en el cBot de cTrader, y en
   su nube no hay numpy ni pandas. Sin red, sin ficheros, sin reloj.
2. Sus parámetros, en un dataclass congelado, y su configuración en el YAML.
3. Sus tests en `tests/domain/`, sin mocks, con los casos límite y el test de
   no-look-ahead.
4. **Y su dibujo.** Lo que la estrategia calcule tiene que verse en el explorador
   antes de darse por terminado: capa propia, entrada en la leyenda y texto de
   estado que diga qué se está viendo y qué no. Se añade al payload en
   `infrastructure/reporting/explorer.py`, se dibuja en `assets/explorer.js` y se
   prueba con el stub del DOM.

Dos reglas que evitan resultados falsos:

1. Los indicadores avanzan vela a vela dentro de `on_bar`, con las clases de
   `domain/strategies/indicators.py` (`Smma`, `HeikinAshi`, `Ema`, `Atr`...):
   **nunca ven datos futuros**. `on_bar` se llama en todas las velas, también en
   el calentamiento; lo que pida ahí se descarta.
2. Los stops se expresan como **distancia** (`stop_distance`), no como precio
   absoluto: el fill ocurre en la barra siguiente y un nivel calculado sobre el
   cierre anterior puede quedar del lado equivocado.

Hay un test —`test_no_se_dibuja_ni_una_capa_calculada`— que hoy exige que el
explorador no pinte nada calculado. Al añadir la primera capa hay que
actualizarlo a propósito; no borrarlo.

## Estructura

`infrastructure → application → domain`. Nunca al revés. Las reglas completas
están en `CLAUDE.md`.

```
config/
  explorer.yaml            # el histórico, la rejilla y las marcas a mano
  backtest.yaml            # una corrida del motor
  instruments/xauusd.yaml  # ficha del símbolo: ticks, costes, swap, sesión
data/
  raw/dukascopy/           # .bi5 crudos: caché de descarga, reanudable
  processed/               # parquet canónico por lado
now/explorador/            # el HTML generado. Salida, no fuente
src/chronos/
  domain/                  # Python puro: instrumento, posición, contrato de estrategia
  domain/strategies/       # aquí va la estrategia nueva; indicadores incrementales
  application/bars.py      # contrato de barras en DataFrame
  application/chart/       # configuración del explorador y verificación horaria
  application/backtest/    # motor, sesión, resultado
  infrastructure/market/   # carga del histórico y agregación de temporalidades
  infrastructure/reporting/# explorador, panel e informes  (assets/ = la fuente)
  infrastructure/ctrader/  # el cBot: runner y empaquetado para cTrader
  interface/               # CLI: punto de composición
tests/
```

`domain/` es Python puro: viaja al cBot de cTrader. pandas y numpy viven en
`application/` e `infrastructure/` (datos, backtest, métricas, dibujo). Lo que
`domain/` no puede tocar es numpy, pandas, red, disco, base de datos ni
`datetime.now()`; `tests/test_layers.py` lo comprueba.

## El cBot de cTrader

```bash
chronos ctrader build --strategy ema_cross     # → dist/ctrader/ChronosEmaCross/
```

cTrader carga los `.py` de un cBot como módulos sueltos, sin paquetes, y en su
nube sólo hay biblioteca estándar. El comando escribe tres ficheros:
`chronos_core.py` (todo `domain/` más el runner, en un módulo), `<Nombre>_main.py`
(la clase que instancia cTrader) y `<Nombre>.cs` (los parámetros del cBot, con la
zona horaria en UTC). En cTrader Windows 5.4+ o Mac 5.7+: *New cBot* en Python con
ese nombre, se copian los tres encima y *Build*.

Qué hace el cBot: el histórico del gráfico calienta la estrategia sin operar;
en cada vela cerrada decide y manda la orden a mercado con SL y TP en pips, que
ejecuta el servidor aunque el cBot se pare; las posiciones se leen del bróker en
cada vela (las de su etiqueta). Tamaño: lote fijo, % de riesgo con el valor del
pip que da el bróker, o los lotes de la señal, con tope de lotes y de posiciones.

Todavía no traduce los cierres a `on_trade_closed` (una estrategia que lo use no
arranca) ni los límites de pérdida diaria y drawdown del backtest. Antes de dinero
real: una instancia de demo en la nube, y mirar el log (la primera línea dice la
versión de Python de cTrader y cuántas velas calentó).

## Datos

El formato canónico es un parquet con índice `DatetimeIndex` en UTC y columnas
`open, high, low, close, volume` (+ `spread` opcional, en precio). `chronos data
import` acepta CSV de cTrader, MT5 o Dukascopy y hace la conversión.

Los datos sintéticos (`chronos data synth`) existen para ejercitar el motor, no
para evaluar señales: un movimiento browniano no tiene la microestructura del oro.

## Calidad

```bash
make check      # lint + tipos + tests
make smoke      # datos sintéticos + corrida completa de punta a punta
make explorer   # regenera now/explorador/explorador.html
```

Los tests del explorador y del panel necesitan `node` en el PATH; si no está, se
saltan solos.

## Hoja de ruta

1. **Backtest** (actual) — construir y validar la estrategia por fases.
2. **Demo** — el cBot de Python en la nube de cTrader (`chronos ctrader build`),
   la misma estrategia sin tocarla, contrastando fills reales contra los simulados.
3. **Live** — solo después de que demo confirme el comportamiento del backtest.
