"""
core/ports/cache.py  —  缓存端口

MemoryCache（开发/回测）和 RedisCache（生产）实现相同接口。
container.py 中换一行配置即可切换，上层代码零改动。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol


class CachePort(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any,
            ttl: timedelta | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
