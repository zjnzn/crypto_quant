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
        max_weight:     float = 0.10,    # 单标的保证金占用上限
        min_score:      float = 0.15,
        allow_short:    bool  = True,
        order_cooldown: float = 0.0,     # 两次同标的下单最小间隔（秒）
        leverage:       int   = 1,       # 目标杠杆倍数
        close_threshold: float = 0.10,   # 平仓信号阈值(信号绝对值<此值时平仓)
    ) -> None:
        self._bus        = bus
        self._cache      = cache
        self._account    = account
        self._account_id = account_id
        self._max_weight     = Decimal(str(max_weight))
        self._min_score      = min_score
        self._allow_short    = allow_short
        self._order_cooldown = order_cooldown
        self._leverage       = leverage
        self._close_threshold = close_threshold

        # (symbol, strategy_id) → SignalEvent
        self._signals: dict[tuple[str, str], SignalEvent] = {}
        # 每个标的最后一次下单的单调时钟时间（防止重复下单）
        self._last_order_ts: dict[str, float] = {}

        bus.subscribe(SignalEvent, self._on_signal)
        log.info("PortfolioService 启动  max_weight=%.0f%%  min_score=%.2f  "
                 "close_threshold=%.2f  short=%s  leverage=%dx",
                 max_weight * 100, min_score, close_threshold, allow_short, leverage)

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

        # 主导策略(置信度最高,用于日志和归因缓存)
        dominant = max(sym_signals, key=lambda s: s.confidence)
        self._cache.set(
            f"attribution:{event.correlation_id}:strategy",
            dominant.strategy_id,
        )

        # ── 预期收益信息传递(手续费保护)────────────────────────────────────────
        # 从主导信号的 meta 中提取预期收益率,写入 cache
        # RiskService 将在构建 RiskContext 时读取并传递给风控中间件
        expected_return_pct = dominant.meta.get("ret")
        if expected_return_pct is not None:
            self._cache.set(
                f"expected_return:{sym}",
                Decimal(str(expected_return_pct)),
            )

        # ── 信号分数传递(翻仓保护)──────────────────────────────────────────────
        # 将组合后的信号分数写入 cache,用于翻仓时检查信号强度
        self._cache.set(
            f"signal_score:{sym}",
            Decimal(str(combined_score)),
        )

        # ── 目标仓位计算 ──────────────────────────────────────────────────────
        # 状态机逻辑：
        # - 无持仓：信号 < min_score → 不开仓，信号 ≥ min_score → 正常计算
        # - 有持仓：信号在 [0, min_score] → 保持最低持仓（按 min_score 算）
        # 用 equity（余额+未实现盈亏）而非 NAV（余额+持仓市值）
        # 避免仓位随持仓增大而失控
        equity = self._account.get_equity_usdt(self._account_id)
        if equity <= 0:
            return

        abs_score = abs(combined_score)

        # 判断当前是否有持仓
        cur_pos = self._account.get_position(self._account_id, sym)
        if cur_pos and not cur_pos.is_empty:
            current_size = cur_pos.size if cur_pos.side == PositionSide.LONG else -cur_pos.size
            has_position = True
        else:
            current_size = Decimal(0)
            has_position = False

        # 计算目标仓位
        # 优先检查平仓阈值：信号低于阈值 → 完全平仓
        if abs_score < self._close_threshold:
            if has_position:
                # 信号低于平仓阈值 → 完全平仓
                target_size = Decimal(0)
                delta = target_size - current_size
                if abs(delta) < event.instrument.lot_size:
                    return

                self._bus.publish(
                    TargetPositionEvent(
                        account_id   = self._account_id,
                        instrument   = event.instrument,
                        target_size  = target_size,
                        current_size = current_size,
                        leverage     = self._leverage,
                    ).caused_by(event)
                )
                return
            else:
                # 无持仓且信号弱 → 不开仓
                target_size = Decimal(0)
                delta = target_size - current_size
                if abs(delta) < event.instrument.lot_size:
                    return

                self._bus.publish(
                    TargetPositionEvent(
                        account_id   = self._account_id,
                        instrument   = event.instrument,
                        target_size  = target_size,
                        current_size = current_size,
                        leverage     = self._leverage,
                    ).caused_by(event)
                )
                return

        # 正常开仓逻辑：检查 min_score
        if abs_score < self._min_score:
            if has_position:
                # 有持仓但信号弱 → 保持最低持仓（按 min_score 算）
                abs_score = Decimal(str(self._min_score))
            else:
                # 无持仓且信号弱 → 不开仓
                target_size = Decimal(0)
                delta = target_size - current_size
                if abs(delta) < event.instrument.lot_size:
                    return

                self._bus.publish(
                    TargetPositionEvent(
                        account_id   = self._account_id,
                        instrument   = event.instrument,
                        target_size  = target_size,
                        current_size = current_size,
                        leverage     = self._leverage,  # 传递杠杆倍数
                    ).caused_by(event)
                )
                return

        # 正常计算仓位：score × max_weight × leverage × NAV / price
        raw_size = (Decimal(str(abs_score))
                    * self._max_weight
                    * self._leverage
                    * equity / price)
        raw_size = event.instrument.round_qty(raw_size)

        # 带符号：score > 0 → 正（多头），score < 0 → 负（空头），score = 0 保持原方向
        if combined_score > 0:
            target_size = raw_size
        elif combined_score < 0:
            target_size = -raw_size
        else:
            # score = 0 时，保持原有仓位方向
            target_size = raw_size if current_size >= 0 else -raw_size

        if not self._allow_short and target_size < 0:
            target_size = Decimal(0)

        delta = target_size - current_size
        if abs(delta) < event.instrument.lot_size:
            return

        # ── 下单冷却（防止填单前重复提交）────────────────────────────────────
        import time as _time
        if self._order_cooldown > 0:
            last_ts = self._last_order_ts.get(sym, 0.0)
            if _time.monotonic() - last_ts < self._order_cooldown:
                log.debug("portfolio cooldown: %s 跳过（距上次下单 < %.0fs）",
                          sym, self._order_cooldown)
                return
            self._last_order_ts[sym] = _time.monotonic()

        self._bus.publish(
            TargetPositionEvent(
                account_id   = self._account_id,
                instrument   = event.instrument,
                target_size  = target_size,
                current_size = current_size,
                leverage     = self._leverage,  # 传递杠杆倍数
            ).caused_by(event)
        )

        n_strategies = len(sym_signals)
        direction = "LONG" if target_size > 0 else ("SHORT" if target_size < 0 else "FLAT")
        log.debug("target  %s  size=%.6f (%s)  current=%.6f  delta=%.6f  "
                  "combined_score=%.3f  n_strategies=%d",
                  sym, abs(target_size), direction, current_size, delta,
                  combined_score, n_strategies)

    @property
    def active_strategies(self) -> set[str]:
        """当前有活跃信号的策略集合（用于监控/调试）。"""
        return {strategy_id for (_, strategy_id) in self._signals}
