"""
core/ports/bus.py  —  消息总线端口

定义边界：application/ 通过此接口通信，不知道是 SyncBus 还是 KafkaBus。
"""
from __future__ import annotations

from typing import Callable, Protocol, Type

from core.domain.event import Event

Handler = Callable[[Event], None]


class EventBusPort(Protocol):
    def subscribe(self, event_type: Type[Event], handler: Handler) -> None: ...
    def unsubscribe(self, event_type: Type[Event], handler: Handler) -> None: ...
    def publish(self, event: Event) -> None: ...
