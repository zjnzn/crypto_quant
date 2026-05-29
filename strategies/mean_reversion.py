"""均值回归策略 — Z-score / Bollinger Band 方法。"""

from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import TYPE_CHECKING

from application.strategy.base import BaseStrategy
from core.domain.signal import Signal
from strategies.mixin import PriceBufferMixin

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import BookEvent, FillEvent, FundingRateEvent, TradeEvent

log = logging.getLogger(__name__)


class MeanReversionStrategy(PriceBufferMixin, BaseStrategy):
    """均值回归策略。

    Z-score 超过阈值时产生反向信号。
    """

    name = "mean_reversion"
    version = "1.0.0"

    def __init__(
        self,
        *,
        window: int = 20,
        z_entry: float = 1.5,
        z_threshold: float = 2.5,
    ) -> None:
        self._init_buffer(window)
        self._z_entry = z_entry
        self._z_threshold = z_threshold

    def on_trade(self, event: TradeEvent, ctx: StrategyContext) -> list[Signal]:
        sym = event.instrument.symbol
        self._append_price(sym, event.price)

        if not self._is_warmed_up(sym):
            return []

        prices = self._prices_list(sym)
        score, confidence, z = self._calc(prices, event.price)
        if score is None:
            return []

        return [Signal(
            instrument=event.instrument,
            score=score,
            confidence=confidence,
            strategy_id=self.name,
            meta={"z_score": round(z, 3), "window": self._window},
        )]

    def on_funding(self, event: FundingRateEvent, ctx: StrategyContext) -> list[Signal]:
        """资金费率高 → 市场过热 → 空头信号。"""
        rate = float(event.rate)
        if abs(rate) < 0.001:
            return []

        score = max(-1.0, min(1.0, -rate / 0.003))
        if abs(score) < 0.1:
            return []

        return [Signal(
            instrument=event.instrument,
            score=score,
            confidence=min(1.0, abs(rate) / 0.003),
            strategy_id=self.name,
            meta={"source": "funding", "rate": rate},
        )]

    # ── 内部计算 ─────────────────────────────────────

    def _calc(self, prices: list[Decimal], current: Decimal) -> tuple[float | None, float, float]:
        floats = [float(p) for p in prices]
        mean = sum(floats) / len(floats)
        var = sum((p - mean) ** 2 for p in floats) / len(floats)
        std = math.sqrt(var)

        if std < 1e-8:
            return None, 0.0, 0.0

        z = (float(current) - mean) / std

        if abs(z) < self._z_entry:
            return None, 0.0, z

        score = max(-1.0, min(1.0, -z / self._z_threshold))
        confidence = min(1.0, abs(z) / (self._z_threshold * 1.5))

        return score, confidence, z
