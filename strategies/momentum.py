"""动量策略 — 趋势跟随。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from application.strategy.base import BaseStrategy
from core.domain.signal import Signal
from strategies.mixin import PriceBufferMixin

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import BookEvent, FillEvent, TradeEvent

log = logging.getLogger(__name__)


class MomentumStrategy(PriceBufferMixin, BaseStrategy):
    """价格动量策略。

    当窗口期收益率超过阈值时产生信号。
    """

    name = "momentum"
    version = "1.0.0"

    def __init__(
        self,
        *,
        window: int = 20,
        scale: float = 0.05,
        min_score: float = 0.20,
    ) -> None:
        self._init_buffer(window)
        self._scale = scale
        self._min_score = min_score

    def on_trade(self, event: TradeEvent, ctx: StrategyContext) -> list[Signal]:
        sym = event.instrument.symbol
        self._append_price(sym, event.price)

        if not self._is_warmed_up(sym):
            return []

        prices = self._prices_list(sym)
        ret = float(prices[-1] / prices[0] - 1)
        score = max(-1.0, min(1.0, ret / self._scale))
        if abs(score) < self._min_score:
            return []

        confidence = min(1.0, abs(ret) / (self._scale * 2))

        return [Signal(
            instrument=event.instrument,
            score=score,
            confidence=confidence,
            strategy_id=self.name,
            meta={"ret": ret, "window": self._window},
        )]
