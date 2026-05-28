"""
application/services/signal.py

信号服务。

订阅：TradeEvent / BookEvent / FundingRateEvent
发布：SignalEvent（携带完整因果链）

职责：
  1. 维护价格缓存（供策略读取 last_price / prices_window）
  2. 将行情事件分发给所有已注册策略
  3. 将策略返回的 Signal 转换为 SignalEvent 并发布到总线
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta
from decimal import Decimal
from typing import Callable

from core.domain.signal import Signal
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.context import StrategyContext
from application.events import (BookEvent, FillEvent, FundingRateEvent,
                                 SignalEvent, TradeEvent)
from application.strategy.base import Strategy

log = logging.getLogger(__name__)

# 价格窗口最大长度（足够大多数技术指标）
_WINDOW_MAX = 500
# 价格缓存 TTL（实盘防止过期价格被使用）
_PRICE_TTL = timedelta(hours=1)


class SignalService:
    """
    将行情事件路由到各策略，聚合信号并发布到总线。

    make_ctx: 工厂函数，签名为 (strategy_id: str) -> StrategyContext
              由 container.py 注入，保证 Context 总是最新状态。
    """

    def __init__(
        self,
        bus:        EventBusPort,
        cache:      CachePort,
        strategies: list[Strategy],
        make_ctx:   Callable[[str], StrategyContext],
    ) -> None:
        self._bus        = bus
        self._cache      = cache
        self._strategies = strategies
        self._make_ctx   = make_ctx

        bus.subscribe(TradeEvent,       self._on_trade)
        bus.subscribe(BookEvent,        self._on_book)
        bus.subscribe(FundingRateEvent, self._on_funding)

        log.info("SignalService 启动，加载策略: %s",
                 [s.name for s in strategies])

    # ── 事件处理 ──────────────────────────────────────────────────────────────

    def _on_trade(self, event: TradeEvent) -> None:
        sym = event.instrument.symbol

        # 1. 更新最新价格缓存（供 ctx.last_price() 读取）
        self._cache.set(f"price:{sym}", event.price, ttl=_PRICE_TTL)

        # 2. 追加价格窗口（供 ctx.prices_window() 读取）
        window: list[Decimal] = self._cache.get(f"prices_window:{sym}") or []
        window.append(event.price)
        if len(window) > _WINDOW_MAX:
            window = window[-_WINDOW_MAX:]
        self._cache.set(f"prices_window:{sym}", window)

        # 3. 分发给所有策略
        self._dispatch(event, "on_trade")

    def _on_book(self, event: BookEvent) -> None:
        sym = event.instrument.symbol
        mid = (event.bid_price + event.ask_price) / 2

        self._cache.set(f"bid:{sym}", event.bid_price, ttl=_PRICE_TTL)
        self._cache.set(f"ask:{sym}", event.ask_price, ttl=_PRICE_TTL)
        self._dispatch(event, "on_book")

        # aggTrade 流在 combined stream 中不可用，用 bookTicker mid price 模拟 TradeEvent
        trade = TradeEvent(
            ts=event.ts,
            instrument=event.instrument,
            price=mid,
            qty=Decimal("0"),
            buyer_maker=False,
        ).caused_by(event)
        self._on_trade(trade)

    def _on_funding(self, event: FundingRateEvent) -> None:
        sym = event.instrument.symbol
        self._cache.set(f"funding:{sym}", event.rate, ttl=timedelta(hours=8))
        self._dispatch(event, "on_funding")

    # ── 内部分发 ──────────────────────────────────────────────────────────────

    def _dispatch(self, event, method: str) -> None:
        for strategy in self._strategies:
            ctx = self._make_ctx(strategy.name)
            try:
                signals: list[Signal] = getattr(strategy, method)(event, ctx)
            except Exception:
                log.exception("策略 %s.%s 异常", strategy.name, method)
                continue

            for sig in signals:
                self._publish_signal(sig, event)

    def _publish_signal(self, sig: Signal, parent_event) -> None:
        self._bus.publish(
            SignalEvent(
                instrument=sig.instrument,
                score=sig.score,
                confidence=sig.confidence,
                strategy_id=sig.strategy_id or "",
                meta=sig.meta,
            ).caused_by(parent_event)   # ★ 因果链传播
        )
        log.debug("signal  %s  score=%.3f  strategy=%s",
                  sig.instrument.symbol, sig.score, sig.strategy_id)
