"""
core/domain/instrument.py

跨交易所统一标的模型。不可变值对象，零外部依赖。
系统内部只使用 Instrument，不使用裸字符串 symbol，
防止交易所私有格式（"BTCUSDT" vs "BTC-USDT-SWAP"）污染业务层。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class InstrumentKind(Enum):
    SPOT   = "spot"
    PERP   = "perp"      # 永续合约（有资金费率）
    FUTURE = "future"    # 交割合约（有到期日）
    OPTION = "option"    # 期权


@dataclass(frozen=True)
class Instrument:
    """
    标的值对象。

    字段说明：
      symbol       — 系统内统一 ID，如 "BTC-USDT-PERP"
      exchange     — 所属交易所，如 "binance" / "okx" / "paper"
      base         — 基础货币，如 "BTC"
      quote        — 计价货币，如 "USDT"
      kind         — 合约类型
      tick_size    — 最小价格变动单位（Decimal 精度）
      lot_size     — 最小下单数量
      min_notional — 最小名义价值（以 quote 计）
      max_leverage — 最大允许杠杆倍数
    """
    symbol:       str
    exchange:     str
    base:         str
    quote:        str
    kind:         InstrumentKind
    tick_size:    Decimal
    lot_size:     Decimal
    min_notional: Decimal
    max_leverage: int = 125

    def __str__(self) -> str:
        return f"{self.symbol}@{self.exchange}"

    def __repr__(self) -> str:
        return f"Instrument({self.symbol}@{self.exchange}, {self.kind.value})"

    def round_price(self, price: Decimal) -> Decimal:
        """将价格对齐到 tick_size 精度。"""
        return (price / self.tick_size).quantize(Decimal(1)) * self.tick_size

    def round_qty(self, qty: Decimal) -> Decimal:
        """将数量对齐到 lot_size 精度。"""
        return (qty / self.lot_size).quantize(Decimal(1)) * self.lot_size
