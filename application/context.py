"""
application/context.py

策略的完整世界视图。策略只通过 StrategyContext 感知外部，
不直接 import 任何 adapter 或 service。

回测和实盘注入不同的 clock/cache/account 实现，策略代码零改动。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from datetime import timedelta

from core.domain.instrument import Instrument
from core.domain.position import Position
from core.ports.account import AccountPort
from core.ports.cache import CachePort
from core.ports.clock import ClockPort


@dataclass
class StrategyContext:
    """
    策略看到的完整世界。由 container.py 中的工厂函数创建，
    每次事件触发时重新创建（保证状态一致性）。

    策略不应该持久化 StrategyContext 实例，只在回调中使用。
    """
    account_id:  str
    strategy_id: str
    clock:       ClockPort
    cache:       CachePort
    account:     AccountPort

    # ── 时间 ──────────────────────────────────────────────────────────────────

    def now(self) -> datetime:
        """当前时间。回测返回模拟时间，实盘返回系统时间。"""
        return self.clock.now()

    # ── 行情 ──────────────────────────────────────────────────────────────────

    def last_price(self, instrument: Instrument) -> Decimal | None:
        """最新成交价。从缓存读取，由 SignalService 在 TradeEvent 时更新。"""
        return self.cache.get(f"price:{instrument.symbol}")

    def bid(self, instrument: Instrument) -> Decimal | None:
        return self.cache.get(f"bid:{instrument.symbol}")

    def ask(self, instrument: Instrument) -> Decimal | None:
        return self.cache.get(f"ask:{instrument.symbol}")

    def funding_rate(self, instrument: Instrument) -> Decimal:
        """最新资金费率，未知时返回 0。"""
        return self.cache.get(f"funding:{instrument.symbol}") or Decimal(0)

    def prices_window(self, instrument: Instrument,
                      n: int) -> list[Decimal]:
        """
        最近 n 根 K 线收盘价（最旧 → 最新）。
        由 SignalService 在每个 TradeEvent 时追加维护。
        最多保留 500 根，足够大多数技术指标计算。
        """
        window: list[Decimal] = self.cache.get(
            f"prices_window:{instrument.symbol}") or []
        return window[-n:] if len(window) >= n else []

    # ── 账号 ──────────────────────────────────────────────────────────────────

    def position(self, instrument: Instrument) -> Position | None:
        """当前虚拟仓位快照（系统记录，非交易所实际仓位）。"""
        return self.account.get_position(self.account_id, instrument.symbol)

    def nav_usdt(self) -> Decimal:
        """账户净值（USDT 计）。"""
        return self.account.get_nav_usdt(self.account_id)
