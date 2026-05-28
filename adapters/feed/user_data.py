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

from core.domain.instrument import Instrument
from core.domain.order import Side
from core.ports.bus import EventBusPort
from application.events import FillEvent

if TYPE_CHECKING:
    from adapters.exchange.binance import BinanceFuturesExchange

log = logging.getLogger(__name__)

# User data stream WS base（2026-04 起迁移到 /private 入口）
_WS_LIVE = "wss://fstream.binance.com/private/ws"
_WS_TEST = "wss://stream.testnet.binance.vision/private/ws"

# listenKey 续期间隔（秒），Binance 要求 < 60 分钟
_KEEPALIVE_INTERVAL = 1800   # 30 分钟


class BinanceUserDataStream:
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
        self._bus         = bus
        self._exchange    = exchange
        self._instruments = {k.lower(): v for k, v in instruments.items()}
        self._ws_base     = _WS_TEST if testnet else _WS_LIVE
        self._listen_key: str | None = None
        self._running     = False
        self._queue:      queue.Queue = queue.Queue(maxsize=1_000)
        self._ws_thread:  threading.Thread | None = None
        self._ka_thread:  threading.Thread | None = None

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        1. 获取 listenKey
        2. 启动 WebSocket 后台线程
        3. 启动续期后台线程
        """
        try:
            self._listen_key = self._exchange.create_listen_key()
        except Exception as e:
            log.error("无法获取 listenKey，用户数据流启动失败: %s", e)
            return

        self._running = True
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

    def stop(self) -> None:
        self._running = False
        if self._listen_key:
            self._exchange.close_listen_key(self._listen_key)
        log.info("BinanceUserDataStream 停止")

    def drain(self, timeout: float = 0.05) -> int:
        """
        从队列中取出所有待处理的 FillEvent 并发布到总线。
        应在主线程的主循环中定期调用（与 BinanceWsFeed.run() 配合）。
        timeout: 单次 queue.get 的超时秒数。
        返回本轮处理的 FillEvent 数量。
        """
        count = 0
        while True:
            try:
                event = self._queue.get(timeout=timeout)
                self._bus.publish(event)
                count += 1
            except queue.Empty:
                break
        return count

    # ── WebSocket 工作线程 ────────────────────────────────────────────────────

    def _ws_worker(self, url: str) -> None:
        def on_message(ws, raw):
            try:
                self._handle(raw)
            except Exception:
                log.exception("处理用户数据流消息失败")

        def on_error(ws, err):
            log.error("用户数据流 WS 错误: %s", err)

        def on_close(ws, code, msg):
            log.warning("用户数据流 WS 断开: %s %s", code, msg)
            if self._running:
                log.info("用户数据流重连...")
                time.sleep(3)
                self._ws_worker(url)

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
        """每 30 分钟续期 listenKey。"""
        while self._running:
            time.sleep(_KEEPALIVE_INTERVAL)
            if not self._running or not self._listen_key:
                break
            try:
                self._exchange.keepalive_listen_key(self._listen_key)
                log.debug("listenKey 续期成功")
            except Exception as e:
                log.error("listenKey 续期失败: %s，尝试重新获取...", e)
                try:
                    self._listen_key = self._exchange.create_listen_key()
                    log.info("listenKey 已重新获取")
                except Exception as e2:
                    log.error("重新获取 listenKey 失败: %s", e2)

    # ── 消息解析 ──────────────────────────────────────────────────────────────

    def _handle(self, raw: str) -> None:
        """
        解析 Binance 用户数据流消息。

        ORDER_TRADE_UPDATE 事件字段（关键字段）：
          o.i   — orderId（exchange_order_id）
          o.S   — side: BUY | SELL
          o.X   — status: NEW / PARTIALLY_FILLED / FILLED / CANCELED ...
          o.l   — lastFilledQty（本次成交量）
          o.L   — lastFilledPrice（本次成交价）
          o.n   — commission（手续费金额）
          o.N   — commissionAsset（手续费币种）
          o.s   — symbol（如 ETHUSDT）
        """
        data = json.loads(raw)
        event_type = data.get("e")

        if event_type == "ORDER_TRADE_UPDATE":
            o      = data.get("o", {})
            status = o.get("X", "")

            if status not in ("FILLED", "PARTIALLY_FILLED"):
                return   # 只处理成交事件

            filled_qty = Decimal(str(o.get("l", "0")))
            fill_price = Decimal(str(o.get("L", "0")))
            if filled_qty <= 0 or fill_price <= 0:
                return

            sym_lower  = o.get("s", "").lower()
            instrument = self._instruments.get(sym_lower)
            if instrument is None:
                log.warning("收到未注册标的的成交: %s", o.get("s"))
                return

            exchange_order_id = str(o.get("i", ""))
            side       = Side.BUY if o.get("S") == "BUY" else Side.SELL
            commission = Decimal(str(o.get("n", "0")))
            comm_asset = o.get("N", instrument.quote)
            is_maker   = (o.get("m", False))  # maker = True

            log.info("用户数据流成交  %s %s qty=%.4f @ %.4f  fee=%.6f %s",
                     side.value, instrument.symbol,
                     filled_qty, fill_price, commission, comm_asset)

            try:
                self._queue.put_nowait(FillEvent(
                    exchange_order_id = exchange_order_id,
                    instrument        = instrument,
                    side              = side,
                    filled_qty        = filled_qty,
                    fill_price        = fill_price,
                    commission        = commission,
                    commission_asset  = comm_asset,
                    is_maker          = bool(is_maker),
                ))
            except queue.Full:
                log.warning("用户数据流 queue 已满，丢弃 FillEvent")

        elif event_type == "ACCOUNT_UPDATE":
            # 可选：同步账户余额变化
            log.debug("账户余额更新事件（已忽略，通过对账同步）")

        elif event_type in ("listenKeyExpired",):
            log.warning("listenKey 已过期，尝试重新获取...")
            try:
                self._listen_key = self._exchange.create_listen_key()
            except Exception as e:
                log.error("重新获取 listenKey 失败: %s", e)