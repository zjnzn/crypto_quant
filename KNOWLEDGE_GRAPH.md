# crypto_quant 代码知识图谱

## 一、架构概览

六角架构（Hexagonal / Ports & Adapters），四层分层：

```text
入口层 (Entry Points)
  ├── run_backtest.py      回测入口
  ├── run_live.py          实盘入口（Instrument DB: BTC/ETH/SOL/DOGE/XRP/ADA/DOT/LTC）
  └── config.py            配置数据类

环境层 (Environment)
  ├── env/backtest.py      SimClock + BacktestEnv
  └── env/live.py          WallClock + LiveEnv

应用层 (Application)
  ├── strategy/base.py     Strategy 协议 + BaseStrategy（no-op 基类）
  ├── strategy/loader.py   PluginRegistry + PluginLoader
  ├── services/
  │   ├── signal.py        SignalService
  │   ├── portfolio.py     PortfolioService（净仓模式）
  │   ├── risk.py          RiskService（净仓模式订单构建）
  │   ├── oms.py           OMSService
  │   ├── settlement.py    SettlementService（防腐层）
  │   ├── account.py       AccountService（双向持仓）
  │   ├── monitor.py       MonitorService
  │   └── reconcile.py     ReconcileService
  ├── risk/
  │   ├── pipeline.py      RiskPipeline + RiskMiddleware
  │   └── builtin.py       6个内置中间件（含小账户容差）
  ├── context.py           StrategyContext
  └── events.py            领域事件（含 TargetPositionEvent 净仓字段）

核心层 (Core)
  ├── domain/
  │   ├── event.py         Event 基类（三ID追踪）
  │   ├── instrument.py    Instrument + InstrumentKind
  │   ├── order.py         Order + Side/OrderType/OrderStatus/TimeInForce
  │   ├── position.py      Position + PositionSide(LONG/SHORT/NET) + MarginMode
  │   ├── balance.py       Balance
  │   └── signal.py        Signal
  └── ports/               端口接口（Protocol）
      ├── bus.py / cache.py / clock.py / execution.py / account.py / risk.py

适配器层 (Adapters)
  ├── bus/sync.py          SyncEventBus（线程安全，RLock）
  ├── cache/memory.py      MemoryCache（TTL支持）
  ├── exchange/paper.py    PaperExchange（模拟，1-tick滑点）
  ├── exchange/binance.py  BinanceFuturesExchange（HMAC签名，listenKey管理）
  └── feed/
      ├── csv_.py          CsvFeed（回测，支持资金费率回调）
      ├── websocket.py     BinanceWsFeed（aggTrade + bookTicker）
      └── user_data.py     BinanceUserDataStream（ORDER_TRADE_UPDATE）

策略层 (Strategies)
  ├── mixin.py             PriceBufferMixin（deque滚动窗口）
  ├── momentum.py          MomentumStrategy（收益率/scale → score）
  ├── mean_reversion.py    MeanReversionStrategy（z-score反向 + 资金费率）
  └── funding_rate_arb.py  FundingRateArbStrategy（资金费率套利）
```

## 二、核心事件流（数据处理管道）

```text
[Feed适配器] ──TradeEvent/BookEvent/FundingRateEvent──► SignalService
                                                         │ 路由到各 Strategy
                                                         │ 返回 list[Signal]
                                                         ▼
                                                   PortfolioService
                                          (置信度加权合并 → 净仓模式计算)
                                          net_position / target_position
                                                         │
                                                   发布 TargetPositionEvent
                                                         ▼
                                                    RiskService
                                              (净仓模式订单构建)
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
                                          ▼
                               [PaperExchange/Binance]
                                          │ 发布 FillEvent
                                          ▼
                                      OMSService
                                 (更新订单状态，满仓时)
                                          │ 发布 OrderFilledEvent
                                          ▼
                                   SettlementService
                                   (防腐层转换)
                                          │ 发布 SettlementEvent
                                          ▼
                                    AccountService
                                 (双向持仓更新/余额)
                                          │
                         ┌────────────────┤────────────────┐
                         ▼                                  ▼
               PositionUpdatedEvent              BalanceUpdatedEvent
                         │
                         ▼
                    MonitorService
               (PnL/回撤/Sharpe/告警)
```

## 三、净仓模式（核心设计）

**PortfolioService** 计算净仓位（带符号）：

```python
# 多头为正，空头为负，0为空仓
net_position    = +size (LONG) | -size (SHORT) | 0 (空仓)
target_position = +size (目标多头) | -size (目标空头) | 0 (目标空仓)
```

**RiskService** 根据净仓位构建订单（避免双向持仓锁仓）：

| 场景 | 操作 |
| --- | --- |
| target=0 | 平掉所有仓位（reduce_only=True） |
| current=0 | 直接开仓 |
| 同方向，\|T\|>\|C\| | 加仓（reduce_only=False） |
| 同方向，\|T\|<\|C\| | 减仓（reduce_only=True） |
| **方向相反** | **先平仓（reduce_only=True），下一信号再开反向仓** |

**关键约束**：方向翻转分两步执行，避免双向持仓锁仓。

## 四、TargetPositionEvent 字段

```python
@dataclass(frozen=True)
class TargetPositionEvent(Event):
    account_id:      str               = ""
    instrument:      Instrument        = None
    target_size:     Decimal           = 0   # 目标持仓量（绝对值）
    target_side:     Side              = Side.BUY  # BUY=多头, SELL=空头
    current_size:    Decimal           = 0   # 当前持仓量（绝对值）
    current_side:    PositionSide|None = None
    net_position:    Decimal           = 0   # 当前净仓位（带符号）
    target_position: Decimal           = 0   # 目标净仓位（带符号）
```

## 五、AccountService 双向持仓逻辑

```text
BUY 成交：
  无仓位 → 开多（LONG）
  多头   → 加多（均价加权）
  空头   → 平空（realized_pnl = close_qty * (entry - price)）
           若 BUY qty > 空头 size → 多余部分开多

SELL 成交：
  无仓位 → 开空（SHORT）
  空头   → 加空（均价加权）
  多头   → 平多（realized_pnl = close_qty * (price - entry)）
           若 SELL qty > 多头 size → 多余部分开空

NAV 计算：
  nav = USDT余额
      + Σ(多头 size × mark_price)   # 资产
      - Σ(空头 size × mark_price)   # 负债

USDT delta：
  BUY：  -(qty × price + commission)  # 花钱买
  SELL： +(qty × price - commission)  # 卖收钱
```

## 六、风控中间件链

执行顺序（从外到内）：

1. **MinNotionalMiddleware** — 名义值 >= instrument.min_notional（不过滤reduce_only）
2. **PositionLimitMiddleware** — 仓位权重 <= max_weight（含小账户容差；平仓方向跳过检查）
3. **MaxLeverageMiddleware** — 杠杆 <= min(instrument.max_leverage, global_max)
4. **DrawdownMiddleware** — 日内回撤 <= max_drawdown（80%预警）
5. **FundingRateMiddleware** — 资金费率 <= max_rate（仅检查BUY开仓）

**关键**：`check_reduce_only=True`（默认）的中间件对 reduce_only 订单直接放行。

## 七、端口-适配器映射

| 端口 | 适配器 | 备注 |
| --- | --- | --- |
| EventBusPort | SyncEventBus | RLock，handler异常不阻断后续 |
| CachePort | MemoryCache | TTL，monotonic时钟 |
| ClockPort | SimClock/WallClock | 回测/实盘 |
| ExecutionPort | PaperExchange/BinanceFuturesExchange | 模拟/实盘 |
| AccountPort | AccountService | 双向持仓 |
| RiskPort | RiskPipeline | 责任链 |

## 八、策略体系

```text
Strategy (Protocol, runtime_checkable)
  name/version + on_trade/on_book/on_funding/on_fill/on_start/on_stop

BaseStrategy (no-op 基类)
  所有方法返回 [] 或 None

PriceBufferMixin
  _init_buffer(window) / _append_price / _is_warmed_up / _prices_list

MomentumStrategy(PriceBufferMixin, BaseStrategy)
  window=20, scale=0.05, min_score=0.20
  score = ret/scale, confidence = abs(ret)/(scale*2)

MeanReversionStrategy(PriceBufferMixin, BaseStrategy)
  window=20, z_entry=1.5, z_threshold=2.5
  score = -z/z_threshold（反向），on_funding 也产生信号

FundingRateArbStrategy(BaseStrategy)
  threshold=0.001, max_score=0.6
  score = -rate/0.003（费率高→做空）
```

## 九、依赖注入容器 (container.py)

`build(cfg: Config) -> System` 组装流程：

1. 基础设施：bus → cache
2. 环境：live/paper → LiveEnv，否则 BacktestEnv
3. 交易所适配器
4. AccountService（订阅 SettlementEvent）
5. make_ctx 工厂闭包
6. 策略加载（PluginLoader）
7. 风控管道（5个中间件）
8. 应用服务链（Signal→Portfolio→Risk→OMS→Settlement→Monitor）
9. 延迟注入：`set_oms()` 打破循环依赖

## 十、实盘配置要点（config_live.yaml）

```yaml
risk:
  max_weight: 0.35    # 小账户(14U): 5U最小名义值/14.72≈34%
  max_leverage: 100
  max_drawdown: 0.05
  max_funding_rate: 0.003
```

**Instrument DB（run_live.py）**：

| 标的 | lot_size | min_notional | max_leverage |
| --- | --- | --- | --- |
| BTCUSDT | 0.001 | 5 | 125 |
| ETHUSDT | 0.001 | 5 | 100 |
| SOLUSDT | 0.1 | 5 | 50 |
| DOGEUSDT | 1 | 5 | 50 |
| XRPUSDT | 0.1 | 5 | 50 |
| ADAUSDT | 1 | 1 | 50 |
| DOTUSDT | 0.1 | 5 | 50 |
| LTCUSDT | 0.01 | 0.1 | 75 |

## 十一、关键设计决策

- **Decimal 全局**：所有金融量使用 Decimal，不用 float
- **不可变值对象**：Event/Instrument/Order frozen=True
- **可变实体**：Position/Balance 原地更新
- **结构化子类型**：端口用 Protocol，适配器无需继承
- **三ID事件追踪**：event_id / correlation_id / causation_id
- **防腐层**：SettlementService 解耦 OMS 与 AccountService
- **净仓模式**：PortfolioService 计算带符号净仓位，RiskService 分步执行方向翻转
- **小账户容差**：PositionLimitMiddleware 在 min_notional/nav > max_weight 时允许1.5x超限
- **策略隔离**：策略仅通过 StrategyContext 交互

## 十二、已知问题和注意事项

- **方向翻转分两步**：多→空需要两个信号周期（第一个平多，第二个开空），策略需要持续发出信号
- **USDT delta 计算**：开空时 SELL 收到 USDT，平空时 BUY 花出 USDT，与多头方向相反
- **PositionSide vs Side**：PositionSide(LONG/SHORT) 用于持仓方向，Side(BUY/SELL) 用于订单方向，不可混用
- **reduce_only 语义**：平仓订单设 reduce_only=True，币安不检查最小名义值；开仓订单 reduce_only=False
- **币安最小名义值**：所有合约统一 5 USDT（除 ADAUSDT=1, LTCUSDT=0.1）
