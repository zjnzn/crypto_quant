"""
adapters/feed/csv_.py

CSV 行情数据源（回测专用）。

CSV 格式（必须包含以下列）：
  timestamp  — ISO 8601，如 "2024-01-01T00:00:00Z"
  symbol     — 与 instruments 字典的 key 一致，如 "BTC-USDT-PERP"
  open, high, low, close — OHLC 价格
  volume     — 成交量（可选，缺省为 0）

每行发布：
  - TradeEvent（使用 close 价格）
  - BookEvent（bid=close-tick, ask=close+tick）

同时推进 SimClock 到该行时间戳。
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from typing import Callable, Protocol

from core.domain.instrument import Instrument
from core.ports.bus import EventBusPort
from application.events import BookEvent, FundingRateEvent, TradeEvent

log = logging.getLogger(__name__)


class _AdvancableClock(Protocol):
    """
    CsvFeed 需要的最小时钟接口（私有协议，不对外暴露）。
    env/backtest.py 中的 SimClock 满足此协议（结构化子类型）。
    """
    def advance(self, ts: datetime) -> None: ...


class CsvFeed:
    """
    从 CSV 文件逐行推送行情事件。

    用法：
        clock = SimClock()
        feed  = CsvFeed(
            bus=bus,
            clock=clock,
            path="data/btc_1h.csv",
            instruments={"BTC-USDT-PERP": btc_perp},
        )
        feed.run()   # 阻塞直到 CSV 读完

    支持 TextIO 作为 path 参数（方便测试中传入 StringIO）。
    """

    def __init__(
        self,
        bus:              EventBusPort,
        clock:            _AdvancableClock,
        path:             str | Path | TextIO,
        instruments:      dict[str, Instrument],
        emit_book:        bool = True,
        funding_rate_fn:  "Callable[[datetime], Decimal] | None" = None,
    ) -> None:
        self._bus              = bus
        self._clock            = clock
        self._path             = path
        self._instruments      = instruments
        self._emit_book        = emit_book
        self._funding_rate_fn  = funding_rate_fn
        self._last_funding_ts: dict[str, datetime | None] = {}
        self._bars_read        = 0

    def run(self) -> int:
        """
        读取并发布所有行情数据。
        返回总共发布的 TradeEvent 数量。
        """
        self._bars_read = 0

        if hasattr(self._path, "read"):          # TextIO (StringIO)
            self._process(self._path)
        else:
            with open(self._path, encoding="utf-8") as f:
                self._process(f)

        log.info("CsvFeed 完成：共推送 %d 根 K 线", self._bars_read)
        return self._bars_read

    def _process(self, f: TextIO) -> None:
        for row in csv.DictReader(f):
            self._emit_row(row)

    def _emit_row(self, row: dict[str, str]) -> None:
        sym = row.get("symbol", "").strip()
        instrument = self._instruments.get(sym)
        if instrument is None:
            return   # 未注册的标的，跳过

        # ── 推进时钟 ──────────────────────────────────────────────────────────
        ts_str = row.get("timestamp", row.get("ts", "")).strip()
        try:
            ts = datetime.fromisoformat(ts_str.rstrip("Z"))
        except ValueError:
            log.warning("无效时间戳格式: %s，跳过", ts_str)
            return
        self._clock.advance(ts)

        # ── 解析价格 ──────────────────────────────────────────────────────────
        try:
            close  = Decimal(row["close"])
            volume = Decimal(row.get("volume", "0") or "0")
        except Exception:
            log.warning("价格解析失败，行: %s，跳过", row)
            return

        # ── 发布 TradeEvent ────────────────────────────────────────────────────
        self._bus.publish(TradeEvent(
            ts=ts,
            instrument=instrument,
            price=close,
            qty=volume,
            buyer_maker=False,
        ))

        # ── 发布 BookEvent（模拟最优盘口）────────────────────────────────────
        if self._emit_book:
            half = instrument.tick_size
            self._bus.publish(BookEvent(
                ts=ts,
                instrument=instrument,
                bid_price=close - half,
                bid_qty=Decimal("1"),
                ask_price=close + half,
                ask_qty=Decimal("1"),
            ))

        # ── 资金费率（每 8 小时结算一次）────────────────────────────────────
        if self._funding_rate_fn is not None:
            self._maybe_emit_funding(ts, instrument)

        self._bars_read += 1

    def _maybe_emit_funding(self, ts: datetime,
                             instrument: Instrument) -> None:
        """每 8 小时在 00:00 / 08:00 / 16:00 UTC 发布一次资金费率。"""
        hour = ts.hour
        if hour % 8 != 0:
            return
        sym = instrument.symbol
        last = self._last_funding_ts.get(sym)
        if last is not None and last.date() == ts.date() and last.hour == hour:
            return   # 同一个 8h 周期已发过
        self._last_funding_ts[sym] = ts
        rate = self._funding_rate_fn(ts)
        self._bus.publish(FundingRateEvent(
            ts=ts,
            instrument=instrument,
            rate=rate,
        ))
