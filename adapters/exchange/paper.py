"""
adapters/exchange/paper.py

模拟交易所。订阅 OrderSubmittedEvent，立即以当前缓存价格成交并发布 FillEvent。

设计要点：
  - 通过订阅 OrderSubmittedEvent 触发成交，而非在 submit() 内直接发布。
    这保证了 OMS 在 FillEvent 到达时，exorder 映射已经写入缓存（时序安全）。
  - submit() 只生成并返回 exchange_order_id，不立即成交。
  - 仿真手续费：0.1%（taker），做市商单：0.02%（maker）。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

from core.domain.balance import Balance
from core.domain.instrument import Instrument
from core.domain.order import Order, Side
from core.domain.position import Position
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort
from application.events import FillEvent, OrderSubmittedEvent

log = logging.getLogger(__name__)

_TAKER_FEE = Decimal("0.001")   # 0.1%
_MAKER_FEE = Decimal("0.0002")  # 0.02%


class PaperExchange:
    """
    满足 ExecutionPort 协议。

    submit() → 生成 exchange_order_id，暂存订单信息
    on_submitted() → 接收 OrderSubmittedEvent，查缓存价格，发布 FillEvent
    """
    exchange_id = "paper"

    def __init__(
        self,
        bus:          EventBusPort,
        cache:        CachePort,
        initial_usdt: Decimal = Decimal("10_000"),
    ) -> None:
        self._bus          = bus
        self._cache        = cache
        self._initial_usdt = initial_usdt
        # exchange_order_id → Order（等待成交）
        self._pending: dict[str, Order] = {}

        bus.subscribe(OrderSubmittedEvent, self._on_submitted)
        log.info("PaperExchange 启动（初始 USDT: %s）", initial_usdt)

    # ── ExecutionPort 接口 ────────────────────────────────────────────────────

    def submit(self, order: Order) -> str:
        """
        生成 exchange_order_id 并暂存订单。
        实际成交在收到 OrderSubmittedEvent 时触发（保证时序安全）。
        """
        exchange_id = f"PAPER_{order.id}"
        self._pending[exchange_id] = order
        log.debug("paper.submit %s → %s", order.id[:8], exchange_id[-8:])
        return exchange_id

    def cancel(self, exchange_order_id: str) -> bool:
        removed = self._pending.pop(exchange_order_id, None)
        return removed is not None

    def amend(self, exchange_order_id: str,
              qty: Decimal | None = None,
              price: Decimal | None = None) -> bool:
        return False   # Paper exchange 不支持改单

    def get_position(self, instrument: Instrument,
                     account_id: str) -> Position | None:
        return None    # Phase 3: 对账功能 Phase 4 实现

    def get_balance(self, account_id: str) -> Balance:
        return Balance(account_id, {"USDT": self._initial_usdt})

    def get_funding_rate(self, instrument: Instrument) -> Decimal:
        return Decimal(0)

    def normalize_instrument(self, raw_symbol: str) -> Instrument:
        raise NotImplementedError

    def denormalize_order(self, order: Order) -> dict:
        raise NotImplementedError

    # ── 内部成交逻辑 ──────────────────────────────────────────────────────────

    def _on_submitted(self, event: OrderSubmittedEvent) -> None:
        """
        收到 OMS 的 OrderSubmittedEvent 后立即以当前价格成交。
        此时 OMS 已写入 exorder 映射，FillEvent 到来时可正确路由。
        """
        exchange_id = event.exchange_order_id
        order = self._pending.pop(exchange_id, None)
        if order is None:
            log.warning("PaperExchange: 未找到订单 %s", exchange_id)
            return

        # 取当前市价
        sym   = order.instrument.symbol
        price = self._cache.get(f"price:{sym}")
        if price is None:
            price = order.limit_price
        if price is None:
            log.error("PaperExchange: %s 无价格，无法成交", sym)
            return

        # 模拟滑点（市价单加一个 tick）
        tick = order.instrument.tick_size
        if order.side == Side.BUY:
            fill_price = price + tick    # 买入略贵
        else:
            fill_price = price - tick    # 卖出略便宜

        # 手续费
        is_maker   = (order.limit_price is not None)
        fee_rate   = _MAKER_FEE if is_maker else _TAKER_FEE
        commission = order.qty * fill_price * fee_rate

        self._bus.publish(
            FillEvent(
                exchange_order_id = exchange_id,
                instrument        = order.instrument,
                side              = order.side,
                filled_qty        = order.qty,
                fill_price        = fill_price,
                commission        = commission,
                commission_asset  = order.instrument.quote,
                is_maker          = is_maker,
            ).caused_by(event)
        )
        log.debug("paper fill  %s  %s  qty=%.4f @ %.2f  fee=%.4f",
                  order.side.value, sym, order.qty, fill_price, commission)
