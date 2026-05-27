"""
core/ports/execution.py  —  交易所执行端口

OMS 通过此接口与交易所通信，不知道具体是哪个交易所。
ExchangeAdapter（adapters/exchange/）实现此接口。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from core.domain.balance import Balance
from core.domain.instrument import Instrument
from core.domain.order import Order
from core.domain.position import Position


class ExecutionPort(Protocol):
    exchange_id: str

    def submit(self, order: Order) -> str:
        """提交订单，返回交易所订单 ID（exchange_order_id）。"""
        ...

    def cancel(self, exchange_order_id: str) -> bool:
        """撤单，返回是否成功。"""
        ...

    def amend(self, exchange_order_id: str,
              qty: Decimal | None = None,
              price: Decimal | None = None) -> bool:
        """改单（数量或价格）。"""
        ...

    def get_position(self, instrument: Instrument,
                     account_id: str) -> Position | None:
        """从交易所拉取当前仓位（用于对账/灾难恢复）。"""
        ...

    def get_balance(self, account_id: str) -> Balance:
        """从交易所拉取账户余额。"""
        ...

    def get_funding_rate(self, instrument: Instrument) -> Decimal:
        """获取当前资金费率（仅对 PERP 有意义）。"""
        ...
