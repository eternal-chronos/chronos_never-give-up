"""Del fichero de precios a las velas de cada temporalidad.

Es el punto donde se junta todo lo que el explorador necesita y no hay nada más:
se lee el histórico, se agrega a las temporalidades pedidas y se devuelve. Ni una
regla, ni un indicador, ni una señal.

Una temporalidad que el histórico no da para construir —M15 o M5 desde un
histórico H1— **no rompe la corrida**: se anota por qué no está y las demás se
dibujan igual. Lo que no puede pasar es fabricar velas finas a partir de velas
gruesas, y de eso ya se encarga `aggregate`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from chronos.application.chart.config import ExplorerConfig
from chronos.domain.errors import DomainError
from chronos.infrastructure.market.aggregation import AggregatedSeries, aggregate
from chronos.infrastructure.market.loader import SidedHistory, load_history


@dataclass(frozen=True, slots=True)
class ChartRun:
    """Las velas del símbolo, por temporalidad, con la traza de cómo se armaron."""

    config: ExplorerConfig
    history: SidedHistory
    #: Temporalidad -> velas agregadas, de mayor a menor. Sólo las que se pudieron armar.
    series: dict[str, AggregatedSeries]
    #: Las que no se pudieron construir, con el motivo. Se imprimen y se dibujan.
    skipped: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.series:
            raise DomainError(
                f"{self.config.symbol}: el histórico no da para ninguna de las "
                "temporalidades pedidas"
            )

    @property
    def frames(self) -> dict[str, pd.DataFrame]:
        return {timeframe: item.frame for timeframe, item in self.series.items()}

    @property
    def timeframes(self) -> tuple[str, ...]:
        return tuple(self.series)


def build_chart_run(config: ExplorerConfig) -> ChartRun:
    """Carga el histórico y lo agrega. Levanta `DomainError` si no sirve para dibujar."""
    history = load_history(config.data, config.price_side)
    series: dict[str, AggregatedSeries] = {}
    skipped: list[str] = []
    for timeframe in config.ordered_timeframes:
        try:
            series[timeframe] = aggregate(history.frame, timeframe, config.aggregation)
        except DomainError as error:
            # Un histórico H1 sirve para el diario, H4 y H1 pero no para M15 ni
            # M5. Quedarse sin ese gráfico es peor que no tener ninguno, pero
            # mucho mejor que fabricar velas falsas: se anota y el resto se dibuja.
            skipped.append(f"{timeframe}: {error}")
    return ChartRun(config=config, history=history, series=series, skipped=tuple(skipped))
