"""
env/live.py

实盘运行环境。使用系统真实时钟，配合 WebSocket 行情源。

切换方式（config.yaml）：
  mode: live
  execution:
    exchange: binance
    api_key: "..."
    api_secret: "..."
"""
from __future__ import annotations

from datetime import datetime, timezone


class WallClock:
    """真实系统时钟，满足 ClockPort 协议。"""

    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)


class LiveEnv:
    """
    实盘环境。

    clock — WallClock（真实时间）
    mode  — "live"
    """
    mode = "live"

    def __init__(self) -> None:
        self.clock = WallClock()
