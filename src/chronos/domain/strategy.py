"""Contrato que toda estrategia debe cumplir.

El motor depende de esta clase base, no de ninguna estrategia concreta. Las
estrategias viven en `chronos.domain.strategies`: Python puro, sin red, sin
ficheros y sin reloj, porque el mismo código corre en el backtest y dentro del
cBot de cTrader, donde no hay numpy ni pandas.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from chronos.domain.context import BarContext
from chronos.domain.signal import StrategyAction
from chronos.domain.trade import Trade


class Strategy(ABC):
    """Estrategia dirigida por barras, vela a vela.

    Ciclo de vida por corrida:
        on_bar(ctx)    -> una vez por CADA barra cerrada, en orden
        on_trade_closed(trade) -> tras cada cierre
        on_finish()    -> al terminar

    Los indicadores se calculan dentro de `on_bar`, de forma incremental (ver
    `chronos.domain.strategies.indicators`): la estrategia ve todas las barras,
    también las del calentamiento y las del corte de sesión, para que sus
    indicadores avancen sin huecos. Lo que pida en esas barras se descarta.
    """

    name: ClassVar[str] = "unnamed"

    def __init__(self, **params: Any) -> None:
        self.params: Mapping[str, Any] = dict(params)

    # --- Ciclo de vida ------------------------------------------------------

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Sequence[StrategyAction]:
        """Decide qué hacer en la barra actual. Devolver `()` significa esperar."""

    def on_trade_closed(self, trade: Trade) -> None:
        """Notificación de cierre. Útil para estados tipo martingala o cooldown."""

    def on_finish(self) -> None:
        """Cierre de la corrida."""

    # --- Requisitos ---------------------------------------------------------

    @property
    def warmup_bars(self) -> int:
        """Barras iniciales cuyas intenciones se descartan (indicadores sin valor)."""
        return 0

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "params": dict(self.params)}
