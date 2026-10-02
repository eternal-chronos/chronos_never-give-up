"""El cBot de cTrader por dentro: eventos de cTrader → estrategia → órdenes.

Este módulo viaja dentro del cBot, empaquetado con `domain/` en un único
`chronos_core.py` (ver `bundle.py`). Por eso sólo importa la biblioteca estándar
y `chronos.domain`, y no importa `cAlgo.API`: recibe el objeto `api` del cBot
—el `Robot` de cTrader— y el enum `TradeType` por constructor. Así se prueba
con dobles fuera de cTrader.

Reparto de papeles:
  - Velas: cTrader llama a `on_bar_closed` al cerrar cada vela y, en ese evento,
    `api.Bars.LastBar` es la vela recién cerrada. La estrategia ve cada vela
    cerrada una sola vez y en orden.
  - Arranque: las velas que el gráfico ya tiene sirven de calentamiento. La
    estrategia las ve, pero lo que pida sobre ellas se descarta: son pasado.
    Sólo se opera a partir de la primera vela que cierra con el cBot en marcha.
  - Señal en la vela `t` → orden a mercado en ese instante, al precio de `t+1`.
  - Stop loss y take profit viajan con la orden, en pips desde el precio real
    de entrada, y los ejecuta el servidor: protegen la posición aunque el cBot
    se pare.
  - Posiciones: las manda el bróker. En cada vela se leen de `api.Positions`
    —las de este símbolo con la etiqueta de este cBot—; no hay copia en memoria
    que reconciliar al arrancar.

Las fechas llegan en UTC porque el cBot se declara con `TimeZone = TimeZones.UTC`
(lo escribe `bundle.py` en el `.cs`).
"""

from __future__ import annotations

import math
import sys
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chronos.domain.context import PRICE_COLUMNS, BarContext
from chronos.domain.enums import Side
from chronos.domain.errors import DomainError, InvalidOrder, StrategyError
from chronos.domain.instrument import InstrumentSpec
from chronos.domain.position import Position
from chronos.domain.signal import EntrySignal, ExitSignal, ModifyStops, StrategyAction
from chronos.domain.strategy import Strategy


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    """Lo que el cBot decide además de la estrategia: identidad y tamaño.

    El tamaño sigue el mismo orden que el motor de backtest: lotes explícitos de
    la señal, después su `risk_fraction`, y si no trae ninguno, `risk_per_trade`
    (si es > 0) o el lote fijo.
    """

    label: str
    fixed_lots: float
    risk_per_trade: float = 0.0  # fracción del equity; 0 = lote fijo
    max_lots: float = 1.0
    max_positions: int = 1

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise DomainError("El cBot necesita una etiqueta para reconocer sus posiciones")
        if self.fixed_lots <= 0 or self.max_lots <= 0:
            raise DomainError("fixed_lots y max_lots deben ser positivos")
        if not 0 <= self.risk_per_trade < 1:
            raise DomainError("risk_per_trade debe estar en [0, 1)")
        if self.max_positions < 1:
            raise DomainError("max_positions debe ser >= 1")


class CTraderRunner:
    """Alimenta la estrategia con las velas de cTrader y ejecuta lo que pide."""

    def __init__(
        self, api: Any, trade_type: Any, strategy: Strategy, settings: RunnerSettings
    ) -> None:
        if type(strategy).on_trade_closed is not Strategy.on_trade_closed:
            # En el backtest se le avisa de cada cierre; aquí todavía no se
            # traducen los cierres de cTrader. Arrancar sin avisarle haría que
            # la misma estrategia se comportara distinto en vivo.
            raise StrategyError(
                f"La estrategia '{strategy.name}' usa on_trade_closed y el cBot todavía no "
                "le avisa de los cierres: no se arranca"
            )
        self._api = api
        self._buy = trade_type.Buy
        self._sell = trade_type.Sell
        self._strategy = strategy
        self._settings = settings
        self._warmup = max(strategy.warmup_bars, 1)
        self._columns: dict[str, list[float]] = {name: [] for name in PRICE_COLUMNS}
        self._timestamps: list[datetime] = []
        self._context: BarContext | None = None

    # --- Eventos de cTrader -------------------------------------------------

    def start(self) -> None:
        """`on_start`: calienta con el histórico del gráfico. No opera."""
        api = self._api
        self._context = BarContext(self._columns, self._timestamps, self._symbol_spec())
        bars = api.Bars
        # Al arrancar, la última vela de `Bars` es la que está en formación.
        for index in range(int(bars.Count) - 1):
            self._feed(bars[index], positions=())
        self._log(
            f"chronos {self._strategy.name} | Python {sys.version.split()[0]} | "
            f"{len(self._timestamps)} velas de histórico como calentamiento "
            f"(necesita {self._warmup})"
        )
        if self._timestamps:
            self._log(f"primera vela {self._timestamps[0]:%Y-%m-%d %H:%M}, última {self._timestamps[-1]:%Y-%m-%d %H:%M} UTC")
        open_positions = self._positions()
        self._log(
            f"posiciones abiertas con la etiqueta '{self._settings.label}': {len(open_positions)}"
        )

    def on_bar_closed(self) -> None:
        """`on_bar_closed`: la estrategia decide sobre la vela recién cerrada."""
        bar = self._api.Bars.LastBar
        if self._timestamps and _utc(bar.OpenTime) <= self._timestamps[-1]:
            return  # ya vista
        actions = self._feed(bar, positions=self._positions())
        if len(self._timestamps) - 1 < self._warmup:
            return
        self._execute(actions)

    def stop(self) -> None:
        """`on_stop`. Las posiciones abiertas siguen con su SL/TP en el servidor."""
        self._strategy.on_finish()
        self._log(
            f"cBot parado; posiciones abiertas que siguen en el bróker: {len(self._positions())}"
        )

    # --- Velas ----------------------------------------------------------------

    def _feed(self, bar: Any, *, positions: Sequence[Position]) -> Sequence[StrategyAction]:
        self._timestamps.append(_utc(bar.OpenTime))
        self._columns["open"].append(float(bar.Open))
        self._columns["high"].append(float(bar.High))
        self._columns["low"].append(float(bar.Low))
        self._columns["close"].append(float(bar.Close))
        self._columns["volume"].append(float(bar.TickVolume))
        context = self._require_context()
        account = self._api.Account
        context.update(
            index=len(self._timestamps) - 1,
            equity=float(account.Equity),
            balance=float(account.Balance),
            positions=positions,
        )
        return self._strategy.on_bar(context)

    # --- Intenciones → órdenes -------------------------------------------------

    def _execute(self, actions: Sequence[StrategyAction]) -> None:
        for action in actions:
            if isinstance(action, EntrySignal):
                self._open(action)
            elif isinstance(action, ExitSignal):
                self._close(action)
            elif isinstance(action, ModifyStops):
                self._modify(action)
            else:  # pragma: no cover - defensivo
                raise StrategyError(f"Acción de estrategia desconocida: {action!r}")

    def _open(self, signal: EntrySignal) -> None:
        api = self._api
        if len(self._broker_positions()) >= self._settings.max_positions:
            self._log(f"entrada {signal.side} descartada: ya hay {self._settings.max_positions} posiciones")
            return
        symbol = api.Symbol
        reference = float(symbol.Ask) if signal.side is Side.BUY else float(symbol.Bid)
        try:
            signal.validate_against(reference)
        except InvalidOrder as error:
            self._log(f"entrada {signal.side} descartada: {error}")
            return
        stop_loss, take_profit = signal.levels(reference)
        units = self._units(signal, reference, stop_loss)
        if units <= 0:
            self._log(f"entrada {signal.side} descartada: el tamaño no llega al mínimo del símbolo")
            return
        pip = float(symbol.PipSize)
        result = api.ExecuteMarketOrder(
            self._buy if signal.side is Side.BUY else self._sell,
            api.SymbolName,
            units,
            self._settings.label,
            None if stop_loss is None else abs(reference - stop_loss) / pip,
            None if take_profit is None else abs(take_profit - reference) / pip,
            signal.tag,
        )
        if not result.IsSuccessful:
            self._log(f"orden {signal.side} rechazada por el bróker: {result.Error}")
            return
        self._log(f"abierta {signal.side} {units:g} unidades, SL {stop_loss}, TP {take_profit}")

    def _units(self, signal: EntrySignal, reference: float, stop_loss: float | None) -> float:
        """Volumen en unidades de cTrader, redondeado hacia abajo al paso del símbolo."""
        symbol = self._api.Symbol
        max_units = float(symbol.QuantityToVolumeInUnits(self._settings.max_lots))
        if signal.volume is not None:
            units = float(symbol.QuantityToVolumeInUnits(signal.volume))
        else:
            fraction = signal.risk_fraction
            if fraction is None and self._settings.risk_per_trade > 0:
                fraction = self._settings.risk_per_trade
            if fraction is None:
                units = float(symbol.QuantityToVolumeInUnits(self._settings.fixed_lots))
            elif stop_loss is None:
                return 0.0  # arriesgar un % sin stop no tiene tamaño
            else:
                # PipValue: dinero que mueve un pip por unidad, en la divisa de
                # la cuenta. Lo da el bróker, con su conversión de divisa.
                pips = abs(reference - stop_loss) / float(symbol.PipSize)
                units = float(self._api.Account.Equity) * fraction / (pips * float(symbol.PipValue))
        step = float(symbol.VolumeInUnitsStep)
        units = math.floor(min(units, max_units) / step + 1e-9) * step
        return units if units >= float(symbol.VolumeInUnitsMin) else 0.0

    def _close(self, signal: ExitSignal) -> None:
        for position in self._broker_positions():
            if signal.position_id is not None and int(position.Id) != signal.position_id:
                continue
            result = self._api.ClosePosition(position)
            if not result.IsSuccessful:
                self._log(f"no se pudo cerrar la posición {position.Id}: {result.Error}")
            else:
                self._log(f"cerrada la posición {position.Id} ({signal.reason})")

    def _modify(self, signal: ModifyStops) -> None:
        spec = self._require_context().spec
        for position in self._broker_positions():
            if signal.position_id is not None and int(position.Id) != signal.position_id:
                continue
            # Mismas invariantes que el bróker simulado, comprobadas sobre la
            # vista de dominio antes de mandar nada.
            view = self._to_domain(position)
            try:
                if signal.stop_loss is not None:
                    view.move_stop(spec.round_price(signal.stop_loss), allow_adverse=True)
                if signal.take_profit is not None:
                    view.move_take_profit(spec.round_price(signal.take_profit))
            except InvalidOrder as error:
                self._log(f"modificación de la posición {position.Id} descartada: {error}")
                continue
            stop_loss = view.stop_loss if signal.stop_loss is not None else position.StopLoss
            take_profit = view.take_profit if signal.take_profit is not None else position.TakeProfit
            result = self._api.ModifyPosition(position, stop_loss, take_profit)
            if not result.IsSuccessful:
                self._log(f"el bróker rechazó modificar la posición {position.Id}: {result.Error}")

    # --- Posiciones: las del bróker ---------------------------------------------

    def _broker_positions(self) -> list[Any]:
        api = self._api
        symbol_name = str(api.SymbolName)
        return [
            position
            for position in api.Positions
            if str(position.Label) == self._settings.label and str(position.SymbolName) == symbol_name
        ]

    def _positions(self) -> list[Position]:
        return [self._to_domain(position) for position in self._broker_positions()]

    def _to_domain(self, position: Any) -> Position:
        entry_time = _utc(position.EntryTime)
        view = Position(
            id=int(position.Id),
            symbol=str(position.SymbolName),
            side=Side.BUY if position.TradeType == self._buy else Side.SELL,
            volume=float(position.Quantity),
            entry_price=float(position.EntryPrice),
            entry_time=entry_time,
            # La vela en la que se abrió, contada desde el inicio del histórico
            # del cBot; -1 si es anterior.
            entry_index=bisect_right(self._timestamps, entry_time) - 1,
            tag=str(position.Comment or ""),
        )
        # Es la foto de lo que hay en el bróker, no una orden nueva: el stop se
        # coloca sin exigirle lado, como cuando ya se ha movido a favor.
        if position.StopLoss is not None:
            view.move_stop(float(position.StopLoss), allow_adverse=True)
        if position.TakeProfit is not None:
            try:
                view.move_take_profit(float(position.TakeProfit))
            except InvalidOrder:
                self._log(
                    f"la posición {position.Id} tiene un TP del lado de la pérdida "
                    "(¿editado a mano?): la estrategia no lo ve"
                )
        return view

    # --- Utilidades ---------------------------------------------------------------

    def _symbol_spec(self) -> InstrumentSpec:
        """Ficha del símbolo con los datos del bróker. Los costes no se leen: la
        estrategia no los usa, y en vivo los cobra el bróker."""
        api = self._api
        symbol = api.Symbol
        lot = float(symbol.LotSize)
        return InstrumentSpec(
            symbol=str(api.SymbolName),
            digits=int(symbol.Digits),
            tick_size=float(symbol.TickSize),
            pip_size=float(symbol.PipSize),
            contract_size=lot,
            min_lot=float(symbol.VolumeInUnitsMin) / lot,
            max_lot=float(symbol.VolumeInUnitsMax) / lot,
            lot_step=float(symbol.VolumeInUnitsStep) / lot,
        )

    def _require_context(self) -> BarContext:
        if self._context is None:
            raise StrategyError("El cBot no ha arrancado: falta llamar a start()")
        return self._context

    def _log(self, message: str) -> None:
        self._api.Print(message)


def _utc(moment: Any) -> datetime:
    """Fecha de cTrader en UTC.

    Llega como `System.DateTime` de .NET (con `Year`, `Month`...) o, según la
    versión del puente, ya convertida a `datetime`.
    """
    if isinstance(moment, datetime):
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return datetime(
        int(moment.Year),
        int(moment.Month),
        int(moment.Day),
        int(moment.Hour),
        int(moment.Minute),
        int(moment.Second),
        tzinfo=UTC,
    )
