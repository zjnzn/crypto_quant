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
from core.domain.order import Order, OrderType, Side, TimeInForce
from core.domain.position import MarginMode, Position, PositionSide

log = logging.getLogger(__name__)


# ── 自定义异常 ─────────────────────────────────────────────────────────────────


class BinanceAPIError(Exception):
    """Binance API 返回的业务错误（HTTP 4XX + JSON body）。"""

    def __init__(self, code: int, msg: str, http_status: int = 400):
        self.code = code
        self.msg = msg
        self.http_status = http_status
        super().__init__(f"[{code}] {msg}")


class BinanceRateLimitError(BinanceAPIError):
    """HTTP 429 — 请求频率超限，应退避重试。"""
    pass


class BinanceServerError(Exception):
    """HTTP 503 — 服务端异常，区分三种子类型。"""

    def __init__(self, msg: str, execution_unknown: bool = False):
        self.execution_unknown = execution_unknown
        super().__init__(msg)


# ── 适配器主体 ─────────────────────────────────────────────────────────────────


class BinanceFuturesExchange:
    """
    Binance USDT-M Futures REST 适配器。

    满足 ExecutionPort 协议（结构化子类型）。

    testnet=True → 使用 https://demo-fapi.binance.com
    """

    exchange_id = "binance"

    _BASE_LIVE = "https://fapi.binance.com"
    _BASE_TEST = "https://demo-fapi.binance.com"

    def __init__(
        self,
        api_key:     str,
        api_secret:  str,
        testnet:     bool = False,
        session:     Any  = None,
        recv_window: int  = 5000,
        hedge_mode:  bool = False,
    ) -> None:
        self._api_key    = api_key
        self._api_secret = api_secret.encode()
        self._base       = self._BASE_TEST if testnet else self._BASE_LIVE
        self._session    = session or self._make_session()
        self._recv_window = recv_window
        self._hedge_mode  = hedge_mode

        # exchange_order_id → {symbol, side} 缓存，用于 cancel/amend
        self._order_cache: dict[str, dict[str, str]] = {}

        log.info("BinanceFuturesExchange 初始化  base=%s  testnet=%s  hedge_mode=%s",
                 self._base, testnet, hedge_mode)

    # ── ExecutionPort 接口 ────────────────────────────────────────────────────

    def submit(self, order: Order) -> str:
        """提交订单，返回 Binance orderId 字符串。"""
        params = self._order_to_binance(order)
        resp   = self._signed_post("/fapi/v1/order", params)
        order_id = str(resp["orderId"])

        # 缓存 exchange_order_id → symbol/side 映射（供 cancel/amend 使用）
        self._order_cache[order_id] = {
            "symbol": params["symbol"],
            "side":   params["side"],
        }

        log.info("binance submit  %s %s qty=%s → orderId=%s",
                 order.side.value, order.instrument.symbol,
                 order.qty, order_id)
        return order_id

    def cancel(self, exchange_order_id: str) -> bool:
        """
        撤单。DELETE /fapi/v1/order

        需要 symbol：从内部缓存查找；缓存未命中时无法撤单。
        """
        cached = self._order_cache.get(exchange_order_id)
        if not cached:
            log.error("cancel 失败: exchange_order_id=%s 不在本地缓存中（无法获取 symbol）",
                      exchange_order_id)
            return False

        try:
            self._signed_delete("/fapi/v1/order", {
                "symbol":  cached["symbol"],
                "orderId": exchange_order_id,
            })
            self._order_cache.pop(exchange_order_id, None)
            log.info("binance cancel orderId=%s symbol=%s 成功",
                     exchange_order_id, cached["symbol"])
            return True
        except BinanceAPIError as e:
            if e.code == -2011:
                log.warning("cancel 被拒: orderId=%s msg=%s", exchange_order_id, e.msg)
            else:
                log.error("cancel 异常: orderId=%s code=%d msg=%s",
                          exchange_order_id, e.code, e.msg)
            return False

    def amend(self, exchange_order_id: str,
              qty:   Decimal | None = None,
              price: Decimal | None = None) -> bool:
        """
        改单。PUT /fapi/v1/order

        Binance 要求同时传 symbol, side, orderId, quantity, price。
        """
        cached = self._order_cache.get(exchange_order_id)
        if not cached:
            log.error("amend 失败: exchange_order_id=%s 不在本地缓存中", exchange_order_id)
            return False

        params: dict[str, Any] = {
            "symbol":  cached["symbol"],
            "side":    cached["side"],
            "orderId": exchange_order_id,
        }
        if qty is not None:
            params["quantity"] = str(qty)
        if price is not None:
            params["price"] = str(price)

        try:
            self._signed_put("/fapi/v1/order", params)
            log.info("binance amend orderId=%s qty=%s price=%s 成功",
                     exchange_order_id, qty, price)
            return True
        except BinanceAPIError as e:
            log.error("amend 异常: orderId=%s code=%d msg=%s",
                      exchange_order_id, e.code, e.msg)
            return False

    def get_position(self, instrument: Instrument,
                     account_id: str) -> Position | None:
        """获取持仓（GET /fapi/v2/positionRisk）。"""
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

    # ── 扩展方法（非 ExecutionPort 必须）──────────────────────────────────────

    def query_order(self, exchange_order_id: str) -> dict | None:
        """
        查询订单状态。GET /fapi/v1/order

        用于 503 Unknown 场景的状态确认。
        """
        cached = self._order_cache.get(exchange_order_id)
        if not cached:
            log.warning("query_order: exchange_order_id=%s 不在缓存中", exchange_order_id)
            return None

        return self._signed_get("/fapi/v1/order", {
            "symbol":  cached["symbol"],
            "orderId": exchange_order_id,
        })

    def cancel_all_orders(self, instrument: Instrument) -> bool:
        """撤销指定交易对的全部挂单。DELETE /fapi/v1/allOpenOrders"""
        try:
            self._signed_delete("/fapi/v1/allOpenOrders", {
                "symbol": self._to_binance_symbol(instrument),
            })
            log.info("binance cancel_all symbol=%s 成功",
                     self._to_binance_symbol(instrument))
            return True
        except BinanceAPIError as e:
            log.error("cancel_all 异常: code=%d msg=%s", e.code, e.msg)
            return False

    def get_exchange_info(self, symbol: str | None = None) -> dict:
        """
        获取交易规则和交易对信息。GET /fapi/v1/exchangeInfo

        返回完整 exchangeInfo 或单个 symbol 的 filter 信息。
        """
        raw = self._get("/fapi/v1/exchangeInfo", {})
        if symbol is None:
            return raw

        for s in raw.get("symbols", []):
            if s.get("symbol") == symbol.upper():
                return s
        return {}

    def normalize_instrument(self, raw_symbol: str) -> Instrument:
        """
        将 Binance symbol（"BTCUSDT"）转换为系统 Instrument。

        从 /fapi/v1/exchangeInfo 获取合约详情。
        """
        info = self.get_exchange_info(raw_symbol)
        if not info:
            raise ValueError(f"Binance symbol {raw_symbol} 未找到")

        base = info.get("baseAsset", raw_symbol[:-4])
        quote = info.get("quoteAsset", "USDT")

        tick_size = Decimal("0.01")
        lot_size = Decimal("0.001")
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick_size = Decimal(str(f["tickSize"]))
            elif f["filterType"] == "LOT_SIZE":
                lot_size = Decimal(str(f["stepSize"]))

        return Instrument(
            symbol=f"{base}-{quote}-PERP",
            base=base,
            quote=quote,
            kind=InstrumentKind.PERP,
            tick_size=tick_size,
            lot_size=lot_size,
            exchange="binance",
        )

    def set_leverage(self, instrument: Instrument, leverage: int) -> dict:
        """调整开仓杠杆。POST /fapi/v1/leverage"""
        return self._signed_post("/fapi/v1/leverage", {
            "symbol":   self._to_binance_symbol(instrument),
            "leverage": leverage,
        })

    def set_margin_type(self, instrument: Instrument, margin_type: str) -> dict:
        """变换逐全仓模式。POST /fapi/v1/marginType  (ISOLATED / CROSSED)"""
        return self._signed_post("/fapi/v1/marginType", {
            "symbol":     self._to_binance_symbol(instrument),
            "marginType": margin_type.upper(),
        })

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

        # ── 订单类型映射 ──
        ot = order.order_type
        if ot == OrderType.MARKET:
            params["type"] = "MARKET"

        elif ot == OrderType.LIMIT:
            params["type"]  = "LIMIT"
            params["price"] = str(order.limit_price)
            params["timeInForce"] = self._tif(order.tif)

        elif ot == OrderType.STOP:
            params["type"]      = "STOP"
            params["stopPrice"] = str(order.stop_price)
            params["price"]     = str(order.limit_price)
            params["timeInForce"] = self._tif(order.tif)

        elif ot == OrderType.STOP_MARKET:
            params["type"]      = "STOP_MARKET"
            params["stopPrice"] = str(order.stop_price)

        elif ot == OrderType.STOP_LIMIT:
            params["type"]      = "STOP"
            params["stopPrice"] = str(order.stop_price)
            params["price"]     = str(order.limit_price)
            params["timeInForce"] = self._tif(order.tif)

        elif ot == OrderType.TAKE_PROFIT:
            params["type"]      = "TAKE_PROFIT"
            params["stopPrice"] = str(order.stop_price)
            params["price"]     = str(order.limit_price)
            params["timeInForce"] = self._tif(order.tif)

        elif ot == OrderType.TAKE_PROFIT_MARKET:
            params["type"]      = "TAKE_PROFIT_MARKET"
            params["stopPrice"] = str(order.stop_price)

        elif ot == OrderType.TRAILING_STOP_MARKET:
            params["type"]         = "TRAILING_STOP_MARKET"
            params["callbackRate"] = str(order.callback_rate)
            if order.activation_price:
                params["activationPrice"] = str(order.activation_price)

        # ── 通用可选参数 ──
        if order.reduce_only:
            params["reduceOnly"] = "true"

        if order.id:
            params["newClientOrderId"] = order.id[:36]

        # ── GTD goodTillDate ──
        if order.tif == TimeInForce.GTD and order.good_till_date:
            params["goodTillDate"] = order.good_till_date

        # ── 双向持仓模式 positionSide ──
        if self._hedge_mode:
            if order.reduce_only:
                # 平仓方向
                params["positionSide"] = "SHORT" if order.side == Side.BUY else "LONG"
            else:
                # 开仓方向
                params["positionSide"] = "LONG" if order.side == Side.BUY else "SHORT"

        return params

    @staticmethod
    def _tif(tif: TimeInForce) -> str:
        return {
            TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC",
            TimeInForce.FOK: "FOK",
            TimeInForce.GTX: "GTX",
            TimeInForce.GTD: "GTD",
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
        params["recvWindow"] = self._recv_window
        params["timestamp"]  = self._ts()
        params["signature"]  = self._sign(params)
        resp = self._session.post(self._base + path, params=params)
        return self._check(resp)

    def _signed_get(self, path: str, params: dict) -> Any:
        params["recvWindow"] = self._recv_window
        params["timestamp"]  = self._ts()
        params["signature"]  = self._sign(params)
        resp = self._session.get(self._base + path, params=params)
        return self._check(resp)

    def _signed_put(self, path: str, params: dict) -> dict:
        params["recvWindow"] = self._recv_window
        params["timestamp"]  = self._ts()
        params["signature"]  = self._sign(params)
        resp = self._session.put(self._base + path, params=params)
        return self._check(resp)

    def _signed_delete(self, path: str, params: dict) -> dict:
        params["recvWindow"] = self._recv_window
        params["timestamp"]  = self._ts()
        params["signature"]  = self._sign(params)
        resp = self._session.delete(self._base + path, params=params)
        return self._check(resp)

    def _get(self, path: str, params: dict) -> Any:
        resp = self._session.get(self._base + path, params=params)
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
        """
        统一响应检查。

        按 Binance 文档处理：
        - 200: 正常返回
        - 4XX: 业务错误（解析 code/msg）
        - 429: 限速
        - 503: 区分 Unknown/ServiceUnavailable/Throttled
        """
        if resp.status_code == 200:
            return resp.json()

        # 尝试解析 JSON 错误体
        body = {}
        try:
            body = resp.json()
        except Exception:
            pass

        code = body.get("code", 0)
        msg  = body.get("msg", resp.text[:200] if hasattr(resp, 'text') else "")

        if resp.status_code == 429:
            raise BinanceRateLimitError(code, msg, 429)

        if resp.status_code == 503:
            if "Unknown error" in msg:
                raise BinanceServerError(msg, execution_unknown=True)
            raise BinanceServerError(msg, execution_unknown=False)

        if code != 0:
            raise BinanceAPIError(code, msg, resp.status_code)

        raise BinanceAPIError(-1, f"HTTP {resp.status_code}: {msg}", resp.status_code)

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
        """POST /fapi/v1/listenKey — 获取用户数据流 listenKey（有效期 60 分钟）。"""
        resp = self._signed_post("/fapi/v1/listenKey", {})
        key = resp["listenKey"]
        log.info("listenKey 已创建（60 分钟有效）")
        return key

    def keepalive_listen_key(self, listen_key: str) -> None:
        """PUT /fapi/v1/listenKey — 续期 listenKey（每 30 分钟调用一次）。"""
        self._signed_put("/fapi/v1/listenKey", {"listenKey": listen_key})
        log.debug("listenKey 已续期")

    def close_listen_key(self, listen_key: str) -> None:
        """DELETE /fapi/v1/listenKey — 主动关闭数据流。"""
        try:
            self._signed_delete("/fapi/v1/listenKey", {"listenKey": listen_key})
        except Exception:
            pass
