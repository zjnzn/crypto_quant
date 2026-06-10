"""
strategies/mean_reversion.py

均值回归策略（改进版：收益率均值回归 + 趋势过滤）。

核心改进：
  1. 使用对数收益率而非价格（解决非平稳序列问题）
  2. 可选的趋势过滤（EMA50/200，只做顺势回归）
  3. 支持 Bollinger Band 模式

信号逻辑：
  1. 计算 log returns = log(price_t / price_{t-1})
  2. 计算 returns 的滚动均值和标准差
  3. Z-score = (current_return - mean_return) / std_return
  4. |Z| < z_entry  → 无信号
  5. Z > z_entry    → 收益率偏高（价格涨幅过大），做空信号
  6. Z < -z_entry   → 收益率偏低（价格跌幅过大），做多信号

趋势过滤：
  - EMA50 > EMA200（牛市）→ 只做多均值回归（只抄底）
  - EMA50 < EMA200（熊市）→ 只做空均值回归（只摸顶）

BTC实盘推荐参数：
  5分钟级别：
    window: 48
    z_entry: 2.2
    z_threshold: 4.0
    trend_filter: true
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
    均值回归策略（改进版）。

    示例（config.yaml）：
      strategies:
        - module: "strategies.mean_reversion.MeanReversionStrategy"
          params:
            window: 48
            z_entry: 2.2
            z_threshold: 4.0
            trend_filter: true
            ema_fast: 50
            ema_slow: 200
    """

    name    = "mean_reversion"
    version = "2.0.0"

    def __init__(
        self,
        window:        int   = 48,
        z_entry:       float = 2.2,
        z_threshold:   float = 4.0,
        trend_filter:  bool  = False,  # 是否启用趋势过滤
        ema_fast:      int   = 50,     # 快速EMA周期
        ema_slow:      int   = 200,    # 慢速EMA周期
        use_returns:   bool  = False,  # 使用收益率而非价格（实盘可显式开启）
    ) -> None:
        self._window        = window
        self._z_entry       = z_entry
        self._z_threshold   = z_threshold
        self._trend_filter  = trend_filter
        self._ema_fast      = ema_fast
        self._ema_slow      = ema_slow
        self._use_returns   = use_returns

        # 价格/收益率缓存
        self._prices:  dict[str, deque[Decimal]] = {}
        self._returns: dict[str, deque[Decimal]] = {}

        # EMA缓存（用于趋势过滤）
        self._ema_fast_vals: dict[str, Decimal] = {}
        self._ema_slow_vals: dict[str, Decimal] = {}

    # ── Strategy 协议 ──────────────────────────────────────────────────────────

    def on_trade(self, event: "TradeEvent",
                 ctx: "StrategyContext") -> list[Signal]:
        sym = event.instrument.symbol

        # 初始化缓存
        if sym not in self._prices:
            self._prices[sym] = deque(maxlen=max(self._window, self._ema_slow) + 1)
        if self._use_returns and sym not in self._returns:
            self._returns[sym] = deque(maxlen=self._window)

        prices = self._prices[sym]
        prices.append(event.price)

        # 计算对数收益率
        if self._use_returns and len(prices) >= 2:
            prev_price = prices[-2]
            curr_price = prices[-1]
            if prev_price > 0:
                ret = Decimal(str(math.log(float(curr_price / prev_price))))
                self._returns[sym].append(ret)

        # 预热期检查
        data_source = self._returns[sym] if self._use_returns else prices
        if len(data_source) < self._window:
            return []

        # 趋势过滤：更新EMA
        trend_bias = None  # None=无过滤, 1=牛市(只做多), -1=熊市(只做空)
        if self._trend_filter and len(prices) >= self._ema_slow:
            trend_bias = self._update_trend(sym, prices)

        # 计算均值回归信号
        score, confidence, z = self._calc(list(data_source), list(data_source)[-1])
        if score is None:
            return []

        # 应用趋势过滤
        if trend_bias is not None:
            # 牛市只做多（score>0），熊市只做空（score<0）
            if trend_bias == 1 and score < 0:
                log.debug("%s 牛市过滤：忽略做空信号 score=%.3f", sym, score)
                return []
            if trend_bias == -1 and score > 0:
                log.debug("%s 熊市过滤：忽略做多信号 score=%.3f", sym, score)
                return []

        return [Signal(
            instrument  = event.instrument,
            score       = score,
            confidence  = confidence,
            strategy_id = self.name,
            meta        = {
                "z_score": round(z, 3),
                "window":  self._window,
                "trend": "bull" if trend_bias == 1 else ("bear" if trend_bias == -1 else "none"),
                "mode": "returns" if self._use_returns else "price",
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
        log.info("%s v%s 启动  window=%d  z_entry=%.1f  z_threshold=%.1f  "
                 "trend_filter=%s  use_returns=%s",
                 self.name, self.version, self._window, self._z_entry, self._z_threshold,
                 self._trend_filter, self._use_returns)

    def on_stop(self, ctx: "StrategyContext") -> None:
        log.info("%s 停止", self.name)

    # ── 内部计算 ───────────────────────────────────────────────────────────────

    def _calc(self, data: list[Decimal],
              current: Decimal) -> tuple[float | None, float, float]:
        """
        计算均值回归信号。

        返回 (score, confidence, z_score)。
        当信号不足时返回 (None, 0, 0)。
        """
        floats = [float(d) for d in data]
        mean   = sum(floats) / len(floats)
        var    = sum((r - mean) ** 2 for r in floats) / len(floats)
        std    = math.sqrt(var)

        if std < 1e-10:              # 数据完全没有波动，忽略
            return None, 0.0, 0.0

        z = (float(current) - mean) / std

        if abs(z) < self._z_entry:  # 未达触发阈值
            return None, 0.0, z

        # score = -Z（收益率偏高 → 价格涨幅过大 → 做空）
        score      = max(-1.0, min(1.0, -z / self._z_threshold))
        confidence = min(1.0, abs(z) / (self._z_threshold * 1.5))

        return score, confidence, z

    def _update_trend(self, sym: str, prices: deque[Decimal]) -> int | None:
        """
        更新EMA并判断趋势。

        返回：
          1  = 牛市（EMA_fast > EMA_slow）
          -1 = 熊市（EMA_fast < EMA_slow）
          None = 未初始化
        """
        price_list = list(prices)
        if len(price_list) < self._ema_slow:
            return None

        # 初始化EMA
        if sym not in self._ema_fast_vals:
            # 用SMA初始化
            self._ema_fast_vals[sym] = Decimal(str(
                sum(float(p) for p in price_list[-self._ema_fast:]) / self._ema_fast
            ))
            self._ema_slow_vals[sym] = Decimal(str(
                sum(float(p) for p in price_list[-self._ema_slow:]) / self._ema_slow
            ))

        # 更新EMA
        alpha_fast = 2.0 / (self._ema_fast + 1)
        alpha_slow = 2.0 / (self._ema_slow + 1)

        curr_price = float(price_list[-1])
        prev_ema_fast = float(self._ema_fast_vals[sym])
        prev_ema_slow = float(self._ema_slow_vals[sym])

        self._ema_fast_vals[sym] = Decimal(str(
            alpha_fast * curr_price + (1 - alpha_fast) * prev_ema_fast
        ))
        self._ema_slow_vals[sym] = Decimal(str(
            alpha_slow * curr_price + (1 - alpha_slow) * prev_ema_slow
        ))

        # 判断趋势
        if self._ema_fast_vals[sym] > self._ema_slow_vals[sym]:
            return 1   # 牛市
        else:
            return -1  # 熊市

