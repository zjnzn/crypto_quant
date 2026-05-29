"""Binance UserDataStream — 监听订单/账户更新。"""

from __future__ import annotations

import json
import logging
import threading
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from adapters.feed.base import _QueueWsFeed
from core.domain import Side

if TYPE_CHECKING:
    from adapters.exchange.binance import BinanceFuturesExchange
    from core.domain import Instrument
    from core.ports import EventBusPort

log = logging.getLogger(__name__)

# User data stream WS base（2026-04 起迁移到 /private 入口）
_WS_LIVE = "wss://fstream.binance.com/private/ws"
_WS_TEST = "wss://stream.testnet.binance.vision/private/ws"


class BinanceUserDataStream(_QueueWsFeed):
    """Binance U本位合约用户数据流。

    负责:
    - listenKey 创建与续期
    - 接收 ORDER_TRADE_UPDATE → FillEvent
    - listenKey 过期后自动重建连接
    """

    def __init__(
        self,
        bus: EventBusPort,
        exchange: BinanceFuturesExchange,
        instruments: dict[str, Instrument],
        *,
        testnet: bool = False,
    ) -> None:
        super().__init__(bus, queue_size=1_000)
        self._exchange = exchange
        self._instruments = {k.lower(): v for k, v in instruments.items()}
        self._testnet = testnet
        self._ws_base = _WS_TEST if testnet else _WS_LIVE
        self._listen_key: str | None = None
        self._keepalive_thread: threading.Thread | None = None

    # ── 启动 ─────────────────────────────────────────

    def start(self) -> None:
        """创建 listenKey 并启动 WS 连接 + 续期线程。"""
        if self._running:
            return
        self._running = True

        self._listen_key = self._exchange.create_listen_key()
        url = f"{self._ws_base}?listenKey={self._listen_key}&events=ORDER_TRADE_UPDATE"
        log.info("BinanceUserDataStream 启动  url=%s", url)

        # WS 工作线程
        threading.Thread(
            target=self._ws_worker,
            args=(url, self._handle),
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
            name="binance-user-data",
        ).start()

        # listenKey 续期线程（每 30 分钟）
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="binance-keepalive"
        )
        self._keepalive_thread.start()

    # ── 覆写 stop ────────────────────────────────────

    def stop(self) -> None:
        """停止 WS 连接 + 关闭 listenKey。"""
        super().stop()
        if self._listen_key is not None:
            try:
                self._exchange.close_listen_key()
            except Exception:
                log.exception("关闭 listenKey 失败")

    # ── listenKey 续期 ────────────────────────────────

    def _keepalive_loop(self) -> None:
        while self._running:
            time.sleep(30 * 60)  # 30 分钟
            if not self._running:
                break
            try:
                self._exchange.keepalive_listen_key()
                log.debug("listenKey 续期成功")
            except Exception:
                log.exception("listenKey 续期失败")

    # ── 消息处理 ─────────────────────────────────────

    def _handle(self, raw: str) -> object | None:
        """解析用户数据流消息，返回 FillEvent 或 None。"""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("非 JSON 消息: %s", raw[:120])
            return None

        # 组合流格式
        if "data" in payload and "e" in payload["data"]:
            return self._parse_event(payload["data"])
        # 单流格式
        if "e" in payload:
            return self._parse_event(payload)

        log.debug("未处理的用户数据流消息: %s", raw[:120])
        return None

    def _parse_event(self, d: dict) -> object | None:
        from application.events import FillEvent

        event_type = d.get("e")

        if event_type == "ORDER_TRADE_UPDATE":
            return self._parse_order_trade(d.get("o", {}))

        if event_type == "listenKeyExpired":
            log.warning("listenKey 过期，将重连")
            self._reconnect()
            return None

        return None

    def _parse_order_trade(self, o: dict) -> object:
        from application.events import FillEvent
        from core.domain import Instrument

        symbol = o.get("s", "").lower()
        instrument = self._instruments.get(symbol)
        if instrument is None:
            log.warning("未知标的: %s", symbol)
            return None

        side = Side.BUY if o.get("S") == "BUY" else Side.SELL
        price = Decimal(str(o.get("L", "0")))
        qty = Decimal(str(o.get("l", "0")))
        commission = Decimal(str(o.get("n", "0")))
        order_id = str(o.get("i", ""))

        return FillEvent(
            instrument=instrument,
            side=side,
            price=price,
            quantity=qty,
            commission=commission,
            exchange_id=order_id,
        )

    def _reconnect(self) -> None:
        """listenKey 过期后重建连接。"""
        try:
            self._listen_key = self._exchange.create_listen_key()
            url = f"{self._ws_base}?listenKey={self._listen_key}&events=ORDER_TRADE_UPDATE"
            log.info("listenKey 重建，重连中...")
            threading.Thread(
                target=self._ws_worker,
                args=(url, self._handle),
                kwargs={"ping_interval": 20, "ping_timeout": 10},
                daemon=True,
            ).start()
        except Exception:
            log.exception("listenKey 重建失败")
