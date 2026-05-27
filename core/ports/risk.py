"""
core/ports/risk.py  —  风控端口

RiskPipeline 实现此接口，RiskService 通过此接口调用风控检查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from core.domain.order import Order
from core.domain.position import Position


@dataclass
class RiskContext:
    """
    风控检查时的完整上下文。由 RiskService 从 cache 和 account 组装。
    """
    account_id:    str
    nav_usdt:      Decimal                   # 账户净值（USDT）
    positions:     dict[str, Position]       # symbol → Position
    open_orders:   list[Order]               # 当前在途订单
    funding_rates: dict[str, Decimal]        # symbol → funding rate
    extra:         dict = field(default_factory=dict)  # 扩展字段


@dataclass
class RiskResult:
    passed:        bool
    reason:        str         = ""
    level:         str         = "hard"        # "hard"（拒绝）| "soft"（警告）
    amended_order: Order | None = None          # 中间件修改后的订单

    @classmethod
    def approve(cls) -> RiskResult:
        return cls(passed=True)

    @classmethod
    def reject(cls, reason: str, level: str = "hard") -> RiskResult:
        return cls(passed=False, reason=reason, level=level)

    @classmethod
    def warn(cls, reason: str) -> RiskResult:
        return cls(passed=True, reason=reason, level="soft")


class RiskPort(Protocol):
    def check(self, order: Order, ctx: RiskContext) -> RiskResult: ...
