"""core/ports/oms.py — OMS 查询端口（打破 RiskService ↔ OMSService 循环依赖）"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from core.domain.order import Order


class OMSPort(Protocol):
    def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...
    def compute_pending_delta(self, symbol: str) -> Decimal: ...
