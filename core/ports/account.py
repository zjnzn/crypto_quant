"""
core/ports/account.py  —  账号端口

AccountService 实现此接口。StrategyContext 和 RiskService 通过此接口
读取仓位和净值，不直接访问 AccountService 的内部状态。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from core.domain.order import Order
from core.domain.position import Position


class AccountPort(Protocol):
    def get_position(self, account_id: str,
                     symbol: str) -> Position | None:
        """获取指定账号指定标的的虚拟仓位（系统内部记录，非交易所实际仓位）。"""
        ...

    def get_nav_usdt(self, account_id: str) -> Decimal:
        """获取账号净值（USDT 计）。"""
        ...

    def get_equity_usdt(self, account_id: str) -> Decimal:
        """获取账号权益 = 余额 + 未实现盈亏（USDT 计）。"""
        ...

    def net_orders(self, account_id: str,
                   orders: list[Order]) -> list[Order]:
        """
        多策略订单轧差。
        同一标的、相反方向的订单合并为净头寸，
        避免对敲和无效手续费。

        示例：策略A买100，策略B卖80 → 净买20
        """
        ...
