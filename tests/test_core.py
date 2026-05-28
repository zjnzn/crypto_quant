"""
tests/test_core.py  —  核心域模型 + 基础适配器单元测试
"""
from __future__ import annotations

import sys
import pathlib
from datetime import timedelta
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.event      import Event
from core.domain.instrument import Instrument, InstrumentKind
from core.domain.order      import Order, Side, OrderType, OrderStatus
from core.domain.position   import Position, PositionSide, MarginMode
from core.domain.balance    import Balance
from core.domain.signal     import Signal

from adapters.bus.sync   import SyncEventBus
from adapters.cache.memory import MemoryCache


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def btc_perp() -> Instrument:
    return Instrument(
        symbol       = "BTC-USDT-PERP",
        exchange     = "paper",
        base         = "BTC",
        quote        = "USDT",
        kind         = InstrumentKind.PERP,
        tick_size    = Decimal("0.1"),
        lot_size     = Decimal("0.001"),
        min_notional = Decimal("5"),
        max_leverage = 125,
    )


@pytest.fixture
def buy_order(btc_perp: Instrument) -> Order:
    return Order(
        instrument = btc_perp,
        account_id = "main",
        side       = Side.BUY,
        qty        = Decimal("0.01"),
        order_type = OrderType.LIMIT,
        limit_price= Decimal("65000"),
    )


# ── Event Tests ───────────────────────────────────────────────────────────────

class TestEvent:
    def test_new_event_has_unique_ids(self) -> None:
        e1 = Event()
        e2 = Event()
        assert e1.event_id != e2.event_id
        assert e1.correlation_id != e2.correlation_id

    def test_caused_by_propagates_correlation(self) -> None:
        parent = Event()
        child  = Event().caused_by(parent)

        assert child.correlation_id == parent.correlation_id
        assert child.causation_id   == parent.event_id
        assert child.event_id       != parent.event_id

    def test_caused_by_chain(self) -> None:
        """三级因果链，correlation_id 全程不变。"""
        root  = Event()
        mid   = Event().caused_by(root)
        leaf  = Event().caused_by(mid)

        assert mid.correlation_id  == root.correlation_id
        assert leaf.correlation_id == root.correlation_id
        assert leaf.causation_id   == mid.event_id

    def test_event_is_immutable(self) -> None:
        e = Event()
        with pytest.raises(Exception):  # frozen=True raises FrozenInstanceError
            e.event_id = "hacked"       # type: ignore

    def test_type_name(self) -> None:
        assert Event().type_name == "Event"


# ── Instrument Tests ──────────────────────────────────────────────────────────

class TestInstrument:
    def test_round_price(self, btc_perp: Instrument) -> None:
        assert btc_perp.round_price(Decimal("65432.16")) == Decimal("65432.2")

    def test_round_qty(self, btc_perp: Instrument) -> None:
        assert btc_perp.round_qty(Decimal("0.0154")) == Decimal("0.015")

    def test_str(self, btc_perp: Instrument) -> None:
        assert str(btc_perp) == "BTC-USDT-PERP@paper"

    def test_hashable(self, btc_perp: Instrument) -> None:
        """Instrument 必须可哈希（用于 dict key）。"""
        d = {btc_perp: "test"}
        assert d[btc_perp] == "test"


# ── Order Tests ───────────────────────────────────────────────────────────────

class TestOrder:
    def test_order_auto_id(self, btc_perp: Instrument) -> None:
        o1 = Order(instrument=btc_perp, account_id="a",
                   side=Side.BUY, qty=Decimal("0.01"))
        o2 = Order(instrument=btc_perp, account_id="a",
                   side=Side.BUY, qty=Decimal("0.01"))
        assert o1.id != o2.id

    def test_order_immutable_update(self, buy_order: Order) -> None:
        submitted = buy_order.with_update(
            status=OrderStatus.SUBMITTED,
            exchange_order_id="ex-123",
        )
        # 原始订单不变
        assert buy_order.status == OrderStatus.NEW
        assert buy_order.exchange_order_id is None
        # 新订单已更新
        assert submitted.status == OrderStatus.SUBMITTED
        assert submitted.exchange_order_id == "ex-123"
        # 其他字段保留
        assert submitted.qty == buy_order.qty

    def test_remaining_qty(self, buy_order: Order) -> None:
        partial = buy_order.with_update(filled_qty=Decimal("0.003"))
        assert partial.remaining_qty == Decimal("0.007")

    def test_is_buy(self, buy_order: Order) -> None:
        assert buy_order.is_buy
        sell = buy_order.with_update(side=Side.SELL)
        assert not sell.is_buy

    def test_order_status_terminal(self) -> None:
        assert OrderStatus.FILLED.is_terminal
        assert OrderStatus.CANCELLED.is_terminal
        assert OrderStatus.REJECTED.is_terminal
        assert not OrderStatus.SUBMITTED.is_terminal
        assert not OrderStatus.PART_FILLED.is_terminal


# ── Position Tests ────────────────────────────────────────────────────────────

class TestPosition:
    def test_calc_unrealized_pnl_long(self, btc_perp: Instrument) -> None:
        pos = Position(
            instrument  = btc_perp,
            account_id  = "main",
            strategy_id = "s1",
            side        = PositionSide.LONG,
            size        = Decimal("0.1"),
            entry_price = Decimal("60000"),
        )
        pnl = pos.calc_unrealized_pnl(Decimal("65000"))
        assert pnl == Decimal("500")   # 0.1 * (65000 - 60000)

    def test_calc_unrealized_pnl_short(self, btc_perp: Instrument) -> None:
        pos = Position(
            instrument  = btc_perp,
            account_id  = "main",
            strategy_id = "s1",
            side        = PositionSide.SHORT,
            size        = Decimal("0.1"),
            entry_price = Decimal("60000"),
        )
        pnl = pos.calc_unrealized_pnl(Decimal("55000"))
        assert pnl == Decimal("500")   # 0.1 * (60000 - 55000)

    def test_notional(self, btc_perp: Instrument) -> None:
        pos = Position(
            instrument  = btc_perp,
            account_id  = "main",
            strategy_id = "s1",
            side        = PositionSide.LONG,
            size        = Decimal("0.1"),
            entry_price = Decimal("60000"),
        )
        assert pos.notional == Decimal("6000")


# ── Balance Tests ─────────────────────────────────────────────────────────────

class TestBalance:
    def test_available_existing(self) -> None:
        bal = Balance("main", {"USDT": Decimal("10000"), "BTC": Decimal("0.5")})
        assert bal.available("USDT") == Decimal("10000")

    def test_available_missing(self) -> None:
        bal = Balance("main", {})
        assert bal.available("USDT") == Decimal(0)

    def test_total_in_quote(self) -> None:
        bal = Balance("main", {
            "USDT": Decimal("5000"),
            "BTC":  Decimal("0.1"),
        })
        prices = {"BTC": Decimal("60000")}
        total = bal.total_in_quote("USDT", prices)
        assert total == Decimal("11000")  # 5000 + 0.1*60000

    def test_update(self) -> None:
        bal = Balance("main", {"USDT": Decimal("10000")})
        bal.update("USDT", Decimal("-500"))
        assert bal.available("USDT") == Decimal("9500")


# ── Signal Tests ──────────────────────────────────────────────────────────────

class TestSignal:
    def test_valid_signal(self, btc_perp: Instrument) -> None:
        sig = Signal(instrument=btc_perp, score=0.7, confidence=0.9)
        assert sig.is_long
        assert not sig.is_short

    def test_score_validation(self, btc_perp: Instrument) -> None:
        with pytest.raises(ValueError, match="score"):
            Signal(instrument=btc_perp, score=1.5)

    def test_confidence_validation(self, btc_perp: Instrument) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Signal(instrument=btc_perp, score=0.5, confidence=1.1)

    def test_flat_signal(self, btc_perp: Instrument) -> None:
        sig = Signal(instrument=btc_perp, score=0.0)
        assert sig.is_flat


# ── SyncEventBus Tests ────────────────────────────────────────────────────────

class TestSyncEventBus:
    def test_subscribe_and_publish(self) -> None:
        bus      = SyncEventBus()
        received = []

        bus.subscribe(Event, received.append)
        e = Event()
        bus.publish(e)

        assert len(received) == 1
        assert received[0] is e

    def test_multiple_subscribers(self) -> None:
        bus = SyncEventBus()
        r1, r2 = [], []
        bus.subscribe(Event, r1.append)
        bus.subscribe(Event, r2.append)
        bus.publish(Event())
        assert len(r1) == 1
        assert len(r2) == 1

    def test_unsubscribe(self) -> None:
        bus      = SyncEventBus()
        received = []
        bus.subscribe(Event, received.append)
        bus.unsubscribe(Event, received.append)
        bus.publish(Event())
        assert len(received) == 0

    def test_handler_exception_does_not_block_others(self) -> None:
        bus      = SyncEventBus()
        received = []

        def bad_handler(e: Event) -> None:
            raise RuntimeError("intentional")

        bus.subscribe(Event, bad_handler)
        bus.subscribe(Event, received.append)
        bus.publish(Event())   # bad_handler raises, but received still gets it
        assert len(received) == 1

    def test_event_type_isolation(self) -> None:
        """不同事件类型的订阅互不干扰。"""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class EventA(Event): pass

        @dataclass(frozen=True)
        class EventB(Event): pass

        bus = SyncEventBus()
        a_received, b_received = [], []
        bus.subscribe(EventA, a_received.append)
        bus.subscribe(EventB, b_received.append)

        bus.publish(EventA())
        assert len(a_received) == 1
        assert len(b_received) == 0

    def test_causation_chain_preserved(self) -> None:
        """通过总线传播的事件保留因果链。"""
        from dataclasses import dataclass as dc
        bus       = SyncEventBus()
        children  = []

        @dc(frozen=True)
        class Parent(Event): pass

        @dc(frozen=True)
        class Child(Event): pass

        def on_parent(e: Parent) -> None:
            bus.publish(Child().caused_by(e))

        bus.subscribe(Parent, on_parent)
        bus.subscribe(Child, children.append)

        parent = Parent()
        bus.publish(parent)

        assert len(children) == 1
        assert children[0].correlation_id == parent.correlation_id
        assert children[0].causation_id   == parent.event_id


# ── MemoryCache Tests ─────────────────────────────────────────────────────────

class TestMemoryCache:
    def test_set_and_get(self) -> None:
        cache = MemoryCache()
        cache.set("key", "value")
        assert cache.get("key") == "value"

    def test_get_missing(self) -> None:
        cache = MemoryCache()
        assert cache.get("nonexistent") is None

    def test_delete(self) -> None:
        cache = MemoryCache()
        cache.set("key", "value")
        cache.delete("key")
        assert cache.get("key") is None

    def test_exists(self) -> None:
        cache = MemoryCache()
        cache.set("key", "value")
        assert cache.exists("key")
        cache.delete("key")
        assert not cache.exists("key")

    def test_ttl_expiry(self) -> None:
        import time
        cache = MemoryCache()
        cache.set("key", "value", ttl=timedelta(milliseconds=50))
        assert cache.get("key") == "value"
        time.sleep(0.06)
        assert cache.get("key") is None

    def test_overwrite(self) -> None:
        cache = MemoryCache()
        cache.set("key", "v1")
        cache.set("key", "v2")
        assert cache.get("key") == "v2"

    def test_decimal_value(self) -> None:
        """Decimal 值可以正确存取（价格缓存的典型用法）。"""
        cache = MemoryCache()
        cache.set("price:BTC-USDT-PERP", Decimal("65432.1"))
        assert cache.get("price:BTC-USDT-PERP") == Decimal("65432.1")

    def test_keys_prefix(self) -> None:
        cache = MemoryCache()
        cache.set("price:BTC", Decimal("65000"))
        cache.set("price:ETH", Decimal("3500"))
        cache.set("order:123", "order_data")
        price_keys = cache.keys("price:")
        assert set(price_keys) == {"price:BTC", "price:ETH"}


# ── RiskPipeline Tests ────────────────────────────────────────────────────────

class TestRiskPipeline:
    @pytest.fixture
    def ctx(self) -> "RiskContext":
        from core.ports.risk import RiskContext
        return RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("10000"),
            positions     = {},
            open_orders   = [],
            funding_rates = {},
        )

    def test_empty_pipeline_approves(self, buy_order: Order,
                                      ctx: "RiskContext") -> None:
        from application.risk.pipeline import RiskPipeline
        pipeline = RiskPipeline([])
        result   = pipeline.check(buy_order, ctx)
        assert result.passed

    def test_single_rejection(self, buy_order: Order,
                               ctx: "RiskContext") -> None:
        from application.risk.pipeline import RiskPipeline
        from application.risk.builtin  import PositionLimitMiddleware

        # limit_price=65000, qty=0.01 → notional=650
        # weight = 650/10000 = 6.5% → 超过 5% 上限
        pipeline = RiskPipeline([PositionLimitMiddleware(max_weight=0.05)])
        result   = pipeline.check(buy_order, ctx)
        assert not result.passed
        assert "5%" in result.reason or "超过" in result.reason

    def test_chain_stops_at_first_rejection(self, buy_order: Order,
                                             ctx: "RiskContext") -> None:
        """第一个中间件拒绝后，后续中间件不应被调用。"""
        from application.risk.pipeline import RiskPipeline, RiskMiddleware, Next
        from core.ports.risk import RiskResult as RR

        call_count = [0]

        class CountingMW(RiskMiddleware):
            name = "counting"
            def process(self, order, ctx, call_next):
                call_count[0] += 1
                return call_next(order, ctx)

        class AlwaysRejectMW(RiskMiddleware):
            name = "always_reject"
            def process(self, order, ctx, call_next):
                return RR.reject("always reject")

        pipeline = RiskPipeline([AlwaysRejectMW(), CountingMW()])
        result   = pipeline.check(buy_order, ctx)

        assert not result.passed
        assert call_count[0] == 0   # CountingMW 未被调用

    def test_reduce_only_bypasses_position_limit(self, btc_perp: Instrument,
                                                  ctx: "RiskContext") -> None:
        """reduce_only 平仓单绕过仓位上限检查。"""
        from application.risk.pipeline import RiskPipeline
        from application.risk.builtin  import PositionLimitMiddleware

        close_order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.SELL,
            qty         = Decimal("0.01"),
            limit_price = Decimal("65000"),
            reduce_only = True,
        )
        pipeline = RiskPipeline([PositionLimitMiddleware(max_weight=0.001)])
        result   = pipeline.check(close_order, ctx)
        assert result.passed

    def test_funding_rate_blocks_long(self, buy_order: Order) -> None:
        from application.risk.pipeline import RiskPipeline
        from application.risk.builtin  import FundingRateMiddleware
        from core.ports.risk import RiskContext

        ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("10000"),
            positions     = {},
            open_orders   = [],
            funding_rates = {"BTC-USDT-PERP": Decimal("0.005")},
        )
        pipeline = RiskPipeline([FundingRateMiddleware(max_rate=0.003)])
        result   = pipeline.check(buy_order, ctx)
        assert not result.passed
        assert "资金费率" in result.reason

    def test_pending_orders_included_in_weight(self, btc_perp: Instrument) -> None:
        """
        在途订单应计入权重计算，防止重复下单导致超量。

        场景：
          - NAV = 10000 USDT
          - 当前持仓 0
          - 在途买单: 0.008 BTC @ 65000 = 520 USDT (5.2%)
          - 新订单: 0.005 BTC @ 65000 = 325 USDT (3.25%)
          - 总权重 = 8.45%，超过 5% 上限 → 应被拒绝
        """
        from application.risk.pipeline import RiskPipeline
        from application.risk.builtin  import PositionLimitMiddleware
        from core.ports.risk import RiskContext
        from core.domain.order import Order, OrderStatus

        # 在途订单（全部未成交）
        pending_order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.008"),
            limit_price = Decimal("65000"),
            status      = OrderStatus.SUBMITTED,
        )

        ctx = RiskContext(
            account_id    = "main",
            nav_usdt      = Decimal("10000"),
            positions     = {},  # 无持仓
            open_orders   = [pending_order],
            funding_rates = {},
        )

        new_order = Order(
            instrument  = btc_perp,
            account_id  = "main",
            side        = Side.BUY,
            qty         = Decimal("0.005"),
            limit_price = Decimal("65000"),
        )

        pipeline = RiskPipeline([PositionLimitMiddleware(max_weight=0.05)])
        result   = pipeline.check(new_order, ctx)
        # pending 0.008 + 新单 0.005 = 0.013 BTC @ 65000 = 845 USDT (8.45%)
        # 超过 5% 上限，应被拒绝
        assert not result.passed
        assert "权重" in result.reason
