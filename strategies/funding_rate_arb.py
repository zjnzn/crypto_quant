"""资金费率套利策略 — 利用永续/交割资金费率差异。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from application.strategy.base import BaseStrategy
from core.domain.signal import Signal

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import BookEvent, FillEvent, FundingRateEvent, TradeEvent

log = logging.getLogger(__name__)


class FundingRateArbStrategy(BaseStrategy):
    """资金费率套利策略。

    当资金费率超过阈值时产生反向信号：
    - 费率过高 → 做空（收取资金费）
    - 费率过低 → 做多（收取资金费）
    """

    name = "funding_rate_arb"
    version = "1.0.0"

    def __init__(
        self,
        *,
        threshold: Decimal = Decimal("0.001"),
        max_score: float = 0.6,
    ) -> None:
        self._threshold = threshold
        self._max_score = max_score
        self._bar_count: dict[str, int] = {}

    def on_funding(self, event: FundingRateEvent, ctx: StrategyContext) -> list[Signal]:
        rate = event.rate
        if abs(rate) < self._threshold:
            return []

        score = max(-self._max_score, min(self._max_score, float(-rate) / 0.003))
        confidence = min(1.0, float(abs(rate)) / 0.003)

        return [Signal(
            instrument=event.instrument,
            score=score,
            confidence=confidence,
            strategy_id=self.name,
            meta={"rate": float(rate), "threshold": float(self._threshold)},
        )]

    def on_trade(self, event: TradeEvent, ctx: StrategyContext) -> list[Signal]:
        sym = event.instrument.symbol
        self._bar_count[sym] = self._bar_count.get(sym, 0) + 1
        return []

    def on_start(self, ctx) -> None:
        log.info(
            "%s 启动  threshold=%.4f%%  max_score=%.2f",
            self.name, float(self._threshold) * 100, self._max_score,
        )

    def on_stop(self, ctx) -> None:
        for sym, count in self._bar_count.items():
            log.info("%s 停止  %s: %d bars processed", self.name, sym, count)
