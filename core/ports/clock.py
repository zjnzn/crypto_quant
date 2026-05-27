"""
core/ports/clock.py  —  时钟端口

回测使用模拟时钟（由数据驱动），实盘使用系统时钟。
策略通过 StrategyContext.now() 获取当前时间，不直接调用 datetime.now()。
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime: ...
