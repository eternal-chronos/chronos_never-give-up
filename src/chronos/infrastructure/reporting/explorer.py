"""Explorador visual de velas.

Un único HTML autocontenido —datos, Plotly y lógica embebidos— que se abre con
doble clic y funciona sin conexión. Está pensado para una cosa concreta: poner
el gráfico del proyecto al lado de las capturas de la plataforma del propietario
y poder marcar encima.

**No dibuja ninguna estrategia porque todavía no hay ninguna.** Lo que sale del
payload son las velas de cada temporalidad —las normales y, si se pasan, sus
Heikin Ashi—, los nombres con los que el propietario marca a mano y una única
capa calculada: las dos SMMA —de los máximos y de los mínimos— del setup 1, una
pareja por tipo de vela. Velas Heikin Ashi y SMMA llegan YA CALCULADAS desde el
punto de composición (`HeikinAshiLayer`, `SmmaLayer`): este módulo no importa
ningún indicador, sólo las serializa alineadas con las velas. Se dibujan en el
JavaScript con su propio control, su entrada en la leyenda y su texto de estado;
todo lo demás que haya encima del precio lo ha puesto una mano.

Del payload sale todo lo que se puede derivar en el navegador: las etiquetas de
los puntos se componen en JavaScript. Y viaja comprimido: cada serie va como
DIFERENCIAS ENTERAS respecto al valor anterior —minutos para los tiempos,
unidades de la última cifra del precio para los precios—. Con ocho años de M5
—más de medio millón de velas— y tres juegos de series por vela, mandar los
números enteros pasaría de cien megabytes; las diferencias son de cuatro o cinco
cifras y los minutos de M5, un «5».
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.offline as pyo

from chronos.application.chart.config import TIMEFRAME_LABELS, MarksConfig
from chronos.infrastructure.clock import SystemClock
from chronos.infrastructure.market.chart_run import ChartRun
from chronos.infrastructure.reporting import theme
from chronos.infrastructure.reporting.timezones import session_label

ASSETS = Path(__file__).parent / "assets"
_MARKER = re.compile(r"__[A-Z][A-Z_]*__")

#: Verde alcista, rojo bajista. Se toman de la paleta del proyecto para no
#: introducir hexadecimales sueltos.
BULLISH = theme.SERIES[2]
BEARISH = theme.NEGATIVE

#: Los tonos de lo que dibuja el PROPIETARIO a mano encima del gráfico. Van a
#: hues bien separados para poder distinguirse entre ellos, y quedan fuera de la
#: serie categórica y del violeta a propósito: el día que haya capas calculadas,
#: lo que se vea con estos tres colores seguirá siendo lo que ha puesto una mano.
HAND_COLORS: tuple[str, ...] = (theme.MAGENTA, theme.CYAN, theme.OLIVE)

#: Las dos SMMA: azul la de los máximos y naranja la de los mínimos. Es el par
#: categórico validado de la paleta, fuera de los colores de la mano y de las
#: velas.
SMMA_HIGH_COLOR = theme.SERIES[0]
SMMA_LOW_COLOR = theme.SERIES[1]

#: Velas que se dibujan como mucho a la vez. Plotly pinta cada vela como un
#: trazo SVG y con decenas de miles —tres meses de M5— el gráfico se arrastra en
#: cada gesto; a 1.200 píxeles de ancho, además, 2.500 velas ya no caben a un
#: píxel cada una. Una ventana con más se queda con las más recientes y lo dice.
MAX_DRAWN_CANDLES = 2500

#: Cifras con las que se dibuja el precio. De aquí sale también el pip con el
#: que se miden las distancias a mano: la última cifra que se enseña.
DECIMALS = 4

#: Unidades de la última cifra en una unidad de precio: los precios viajan como
#: enteros de esta escala.
_PRICE_SCALE = 10**DECIMALS

#: Minuto cero de la escala de tiempos del explorador.
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


@dataclass(frozen=True, slots=True)
class SmmaLayer:
    """Las dos SMMA del setup 1 por temporalidad, ya calculadas.

    Cada array va alineado con TODAS las velas de su temporalidad —antes de
    cualquier recorte— y con `NaN` en el calentamiento. Se calculan fuera del
    dibujo: si el explorador las calculara, el dibujo acabaría decidiendo.
    """

    period: int
    high: Mapping[str, np.ndarray]
    low: Mapping[str, np.ndarray]


@dataclass(frozen=True, slots=True)
class HeikinAshiLayer:
    """Las velas Heikin Ashi de cada temporalidad y sus SMMA, ya calculadas.

    `frames` lleva `open/high/low/close` con exactamente las mismas velas que
    `ChartRun.frames`. Las SMMA son las de ESTAS velas: como en TradingView, la
    media se calcula sobre las velas que se están mirando.
    """

    frames: Mapping[str, pd.DataFrame]
    smma: SmmaLayer | None = None


def render_explorer(
    run: ChartRun,
    generated_at: datetime | None = None,
    smma: SmmaLayer | None = None,
    heikin_ashi: HeikinAshiLayer | None = None,
) -> str:
    """Devuelve el HTML completo del explorador."""
    generated_at = generated_at or SystemClock().now()
    payload = build_payload(run, smma, heikin_ashi)
    # El JSON viaja dentro de un <script>: escapar `</` evita que un texto
    # cualquiera pueda cerrar la etiqueta antes de tiempo.
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).replace(
        "</", "<\\/"
    )
    symbol = run.config.symbol
    replacements = {
        "__TITLE__": f"Explorador de velas · {symbol}",
        "__HEADER__": f"{symbol} · explorador de velas · lado {run.history.side}",
        "__SUBTITLE__": _subtitle(run, smma, heikin_ashi),
        "__GENERATED__": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "__DATA__": data,
        "__PLOTLY__": pyo.get_plotlyjs(),
        "__EXPLORER_JS__": (ASSETS / "explorer.js").read_text(encoding="utf-8"),
    }
    template = (ASSETS / "explorer.html").read_text(encoding="utf-8")
    # Sustitución en una sola pasada: el contenido insertado (plotly.js incluido)
    # nunca se reescanea en busca de más marcadores.
    return _MARKER.sub(lambda match: replacements.get(match.group(0), match.group(0)), template)


def build_payload(
    run: ChartRun,
    smma: SmmaLayer | None = None,
    heikin_ashi: HeikinAshiLayer | None = None,
) -> dict[str, Any]:
    """Serializa la corrida a la estructura que consume el explorador."""
    config = run.config
    frames = run.frames
    max_bars = config.reporting.max_explorer_bars
    return {
        "meta": {
            "symbol": config.symbol,
            "side": run.history.side,
            "provenance": run.history.provenance,
            "sessionTimezone": config.reporting.session_timezone,
            "sessionTimezoneLabel": session_label(config.reporting.session_timezone),
            "h4OffsetHours": config.aggregation.h4_offset_hours,
            "dSessionStart": config.aggregation.d_session_start,
            #: También dice la escala de los precios: enteros de 10^-decimals.
            "decimals": DECIMALS,
            "maxCandles": MAX_DRAWN_CANDLES,
        },
        "colors": {
            "bullish": BULLISH,
            "bearish": BEARISH,
            "ink": theme.INK_PRIMARY,
            "muted": theme.INK_MUTED,
            "grid": theme.GRIDLINE,
            "surface": theme.SURFACE,
            "font": theme.FONT_FAMILY,
            #: Un tono de la mano por nombre de recuadro. Ninguna capa calculada
            #: puede usarlos: con ellos sólo se dibuja lo que ha puesto una mano.
            "rects": hand_colors(config.marks.rects),
            "smmaHigh": SMMA_HIGH_COLOR,
            "smmaLow": SMMA_LOW_COLOR,
        },
        #: Sólo se ofrecen los gráficos que tienen velas: si el histórico no daba
        #: para construir M15 o M5, su pestaña no puede quedarse ahí esperando a
        #: que alguien la pulse.
        "charts": list(run.timeframes),
        "labels": {timeframe: TIMEFRAME_LABELS[timeframe] for timeframe in run.timeframes},
        "spans": {timeframe: _span_minutes(frame) for timeframe, frame in frames.items()},
        "bars": {timeframe: _bars_payload(frame, max_bars) for timeframe, frame in frames.items()},
        #: Las SMMA de las velas normales, o `None` si no se ha pasado ninguna.
        "smma": None if smma is None else _smma_payload(smma, frames, max_bars),
        #: Las velas Heikin Ashi y sus SMMA, o `None` si no se han pasado.
        "heikinAshi": (
            None if heikin_ashi is None else _heikin_ashi_payload(heikin_ashi, frames, max_bars)
        ),
        "marks": _marks(config.marks),
        #: Temporalidades pedidas que el histórico no daba para construir, con el
        #: motivo. Una pestaña que falta se lee como que no existe: hay que decirlo.
        "skipped": list(run.skipped),
    }


def hand_colors(names: tuple[str, ...]) -> dict[str, str]:
    """Un color de la mano por nombre, en el orden en que se declararon."""
    return {name: HAND_COLORS[index % len(HAND_COLORS)] for index, name in enumerate(names)}


def _marks(marks: MarksConfig) -> dict[str, list[str]]:
    return {"rects": list(marks.rects)}


# --- Velas y capas ------------------------------------------------------------


def _bars_payload(frame: pd.DataFrame, max_bars: int) -> dict[str, Any]:
    total = len(frame)
    frame = frame.iloc[_kept(total, max_bars)]
    return {
        "truncated": len(frame) < total,
        "total": total,
        "t": _minute_steps(pd.DatetimeIndex(frame.index)),
        **_candles_payload(frame),
    }


def _candles_payload(frame: pd.DataFrame) -> dict[str, list[int | None]]:
    return {
        key: _price_steps(frame[column].to_numpy(dtype=float))
        for key, column in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))
    }


def _heikin_ashi_payload(
    layer: HeikinAshiLayer, frames: Mapping[str, pd.DataFrame], max_bars: int
) -> dict[str, Any]:
    bars: dict[str, Any] = {}
    for timeframe, frame in frames.items():
        candles = layer.frames[timeframe]
        if not candles.index.equals(frame.index):
            raise ValueError(f"las velas Heikin Ashi de {timeframe} no son las mismas velas")
        bars[timeframe] = _candles_payload(candles.iloc[_kept(len(candles), max_bars)])
    return {
        "bars": bars,
        "smma": None if layer.smma is None else _smma_payload(layer.smma, frames, max_bars),
    }


def _smma_payload(
    smma: SmmaLayer, frames: Mapping[str, pd.DataFrame], max_bars: int
) -> dict[str, Any]:
    return {
        "period": smma.period,
        "high": {tf: _layer_values(smma.high[tf], frame, max_bars) for tf, frame in frames.items()},
        "low": {tf: _layer_values(smma.low[tf], frame, max_bars) for tf, frame in frames.items()},
    }


def _layer_values(values: np.ndarray, frame: pd.DataFrame, max_bars: int) -> list[int | None]:
    """Un valor por vela, recortado por el mismo sitio que las velas.

    Se calcula sobre todo el histórico y se recorta después: recortar antes
    reiniciaría la media en la primera vela embebida.
    """
    if len(values) != len(frame):
        raise ValueError(f"la capa trae {len(values)} valores para {len(frame)} velas")
    return _price_steps(values[_kept(len(values), max_bars)])


def _kept(length: int, max_bars: int) -> slice:
    """Las `max_bars` últimas, que es lo que se conserva de las velas al recortar."""
    return slice(-max_bars, None) if 0 < max_bars < length else slice(None)


def _minute_steps(index: pd.DatetimeIndex) -> list[int | None]:
    """Minutos desde la época, como diferencias con la vela anterior."""
    utc = index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
    minutes = ((utc - _EPOCH) // pd.Timedelta(minutes=1)).to_numpy(dtype=np.int64)
    return _steps(minutes)


def _price_steps(values: np.ndarray) -> list[int | None]:
    """Precios en unidades de la última cifra, como diferencias con el anterior.

    Donde no hay valor viaja `null` —el JSON no tiene `NaN`— y la diferencia
    siguiente se cuenta desde el último valor que sí lo tenía.
    """
    ticks = np.round(np.asarray(values, dtype=float) * _PRICE_SCALE)
    valid = ~np.isnan(ticks)
    steps = _steps(ticks[valid].astype(np.int64))
    if valid.all():
        return steps
    encoded: list[int | None] = [None] * len(ticks)
    for position, step in zip(np.flatnonzero(valid).tolist(), steps, strict=True):
        encoded[position] = step
    return encoded


def _steps(values: np.ndarray) -> list[int | None]:
    """El primero tal cual y, desde ahí, la diferencia con el anterior."""
    result: list[int | None] = np.diff(values, prepend=0).tolist()
    return result


def _span_minutes(frame: pd.DataFrame) -> int:
    """Duración de la vela, en minutos, medida sobre las propias velas.

    Es lo que le falta al replay para saber **cuándo** se supo cada cosa. Las
    velas van etiquetadas al inicio del intervalo, así que la vela de `t` no
    cierra hasta `t + span`.

    Se mide con la moda de las diferencias en vez de deducirla del nombre de la
    temporalidad: con ancla de sesión el diario no dura siempre lo mismo y el
    nombre mentiría.
    """
    return int(_bar_span(pd.DatetimeIndex(frame.index)) // pd.Timedelta(minutes=1))


def _bar_span(index: pd.DatetimeIndex) -> pd.Timedelta:
    if len(index) < 2:
        return pd.Timedelta(hours=4)
    deltas = index.to_series().diff().dropna()
    return pd.Timedelta(deltas.mode().iloc[0]) if not deltas.empty else pd.Timedelta(hours=4)


def _subtitle(run: ChartRun, smma: SmmaLayer | None, heikin_ashi: HeikinAshiLayer | None) -> str:
    config = run.config
    graficos = ", ".join(TIMEFRAME_LABELS[timeframe] for timeframe in run.timeframes)
    faltan = (
        f" · sin construir: {', '.join(item.split(':')[0] for item in run.skipped)}"
        if run.skipped
        else ""
    )
    velas = "velas (normales y Heikin Ashi)" if heikin_ashi is not None else "velas"
    medias = (
        f", SMMA {smma.period} de máximos y de mínimos" if smma is not None else ""
    )
    return (
        f"{graficos} · lado {run.history.side} · día desde "
        f"{config.aggregation.describe_daily_start()} · sin estrategia: sólo "
        f"{velas}{medias} y lo que marques a mano{faltan}"
    )


def payload_size(payload: dict[str, Any]) -> int:
    """Bytes del JSON embebido. Útil para vigilar que el fichero no se dispare."""
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


def bar_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {chart: len(bars["t"]) for chart, bars in payload["bars"].items()}
