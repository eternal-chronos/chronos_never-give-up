"""Indicadores técnicos en Python puro, vela a vela.

Cada indicador es una clase con estado: se alimenta con `update` una vela cada
vez y devuelve el valor de esa vela, o `None` mientras calienta. Es la forma que
corre igual en el backtest y dentro del cBot de cTrader, donde no hay numpy ni
pandas, y la que usa una estrategia dentro de `on_bar`.

Las funciones de serie completa (`smma(valores, 5)`, `heikin_ashi(...)`) son la
misma clase recorriendo la serie: devuelven listas alineadas con la entrada y
con `nan` en el calentamiento. Las usa quien necesita la serie entera, como el
explorador. Una sola implementación por indicador: lo que se dibuja es lo que
ve la estrategia.

Ninguno mira hacia el futuro: el valor de la vela `i` solo usa datos de `[0, i]`.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

# --- Medias -----------------------------------------------------------------


class Sma:
    """Media móvil simple de las últimas `period` velas."""

    __slots__ = ("_window", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._window: deque[float] = deque(maxlen=period)

    def update(self, value: float) -> float | None:
        self._window.append(value)
        if len(self._window) < self.period:
            return None
        # Suma de la ventana entera y no un acumulado: en cientos de miles de
        # velas M5 un acumulado arrastra error de redondeo.
        return math.fsum(self._window) / self.period


class Ema:
    """Media móvil exponencial (suavizado estándar, alfa = 2/(n+1)).

    Arranca en el primer valor y no da valor hasta haber visto `period` velas.
    """

    __slots__ = ("_alpha", "_count", "_value", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._alpha = 2.0 / (period + 1)
        self._count = 0
        self._value = 0.0

    def update(self, value: float) -> float | None:
        self._count += 1
        if self._count == 1:
            self._value = value
        else:
            self._value += self._alpha * (value - self._value)
        return self._value if self._count >= self.period else None


class Rma:
    """Media suavizada de Wilder (alfa = 1/n). Base de ATR y RSI.

    Arranca en el primer valor y da valor desde la primera vela.
    """

    __slots__ = ("_started", "_value", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._started = False
        self._value = 0.0

    def update(self, value: float) -> float:
        if not self._started:
            self._started = True
            self._value = value
        else:
            self._value += (value - self._value) / self.period
        return self._value


class Smma:
    """Media móvil suavizada (SMMA), la «Smoothed» de MT4 y TradingView.

    Arranca en la media simple de los `period` primeros valores y desde ahí
    `smma[i] = (smma[i-1] * (period - 1) + valor[i]) / period`, o sea alfa =
    1/period. Es la recursión de `Rma`; sólo cambia el arranque, que deja de
    notarse a las pocas decenas de valores.
    """

    __slots__ = ("_seed", "_value", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._seed: list[float] | None = []
        self._value = 0.0

    def update(self, value: float) -> float | None:
        if self._seed is not None:
            self._seed.append(value)
            if len(self._seed) < self.period:
                return None
            self._value = math.fsum(self._seed) / self.period
            self._seed = None
            return self._value
        self._value += (value - self._value) / self.period
        return self._value


# --- Velas -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeikinAshiCandle:
    open: float
    high: float
    low: float
    close: float


class HeikinAshi:
    """Velas Heikin Ashi, la de TradingView.

    cierre   = (O + H + L + C) / 4
    apertura = (apertura HA anterior + cierre HA anterior) / 2; la primera, (O + C) / 2
    máximo   = max(H, apertura, cierre) y mínimo = min(L, apertura, cierre)

    La vela `i` sólo usa velas hasta `i`: la apertura mira la HA anterior, nunca
    la siguiente.
    """

    __slots__ = ("_previous",)

    def __init__(self) -> None:
        self._previous: HeikinAshiCandle | None = None

    def update(self, open_: float, high: float, low: float, close: float) -> HeikinAshiCandle:
        ha_close = (open_ + high + low + close) / 4.0
        previous = self._previous
        ha_open = (
            (open_ + close) / 2.0 if previous is None else (previous.open + previous.close) / 2.0
        )
        candle = HeikinAshiCandle(
            open=ha_open,
            high=max(high, ha_open, ha_close),
            low=min(low, ha_open, ha_close),
            close=ha_close,
        )
        self._previous = candle
        return candle


# --- Volatilidad -------------------------------------------------------------


class TrueRange:
    """Rango verdadero: incluye el hueco respecto al cierre anterior.

    En la primera vela no hay cierre anterior y se usa su propio cierre.
    """

    __slots__ = ("_previous_close",)

    def __init__(self) -> None:
        self._previous_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float:
        previous = close if self._previous_close is None else self._previous_close
        self._previous_close = close
        return max(high - low, abs(high - previous), abs(low - previous))


class Atr:
    """Average True Range de Wilder. Sin valor hasta haber visto `period` velas."""

    __slots__ = ("_average", "_count", "_true_range", "period")

    def __init__(self, period: int = 14) -> None:
        _check_period(period)
        self.period = period
        self._true_range = TrueRange()
        self._average = Rma(period)
        self._count = 0

    def update(self, high: float, low: float, close: float) -> float | None:
        self._count += 1
        value = self._average.update(self._true_range.update(high, low, close))
        return value if self._count >= self.period else None


class Rsi:
    """Índice de fuerza relativa de Wilder, en [0, 100].

    Sin valor en las `period` primeras velas.
    """

    __slots__ = ("_count", "_gains", "_losses", "_previous_close", "period")

    def __init__(self, period: int = 14) -> None:
        _check_period(period)
        self.period = period
        self._gains = Rma(period)
        self._losses = Rma(period)
        self._previous_close: float | None = None
        self._count = 0

    def update(self, close: float) -> float | None:
        self._count += 1
        delta = 0.0 if self._previous_close is None else close - self._previous_close
        self._previous_close = close
        gains = self._gains.update(delta if delta > 0 else 0.0)
        losses = self._losses.update(-delta if delta < 0 else 0.0)
        if self._count <= self.period:
            return None
        if losses <= 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + gains / losses)


# --- Ventanas ------------------------------------------------------------------


class RollingMax:
    __slots__ = ("_window", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._window: deque[float] = deque(maxlen=period)

    def update(self, value: float) -> float | None:
        self._window.append(value)
        return max(self._window) if len(self._window) == self.period else None


class RollingMin:
    __slots__ = ("_window", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._window: deque[float] = deque(maxlen=period)

    def update(self, value: float) -> float | None:
        self._window.append(value)
        return min(self._window) if len(self._window) == self.period else None


class RollingStd:
    """Desviación típica poblacional (ddof = 0) de las últimas `period` velas."""

    __slots__ = ("_window", "period")

    def __init__(self, period: int) -> None:
        _check_period(period)
        self.period = period
        self._window: deque[float] = deque(maxlen=period)

    def update(self, value: float) -> float | None:
        self._window.append(value)
        if len(self._window) < self.period:
            return None
        mean = math.fsum(self._window) / self.period
        return math.sqrt(math.fsum((x - mean) ** 2 for x in self._window) / self.period)


@dataclass(frozen=True, slots=True)
class BollingerBands:
    upper: float
    middle: float
    lower: float


class Bollinger:
    """Bandas de Bollinger: media simple ± `deviations` desviaciones típicas."""

    __slots__ = ("_deviation", "_mean", "deviations")

    def __init__(self, period: int = 20, deviations: float = 2.0) -> None:
        self._mean = Sma(period)
        self._deviation = RollingStd(period)
        self.deviations = deviations

    def update(self, close: float) -> BollingerBands | None:
        middle = self._mean.update(close)
        deviation = self._deviation.update(close)
        if middle is None or deviation is None:
            return None
        spread = deviation * self.deviations
        return BollingerBands(upper=middle + spread, middle=middle, lower=middle - spread)


# --- Series completas ---------------------------------------------------------
#
# La misma clase recorriendo la serie. `nan` donde la clase aún no da valor.


def sma(values: Sequence[float], period: int) -> list[float]:
    """Media móvil simple."""
    indicator = Sma(period)
    return [_or_nan(indicator.update(value)) for value in values]


def ema(values: Sequence[float], period: int) -> list[float]:
    """Media móvil exponencial (alfa = 2/(n+1))."""
    indicator = Ema(period)
    return [_or_nan(indicator.update(value)) for value in values]


def rma(values: Sequence[float], period: int) -> list[float]:
    """Media suavizada de Wilder (alfa = 1/n)."""
    indicator = Rma(period)
    return [indicator.update(value) for value in values]


def smma(values: Sequence[float], period: int) -> list[float]:
    """Media móvil suavizada (SMMA), ver `Smma`."""
    indicator = Smma(period)
    return [_or_nan(indicator.update(value)) for value in values]


def heikin_ashi(
    open_: Sequence[float], high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Velas Heikin Ashi: devuelve (apertura, máximo, mínimo, cierre). Ver `HeikinAshi`."""
    indicator = HeikinAshi()
    candles = [
        indicator.update(o, h, low_, c) for o, h, low_, c in zip(open_, high, low, close, strict=True)
    ]
    return (
        [candle.open for candle in candles],
        [candle.high for candle in candles],
        [candle.low for candle in candles],
        [candle.close for candle in candles],
    )


def true_range(
    high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> list[float]:
    indicator = TrueRange()
    return [indicator.update(h, low_, c) for h, low_, c in zip(high, low, close, strict=True)]


def atr(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14
) -> list[float]:
    """Average True Range de Wilder."""
    indicator = Atr(period)
    return [
        _or_nan(indicator.update(h, low_, c)) for h, low_, c in zip(high, low, close, strict=True)
    ]


def rsi(close: Sequence[float], period: int = 14) -> list[float]:
    """Índice de fuerza relativa de Wilder, en [0, 100]."""
    indicator = Rsi(period)
    return [_or_nan(indicator.update(value)) for value in close]


def rolling_max(values: Sequence[float], period: int) -> list[float]:
    indicator = RollingMax(period)
    return [_or_nan(indicator.update(value)) for value in values]


def rolling_min(values: Sequence[float], period: int) -> list[float]:
    indicator = RollingMin(period)
    return [_or_nan(indicator.update(value)) for value in values]


def rolling_std(values: Sequence[float], period: int) -> list[float]:
    indicator = RollingStd(period)
    return [_or_nan(indicator.update(value)) for value in values]


def bollinger(
    close: Sequence[float], period: int = 20, deviations: float = 2.0
) -> tuple[list[float], list[float], list[float]]:
    """Devuelve (banda superior, media, banda inferior)."""
    indicator = Bollinger(period, deviations)
    bands = [indicator.update(value) for value in close]
    return (
        [math.nan if band is None else band.upper for band in bands],
        [math.nan if band is None else band.middle for band in bands],
        [math.nan if band is None else band.lower for band in bands],
    )


def _or_nan(value: float | None) -> float:
    return math.nan if value is None else value


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("El periodo debe ser >= 1")
