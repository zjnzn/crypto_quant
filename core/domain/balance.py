"""
core/domain/balance.py

多币种余额。加密交易所通常以 USDT/BTC/ETH 等多种资产作为保证金，
必须在领域层明确建模，不能用单一 float 表示。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Balance:
    """
    账户多币种余额快照。

    balances: {"USDT": Decimal("10000"), "BTC": Decimal("0.5")}
    """
    account_id: str
    balances:   dict[str, Decimal] = field(default_factory=dict)

    def available(self, currency: str) -> Decimal:
        """返回指定币种可用余额，未持有返回 0。"""
        return self.balances.get(currency, Decimal(0))

    def total_in_quote(self, quote: str, prices: dict[str, Decimal]) -> Decimal:
        """
        将所有资产折算为指定计价货币（通常是 USDT）的总价值。

        prices: {"BTC": Decimal("65000"), "ETH": Decimal("3500")}
        """
        total = Decimal(0)
        for currency, amount in self.balances.items():
            if currency == quote:
                total += amount
            elif currency in prices:
                total += amount * prices[currency]
            # 未知价格的资产忽略（保守估计）
        return total

    def update(self, currency: str, delta: Decimal) -> None:
        """原地更新余额（由 AccountService 调用）。"""
        current = self.balances.get(currency, Decimal(0))
        self.balances[currency] = current + delta

    def __repr__(self) -> str:
        items = ", ".join(f"{c}:{v}" for c, v in self.balances.items())
        return f"Balance({self.account_id}: {{{items}}})"
