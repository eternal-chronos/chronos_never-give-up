"""Estrategia de referencia: cruce de medias con stops por ATR.

NO es la estrategia del proyecto. Está aquí como baseline verificable: sirve
para comprobar que el motor, los costes, las métricas y el informe funcionan de
punta a punta, y como plantilla de cómo se escribe una fase nueva: indicadores
incrementales que avanzan una vela en cada `on_bar`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chronos.domain.context import BarContext
from chronos.domain.enums import ExitReason, Side
from chronos.domain.signal import EntrySignal, ExitSignal, StrategyAction
from chronos.domain.strategies.indicators import Atr, Ema
from chronos.domain.strategies.registry import register
from chronos.domain.strategy import Strategy


@register("ema_cross")
class EmaCrossStrategy(Strategy):
    """Largo al cruce alcista, corto al bajista. Una posición a la vez."""

    def __init__(
        self,
        fast_period: int = 20,
        slow_period: int = 50,
        atr_period: int = 14,
        sl_atr_mult: float = 2.0,
        tp_atr_mult: float = 3.0,
        allow_shorts: bool = True,
        **extra: Any,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError("fast_period debe ser menor que slow_period")
        super().__init__(
            fast_period=fast_period,
            slow_period=slow_period,
            atr_period=atr_period,
            sl_atr_mult=sl_atr_mult,
            tp_atr_mult=tp_atr_mult,
            allow_shorts=allow_shorts,
            **extra,
        )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.allow_shorts = allow_shorts
        self._fast = Ema(fast_period)
        self._slow = Ema(slow_period)
        self._atr = Atr(atr_period)
        #: Medias de la vela anterior, `None` mientras alguna calienta.
        self._previous: tuple[float, float] | None = None

    @property
    def warmup_bars(self) -> int:
        return max(self.slow_period, self.atr_period) + 1

    def on_bar(self, ctx: BarContext) -> Sequence[StrategyAction]:
        fast = self._fast.update(ctx.close)
        slow = self._slow.update(ctx.close)
        current_atr = self._atr.update(ctx.high, ctx.low, ctx.close)
        previous = self._previous
        self._previous = None if fast is None or slow is None else (fast, slow)
        if previous is None or fast is None or slow is None or current_atr is None:
            return ()
        fast_prev, slow_prev = previous

        crossed_up = fast_prev <= slow_prev and fast > slow
        crossed_down = fast_prev >= slow_prev and fast < slow
        if not (crossed_up or crossed_down):
            return ()

        side = Side.BUY if crossed_up else Side.SELL
        if side is Side.SELL and not self.allow_shorts:
            # Solo cerramos lo que hubiera abierto; no abrimos corto.
            return (ExitSignal(reason=ExitReason.SIGNAL),) if ctx.has_position() else ()

        actions: list[StrategyAction] = []
        if ctx.has_position(side.opposite()):
            actions.append(ExitSignal(reason=ExitReason.OPPOSITE_SIGNAL))
        if ctx.has_position(side):
            return actions  # ya estamos posicionados en esa dirección

        # Distancias, no precios: el fill ocurre en la apertura de la barra
        # siguiente y un nivel absoluto podría quedar del lado equivocado.
        actions.append(
            EntrySignal(
                side=side,
                stop_distance=current_atr * self.sl_atr_mult,
                take_profit_distance=current_atr * self.tp_atr_mult,
                tag="ema_cross",
            )
        )
        return actions
