# 05 — Configuration & Entry Points (F15+F16)

## Config Dataclass Hierarchy

```mermaid
flowchart TD
    subgraph YAML["config_live.yaml"]
        y_mode["mode: live"]
        y_bus["bus: backend: sync"]
        y_cache["cache: backend: memory"]
        y_exec["execution: exchange: binance, api_key, api_secret"]
        y_risk["risk: max_weight, max_leverage, drawdown, funding_rate"]
        y_acct["account: main_id, initial_usdt"]
        y_mon["monitor: warn_dd, critical_dd"]
        y_strat["strategies: [momentum, mean_reversion, funding_rate_arb]"]
    end

    subgraph PY["Config dataclasses (config.py)"]
        Config["Config<br/>config.py:57"]
        Config --> BusConfig["BusConfig<br/>config.py:15"]
        Config --> CacheConfig["CacheConfig<br/>config.py:21"]
        Config --> ExecutionConfig["ExecutionConfig<br/>config.py:28"]
        Config --> RiskConfig["RiskConfig<br/>config.py:35"]
        Config --> AccountConfig["AccountConfig<br/>config.py:44"]
        Config --> MonitorConfig["MonitorConfig<br/>config.py:50"]
        Config --> SL["list[dict] (strategies)"]
    end

    y_mode --> Config
    y_bus --> BusConfig
    y_cache --> CacheConfig
    y_exec --> ExecutionConfig
    y_risk --> RiskConfig
    y_acct --> AccountConfig
    y_mon --> MonitorConfig
    y_strat --> SL
```

## Container build() Assembly Order

```mermaid
flowchart TD
    A["build(cfg)<br/>container.py:80"] --> B["1. _make_bus → SyncEventBus<br/>container.py:94"]
    B --> C["2. _make_cache → MemoryCache<br/>container.py:95"]
    C --> D{"3. mode?"}
    D -->|live/paper| E["LiveEnv → WallClock<br/>container.py:99-100"]
    D -->|backtest| F["BacktestEnv → SimClock<br/>container.py:102"]
    E --> G["4. _make_exchange → Paper/Binance<br/>container.py:105"]
    F --> G
    G --> H["5. AccountService(bus,cache,usdt)<br/>container.py:108-112"]
    H --> I["6. make_ctx closure<br/>container.py:117-124"]
    I --> J{"7. cfg.strategies?"}
    J -->|yes| K["PluginLoader.load_all<br/>container.py:128-129"]
    J -->|no| L["MomentumStrategy fallback<br/>container.py:132-133"]
    K --> M["8. RiskPipeline(5 middleware)<br/>container.py:137-143"]
    L --> M
    M --> N["9. SignalService<br/>container.py:146-151"]
    N --> O["10. PortfolioService<br/>container.py:152-158"]
    O --> P["11. OMSService<br/>container.py:160-164"]
    P --> Q["12. portfolio.set_oms(oms)<br/>container.py:165"]
    Q --> R["13. RiskService + set_oms<br/>container.py:166-172"]
    R --> S["14. SettlementService<br/>container.py:173"]
    S --> T["15. MonitorService<br/>container.py:176-184"]
    T --> U["16. return System(bus,cache,clock,account,monitor,exchange)<br/>container.py:190-196"]
```

## run_live.py Startup Sequence

```mermaid
flowchart TD
    A["main()<br/>run_live.py:109"] --> B["parse_args()<br/>run_live.py:32"]
    B --> C["Config.from_yaml(args.config)<br/>run_live.py:114"]
    C --> D{"--paper / --dry-run?"}
    D --> E["build_instruments(symbols)<br/>run_live.py:41-70"]
    E --> F["container.build(cfg) → System<br/>run_live.py:137"]
    F --> G{"exchange == paper?"}
    G -->|no| H["startup_checks:<br/>1. ReconcileService.rebuild<br/>2. NAV >= 10 USDT<br/>3. Position snapshot<br/>run_live.py:73-106"]
    G -->|yes| I["skip checks"]
    H --> J["monitor.reset_baseline()<br/>run_live.py:144"]
    I --> J
    J --> K["signal(SIGINT/SIGTERM, shutdown)<br/>run_live.py:183-184"]
    K --> L{"exchange == binance?"}
    L -->|yes| M["BinanceUserDataStream<br/>run_live.py:191-196"]
    L -->|no| N["skip"]
    M --> O["BinanceWsFeed(bus,instr,...)<br/>run_live.py:204-210"]
    N --> O
    O --> P["feed.run() — BLOCKING<br/>run_live.py:215"]
    P -.->|Ctrl+C| Q["shutdown → reconcile EOD → report → exit<br/>run_live.py:154-181"]
```

## run_backtest.py Sequence

```mermaid
flowchart TD
    A["main()<br/>run_backtest.py:51"] --> B["gen_csv(n=300)<br/>run_backtest.py:29"]
    B --> C["synthetic_funding_rate<br/>run_backtest.py:43"]
    C --> D["Instrument('BTC-USDT-PERP')<br/>run_backtest.py:55"]
    D --> E["Config.backtest_default(usdt=10000, strategies=[3])<br/>run_backtest.py:62"]
    E --> F["container.build(cfg) → System<br/>run_backtest.py:74"]
    F --> G["bus.subscribe for Signal/Risk/Fill/Alert<br/>run_backtest.py:80-87"]
    G --> H["system.make_feed(csv, instr, funding_fn)<br/>run_backtest.py:89-93"]
    H --> I["feed.run() → 300 rows processed<br/>run_backtest.py:94"]
    I --> J["monitor.snapshot()<br/>run_backtest.py:96"]
    J --> K["Print performance report<br/>run_backtest.py:101-145"]
```

## config_live.yaml Key Values vs Defaults

| YAML Path | YAML Value | Python Default |
|-----------|-----------|----------------|
| `mode` | `live` | `backtest` |
| `execution.exchange` | `binance` | `paper` |
| `account.initial_usdt` | `10.0` | `10_000.0` |
| `risk.max_weight` | `0.10` | `0.10` |
| `risk.max_leverage` | `100` | `10` |
| `risk.max_drawdown` | `0.05` | `0.05` |
| `risk.max_funding_rate` | `0.003` | `0.003` |
| `strategies` | 3 strategies | `[]` (empty) |
