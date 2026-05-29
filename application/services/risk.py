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

        # ── 1. 计算下单 delta ─────────────────────────────────────────────────
        delta = event.target_size - event.current_size
        if abs(delta) < instrument.lot_size:
            return   # delta 太小，忽略（PortfolioService 应已过滤，双重保险）

        # ── 2. 构建订单 ───────────────────────────────────────────────────────
        price: Decimal | None = self._cache.get(f"price:{sym}")
        # ── 订单方向由 delta 符号决定，而非 target_side ──────────────────────
        # target_side 是目标仓位方向（多/空），不是订单方向。
        # 当 current > target（需要减仓）时：delta < 0 → SELL 单
        # 当 current < target（需要增仓）时：delta > 0 → BUY 单
        is_reducing  = delta < 0
        order_side   = Side.SELL if is_reducing else Side.BUY

        # 减仓时，将数量限制在已知持仓范围内，
        # 防止因缺少 FillEvent 回调导致仓位记录滞后时超量下单（-2022）。
        order_qty = instrument.round_qty(abs(delta))
        if is_reducing:
            cur_pos  = self._account.get_position(
                event.account_id, instrument.symbol)
            cur_size = cur_pos.size if cur_pos else Decimal(0)
            # 注意：这里只减去在途卖单，与 compute_pending_delta 逻辑不同
            # 因为 reduce_only 只关心已有的卖出挂单，避免超额平仓
            if self._oms is not None:
                for pending in self._oms.get_open_orders(sym):
                    if pending.side == Side.SELL:
                        cur_size -= pending.remaining_qty
            order_qty = instrument.round_qty(min(order_qty, cur_size))
            if order_qty < instrument.lot_size:
                log.debug("reduce_only qty capped to 0, skip: %s", instrument.symbol)
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