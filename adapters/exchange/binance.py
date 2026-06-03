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
import json
import logging
import time
import urllib.parse
from decimal import Decimal
from typing import Any

from core.domain.balance import Balance
from core.domain.instrument import Instrument, InstrumentKind
from core.domain.order import Order, PositionSide, Side, TimeInForce, WorkingType
from core.domain.position import MarginMode, Position, PositionSide as PosSide

log = logging.getLogger(__name__)


# ── Binance API 错误 ────────────────────────────────────────────────────────

class BinanceAPIError(Exception):
    """Binance API 错误，包含错误码和 HTTP 状态。"""
    UNKNOWN            = -1000
    DISCONNECTED       = -1001
    UNAUTHORIZED       = -1002
    TOO_MANY_REQUESTS  = -1003
    TOO_MANY_ORDERS    = -1015
    INVALID_QUANTITY   = -1013
    TIMESTAMP_MISMATCH = -1021
    INVALID_SIGNATURE  = -1022
    NEW_ORDER_REJECTED = -2010
    CANCEL_REJECTED    = -2011
    NO_SUCH_ORDER      = -2013

    def __init__(self, code: int, msg: str, http_status: int):
        self.code = code
        self.msg = msg
        self.http_status = http_status
        super().__init__(f"[{code}] {msg}")

    @property
    def retryable(self) -> bool:
        """是否可重试（临时性错误）。"""
        return self.code in (
            self.DISCONNECTED,
            self.TOO_MANY_REQUESTS,
            self.TOO_MANY_ORDERS,
            self.TIMESTAMP_MISMATCH,
        )


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
        self._symbol_cache: dict[str, str] = {}  # exchange_order_id → symbol

        log.info("BinanceFuturesExchange 初始化  base=%s  testnet=%s",
                 self._base, testnet)

    # ── ExecutionPort 接口 ────────────────────────────────────────────────────

    def submit(self, order: Order) -> str:
        """提交订单，返回 Binance orderId 字符串。"""
        params = self._order_to_binance(order)
        resp   = self._signed_post("/fapi/v1/order", params)
        order_id = str(resp["orderId"])
        # 缓存 order_id → symbol 映射，供 cancel/amend 使用
        symbol = self._to_binance_symbol(order.instrument)
        self._symbol_cache[order_id] = symbol
        log.info("binance submit  %s %s qty=%.4f → orderId=%s",
                 order.side.value, order.instrument.symbol,
                 order.qty, order_id)
        return order_id

    def cancel(self, exchange_order_id: str, symbol: str = "") -> bool:
        """撤单。需要 symbol（从缓存查或显式传入）。"""
        if not symbol:
            log.warning("cancel 需要 symbol，尝试从 pending 缓存查询")
            symbol = self._symbol_cache.get(exchange_order_id, "")
        if not symbol:
            log.error("cancel 失败：缺少 symbol，exchange_order_id=%s",
                      exchange_order_id)
            return False
        try:
            self._signed_delete("/fapi/v1/order", {
                "symbol":  symbol,
                "orderId": exchange_order_id,
            })
            log.info("binance cancel  orderId=%s  symbol=%s", exchange_order_id, symbol)
            return True
        except Exception as e:
            log.error("撤单失败: %s", e)
            return False

    def amend(self, exchange_order_id: str,
              qty:   Decimal | None = None,
              price: Decimal | None = None) -> bool:
        """改单（PUT /fapi/v1/order）。需要 symbol + 至少一个修改参数。"""
        symbol = self._symbol_cache.get(exchange_order_id, "")
        if not symbol:
            log.error("amend 失败：缺少 symbol，exchange_order_id=%s",
                      exchange_order_id)
            return False
        params: dict[str, Any] = {
            "symbol":  symbol,
            "orderId": exchange_order_id,
        }
        if qty is not None:
            params["quantity"] = str(qty)
        if price is not None:
            params["price"] = str(price)
        if "quantity" not in params and "price" not in params:
            log.error("amend 失败：至少需要 qty 或 price")
            return False
        try:
            self._signed_put("/fapi/v1/order", params)
            log.info("binance amend  orderId=%s  qty=%s  price=%s",
                     exchange_order_id, qty, price)
            return True
        except Exception as e:
            log.error("改单失败: %s", e)
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

    # ── 账户配置 ───────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """设置杠杆倍数 1~125。"""
        return self._signed_post("/fapi/v1/leverage", {
            "symbol":   symbol,
            "leverage": leverage,
        })

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> dict:
        """设置保证金模式：CROSSED(全仓) / ISOLATED(逐仓)。"""
        return self._signed_post("/fapi/v1/marginType", {
            "symbol":     symbol,
            "marginType": margin_type,
        })

    def get_position_mode(self) -> dict:
        """查询仓位模式：dualSidePosition=true(双向) / false(单向)。"""
        return self._signed_get("/fapi/v1/positionSide/dual", {})

    def set_position_mode(self, dual_side: bool) -> dict:
        """设置仓位模式：True=双向持仓 / False=单向持仓。"""
        return self._signed_post("/fapi/v1/positionSide/dual", {
            "dualSidePosition": "true" if dual_side else "false",
        })

    # ── 批量操作 ───────────────────────────────────────────────────────────

    def batch_submit(self, orders: list[Order]) -> list[dict]:
        """批量下单（最多 5 个）。"""
        if len(orders) > 5:
            raise ValueError("批量下单最多 5 个订单")
        batch = [json.dumps(self._order_to_binance(o)) for o in orders]
        return self._signed_post("/fapi/v1/batchOrders", {
            "batchOrders": json.dumps(batch),
        })

    def batch_cancel(self, symbol: str, order_ids: list[str]) -> list[dict]:
        """批量撤单。"""
        return self._signed_delete("/fapi/v1/batchOrders", {
            "symbol":      symbol,
            "orderIdList": json.dumps(order_ids),
        })

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
        from core.domain.order import OrderType
        params: dict[str, Any] = {
            "symbol":   self._to_binance_symbol(order.instrument),
            "side":     "BUY" if order.side == Side.BUY else "SELL",
        }

        # ── 订单类型 + 必选参数 ─────────────────────────────────────────────
        if order.order_type == OrderType.MARKET:
            params["type"] = "MARKET"
            params["quantity"] = str(order.qty)
        elif order.order_type == OrderType.LIMIT:
            params["type"] = "LIMIT"
            params["quantity"] = str(order.qty)
            params["price"] = str(order.limit_price)
            params["timeInForce"] = self._tif(order.tif)
        elif order.order_type == OrderType.STOP_MARKET:
            params["type"] = "STOP_MARKET"
            params["stopPrice"] = str(order.stop_price)
            if not order.close_position:
                params["quantity"] = str(order.qty)
        elif order.order_type == OrderType.STOP_LIMIT:
            params["type"] = "STOP"
            params["quantity"] = str(order.qty)
            params["price"] = str(order.limit_price)
            params["stopPrice"] = str(order.stop_price)
            params["timeInForce"] = self._tif(order.tif)
        elif order.order_type == OrderType.TAKE_PROFIT_MARKET:
            params["type"] = "TAKE_PROFIT_MARKET"
            params["stopPrice"] = str(order.stop_price)
            if not order.close_position:
                params["quantity"] = str(order.qty)
        elif order.order_type == OrderType.TAKE_PROFIT:
            params["type"] = "TAKE_PROFIT"
            params["quantity"] = str(order.qty)
            params["price"] = str(order.limit_price)
            params["stopPrice"] = str(order.stop_price)
            params["timeInForce"] = self._tif(order.tif)
        elif order.order_type == OrderType.TRAILING_STOP_MARKET:
            params["type"] = "TRAILING_STOP_MARKET"
            params["callbackRate"] = str(order.callback_rate)
            if not order.close_position:
                params["quantity"] = str(order.qty)
            if order.activation_price is not None:
                params["activationPrice"] = str(order.activation_price)

        # ── 通用可选参数 ─────────────────────────────────────────────────────
        if order.reduce_only:
            params["reduceOnly"] = "true"

        if order.position_side is not None:
            params["positionSide"] = order.position_side.value.upper()

        if order.close_position:
            params["closePosition"] = "true"

        # 条件单触发价格类型 + 价格保护
        if order.order_type in (
            OrderType.STOP_MARKET, OrderType.STOP_LIMIT,
            OrderType.TAKE_PROFIT_MARKET, OrderType.TAKE_PROFIT,
            OrderType.TRAILING_STOP_MARKET,
        ):
            params["workingType"] = self._working_type(order.working_type)
            params["priceProtect"] = "true" if order.price_protect else "false"

        if order.id:
            params["newClientOrderId"] = order.id[:36]

        return params

    @staticmethod
    def _tif(tif: TimeInForce) -> str:
        return {
            TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC",
            TimeInForce.FOK: "FOK",
            TimeInForce.GTX: "GTX",
        }.get(tif, "GTC")

    @staticmethod
    def _working_type(wt: WorkingType) -> str:
        return {
            WorkingType.CONTRACT_PRICE: "CONTRACT_PRICE",
            WorkingType.MARK_PRICE:     "MARK_PRICE",
        }.get(wt, "CONTRACT_PRICE")

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
        """检查 API 响应，解析 Binance 错误码并抛出结构化异常。"""
        if resp.status_code == 200:
            return resp.json()
        # 解析错误体
        try:
            body = resp.json()
            code = body.get("code", BinanceAPIError.UNKNOWN)
            msg  = body.get("msg", resp.text[:200])
        except Exception:
            code = BinanceAPIError.UNKNOWN
            msg  = resp.text[:200]
        raise BinanceAPIError(code=code, msg=msg, http_status=resp.status_code)

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
