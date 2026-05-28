"""
application/services/oms.py

订单管理服务（OMS）。

订阅：RiskApprovedEvent → 提交到交易所
订阅：FillEvent → 更新订单状态
发布：OrderSubmittedEvent（提交后）
发布：OrderFilledEvent（完全成交后）

状态机：
  NEW → SUBMITTED → PART_FILLED → FILLED
                 ↘ CANCELLED
                 ↘ REJECTED

缓存键（重启安全）：
  order:{order_id}       → Order 对象
  exorder:{exchange_id}  → order_id（反查映射）

In-flight 追踪：
  _pending_orders 维护当前在途订单（status.is_active），
  RiskService 通过 get_open_orders() 获取，防止超量下单。
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from decimal import Decimal

from core.domain.order import Order, OrderStatus
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from core.ports.execution import ExecutionPort
from application.events import (FillEvent, OrderFilledEvent,
                                 OrderSubmittedEvent, RiskApprovedEvent)

log = logging.getLogger(__name__)

_ORDER_TTL = timedelta(days=1)


class OMSService:
    """
    订单生命周期管理。

    重启安全：所有订单状态写入 cache，重启后可从 cache 恢复。
    实盘：重启时先从交易所拉持仓/在途订单，再重建 cache（Phase 4）。
    """

    def __init__(
        self,
        bus:      EventBusPort,
        exchange: ExecutionPort,
        cache:    CachePort,
    ) -> None:
        self._bus      = bus
        self._exchange = exchange
        self._cache    = cache
        self._pending: dict[str, Order] = {}   # order_id → active Order
        self._lock     = threading.RLock()

        bus.subscribe(RiskApprovedEvent, self._on_approved)
        bus.subscribe(FillEvent,         self._on_fill)

        log.info("OMSService 启动（交易所: %s）", exchange.exchange_id)

    # ── RiskApprovedEvent → 提交 ──────────────────────────────────────────────

    def _on_approved(self, event: RiskApprovedEvent) -> None:
        order = event.order

        # ① 先写缓存（重启安全）
        self._cache.set(f"order:{order.id}", order, ttl=_ORDER_TTL)

        # ② 加入 pending（供 RiskService 查询在途订单）
        with self._lock:
            self._pending[order.id] = order

        # ③ 提交到交易所
        try:
            exchange_id = self._exchange.submit(order)
        except Exception:
            log.exception("提交订单失败: %s", order.id)
            rejected = order.with_update(status=OrderStatus.REJECTED)
            self._cache.set(f"order:{order.id}", rejected, ttl=_ORDER_TTL)
            with self._lock:
                self._pending.pop(order.id, None)
            return

        # ④ 注册 exorder 反查映射（必须在 OrderSubmittedEvent 发布之前）
        #    因为 PaperExchange 会在 OrderSubmittedEvent 触发时立即发布 FillEvent，
        #    OMS.on_fill 需要此映射来定位订单。
        self._cache.set(f"exorder:{exchange_id}", order.id, ttl=_ORDER_TTL)

        # ⑤ 更新订单状态为 SUBMITTED
        submitted = order.with_update(
            status=OrderStatus.SUBMITTED,
            exchange_order_id=exchange_id,
        )
        self._cache.set(f"order:{order.id}", submitted, ttl=_ORDER_TTL)
        with self._lock:
            self._pending[order.id] = submitted

        # ⑥ 发布 OrderSubmittedEvent
        #    （PaperExchange 会在此处理函数内同步发布 FillEvent）
        self._bus.publish(
            OrderSubmittedEvent(
                order=submitted,
                exchange_order_id=exchange_id,
            ).caused_by(event)
        )
        log.debug("order submitted  %s → exch:%s", order.id[:8], exchange_id[-8:])

    # ── FillEvent → 更新状态 ──────────────────────────────────────────────────

    def _on_fill(self, event: FillEvent) -> None:
        # 通过 exchange_order_id 反查 order_id
        order_id = self._cache.get(f"exorder:{event.exchange_order_id}")
        if order_id is None:
            log.warning("OMS: 未找到 exorder 映射 %s", event.exchange_order_id)
            return

        order: Order | None = self._cache.get(f"order:{order_id}")
        if order is None:
            log.warning("OMS: 未找到订单 %s", order_id)
            return

        if order.status.is_terminal:
            log.debug("OMS: 忽略终态订单的成交回报 %s", order_id)
            return

        # 滚动计算成交均价
        prev_cost   = order.filled_qty * order.avg_fill_price
        new_filled  = order.filled_qty + event.filled_qty
        new_avg     = (prev_cost + event.filled_qty * event.fill_price) / new_filled

        new_status = (
            OrderStatus.FILLED
            if new_filled >= order.qty * Decimal("0.9999")   # 容忍极小浮点误差
            else OrderStatus.PART_FILLED
        )

        updated = order.with_update(
            filled_qty=new_filled,
            avg_fill_price=new_avg,
            status=new_status,
        )
        self._cache.set(f"order:{order_id}", updated, ttl=_ORDER_TTL)

        # 更新 pending：终态移除，否则更新
        with self._lock:
            if new_status.is_terminal:
                self._pending.pop(order_id, None)
            else:
                self._pending[order_id] = updated

        log.debug("order fill  %s  filled=%.4f/%.4f  avg=%.2f  status=%s",
                  order_id[:8], new_filled, order.qty,
                  new_avg, new_status.value)

        if new_status == OrderStatus.FILLED:
            self._bus.publish(
                OrderFilledEvent(
                    order=updated,
                    avg_price=new_avg,
                    commission=event.commission,
                ).caused_by(event)
            )

    # ── 在途订单查询（供 RiskService 使用）────────────────────────────────────

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """
        返回当前在途订单列表（status.is_active）。
        symbol=None → 返回所有在途订单。
        symbol="ETH-USDT-PERP" → 只返回该标的的在途订单。
        """
        with self._lock:
            orders = list(self._pending.values())
        if symbol is not None:
            orders = [o for o in orders if o.instrument.symbol == symbol]
        return orders

    # ── 查询接口（测试 / 监控用）─────────────────────────────────────────────

    def get_order(self, order_id: str) -> Order | None:
        return self._cache.get(f"order:{order_id}")