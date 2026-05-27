"""
strategies/funding_rate_arb.py

资金费率套利策略（Funding Rate Arbitrage）。

核心逻辑：
  永续合约资金费率 > 阈值时，做空永续（收取资金费），同时做多现货（对冲价格风险）。
  本系统只交易永续，故简化为：
    - 高资金费率 → 做空信号（空仓捕获资金费支付）
    - 低/负资金费率 → 做多信号（反向持仓获益）
    - 中性费率 → 无信号

资金费率通常每 8 小时结算一次（00:00 / 08:00 / 16:00 UTC）。
年化收益 = rate_per_period × periods_per_year = rate × 3 × 365。

参数：
  min_rate     — 触发信号的最小资金费率，默认 0.1%/8h
  max_rate     — score=-1 对应的费率（完全做空），默认 0.5%/8h
  cooldown_bars — 每次信号后的冷却 K 线数（防止连续下单），默认 8
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from core.domain.signal import Signal

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import (BookEvent, FillEvent,
                                    FundingRateEvent, TradeEvent)

log = logging.getLogger(__name__)


class FundingRateArbStrategy:
    """
    资金费率套利。满足 Strategy 协议，无需继承。

    config.yaml 示例：
      strategies:
        - module: "strategies.funding_rate_arb.FundingRateArbStrategy"
          params: {min_rate: 0.001, max_rate: 0.005, cooldown_bars: 8}
    """

    name    = "funding_rate_arb"
    version = "1.0.0"

    def __init__(
        self,
        min_rate:      float = 0.001,   # 0.1%/8h = 约 10.95% 年化
        max_rate:      float = 0.005,   # 0.5%/8h = 约 54.75% 年化
        cooldown_bars: int   = 8,
    ) -> None:
        self._min_rate      = min_rate
        self._max_rate      = max_rate
        self._cooldown      = cooldown_bars
        self._bar_count:  dict[str, int]     = {}   # symbol → K 线计数
        self._last_signal:dict[str, float]   = {}   # symbol → last score

    # ── Strategy 协议 ──────────────────────────────────────────────────────────

    def on_funding(self, event: "FundingRateEvent",
                   ctx: "StrategyContext") -> list[Signal]:
        """
        资金费率更新时触发。每 8 小时结算，信号寿命最多 8 根 K 线。
        """
        sym  = event.instrument.symbol
        rate = float(event.rate)

        # 重置冷却计数（新的资金费率到了）
        self._bar_count[sym] = 0

        if abs(rate) < self._min_rate:
            self._last_signal[sym] = 0.0
            return []   # 费率太低，不值得交易成本

        # 费率高 → 做空（score < 0）：rate=min → score=0，rate=max → score=-1
        if rate > 0:
            raw_score = -(rate - self._min_rate) / (self._max_rate - self._min_rate)
        else:
            # 负费率 → 做多（price < cost of carry）
            raw_score = (-rate - self._min_rate) / (self._max_rate - self._min_rate)

        score      = max(-1.0, min(1.0, raw_score))
        confidence = min(1.0, abs(rate) / (self._max_rate * 1.5))
        self._last_signal[sym] = score

        ann_rate = rate * 3 * 365 * 100   # 年化百分比（3次/天，365天）
        log.debug("funding arb  %s  rate=%.4f%%  ann=%.1f%%  score=%.3f",
                  sym, rate * 100, ann_rate, score)

        return [Signal(
            instrument  = event.instrument,
            score       = score,
            confidence  = confidence,
            strategy_id = self.name,
            meta        = {
                "rate":     round(rate, 6),
                "ann_rate": round(ann_rate, 2),
            },
        )]

    def on_trade(self, event: "TradeEvent",
                 ctx: "StrategyContext") -> list[Signal]:
        """
        K 线更新时：如果在冷却期内且上次有信号，持续发送信号（维持仓位）。
        """
        sym = event.instrument.symbol
        cnt = self._bar_count.get(sym, self._cooldown)
        if cnt >= self._cooldown:
            return []

        last_score = self._last_signal.get(sym, 0.0)
        if abs(last_score) < 0.01:
            self._bar_count[sym] = self._cooldown
            return []

        self._bar_count[sym] = cnt + 1

        # 信号强度随时间衰减（线性衰减到 0）
        decay     = 1.0 - (cnt / self._cooldown)
        score     = last_score * decay
        if abs(score) < 0.05:
            return []

        return [Signal(
            instrument  = event.instrument,
            score       = score,
            confidence  = decay * 0.5,   # 衰减信号置信度低
            strategy_id = self.name,
            meta        = {"source": "decay", "bar": cnt},
        )]

    def on_book(self, event: "BookEvent",
                ctx: "StrategyContext") -> list[Signal]:
        return []

    def on_fill(self, event: "FillEvent",
                ctx: "StrategyContext") -> None:
        pass

    def on_start(self, ctx: "StrategyContext") -> None:
        log.info("%s 启动  min_rate=%.4f%%  max_rate=%.4f%%  cooldown=%d",
                 self.name, self._min_rate * 100, self._max_rate * 100,
                 self._cooldown)

    def on_stop(self, ctx: "StrategyContext") -> None:
        log.info("%s 停止", self.name)
