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

实盘手续费保护框架：
  1. 最小调仓阈值（min_rebalance）：仓位变化 < N% 不交易
  2. 双阈值（hysteresis）：开仓阈值 > 平仓阈值，防止信号边界来回打脸
  3. 交易成本过滤：预测收益 < 3×手续费 不交易
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
        min_score:      float = 0.15,    # 平仓阈值（有持仓时，低于此保持最低持仓）
        open_score:     float = 0.30,    # 开仓阈值（无持仓时，低于此不开仓）
        allow_short:    bool  = True,
        order_cooldown: float = 0.0,     # 两次同标的下单最小间隔（秒）
        leverage:       int   = 1,       # 目标杠杆倍数
        min_rebalance:  float = 0.05,    # 最小调仓比例：仓位变化 < 5% 不交易
        taker_fee:      float = 0.0004,  # 币安 USDT-M taker 手续费率 0.04%
    ) -> None:
        self._bus        = bus
        self._cache      = cache
        self._account    = account
        self._account_id = account_id
        self._max_weight     = Decimal(str(max_weight))
        self._min_score      = min_score   # 平仓/保持阈值
        self._open_score     = open_score  # 开仓阈值
        self._allow_short    = allow_short
        self._order_cooldown = order_cooldown
        self._leverage       = leverage
        self._min_rebalance  = Decimal(str(min_rebalance))
        self._taker_fee      = Decimal(str(taker_fee))

        # (symbol, strategy_id) → SignalEvent
        self._signals: dict[tuple[str, str], SignalEvent] = {}
        # 每个标的最后一次下单的单调时钟时间（防止重复下单）
        self._last_order_ts: dict[str, float] = {}

        bus.subscribe(SignalEvent, self._on_signal)
        log.info("PortfolioService 启动  max_weight=%.0f%%  open=%.2f  close=%.2f  "
                 "short=%s  leverage=%dx  min_rebalance=%.0f%%  fee=%.2f%%",
                 max_weight * 100, open_score, min_score,
                 allow_short, leverage, min_rebalance * 100, taker_fee * 100)

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

        # ── 双阈值（Hysteresis）────────────────────────────────────────────────
        # 开仓阈值 > 平仓阈值，防止信号在边界来回打脸
        # 无持仓：信号 ≥ open_score → 开仓
        # 有持仓：信号 < min_score → 保持最低持仓（不立即平仓）
        if has_position:
            threshold = self._min_score
        else:
            threshold = self._open_score

        # 计算目标仓位
        if abs_score < threshold:
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
                    ).caused_by(event)
                )
                return

        # 正常计算仓位：score × max_weight × leverage × equity / price
        raw_size = (Decimal(str(abs_score))
                    * self._max_weight
                    * Decimal(str(self._leverage))
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

        # ── 最小调仓阈值 ──────────────────────────────────────────────────────
        # 仓位变化 < min_rebalance% 不交易
        # 防止微小波动触发调仓，手续费吞噬收益
        if current_size != 0:
            rebalance_pct = abs(delta / current_size)
            if rebalance_pct < self._min_rebalance:
                log.debug("portfolio skip: %s 调仓比例 %.1f%% < %.0f%%",
                          sym, float(rebalance_pct) * 100,
                          float(self._min_rebalance) * 100)
                return

        # ── 交易成本过滤 ──────────────────────────────────────────────────────
        # 预测收益 < 3×手续费 不交易
        # 预测收益 = abs(delta) × price × 信号方向预期波动
        # 简化：名义价值变化 × 手续费 = 调仓成本
        # 如果调仓太小，手续费占比就太高
        if abs(delta) < event.instrument.lot_size:
            return

        notional_delta = abs(delta) * price
        trading_cost = notional_delta * self._taker_fee * Decimal("3")
        # 粗略预测收益：信号强度 × 名义价值变化
        # 如果调仓的名义价值本身就小于 3×手续费 × 总仓位名义价值，
        # 说明调仓意义不大
        total_notional = abs(current_size) * price if current_size != 0 else equity
        if notional_delta < trading_cost:
            log.debug("portfolio skip: %s 调仓金额 %.2f < 3×手续费 %.2f",
                      sym, float(notional_delta), float(trading_cost))
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