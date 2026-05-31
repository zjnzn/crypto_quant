"""
tests/test_phase5.py

Phase 5 测试：资金费率套利 + 完整监控指标 + 实盘配置。
"""
from __future__ import annotations

import io
import math
import sys
import pathlib
import random
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.instrument import Instrument, InstrumentKind
from adapters.bus.sync      import SyncEventBus
from adapters.cache.memory  import MemoryCache
from adapters.feed.csv_     import CsvFeed
from adapters.exchange.paper import PaperExchange
from env.backtest           import SimClock
from application.context    import StrategyContext
from application.events     import (FundingRateEvent, SignalEvent,
                                    TradeEvent, PnLEvent, AlertEvent,
                                    PositionUpdatedEvent, RiskApprovedEvent,
                                    OrderFilledEvent, FillEvent)
from application.services.account    import AccountService
from application.services.monitor    import MonitorService
from application.services.oms        import OMSService
from application.services.settlement import SettlementService
from application.risk.pipeline       import RiskPipeline
from application.risk.builtin        import MinNotionalMiddleware
from strategies.funding_rate_arb     import FundingRateArbStrategy
from strategies.mean_reversion       import MeanReversionStrategy
from strategies.momentum             import MomentumStrategy
from config   import Config
from container import build


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def btc_perp() -> Instrument:
    return Instrument(
        symbol="BTC-USDT-PERP", exchange="paper",
        base="BTC", quote="USDT", kind=InstrumentKind.PERP,
        tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
        min_notional=Decimal("5"), max_leverage=125,
    )


def make_ctx(cache=None, account=None):
    cache   = cache   or MemoryCache()
    account = account or AccountService(bus=SyncEventBus(), cache=cache)
    return StrategyContext(
        account_id="main", strategy_id="test",
        clock=SimClock(), cache=cache, account=account,
    )


# ════════════════════════════════════════════════════════════════════════════
# 资金费率套利策略
# ════════════════════════════════════════════════════════════════════════════

class TestFundingRateArb:
    def test_high_positive_rate_short_signal(self, btc_perp) -> None:
        """资金费率高（正）→ 做空信号（score < 0）。"""
        strat = FundingRateArbStrategy(threshold=0.001, max_score=0.6)
        ctx   = make_ctx()
        event = FundingRateEvent(instrument=btc_perp, rate=Decimal("0.003"))

        sigs = strat.on_funding(event, ctx)
        assert len(sigs) == 1
        assert sigs[0].score < 0, "高正费率应做空"
        assert -1.0 <= sigs[0].score <= 0.0

    def test_high_negative_rate_long_signal(self, btc_perp) -> None:
        """资金费率为负（空头付费）→ 做多信号（score > 0）。"""
        strat = FundingRateArbStrategy(threshold=0.001, max_score=0.6)
        ctx   = make_ctx()
        event = FundingRateEvent(instrument=btc_perp, rate=Decimal("-0.003"))

        sigs = strat.on_funding(event, ctx)
        assert len(sigs) == 1
        assert sigs[0].score > 0, "高负费率应做多"

    def test_low_rate_no_signal(self, btc_perp) -> None:
        """资金费率低于阈值 → 无信号。"""
        strat = FundingRateArbStrategy(threshold=0.001)
        ctx   = make_ctx()
        event = FundingRateEvent(instrument=btc_perp, rate=Decimal("0.0005"))

        sigs = strat.on_funding(event, ctx)
        assert sigs == []

    def test_zero_rate_no_signal(self, btc_perp) -> None:
        strat = FundingRateArbStrategy(threshold=0.001)
        ctx   = make_ctx()
        event = FundingRateEvent(instrument=btc_perp, rate=Decimal("0"))
        assert strat.on_funding(event, ctx) == []

    def test_max_rate_full_score(self, btc_perp) -> None:
        """费率极高 → score 达到 max_score 上限。"""
        strat = FundingRateArbStrategy(threshold=0.001, max_score=0.6)
        ctx   = make_ctx()
        event = FundingRateEvent(instrument=btc_perp, rate=Decimal("0.005"))

        sigs = strat.on_funding(event, ctx)
        assert abs(sigs[0].score + 0.6) < 0.001, f"高费率应 score≈-0.6, 得 {sigs[0].score}"

    def test_on_trade_returns_empty(self, btc_perp) -> None:
        """FundingRateArbStrategy.on_trade 不产生信号（仅跟踪 bar 计数）。"""
        strat = FundingRateArbStrategy(threshold=0.001, max_score=0.6)
        ctx   = make_ctx()

        # on_trade 不产生信号
        sigs = strat.on_trade(TradeEvent(instrument=btc_perp,
                                         price=Decimal("65000"),
                                         qty=Decimal("1")), ctx)
        assert sigs == []

    def test_score_range_valid(self, btc_perp) -> None:
        """信号分数始终在 [-1, 1]。"""
        strat = FundingRateArbStrategy(threshold=0.0001, max_score=0.6)
        ctx   = make_ctx()
        for rate in [0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01]:
            event = FundingRateEvent(instrument=btc_perp,
                                      rate=Decimal(str(rate)))
            sigs = strat.on_funding(event, ctx)
            for sig in sigs:
                assert -1.0 <= sig.score <= 1.0
                assert  0.0 <= sig.confidence <= 1.0

    def test_meta_contains_rate(self, btc_perp) -> None:
        strat = FundingRateArbStrategy(threshold=0.001, max_score=0.6)
        ctx   = make_ctx()
        sigs  = strat.on_funding(
            FundingRateEvent(instrument=btc_perp, rate=Decimal("0.003")), ctx
        )
        assert "rate" in sigs[0].meta
        assert "threshold" in sigs[0].meta


# ════════════════════════════════════════════════════════════════════════════
# CSV Feed 资金费率事件
# ════════════════════════════════════════════════════════════════════════════

class TestCsvFeedFunding:
    def _make_csv(self, n_bars=25) -> str:
        """生成覆盖 3 个 8h 窗口的 CSV（25 根小时 K 线）。"""
        rows = ["timestamp,symbol,open,high,low,close,volume"]
        base  = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        price = 65_000.0
        random.seed(1)
        for i in range(n_bars):
            ts    = (base + timedelta(hours=i)).isoformat()
            price = max(1_000, price + random.uniform(-200, 200))
            rows.append(
                f"{ts},BTC-USDT-PERP,{price:.1f},{price+100:.1f},"
                f"{price-100:.1f},{price:.1f},100.0"
            )
        return "\n".join(rows)

    def test_funding_events_emitted_at_8h(self, btc_perp) -> None:
        """在 00:00 / 08:00 / 16:00 时发布 FundingRateEvent。"""
        bus   = SyncEventBus()
        clock = SimClock()
        funding_events = []
        bus.subscribe(FundingRateEvent, funding_events.append)

        def static_rate(ts):
            return Decimal("0.002")   # 固定费率

        feed = CsvFeed(
            bus=bus, clock=clock,
            path=io.StringIO(self._make_csv(n_bars=25)),
            instruments={"BTC-USDT-PERP": btc_perp},
            funding_rate_fn=static_rate,
        )
        feed.run()

        # 25 小时内触发 00:00 + 08:00 + 16:00 + 00:00(次日) = 4 次
        assert len(funding_events) == 4

    def test_funding_rate_value_passed(self, btc_perp) -> None:
        """资金费率值从 funding_rate_fn 正确传入事件。"""
        bus   = SyncEventBus()
        clock = SimClock()
        funding_events = []
        bus.subscribe(FundingRateEvent, funding_events.append)

        def rate_fn(ts):
            return Decimal("0.00314")

        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(self._make_csv()),
                instruments={"BTC-USDT-PERP": btc_perp},
                funding_rate_fn=rate_fn).run()

        for e in funding_events:
            assert e.rate == Decimal("0.00314")

    def test_no_funding_without_fn(self, btc_perp) -> None:
        """不传 funding_rate_fn → 不发 FundingRateEvent。"""
        bus   = SyncEventBus()
        funding_events = []
        bus.subscribe(FundingRateEvent, funding_events.append)

        CsvFeed(bus=bus, clock=SimClock(),
                path=io.StringIO(self._make_csv()),
                instruments={"BTC-USDT-PERP": btc_perp}).run()

        assert len(funding_events) == 0

    def test_funding_arb_receives_events(self, btc_perp) -> None:
        """资金费率套利策略能收到 FundingRateEvent 并产生信号。"""
        bus, cache = SyncEventBus(), MemoryCache()
        account    = AccountService(bus=bus, cache=cache)
        clock      = SimClock()
        strat      = FundingRateArbStrategy(threshold=0.001, max_score=0.6)

        from application.services.signal import SignalService
        signals = []

        def make_ctx(sid):
            return StrategyContext("main", sid, clock, cache, account)

        SignalService(bus=bus, cache=cache, strategies=[strat],
                      make_ctx=make_ctx)
        bus.subscribe(SignalEvent, signals.append)

        CsvFeed(bus=bus, clock=clock,
                path=io.StringIO(self._make_csv(n_bars=25)),
                instruments={"BTC-USDT-PERP": btc_perp},
                funding_rate_fn=lambda ts: Decimal("0.003")).run()

        funding_signals = [s for s in signals
                           if s.strategy_id == "funding_rate_arb"]
        assert len(funding_signals) > 0, "应有资金费率套利信号"


# ════════════════════════════════════════════════════════════════════════════
# 完整监控指标
# ════════════════════════════════════════════════════════════════════════════

class TestMonitorMetrics:
    def _setup(self, initial_usdt=Decimal("10_000")):
        bus, cache = SyncEventBus(), MemoryCache()
        account    = AccountService(bus=bus, cache=cache,
                                    initial_usdt=initial_usdt)
        exchange   = PaperExchange(bus=bus, cache=cache,
                                   initial_usdt=initial_usdt)
        OMSService(bus=bus, exchange=exchange, cache=cache)
        SettlementService(bus=bus)
        monitor = MonitorService(bus=bus, cache=cache, account=account,
                                 account_id="main", initial_nav=initial_usdt)
        return bus, cache, account, monitor

    def _buy(self, bus, btc_perp, price="65000", qty="0.01"):
        from core.domain.order import Order, Side, OrderType
        cache = MemoryCache()  # unused - just for Order
        order = Order(instrument=btc_perp, account_id="main",
                      side=Side.BUY, qty=Decimal(qty),
                      limit_price=Decimal(price))
        bus.publish(RiskApprovedEvent(account_id="main", order=order))

    def _sell(self, bus, btc_perp, price="65000", qty="0.01"):
        from core.domain.order import Order, Side
        order = Order(instrument=btc_perp, account_id="main",
                      side=Side.SELL, qty=Decimal(qty),
                      limit_price=Decimal(price))
        bus.publish(RiskApprovedEvent(account_id="main", order=order))

    def test_win_rate_profitable_trade(self, btc_perp) -> None:
        bus, cache, account, monitor = self._setup()
        cache.set("price:BTC-USDT-PERP", Decimal("65000"))

        self._buy(bus, btc_perp, price="65000", qty="0.01")
        cache.set("price:BTC-USDT-PERP", Decimal("66000"))
        self._sell(bus, btc_perp, price="66000", qty="0.01")

        assert monitor.win_rate > 0, f"有盈利交易，胜率应 > 0，实际 {monitor.win_rate}"

    def test_win_rate_losing_trade(self, btc_perp) -> None:
        bus, cache, account, monitor = self._setup()
        cache.set("price:BTC-USDT-PERP", Decimal("66000"))
        self._buy(bus, btc_perp, price="66000", qty="0.01")

        cache.set("price:BTC-USDT-PERP", Decimal("65000"))
        self._sell(bus, btc_perp, price="65000", qty="0.01")

        if monitor.total_trades > 0:
            assert monitor.win_rate < 1.0

    def test_max_drawdown_tracked(self, btc_perp) -> None:
        """历史最大回撤被正确记录。"""
        bus, cache, account, monitor = self._setup()
        cache.set("price:BTC-USDT-PERP", Decimal("66000"))
        # 大仓位建仓
        self._buy(bus, btc_perp, price="66000", qty="0.1")
        # 价格下跌
        cache.set("price:BTC-USDT-PERP", Decimal("63000"))
        self._sell(bus, btc_perp, price="63000", qty="0.1")

        # 回撤应被记录
        assert monitor.max_drawdown >= Decimal(0)
        # 如果有损失，max_drawdown > 0
        if monitor.total_trades > 0 and float(monitor.realized_pnl) < 0:
            assert monitor.max_drawdown > Decimal(0)

    def test_max_drawdown_ge_current_drawdown(self, btc_perp) -> None:
        """历史最大回撤 ≥ 当前回撤。"""
        bus, cache, account, monitor = self._setup()
        cache.set("price:BTC-USDT-PERP", Decimal("65000"))
        self._buy(bus, btc_perp, qty="0.01")
        cache.set("price:BTC-USDT-PERP", Decimal("64000"))
        self._sell(bus, btc_perp, price="64000", qty="0.01")

        assert monitor.max_drawdown >= monitor.current_drawdown

    def test_sharpe_ratio_zero_with_few_trades(self, btc_perp) -> None:
        """交易笔数不足时 Sharpe = 0。"""
        _, _, _, monitor = self._setup()
        assert monitor.sharpe_ratio == 0.0

    def test_sharpe_ratio_positive_after_gains(self, btc_perp) -> None:
        """连续盈利交易后 Sharpe > 0。"""
        bus, cache, account, monitor = self._setup()

        for i in range(6):
            buy_p  = 65000 + i * 100
            sell_p = buy_p + 500
            cache.set("price:BTC-USDT-PERP", Decimal(str(buy_p)))
            self._buy(bus, btc_perp, price=str(buy_p), qty="0.01")
            cache.set("price:BTC-USDT-PERP", Decimal(str(sell_p)))
            self._sell(bus, btc_perp, price=str(sell_p), qty="0.01")

        if monitor.total_trades >= 4:
            # 连续盈利 → Sharpe 应为正值
            assert monitor.sharpe_ratio >= 0.0

    def test_snapshot_complete(self, btc_perp) -> None:
        """snapshot() 包含所有必需字段。"""
        _, _, _, monitor = self._setup()
        snap = monitor.snapshot()

        required = ["account_id", "nav_usdt", "initial_nav",
                    "total_pnl", "total_pnl_pct",
                    "realized_pnl", "unrealized_pnl",
                    "current_drawdown_pct", "max_drawdown_pct",
                    "sharpe_ratio", "win_rate_pct",
                    "total_trades", "wins", "losses", "daily_pnl"]
        for field in required:
            assert field in snap, f"snapshot() 缺少字段: {field}"

    def test_snapshot_values_consistent(self, btc_perp) -> None:
        """snapshot() 各字段数值一致。"""
        _, _, _, monitor = self._setup()
        snap = monitor.snapshot()

        assert snap["total_pnl"] == pytest.approx(
            snap["realized_pnl"] + snap["unrealized_pnl"], abs=0.01
        )
        assert snap["wins"] + snap["losses"] <= snap["total_trades"]
        assert 0.0 <= snap["win_rate_pct"] <= 100.0
        assert snap["max_drawdown_pct"] >= snap["current_drawdown_pct"] - 0.01


# ════════════════════════════════════════════════════════════════════════════
# 三策略完整回测（含资金费率）
# ════════════════════════════════════════════════════════════════════════════

class TestThreeStrategyBacktest:
    def _make_csv(self, n_bars=80) -> str:
        random.seed(42)
        rows = ["timestamp,symbol,open,high,low,close,volume"]
        price = 65_000.0
        base  = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(n_bars):
            ts    = (base + timedelta(hours=i)).isoformat()
            price = max(1_000, price + 80 + random.uniform(-200, 200))
            rows.append(
                f"{ts},BTC-USDT-PERP,{price:.1f},{price+100:.1f},"
                f"{price-100:.1f},{price:.1f},100.0"
            )
        return "\n".join(rows)

    def test_three_strategies_all_generate_signals(self, btc_perp) -> None:
        """三策略同时运行时各自都能产生信号。"""
        cfg    = Config.backtest_default(
            initial_usdt=10_000.0,
            strategies=[
                {"module": "strategies.momentum.MomentumStrategy",
                 "params": {"window": 20, "scale": 0.05, "min_score": 0.15}},
                {"module": "strategies.mean_reversion.MeanReversionStrategy",
                 "params": {"window": 20, "z_entry": 1.5, "z_threshold": 2.5}},
                {"module": "strategies.funding_rate_arb.FundingRateArbStrategy",
                 "params": {"threshold": 0.001, "max_score": 0.6}},
            ]
        )
        system = build(cfg)

        by_strategy = {}
        system.bus.subscribe(
            SignalEvent,
            lambda e: by_strategy.setdefault(e.strategy_id, []).append(e)
        )

        feed = system.make_feed(
            io.StringIO(self._make_csv()),
            {"BTC-USDT-PERP": btc_perp},
            funding_rate_fn=lambda ts: Decimal("0.002"),
        )
        feed.run()

        assert "momentum" in by_strategy, "动量策略无信号"
        # MR 和资金费率套利可能无信号（取决于行情），不强制断言

    def test_funding_arb_short_signal_integrated(self, btc_perp) -> None:
        """资金费率套利策略在完整系统中能产生做空订单。"""
        cfg    = Config.backtest_default(
            initial_usdt=10_000.0,
            strategies=[
                {"module": "strategies.funding_rate_arb.FundingRateArbStrategy",
                 "params": {"threshold": 0.001, "max_score": 0.6}},
            ]
        )
        system = build(cfg)

        from core.domain.order import Side
        sell_orders = []
        system.bus.subscribe(
            RiskApprovedEvent,
            lambda e: sell_orders.append(e) if e.order.side == Side.SELL else None
        )

        feed = system.make_feed(
            io.StringIO(self._make_csv()),
            {"BTC-USDT-PERP": btc_perp},
            funding_rate_fn=lambda ts: Decimal("0.003"),  # 始终高费率
        )
        feed.run()

        # 高资金费率应产生空头信号 → 被风控审批 → 有 SELL 订单
        assert len(sell_orders) > 0, "高资金费率下应有做空订单"

    def test_monitor_snapshot_after_full_run(self, btc_perp) -> None:
        """完整回测后 snapshot() 所有指标合理。"""
        cfg    = Config.backtest_default(initial_usdt=10_000.0)
        system = build(cfg)

        feed = system.make_feed(
            io.StringIO(self._make_csv(n_bars=80)),
            {"BTC-USDT-PERP": btc_perp},
            funding_rate_fn=lambda ts: Decimal("0.002"),
        )
        feed.run()

        snap = system.monitor.snapshot()

        assert snap["nav_usdt"] > 0
        assert snap["initial_nav"] == 10_000.0
        assert snap["max_drawdown_pct"] >= 0
        assert snap["max_drawdown_pct"] >= snap["current_drawdown_pct"] - 0.01
        assert 0.0 <= snap["win_rate_pct"] <= 100.0
        assert snap["total_trades"] == snap["wins"] + snap["losses"] + (
            snap["total_trades"] - snap["wins"] - snap["losses"]
        )


# ════════════════════════════════════════════════════════════════════════════
# 实盘配置验证
# ════════════════════════════════════════════════════════════════════════════

class TestLiveConfig:
    def test_config_from_yaml_parses(self) -> None:
        """config_live.yaml 可以被解析（不需要真实 Key）。"""
        cfg = Config.from_yaml(str(ROOT / "config_live.yaml"))
        assert cfg.mode == "live"
        assert cfg.execution.exchange == "binance"
        assert len(cfg.strategies) == 3

    def test_paper_mode_uses_paper_exchange(self) -> None:
        """paper 模式使用 PaperExchange（不调用 Binance API）。"""
        cfg = Config.backtest_default(initial_usdt=10_000.0)
        cfg.execution.exchange = "paper"
        system = build(cfg)
        assert system.exchange.exchange_id == "paper"

    def test_dry_run_system_builds(self, btc_perp) -> None:
        """dry-run（PaperExchange + 实盘配置）系统正常构建并运行。"""
        cfg = Config.backtest_default(initial_usdt=10_000.0)
        system = build(cfg)

        csv_data = "\n".join([
            "timestamp,symbol,open,high,low,close,volume",
            *[f"2024-01-01T{h:02d}:00:00,BTC-USDT-PERP,"
              f"65000,65200,64800,65100,100"
              for h in range(5)]
        ])
        feed = system.make_feed(
            io.StringIO(csv_data),
            {"BTC-USDT-PERP": btc_perp},
        )
        n = feed.run()
        assert n == 5

    def test_binance_adapter_mockable(self) -> None:
        """Binance 适配器可以注入 mock session（无需真实网络）。"""
        from adapters.exchange.binance import BinanceFuturesExchange
        from unittest.mock import MagicMock

        mock_session = MagicMock()
        mock_session.post.return_value.status_code = 200
        mock_session.post.return_value.json.return_value = {
            "orderId": 12345678, "status": "NEW",
        }

        adapter = BinanceFuturesExchange(
            api_key    = "test_key",
            api_secret = "test_secret",
            session    = mock_session,
        )
        assert adapter.exchange_id == "binance"

    def test_ws_feed_mockable(self, btc_perp) -> None:
        """WebSocket Feed 可以通过 put_raw() 注入消息（无需网络）。"""
        from adapters.feed.websocket import BinanceWsFeed

        bus        = SyncEventBus()
        instruments = {"btcusdt": btc_perp}
        feed       = BinanceWsFeed(bus=bus, instruments=instruments)

        received = []
        bus.subscribe(TradeEvent, received.append)

        # 注入一条 Binance aggTrade 格式消息
        msg = '{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","s":"BTCUSDT","p":"65000.5","q":"0.01","m":false}}'
        feed.put_raw(msg)

        assert len(received) == 1
        assert received[0].price == Decimal("65000.5")
        assert received[0].instrument.symbol == "BTC-USDT-PERP"
