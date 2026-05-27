"""
core/domain/event.py

所有事件的基类。不可变值对象，零外部依赖。

三个 ID 构成完整分布式追踪上下文：
  event_id       — 本事件唯一标识
  correlation_id — 同一业务链路共享（BarEvent → ... → PnLEvent 全相同）
  causation_id   — 直接父事件 ID（可还原完整因果图）
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4


def _new_id() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class Event:
    event_id:       str           = field(default_factory=_new_id)
    correlation_id: str           = field(default_factory=_new_id)
    causation_id:   str | None    = None
    ts:             datetime      = field(default_factory=_utcnow)

    def caused_by(self, parent: Event) -> Event:
        """
        返回一个新事件，继承父事件的 correlation_id 并记录 causation_id。
        所有服务发布下游事件时必须调用此方法。

        示例：
            self._bus.publish(
                SignalEvent(...).caused_by(trade_event)
            )
        """
        return replace(
            self,
            event_id=_new_id(),
            correlation_id=parent.correlation_id,
            causation_id=parent.event_id,
        )

    @property
    def type_name(self) -> str:
        return type(self).__name__
