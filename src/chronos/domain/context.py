"""Contexto que la estrategia recibe en cada barra.

Es una clase concreta, no un puerto: solo hay una forma de mirar una barra.
Quien la alimenta —el motor de backtest o el cBot de cTrader— le da las columnas
como secuencias (listas de floats) y la sitúa en una barra con `update` antes de
cada `on_bar`. Leerla es O(1) y no puede ver el futuro: todos los accesos se
recortan en el índice actual.

Python puro: viaja al cBot, donde no hay numpy ni pandas.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from chronos.domain.enums import Side
from chronos.domain.errors import StrategyError
from chronos.domain.instrument import InstrumentSpec
from chronos.domain.position import Position

#: Columnas que una estrategia puede leer barra a barra.
PRICE_COLUMNS = ("open", "high", "low", "close", "volume")


class BarContext:
    """Vista de solo lectura de la barra en curso y del estado de la cuenta."""

    __slots__ = ("_balance", "_columns", "_equity", "_index", "_positions", "_spec", "_timestamps")

    def __init__(
        self,
        columns: Mapping[str, Sequence[float]],
        timestamps: Sequence[datetime],
        spec: InstrumentSpec,
    ) -> None:
        """`columns` lleva al menos `PRICE_COLUMNS`, alineadas con `timestamps`.

        No se copian: el cBot añade cada vela nueva a las mismas listas y el
        contexto la ve sin reconstruirse.
        """
        missing = [name for name in PRICE_COLUMNS if name not in columns]
        if missing:
            raise StrategyError(f"Faltan columnas en el contexto: {', '.join(missing)}")
        self._columns = columns
        self._timestamps = timestamps
        self._spec = spec
        self._index = 0
        self._equity = 0.0
        self._balance = 0.0
        self._positions: tuple[Position, ...] = ()

    # --- Uso interno de quien la alimenta ----------------------------------

    def update(
        self,
        *,
        index: int,
        equity: float,
        balance: float,
        positions: Sequence[Position],
    ) -> None:
        """Sitúa el contexto en la barra `index` con el estado de cuenta de ese momento."""
        self._index = index
        self._equity = equity
        self._balance = balance
        self._positions = tuple(positions)

    # --- Barra actual -------------------------------------------------------

    @property
    def index(self) -> int:
        return self._index

    @property
    def now(self) -> datetime:
        return self._timestamps[self._index]

    @property
    def open(self) -> float:
        return self.value("open")

    @property
    def high(self) -> float:
        return self.value("high")

    @property
    def low(self) -> float:
        return self.value("low")

    @property
    def close(self) -> float:
        return self.value("close")

    @property
    def volume(self) -> float:
        return self.value("volume")

    # --- Estado de la cuenta ------------------------------------------------

    @property
    def spec(self) -> InstrumentSpec:
        return self._spec

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def positions(self) -> tuple[Position, ...]:
        return self._positions

    def has_position(self, side: Side | None = None) -> bool:
        if side is None:
            return bool(self._positions)
        return any(p.side is side for p in self._positions)

    # --- Histórico ----------------------------------------------------------

    def value(self, column: str, offset: int = 0) -> float:
        """Valor de la columna `offset` barras atrás (0 = barra actual)."""
        if offset < 0:
            raise StrategyError("No se puede leer una barra futura: offset debe ser >= 0")
        position = self._index - offset
        if position < 0:
            raise StrategyError(f"Histórico insuficiente: se pidió la barra {position}")
        return float(self._column(column)[position])

    def _column(self, column: str) -> Sequence[float]:
        try:
            return self._columns[column]
        except KeyError:
            raise StrategyError(
                f"Columna desconocida '{column}'. Disponibles: {', '.join(PRICE_COLUMNS)}"
            ) from None
