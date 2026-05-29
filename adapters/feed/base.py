"""WebSocket feed 基类 — 提供队列 + 线程 + 重连骨架。"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.domain import Event
    from core.ports import EventBusPort

log = logging.getLogger(__name__)


class _QueueWsFeed:
    """基于队列的 WebSocket feed 基类。

    子类只需实现解析方法 (parser)，基类负责:
    - queue + _running 生命周期管理
    - _ws_worker 线程骨架（on_message / on_error / on_close / 重连）
    - drain：从队列取事件并发布到 bus
    - stop：优雅关闭
    """

    def __init__(self, bus: EventBusPort, *, queue_size: int = 10_000) -> None:
        self._bus = bus
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=queue_size)
        self._running = False

    # ── 生命周期 ──────────────────────────────────────

    def stop(self) -> None:
        self._running = False

    def drain(self, timeout: float = 0.05) -> int:
        """将队列中所有事件发布到 bus，返回发布数量。"""
        count = 0
        while True:
            try:
                event = self._queue.get(timeout=timeout)
                self._bus.publish(event)
                count += 1
            except queue.Empty:
                break
        return count

    # ── WebSocket 工作线程 ────────────────────────────

    def _ws_worker(
        self,
        url: str,
        parser: Callable[[str], Event | None],
        *,
        ping_interval: float = 30,
        ping_timeout: float = 10,
    ) -> None:
        """WebSocket 工作线程模板。

        Parameters
        ----------
        url : WebSocket 连接地址
        parser : 消息解析函数，返回 Event 或 None（丢弃）
        ping_interval / ping_timeout : 透传给 run_forever
        """
        import websocket  # late import — 可能在无 GUI 环境下不可用

        def on_message(_ws, msg: str) -> None:
            try:
                event = parser(msg)
                if event is not None:
                    self._queue.put_nowait(event)
            except queue.Full:
                log.warning("WS 队列已满，丢弃消息")
            except Exception:
                log.exception("WS 解析异常")

        def on_error(_ws, err) -> None:
            log.error("WS 错误: %s", err)

        def on_close(_ws, code, msg) -> None:
            log.warning("WS 断开: %s %s", code, msg)
            if self._running:
                log.info("尝试重连...")
                time.sleep(3)
                self._ws_worker(
                    url, parser,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                )

        ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(ping_interval=ping_interval, ping_timeout=ping_timeout)
