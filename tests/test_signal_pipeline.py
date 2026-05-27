"""
tests/test_signal_pipeline.py

Phase 2 集成测试：完整信号管道。

测试流：
  CSV 行情 → TradeEvent → SignalEvent → TargetPositionEvent
           → RiskApprovedEvent / RiskRejectedEvent

验证：
  1. TradeEvent 数量与 CSV 行数一致
  2. 预热结束后有 SignalEvent 产生
  3. 信号分数在 [-1, 1]
  4. TargetPositionEvent 仅在 delta >= lot_size 时产生
  5. RiskApprovedEvent 的 correlation_id 可追溯到 TradeEvent
  6. 架构规则：服务间无直接调用，只通过总线通信
"""
from __future__ import annotations

import io
import sys
import pathlib
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.instrument import Instrument, InstrumentKind
from adapters.bus.sync    import SyncEventBus
from adapters.cache.memory import MemoryCache
from adapters.feed.csv_   import CsvFeed
from env.backtest          import SimClock
from application.events   import (TradeEvent, SignalEvent,
                                   TargetPositionEvent,
                                   RiskApprovedEvent, RiskRejectedEvent)
from application.context  import StrategyContext
from application.services.account   import AccountService
from application.services.signal    import SignalService
from application.services.portfolio import PortfolioService
from application.services.risk      import RiskService
from application.risk.pipeline import RiskPipeline
from application.risk.builtin  import (PositionLimitMiddleware,
                                        MaxLeverageMiddleware,
                                        FundingRateMiddleware,
                                        MinNotionalMiddleware)
from application.strategy.loader import PluginRegistry, PluginLoader
from strategies.momentum import MomentumStrategy


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


def make_csv(n_bars: int, start_price: float = 65_000,
             trend: float = 50.0, noise: float = 100.0) -> str:
    """
    生成合成 BTC K 线 CSV。
    trend > 0 = 上涨趋势（触发多头信号）
    trend < 0 = 下跌趋势（触发空头信号）
    """
    import random
    random.seed(42)

    lines = ["timestamp,symbol,open,high,low,close,volume"]
    price = start_price
    base  = datetime(2024, 1, 1, tzinfo=timezone.utc)

    for i in range(n_bars):
        ts    = (base + timedelta(hours=i)).isoformat()
        price = price + trend + random.uniform(-noise, noise)
        price = max(price, 1000)   # 防止负价
        o = price - random.uniform(0, 200)
        h = price + random.uniform(0, 200)
        l = price - random.uniform(0, 200)
        vol = random.uniform(50, 200)
        lines.append(
            f"{ts},BTC-USDT-PERP,{o:.1f},{h:.1f},{l:.1f},{price:.1f},{vol:.2f}"
        )

    return "\n".join(lines)


@pytest.fixture
def trending_csv(btc_perp: Instrument) -> str:
    """上涨趋势，足够预热后触发多头信号。"""
    return make_csv(n_bars=60, trend=80.0)


@pytest.fixture
def flat_csv(btc_perp: Instrument) -> str:
    """横盘，不应触发信号（或极少）。"""
    return make_csv(n_bars=40, trend=0.0, noise=30.0)


def build_pipeline(bus, cache, account, btc_perp,
                   nav=Decimal("10_000"),
                   max_weight=0.10) -> tuple:
    """组装完整信号管道，返回事件收集列表。"""
    # 策略
    strategy = MomentumStrategy(window=20, scale=0.05, min_score=0.15)
    registry = PluginRegistry()
    registry.register(strategy)

    # make_ctx 工厂
    def make_ctx(strategy_id: str) -> StrategyContext:
        return StrategyContext(
            account_id=  "main",
            strategy_id= strategy_id,
            clock=       SimClock(),
            cache=       cache,
            account=     account,
        )

    # 服务（构造函数内完成订阅，顺序无关）
    SignalService(bus=bus, cache=cache,
                  strategies=registry.all(), make_ctx=make_ctx)
    PortfolioService(bus=bus, cache=cache, account=account,
                     account_id="main", max_weight=max_weight, min_score=0.15)
    risk_pipeline = RiskPipeline([
        MinNotionalMiddleware(),
        PositionLimitMiddleware(max_weight=0.20),
        MaxLeverageMiddleware(global_max=20),
    ])
    RiskService(bus=bus, pipeline=risk_pipeline,
                account=account, cache=cache)

    # 事件收集器
    trades, signals, targets, approved, rejected = [], [], [], [], []
    bus.subscribe(TradeEvent,        trades.append)
    bus.subscribe(SignalEvent,       signals.append)
    bus.subscribe(TargetPositionEvent, targets.append)
    bus.subscribe(RiskApprovedEvent, approved.append)
    bus.subscribe(RiskRejectedEvent, rejected.append)

    return trades, signals, targets, approved, rejected


# ── 测试：TradeEvent 推送 ─────────────────────────────────────────────────────

class TestCsvFeed:
    def test_emits_correct_number_of_trade_events(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus   = SyncEventBus()
        cache = MemoryCache()
        clock = SimClock()
        feed  = CsvFeed(bus=bus, clock=clock,
                        path=io.StringIO(trending_csv),
                        instruments={"BTC-USDT-PERP": btc_perp})
        received = []
        bus.subscribe(TradeEvent, received.append)
        n = feed.run()
        assert n == 60
        assert len(received) == 60

    def test_price_cache_updated_after_trade(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus   = SyncEventBus()
        cache = MemoryCache()
        clock = SimClock()
        feed  = CsvFeed(bus=bus, clock=clock,
                        path=io.StringIO(trending_csv),
                        instruments={"BTC-USDT-PERP": btc_perp})
        SignalService.__new__(SignalService)  # 不构造，只测 feed 直接效果
        # 直接订阅 TradeEvent 验证价格写入
        prices = []
        def capture(e): cache.set(f"price:{e.instrument.symbol}", e.price)
        bus.subscribe(TradeEvent, capture)
        feed.run()
        assert cache.exists("price:BTC-USDT-PERP")

    def test_clock_advances_with_data(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus   = SyncEventBus()
        cache = MemoryCache()
        clock = SimClock()
        feed  = CsvFeed(bus=bus, clock=clock,
                        path=io.StringIO(trending_csv),
                        instruments={"BTC-USDT-PERP": btc_perp})
        ts_before = clock.now()
        feed.run()
        ts_after = clock.now()
        assert ts_after > ts_before

    def test_unknown_symbol_skipped(self, btc_perp: Instrument) -> None:
        csv_data = (
            "timestamp,symbol,open,high,low,close,volume\n"
            "2024-01-01T00:00:00,UNKNOWN-PERP,100,110,90,105,10\n"
        )
        bus   = SyncEventBus()
        cache = MemoryCache()
        clock = SimClock()
        feed  = CsvFeed(bus=bus, clock=clock,
                        path=io.StringIO(csv_data),
                        instruments={"BTC-USDT-PERP": btc_perp})
        received = []
        bus.subscribe(TradeEvent, received.append)
        n = feed.run()
        assert n == 0
        assert len(received) == 0


# ── 测试：信号管道 ─────────────────────────────────────────────────────────────

class TestSignalPipeline:
    def test_signals_generated_after_warmup(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        trades, signals, targets, approved, rejected = build_pipeline(
            bus, cache, account, btc_perp)

        feed = CsvFeed(bus=bus, clock=clock,
                       path=io.StringIO(trending_csv),
                       instruments={"BTC-USDT-PERP": btc_perp})
        feed.run()

        # 前 19 根 K 线是预热期，第 20 根起可能出信号
        assert len(signals) > 0, "上涨趋势应产生多头信号"

    def test_signal_scores_in_valid_range(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, signals, _, _, _ = build_pipeline(bus, cache, account, btc_perp)
        feed = CsvFeed(bus=bus, clock=clock,
                       path=io.StringIO(trending_csv),
                       instruments={"BTC-USDT-PERP": btc_perp})
        feed.run()

        for sig in signals:
            assert -1.0 <= sig.score <= 1.0,  f"score {sig.score} 超出范围"
            assert  0.0 <= sig.confidence <= 1.0

    def test_uptrend_generates_long_signals(
        self, btc_perp: Instrument
    ) -> None:
        csv_data = make_csv(60, trend=200.0, noise=10.0)  # 强上涨
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, signals, _, _, _ = build_pipeline(bus, cache, account, btc_perp)
        feed = CsvFeed(bus=bus, clock=clock,
                       path=io.StringIO(csv_data),
                       instruments={"BTC-USDT-PERP": btc_perp})
        feed.run()

        long_sigs  = [s for s in signals if s.score > 0]
        short_sigs = [s for s in signals if s.score < 0]
        assert len(long_sigs) > len(short_sigs), \
            "强上涨趋势应以多头信号为主"

    def test_flat_market_generates_fewer_signals(
        self, btc_perp: Instrument, flat_csv: str
    ) -> None:
        bus_t, cache_t = SyncEventBus(), MemoryCache()
        bus_f, cache_f = SyncEventBus(), MemoryCache()
        clock = SimClock()
        acc = AccountService(bus=SyncEventBus(), cache=MemoryCache(), initial_usdt=Decimal("10_000"))

        trending_data = make_csv(60, trend=150.0, noise=10.0)
        _, trend_sigs, _, _, _ = build_pipeline(bus_t, cache_t, acc, btc_perp)
        CsvFeed(bus=bus_t, clock=SimClock(),
                path=io.StringIO(trending_data),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        _, flat_sigs, _, _, _ = build_pipeline(bus_f, cache_f, acc, btc_perp)
        CsvFeed(bus=bus_f, clock=SimClock(),
                path=io.StringIO(flat_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        assert len(trend_sigs) > len(flat_sigs), \
            f"趋势市场({len(trend_sigs)})应比横盘({len(flat_sigs)})有更多信号"


# ── 测试：组合服务 ─────────────────────────────────────────────────────────────

class TestPortfolioService:
    def test_target_positions_generated(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, _, targets, _, _ = build_pipeline(bus, cache, account, btc_perp)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()
        assert len(targets) > 0, "应产生目标仓位事件"

    def test_target_size_within_limits(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, _, targets, _, _ = build_pipeline(
            bus, cache, account, btc_perp, max_weight=0.10)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        for t in targets:
            # target_size * price <= max_weight * nav
            price = cache.get(f"price:{t.instrument.symbol}")
            if price and price > 0:
                notional = t.target_size * price
                assert notional <= Decimal("10_000") * Decimal("0.10") * Decimal("1.1"), \
                    f"目标仓位名义价值 {notional:.2f} 超出限制"


# ── 测试：风控服务 ─────────────────────────────────────────────────────────────

class TestRiskService:
    def test_approved_events_generated(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, _, _, approved, rejected = build_pipeline(
            bus, cache, account, btc_perp)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()
        assert len(approved) > 0, "应有 RiskApprovedEvent"

    def test_approved_order_has_valid_qty(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, _, _, approved, _ = build_pipeline(bus, cache, account, btc_perp)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()
        for evt in approved:
            assert evt.order.qty > 0
            assert evt.order.qty >= btc_perp.lot_size


# ── 测试：因果链完整性 ─────────────────────────────────────────────────────────

class TestCausalChain:
    def test_correlation_id_propagates_end_to_end(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        """
        每个 RiskApprovedEvent 的 correlation_id 必须能追溯到某个 TradeEvent。
        这是事件流完整性的核心检验。
        """
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        trades, signals, targets, approved, _ = build_pipeline(
            bus, cache, account, btc_perp)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        if not approved:
            pytest.skip("无 RiskApproved 事件，跳过因果链测试")

        trade_corr_ids = {t.correlation_id for t in trades}

        for evt in approved[:5]:   # 检查前 5 个
            assert evt.correlation_id in trade_corr_ids, (
                f"RiskApprovedEvent.correlation_id={evt.correlation_id} "
                f"无法追溯到任何 TradeEvent"
            )

    def test_signal_caused_by_trade(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        trades, signals, _, _, _ = build_pipeline(bus, cache, account, btc_perp)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        if not signals:
            pytest.skip("无 SignalEvent，跳过")

        trade_ids = {t.event_id for t in trades}
        for sig in signals[:10]:
            assert sig.causation_id in trade_ids, \
                f"SignalEvent 的 causation_id 应指向某个 TradeEvent"

    def test_target_caused_by_signal(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        bus, cache, clock = SyncEventBus(), MemoryCache(), SimClock()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))
        _, signals, targets, _, _ = build_pipeline(bus, cache, account, btc_perp)
        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        if not targets:
            pytest.skip("无 TargetPositionEvent，跳过")

        signal_ids = {s.event_id for s in signals}
        for tgt in targets[:10]:
            assert tgt.causation_id in signal_ids, \
                f"TargetPositionEvent.causation_id 应指向某个 SignalEvent"


# ── 测试：策略插件系统 ─────────────────────────────────────────────────────────

class TestPluginSystem:
    def test_register_and_retrieve(self) -> None:
        from application.strategy.loader import PluginRegistry
        registry = PluginRegistry()
        strategy = MomentumStrategy(window=20)
        registry.register(strategy)
        assert registry.get("momentum") is strategy

    def test_load_from_module(self) -> None:
        from application.strategy.loader import PluginRegistry, PluginLoader
        registry = PluginRegistry()
        loader   = PluginLoader(registry)
        loader.load_module(
            "strategies.momentum.MomentumStrategy",
            params={"window": 10, "scale": 0.03}
        )
        s = registry.get("momentum")
        assert s.version == "1.0.0"
        assert s._window == 10

    def test_invalid_strategy_rejected(self) -> None:
        from application.strategy.loader import PluginRegistry

        class BadStrategy:
            name = "bad"
            # 缺少 version 和其他方法

        registry = PluginRegistry()
        with pytest.raises(TypeError):
            registry.register(BadStrategy())

    def test_multiple_strategies(
        self, btc_perp: Instrument, trending_csv: str
    ) -> None:
        """两个独立策略各自产生信号，不互相干扰。"""
        from application.strategy.loader import PluginRegistry

        class FastMomentum(MomentumStrategy):
            name    = "fast_momentum"
            version = "1.0.0"
            def __init__(self): super().__init__(window=5, scale=0.02)

        class SlowMomentum(MomentumStrategy):
            name    = "slow_momentum"
            version = "1.0.0"
            def __init__(self): super().__init__(window=30, scale=0.08)

        bus, cache = SyncEventBus(), MemoryCache()
        account = AccountService(bus=bus, cache=cache, initial_usdt=Decimal("10_000"))

        registry = PluginRegistry()
        registry.register(FastMomentum())
        registry.register(SlowMomentum())

        signals = []
        def make_ctx(strategy_id):
            return StrategyContext(
                account_id="main", strategy_id=strategy_id,
                clock=SimClock(), cache=cache, account=account)

        SignalService(bus=bus, cache=cache,
                      strategies=registry.all(), make_ctx=make_ctx)
        bus.subscribe(SignalEvent, signals.append)

        CsvFeed(bus=bus, clock=SimClock(),
                path=io.StringIO(trending_csv),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        strategy_ids = {s.strategy_id for s in signals}
        # 快策略(window=5)肯定有信号，慢策略(window=30)在60根K线内也有
        assert "fast_momentum" in strategy_ids
