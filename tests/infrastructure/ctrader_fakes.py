"""Dobles del `Robot` de cTrader para probar el cBot fuera de cTrader.

Imitan sólo lo que usa el runner, con los nombres de la API de cTrader tal como
aparecen en los ejemplos oficiales de Spotware para Python
(github.com/spotware/ctrader-python-algo-samples). Sólo biblioteca estándar: el
test del cBot empaquetado copia este fichero junto al núcleo y lo ejecuta en un
Python sin chronos, ni numpy, ni pandas.

No simulan stops ni objetivos: una posición sólo se cierra con `ClosePosition`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


class TradeType:
    """`cAlgo.API.TradeType`."""

    Buy = "Buy"
    Sell = "Sell"


class NetDateTime:
    """`System.DateTime` tal como llega por el puente: atributos, sin zona."""

    def __init__(self, moment: datetime) -> None:
        self.Year = moment.year
        self.Month = moment.month
        self.Day = moment.day
        self.Hour = moment.hour
        self.Minute = moment.minute
        self.Second = moment.second
        self.moment = moment


@dataclass
class Bar:
    OpenTime: Any
    Open: float
    High: float
    Low: float
    Close: float
    TickVolume: float = 100.0


class Bars:
    """`Robot.Bars`. Al arrancar, la última es la vela en formación."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = list(bars)

    @property
    def Count(self) -> int:
        return len(self._bars)

    @property
    def LastBar(self) -> Bar:
        return self._bars[-1]

    def __getitem__(self, index: int) -> Bar:
        return self._bars[index]

    def close(self, bar: Bar) -> None:
        """La vela `bar` acaba de cerrar: si era la que estaba en formación, la sustituye."""
        if self._bars and _opened(self._bars[-1]) == _opened(bar):
            self._bars[-1] = bar
        else:
            self._bars.append(bar)


@dataclass
class Symbol:
    """`Robot.Symbol` de XAUUSD: un lote son 100 onzas y un pip, 0.01."""

    Ask: float = 2000.2
    Bid: float = 2000.0
    PipSize: float = 0.01
    PipValue: float = 0.01  # USD por pip y por unidad (onza)
    TickSize: float = 0.01
    Digits: int = 2
    LotSize: float = 100.0
    VolumeInUnitsMin: float = 1.0
    VolumeInUnitsStep: float = 1.0
    VolumeInUnitsMax: float = 10_000.0

    def QuantityToVolumeInUnits(self, lots: float) -> float:
        return lots * self.LotSize


@dataclass
class Account:
    Equity: float = 10_000.0
    Balance: float = 10_000.0


@dataclass
class Position:
    """`cAlgo.API.Position`."""

    Id: int
    Label: str
    SymbolName: str
    TradeType: str
    VolumeInUnits: float
    EntryPrice: float
    EntryTime: Any
    StopLoss: float | None = None
    TakeProfit: float | None = None
    Comment: str = ""

    @property
    def Quantity(self) -> float:
        return self.VolumeInUnits / 100.0


@dataclass
class TradeResult:
    IsSuccessful: bool = True
    Error: str | None = None
    Position: Position | None = None


@dataclass
class Robot:
    """El objeto `api`: el `Robot` del cBot, con sus parámetros como atributos."""

    bars: list[Bar]
    SymbolName: str = "XAUUSD"
    Symbol: Symbol = field(default_factory=Symbol)
    Account: Account = field(default_factory=Account)
    Positions: list[Position] = field(default_factory=list)
    printed: list[str] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    modified: list[tuple[int, float | None, float | None]] = field(default_factory=list)
    reject_orders: bool = False

    def __post_init__(self) -> None:
        self.Bars = Bars(self.bars)
        self._next_id = 100
        self._forming = True  # la última vela de `bars` está en formación

    def with_parameters(self, **parameters: Any) -> Robot:
        """Los `[Parameter]` del `.cs`, que el cBot lee como `api.FastPeriod`..."""
        for name, value in parameters.items():
            setattr(self, name, value)
        return self

    def close_bar(self, bar: Bar) -> None:
        self.Bars.close(bar)
        self._forming = False

    def close_next(self, close: float) -> None:
        """Cierra en `close` la vela en formación o, si no la hay, la siguiente."""
        last = self.Bars.LastBar
        if self._forming:
            opened, open_ = _opened(last), last.Open
        else:
            opened, open_ = _opened(last) + timedelta(minutes=5), last.Close
        self.close_bar(
            Bar(
                OpenTime=NetDateTime(opened),
                Open=open_,
                High=max(open_, close) + 0.5,
                Low=min(open_, close) - 0.5,
                Close=close,
            )
        )

    # --- API de cTrader ---------------------------------------------------------

    def Print(self, message: Any) -> None:
        self.printed.append(str(message))

    def ExecuteMarketOrder(
        self,
        trade_type: str,
        symbol_name: str,
        volume: float,
        label: str | None = None,
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
        comment: str | None = None,
    ) -> TradeResult:
        self.orders.append(
            {
                "trade_type": trade_type,
                "symbol": symbol_name,
                "volume": volume,
                "label": label,
                "stop_loss_pips": stop_loss_pips,
                "take_profit_pips": take_profit_pips,
                "comment": comment,
                "bar": self.Bars.Count - 1,
            }
        )
        if self.reject_orders:
            return TradeResult(IsSuccessful=False, Error="NoMoney")
        buy = trade_type == TradeType.Buy
        entry = self.Symbol.Ask if buy else self.Symbol.Bid
        sign = 1 if buy else -1
        pip = self.Symbol.PipSize
        position = Position(
            Id=self._next_id,
            Label=label or "",
            SymbolName=symbol_name,
            TradeType=trade_type,
            VolumeInUnits=volume,
            EntryPrice=entry,
            EntryTime=NetDateTime(_opened(self.Bars.LastBar) + timedelta(minutes=5)),
            StopLoss=None if stop_loss_pips is None else entry - sign * stop_loss_pips * pip,
            TakeProfit=None if take_profit_pips is None else entry + sign * take_profit_pips * pip,
            Comment=comment or "",
        )
        self._next_id += 1
        self.Positions.append(position)
        return TradeResult(Position=position)

    def ClosePosition(self, position: Position) -> TradeResult:
        self.Positions.remove(position)
        self.closed.append(position.Id)
        return TradeResult()

    def ModifyPosition(
        self, position: Position, stop_loss: float | None, take_profit: float | None
    ) -> TradeResult:
        position.StopLoss = stop_loss
        position.TakeProfit = take_profit
        self.modified.append((position.Id, stop_loss, take_profit))
        return TradeResult()


def make_bars(
    closes: list[float], start: datetime, minutes: int = 5, net_dates: bool = True
) -> list[Bar]:
    """Velas encadenadas: cada una abre en el cierre de la anterior."""
    bars: list[Bar] = []
    previous = closes[0]
    for i, close in enumerate(closes):
        moment = start + timedelta(minutes=minutes * i)
        bars.append(
            Bar(
                OpenTime=NetDateTime(moment) if net_dates else moment,
                Open=previous,
                High=max(previous, close) + 0.5,
                Low=min(previous, close) - 0.5,
                Close=close,
            )
        )
        previous = close
    return bars


def _opened(bar: Bar) -> datetime:
    moment = bar.OpenTime
    return moment.moment if isinstance(moment, NetDateTime) else moment
