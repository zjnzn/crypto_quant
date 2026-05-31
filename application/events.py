"""
application/events.py

所有应用事件的定义。一个文件，一目了然。

事件流向：
  行情适配器  → TradeEvent / BookEvent / FundingRateEvent
  SignalService   → SignalEvent
  PortfolioService → TargetPositionEvent
  RiskService    → RiskApprovedEvent | RiskRejectedEvent
  AccountService → OrderReadyEvent
  OMS            → OrderSubmittedEvent
  ExchangeAdapter → FillEvent
  OMS            → OrderFilledEvent
  SettlementService → SettlementEvent
  AccountService → PositionUpdatedEvent | BalanceUpdatedEvent
  MonitorService → PnLEvent | AlertEvent
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.domain.event import Event
from core.domain.instrument import Instrument
from core.domain.order import Order, Side
from core.domain.position import PositionSide


# ── 行情事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeEvent(Event):
    """单笔逐笔成交（来自交易所 WebSocket trades 频道）。"""
    instrument:  Instrument = None
    price:       Decimal    = Decimal(0)
    qty:         Decimal    = Decimal(0)
    buyer_maker: bool       = False      # True = 买方是挂单方（下跌成交）


@dataclass(frozen=True)
class BookEvent(Event):
    """最优盘口快照（BBO）。"""
    instrument: Instrument = None
    bid_price:  Decimal    = Decimal(0)
    bid_qty:    Decimal    = Decimal(0)
    ask_price:  Decimal    = Decimal(0)
    ask_qty:    Decimal    = Decimal(0)

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price


@dataclass(frozen=True)
class FundingRateEvent(Event):
    """
    资金费率事件（永续合约特有）。
    正值 = 多头付给空头；负值 = 空头付给多头。
    """
    instrument:      Instrument        = None
    rate:            Decimal           = Decimal(0)
    next_funding_ts: "datetime | None" = None


# ── 信号事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalEvent(Event):
    """策略产生的交易信号。"""
    instrument:  Instrument = None
    score:       float      = 0.0    # [-1, 1]：正数做多，负数做空，0 平仓
    confidence:  float      = 0.0    # [0, 1]
    strategy_id: str        = ""
    meta:        dict       = field(default_factory=dict)


# ── 组合事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TargetPositionEvent(Event):
    """
    PortfolioService 计算后的目标仓位（净仓模式）。

    净仓位规则：
      正数 = 多头仓位数量
      负数 = 空头仓位数量
      0    = 空仓
    """
    account_id:      str              = ""
    instrument:      Instrument       = None
    target_size:     Decimal          = Decimal(0)   # 目标持仓量（绝对值）
    target_side:     Side             = Side.BUY     # 目标仓位方向
    current_size:    Decimal          = Decimal(0)   # 当前持仓量（绝对值）
    current_side:    PositionSide | None = None      # 当前仓位方向
    net_position:    Decimal          = Decimal(0)   # 当前净仓位（带符号）
    target_position: Decimal          = Decimal(0)   # 目标净仓位（带符号）


# ── 风控事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskApprovedEvent(Event):
    account_id: str   = ""
    order:      Order = None


@dataclass(frozen=True)
class RiskRejectedEvent(Event):
    account_id: str   = ""
    order:      Order = None
    reason:     str   = ""
    level:      str   = "hard"    # "hard" | "soft"


# ── 账号 → 执行事件 ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderReadyEvent(Event):
    """AccountService 轧差后，准备提交给 OMS 的净订单。"""
    account_id: str   = ""
    order:      Order = None


# ── 执行事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderSubmittedEvent(Event):
    """OMS 已提交到交易所。"""
    order:             Order = None
    exchange_order_id: str   = ""


@dataclass(frozen=True)
class FillEvent(Event):
    """
    来自交易所的成交回报。
    可由 ExchangeAdapter（实盘 WebSocket）或 PaperExchange（模拟）发布。
    """
    exchange_order_id: str        = ""
    instrument:        Instrument = None
    side:              Side       = Side.BUY
    filled_qty:        Decimal    = Decimal(0)
    fill_price:        Decimal    = Decimal(0)
    commission:        Decimal    = Decimal(0)
    commission_asset:  str        = "USDT"
    is_maker:          bool       = False


@dataclass(frozen=True)
class OrderFilledEvent(Event):
    """OMS 确认订单完全成交后发布，触发结算层。"""
    order:      Order   = None
    avg_price:  Decimal = Decimal(0)
    commission: Decimal = Decimal(0)


# ── 结算事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SettlementEvent(Event):
    """
    SettlementService 发布，AccountService 订阅。
    解耦 OMS 和 AccountService——两者之间不直接依赖。
    """
    account_id:  str        = ""
    instrument:  Instrument = None
    settled_qty: Decimal    = Decimal(0)
    side:        Side       = Side.BUY
    avg_price:   Decimal    = Decimal(0)
    net_pnl:     Decimal    = Decimal(0)    # 已扣除手续费的净盈亏
    commission:  Decimal    = Decimal(0)


# ── 账号事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionUpdatedEvent(Event):
    account_id:   str        = ""
    instrument:   Instrument = None
    new_size:     Decimal    = Decimal(0)
    entry_price:  Decimal    = Decimal(0)
    realized_pnl: Decimal    = Decimal(0)


@dataclass(frozen=True)
class BalanceUpdatedEvent(Event):
    account_id:  str     = ""
    currency:    str     = "USDT"
    delta:       Decimal = Decimal(0)
    new_balance: Decimal = Decimal(0)


# ── 监控事件 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PnLEvent(Event):
    account_id:  str     = ""
    realized:    Decimal = Decimal(0)
    unrealized:  Decimal = Decimal(0)
    total:       Decimal = Decimal(0)
    nav_usdt:    Decimal = Decimal(0)


@dataclass(frozen=True)
class AlertEvent(Event):
    level:  str  = "info"    # "info" | "warn" | "critical"
    title:  str  = ""
    detail: dict = field(default_factory=dict)


# ── 便捷集合（用于类型检查和 isinstance 判断）────────────────────────────────

MARKET_EVENTS  = (TradeEvent, BookEvent, FundingRateEvent)
SIGNAL_EVENTS  = (SignalEvent,)
RISK_EVENTS    = (RiskApprovedEvent, RiskRejectedEvent)
FILL_EVENTS    = (FillEvent, OrderFilledEvent)
ACCOUNT_EVENTS = (PositionUpdatedEvent, BalanceUpdatedEvent)
MONITOR_EVENTS = (PnLEvent, AlertEvent)

ALL_EVENT_TYPES = (
    TradeEvent, BookEvent, FundingRateEvent,
    SignalEvent,
    TargetPositionEvent,
    RiskApprovedEvent, RiskRejectedEvent,
    OrderReadyEvent,
    OrderSubmittedEvent, FillEvent, OrderFilledEvent,
    SettlementEvent,
    PositionUpdatedEvent, BalanceUpdatedEvent,
    PnLEvent, AlertEvent,
)
