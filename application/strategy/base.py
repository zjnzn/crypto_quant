"""
application/strategy/base.py

策略插件协议。满足此协议的任何类都可被系统加载，无需继承基类。
通过 PluginRegistry 注册，通过 PluginLoader 动态加载。

策略只返回 list[Signal]，不直接访问总线，不知道自己运行在回测还是实盘中。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.domain.signal import Signal

if TYPE_CHECKING:
    from application.context import StrategyContext
    from application.events import (BookEvent, FillEvent,
                                    FundingRateEvent, TradeEvent)


@runtime_checkable
class Strategy(Protocol):
    """
    策略插件接口（结构化子类型）。

    实现示例：
        class MyStrategy:
            name    = "my_strategy"
            version = "1.0.0"

            def on_trade(self, event, ctx) -> list[Signal]:
                price = event.price
                # ... 计算逻辑 ...
                return [Signal(instrument=event.instrument, score=0.5)]

            def on_book(self, event, ctx):    return []
            def on_funding(self, event, ctx): return []
            def on_fill(self, event, ctx):    pass
            def on_start(self, ctx):          pass
            def on_stop(self, ctx):           pass
    """
    name:    str   # 全局唯一标识符，用于注册和日志
    version: str   # SemVer，用于兼容性检查

    def on_trade(self, event: "TradeEvent",
                 ctx: "StrategyContext") -> list[Signal]:
        """每笔逐笔成交时触发。高频行情的主要入口。"""
        ...

    def on_book(self, event: "BookEvent",
                ctx: "StrategyContext") -> list[Signal]:
        """盘口更新时触发。适合做市策略。"""
        ...

    def on_funding(self, event: "FundingRateEvent",
                   ctx: "StrategyContext") -> list[Signal]:
        """资金费率更新时触发。永续合约套利策略的入口。"""
        ...

    def on_fill(self, event: "FillEvent",
                ctx: "StrategyContext") -> None:
        """成交回报。用于更新策略内部状态（如持仓跟踪）。"""
        ...

    def on_start(self, ctx: "StrategyContext") -> None:
        """Engine 启动时调用一次。用于初始化内部状态。"""
        ...

    def on_stop(self, ctx: "StrategyContext") -> None:
        """Engine 停止时调用。用于保存状态、释放资源。"""
        ...


class BaseStrategy:
    """策略基类，提供所有接口方法的默认 no-op 实现。

    子类只需覆盖感兴趣的方法，其余自动返回空列表 / 无操作。
    """

    name: str = ""
    version: str = ""

    def on_trade(self, event: "TradeEvent",
                 ctx: "StrategyContext") -> list[Signal]:
        return []

    def on_book(self, event: "BookEvent",
                ctx: "StrategyContext") -> list[Signal]:
        return []

    def on_funding(self, event: "FundingRateEvent",
                   ctx: "StrategyContext") -> list[Signal]:
        return []

    def on_fill(self, event: "FillEvent",
                ctx: "StrategyContext") -> None:
        pass

    def on_start(self, ctx: "StrategyContext") -> None:
        pass

    def on_stop(self, ctx: "StrategyContext") -> None:
        pass
