"""
core/domain/position.py
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from core.domain.instrument import Instrument


class MarginMode(Enum):
    CROSS    = "cross"     # 全仓（共享账户保证金）
    ISOLATED = "isolated"  # 逐仓（每个仓位独立保证金）


class PositionSide(Enum):
    LONG  = "long"
    SHORT = "short"
    NET   = "net"   # 现货 / 单向持仓模式


@dataclass
class Position:
    """
    仓位快照。可变——由 AccountService 持续更新。
    每次变更同时发布 PositionUpdatedEvent，不直接暴露给策略。
    策略通过 StrategyContext.position() 读取只读快照。
    """
    instrument:        Instrument
    account_id:        str
    strategy_id:       str
    side:              PositionSide
    size:              Decimal         # 持仓数量（始终为正，方向由 side 表示）
    entry_price:       Decimal         # 开仓均价
    leverage:          int    = 1
    margin_mode:       MarginMode = MarginMode.CROSS
    liquidation_price: Decimal | None = None
    unrealized_pnl:    Decimal = Decimal(0)

    @property
    def notional(self) -> Decimal:
        """以开仓均价计算名义价值。"""
        return self.size * self.entry_price

    @property
    def is_empty(self) -> bool:
        return self.size == Decimal(0)

    def calc_unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        if self.is_empty:
            return Decimal(0)
        if self.side == PositionSide.LONG:
            return self.size * (mark_price - self.entry_price)
        else:
            return self.size * (self.entry_price - mark_price)

    def __repr__(self) -> str:
        return (f"Position({self.instrument.symbol} "
                f"{self.side.value} {self.size} @ {self.entry_price})")
