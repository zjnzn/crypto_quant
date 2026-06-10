"""
Signal service.

It keeps the latest market data in cache, dispatches market events to loaded
strategies, and publishes strategy outputs as SignalEvent.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from decimal import Decimal
from typing import Callable

from core.domain.signal import Signal
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.context import StrategyContext
from application.events import BookEvent, FundingRateEvent, SignalEvent, TradeEvent
from application.strategy.base import Strategy

log = logging.getLogger(__name__)

_WINDOW_MAX = 500
_PRICE_TTL = timedelta(hours=1)


class SignalService:
    def __init__(
        self,
        bus: EventBusPort,
        cache: CachePort,
        strategies: list[Strategy],
        make_ctx: Callable[[str], StrategyContext],
        signal_throttle_sec: float = 0.0,
    ) -> None:
        self._bus = bus
        self._cache = cache
        self._strategies = strategies
        self._make_ctx = make_ctx
        self._signal_throttle_sec = signal_throttle_sec
        self._last_signal_time: dict[str, float] = {}

        bus.subscribe(TradeEvent, self._on_trade)
        bus.subscribe(BookEvent, self._on_book)
        bus.subscribe(FundingRateEvent, self._on_funding)

        log.info("SignalService started, strategies=%s", [s.name for s in strategies])

    def _on_trade(self, event: TradeEvent) -> None:
        sym = event.instrument.symbol

        self._cache.set(f"price:{sym}", event.price, ttl=_PRICE_TTL)

        window: list[Decimal] = self._cache.get(f"prices_window:{sym}") or []
        window.append(event.price)
        if len(window) > _WINDOW_MAX:
            window = window[-_WINDOW_MAX:]
        self._cache.set(f"prices_window:{sym}", window)

        if self._signal_throttle_sec > 0:
            now = time.time()
            last_time = self._last_signal_time.get(sym, 0.0)
            if now - last_time < self._signal_throttle_sec:
                return
            self._last_signal_time[sym] = now

        self._dispatch(event, "on_trade")

    def _on_book(self, event: BookEvent) -> None:
        sym = event.instrument.symbol
        self._cache.set(f"bid:{sym}", event.bid_price, ttl=_PRICE_TTL)
        self._cache.set(f"ask:{sym}", event.ask_price, ttl=_PRICE_TTL)
        self._dispatch(event, "on_book")

    def _on_funding(self, event: FundingRateEvent) -> None:
        sym = event.instrument.symbol
        self._cache.set(f"funding:{sym}", event.rate, ttl=timedelta(hours=8))
        self._dispatch(event, "on_funding")

    def _dispatch(self, event, method: str) -> None:
        for strategy in self._strategies:
            ctx = self._make_ctx(strategy.name)
            try:
                signals: list[Signal] = getattr(strategy, method)(event, ctx)
            except Exception:
                log.exception("strategy %s.%s failed", strategy.name, method)
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
            ).caused_by(parent_event)
        )
        log.debug(
            "signal %s score=%.3f strategy=%s",
            sig.instrument.symbol,
            sig.score,
            sig.strategy_id,
        )
