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

from application.risk.builtin import (
    MinRebalanceMiddleware, 
    ExpectedProfitMiddleware,
    MinNotionalMiddleware,
    FundingRateMiddleware,
    MaxLeverageMiddleware,
    PositionLimitMiddleware,
)


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

    def test_warn_when_expected_profit_below_cost(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        RED: 预期收益 < 交易成本应 soft 警告（不拒单）
        场景: 币安永续手续费 0.04% (双边) + 滑点 0.03% = 总成本 0.07%
             预测收益 0.05% < 0.07% → soft 警告，仍放行

        ⚠️ 重要：策略自我报告的预期收益可能过度乐观，不能作为硬风控
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

        # ⚠️ soft 级别：passed=True, level="soft", 有警告信息
        assert result.passed  # 放行
        assert result.level == "soft"  # soft 级别
        assert "预期收益" in result.reason  # 有警告
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

    def test_warn_with_profit_multiplier(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        EDGE: 使用安全系数 3x (预期收益必须 ≥ 3×成本) - soft 警告
        场景: 成本 0.07%, 预测收益 0.15%
             0.15% < 3×0.07%=0.21% → soft 警告，仍放行
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

        # 0.15% < 3×0.07%=0.21% → soft 警告，不拒单
        assert result.passed  # 放行
        assert result.level == "soft"  # soft 级别
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


# ── MinNotionalMiddleware Tests ────────────────────────────────────────────

class TestMinNotionalMiddleware:
    """市价单价格获取测试"""

    def test_market_order_uses_mark_price(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        市价单应从 ctx.extra["mark_price"] 获取价格，而非使用默认值 1
        """
        middleware = MinNotionalMiddleware()

        # 市价单：无 limit_price
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.001"),  # 0.001 BTC
            order_type  = OrderType.MARKET,
            limit_price = None,  # 市价单无价格
        )

        # 设置市场价格
        risk_ctx.extra = {
            "mark_price": Decimal("65000"),  # BTC 价格 65000
        }

        # 计算名义价值: 0.001 × 65000 = 65 USDT > 5 USDT (min_notional)
        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 应放行（如果用默认值 1，则会是 0.001 USDT，被拒绝）
        assert result.passed

    def test_market_order_rejected_when_price_missing(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        市价单在无 mark_price 时应使用默认值 1（兜底），可能被拒绝
        """
        middleware = MinNotionalMiddleware()

        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.001"),
            order_type  = OrderType.MARKET,
            limit_price = None,
        )

        # 无市场价格信息
        risk_ctx.extra = {}

        # 使用默认价格 1，名义价值 = 0.001 × 1 = 0.001 USDT < 5 USDT
        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 应被拒绝
        assert not result.passed
        assert "名义价值" in result.reason


# ── FundingRateMiddleware Tests ─────────────────────────────────────────────

class TestFundingRateMiddleware:
    """资金费率风控测试"""

    @pytest.fixture
    def funding_ctx(self, btc_perp: Instrument) -> RiskContext:
        """带有资金费率的风控上下文"""
        return RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("1000"),
            positions     = {},
            open_orders   = [],
            funding_rates = {
                "BTC-USDT-PERP": Decimal("0.005"),  # 0.5% 正资金费率
            },
            extra         = {},
        )

    def test_reject_long_when_high_positive_rate(
        self, btc_perp: Instrument, funding_ctx: RiskContext
    ) -> None:
        """
        RED: 高正资金费率时禁止开多
        场景: 资金费率 0.5% > 阈值 0.3% → 禁止开多
        """
        middleware = FundingRateMiddleware(
            max_positive=0.003,  # 0.3%
            max_negative=0.003,
        )

        # 开多仓订单
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
        )

        result = middleware.process(order, funding_ctx, lambda o, c: RiskResult.approve())

        # soft 级别拒绝
        assert not result.passed
        assert result.level == "soft"
        assert "资金费率" in result.reason
        assert "禁止开多" in result.reason

    def test_reject_short_when_extreme_negative_rate(
        self, btc_perp: Instrument
    ) -> None:
        """
        EDGE: 极端负资金费率时禁止开空
        场景: 资金费率 -0.5% < -0.3% → 禁止开空
        """
        middleware = FundingRateMiddleware(
            max_positive=0.003,
            max_negative=0.003,
        )

        # 极端负资金费率
        ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("1000"),
            positions     = {},
            open_orders   = [],
            funding_rates = {
                "BTC-USDT-PERP": Decimal("-0.005"),  # -0.5%
            },
            extra         = {},
        )

        # 开空仓订单
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
        )

        result = middleware.process(order, ctx, lambda o, c: RiskResult.approve())

        # soft 级别拒绝
        assert not result.passed
        assert result.level == "soft"
        assert "资金费率" in result.reason
        assert "禁止开空" in result.reason

    def test_approve_long_when_normal_rate(
        self, btc_perp: Instrument
    ) -> None:
        """
        GREEN: 正常资金费率时允许开多
        场景: 资金费率 0.2% < 阈值 0.3% → 允许开多
        """
        middleware = FundingRateMiddleware(
            max_positive=0.003,
            max_negative=0.003,
        )

        ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("1000"),
            positions     = {},
            open_orders   = [],
            funding_rates = {
                "BTC-USDT-PERP": Decimal("0.002"),  # 0.2%
            },
            extra         = {},
        )

        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
        )

        result = middleware.process(order, ctx, lambda o, c: RiskResult.approve())

        assert result.passed

    def test_approve_reduce_only_always(
        self, btc_perp: Instrument, btc_position: Position
    ) -> None:
        """
        EDGE: 平仓单即使资金费率极端也应放行
        场景: 高正资金费率 0.5%，但 reduce_only=True → 放行
        """
        middleware = FundingRateMiddleware(
            max_positive=0.003,
            max_negative=0.003,
        )

        ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("1000"),
            positions     = {"BTC-USDT-PERP": btc_position},
            open_orders   = [],
            funding_rates = {
                "BTC-USDT-PERP": Decimal("0.005"),  # 高正费率
            },
            extra         = {},
        )

        # 平仓订单
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.1"),
            order_type  = OrderType.MARKET,
            limit_price = Decimal("65000"),
            reduce_only = True,
        )

        result = middleware.process(order, ctx, lambda o, c: RiskResult.approve())

        # 平仓应放行
        assert result.passed


# ── MaxLeverageMiddleware Tests ─────────────────────────────────────────────

class TestMaxLeverageMiddleware:
    """最大杠杆倍数检查测试"""

    def test_reject_high_leverage_order(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        RED: 高杠杆订单应被拒绝
        场景: 订单使用 20x 杠杆，但限制为 10x → 拒绝
        """
        middleware = MaxLeverageMiddleware(global_max=10)

        # 20x 杠杆订单
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            leverage    = 20,  # 订单杠杆
            limit_price = Decimal("65000"),
        )

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert not result.passed
        assert "杠杆" in result.reason
        assert "20x" in result.reason

    def test_approve_normal_leverage_order(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        GREEN: 正常杠杆订单应放行
        场景: 订单使用 5x 杠杆，限制 10x → 放行
        """
        middleware = MaxLeverageMiddleware(global_max=10)

        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            leverage    = 5,
            limit_price = Decimal("65000"),
        )

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert result.passed

    def test_use_position_leverage_when_order_leverage_zero(
        self, btc_perp: Instrument, btc_position: Position
    ) -> None:
        """
        EDGE: 订单杠杆为0时应使用仓位杠杆
        场景: 订单杠杆=0，仓位杠杆=10x，限制=10x → 放行
        """
        middleware = MaxLeverageMiddleware(global_max=10)

        # 仓位杠杆 10x
        btc_position.leverage = 10
        risk_ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("1000"),
            positions     = {"BTC-USDT-PERP": btc_position},
            open_orders   = [],
            funding_rates = {},
            extra         = {},
        )

        # 订单杠杆=0（未指定，使用仓位杠杆）
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            leverage    = 0,  # 未指定杠杆
            limit_price = Decimal("65000"),
        )

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 使用仓位杠杆 10x = 限制 10x → 放行
        assert result.passed

    def test_reduce_order_always_approved(
        self, btc_perp: Instrument, btc_position: Position
    ) -> None:
        """
        EDGE: 平仓单即使杠杆超标也应放行
        场景: 订单杠杆 20x，但 reduce_only=True → 放行
        """
        middleware = MaxLeverageMiddleware(global_max=10)

        risk_ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("1000"),
            positions     = {"BTC-USDT-PERP": btc_position},
            open_orders   = [],
            funding_rates = {},
            extra         = {},
        )

        # 平仓订单，高杠杆
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.1"),
            order_type  = OrderType.MARKET,
            leverage    = 20,  # 高杠杆
            limit_price = Decimal("65000"),
            reduce_only = True,
        )

        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 平仓应放行
        assert result.passed
# ── PositionLimitMiddleware Tests ──────────────────────────────────────────

class TestPositionLimitMiddleware:
    """仓位上限检查测试"""

    @pytest.fixture
    def short_position(self, btc_perp: Instrument) -> Position:
        """BTC 空头持仓"""
        return Position(
            instrument        = btc_perp,
            account_id        = "main",
            strategy_id       = "momentum",
            side              = PositionSide.SHORT,
            size              = Decimal("0.1"),
            entry_price       = Decimal("65000"),
            leverage          = 5,
            margin_mode       = MarginMode.CROSS,
            unrealized_pnl    = Decimal("-100"),
        )

    def test_market_order_price_estimation(
        self, btc_perp: Instrument, risk_ctx: RiskContext
    ) -> None:
        """
        市价单应使用 mark_price 而非默认值 1
        场景: BTC 市价单，无 limit_price，应从 ctx.extra 获取 mark_price
        """
        middleware = PositionLimitMiddleware(max_weight=0.10, leverage=5)

        # 市价单
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.01"),
            order_type  = OrderType.MARKET,
            leverage    = 5,
            limit_price = None,  # 市价单无价格
        )

        # 设置市场价格
        risk_ctx.extra = {"mark_price": Decimal("65000")}
        risk_ctx.nav_usdt = Decimal("10000")

        # notional = 0.01 × 65000 = 650 USDT
        # margin = 650 / 5 = 130 USDT
        # weight = 130 / 10000 = 1.3% < 10% → 放行
        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert result.passed

    def test_reversal_position_check(
        self, btc_perp: Instrument, short_position: Position
    ) -> None:
        """
        EDGE: 翻仓场景（空仓 → 多仓）保证金检查
        场景: 当前空仓 -0.1 BTC，订单 BUY 0.2 BTC → 翻仓为多仓 +0.1 BTC
        
        单向持仓模式下，交易所会先平空仓再开多仓，最大风险是最终仓位
        """
        middleware = PositionLimitMiddleware(max_weight=0.10, leverage=5)

        risk_ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("10000"),
            positions     = {"BTC-USDT-PERP": short_position},
            open_orders   = [],
            funding_rates = {},
            extra         = {"mark_price": Decimal("65000")},
        )

        # 翻仓订单：BUY 0.2 BTC
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.2"),
            order_type  = OrderType.MARKET,
            leverage    = 5,
            limit_price = None,
        )

        # new_size = -0.1 + 0.2 = +0.1 BTC
        # notional = 0.1 × 65000 = 6500 USDT
        # margin = 6500 / 5 = 1300 USDT
        # weight = 1300 / 10000 = 13% > 10% → 拒绝
        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        # 应拒绝（保证金占用超过限制）
        assert not result.passed
        assert "保证金占用" in result.reason

    def test_use_order_leverage(
        self, btc_perp: Instrument, short_position: Position
    ) -> None:
        """
        EDGE: 应优先使用订单杠杆而非仓位杠杆
        场景: 仓位杠杆 5x，订单杠杆 10x → 使用 10x 计算
        """
        middleware = PositionLimitMiddleware(max_weight=0.10, leverage=5)

        short_position.leverage = 5
        risk_ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("10000"),
            positions     = {"BTC-USDT-PERP": short_position},
            open_orders   = [],
            funding_rates = {},
            extra         = {"mark_price": Decimal("65000")},
        )

        # 订单使用更高杠杆
        order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.02"),
            order_type  = OrderType.MARKET,
            leverage    = 10,  # 订单杠杆 10x > 仓位杠杆 5x
            limit_price = None,
        )

        # new_size = -0.1 + 0.02 = -0.08 BTC (仍是空仓)
        # notional = 0.08 × 65000 = 5200 USDT
        # margin = 5200 / 10 = 520 USDT (使用订单杠杆 10x)
        # weight = 520 / 10000 = 5.2% < 10% → 放行
        result = middleware.process(order, risk_ctx, lambda o, c: RiskResult.approve())

        assert result.passed

