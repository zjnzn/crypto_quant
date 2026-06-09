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

    @property
    def opposite(self) -> Side:
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderType(Enum):
    MARKET              = "market"
    LIMIT               = "limit"
    STOP                = "stop"
    STOP_MARKET         = "stop_market"
    STOP_LIMIT          = "stop_limit"
    TAKE_PROFIT         = "take_profit"
    TAKE_PROFIT_MARKET  = "take_profit_market"
    TRAILING_STOP_MARKET = "trailing_stop_market"


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


class TimeInForce(Enum):
    GTC = "gtc"   # Good Till Cancel
    IOC = "ioc"   # Immediate or Cancel
    FOK = "fok"   # Fill or Kill
    GTX = "gtx"   # Post-only（只挂 maker 单）
    GTD = "gtd"   # Good Till Date（指定时间自动撤单）


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
    order_type:        OrderType    = OrderType.MARKET
    id:                str          = field(default_factory=lambda: str(uuid4()))
    strategy_id:       str          = ""
    leverage:          int          = 1              # 订单杠杆倍数（开仓/加仓时使用）
    limit_price:       Decimal|None = None
    stop_price:        Decimal|None = None
    tif:               TimeInForce  = TimeInForce.GTC
    reduce_only:       bool         = False
    callback_rate:     Decimal|None = None      # 追踪止损回调比率（%）
    activation_price:  Decimal|None = None      # 追踪止损激活价格
    good_till_date:    int|None     = None      # GTD 自动撤单时间戳 (ms)

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
        """返回带更新字段的新 Order 实例（不可变更新）。"""
        return replace(self, **kwargs)

    def __repr__(self) -> str:
        return (f"Order({self.id[:8]}… {self.side.value} "
                f"{self.qty} {self.instrument.symbol} "
                f"{self.order_type.value} [{self.status.value}])")
