"""
core/domain/signal.py

策略产生的交易信号。策略返回 list[Signal]，
SignalService 将其转换为 SignalEvent 并发布到总线。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.domain.instrument import Instrument


@dataclass
class Signal:
    """
    交易信号值对象（可变，不需要 hashable）。

    score:      [-1.0, 1.0]，正数做多，负数做空，0 平仓
    confidence: [0.0, 1.0]，策略对该信号的置信度
    meta:       策略自定义字段（因子值、模型输出等）
    """
    instrument:  Instrument
    score:       float
    confidence:  float           = 1.0
    strategy_id: str             = ""
    meta:        dict            = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not -1.0 <= self.score <= 1.0:
            raise ValueError(f"score 必须在 [-1, 1]，得到 {self.score}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence 必须在 [0, 1]，得到 {self.confidence}")

    @property
    def is_long(self) -> bool:
        return self.score > 0

    @property
    def is_short(self) -> bool:
        return self.score < 0

    @property
    def is_flat(self) -> bool:
        return self.score == 0

    def __repr__(self) -> str:
        direction = "LONG" if self.is_long else ("SHORT" if self.is_short else "FLAT")
        return (f"Signal({self.instrument.symbol} "
                f"{direction} score={self.score:.3f} "
                f"conf={self.confidence:.2f})")
