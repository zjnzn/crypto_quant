"""
adapters/feed/base.py

WebSocket feed 基类 — 提供两个 Binance WS 适配器共享的生命周期骨架：
  - queue.Queue + _running 标志的初始化
  - stop()：通知主循环停止
  - drain()：从队列取出事件并发布到总线（保证总线线程安全）

各子类自行实现 WebSocket 连接与消息解析（_ws_worker / _parse / _handle），
因为行情流（多 URL + ws_factory 注入）与用户数据流（listenKey 生命周期）
差异较大，强行共用 _ws_worker 反而增加耦合。
"""
from __future__ import annotations

import logging
import queue

from core.ports.bus import EventBusPort

log = logging.getLogger(__name__)


class _QueueWsFeed:
    """基于队列的 WebSocket feed 基类（仅生命周期，不含连接逻辑）。"""

    def __init__(self, bus: EventBusPort, *, queue_size: int = 10_000) -> None:
        self._bus     = bus
        self._queue:   queue.Queue = queue.Queue(maxsize=queue_size)
        self._running  = False

    def stop(self) -> None:
        """通知主循环停止（子类可覆写以追加清理逻辑）。"""
        self._running = False

    def drain(self, timeout: float = 0.05) -> int:
        """
        从队列取出所有待处理事件并发布到总线，返回本轮发布数量。
        应在主线程的主循环中定期调用。
        """
        count = 0
        while True:
            try:
                event = self._queue.get(timeout=timeout)
                self._bus.publish(event)
                count += 1
            except queue.Empty:
                break
        return count
