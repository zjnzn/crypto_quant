"""Binance USDT-M Futures WebSocket 行情适配器。"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import TYPE_CHECKING

from adapters.feed.base import _QueueWsFeed
from core.domain import Instrument, Side

if TYPE_CHECKING:
    from adapters.feed.user_data import BinanceUserDataStream
    from core.ports import EventBusPort

log = logging.getLogger(__name__)


class BinanceWsFeed(_QueueWsFeed):
    """Binance U本位合约 WebSocket 行情推送。

    按 Binance 2026-04 新规则分流:
      bookTicker → /public/stream
      aggTrade   → /market/stream
    """

    WS_BASE_FUTURES_LIVE = "wss://fstream.binance.com"
    WS_BASE_FUTURES_TEST = "wss://stream.testnet.binance.vision"
    WS_BASE_SPOT_LIVE = "wss://stream.binance.com:9443"
    WS_BASE_SPOT_TEST = "wss://testnet.binance.vision"

    def __init__(
        self,
        bus: EventBusPort,
        instruments: dict[str, Instrument],
        *,
        market_type: str = "futures",
        testnet: bool = False,
        user_data_stream: BinanceUserDataStream | None = None,
    ) -> None:
        super().__init__(bus, queue_size=10_000)
        self._instruments = {k.lower(): v for k, v in instruments.items()}
        self._market_type = market_type
        self._testnet = testnet
        self._uds = user_data_stream

        if market_type == "futures":
            self._ws_base = (
                self.WS_BASE_FUTURES_TEST if testnet
                else self.WS_BASE_FUTURES_LIVE
            )
        else:
            self._ws_base = (
                self.WS_BASE_SPOT_TEST if testnet
                else self.WS_BASE_SPOT_LIVE
            )

    # ── 启动 ─────────────────────────────────────────

    def start(self) -> None:
        """启动后台 WebSocket 线程（按 public/market 分流各一个连接）。"""
        if self._running:
            return
        self._running = True
        urls = self._build_urls()
        for i, url in enumerate(urls):
            log.info("BinanceWsFeed 启动 [%d/%d] url=%s", i + 1, len(urls), url)
            t = threading.Thread(
                target=self._ws_worker,
                args=(url, self._parse),
                daemon=True,
                name=f"binance-ws-{i}",
            )
            t.start()

    def run(self) -> None:
        """主循环：排空队列 + 用户数据流。"""
        while self._running:
            self.drain(timeout=0.05)
            if self._uds is not None:
                self._uds.drain(timeout=0.02)

    # ── URL 构建 ─────────────────────────────────────

    def _build_urls(self) -> list[str]:
        """按 Binance 新 WebSocket 分流规则构建 URL：
        bookTicker → /public，aggTrade → /market"""
        symbols = list(self._instruments.keys())
        urls: list[str] = []

        public_streams = "/".join(f"{s}@bookTicker" for s in symbols)
        if public_streams:
            urls.append(f"{self._ws_base}/public/stream?streams={public_streams}")

        market_streams = "/".join(f"{s}@aggTrade" for s in symbols)
        if market_streams:
            urls.append(f"{self._ws_base}/market/stream?streams={market_streams}")

        return urls

    # ── 消息解析 ─────────────────────────────────────

    def _parse(self, raw: str) -> object | None:
        """解析 WebSocket 消息，返回 Event 或 None。"""
        import json

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("非 JSON 消息: %s", raw[:120])
            return None

        stream: str | None = payload.get("stream", "")
        data = payload.get("data", payload)

        if not stream:
            return None

        sym = stream.split("@")[0].upper()
        instrument = self._instruments.get(sym)
        if instrument is None:
            return None

        if "@bookTicker" in stream:
            return self._parse_book(instrument, data)
        if "@aggTrade" in stream:
            return self._parse_trade(instrument, data)

        log.debug("未处理的 stream 类型: %s", stream)
        return None

    def _parse_book(self, instrument: Instrument, d: dict) -> object:
        from application.events import BookEvent

        bid = Decimal(str(d.get("b", "0")))
        ask = Decimal(str(d.get("a", "0")))
        return BookEvent(
            instrument=instrument,
            bid=bid,
            ask=ask,
        )

    def _parse_trade(self, instrument: Instrument, d: dict) -> object:
        from application.events import TradeEvent

        price = Decimal(str(d.get("p", "0")))
        qty = Decimal(str(d.get("q", "0")))
        side = Side.BUY if d.get("m") is False else Side.SELL
        return TradeEvent(
            instrument=instrument,
            price=price,
            quantity=qty,
            side=side,
        )
