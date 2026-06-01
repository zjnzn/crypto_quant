"""
application/services/portfolio.py  (Phase 4 更新)

多策略信号合并 + 目标仓位计算。

Phase 3：同一标的只保留最新信号（单策略）。
Phase 4：按 (symbol, strategy_id) 存储所有策略的最新信号，
         通过置信度加权平均得到合并分数，再计算目标仓位。

合并公式：
  combined_score = Σ(score_i × conf_i) / Σ(conf_i)

示例（动量+均值回归同时运行）：
  momentum      score=+0.6  conf=0.8  → weighted=+0.48
  mean_reversion score=-0.3  conf=0.5  → weighted=-0.15
  combined = (0.48 - 0.15) / (0.8 + 0.5) = +0.254  → 小多头
"""
from __future__ import annotations

import logging
from decimal import Decimal

from core.domain.order import Side
from core.domain.position import PositionSide
from core.ports.account import AccountPort
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.events import SignalEvent, TargetPositionEvent

log = logging.getLogger(__name__)


class PortfolioService:
    def __init__(
        self,
        bus:            EventBusPort,
        cache:          CachePort,
        account:        AccountPort,
        account_id:     str,
        max_weight:     float = 0.10,
        allow_short:    bool  = True,
        oms:           object | None = None,  # OMSService（延迟注入）
    ) -> None:
        self._bus        = bus
        self._cache      = cache
        self._account    = account
        self._account_id = account_id
        self._max_weight     = Decimal(str(max_weight))
        self._allow_short    = allow_short
        self._oms            = oms

        # (symbol, strategy_id) → SignalEvent
        self._signals: dict[tuple[str, str], SignalEvent] = {}

        bus.subscribe(SignalEvent, self._on_signal)
        log.info("PortfolioService 启动  max_weight=%.0f%%  short=%s",
                 max_weight * 100, allow_short)

    def set_oms(self, oms: object) -> None:
        """延迟注入 OMS（container 中解决循环依赖）。"""
        self._oms = oms

    def _on_signal(self, event: SignalEvent) -> None:
        sym = event.instrument.symbol
        key = (sym, event.strategy_id)

        # 更新该策略对该标的的最新信号
        self._signals[key] = event

        # 价格检查
        price: Decimal | None = self._cache.get(f"price:{sym}")
        if price is None or price <= 0:
            return

        # ── 多策略信号合并 ────────────────────────────────────────────────────
        sym_signals = [s for (s_sym, _), s in self._signals.items()
                       if s_sym == sym]

        total_conf = sum(s.confidence for s in sym_signals)
        if total_conf <= 0:
            return

        combined_score = (sum(s.score * s.confidence for s in sym_signals)
                          / total_conf)

        # 主导策略（置信度最高，用于日志和归因缓存）
        dominant = max(sym_signals, key=lambda s: s.confidence)
        self._cache.set(
            f"attribution:{event.correlation_id}:strategy",
            dominant.strategy_id,
        )

        # ── 目标仓位计算 ──────────────────────────────────────────────────────
        nav = self._account.get_nav_usdt(self._account_id)
        if nav <= 0:
            return

        abs_score = Decimal(str(min(abs(combined_score), 1.0)))

        if abs_score == Decimal(0):
            return  # 信号为零，不调整仓位
        else:
            target_size = (abs_score
                           * self._max_weight * nav / price)
            target_size = event.instrument.round_qty(target_size)
            target_side = Side.BUY if combined_score > 0 else Side.SELL

            if not self._allow_short and target_side == Side.SELL:
                target_size = Decimal(0)
                target_side = Side.BUY

        # ── delta 检查（净仓模式）────────────────────────────────────────────────
        cur_pos = self._account.get_position(self._account_id, sym)
        current_size = cur_pos.size if cur_pos else Decimal(0)
        current_side = cur_pos.side if cur_pos else None

        # ★ 计入在途订单（已提交但未成交的增量），防止重复下单
        pending_delta = Decimal(0)
        if self._oms is not None:
            pending_delta = self._oms.compute_pending_delta(sym)

        # 当前净仓位：多头为正，空头为负，空仓为 0
        if current_side is None:
            net_position = pending_delta  # 只有在途订单
        elif current_side == PositionSide.LONG:
            net_position = current_size + pending_delta
        else:  # SHORT
            net_position = -(current_size + pending_delta)

        # 目标净仓位：多头为正，空头为负，0 为空仓
        target_position = target_size if target_side == Side.BUY else -target_size

        # 检查是否需要调整
        delta = abs(target_position - net_position)
        if delta < event.instrument.lot_size:
            return

        self._bus.publish(
            TargetPositionEvent(
                account_id     = self._account_id,
                instrument     = event.instrument,
                target_size    = target_size,
                target_side    = target_side,
                current_size   = current_size,
                current_side   = current_side,
                net_position   = net_position,      # 当前净仓位（带方向）
                target_position= target_position,   # 目标净仓位（带方向）
            ).caused_by(event)
        )

        n_strategies = len(sym_signals)
        log.debug("target  %s  size=%.6f  side=%s  combined_score=%.3f  "
                  "n_strategies=%d",
                  sym, target_size, target_side.value, combined_score,
                  n_strategies)

    @property
    def active_strategies(self) -> set[str]:
        """当前有活跃信号的策略集合（用于监控/调试）。"""
        return {strategy_id for (_, strategy_id) in self._signals}
