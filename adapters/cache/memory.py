"""
adapters/cache/memory.py  —  线程安全的内存缓存

零外部依赖，支持 TTL。用于回测和本地开发。
生产环境替换为 RedisCache，接口完全相同。
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any


class MemoryCache:
    """满足 CachePort 协议（结构化子类型，无需继承）。线程安全。"""

    def __init__(self) -> None:
        self._data:   dict[str, Any]   = {}
        self._expiry: dict[str, float] = {}  # key → Unix timestamp
        self._lock    = threading.RLock()

    # ── CachePort 接口 ────────────────────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        with self._lock:
            if self._is_expired(key):
                self._evict(key)
                return None
            return self._data.get(key)

    def set(self, key: str, value: Any,
            ttl: timedelta | None = None) -> None:
        with self._lock:
            self._data[key] = value
            if ttl is not None:
                self._expiry[key] = time.monotonic() + ttl.total_seconds()
            else:
                self._expiry.pop(key, None)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    def exists(self, key: str) -> bool:
        with self._lock:
            if self._is_expired(key):
                self._evict(key)
                return False
            return key in self._data

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    def _is_expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        return exp is not None and time.monotonic() > exp

    def _evict(self, key: str) -> None:
        self._data.pop(key, None)
        self._expiry.pop(key, None)

    # ── 测试 / 调试辅助 ───────────────────────────────────────────────────────

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._expiry.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._data)

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]
