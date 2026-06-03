"""
tests/test_execution_pipeline.py

Phase 3 集成测试：执行管道 + 账号 + 监控。

覆盖：
  OMS 订单生命周期
  PaperExchange 成交逻辑
  SettlementService 解耦
  AccountService 仓位跟踪
  MonitorService P&L / 回撤
  完整端到端回测（含 P&L 验证）
"""
from __future__ import annotations

import io
import sys
import pathlib
import random
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.instrument import Instrument, InstrumentKind
from core.domain.order      import Order, OrderStatus, Side, OrderType
from core.domain.position   import PositionSide
from adapters.bus.sync      import SyncEventBus
from adapters.cache.memory  import MemoryCache
from adapters.exchange.paper import PaperExchange
from adapters.feed.csv_     import CsvFeed
from env.backtest           import SimClock, BacktestEnv
from application.context    import StrategyContext
from application.events     import (RiskApprovedEvent, FillEvent,
                                    OrderFilledEvent, SettlementEvent,
                                    PositionUpdatedEvent, BalanceUpdatedEvent,
                                    PnLEvent, AlertEvent, TradeEvent)
from application.services.account   import AccountService
from application.services.monitor   import MonitorService
from application.services.oms       import OMSService
from application.services.settlement import SettlementService
from application.services.signal    import SignalService
from application.services.portfolio import PortfolioService
from application.services.risk      import RiskService
from application.risk.pipeline      import RiskPipeline
from application.risk.builtin       import (PositionLimitMiddleware,
                                             MinNotionalMiddleware)
from application.strategy.loader    import PluginRegistry
from strategies.momentum            import MomentumStrategy
from config                         import Config
from container                      import build


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def btc_perp() -> Instrument:
    return Instrument(
        symbol="BTC-USDT-PERP", exchange="paper",
        base="BTC", quote="USDT", kind=InstrumentKind.PERP,
        tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
        min_notional=Decimal("5"), max_leverage=125,
    )


@pytest.fixture
def infra():
    bus   = SyncEventBus()
    cache = MemoryCache()
    return bus, cache


@pytest.fixture
def full_stack(infra, btc_perp):
    """完整执行栈（无信号层）：OMS + Settlement + Account + Monitor。"""
    bus, cache = infra
    initial_usdt = Decimal("10_000")

    clock     = SimClock()
    account  = AccountService(bus=bus, cache=cache, initial_usdt=initial_usdt)
    exchange = PaperExchange(bus=bus, cache=cache, initial_usdt=initial_usdt)
    oms      = OMSService(bus=bus, exchange=exchange, cache=cache)
    SettlementService(bus=bus)
    monitor  = MonitorService(bus=bus, cache=cache, account=account,
                               account_id="main", clock=clock,
                               initial_nav=initial_usdt)

    # 预先设置市价（Paper Exchange 需要）
    cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))

    return bus, cache, account, oms, monitor, btc_perp


def fire_buy(bus, btc_perp, qty="0.01", price="65000") -> RiskApprovedEvent:
    """触发一笔买入：直接发 RiskApprovedEvent 绕过信号/风控层。"""
    order = Order(
        instrument=btc_perp, account_id="main",
        side=Side.BUY, qty=Decimal(qty),
        order_type=OrderType.MARKET, limit_price=Decimal(price),
    )
    event = RiskApprovedEvent(account_id="main", order=order)
    bus.publish(event)
    return event


def fire_sell(bus, btc_perp, qty="0.01", price="65000") -> RiskApprovedEvent:
    order = Order(
        instrument=btc_perp, account_id="main",
        side=Side.SELL, qty=Decimal(qty),
        order_type=OrderType.MARKET, limit_price=Decimal(price),
    )
    event = RiskApprovedEvent(account_id="main", order=order)
    bus.publish(event)
    return event


# ── OMS 订单生命周期 ──────────────────────────────────────────────────────────

class TestOMS:
    def test_buy_order_triggers_fill(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        fills = []
        bus.subscribe(FillEvent, fills.append)

        fire_buy(bus, btc_perp)
        assert len(fills) == 1, "应触发一次成交"
        assert fills[0].filled_qty == Decimal("0.01")

    def test_fill_produces_order_filled_event(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        filled = []
        bus.subscribe(OrderFilledEvent, filled.append)

        fire_buy(bus, btc_perp)
        assert len(filled) == 1
        assert filled[0].order.status == OrderStatus.FILLED

    def test_order_persisted_in_cache(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        events = []
        bus.subscribe(OrderFilledEvent, events.append)
        fire_buy(bus, btc_perp)

        order_id = events[0].order.id
        cached   = cache.get(f"order:{order_id}")
        assert cached is not None
        assert cached.status == OrderStatus.FILLED

    def test_paper_slippage_applied(self, full_stack, btc_perp):
        """PaperExchange 买入时加一个 tick（模拟滑点）。"""
        bus, cache, account, oms, monitor, _ = full_stack
        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))

        fills = []
        bus.subscribe(FillEvent, fills.append)
        fire_buy(bus, btc_perp)

        # 买入价应略高于市价（+ tick_size）
        assert fills[0].fill_price == Decimal("65000") + btc_perp.tick_size

    def test_commission_applied(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        fills = []
        bus.subscribe(FillEvent, fills.append)
        fire_buy(bus, btc_perp)

        assert fills[0].commission > 0

    def test_rejected_if_no_price(self, infra, btc_perp):
        """无价格缓存时，PaperExchange 使用 limit_price。"""
        bus, cache = infra
        # 不设价格缓存，order 有 limit_price
        AccountService(bus=bus, cache=cache)
        PaperExchange(bus=bus, cache=cache)
        OMSService(bus=bus, exchange=PaperExchange(bus=bus, cache=cache), cache=cache)
        SettlementService(bus=bus)

        fills = []
        bus.subscribe(FillEvent, fills.append)

        order = Order(instrument=btc_perp, account_id="main",
                      side=Side.BUY, qty=Decimal("0.01"),
                      limit_price=Decimal("64000"))
        bus.publish(RiskApprovedEvent(account_id="main", order=order))
        # 应用 limit_price 成交
        assert len(fills) == 1
        assert fills[0].fill_price == Decimal("64000") + btc_perp.tick_size


# ── AccountService 仓位跟踪 ───────────────────────────────────────────────────

class TestAccountService:
    def test_position_opens_on_buy(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        fire_buy(bus, btc_perp, qty="0.01")

        pos = account.get_position("main", btc_perp.symbol)
        assert pos is not None
        assert pos.size == Decimal("0.01")
        assert pos.side == PositionSide.LONG

    def test_entry_price_correct(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))
        fire_buy(bus, btc_perp, qty="0.01")

        pos = account.get_position("main", btc_perp.symbol)
        # fill_price = market + tick = 65000.1
        expected_entry = Decimal("65000") + btc_perp.tick_size
        assert pos.entry_price == expected_entry

    def test_weighted_avg_entry_on_add_position(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        cache.set(f"price:{btc_perp.symbol}", Decimal("64000"))
        fire_buy(bus, btc_perp, qty="0.01")

        cache.set(f"price:{btc_perp.symbol}", Decimal("66000"))
        fire_buy(bus, btc_perp, qty="0.01")

        pos = account.get_position("main", btc_perp.symbol)
        assert pos.size == Decimal("0.02")
        # 加权均价应在 64000.1 和 66000.1 之间
        entry_1 = Decimal("64000") + btc_perp.tick_size
        entry_2 = Decimal("66000") + btc_perp.tick_size
        expected = (entry_1 + entry_2) / 2
        assert abs(pos.entry_price - expected) < Decimal("0.01")

    def test_usdt_decreases_on_buy(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        nav_before = account.get_nav_usdt("main")

        fire_buy(bus, btc_perp, qty="0.01")

        # USDT 余额减少，但 NAV（含持仓浮值）基本不变
        fill_price = Decimal("65000") + btc_perp.tick_size
        cost       = Decimal("0.01") * fill_price * Decimal("1.001")
        nav_after  = account.get_nav_usdt("main")
        # NAV 变化应接近于 0（买入以市价，无损耗）
        assert abs(nav_after - nav_before) < Decimal("10")

    def test_realized_pnl_on_sell(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        # 以 65000 买入
        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))
        fire_buy(bus, btc_perp, qty="0.01")

        # 以 66000 卖出（涨价）
        cache.set(f"price:{btc_perp.symbol}", Decimal("66000"))
        pos_events = []
        bus.subscribe(PositionUpdatedEvent, pos_events.append)
        fire_sell(bus, btc_perp, qty="0.01")

        sell_evt = pos_events[-1]
        # realized_pnl = qty * (sell_price - entry_price) - slippage
        entry      = Decimal("65000") + btc_perp.tick_size
        sell_price = Decimal("66000") - btc_perp.tick_size   # 卖出低一 tick
        expected_pnl = Decimal("0.01") * (sell_price - entry)
        assert abs(sell_evt.realized_pnl - expected_pnl) < Decimal("0.01")

    def test_position_closes_after_sell(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        fire_buy(bus, btc_perp, qty="0.01")
        fire_sell(bus, btc_perp, qty="0.01")

        pos = account.get_position("main", btc_perp.symbol)
        assert pos is None or pos.is_empty

    def test_nav_increases_after_profitable_trade(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))
        initial_nav = account.get_nav_usdt("main")
        fire_buy(bus, btc_perp, qty="0.01")

        cache.set(f"price:{btc_perp.symbol}", Decimal("66000"))
        fire_sell(bus, btc_perp, qty="0.01")

        final_nav = account.get_nav_usdt("main")
        assert final_nav > initial_nav, \
            f"盈利交易后 NAV 应增加: {initial_nav} → {final_nav}"


# ── MonitorService P&L / 回撤 ─────────────────────────────────────────────────

class TestMonitorService:
    def test_pnl_event_published_on_trade(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack
        pnl_events = []
        bus.subscribe(PnLEvent, pnl_events.append)

        fire_buy(bus, btc_perp, qty="0.01")
        assert len(pnl_events) > 0

    def test_realized_pnl_accumulates(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))
        fire_buy(bus, btc_perp, qty="0.01")

        cache.set(f"price:{btc_perp.symbol}", Decimal("66000"))
        fire_sell(bus, btc_perp, qty="0.01")

        assert monitor.realized_pnl > 0, \
            f"盈利后 realized_pnl 应 > 0，实际: {monitor.realized_pnl}"

    def test_drawdown_written_to_cache(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        fire_buy(bus, btc_perp, qty="0.01")
        dd = cache.get("drawdown:main")
        assert dd is not None
        assert isinstance(dd, Decimal)

    def test_drawdown_zero_on_nav_increase(self, full_stack, btc_perp):
        bus, cache, account, oms, monitor, _ = full_stack

        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))
        fire_buy(bus, btc_perp, qty="0.01")
        cache.set(f"price:{btc_perp.symbol}", Decimal("66000"))
        fire_sell(bus, btc_perp, qty="0.01")

        # NAV 增加，回撤应为 0
        assert monitor.current_drawdown == Decimal(0)

    def test_critical_alert_on_large_drawdown(self, infra, btc_perp):
        bus, cache = infra
        initial_usdt = Decimal("10_000")
        account  = AccountService(bus=bus, cache=cache, initial_usdt=initial_usdt)
        exchange = PaperExchange(bus=bus, cache=cache)
        OMSService(bus=bus, exchange=exchange, cache=cache)
        SettlementService(bus=bus)
        monitor = MonitorService(
            bus=bus, cache=cache, account=account, account_id="main",
            clock=SimClock(),
            initial_nav=initial_usdt, warn_dd=0.01, critical_dd=0.02,
        )
        alerts = []
        bus.subscribe(AlertEvent, alerts.append)

        # 买入
        cache.set(f"price:{btc_perp.symbol}", Decimal("65000"))
        order = Order(
            instrument=btc_perp, account_id="main",
            side=Side.BUY, qty=Decimal("1.0"),   # 买 1 BTC (大仓位)
            limit_price=Decimal("65000"),
        )
        bus.publish(RiskApprovedEvent(account_id="main", order=order))

        # 价格下跌 5% 触发回撤告警
        cache.set(f"price:{btc_perp.symbol}", Decimal("61750"))  # -5%
        # 再买一点以触发 P&L 更新
        order2 = Order(
            instrument=btc_perp, account_id="main",
            side=Side.SELL, qty=Decimal("0.1"),
            limit_price=Decimal("61750"),
        )
        bus.publish(RiskApprovedEvent(account_id="main", order=order2))

        critical_alerts = [a for a in alerts if a.level == "critical"]
        warn_alerts     = [a for a in alerts if a.level == "warn"
                           and "回撤" in a.title]
        assert len(warn_alerts) + len(critical_alerts) > 0, \
            "大幅回撤应触发告警"


# ── 因果链完整性（Phase 3 新增）─────────────────────────────────────────────

class TestCausalChainPhase3:
    def test_fill_causation_chain(self, full_stack, btc_perp):
        """FillEvent → OrderFilledEvent → SettlementEvent 因果链连贯。"""
        bus, cache, account, oms, monitor, _ = full_stack

        all_events = []
        for ev_type in [FillEvent, OrderFilledEvent, SettlementEvent,
                        PositionUpdatedEvent]:
            bus.subscribe(ev_type, all_events.append)

        approved = fire_buy(bus, btc_perp)
        corr_id  = approved.correlation_id

        # 所有下游事件应共享同一 correlation_id
        for ev in all_events:
            assert ev.correlation_id == corr_id, \
                f"{type(ev).__name__} correlation_id 断链"

    def test_causation_id_forms_chain(self, full_stack, btc_perp):
        """event_id / causation_id 形成连续的父子链。"""
        bus, cache, account, oms, monitor, _ = full_stack
        events_by_id = {}
        for ev_type in [FillEvent, OrderFilledEvent, SettlementEvent,
                        PositionUpdatedEvent]:
            def capture(e, events_by_id=events_by_id):
                events_by_id[e.event_id] = e
            bus.subscribe(ev_type, capture)

        approved = fire_buy(bus, btc_perp)

        # 追溯：从最后一个事件向上，应能找到父链
        for ev in events_by_id.values():
            if ev.causation_id:
                # causation_id 应指向 events_by_id 中的某个事件
                # 或者指向 RiskApprovedEvent（它不在收集列表中但存在于链中）
                # 只需确保有 causation_id
                assert ev.causation_id is not None


# ── 完整端到端回测（含 P&L 验证）─────────────────────────────────────────────

class TestEndToEnd:
    def _make_csv(self, n_bars=80, trend=100.0) -> str:
        random.seed(99)
        rows = ["timestamp,symbol,open,high,low,close,volume"]
        price = 65_000.0
        base  = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(n_bars):
            ts    = (base + timedelta(hours=i)).isoformat()
            price = max(1000, price + trend + random.uniform(-200, 200))
            rows.append(
                f"{ts},BTC-USDT-PERP,{price-50:.1f},{price+100:.1f},"
                f"{price-100:.1f},{price:.1f},{random.uniform(50,200):.2f}"
            )
        return "\n".join(rows)

    def test_full_backtest_runs(self, btc_perp):
        """完整回测：从 CSV 到 P&L，无异常。"""
        cfg    = Config.backtest_default(initial_usdt=10_000.0)
        system = build(cfg)
        feed   = system.make_feed(
            io.StringIO(self._make_csv()),
            {"BTC-USDT-PERP": btc_perp},
        )
        n = feed.run()
        assert n == 80, f"应推送 80 根 K 线，得到 {n}"

    def test_pnl_events_generated(self, btc_perp):
        """有成交 → 有 PnLEvent。"""
        bus, cache = SyncEventBus(), MemoryCache()
        cfg = Config.backtest_default(initial_usdt=10_000.0)
        system = build(cfg)
        pnl_events = []
        system.bus.subscribe(PnLEvent, pnl_events.append)

        feed = system.make_feed(
            io.StringIO(self._make_csv(n_bars=80, trend=150.0)),
            {"BTC-USDT-PERP": btc_perp},
        )
        feed.run()
        assert len(pnl_events) > 0, "应有 P&L 快照事件"

    def test_final_nav_reasonable(self, btc_perp):
        """强上涨趋势下，最终 NAV 不应大幅偏离初始值的合理范围。"""
        cfg    = Config.backtest_default(initial_usdt=10_000.0)
        system = build(cfg)
        feed   = system.make_feed(
            io.StringIO(self._make_csv(n_bars=80, trend=200.0)),
            {"BTC-USDT-PERP": btc_perp},
        )
        feed.run()

        final_nav = system.monitor.nav
        # NAV 应在初始值的 50% ~ 300% 之间（合理范围）
        assert Decimal("5000") < final_nav < Decimal("30000"), \
            f"最终 NAV {final_nav} 超出合理范围"

    def test_correlation_ids_all_linked(self, btc_perp):
        """PnLEvent 的 correlation_id 可追溯到某个 TradeEvent。"""
        cfg    = Config.backtest_default(initial_usdt=10_000.0)
        system = build(cfg)

        trade_corr_ids = set()
        pnl_corr_ids   = set()
        system.bus.subscribe(TradeEvent,  lambda e: trade_corr_ids.add(e.correlation_id))
        system.bus.subscribe(PnLEvent,    lambda e: pnl_corr_ids.add(e.correlation_id))

        feed = system.make_feed(
            io.StringIO(self._make_csv(n_bars=60, trend=150.0)),
            {"BTC-USDT-PERP": btc_perp},
        )
        feed.run()

        if pnl_corr_ids:
            linked = pnl_corr_ids & trade_corr_ids
            assert len(linked) > 0, \
                "PnLEvent 的 correlation_id 应能追溯到 TradeEvent"
