# crypto_quant 代码知识图谱

## 一、架构概览

六角架构（Hexagonal / Ports & Adapters），四层分层：

```
入口层 (Entry Points)
  ├── run_backtest.py      回测入口
  ├── run_live.py          实盘入口
  └── config.py            配置数据类

环境层 (Environment)
  ├── env/backtest.py      SimClock + BacktestEnv
  └── env/live.py          WallClock + LiveEnv

应用层 (Application)
  ├── strategy/base.py     Strategy 协议
  ├── strategy/loader.py   插件注册与加载
  ├── services/            领域服务
  │   ├── signal.py        SignalService
  │   ├── portfolio.py     PortfolioService
  │   ├── risk.py          RiskService
  │   ├── oms.py           OMSService
  │   ├── settlement.py    SettlementService
  │   ├── account.py       AccountService
  │   ├── monitor.py       MonitorService
  │   └── reconcile.py     ReconcileService
  ├── risk/                风控中间件链
  │   ├── pipeline.py      RiskPipeline + RiskMiddleware
  │   └── builtin.py       6个内置中间件
  ├── context.py           StrategyContext
  └── events.py            15种领域事件

核心层 (Core)
  ├── domain/              领域实体
  │   ├── event.py         Event 基类
  │   ├── instrument.py    Instrument + InstrumentKind
  │   ├── order.py         Order + Side/OrderType/OrderStatus/TimeInForce
  │   ├── position.py      Position + PositionSide/MarginMode
  │   ├── balance.py       Balance
  │   └── signal.py        Signal
  └── ports/               端口接口（Protocol）
      ├── bus.py           EventBusPort
      ├── cache.py         CachePort
      ├── clock.py         ClockPort
      ├── execution.py     ExecutionPort
      ├── account.py       AccountPort
      └── risk.py          RiskPort + RiskContext + RiskResult

适配器层 (Adapters)
  ├── bus/sync.py          SyncEventBus
  ├── cache/memory.py      MemoryCache
  ├── exchange/paper.py    PaperExchange
  ├── exchange/binance.py  BinanceFuturesExchange
  └── feed/
      ├── csv_.py          CsvFeed（回测）
      ├── websocket.py     BinanceWsFeed（实盘行情）
      └── user_data.py     BinanceUserDataStream（实盘订单）

策略层 (Strategies)
  ├── mixin.py             PriceBufferMixin
  ├── momentum.py          MomentumStrategy
  ├── mean_reversion.py    MeanReversionStrategy
  └── funding_rate_arb.py  FundingRateArbStrategy
```

## 二、核心事件流（数据处理管道）

```
[交易所适配器] ──TradeEvent/BookEvent/FundingRateEvent──► SignalService
                                                          │
                                                   路由到各 Strategy
                                                          │
                                                   返回 list[Signal]
                                                          │
                                                   发布 SignalEvent
                                                          ▼
                                                   PortfolioService
                                                   (置信度加权合并信号)
                                                          │
                                                   发布 TargetPositionEvent
                                                          ▼
                                                     RiskService
                                                   (构建Order + RiskContext)
                                                          │
                                                   RiskPipeline.check()
                                                          │
                                            ┌──通过──┤──拒绝──┐
                                            ▼                ▼
                                    RiskApprovedEvent   RiskRejectedEvent
                                            │
                                            ▼
                                        OMSService
                                   (提交到交易所适配器)
                                            │
                                   发布 OrderSubmittedEvent
                                            │
                                            ▼
                                     [PaperExchange/Binance]
                                            │
                                       发布 FillEvent
                                            │
                                            ▼
                                        OMSService
                                   (更新订单状态, 满仓时)
                                            │
                                   发布 OrderFilledEvent
                                            │
                                            ▼
                                     SettlementService
                                   (防腐层转换)
                                            │
                                   发布 SettlementEvent
                                            │
                                            ▼
                                      AccountService
                                   (更新持仓/余额)
                                            │
                            ┌───────────────┤───────────────┐
                            ▼                               ▼
                  PositionUpdatedEvent            BalanceUpdatedEvent
                            │
                            ▼
                       MonitorService
                  (PnL/回撤/Sharpe/告警)
```

## 三、端口-适配器映射

| 端口 (Protocol) | 适配器实现 | 用途 |
|---|---|---|
| EventBusPort | SyncEventBus | 同步事件总线(线程安全) |
| CachePort | MemoryCache | 内存缓存(支持TTL) |
| ClockPort | SimClock / WallClock | 回测模拟时钟 / 实盘系统时钟 |
| ExecutionPort | PaperExchange / BinanceFuturesExchange | 模拟交易所 / 币安U本位合约 |
| AccountPort | AccountService | 账户服务(持仓/余额/净额) |
| RiskPort | RiskPipeline | 风控中间件链 |

## 四、领域实体关系

```
Instrument (核心引用类型, frozen)
  ├── Order.instrument    (订单关联标的)
  ├── Position.instrument (持仓关联标的)
  └── Signal.instrument   (信号关联标的)

Event (基类, frozen, 三ID追踪)
  ├── event_id: 唯一标识
  ├── correlation_id: 业务链共享
  └── causation_id: 直接父事件

Order (frozen, 不可变更新)
  ├── Side: BUY/SELL (含 opposite 属性)
  ├── OrderType: MARKET/LIMIT/STOP_MARKET/STOP_LIMIT
  ├── OrderStatus: NEW→SUBMITTED→PART_FILLED→FILLED/CANCELLED/REJECTED
  └── TimeInForce: GTC/IOC/FOK/GTX

Position (mutable, 原地更新)
  ├── PositionSide: LONG/SHORT/NET
  └── MarginMode: CROSS/ISOLATED

Balance (mutable, 多币种)
  └── balances: dict[str, Decimal]

Signal (mutable, 分数[-1,1])
  ├── score > 0: 做多
  ├── score < 0: 做空
  └── score = 0: 平仓
```

## 五、15种领域事件分类

| 分类 | 事件 | 发布者 | 订阅者 |
|---|---|---|---|
| 行情 | TradeEvent | Feed适配器 | SignalService, MonitorService |
| 行情 | BookEvent | Feed适配器 | SignalService |
| 行情 | FundingRateEvent | Feed适配器/CsvFeed | SignalService |
| 信号 | SignalEvent | SignalService | PortfolioService |
| 组合 | TargetPositionEvent | PortfolioService | RiskService |
| 风控 | RiskApprovedEvent | RiskService | OMSService, MonitorService(拒绝时) |
| 风控 | RiskRejectedEvent | RiskService | MonitorService |
| 执行 | OrderSubmittedEvent | OMSService | PaperExchange |
| 执行 | FillEvent | Exchange适配器 | OMSService |
| 执行 | OrderFilledEvent | OMSService | SettlementService |
| 结算 | SettlementEvent | SettlementService | AccountService |
| 账户 | PositionUpdatedEvent | AccountService | MonitorService |
| 账户 | BalanceUpdatedEvent | AccountService | — |
| 监控 | PnLEvent | MonitorService | — |
| 监控 | AlertEvent | MonitorService | — |

## 六、风控中间件链

执行顺序（从外到内）：

1. **MinNotionalMiddleware** — 最小名义值检查（不过滤reduce_only）
2. **PositionLimitMiddleware** — 单标的仓位权重上限（默认10%）
3. **MaxLeverageMiddleware** — 杠杆上限（默认10x）
4. **DrawdownMiddleware** — 回撤上限（默认5%，80%预警）
5. **FundingRateMiddleware** — 资金费率上限（默认0.3%/8h，仅检查BUY）

模板方法模式：`reduce_only` 订单默认跳过检查（`check_reduce_only=True`），仅 MinNotional 和 OpenOrdersLimit 也检查平仓单。

## 七、策略体系

```
Strategy (Protocol)
  ├── name: str
  ├── version: str
  ├── on_trade(event, ctx) -> list[Signal]
  ├── on_book(event, ctx) -> list[Signal]
  ├── on_funding(event, ctx) -> list[Signal]
  ├── on_fill(event, ctx) -> None
  ├── on_start(ctx) -> None
  └── on_stop(ctx) -> None

BaseStrategy (默认no-op实现)
  └── 所有方法返回[]或None

PriceBufferMixin (滚动价格窗口)
  ├── _init_buffer(window)
  ├── _append_price(sym, price)
  ├── _is_warmed_up(sym) -> bool
  └── _prices_list(sym) -> list[Decimal]

MomentumStrategy (PriceBufferMixin + BaseStrategy)
  ├── window=20, scale=0.05, min_score=0.20
  └── on_trade: 收益率 / scale → score

MeanReversionStrategy (PriceBufferMixin + BaseStrategy)
  ├── window=20, z_entry=1.5, z_threshold=2.5
  ├── on_trade: z-score → 反向score
  └── on_funding: 费率 → 反向score

FundingRateArbStrategy (BaseStrategy only)
  ├── threshold=0.001, max_score=0.6
  └── on_funding: 资金费率套利信号
```

## 八、依赖注入容器 (container.py)

`build(cfg: Config) -> System` 组装流程：

1. 基础设施：`_make_bus(cfg)` → `_make_cache(cfg)`
2. 环境选择：live/paper → `LiveEnv()`，否则 `BacktestEnv()`
3. 交易所适配器：`_make_exchange(cfg, bus, cache, initial_usdt)`
4. AccountService（订阅 SettlementEvent）
5. StrategyContext 工厂闭包 `make_ctx`
6. 策略加载：`PluginLoader.load_all(cfg.strategies)`
7. 风控管道：5个中间件按序组装
8. 应用服务：SignalService → PortfolioService → RiskService → OMSService → SettlementService → MonitorService
9. 延迟注入：`PortfolioService.set_oms(oms)` + `RiskService.set_oms(oms)`（打破循环依赖）

## 九、配置体系 (config.py)

```
Config (根配置)
  ├── mode: "backtest" | "paper" | "live"
  ├── bus: BusConfig (backend, kafka_servers)
  ├── cache: CacheConfig (backend, redis_host/port)
  ├── execution: ExecutionConfig (exchange, api_key/secret)
  ├── risk: RiskConfig (max_weight, max_leverage, max_drawdown, max_funding_rate)
  ├── account: AccountConfig (main_id, initial_usdt)
  ├── monitor: MonitorConfig (warn_drawdown, critical_drawdown)
  └── strategies: list[dict] (策略配置列表)
      ├── from_yaml(path) 类方法
      └── backtest_default(...) 快捷方法
```

## 十、关键设计决策

- **Decimal 全局使用**：所有金融量（价格/数量/PnL/名义值）使用 `Decimal`，不用 `float`
- **不可变值对象**：Event/Instrument/Order 使用 `frozen=True`，状态变更通过 `dataclasses.replace()`
- **可变实体**：Position/Balance 原地更新（性能考虑）
- **结构化子类型**：端口使用 `Protocol`，适配器无需显式继承
- **事件因果链**：三ID模型（event_id / correlation_id / causation_id）
- **防腐层**：SettlementService 解耦 OMS 与 AccountService
- **延迟注入**：OMSService 通过 `set_oms()` 打破 PortfolioService/RiskService 循环依赖
- **策略隔离**：策略仅通过 StrategyContext 与外界交互，无法直接访问适配器或服务
