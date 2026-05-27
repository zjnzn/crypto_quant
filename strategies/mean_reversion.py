"""
strategies/mean_reversion.py

均值回归策略（Z-score / Bollinger Band 方法）。

信号逻辑：
  1. 计算窗口内滚动均值和标准差
  2. Z-score = (当前价 - 均值) / 标准差
  3. |Z| < z_entry  → 无信号（价格在正常波动范围内）
  4. Z > z_entry    → 价格偏高，做空信号（score < 0，期待回归）
  5. Z < -z_entry   → 价格偏低，做多信号（score > 0，期待回归）
  6. score = -clip(Z / z_threshold, -1, 1)

与动量策略的关系：
  - 动量策略追趋势（高 → 更高）
  - 均值回归策略逆趋势（高 → 回落）
  - 两者结合：趋势初期动量主导，趋势末期均值回归主导
"""
from __future__ import annotations

import logging
import math
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING

from core.domain.signal import Signal

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import (BookEvent, FillEvent,
                                    FundingRateEvent, TradeEvent)

log = logging.getLogger(__name__)


class MeanReversionStrategy:
    """
    均值回归策略。满足 Strategy 协议，无需继承。

    示例（config.yaml）：
      strategies:
        - module: "strategies.mean_reversion.MeanReversionStrategy"
          params: {window: 20, z_entry: 1.5, z_threshold: 2.5}
    """

    name    = "mean_reversion"
    version = "1.0.0"

    def __init__(
        self,
        window:      int   = 20,
        z_entry:     float = 1.5,    # 触发信号的最小 Z-score 绝对值
        z_threshold: float = 2.5,    # score=±1 对应的 Z-score 值
    ) -> None:
        self._window      = window
        self._z_entry     = z_entry
        self._z_threshold = z_threshold
        self._prices: dict[str, deque[Decimal]] = {}

    # ── Strategy 协议 ──────────────────────────────────────────────────────────

    def on_trade(self, event: "TradeEvent",
                 ctx: "StrategyContext") -> list[Signal]:
        sym = event.instrument.symbol
        if sym not in self._prices:
            self._prices[sym] = deque(maxlen=self._window)
        buf = self._prices[sym]
        buf.append(event.price)

        if len(buf) < self._window:
            return []   # 预热期

        score, confidence, z = self._calc(list(buf), event.price)
        if score is None:
            return []

        return [Signal(
            instrument  = event.instrument,
            score       = score,
            confidence  = confidence,
            strategy_id = self.name,
            meta        = {
                "z_score": round(z, 3),
                "window":  self._window,
            },
        )]

    def on_book(self, event: "BookEvent",
                ctx: "StrategyContext") -> list[Signal]:
        return []

    def on_funding(self, event: "FundingRateEvent",
                   ctx: "StrategyContext") -> list[Signal]:
        """资金费率高 → 市场过热 → 空头信号。"""
        rate = float(event.rate)
        if abs(rate) < 0.001:   # < 0.1% 忽略
            return []

        # 资金费率为正（多头付费）→ 市场偏多头 → 偏空信号
        score = max(-1.0, min(1.0, -rate / 0.003))
        if abs(score) < 0.1:
            return []

        return [Signal(
            instrument  = event.instrument,
            score       = score,
            confidence  = min(1.0, abs(rate) / 0.003),
            strategy_id = self.name,
            meta        = {"source": "funding", "rate": rate},
        )]

    def on_fill(self, event: "FillEvent",
                ctx: "StrategyContext") -> None:
        pass

    def on_start(self, ctx: "StrategyContext") -> None:
        log.info("%s 启动  window=%d  z_entry=%.1f  z_threshold=%.1f",
                 self.name, self._window, self._z_entry, self._z_threshold)

    def on_stop(self, ctx: "StrategyContext") -> None:
        log.info("%s 停止", self.name)

    # ── 内部计算 ───────────────────────────────────────────────────────────────

    def _calc(self, prices: list[Decimal],
              current: Decimal) -> tuple[float | None, float, float]:
        """
        返回 (score, confidence, z_score)。
        当信号不足时返回 (None, 0, 0)。
        """
        floats = [float(p) for p in prices]
        mean   = sum(floats) / len(floats)
        var    = sum((p - mean) ** 2 for p in floats) / len(floats)
        std    = math.sqrt(var)

        if std < 1e-8:              # 价格完全没有波动，忽略
            return None, 0.0, 0.0

        z = (float(current) - mean) / std

        if abs(z) < self._z_entry:  # 未达触发阈值
            return None, 0.0, z

        # score = -Z（价格偏高 → 做空，score < 0）
        score      = max(-1.0, min(1.0, -z / self._z_threshold))
        confidence = min(1.0, abs(z) / (self._z_threshold * 1.5))

        return score, confidence, z
