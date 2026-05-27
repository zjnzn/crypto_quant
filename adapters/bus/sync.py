"""
adapters/bus/sync.py  —  同步事件总线

单线程、同进程。publish() 直接调用所有 handler，按注册顺序执行。
- Phase 1（回测/开发）：直接使用
- Phase 2（生产）：替换为 KafkaBus，上层代码零改动

特性：
  - 单个 handler 异常不阻断后续 handler
  - handler 注册/注销线程安全（使用 list copy）
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Type

from core.domain.event import Event

log = logging.getLogger(__name__)

Handler = Callable[[Event], None]


class SyncEventBus:
    """
    满足 EventBusPort 协议（结构化子类型，无需继承）。
    """

    def __init__(self) -> None:
        self._handlers: dict[Type[Event], list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: Type[Event], handler: Handler) -> None:
        self._handlers[event_type].append(handler)
        log.debug("subscribe  %s → %s", event_type.__name__,
                  handler.__qualname__)

    def unsubscribe(self, event_type: Type[Event], handler: Handler) -> None:
        try:
            self._handlers[event_type].remove(handler)
        except ValueError:
            pass

    def publish(self, event: Event) -> None:
        handlers = list(self._handlers.get(type(event), []))
        log.debug("publish %s  (corr=%s)  → %d handler(s)",
                  event.type_name, event.correlation_id[:8], len(handlers))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                log.exception(
                    "handler %s raised on %s (event_id=%s)",
                    handler.__qualname__, event.type_name, event.event_id
                )
                # 继续执行其他 handler，不因单个失败而中断整条链

    def subscriber_count(self, event_type: Type[Event]) -> int:
        """测试辅助：返回某事件类型的订阅数量。"""
        return len(self._handlers.get(event_type, []))
