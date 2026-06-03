"""
tests/test_binance_adapter.py

测试 BinanceFuturesExchange 新增功能。
"""
import json
import pytest
from decimal import Decimal
from unittest.mock import Mock, MagicMock

from adapters.exchange.binance import (
    BinanceFuturesExchange,
    BinanceAPIError,
)
from core.domain.instrument import Instrument, InstrumentKind
from core.domain.order import (
    Order, OrderType, Side, TimeInForce,
    PositionSide, WorkingType,
)


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def btc_perp() -> Instrument:
    return Instrument(
        symbol="BTC-USDT-PERP",
        exchange="binance",
        base="BTC",
        quote="USDT",
        kind=InstrumentKind.PERP,
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


@pytest.fixture
def mock_session():
    """Mock requests.Session。"""
    session = Mock()
    session.headers = {}
    return session


@pytest.fixture
def exchange(mock_session):
    """创建测试交易所实例。"""
    return BinanceFuturesExchange(
        api_key="test_key",
        api_secret="test_secret",
        testnet=True,
        session=mock_session,
    )


# ── 订单类型测试 ───────────────────────────────────────────────────────

class TestOrderTypes:
    """测试各种订单类型的参数映射。"""

    def test_market_order_params(self, exchange, btc_perp):
        """市价单参数。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.BUY,
            qty=Decimal("0.01"),
            order_type=OrderType.MARKET,
        )
        params = exchange._order_to_binance(order)

        assert params["type"] == "MARKET"
        assert params["symbol"] == "BTCUSDT"
        assert params["side"] == "BUY"
        assert params["quantity"] == "0.01"

    def test_limit_order_params(self, exchange, btc_perp):
        """限价单参数。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.SELL,
            qty=Decimal("0.01"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("65000"),
            tif=TimeInForce.GTC,
        )
        params = exchange._order_to_binance(order)

        assert params["type"] == "LIMIT"
        assert params["price"] == "65000"
        assert params["timeInForce"] == "GTC"

    def test_stop_market_order_params(self, exchange, btc_perp):
        """止损市价单参数。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.SELL,
            qty=Decimal("0.01"),
            order_type=OrderType.STOP_MARKET,
            stop_price=Decimal("60000"),
            working_type=WorkingType.MARK_PRICE,
            price_protect=True,
        )
        params = exchange._order_to_binance(order)

        assert params["type"] == "STOP_MARKET"
        assert params["stopPrice"] == "60000"
        assert params["workingType"] == "MARK_PRICE"
        assert params["priceProtect"] == "true"

    def test_trailing_stop_order_params(self, exchange, btc_perp):
        """追踪止损单参数。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.SELL,
            qty=Decimal("0.01"),
            order_type=OrderType.TRAILING_STOP_MARKET,
            callback_rate=Decimal("1.5"),  # 1.5% 回调
            activation_price=Decimal("70000"),
        )
        params = exchange._order_to_binance(order)

        assert params["type"] == "TRAILING_STOP_MARKET"
        assert params["callbackRate"] == "1.5"
        assert params["activationPrice"] == "70000"

    def test_take_profit_order_params(self, exchange, btc_perp):
        """止盈限价单参数。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.SELL,
            qty=Decimal("0.01"),
            order_type=OrderType.TAKE_PROFIT,
            limit_price=Decimal("70000"),
            stop_price=Decimal("69000"),
            tif=TimeInForce.GTC,
        )
        params = exchange._order_to_binance(order)

        assert params["type"] == "TAKE_PROFIT"
        assert params["price"] == "70000"
        assert params["stopPrice"] == "69000"


# ── 双向持仓测试 ───────────────────────────────────────────────────────

class TestPositionSide:
    """测试双向持仓模式参数。"""

    def test_long_position_side(self, exchange, btc_perp):
        """做多仓位方向。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.BUY,
            qty=Decimal("0.01"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("65000"),
            position_side=PositionSide.LONG,
        )
        params = exchange._order_to_binance(order)

        assert params["positionSide"] == "LONG"

    def test_short_position_side(self, exchange, btc_perp):
        """做空仓位方向。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.SELL,
            qty=Decimal("0.01"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("65000"),
            position_side=PositionSide.SHORT,
        )
        params = exchange._order_to_binance(order)

        assert params["positionSide"] == "SHORT"


# ── 一键平仓测试 ───────────────────────────────────────────────────────

class TestClosePosition:
    """测试一键平仓功能。"""

    def test_close_position_flag(self, exchange, btc_perp):
        """一键平仓标志。"""
        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.SELL,
            qty=Decimal("0"),  # 平仓时 quantity 可忽略
            order_type=OrderType.STOP_MARKET,
            stop_price=Decimal("60000"),
            close_position=True,
        )
        params = exchange._order_to_binance(order)

        assert params["closePosition"] == "true"
        # close_position=True 时不需要 quantity
        assert "quantity" not in params or params.get("quantity") == "0"


# ── Symbol 缓存测试 ─────────────────────────────────────────────────────

class TestSymbolCache:
    """测试订单 ID 到 symbol 的缓存。"""

    def test_submit_caches_symbol(self, exchange, btc_perp, mock_session):
        """提交订单后缓存 symbol。"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"orderId": 12345}
        mock_session.post.return_value = mock_response

        order = Order(
            instrument=btc_perp,
            account_id="main",
            side=Side.BUY,
            qty=Decimal("0.01"),
            order_type=OrderType.MARKET,
        )
        order_id = exchange.submit(order)

        assert order_id == "12345"
        assert exchange._symbol_cache["12345"] == "BTCUSDT"

    def test_cancel_uses_cache(self, exchange, mock_session):
        """撤单使用缓存中的 symbol。"""
        # 预先缓存
        exchange._symbol_cache["12345"] = "BTCUSDT"

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_session.delete.return_value = mock_response

        result = exchange.cancel("12345")

        assert result is True
        # 验证 delete 调用包含正确的 symbol
        call_args = mock_session.delete.call_args
        assert "BTCUSDT" in str(call_args)


# ── 账户配置接口测试 ─────────────────────────────────────────────────────

class TestAccountConfiguration:
    """测试账户配置接口。"""

    def test_set_leverage(self, exchange, mock_session):
        """设置杠杆倍数。"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "leverage": 20,
            "maxNotionalValue": "1000000",
        }
        mock_session.post.return_value = mock_response

        result = exchange.set_leverage("BTCUSDT", 20)

        assert result["leverage"] == 20
        call_args = mock_session.post.call_args
        assert "leverage" in str(call_args)

    def test_set_margin_type(self, exchange, mock_session):
        """设置保证金模式。"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_session.post.return_value = mock_response

        result = exchange.set_margin_type("BTCUSDT", "ISOLATED")

        assert result == {}
        call_args = mock_session.post.call_args
        assert "ISOLATED" in str(call_args)

    def test_get_position_mode(self, exchange, mock_session):
        """查询仓位模式。"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "dualSidePosition": False,  # 单向持仓
        }
        mock_session.get.return_value = mock_response

        result = exchange.get_position_mode()

        assert result["dualSidePosition"] is False

    def test_set_position_mode(self, exchange, mock_session):
        """设置仓位模式。"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_session.post.return_value = mock_response

        result = exchange.set_position_mode(dual_side=True)

        assert result == {}
        call_args = mock_session.post.call_args
        assert "true" in str(call_args)


# ── 批量操作测试 ───────────────────────────────────────────────────────

class TestBatchOperations:
    """测试批量订单接口。"""

    def test_batch_submit_max_5_orders(self, exchange, btc_perp, mock_session):
        """批量下单最多 5 个。"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_session.post.return_value = mock_response

        orders = [
            Order(
                instrument=btc_perp,
                account_id="main",
                side=Side.BUY,
                qty=Decimal("0.01"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("65000"),
            )
            for _ in range(5)
        ]

        result = exchange.batch_submit(orders)

        assert result == []

    def test_batch_submit_rejects_6_orders(self, exchange, btc_perp):
        """批量下单超过 5 个抛出异常。"""
        orders = [
            Order(
                instrument=btc_perp,
                account_id="main",
                side=Side.BUY,
                qty=Decimal("0.01"),
                order_type=OrderType.MARKET,
            )
            for _ in range(6)
        ]

        with pytest.raises(ValueError, match="最多 5"):
            exchange.batch_submit(orders)


# ── 错误处理测试 ───────────────────────────────────────────────────────

class TestErrorHandling:
    """测试 BinanceAPIError 错误处理。"""

    def test_api_error_with_code(self, mock_session):
        """API 返回错误码时抛出 BinanceAPIError。"""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "code": -2010,
            "msg": "Account has insufficient balance",
        }
        mock_response.text = '{"code": -2010, "msg": "Account has insufficient balance"}'
        mock_session.post.return_value = mock_response

        exchange = BinanceFuturesExchange(
            api_key="test",
            api_secret="test",
            testnet=True,
            session=mock_session,
        )

        with pytest.raises(BinanceAPIError) as exc_info:
            exchange._signed_post("/fapi/v1/order", {})

        assert exc_info.value.code == -2010
        assert exc_info.value.http_status == 400

    def test_api_error_retryable(self):
        """判断可重试错误。"""
        err1 = BinanceAPIError(
            code=BinanceAPIError.TOO_MANY_REQUESTS,
            msg="Too many requests",
            http_status=429,
        )
        assert err1.retryable is True

        err2 = BinanceAPIError(
            code=BinanceAPIError.NEW_ORDER_REJECTED,
            msg="Order rejected",
            http_status=400,
        )
        assert err2.retryable is False

    def test_api_error_non_json_response(self, mock_session):
        """API 返回非 JSON 错误时降级处理。"""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.side_effect = Exception("Not JSON")
        mock_response.text = "Internal Server Error"
        mock_session.post.return_value = mock_response

        exchange = BinanceFuturesExchange(
            api_key="test",
            api_secret="test",
            testnet=True,
            session=mock_session,
        )

        with pytest.raises(BinanceAPIError) as exc_info:
            exchange._signed_post("/fapi/v1/order", {})

        assert exc_info.value.code == BinanceAPIError.UNKNOWN
        assert exc_info.value.http_status == 500
