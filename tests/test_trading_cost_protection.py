"""
tests/test_trading_cost_protection.py  —  实盘手续费保护中间件测试

测试目标:
  1. MinRebalanceMiddleware - 最小调仓量过滤
  2. ExpectedProfitMiddleware - 预期收益 > 交易成本
  3. HysteresisMiddleware - 双阈值机制
"""
from __future__ import annotations

import sys
import pathlib
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.instrument import Instrument, InstrumentKind
from core.domain.order      import Order, Side, OrderType
from core.domain.position   import Position, PositionSide, MarginMode
from core.ports.risk        import RiskContext, RiskResult

from application.risk.builtin import MinRebalanceMiddleware, ExpectedProfitMiddleware


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def btc_perp() -> Instrument:
    return Instrument(
        symbol       = "BTC-USDT-PERP",
        exchange     = "binance",
        base         = "BTC",
        quote        = "USDT",
        kind         = InstrumentKind.PERP,
        tick_size    = Decimal("0.1"),
        lot_size     = Decimal("0.001"),
        min_notional = Decimal("5"),
        max_leverage = 125,
    )


@pytest.fixture
def eth_perp() -> Instrument:
    return Instrument(
        symbol       = "ETH-USDT-PERP",
        exchange     = "binance",
        base         = "ETH",
        quote        = "USDT",
        kind         = InstrumentKind.PERP,
        tick_size    = Decimal("0.01"),
        lot_size     = Decimal("0.01"),
        min_notional = Decimal("5"),
        max_leverage = 125,
    )


@pytest.fixture
def risk_ctx(btc_perp: Instrument) -> RiskContext:
    """基础风控上下文,无持仓"""
    return RiskContext(
        account_id    = "main",
        nav_usdt      = Decimal("1000"),
        positions     = {},
        open_orders   = [],
        funding_rates = {},
        extra         = {},
    )


@pytest.fixture
def btc_position(btc_perp: Instrument) -> Position:
    """BTC 多头持仓"""
    return Position(
        instrument        = btc_perp,
        account_id        = "main",
        strategy_id       = "momentum",
        side              = PositionSide.LONG,
        size              = Decimal("0.1"),
        entry_price       = Decimal("65000"),
        leverage          = 10,
        margin_mode       = MarginMode.CROSS,
        unrealized_pnl    = Decimal("100"),
    )


# ── MinRebalanceMiddleware Tests ─────────────────────────────────────────────

class TestMinRebalanceMiddleware:
    """最小调仓量过滤:仓位变化小于阈值拒绝下单"""

    def test_reject_when_delta_below_threshold(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        RED: 仓位变化 < 5% NAV 应拒绝
        场景: NAV=1000USDT, 当前持仓=0, 目标持仓=0.0006BTC@65000=39USDT
             delta=39USDT < 5%×1000=50USDT → 拒绝
        """
        middleware = MinRebalanceMiddleware(min_delta_pct=Decimal("0.05"))

        # 订单: BUY 0.0006 BTC @ 65000 = 39 USDT notional
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.0006"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
        )

        # 当前无持仓,目标仓位小
        risk_ctx.positions = {}  # 无持仓

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert not result.passed
        assert "调仓量过小" in result.reason
        assert "39.00 USDT" in result.reason  # 显示实际调仓量
        assert "50.00 USDT" in result.reason   # 显示阈值

    def test_approve_when_delta_above_threshold(
        self, btc_perp: Instrument, risk_ctx: RiskContext, btc_position: Position
    ) -> None:
        """
        GREEN: 仓位变化 ≥ 5% NAV 应放行
        场景: NAV=1000USDT, 当前持仓=0.1BTC@65000=6500USDT,
             目标持仓=0.12BTC → delta=0.02BTC=1300USDT > 5%×1000=50USDT → 放行
        """
        middleware = MinRebalanceMiddleware(min_delta_pct=Decimal("0.05"))

        # 订单: BUY 0.02 BTC (增加持仓)
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.02"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
        )

        # 当前有多头持仓
        risk_ctx.positions = {"BTC-USDT-PERP": btc_position}

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert result.passed

    def test_reject_small_reduction(
        self, btc_perp: Instrument, risk_ctx: RiskContext, btc_position: Position
    ) -> None:
        """
        EDGE: 减仓量过小也应拒绝
        场景: 当前持仓=0.1BTC, 目标=0.098BTC (减2%)
             delta=0.002BTC=130USDT > 50USDT → 但如果 min_delta_pct=0.15 则拒绝
        """
        middleware = MinRebalanceMiddleware(min_delta_pct=Decimal("0.15"))

        # 订单: SELL 0.002 BTC (小幅减仓)
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.002"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            reduce_only = True,
        )

        risk_ctx.positions = {"BTC-USDT-PERP": btc_position}

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # delta=130USDT < 15%×1000=150USDT → 拒绝
        assert not result.passed
        assert "调仓量过小" in result.reason

    def test_approve_close_position(
        self, btc_perp: Instrument, risk_ctx: RiskContext, btc_position: Position
    ) -> None:
        """
        EDGE: 完全平仓应放行(即使 delta 小于阈值,平仓是必须的)
        场景: 当前持仓=0.1BTC, 目标=0 → 完全平仓应放行
        """
        middleware = MinRebalanceMiddleware(min_delta_pct=Decimal("0.05"))

        # 订单: SELL 0.1 BTC (完全平仓)
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.1"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            reduce_only = True,
        )

        risk_ctx.positions = {"BTC-USDT-PERP": btc_position}

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 完全平仓应放行(特殊情况)
        assert result.passed


# ── ExpectedProfitMiddleware Tests ────────────────────────────────────────────

class TestExpectedProfitMiddleware:
    """预期收益必须大于交易成本"""

    def test_reject_when_expected_profit_below_cost(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        RED: 预期收益 < 交易成本应拒绝
        场景: 币安永续手续费 0.04% (双边) + 滑点 0.03% = 总成本 0.07%
             预测收益 0.05% < 0.07% → 拒绝

        实现: 需要在 order.strategy_id 或 extra 中传递信号强度信息
        """
        middleware = ExpectedProfitMiddleware(
            trading_cost_pct=Decimal("0.0007"),  # 0.07%
            min_profit_multiplier=Decimal("1.0"),  # 预期收益必须 >= 成本
        )

        # 订单: BUY 0.01 BTC @ 65000 = 650 USDT
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            strategy_id = "momentum",  # 策略 ID
        )

        # 通过 extra 传递预期收益信息
        risk_ctx.extra = {
            "expected_return_pct": Decimal("0.0005"),  # 0.05% 预期收益
        }

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert not result.passed
        assert "预期收益" in result.reason
        assert "成本" in result.reason

    def test_approve_when_expected_profit_above_cost(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        GREEN: 预期收益 ≥ 交易成本应放行
        场景: 预测收益 0.15% > 0.07% → 放行
        """
        middleware = ExpectedProfitMiddleware(
            trading_cost_pct=Decimal("0.0007"),  # 0.07%
            min_profit_multiplier=Decimal("1.0"),
        )

        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            strategy_id = "momentum",
        )

        risk_ctx.extra = {
            "expected_return_pct": Decimal("0.0015"),  # 0.15% 预期收益
        }

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert result.passed

    def test_reject_with_profit_multiplier(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        EDGE: 使用安全系数 3x (预期收益必须 ≥ 3×成本)
        场景: 成本 0.07%, 预测收益 0.15%
             0.15% < 3×0.07%=0.21% → 拒绝
        """
        middleware = ExpectedProfitMiddleware(
            trading_cost_pct=Decimal("0.0007"),  # 0.07%
            min_profit_multiplier=Decimal("3.0"),  # 3x 安全系数
        )

        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            strategy_id = "momentum",
        )

        risk_ctx.extra = {
            "expected_return_pct": Decimal("0.0015"),  # 0.15% 预期收益
        }

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 0.15% < 3×0.07%=0.21% → 拒绝
        assert not result.passed
        assert "3x" in result.reason or "安全系数" in result.reason

    def test_approve_close_position_without_expected_return(
        self, btc_perp: Instrument, risk_ctx: RiskContext, btc_position: Position
    ) -> None:
        """
        EDGE: 平仓订单即使没有预期收益信息也应放行
        场景: reduce_only 订单直接放行(风控逻辑在其他中间件)
        """
        middleware = ExpectedProfitMiddleware(
            trading_cost_pct=Decimal("0.0007"),
            min_profit_multiplier=Decimal("1.0"),
        )

        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            reduce_only = True,
        )

        risk_ctx.positions = {"BTC-USDT-PERP": btc_position}
        risk_ctx.extra = {}  # 无预期收益信息

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 平仓订单应放行
        assert result.passed