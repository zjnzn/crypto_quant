"""
env/backtest.py

回测运行环境。

SimClock 定义在此处（只依赖 stdlib），满足 ClockPort 协议。
CsvFeed 通过 duck typing 调用 advance()，无需 import。

BacktestEnv 只持有 clock，账号由 container.py 中的 AccountService 管理。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


class SimClock:
    """
    模拟时钟。由 CsvFeed 每行数据推进一次。

    满足 ClockPort（有 now()），同时提供 advance() 供 CsvFeed 调用。
    """
    def __init__(self) -> None:
        self._ts = datetime(2000, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._ts

    def advance(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts > self._ts:
            self._ts = ts


class BacktestEnv:
    """回测环境：持有 SimClock。"""
    mode = "backtest"

    def __init__(self) -> None:
        self.clock = SimClock()
