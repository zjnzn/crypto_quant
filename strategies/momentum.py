"""
Momentum strategy.

Live mode should use the time-window parameters `window_sec` and
`slope_scale`, because Binance aggTrade emits tick-by-tick trades rather than
fixed interval bars. The old count-window parameters `window` and `scale` are
kept for existing tests/backtests and old configs.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING

from core.domain.signal import Signal

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import BookEvent, FillEvent, FundingRateEvent, TradeEvent

log = logging.getLogger(__name__)


class MomentumStrategy:
    """Momentum strategy compatible with both tick-time and legacy bar modes."""

    name = "momentum"
    version = "1.0.0"

    def __init__(
        self,
        window_sec: int | None = None,
        slope_scale: float | None = None,
        window: int | None = None,
        scale: float | None = None,
        name_override: str | None = None,
    ) -> None:
        if name_override:
            self.name = name_override

        self._legacy_mode = window is not None or scale is not None
        if self._legacy_mode:
            self._window = window or 20
            self._scale = scale or 0.05
            self._prices: dict[str, deque[Decimal]] = {}
            return

        self._window_sec = window_sec if window_sec is not None else 300
        self._slope_scale = slope_scale if slope_scale is not None else 0.00001
        self._prices: dict[str, deque[tuple[float, Decimal]]] = {}

    def on_trade(self, event: "TradeEvent", ctx: "StrategyContext") -> list[Signal]:
        if self._legacy_mode:
            return self._on_trade_legacy(event)
        return self._on_trade_time_window(event)

    def on_book(self, event: "BookEvent", ctx: "StrategyContext") -> list[Signal]:
        return []

    def on_funding(
        self, event: "FundingRateEvent", ctx: "StrategyContext"
    ) -> list[Signal]:
        return []

    def on_fill(self, event: "FillEvent", ctx: "StrategyContext") -> None:
        pass

    def on_start(self, ctx: "StrategyContext") -> None:
        if self._legacy_mode:
            log.info(
                "%s v%s start legacy window=%d scale=%.4f",
                self.name,
                self.version,
                self._window,
                self._scale,
            )
            return

        log.info(
            "%s v%s start window_sec=%d slope_scale=%.2e",
            self.name,
            self.version,
            self._window_sec,
            self._slope_scale,
        )

    def on_stop(self, ctx: "StrategyContext") -> None:
        log.info("%s stop", self.name)

    def _on_trade_legacy(self, event: "TradeEvent") -> list[Signal]:
        sym = event.instrument.symbol
        if sym not in self._prices:
            self._prices[sym] = deque(maxlen=self._window)

        buf = self._prices[sym]
        buf.append(event.price)
        if len(buf) < self._window:
            return []

        prices = list(buf)
        score, confidence = self._calc_score(prices)
        return [
            Signal(
                instrument=event.instrument,
                score=score,
                confidence=confidence,
                strategy_id=self.name,
                meta={
                    "ret": float(prices[-1] / prices[0] - 1),
                    "window": self._window,
                },
            )
        ]

    def _on_trade_time_window(self, event: "TradeEvent") -> list[Signal]:
        sym = event.instrument.symbol
        now = event.ts.timestamp() if getattr(event, "ts", None) else time.time()

        if sym not in self._prices:
            self._prices[sym] = deque()

        queue = self._prices[sym]
        queue.append((now, event.price))

        cutoff = now - self._window_sec
        while queue and queue[0][0] < cutoff:
            queue.popleft()

        if len(queue) < 10:
            return []

        start_time = queue[0][0]
        end_time = queue[-1][0]
        if (end_time - start_time) < self._window_sec * 0.5:
            return []

        slope, score, confidence = self._calc_slope_score(list(queue))
        expected_return = (
            math.exp(slope * self._window_sec) - 1 if abs(slope) < 0.01 else 0.0
        )

        return [
            Signal(
                instrument=event.instrument,
                score=score,
                confidence=confidence,
                strategy_id=self.name,
                meta={
                    "slope": slope,
                    "ret": expected_return,
                    "window_sec": self._window_sec,
                    "data_points": len(queue),
                },
            )
        ]

    def _calc_score(self, prices: list[Decimal]) -> tuple[float, float]:
        if len(prices) < 2 or prices[0] <= 0:
            return 0.0, 0.0

        ret = float(prices[-1] / prices[0] - 1)
        score = max(-1.0, min(1.0, ret / self._scale))
        confidence = min(1.0, abs(ret) / (self._scale * 2))
        return score, confidence

    def _calc_slope_score(
        self, price_points: list[tuple[float, Decimal]]
    ) -> tuple[float, float, float]:
        if len(price_points) < 2:
            return 0.0, 0.0, 0.0

        t0 = price_points[0][0]
        x_vals = [t - t0 for t, _ in price_points]
        y_vals = [math.log(float(p)) for _, p in price_points if p > 0]

        if len(x_vals) != len(y_vals) or len(x_vals) < 2:
            return 0.0, 0.0, 0.0

        n = len(x_vals)
        mean_x = sum(x_vals) / n
        mean_y = sum(y_vals) / n

        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_vals, y_vals))
        denominator = sum((x - mean_x) ** 2 for x in x_vals)
        if denominator < 1e-10:
            return 0.0, 0.0, 0.0

        slope = numerator / denominator
        score = max(-1.0, min(1.0, slope / self._slope_scale))
        confidence = min(1.0, abs(slope) / (self._slope_scale * 2))
        return slope, score, confidence
