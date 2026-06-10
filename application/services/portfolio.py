"""
Portfolio service.

Merges strategy signals per symbol and converts the combined score into a
target signed position size.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

from core.domain.position import PositionSide
from core.ports.account import AccountPort
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.events import SignalEvent, TargetPositionEvent

log = logging.getLogger(__name__)


class PortfolioService:
    def __init__(
        self,
        bus: EventBusPort,
        cache: CachePort,
        account: AccountPort,
        account_id: str,
        max_weight: float = 0.10,
        min_score: float = 0.15,
        allow_short: bool = True,
        order_cooldown: float = 0.0,
        leverage: int = 1,
        close_threshold: float = 0.10,
    ) -> None:
        self._bus = bus
        self._cache = cache
        self._account = account
        self._account_id = account_id
        self._max_weight = Decimal(str(max_weight))
        self._min_score = min_score
        self._allow_short = allow_short
        self._order_cooldown = order_cooldown
        self._leverage = leverage
        self._close_threshold = close_threshold

        self._signals: dict[tuple[str, str], SignalEvent] = {}
        self._last_order_ts: dict[str, float] = {}

        bus.subscribe(SignalEvent, self._on_signal)
        log.info(
            "PortfolioService started max_weight=%.0f%% min_score=%.2f "
            "close_threshold=%.2f short=%s leverage=%dx",
            max_weight * 100,
            min_score,
            close_threshold,
            allow_short,
            leverage,
        )

    def _on_signal(self, event: SignalEvent) -> None:
        sym = event.instrument.symbol
        self._signals[(sym, event.strategy_id)] = event

        price: Decimal | None = self._cache.get(f"price:{sym}")
        if price is None or price <= 0:
            return

        sym_signals = [s for (s_sym, _), s in self._signals.items() if s_sym == sym]
        total_conf = sum(s.confidence for s in sym_signals)
        if total_conf <= 0:
            return

        combined_score = sum(s.score * s.confidence for s in sym_signals) / total_conf
        dominant = max(sym_signals, key=lambda s: s.confidence)

        self._cache.set(f"attribution:{event.correlation_id}:strategy", dominant.strategy_id)

        expected_return_pct = dominant.meta.get("ret")
        if expected_return_pct is not None:
            self._cache.set(f"expected_return:{sym}", Decimal(str(expected_return_pct)))

        self._cache.set(f"signal_score:{sym}", Decimal(str(combined_score)))

        equity = self._account.get_equity_usdt(self._account_id)
        if equity <= 0:
            return

        cur_pos = self._account.get_position(self._account_id, sym)
        if cur_pos and not cur_pos.is_empty:
            current_size = (
                cur_pos.size if cur_pos.side == PositionSide.LONG else -cur_pos.size
            )
            has_position = True
        else:
            current_size = Decimal(0)
            has_position = False

        abs_score = abs(combined_score)

        if not has_position and abs_score < self._min_score:
            return

        if has_position and abs_score < self._min_score:
            # Keep a minimum position in the current signal direction. If the
            # signal is exactly flat, preserve the existing direction.
            abs_score = Decimal(str(self._min_score))

        raw_size = (
            Decimal(str(abs_score))
            * self._max_weight
            * self._leverage
            * equity
            / price
        )
        raw_size = event.instrument.round_qty(raw_size)

        if combined_score > 0:
            target_size = raw_size
        elif combined_score < 0:
            target_size = -raw_size
        else:
            target_size = raw_size if current_size >= 0 else -raw_size

        if not self._allow_short and target_size < 0:
            target_size = Decimal(0)

        delta = target_size - current_size
        if abs(delta) < event.instrument.lot_size:
            return

        if self._order_cooldown > 0:
            last_ts = self._last_order_ts.get(sym, 0.0)
            if time.monotonic() - last_ts < self._order_cooldown:
                log.debug(
                    "portfolio cooldown: %s skipped within %.0fs",
                    sym,
                    self._order_cooldown,
                )
                return
            self._last_order_ts[sym] = time.monotonic()

        self._bus.publish(
            TargetPositionEvent(
                account_id=self._account_id,
                instrument=event.instrument,
                target_size=target_size,
                current_size=current_size,
                leverage=self._leverage,
            ).caused_by(event)
        )

        direction = "LONG" if target_size > 0 else ("SHORT" if target_size < 0 else "FLAT")
        log.debug(
            "target %s size=%.6f (%s) current=%.6f delta=%.6f "
            "combined_score=%.3f n_strategies=%d",
            sym,
            abs(target_size),
            direction,
            current_size,
            delta,
            combined_score,
            len(sym_signals),
        )

    @property
    def active_strategies(self) -> set[str]:
        return {strategy_id for _, strategy_id in self._signals}
