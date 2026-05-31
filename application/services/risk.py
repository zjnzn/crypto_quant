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

        # ── 1. 净仓模式判断 ─────────────────────────────────────────────────
        # target_position: 正数=多头数量，负数=空头数量，0=空仓
        # net_position: 当前净仓位（同上）

        target_pos = event.target_position  # 目标净仓位
        current_pos = event.net_position    # 当前净仓位

        # 判断是否需要交易
        if target_pos == current_pos:
            return  # 净仓位一致，无需调整

        # ── 2. 构建订单（净仓模式）─────────────────────────────────────────
        price: Decimal | None = self._cache.get(f"price:{sym}")

        # 核心逻辑：避免双向持仓锁仓
        if target_pos == 0:
            # 目标空仓 → 平掉所有仓位
            if current_pos > 0:
                # 多头 → 平多
                order_side = Side.SELL
                order_qty = abs(current_pos)
                is_reducing = True
            else:  # current_pos < 0
                # 空头 → 平空
                order_side = Side.BUY
                order_qty = abs(current_pos)
                is_reducing = True
        elif current_pos == 0:
            # 当前空仓 → 开仓
            order_side = Side.BUY if target_pos > 0 else Side.SELL
            order_qty = abs(target_pos)
            is_reducing = False
        elif (target_pos > 0 and current_pos > 0) or (target_pos < 0 and current_pos < 0):
            # 同方向调整
            if abs(target_pos) > abs(current_pos):
                # 加仓
                order_side = Side.BUY if target_pos > 0 else Side.SELL
                order_qty = abs(target_pos) - abs(current_pos)
                is_reducing = False
            else:
                # 减仓
                order_side = Side.SELL if current_pos > 0 else Side.BUY
                order_qty = abs(current_pos) - abs(target_pos)
                is_reducing = True
        else:
            # 方向相反 → 先平仓（分步执行，避免锁仓）
            # 第一笔：平掉当前仓位
            order_side = Side.SELL if current_pos > 0 else Side.BUY
            order_qty = abs(current_pos)
            is_reducing = True

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