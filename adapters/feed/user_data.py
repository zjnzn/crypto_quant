"""
adapters/feed/user_data.py

Binance 用户数据流（User Data Stream）。

职责：
  订阅交易所的私有 WebSocket 流，当真实订单成交时，
  将 FillEvent 放入 queue，由主线程从 queue 取出并发布到事件总线。

设计要点（与 BinanceWsFeed 一致）：
  - 后台线程建立 WS 连接，将原始消息解析后放入 queue.Queue
  - 主线程调用 drain() 从 queue 取出并 bus.publish（保证总线线程安全）
  - 避免后台线程直接 bus.publish 导致竞态条件

协议：
  1. POST /fapi/v1/listenKey  → 获取一次性 key
  2. wss://.../ws/{listenKey} → 建立私有流
  3. 每 30 分钟 PUT /fapi/v1/listenKey 续期
  4. 解析 ORDER_TRADE_UPDATE 消息 → FillEvent

依赖：pip install websocket-client
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from adapters.feed.base import _QueueWsFeed
from application.events import (
    BalanceUpdatedEvent, BookEvent, FillEvent,
    FundingRateEvent, OrderCanceledEvent,
    OrderCreatedEvent, TradeEvent,
)
from core.domain.balance import Balance
from core.domain.instrument import Instrument
from core.domain.order import Side
from core.ports.bus import EventBusPort

if TYPE_CHECKING:
    from adapters.exchange.binance import BinanceFuturesExchange

log = logging.getLogger(__name__)

# User data stream WS base（2026-04 起迁移到 /private 入口）
_WS_LIVE = "wss://fstream.binance.com/private/ws"
_WS_TEST = "wss://stream.testnet.binance.vision/private/ws"

# listenKey 续期间隔（秒），Binance 要求 < 60 分钟
_KEEPALIVE_INTERVAL = 1800   # 30 分钟

# WebSocket 最大连接时长（秒），Binance 强制 24 小时断开
_MAX_CONNECTION_TIME = 82800  # 23 小时（提前 1 小时重建）


class BinanceUserDataStream(_QueueWsFeed):
    """
    订阅 Binance 用户数据流，接收实盘订单成交回报。

    用法：
        instruments = {"ethusdt": eth_perp, ...}
        user_stream = BinanceUserDataStream(
            bus=system.bus,
            exchange=binance_adapter,
            instruments=instruments,
            testnet=True,
        )
        user_stream.start()   # 启动后台 WS + 续期线程
        # 主循环中定期调用 user_stream.drain() 发布成交事件

    停止：
        user_stream.stop()
    """

    def __init__(
        self,
        bus:         EventBusPort,
        exchange:    "BinanceFuturesExchange",
        instruments: dict[str, Instrument],   # binance_symbol_lower → Instrument
        testnet:     bool = False,
    ) -> None:
        super().__init__(bus, queue_size=1_000)
        self._exchange    = exchange
        self._instruments = {k.lower(): v for k, v in instruments.items()}
        self._ws_base     = _WS_TEST if testnet else _WS_LIVE
        self._listen_key: str | None = None
        self._ws_thread:  threading.Thread | None = None
        self._ka_thread:  threading.Thread | None = None
        self._connection_start_time: float = 0  # 连接建立时间（用于 24h 重建）

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        1. 获取 listenKey
        2. 启动 WebSocket 后台线程
        3. 启动续期后台线程
        4. 启动 24 小时重建监控线程
        """
        try:
            self._listen_key = self._exchange.create_listen_key()
        except Exception as e:
            log.error("无法获取 listenKey，用户数据流启动失败: %s", e)
            return

        self._running = True
        self._connection_start_time = time.time()
        url = f"{self._ws_base}?listenKey={self._listen_key}&events=ORDER_TRADE_UPDATE"
        log.info("BinanceUserDataStream 启动  url=%s", url)

        self._ws_thread = threading.Thread(
            target=self._ws_worker, args=(url,),
            name="binance-user-data", daemon=True,
        )
        self._ws_thread.start()

        self._ka_thread = threading.Thread(
            target=self._keepalive_worker,
            name="binance-keepalive", daemon=True,
        )
        self._ka_thread.start()

        self._rebuild_thread = threading.Thread(
            target=self._rebuild_monitor,
            name="binance-rebuild", daemon=True,
        )
        self._rebuild_thread.start()

    def stop(self) -> None:
        super().stop()
        if self._listen_key:
            self._exchange.close_listen_key(self._listen_key)
        log.info("BinanceUserDataStream 停止")

    # ── WebSocket 工作线程 ────────────────────────────────────────────────────

    def _ws_worker(self, url: str, retry_count: int = 0) -> None:
        """WebSocket 工作线程，支持指数退避重连。"""
        max_retries = 5
        base_delay = 1.0  # 基础延迟秒数

        def on_message(ws, raw):
            try:
                self._handle(raw)
            except Exception:
                log.exception("处理用户数据流消息失败")

        def on_error(ws, err):
            log.error("用户数据流 WS 错误: %s", err)

        def on_close(ws, code, msg):
            log.warning("用户数据流 WS 断开: code=%s msg=%s", code, msg)

            if not self._running:
                return

            if retry_count >= max_retries:
                log.error("重连失败次数过多（%d 次），停止重连", max_retries)
                self._running = False
                return

            # 指数退避
            delay = min(base_delay * (2 ** retry_count), 60.0)
            log.info("将在 %.1fs 后重连（第 %d 次）...", delay, retry_count + 1)
            time.sleep(delay)

            # 重新获取 listenKey
            try:
                self._listen_key = self._exchange.create_listen_key()
                new_url = f"{self._ws_base}?listenKey={self._listen_key}"
                self._ws_worker(new_url, retry_count + 1)
            except Exception as e:
                log.error("重连失败: %s", e)
                self._running = False

        try:
            import websocket
            ws = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except ImportError:
            raise ImportError("需要安装 websocket-client：pip install websocket-client")

    def _keepalive_worker(self) -> None:
        """每 30 分钟续期 listenKey，含过期检测。"""
        last_renewal = time.time()

        while self._running:
            time.sleep(_KEEPALIVE_INTERVAL)

            if not self._running or not self._listen_key:
                break

            # 检查是否接近过期（50 分钟）
            elapsed = time.time() - last_renewal
            if elapsed > 3000:  # 50 分钟
                log.warning("listenKey 接近过期（%.0fs），强制续期", elapsed)

            try:
                self._exchange.keepalive_listen_key(self._listen_key)
                last_renewal = time.time()
                log.debug("listenKey 续期成功")
            except Exception as e:
                log.error("listenKey 续期失败: %s，尝试重新获取...", e)
                try:
                    self._listen_key = self._exchange.create_listen_key()
                    last_renewal = time.time()
                    log.info("listenKey 已重新获取")
                except Exception as e2:
                    log.error("重新获取 listenKey 失败: %s", e2)

    def _rebuild_monitor(self) -> None:
        """监控连接时长，在 23 小时后主动重建连接。"""
        check_interval = 3600  # 每小时检查一次

        while self._running:
            time.sleep(check_interval)

            if not self._running:
                break

            elapsed = time.time() - self._connection_start_time
            if elapsed >= _MAX_CONNECTION_TIME:
                log.warning("WebSocket 连接已运行 %.1f 小时，主动重建连接...", elapsed / 3600)
                # 通知 WS 线程关闭（触发 on_close 重连）
                self._running = False
                # 等待线程结束
                if self._ws_thread and self._ws_thread.is_alive():
                    self._ws_thread.join(timeout=5)
                # 重新启动
                self._running = True
                self._connection_start_time = time.time()
                try:
                    self._listen_key = self._exchange.create_listen_key()
                    url = f"{self._ws_base}?listenKey={self._listen_key}&events=ORDER_TRADE_UPDATE"
                    self._ws_thread = threading.Thread(
                        target=self._ws_worker, args=(url,),
                        name="binance-user-data", daemon=True,
                    )
                    self._ws_thread.start()
                    log.info("WebSocket 连接已重建")
                except Exception as e:
                    log.error("重建连接失败: %s", e)

    # ── 消息解析 ──────────────────────────────────────────────────────────────

    def _handle(self, raw: str) -> None:
        """解析用户数据流事件。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("用户数据流 JSON 解析失败: %s", raw[:200])
            return

        event_type = data.get("e")

        # ── ORDER_TRADE_UPDATE ───────────────────────────────────────────
        if event_type == "ORDER_TRADE_UPDATE":
            o = data.get("o", {})
            execution_type = o.get("x", "")
            symbol_raw = o.get("s", "")
            instrument = self._instruments.get(symbol_raw.lower())

            if execution_type == "NEW":
                # 订单创建确认
                self._bus.publish(OrderCreatedEvent(
                    exchange_order_id=str(o.get("i", "")),
                    instrument=instrument,
                    side=Side.BUY if o.get("S") == "BUY" else Side.SELL,
                    order_type=o.get("o", ""),
                    client_order_id=o.get("c", ""),
                ))
                log.debug("订单创建  orderId=%s  clientOid=%s  type=%s",
                          o.get("i"), o.get("c"), o.get("o"))
                return

            if execution_type == "CANCELED":
                # 订单撤销确认
                self._bus.publish(OrderCanceledEvent(
                    exchange_order_id=str(o.get("i", "")),
                    instrument=instrument,
                    client_order_id=o.get("c", ""),
                ))
                log.debug("订单撤销  orderId=%s", o.get("i"))
                return

            if execution_type not in ("FILLED", "TRADE", "PARTIALLY_FILLED"):
                return

            # 成交回报
            filled_qty = Decimal(str(o.get("l", "0")))
            fill_price = Decimal(str(o.get("L", "0")))
            if filled_qty == 0 or fill_price == 0:
                return

            self._queue.put_nowait(FillEvent(
                exchange_order_id=str(o.get("i", "")),
                instrument=instrument,
                side=Side.BUY if o.get("S") == "BUY" else Side.SELL,
                filled_qty=filled_qty,
                fill_price=fill_price,
                commission=Decimal(str(o.get("n", "0"))),
                commission_asset=o.get("N", "USDT"),
                is_maker=bool(o.get("m", False)),
                realized_pnl=Decimal(str(o.get("r", "0"))),
            ))
            return

        # ── ACCOUNT_UPDATE（余额/仓位变动）─────────────────────────────────
        if event_type == "ACCOUNT_UPDATE":
            accts = data.get("a", {})
            # 余额更新
            for b in accts.get("B", []):
                asset = b.get("a", "")
                if asset == "USDT":
                    self._bus.publish(BalanceUpdatedEvent(
                        balance=Balance(
                            asset=asset,
                            free=Decimal(str(b.get("f", "0"))),
                            used=Decimal(str(b.get("l", "0"))),
                        ),
                    ))
            return

        # ── MARGIN CALL ──────────────────────────────────────────────────
        if event_type == "MARGIN_CALL":
            log.warning("收到 Binance MARGIN_CALL: %s", raw[:500])
            return