"""
core/domain/order.py

订单值对象。不可变——每次状态变更通过 replace() 产生新实例，
旧实例自动保留，天然形成审计轨迹。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from core.domain.instrument import Instrument


class Side(Enum):
    BUY  = "buy"
    SELL = "sell"


class PositionSide(Enum):
    BOTH  = "both"
    LONG  = "long"
    SHORT = "short"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderType(Enum):
    MARKET              = "market"
    LIMIT               = "limit"
    STOP_MARKET         = "stop_market"
    STOP_LIMIT          = "stop_limit"
    TAKE_PROFIT_MARKET  = "take_profit_market"
    TAKE_PROFIT         = "take_profit"        # 止盈限价
    TRAILING_STOP_MARKET = "trailing_stop_market"


class WorkingType(Enum):
    CONTRACT_PRICE = "contract_price"   # 标记价格触发
    MARK_PRICE     = "mark_price"       # 最新标记价格触发


class OrderStatus(Enum):
    NEW         = "new"
    SUBMITTED   = "submitted"
    PART_FILLED = "part_filled"
    FILLED      = "filled"
    CANCELLED   = "cancelled"
    REJECTED    = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                        OrderStatus.REJECTED)

    @property
    def is_active(self) -> bool:
        return self in (OrderStatus.SUBMITTED, OrderStatus.PART_FILLED)


# 合法状态转换表
_ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({
        OrderStatus.SUBMITTED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }),
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.PART_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }),
    OrderStatus.PART_FILLED: frozenset({
        OrderStatus.PART_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class TimeInForce(Enum):
    GTC = "gtc"   # Good Till Cancel
    IOC = "ioc"   # Immediate or Cancel
    FOK = "fok"   # Fill or Kill
    GTX = "gtx"   # Post-only（只挂 maker 单）


@dataclass(frozen=True)
class Order:
    """
    不可变订单值对象。

    构造示例：
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.BUY,
            qty=Decimal("0.01"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("65000"),
        )

    状态更新示例（产生新实例，旧实例保留）：
        submitted = order.with_update(
            status=OrderStatus.SUBMITTED,
            exchange_order_id="binance-12345",
        )
    """
    # ── 必填字段（无默认值）──────────────────────────────────────────────
    instrument:  Instrument
    account_id:  str
    side:        Side
    qty:         Decimal

    # ── 可选字段（有默认值）──────────────────────────────────────────────
    order_type:        OrderType       = OrderType.MARKET
    id:                str             = field(default_factory=lambda: str(uuid4()))
    strategy_id:       str             = ""
    limit_price:       Decimal|None    = None
    stop_price:        Decimal|None    = None
    tif:               TimeInForce     = TimeInForce.GTC
    reduce_only:       bool            = False

    # ── Binance 条件单参数 ───────────────────────────────────────────────
    position_side:     PositionSide|None = None    # 双向持仓模式需指定
    close_position:    bool            = False     # 一键平仓（不需要 quantity）
    working_type:      WorkingType     = WorkingType.CONTRACT_PRICE
    price_protect:     bool            = True      # 条件单触发价格保护
    callback_rate:     Decimal|None    = None      # TRAILING_STOP_MARKET 回调比例
    activation_price:  Decimal|None    = None      # TRAILING_STOP_MARKET 激活价格

    # ── 成交后填充（不应在构造时传入）──────────────────────────────────
    status:            OrderStatus  = OrderStatus.NEW
    filled_qty:        Decimal      = Decimal(0)
    avg_fill_price:    Decimal      = Decimal(0)
    exchange_order_id: str | None   = None

    # ── 便捷属性 ──────────────────────────────────────────────────────
    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def notional(self) -> Decimal:
        """以 limit_price 或 avg_fill_price 估算名义价值。"""
        price = self.avg_fill_price or self.limit_price or Decimal(0)
        return self.qty * price

    @property
    def is_buy(self) -> bool:
        return self.side == Side.BUY

    def with_update(self, **kwargs) -> Order:
        """返回带更新字段的新 Order 实例（不可变更新），含状态转换验证。"""
        new_status = kwargs.get("status")
        if new_status is not None and new_status != self.status:
            allowed = _ORDER_TRANSITIONS.get(self.status, frozenset())
            if new_status not in allowed:
                raise ValueError(
                    f"非法状态转换: {self.status.value} → {new_status.value}"
                )
        return replace(self, **kwargs)

    def __repr__(self) -> str:
        return (f"Order({self.id[:8]}… {self.side.value} "
                f"{self.qty} {self.instrument.symbol} "
                f"{self.order_type.value} [{self.status.value}])")
