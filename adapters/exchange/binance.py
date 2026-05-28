"""
adapters/exchange/binance.py

Binance USDT-M 永续合约交易所适配器。

满足 ExecutionPort 协议。

依赖：
  pip install requests

认证：HMAC-SHA256 签名（每个请求附加 timestamp + signature）

测试：注入 mock_session 参数，无需真实 API Key：
  adapter = BinanceFuturesExchange(
      api_key="test", api_secret="test",
      session=mock_session,
  )
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from decimal import Decimal
from typing import Any

from core.domain.balance import Balance
from core.domain.instrument import Instrument, InstrumentKind
from core.domain.order import Order, Side, TimeInForce
from core.domain.position import MarginMode, Position, PositionSide

log = logging.getLogger(__name__)


class BinanceFuturesExchange:
    """
    Binance USDT-M Futures REST 适配器。

    满足 ExecutionPort 协议（结构化子类型）。

    testnet=True → 使用 https://testnet.binancefuture.com
    """

    exchange_id = "binance"

    _BASE_LIVE = "https://fapi.binance.com"
    _BASE_TEST = "https://testnet.binancefuture.com"

    def __init__(
        self,
        api_key:    str,
        api_secret: str,
        testnet:    bool = False,
        session:    Any  = None,    # requests.Session() 或 Mock
    ) -> None:
        self._api_key    = api_key
        self._api_secret = api_secret.encode()
        self._base       = self._BASE_TEST if testnet else self._BASE_LIVE
        self._session    = session or self._make_session()

        log.info("BinanceFuturesExchange 初始化  base=%s  testnet=%s",
                 self._base, testnet)

    # ── ExecutionPort 接口 ────────────────────────────────────────────────────

    def submit(self, order: Order) -> str:
        """提交订单，返回 Binance orderId 字符串。"""
        params = self._order_to_binance(order)
        resp   = self._signed_post("/fapi/v1/order", params)
        order_id = str(resp["orderId"])
        log.info("binance submit  %s %s qty=%.4f → orderId=%s",
                 order.side.value, order.instrument.symbol,
                 order.qty, order_id)
        return order_id

    def cancel(self, exchange_order_id: str) -> bool:
        """撤单。"""
        # 需要 symbol，这里简化（实际应从缓存查 symbol）
        log.warning("cancel 需要 symbol，此简化实现可能失败: %s",
                    exchange_order_id)
        return False

    def amend(self, exchange_order_id: str,
              qty:   Decimal | None = None,
              price: Decimal | None = None) -> bool:
        """Binance Futures 支持通过 PUT /fapi/v1/order 改单。"""
        # Phase 5 实现
        return False

    def get_position(self, instrument: Instrument,
                     account_id: str) -> Position | None:
        """获取持仓（GET /fapi/v3/positionRisk）。"""
        raw_list = self._signed_get("/fapi/v2/positionRisk", {
            "symbol": self._to_binance_symbol(instrument),
        })
        for raw in raw_list:
            if raw.get("symbol") == self._to_binance_symbol(instrument):
                return self._parse_position(raw, instrument, account_id)
        return None

    def get_balance(self, account_id: str) -> Balance:
        """获取账户余额（GET /fapi/v2/balance）。"""
        raw_list = self._signed_get("/fapi/v2/balance", {})
        balances: dict[str, Decimal] = {}
        for item in raw_list:
            asset = item.get("asset", "")
            available = Decimal(str(item.get("availableBalance", "0")))
            if available > 0:
                balances[asset] = available
        return Balance(account_id=account_id, balances=balances)

    def get_funding_rate(self, instrument: Instrument) -> Decimal:
        """获取当前资金费率（GET /fapi/v1/premiumIndex）。"""
        raw = self._get("/fapi/v1/premiumIndex", {
            "symbol": self._to_binance_symbol(instrument),
        })
        return Decimal(str(raw.get("lastFundingRate", "0")))

    def normalize_instrument(self, raw_symbol: str) -> Instrument:
        """
        将 Binance 私有 symbol（"BTCUSDT"）转换为系统统一 Instrument。
        需要先从 /fapi/v1/exchangeInfo 拉取合约规格。
        Phase 5 实现完整版本，此处返回占位值。
        """
        raise NotImplementedError("normalize_instrument: Phase 5 实现")

    def denormalize_order(self, order: Order) -> dict:
        return self._order_to_binance(order)

    # ── 归一化辅助 ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_binance_symbol(instrument: Instrument) -> str:
        """BTC-USDT-PERP → BTCUSDT"""
        return (instrument.base + instrument.quote).upper()

    def _order_to_binance(self, order: Order) -> dict:
        """将系统 Order 转换为 Binance API 参数。"""
        params: dict[str, Any] = {
            "symbol":   self._to_binance_symbol(order.instrument),
            "side":     "BUY" if order.side == Side.BUY else "SELL",
            "quantity": str(order.qty),
        }

        from core.domain.order import OrderType
        if order.order_type == OrderType.MARKET:
            params["type"] = "MARKET"
        elif order.order_type == OrderType.LIMIT:
            params["type"]  = "LIMIT"
            params["price"] = str(order.limit_price)
            params["timeInForce"] = self._tif(order.tif)
        elif order.order_type == OrderType.STOP_MARKET:
            params["type"]      = "STOP_MARKET"
            params["stopPrice"] = str(order.stop_price)

        if order.reduce_only:
            params["reduceOnly"] = "true"

        if order.id:
            params["newClientOrderId"] = order.id[:36]   # Binance 限 36 字符

        return params

    @staticmethod
    def _tif(tif: TimeInForce) -> str:
        return {
            TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC",
            TimeInForce.FOK: "FOK",
            TimeInForce.GTX: "GTX",
        }.get(tif, "GTC")

    def _parse_position(
        self,
        raw:        dict,
        instrument: Instrument,
        account_id: str,
    ) -> Position | None:
        """将 Binance positionRisk 转换为 Position。"""
        amt = Decimal(str(raw.get("positionAmt", "0")))
        if amt == 0:
            return None

        entry = Decimal(str(raw.get("entryPrice", "0")))
        lev   = int(raw.get("leverage", 1))
        side  = PositionSide.LONG if amt > 0 else PositionSide.SHORT
        unr   = Decimal(str(raw.get("unrealizedProfit", "0")))

        margin_type = (
            MarginMode.ISOLATED
            if raw.get("marginType") == "isolated"
            else MarginMode.CROSS
        )

        return Position(
            instrument        = instrument,
            account_id        = account_id,
            strategy_id       = "",
            side              = side,
            size              = abs(amt),
            entry_price       = entry,
            leverage          = lev,
            margin_mode       = margin_type,
            unrealized_pnl    = unr,
            liquidation_price = Decimal(str(raw.get("liquidationPrice", "0"))) or None,
        )

    # ── HTTP 封装 ─────────────────────────────────────────────────────────────

    def _signed_post(self, path: str, params: dict) -> dict:
        params["timestamp"] = self._ts()
        params["signature"] = self._sign(params)
        url = self._base + path
        log.info("POST %s", url)
        resp = self._session.post(url, params=params)
        return self._check(resp)

    def _signed_get(self, path: str, params: dict) -> Any:
        params["timestamp"] = self._ts()
        params["signature"] = self._sign(params)
        url = self._base + path
        log.info("GET %s", url)
        resp = self._session.get(url, params=params)
        return self._check(resp)

    def _get(self, path: str, params: dict) -> Any:
        url = self._base + path
        log.info("GET %s", url)
        resp = self._session.get(url, params=params)
        return self._check(resp)

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        return hmac.new(self._api_secret, query.encode(),
                        hashlib.sha256).hexdigest()

    @staticmethod
    def _ts() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _check(resp: Any) -> Any:
        if resp.status_code != 200:
            raise RuntimeError(
                f"Binance API 错误 {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def _make_session(self) -> Any:
        try:
            import requests
            s = requests.Session()
            s.headers["X-MBX-APIKEY"] = self._api_key
            return s
        except ImportError:
            raise ImportError(
                "实盘交易需要安装 requests：\n  pip install requests"
            )


    # ── User Data Stream ──────────────────────────────────────────────────────

    def create_listen_key(self) -> str:
        """
        POST /fapi/v1/listenKey
        获取用户数据流 listenKey（有效期 60 分钟）。
        """
        resp = self._signed_post("/fapi/v1/listenKey", {})
        key = resp["listenKey"]
        log.info("listenKey 已创建（60 分钟有效）")
        return key

    def keepalive_listen_key(self, listen_key: str) -> None:
        """
        PUT /fapi/v1/listenKey
        续期 listenKey（每 30 分钟调用一次）。
        """
        self._signed_put("/fapi/v1/listenKey", {"listenKey": listen_key})
        log.debug("listenKey 已续期")

    def close_listen_key(self, listen_key: str) -> None:
        """DELETE /fapi/v1/listenKey — 主动关闭数据流。"""
        try:
            self._signed_delete("/fapi/v1/listenKey", {"listenKey": listen_key})
        except Exception:
            pass

    def _signed_put(self, path: str, params: dict) -> dict:
        params = {**params, "timestamp": self._ts()}
        params["signature"] = self._sign(params)
        url = self._base + path
        log.info("PUT %s", url)
        resp = self._session.put(url, params=params)
        return self._check(resp)

    def _signed_delete(self, path: str, params: dict) -> dict:
        params = {**params, "timestamp": self._ts()}
        params["signature"] = self._sign(params)
        url = self._base + path
        log.info("DELETE %s", url)
        resp = self._session.delete(url, params=params)
        return self._check(resp)
