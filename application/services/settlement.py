"""
application/services/settlement.py

结算服务。

订阅：OrderFilledEvent
发布：SettlementEvent

职责（防腐层）：
  - OMS 只认识成交（OrderFilledEvent）
  - AccountService 只认识结算（SettlementEvent → 仓位/余额变更）
  - 两者之间通过 SettlementService 解耦，未来可在此加入：
      T+1 清算延迟、税费计算、多账号分摊、汇率换算
"""
from __future__ import annotations

import logging
from decimal import Decimal

from core.ports.bus import EventBusPort
from application.events import OrderFilledEvent, SettlementEvent

log = logging.getLogger(__name__)


class SettlementService:
    """Phase 3：T+0 直通结算（无延迟、无税费）。"""

    def __init__(self, bus: EventBusPort) -> None:
        self._bus = bus
        bus.subscribe(OrderFilledEvent, self._on_filled)
        log.info("SettlementService 启动（T+0 模式）")

    def _on_filled(self, event: OrderFilledEvent) -> None:
        """每次部分/全部成交都结算，使用增量 fill_qty。"""
        order = event.order
        self._bus.publish(
            SettlementEvent(
                account_id   = order.account_id,
                instrument   = order.instrument,
                settled_qty  = event.fill_qty,      # 增量而非累计
                side         = order.side,
                avg_price    = event.avg_price,     # 本次成交价
                commission   = event.commission,
            ).caused_by(event)
        )
        log.debug("settlement  %s  %s  qty=%.4f @ %.2f",
                  order.side.value, order.instrument.symbol,
                  event.fill_qty, event.avg_price)
