# 00 — Feature Inventory

**Project:** crypto_quant — 事件驱动型虚拟币量化交易系统
**Date:** 2026-05-29

---

## Feature Boundaries

### F1: Core Domain (纯领域模型)
- **Scope:** `core/domain/`, `core/ports/`
- **Entry:** `Event` ([core/domain/event.py:27](core/domain/event.py#L27)), `Order` ([core/domain/order.py:59](core/domain/order.py#L59)), `Instrument` ([core/domain/instrument.py:23](core/domain/instrument.py#L23)), `Position` ([core/domain/position.py:25](core/domain/position.py#L25))
- **Ports:** `EventBusPort`, `CachePort`, `ExecutionPort`, `AccountPort`, `ClockPort`, `RiskPort`
- **Files:** 10 (6 domain + 6 ports, some ports share files)
- **Deps:** stdlib only

### F2: Event System (事件定义)
- **Scope:** `application/events.py`
- **Entry:** 17 event types from `TradeEvent` ([events.py:32](application/events.py#L32)) to `AlertEvent` ([events.py:201](application/events.py#L201))
- **Files:** 1
- **Deps:** `core/domain/`

### F3: Strategy Framework (策略框架)
- **Scope:** `application/strategy/`, `application/context.py`
- **Entry:** `Strategy` protocol ([base.py:22](application/strategy/base.py#L22)), `PluginLoader` ([loader.py:62](application/strategy/loader.py#L62)), `StrategyContext` ([context.py:24](application/context.py#L24))
- **Files:** 3
- **Deps:** `core/`, `application/events.py` (TYPE_CHECKING)

### F4: Signal Pipeline (信号生成与合并)
- **Scope:** `application/services/signal.py`, `application/services/portfolio.py`
- **Entry:** `SignalService` ([signal.py:38](application/services/signal.py#L38)), `PortfolioService` ([portfolio.py:32](application/services/portfolio.py#L32))
- **Flow:** MarketEvent → Signal → SignalEvent → (fusion) → TargetPositionEvent
- **Deps:** `core/`, `application/events.py`, `application/strategy/`, `application/context.py`

### F5: Risk Pipeline (风控审查)
- **Scope:** `application/services/risk.py`, `application/risk/`
- **Entry:** `RiskService` ([risk.py:33](application/services/risk.py#L33)), `RiskPipeline` ([pipeline.py:41](application/risk/pipeline.py#L41))
- **Middleware:** MinNotional → PositionLimit → MaxLeverage → Drawdown → FundingRate ([container.py:137-143](container.py#L137-L143))
- **Deps:** `core/`, `application/events.py`, `application/risk/`

### F6: Order Execution (订单执行)
- **Scope:** `application/services/oms.py`, `application/services/settlement.py`
- **Entry:** `OMSService` ([oms.py:43](application/services/oms.py#L43)), `SettlementService` ([settlement.py:26](application/services/settlement.py#L26))
- **Flow:** RiskApproved → submit → Fill → Settlement
- **Deps:** `core/`, `application/events.py`

### F7: Account Management (账户管理)
- **Scope:** `application/services/account.py`
- **Entry:** `AccountService` ([account.py:24](application/services/account.py#L24))
- **Flow:** SettlementEvent → position/balance update → PositionUpdatedEvent
- **Deps:** `core/`, `application/events.py`

### F8: Monitoring & P&L (监控与盈亏)
- **Scope:** `application/services/monitor.py`
- **Entry:** `MonitorService` ([monitor.py:37](application/services/monitor.py#L37))
- **Metrics:** NAV, realized P&L, drawdown, Sharpe, win rate
- **Deps:** `core/`, `application/events.py`

### F9: Reconciliation (对账)
- **Scope:** `application/services/reconcile.py`
- **Entry:** `ReconcileService` ([reconcile.py:94](application/services/reconcile.py#L94))
- **Flow:** exchange positions → compare → discrepancy report → disaster recovery
- **Deps:** `core/`, `application/events.py` (only for instrument/position types)

### F10: Adapters — Bus & Cache (基础设施)
- **Scope:** `adapters/bus/`, `adapters/cache/`
- **Entry:** `SyncEventBus` ([sync.py:27](adapters/bus/sync.py#L27)), `MemoryCache` ([memory.py:15](adapters/cache/memory.py#L15))
- **Deps:** `core/`

### F11: Adapters — Exchange (交易所)
- **Scope:** `adapters/exchange/`
- **Entry:** `PaperExchange` ([paper.py:32](adapters/exchange/paper.py#L32)), `BinanceFuturesExchange` ([binance.py:37](adapters/exchange/binance.py#L37))
- **Deps:** `core/`, `application/events.py`

### F12: Adapters — Feed (行情数据源)
- **Scope:** `adapters/feed/`
- **Entry:** `CsvFeed` ([csv_.py:44](adapters/feed/csv_.py#L44)), `BinanceWsFeed` ([websocket.py:38](adapters/feed/websocket.py#L38)), `BinanceUserDataStream` ([user_data.py:52](adapters/feed/user_data.py#L52))
- **Deps:** `core/`, `application/events.py`

### F13: Strategies (具体策略实现)
- **Scope:** `strategies/`
- **Entry:** `MomentumStrategy` ([momentum.py:35](strategies/momentum.py#L35)), `MeanReversionStrategy` ([mean_reversion.py:37](strategies/mean_reversion.py#L37)), `FundingRateArbStrategy` ([funding_rate_arb.py:37](strategies/funding_rate_arb.py#L37))
- **Deps:** `core/`

### F14: Environment (运行环境)
- **Scope:** `env/`
- **Entry:** `BacktestEnv` ([backtest.py:36](env/backtest.py#L36)), `LiveEnv` ([live.py:25](env/live.py#L25))
- **Deps:** `core/`

### F15: Configuration & Assembly (配置与装配)
- **Scope:** `config.py`, `container.py`, `config_live.yaml`
- **Entry:** `Config.from_yaml()` ([config.py:68](config.py#L68)), `build()` ([container.py:80](container.py#L80))
- **Deps:** All layers

### F16: Entry Points (入口)
- **Scope:** `run_backtest.py`, `run_live.py`
- **Entry:** `run_live.main()` ([run_live.py:109](run_live.py#L109)), `run_backtest.main()` ([run_backtest.py:51](run_backtest.py#L51))
- **Deps:** All layers

---

## Cross-Feature Dependencies

```
                    F15 (Config/Assembly)
                         |
        +-------+--------+--------+-------+-------+
        |       |        |        |       |       |
       F14     F10      F11      F12     F4-F9   F13
      (Env)  (Bus/     (Exch)   (Feed)  (App     (Strategies)
              Cache)                    Services)
        |       |        |        |       |       |
        +-------+--------+--------+-------+-------+
                         |
                        F1 (Core Domain)
                         |
                        F2 (Events)
```

All features depend on F1+F2. Application services (F4-F9) form a sequential processing chain. Adapters (F10-F12) implement ports from F1.
