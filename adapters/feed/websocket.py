"""
adapters/feed/websocket.py

WebSocket 实时行情源。

设计要点：
  - 后台线程建立 WS 连接，将原始消息放入 queue.Queue
  - 主线程从 queue 取出并发布到 EventBus（保证总线线程安全）
  - ws_factory 参数可注入 Mock，测试时不需要真实网络连接
  - 若设置了 user_data_stream，run() 也会从其 queue 中取 FillEvent

目前支持：Binance USDT-M Futures 的 aggTrade + bookTicker 流

依赖：
  pip install websocket-client   # websocket._core.WebSocketApp
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from adapters.feed.base import _QueueWsFeed
from core.domain.instrument import Instrument
from core.ports.bus import EventBusPort
from application.events import BookEvent, FundingRateEvent, TradeEvent

log = logging.getLogger(__name__)

# 类型别名：工厂函数签名 (url, on_message, on_error, on_close) -> None（阻塞）
WsFactory = Callable[[str, Callable, Callable, Callable], None]


class BinanceWsFeed(_QueueWsFeed):
    """
    Binance 实时行情 WebSocket。

    订阅 aggTrade（逐笔成交）和 bookTicker（最优盘口）流。

    用法：
        feed = BinanceWsFeed(
            bus=bus,
            instruments={"btcusdt": btc_perp, "ethusdt": eth_perp},
            user_data_stream=uds,   # 可选：同时处理 FillEvent
        )
        feed.start()            # 启动后台 WS 线程
        feed.run(n_events=1000) # 主线程处理事件（阻塞）
        feed.stop()
    """

    # Binance USDT-M Futures WebSocket（2026-04 起按 public/market/private 分流）
    WS_BASE_FUTURES_LIVE = "wss://fstream.binance.com"
    WS_BASE_FUTURES_TEST = "wss://stream.testnet.binance.vision"
    # Binance Spot WebSocket
    WS_BASE_SPOT_LIVE = "wss://stream.binance.com:9443"
    WS_BASE_SPOT_TEST = "wss://testnet.binance.vision"

    def __init__(
        self,
        bus:               EventBusPort,
        instruments:       dict[str, Instrument],  # binance_symbol_lower → Instrument
        market_type:       str  = "futures",       # "futures" | "spot"
        testnet:           bool = False,
        ws_factory:        WsFactory | None = None,
        user_data_stream:  Optional[object] = None,  # BinanceUserDataStream
    ) -> None:
        super().__init__(bus, queue_size=10_000)
        self._instruments = {k.lower(): v for k, v in instruments.items()}
        self._ws_factory  = ws_factory or self._default_ws_factory
        self._thread:      threading.Thread | None = None
        self._uds          = user_data_stream
        # 根据 market_type 和 testnet 选取正确的 WebSocket 地址
        if market_type == "spot":
            self._ws_base = self.WS_BASE_SPOT_TEST if testnet else self.WS_BASE_SPOT_LIVE
        else:
            self._ws_base = self.WS_BASE_FUTURES_TEST if testnet else self.WS_BASE_FUTURES_LIVE

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动后台 WebSocket 线程（按 public/market 分流各一个连接）。"""
        if self._running:
            return
        self._running = True
        urls = self._build_urls()
        for i, url in enumerate(urls):
            log.info("BinanceWsFeed 启动 [%d/%d] url=%s", i + 1, len(urls), url)
            t = threading.Thread(
                target=self._ws_worker, args=(url,), daemon=True,
                name=f"binance-ws-{i}",
            )
            t.start()

    def stop(self) -> None:
        """通知主循环停止。"""
        self._running = False
        log.info("BinanceWsFeed 停止")

    def run(self, n_events: int | None = None) -> int:
        """
        主线程阻塞处理事件。
        n_events=None → 无限循环直到 stop() 被调用。
        返回已处理事件数。

        同时处理行情事件和用户数据流的 FillEvent。
        """
        # 启动后台 WS 线程
        self.start()

        # 启动用户数据流（如果有）
        if self._uds is not None:
            self._uds.start()

        count = 0
        while self._running:
            try:
                event = self._queue.get(timeout=0.05)
                self._bus.publish(event)
                count += 1
                if n_events is not None and count >= n_events:
                    break
            except queue.Empty:
                pass

            # 每轮循环也处理用户数据流的 FillEvent（非阻塞）
            if self._uds is not None:
                self._uds.drain(timeout=0.0)

        # 停止用户数据流
        if self._uds is not None:
            self._uds.stop()

        log.info("BinanceWsFeed 处理了 %d 个事件", count)
        return count

    def put_raw(self, raw_msg: str) -> None:
        """
        直接注入原始消息并立即发布到总线（测试专用）。
        绕过后台线程和队列，适合在单元测试中同步验证事件。
        """
        event = self._parse(raw_msg)
        if event:
            self._bus.publish(event)   # 直接发布，不走队列

    # ── 内部 ──────────────────────────────────────────────────────────────────

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

    def _ws_worker(self, url: str) -> None:
        """后台线程：建立 WS 连接，收到消息放入 queue。"""
        def on_message(ws, msg):
            try:
                event = self._parse(msg)
                if event:
                    self._queue.put_nowait(event)
            except queue.Full:
                log.warning("WS queue 已满，丢弃消息")
            except Exception:
                log.exception("解析 WS 消息失败")

        def on_error(ws, err):
            log.error("WS 错误: %s", err)

        def on_close(ws, code, msg):
            log.warning("WS 断开: %s %s", code, msg)
            if self._running:
                log.info("尝试重连...")
                self._ws_worker(url)    # 简单重连策略

        self._ws_factory(url, on_message, on_error, on_close)

    def _parse(self, raw: str) -> TradeEvent | BookEvent | None:
        """
        解析 Binance combined stream 消息。

        aggTrade 格式：
          {"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","s":"BTCUSDT","p":"65000","q":"0.01",...}}

        bookTicker 格式：
          {"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","s":"BTCUSDT","b":"64999.9","a":"65000.1",...}}
        """
        try:
            outer = json.loads(raw)
            data  = outer.get("data", outer)
            event_type = data.get("e", "")
            sym_lower  = data.get("s", "").lower()
            instrument = self._instruments.get(sym_lower)
            if instrument is None:
                return None

            ts = datetime.now(tz=timezone.utc)

            if event_type == "aggTrade":
                return TradeEvent(
                    ts          = ts,
                    instrument  = instrument,
                    price       = Decimal(data["p"]),
                    qty         = Decimal(data["q"]),
                    buyer_maker = bool(data.get("m", False)),
                )
            elif event_type == "bookTicker":
                return BookEvent(
                    ts          = ts,
                    instrument  = instrument,
                    bid_price   = Decimal(data["b"]),
                    bid_qty     = Decimal(data["B"]),
                    ask_price   = Decimal(data["a"]),
                    ask_qty     = Decimal(data["A"]),
                )
        except (KeyError, ValueError):
            log.debug("无法解析 WS 消息: %s", raw[:100])
        return None

    @staticmethod
    def _default_ws_factory(url, on_message, on_error, on_close) -> None:
        """默认 WS 工厂：使用 websocket-client 库。"""
        try:
            import websocket
            ws = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except ImportError:
            raise ImportError(
                "实盘 WebSocket 需要安装 websocket-client：\n"
                "  pip install websocket-client"
            )