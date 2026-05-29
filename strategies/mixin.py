"""策略共享 Mixin — 滚动价格缓冲区。"""

from __future__ import annotations

from collections import deque
from decimal import Decimal


class PriceBufferMixin:
    """为策略提供 per-symbol 滚动价格窗口。

    子类在 __init__ 中调用 _init_buffer(window) 即可。
    """

    _prices: dict[str, deque[Decimal]]
    _window: int

    def _init_buffer(self, window: int) -> None:
        self._prices = {}
        self._window = window

    def _ensure_buffer(self, sym: str) -> deque[Decimal]:
        if sym not in self._prices:
            self._prices[sym] = deque(maxlen=self._window)
        return self._prices[sym]

    def _append_price(self, sym: str, price: Decimal) -> None:
        self._ensure_buffer(sym).append(price)

    def _is_warmed_up(self, sym: str) -> bool:
        return len(self._ensure_buffer(sym)) >= self._window

    def _prices_list(self, sym: str) -> list[Decimal]:
        return list(self._ensure_buffer(sym))
