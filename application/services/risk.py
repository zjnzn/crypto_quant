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
from core.domain.position import PositionSide
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

    OMS in-flight 追踪：
      _build_context() 从 OMS 获取当前在途订单，
      计入 RiskContext.open_orders，供 OpenOrdersLimitMiddleware 等使用，
      防止在途订单未确认前重复下单导致超量。
    """

    def __init__(
        self,
        bus:      EventBusPort,
        pipeline: RiskPipeline,
        account:  AccountPort,
        cache:    CachePort,
        oms:     object | None = None,  # OMSService（延迟注入，避免循环依赖）
    ) -> None:
        self._bus      = bus
        self._pipeline = pipeline
        self._account  = account
        self._cache    = cache
        self._oms      = oms

        bus.subscribe(TargetPositionEvent, self._on_target)
        log.info("RiskService 启动，中间件: %s",
                 self._pipeline.middleware_names)

    def set_oms(self, oms: object) -> None:
        """延迟注入 OMS（container 中解决循环依赖）。"""
        self._oms = oms

    def _on_target(self, event: TargetPositionEvent) -> None:
        instrument = event.instrument
        sym        = instrument.symbol

        # ── 1. 计算下单 delta ─────────────────────────────────────────────────
        # target_side 决定仓位方向（LONG/SHORT），target_size 是数量（永远为正）
        # current_size 是当前持仓数量（永远为正），current_side 是当前方向

        cur_pos = self._account.get_position(event.account_id, sym)
        current_size = cur_pos.size if cur_pos else Decimal(0)
        current_side = cur_pos.side if cur_pos else None

        # 判断是否需要交易
        if current_side == event.target_side and current_size == event.target_size:
            return  # 方向和数量都一致，无需交易

        # ── 2. 构建订单 ───────────────────────────────────────────────────────
        price: Decimal | None = self._cache.get(f"price:{sym}")

        # 计算订单方向和数量
        # 关键：先处理方向翻转（多→空或空→多），再处理同方向调整
        if current_side is None or current_size == 0:
            # 无仓位 → 开仓
            order_side = Side.BUY if event.target_side == Side.BUY else Side.SELL
            order_qty = event.target_size
            is_reducing = False
        elif (current_side == PositionSide.LONG and event.target_side == Side.SELL):
            # 多头 → 目标空头：先平多
            order_side = Side.SELL
            order_qty = current_size  # 平掉全部多头
            is_reducing = True
        elif (current_side == PositionSide.SHORT and event.target_side == Side.BUY):
            # 空头 → 目标多头：先平空
            order_side = Side.BUY
            order_qty = current_size  # 平掉全部空头
            is_reducing = True
        elif current_side == PositionSide.LONG:
            # 多头 → 调整多头数量
            delta = event.target_size - current_size
            if abs(delta) < instrument.lot_size:
                return
            if delta > 0:
                order_side = Side.BUY
                order_qty = delta
                is_reducing = False
            else:
                order_side = Side.SELL
                order_qty = abs(delta)
                is_reducing = True
        elif current_side == PositionSide.SHORT:
            # 空头 → 调整空头数量
            delta = event.target_size - current_size
            if abs(delta) < instrument.lot_size:
                return
            if delta > 0:
                order_side = Side.SELL
                order_qty = delta
                is_reducing = False
            else:
                order_side = Side.BUY
                order_qty = abs(delta)
                is_reducing = True
        else:
            return

        order_qty = instrument.round_qty(order_qty)
        if order_qty < instrument.lot_size:
            return

        order = Order(
            instrument  = instrument,
            account_id  = event.account_id,
            side        = order_side,
            qty         = order_qty,
            order_type  = OrderType.MARKET,
            strategy_id = "",
            limit_price = price,
            reduce_only = is_reducing,
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
        """从 cache + account + OMS 组装风控上下文。"""
        nav = self._account.get_nav_usdt(account_id)

        # 当前持仓（Phase 2：account 存根始终返回 None）
        pos = self._account.get_position(account_id, symbol)
        positions = {symbol: pos} if pos else {}

        # ★ 在途订单：从 OMS 获取，替代原来的空列表
        open_orders: list[Order] = []
        if self._oms is not None:
            open_orders = self._oms.get_open_orders(symbol)

        # 资金费率（从 cache 读取所有 funding:* 键）
        funding_rates: dict[str, Decimal] = {}
        rate = self._cache.get(f"funding:{symbol}")
        if rate is not None:
            funding_rates[symbol] = rate

        # 日内回撤（Phase 2 暂无，Phase 3 由 MonitorService 写入）
        daily_drawdown = self._cache.get(f"drawdown:{account_id}") or Decimal(0)

        # 最新成交价（供风控中间件估算 market order 名义价值）
        price = self._cache.get(f"price:{symbol}")

        # Precompute pending delta for middleware
        pending_delta = Decimal(0)
        if self._oms is not None:
            pending_delta = self._oms.compute_pending_delta(symbol)

        return RiskContext(
            account_id    = account_id,
            nav_usdt      = nav,
            positions     = positions,
            open_orders   = open_orders,
            funding_rates = funding_rates,
            extra         = {"daily_drawdown": daily_drawdown, "price": price, "pending_delta": pending_delta},
        )