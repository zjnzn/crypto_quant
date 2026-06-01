"""
container.py

依赖注入容器。唯一知道所有具体实现类的文件。

修改运行配置（Redis / Binance / Kafka）只改这一个文件，其余代码零改动。

build(cfg) 返回 System，包含运行回测或实盘所需的全部组件。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from config import Config
from core.domain.instrument import Instrument
from core.ports.bus import EventBusPort
from core.ports.cache import CachePort

# ── Adapters ──────────────────────────────────────────────────────────────────
from adapters.bus.sync      import SyncEventBus
from adapters.cache.memory  import MemoryCache
from adapters.exchange.paper import PaperExchange
from adapters.feed.csv_     import CsvFeed

# ── Application ───────────────────────────────────────────────────────────────
from application.context            import StrategyContext
from application.events             import *   # noqa: F403 (事件类型全量导入，供外部使用)
from application.risk.pipeline      import RiskPipeline
from application.risk.builtin       import (DrawdownMiddleware,
                                             FundingRateMiddleware,
                                             MaxLeverageMiddleware,
                                             MinNotionalMiddleware,
                                             PositionLimitMiddleware)
from application.services.account   import AccountService
from application.services.monitor   import MonitorService
from application.services.oms       import OMSService
from application.services.portfolio import PortfolioService
from application.services.risk      import RiskService
from application.services.settlement import SettlementService
from application.services.signal    import SignalService
from application.strategy.loader    import PluginLoader, PluginRegistry

# ── Environment ───────────────────────────────────────────────────────────────
from env.backtest import BacktestEnv, SimClock

log = logging.getLogger(__name__)


@dataclass
class System:
    """组装好的系统，持有运行所需的全部组件引用。"""
    bus:        EventBusPort
    cache:      CachePort
    clock:      SimClock
    account:    AccountService
    monitor:    MonitorService
    exchange:   PaperExchange

    def make_feed(
        self,
        path: "str | Path | TextIO",
        instruments: dict[str, Instrument],
        funding_rate_fn=None,
    ) -> CsvFeed:
        """工厂方法：创建 CsvFeed（回测专用）。
        funding_rate_fn: callable(datetime) -> Decimal，不传则不发资金费率事件。
        """
        return CsvFeed(
            bus=self.bus,
            clock=self.clock,
            path=path,
            instruments=instruments,
            funding_rate_fn=funding_rate_fn,
        )


def build(cfg: Config) -> System:
    """
    根据配置组装系统。

    调用方式：
        cfg    = Config.backtest_default(initial_usdt=10_000)
        system = build(cfg)
        feed   = system.make_feed("data/btc.csv", {"BTC-USDT-PERP": btc_perp})
        feed.run()
        print(system.monitor.nav)
    """
    initial_usdt = Decimal(str(cfg.account.initial_usdt))

    # ── 1. 基础设施 ───────────────────────────────────────────────────────────
    bus   = _make_bus(cfg)
    cache = _make_cache(cfg)

    # ── 2. 运行环境（时钟 + 账号初态）────────────────────────────────────────
    if cfg.mode in ("live", "paper"):
        from env.live import LiveEnv
        env = LiveEnv()
    else:
        env = BacktestEnv()

    # ── 3. 交易所适配器 ───────────────────────────────────────────────────────
    exchange = _make_exchange(cfg, bus, cache, initial_usdt)

    # ── 4. 账号服务（最先构造，因为其他服务依赖 AccountPort）──────────────────
    account_svc = AccountService(
        bus          = bus,
        cache        = cache,
        initial_usdt = initial_usdt,
    )

    # ── 5. StrategyContext 工厂 ───────────────────────────────────────────────
    account_id = cfg.account.main_id

    def make_ctx(strategy_id: str) -> StrategyContext:
        return StrategyContext(
            account_id  = account_id,
            strategy_id = strategy_id,
            clock       = env.clock,
            cache       = cache,
            account     = account_svc,
        )

    # ── 6. 策略插件动态加载 ───────────────────────────────────────────────────
    registry = PluginRegistry()
    if cfg.strategies:
        PluginLoader(registry).load_all(cfg.strategies)
    else:
        # 无配置时默认加载动量策略（方便开发调试）
        from strategies.momentum import MomentumStrategy
        registry.register(MomentumStrategy())
        log.warning("未配置策略，使用默认 MomentumStrategy")

    # ── 7. 风控管道（顺序 = 优先级）──────────────────────────────────────────
    risk_pipeline = RiskPipeline([
        MinNotionalMiddleware(),
        PositionLimitMiddleware(max_weight=cfg.risk.max_weight),
        MaxLeverageMiddleware(global_max=cfg.risk.max_leverage),
        DrawdownMiddleware(max_drawdown=cfg.risk.max_drawdown),
        FundingRateMiddleware(max_rate=cfg.risk.max_funding_rate),
    ])

    # ── 8. 应用服务（构造即完成 bus.subscribe 注册）──────────────────────────
    SignalService(
        bus        = bus,
        cache      = cache,
        strategies = registry.all(),
        make_ctx   = make_ctx,
    )
    portfolio_svc = PortfolioService(
        bus        = bus,
        cache      = cache,
        account    = account_svc,
        account_id = account_id,
        max_weight = cfg.risk.max_weight,
    )
    oms_svc = OMSService(
        bus      = bus,
        exchange = exchange,
        cache    = cache,
    )
    portfolio_svc.set_oms(oms_svc)  # 延迟注入，让 delta 检查计入在途订单
    risk_svc = RiskService(
        bus      = bus,
        pipeline = risk_pipeline,
        account  = account_svc,
        cache    = cache,
    )
    risk_svc.set_oms(oms_svc)   # 延迟注入，让 RiskContext.open_orders 有数据
    SettlementService(bus=bus)
    # AccountService 已在步骤 4 构造（已订阅 SettlementEvent）

    monitor_svc = MonitorService(
        bus         = bus,
        cache       = cache,
        account     = account_svc,
        account_id  = account_id,
        initial_nav = initial_usdt,
        warn_dd     = cfg.monitor.warn_drawdown,
        critical_dd = cfg.monitor.critical_drawdown,
    )

    log.info("系统装配完成：mode=%s  exchange=%s  strategies=%s",
             cfg.mode, exchange.exchange_id,
             [s.name for s in registry.all()])

    return System(
        bus      = bus,
        cache    = cache,
        clock    = env.clock,
        account  = account_svc,
        monitor  = monitor_svc,
        exchange = exchange,
    )


# ── 私有工厂函数 ──────────────────────────────────────────────────────────────

def _make_bus(cfg: Config) -> EventBusPort:
    if cfg.bus.backend == "sync":
        return SyncEventBus()
    if cfg.bus.backend == "kafka":
        from adapters.bus.kafka_ import KafkaBus  # Phase 5
        return KafkaBus(cfg.bus.kafka_servers)
    raise ValueError(f"未知 bus backend: {cfg.bus.backend}")


def _make_cache(cfg: Config) -> CachePort:
    if cfg.cache.backend == "memory":
        return MemoryCache()
    if cfg.cache.backend == "redis":
        from adapters.cache.redis_ import RedisCache  # Phase 5
        return RedisCache(cfg.cache.redis_host, cfg.cache.redis_port)
    raise ValueError(f"未知 cache backend: {cfg.cache.backend}")


def _make_exchange(
    cfg:         Config,
    bus:         EventBusPort,
    cache:       CachePort,
    initial_usdt: Decimal,
):
    if cfg.execution.exchange == "paper":
        return PaperExchange(bus=bus, cache=cache, initial_usdt=initial_usdt)
    if cfg.execution.exchange == "binance":
        from adapters.exchange.binance import BinanceFuturesExchange
        testnet = (cfg.mode == "paper")
        return BinanceFuturesExchange(
            api_key    = cfg.execution.api_key,
            api_secret = cfg.execution.api_secret,
            testnet    = testnet,
        )
    raise ValueError(f"未知交易所: {cfg.execution.exchange}")
