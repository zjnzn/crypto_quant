"""
application/services/risk.py

风控服务。

订阅：TargetPositionEvent
发布：RiskApprovedEvent | RiskRejectedEvent

职责：
  1. 从 TargetPositionEvent 构建待检查的 Order
  2. 从 cache + account 组装 RiskContext
  3. 调用 RiskPipeline.check()
  4. 发布审核结果事件
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

from core.domain.order import Order, OrderStatus, OrderType, Side
from core.ports.account import AccountPort
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from core.ports.risk import RiskContext
from application.events import (RiskApprovedEvent, RiskRejectedEvent,
                                 TargetPositionEvent)
from application.risk.pipeline import RiskPipeline

log = logging.getLogger(__name__)


class RiskService:
    """
    将目标仓位提交风控管道审核。

    通过审核 → RiskApprovedEvent（携带最终 Order）
    未通过  → RiskRejectedEvent（携带拒绝原因）

    soft 级别拒绝：记录警告，仍发布 RiskApprovedEvent（策略可接受此风险）
    hard 级别拒绝：发布 RiskRejectedEvent，链路终止
    """

    def __init__(
        self,
        bus:      EventBusPort,
        pipeline: RiskPipeline,
        account:  AccountPort,
        cache:    CachePort,
    ) -> None:
        self._bus      = bus
        self._pipeline = pipeline
        self._account  = account
        self._cache    = cache

        bus.subscribe(TargetPositionEvent, self._on_target)
        log.info("RiskService 启动，中间件: %s",
                 self._pipeline.middleware_names)

    def _on_target(self, event: TargetPositionEvent) -> None:
        instrument = event.instrument
        sym        = instrument.symbol

        # ── 1. 计算下单 delta ─────────────────────────────────────────────────
        # target_size 和 current_size 均带符号：正=多头，负=空头
        # delta > 0 → 需要买入（增多头/减空头）
        # delta < 0 → 需要卖出（减多头/增空头）
        delta = event.target_size - event.current_size
        if abs(delta) < instrument.lot_size:
            return   # delta 太小，忽略

        # ── 2. 构建订单 ───────────────────────────────────────────────────────
        price: Decimal | None = self._cache.get(f"price:{sym}")
        is_buy = delta > 0
        order_side = Side.BUY if is_buy else Side.SELL
        order_qty = instrument.round_qty(abs(delta))

        # reduce_only 逻辑：
        # 单向持仓模式（Binance 默认）：不需要 reduceOnly，交易所自动识别
        # 双向持仓模式（hedge_mode=True）：需要 reduceOnly + positionSide
        # 当前系统使用单向持仓模式，因此不设 reduceOnly
        reduce_only = False

        order = Order(
            instrument  = instrument,
            account_id  = event.account_id,
            side        = order_side,
            qty         = order_qty,
            order_type  = OrderType.MARKET,
            strategy_id = "",
            limit_price = price,
            reduce_only = reduce_only,
        )

        # ── 3. 组装 RiskContext ───────────────────────────────────────────────
        ctx = self._build_context(event.account_id, instrument.symbol)

        # ── 4. 执行风控检查 ───────────────────────────────────────────────────
        result = self._pipeline.check(order, ctx)

        # ── 5. 发布结果 ───────────────────────────────────────────────────────
        if result.passed:
            if result.level == "soft" and result.reason:
                log.warning("风控软告警 %s: %s", sym, result.reason)

            final_order = result.amended_order or order
            self._bus.publish(
                RiskApprovedEvent(
                    account_id=event.account_id,
                    order=final_order,
                ).caused_by(event)
            )
            log.debug("risk ✓  %s  qty=%.6f  side=%s",
                      sym, final_order.qty, final_order.side.value)
        else:
            self._bus.publish(
                RiskRejectedEvent(
                    account_id=event.account_id,
                    order=order,
                    reason=result.reason,
                    level=result.level,
                ).caused_by(event)
            )
            log.warning("risk ✗  %s  [%s] %s",
                        sym, result.level, result.reason)

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _build_context(self, account_id: str, symbol: str) -> RiskContext:
        """从 cache + account 组装风控上下文。"""
        nav = self._account.get_nav_usdt(account_id)

        # 当前持仓（Phase 2：account 存根始终返回 None）
        pos = self._account.get_position(account_id, symbol)
        positions = {symbol: pos} if pos else {}

        # 在途订单（Phase 2：暂无 OMS，为空列表；Phase 3 从 cache 读取）
        open_orders: list[Order] = []

        # 资金费率（从 cache 读取所有 funding:* 键）
        funding_rates: dict[str, Decimal] = {}
        rate = self._cache.get(f"funding:{symbol}")
        if rate is not None:
            funding_rates[symbol] = rate

        # 日内回撤（Phase 2 暂无，Phase 3 由 MonitorService 写入）
        daily_drawdown = self._cache.get(f"drawdown:{account_id}") or Decimal(0)

        return RiskContext(
            account_id    = account_id,
            nav_usdt      = nav,
            positions     = positions,
            open_orders   = open_orders,
            funding_rates = funding_rates,
            extra         = {"daily_drawdown": daily_drawdown},
        )
