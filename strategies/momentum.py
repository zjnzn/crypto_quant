"""
strategies/momentum.py

动量策略（Momentum）。

信号逻辑：
  1. 维护每个标的的滚动收盘价窗口（长度 = window）
  2. 计算窗口内的累计收益率：ret = close[-1] / close[0] - 1
  3. 归一化为 [-1, 1] 分数：score = clip(ret / scale, -1, 1)
  4. |score| < min_score 时返回空列表（无信号）
  5. confidence = min(1, |ret| / (scale * 2))

参数：
  window    — 动量计算窗口长度（默认 20 根 K 线）
  scale     — 归一化基准收益率（默认 5%，超过则 score 趋近 ±1）
  min_score — 最小信号阈值（默认 0.15，低于此值不发信号）
"""
from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING

from core.domain.signal import Signal

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import (BookEvent, FillEvent,
                                    FundingRateEvent, TradeEvent)

log = logging.getLogger(__name__)


class MomentumStrategy:
    """
    纯动量策略。满足 Strategy 协议，无需继承任何基类。

    示例（config.yaml）：
      strategies:
        - module: "strategies.momentum.MomentumStrategy"
          params: {window: 20, scale: 0.05, min_score: 0.15}
    """

    name    = "momentum"
    version = "1.0.0"

    def __init__(
        self,
        window:    int   = 20,
        scale:     float = 0.05,
        min_score: float = 0.15,
    ) -> None:
        self._window    = window
        self._scale     = scale
        self._min_score = min_score
        # deque 自动限制窗口长度（满后自动丢弃最旧值）
        self._prices: dict[str, deque[Decimal]] = {}

    # ── Strategy 协议实现 ──────────────────────────────────────────────────────

    def on_trade(self, event: "TradeEvent",
                 ctx: "StrategyContext") -> list[Signal]:
        sym = event.instrument.symbol

        # 1. 追加价格到窗口
        if sym not in self._prices:
            self._prices[sym] = deque(maxlen=self._window)
        buf = self._prices[sym]
        buf.append(event.price)

        # 2. 窗口未满，等待预热
        if len(buf) < self._window:
            return []

        # 3. 计算动量分数
        prices = list(buf)
        score, confidence = self._calc_score(prices)

        # 4. 信号过弱，不发出
        if abs(score) < self._min_score:
            return []

        return [Signal(
            instrument  = event.instrument,
            score       = score,
            confidence  = confidence,
            strategy_id = self.name,
            meta        = {
                "ret":   float(prices[-1] / prices[0] - 1),
                "window": self._window,
            },
        )]

    def on_book(self, event: "BookEvent",
                ctx: "StrategyContext") -> list[Signal]:
        return []   # 动量策略不使用盘口

    def on_funding(self, event: "FundingRateEvent",
                   ctx: "StrategyContext") -> list[Signal]:
        return []   # 动量策略不使用资金费率

    def on_fill(self, event: "FillEvent",
                ctx: "StrategyContext") -> None:
        pass        # 动量策略状态不依赖成交回报

    def on_start(self, ctx: "StrategyContext") -> None:
        log.info("%s 启动  window=%d  scale=%.2f%%  min_score=%.2f",
                 self.name, self._window, self._scale * 100, self._min_score)

    def on_stop(self, ctx: "StrategyContext") -> None:
        log.info("%s 停止", self.name)

    # ── 内部计算 ───────────────────────────────────────────────────────────────

    def _calc_score(self, prices: list[Decimal]) -> tuple[float, float]:
        """
        计算 (score, confidence)。

        score      ∈ [-1, 1]，基于窗口首尾收益率归一化
        confidence ∈ [0, 1]，信号越强置信度越高
        """
        ret = float(prices[-1] / prices[0] - 1)

        # 归一化：scale 对应 score = 1.0
        score      = max(-1.0, min(1.0, ret / self._scale))
        # 置信度：|ret| 达到 scale*2 时置信度为 1.0
        confidence = min(1.0, abs(ret) / (self._scale * 2))

        return score, confidence
