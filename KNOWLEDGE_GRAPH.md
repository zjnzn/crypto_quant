# crypto_quant 完整逻辑文档

## 一、系统架构

六角架构（Hexagonal / Ports & Adapters），事件驱动：

```
行情(Feed) → SignalService → PortfolioService → RiskService → OMSService → Exchange
                                        ↑                              ↓
                                   AccountService ←── SettlementService ←── OMS(FillEvent)
                                        ↓
                                   MonitorService
```

## 二、完整数据流（含公式）

### 2.1 行情接入 → 信号生成

**输入**：TradeEvent(price, qty, buyer_maker) / BookEvent(bid/ask) / FundingRateEvent(rate)

**SignalService** 处理：
1. 更新价格缓存：`cache["price:{sym}"] = event.price`（TTL 1小时）
2. 追加价格窗口：`cache["prices_window:{sym}"].append(price)`（最多500根）
3. 分发事件到所有策略

**策略计算**：

#### MomentumStrategy（动量策略）
```
参数：window=20, scale=0.05, min_score=0.20

1. 追加价格到 deque 滚动窗口（maxlen=window）
2. 预热检查：窗口满 window 根才产出信号
3. 计算窗口期收益率：
   ret = prices[-1] / prices[0] - 1
4. 计算信号分数（线性缩放，限幅 [-1, 1]）：
   score = clamp(ret / scale, -1, 1)
5. 信号过滤：
   if |score| < min_score → 不产出信号
6. 计算置信度：
   confidence = min(1.0, |ret| / (scale * 2))

示例：价格从 0.1000 涨到 0.1006
  ret = 0.1006/0.1000 - 1 = +0.006
  score = 0.006/0.05 = +0.12  ← 低于 min_score=0.20，不产出
```

#### MeanReversionStrategy（均值回归策略）
```
参数：window=20, z_entry=2.0, z_threshold=3.0

1. 追加价格到 deque 滚动窗口（maxlen=window）
2. 预热检查：窗口满 window 根才产出信号
3. 计算 Z-score：
   mean = avg(prices)
   std  = sqrt(Σ(pi - mean)² / n)
   z    = (current_price - mean) / std
4. 信号过滤：
   if |z| < z_entry → 不产出信号
5. 计算信号分数（反向，限幅 [-1, 1]）：
   score = clamp(-z / z_threshold, -1, 1)
6. 计算置信度：
   confidence = min(1.0, |z| / (z_threshold * 1.5))

on_funding 信号（辅助）：
  if |rate| < 0.001 → 不产出
  score = clamp(-rate / 0.003, -1, 1)
  confidence = min(1.0, |rate| / 0.003)

示例：价格均值0.1000，当前0.1006，std=0.0003
  z = (0.1006 - 0.1000) / 0.0003 = 2.0  ← 刚到 z_entry
  score = -2.0/3.0 = -0.667  ← 做空信号
```

#### FundingRateArbStrategy（资金费率套利）
```
参数：threshold=0.001, max_score=0.6

1. 只在 on_funding 中产出信号
2. 信号过滤：
   if |rate| < threshold → 不产出
3. 计算信号分数（反向，限幅 [-max_score, max_score]）：
   score = clamp(-rate / 0.003, -max_score, max_score)
4. 计算置信度：
   confidence = min(1.0, |rate| / 0.003)

示例：rate = +0.003（0.3%/8h，多头付费给空头）
  score = -0.003/0.003 = -1.0 → clamp → -0.6  ← 做空
  confidence = 1.0
```

### 2.2 信号合并 → 目标仓位

**PortfolioService** 订阅 SignalEvent，处理流程：

#### A. 多策略信号合并
```
1. 按 (symbol, strategy_id) 存储每个策略的最新信号
2. 取出同标的的所有策略信号 [s1, s2, ...]
3. 置信度加权平均：
   combined_score = Σ(score_i × confidence_i) / Σ(confidence_i)

示例：
  momentum:       score=+0.6  confidence=0.8  → weighted=+0.48
  mean_reversion:  score=-0.3  confidence=0.5  → weighted=-0.15
  combined = (0.48 - 0.15) / (0.8 + 0.5) = +0.254
```

#### B. 目标仓位计算
```
1. 计算账户净值：
   nav = AccountService.get_nav_usdt(account_id)

2. 判断信号强度：
   abs_score = min(|combined_score|, 1.0)
   if abs_score < min_score → target_size = 0, target_side = BUY（空仓）

3. 计算目标持仓量：
   target_size = abs_score × max_weight × nav / price
   target_size = instrument.round_qty(target_size)

   说明：max_weight 是仓位权重上限
   例：abs_score=0.254, max_weight=0.35, nav=11.93, price=0.1001
   target_size = 0.254 × 0.35 × 11.93 / 0.1001 = 10.6
   DOGEUSDT lot_size=1 → round_qty(10.6) = 10（或11，取决于取整方向）

4. 确定方向：
   target_side = BUY if combined_score > 0 else SELL

5. 空头检查：
   if not allow_short and target_side == SELL:
       target_size = 0, target_side = BUY
```

#### C. 当前净仓位计算
```
1. 从 AccountService 读取当前持仓：
   cur_pos = account.get_position(account_id, symbol)
   current_size = cur_pos.size  （0 if 无仓位）
   current_side = cur_pos.side  （None if 无仓位）

2. 计入在途订单增量：
   pending_delta = oms.compute_pending_delta(symbol)
   # BUY → +remaining_qty，SELL → -remaining_qty

3. 计算当前净仓位（带符号）：
   if current_side is None:
       net_position = pending_delta
   elif current_side == LONG:
       net_position = current_size + pending_delta    # 正数
   else:  # SHORT
       net_position = -(current_size + pending_delta)  # 负数

4. 计算目标净仓位（带符号）：
   target_position = target_size if target_side == BUY else -target_size

5. 变动量检查：
   delta = |target_position - net_position|
   if delta < lot_size → 不发布事件（变动太小）

6. 发布 TargetPositionEvent
```

### 2.3 目标仓位 → 订单构建

**RiskService** 订阅 TargetPositionEvent：

```
1. 直接计算 delta：
   delta = target_position - net_position
   if delta == 0 → return

2. 确定订单方向和数量：
   if delta > 0 → BUY delta（开多/加多/空翻多）
   if delta < 0 → SELL |delta|（开空/加空/多翻空）

3. 判断 reduce_only（纯减仓/平仓，不跨越零点）：
   is_reducing = False
   if current_pos ≠ 0 and delta ≠ 0:
       if (current_pos > 0 and delta < 0) or (current_pos < 0 and delta > 0):
           # delta 方向与 current 相反 → 在减小仓位
           if |delta| ≤ |current_pos|:
               # delta 不超过当前仓位 → 不跨越零点 → 纯减仓
               is_reducing = True

4. 取整和最小量检查：
   order_qty = instrument.round_qty(order_qty)
   if order_qty < lot_size → return

5. 构建订单：
   Order(side, qty, order_type=MARKET, reduce_only=is_reducing)
```

**所有场景**：

| 场景 | current | target | delta | 操作 | reduce_only | 说明 |
|---|---|---|---|---|---|---|
| 开多 | 0 | +1 | +1 | BUY 1 | 否 | 从空仓到多头 |
| 加多 | +0.5 | +1 | +0.5 | BUY 0.5 | 否 | 同向加仓 |
| 减多 | +1 | +0.8 | -0.2 | SELL 0.2 | 是 | 纯减仓 |
| 平多 | +0.8 | 0 | -0.8 | SELL 0.8 | 是 | 纯平仓 |
| 多翻空 | +0.8 | -0.2 | -1.0 | SELL 1.0 | 否 | 含开空部分 |
| 开空 | 0 | -1 | -1 | SELL 1 | 否 | 从空仓到空头 |
| 加空 | -0.3 | -0.8 | -0.5 | SELL 0.5 | 否 | 同向加仓 |
| 减空 | -0.8 | -0.3 | +0.5 | BUY 0.5 | 是 | 纯减仓 |
| 平空 | -0.5 | 0 | +0.5 | BUY 0.5 | 是 | 纯平仓 |
| 空翻多 | -0.2 | +0.3 | +0.5 | BUY 0.5 | 否 | 含开多部分 |

**币安单向持仓模式**：方向翻转一步完成。SELL 1.0 自动 = 先平多0.8 + 开空0.2。

### 2.4 风控检查

**RiskPipeline** 责任链，5个中间件从外到内执行：

```
Order → MinNotional → PositionLimit → MaxLeverage → Drawdown → FundingRate → 通过/拒绝
```

#### 1. MinNotionalMiddleware（最小名义值）
```
notional = qty × est_price
if notional < instrument.min_notional → hard 拒绝
check_reduce_only = False（平仓单也需满足）

est_price 解析：order.limit_price > cache["price:{sym}"] > 拒绝
```

#### 2. PositionLimitMiddleware（仓位权重上限）
```
1. 计算新仓位：
   delta = qty if BUY else -qty
   pending_delta = oms.compute_pending_delta(symbol)
   new_size = current_size + pending_delta + delta

2. 计算权重：
   weight = |new_size × est_price| / nav

3. 小账户容差：
   min_weight_needed = instrument.min_notional / nav
   if min_weight_needed > max_weight:
       max_allowed = max_weight × 1.5
   else:
       max_allowed = max_weight

4. 平仓方向跳过检查：
   is_reducing = (LONG + SELL) or (SHORT + BUY)
   if is_reducing → 跳过权重检查

5. if not is_reducing and weight > max_allowed → hard 拒绝

示例（14U 账户，DOGEUSDT）：
  nav = 11.93, max_weight = 0.35, min_notional = 5
  min_weight_needed = 5 / 11.93 = 0.419 > 0.35 → 启用容差
  max_allowed = 0.35 × 1.5 = 0.525
  BUY 50 DOGE @ 0.1001 → weight = 50 × 0.1001 / 11.93 = 0.420 < 0.525 ✓
```

#### 3. MaxLeverageMiddleware（最大杠杆）
```
leverage = current_position.leverage  （从 AccountService 读取）
allowed = min(instrument.max_leverage, global_max)
if leverage > allowed → hard 拒绝
```

#### 4. DrawdownMiddleware（回撤熔断）
```
drawdown = cache["drawdown:{account_id}"]  （由 MonitorService 写入）

if drawdown > max_drawdown → hard 拒绝（禁止开仓）
elif drawdown > max_drawdown × 0.8 → 软告警（放行但记录）
else → 放行

示例：max_drawdown = 0.05
  drawdown = 0.06 → 拒绝（超过5%）
  drawdown = 0.042 → 软告警（超过4% = 5%×80%）
  drawdown = 0.03 → 正常放行
```

#### 5. FundingRateMiddleware（资金费率）
```
rate = cache["funding:{symbol}"]

if SELL → 直接放行（做空收取资金费）
if BUY and rate > max_rate → soft 拒绝（开多需付费）

示例：max_rate = 0.003
  rate = 0.005 + BUY → soft 拒绝（资金费过高，开多不划算）
  rate = 0.005 + SELL → 放行（做空收钱）
```

**风控结果**：
- 通过 → RiskApprovedEvent（可能含 amended_order）
- soft 拒绝 → 仍发布 RiskApprovedEvent + 警告日志
- hard 拒绝 → RiskRejectedEvent，链路终止

### 2.5 订单执行

**OMSService** 订阅 RiskApprovedEvent：

```
1. 写入缓存：cache["order:{order_id}"] = order
2. 加入在途：_pending[order_id] = order
3. 提交到交易所：exchange.submit(order) → exchange_order_id
4. 注册反查：cache["exorder:{exchange_id}"] = order_id
5. 更新状态：order.status = SUBMITTED
6. 发布 OrderSubmittedEvent
```

**FillEvent 处理**（交易所回报成交）：

```
1. 通过 exorder 反查定位 order_id
2. 滚动计算成交均价：
   prev_cost = filled_qty × avg_fill_price
   new_filled = filled_qty + fill_qty
   new_avg = (prev_cost + fill_qty × fill_price) / new_filled
3. 判断完全成交：
   if new_filled >= qty × 0.9999 → FILLED（容忍浮点误差）
   else → PART_FILLED
4. 终态订单从 _pending 移除
5. FILLED 时发布 OrderFilledEvent
```

### 2.6 结算

**SettlementService** 订阅 OrderFilledEvent → 发布 SettlementEvent：

```
SettlementEvent(
    account_id, instrument,
    settled_qty = order.filled_qty,
    side        = order.side,
    avg_price   = event.avg_price,
    net_pnl     = 0,          ← AccountService 自行计算
    commission  = event.commission,
)
```

### 2.7 账号更新

**AccountService** 订阅 SettlementEvent：

#### A. USDT 变动
```
BUY:  usdt_delta = -(settled_qty × avg_price + commission)  # 花钱买
SELL: usdt_delta = +(settled_qty × avg_price - commission)  # 卖收钱
```

#### B. 持仓更新（_apply_buy）

```
case 无仓位 → 开多(LONG)：
  new_side = LONG, new_size = qty, new_entry = price

case LONG → 加多：
  new_size = old_size + qty
  new_entry = (old_size × old_entry + qty × price) / new_size  # 均价加权

case SHORT → 平空（可能同时开多）：
  close_qty = min(qty, short_size)
  realized_pnl = close_qty × (entry_price - price)  # 空头盈亏 = 入场价 - 平仓价
  remaining_short = short_size - close_qty
  excess_buy = qty - close_qty

  if remaining_short ≤ 0 and excess_buy > 0:
      # 空头全部平掉 + 多余开多
      → LONG(excess_buy, entry=price), pnl=realized_pnl
  elif remaining_short ≤ 0:
      # 刚好全部平掉
      → LONG(0, entry=0), pnl=realized_pnl
  else:
      # 部分平空
      → SHORT(remaining_short, entry=old_entry), pnl=realized_pnl
```

#### C. 持仓更新（_apply_sell）

```
case 无仓位 → 开空(SHORT)：
  new_side = SHORT, new_size = qty, new_entry = price

case SHORT → 加空：
  new_size = old_size + qty
  new_entry = (old_size × old_entry + qty × price) / new_size  # 均价加权

case LONG → 平多（可能同时开空）：
  close_qty = min(qty, long_size)
  realized_pnl = close_qty × (price - entry_price)  # 多头盈亏 = 平仓价 - 入场价
  remaining_long = long_size - close_qty
  excess_sell = qty - close_qty

  if remaining_long ≤ 0 and excess_sell > 0:
      # 多头全部平掉 + 多余开空
      → SHORT(excess_sell, entry=price), pnl=realized_pnl
  elif remaining_long ≤ 0:
      # 刚好全部平掉
      → LONG(0, entry=0), pnl=realized_pnl
  else:
      # 部分平多
      → LONG(remaining_long, entry=old_entry), pnl=realized_pnl
```

#### D. NAV 计算
```
nav = USDT余额
    + Σ(多头 size × mark_price)   # 多头：资产
    - Σ(空头 size × mark_price)   # 空头：负债

mark_price = cache["price:{sym}"] or entry_price

示例（持有50 DOGE多头）：
  USDT = 6.93
  多头 = 50 × 0.1001 = 5.005
  NAV = 6.93 + 5.005 = 11.935
```

#### E. 未实现盈亏
```
Position.calc_unrealized_pnl(mark_price):
  LONG:  size × (mark_price - entry_price)
  SHORT: size × (entry_price - mark_price)
```

### 2.8 监控

**MonitorService**：

```
1. 已实现盈亏：逐笔累加 PositionUpdatedEvent.realized_pnl
2. 总盈亏：total = nav - initial_nav
3. 未实现盈亏：unrealized = total - realized_pnl
4. 回撤计算：
   high_watermark = max(high_watermark, nav)  # 历史最高净值
   drawdown = (high_watermark - nav) / high_watermark
   cache["drawdown:{account_id}"] = drawdown  # 供风控中间件读取
5. 回撤告警：
   drawdown ≥ critical_dd → critical 告警（5%）
   drawdown ≥ warn_dd → warn 告警（3%）
   drawdown < warn_dd × 0.5 → 重置告警状态
6. Sharpe 比率：
   每笔交易相对收益率：ret = (nav - prev_nav) / prev_nav
   sharpe = mean(returns) / std(returns) × sqrt(8760)
   至少4笔交易才有统计意义
7. 胜率：
   win_rate = wins / (wins + losses)
   pnl > 0.01 → win, pnl < -0.01 → loss, else → breakeven
```

## 三、配置体系

### config_live.yaml 当前参数

```yaml
mode: live

portfolio:
  min_score: 0.30    # 信号合并后最低分数阈值

risk:
  max_weight:       0.35  # 仓位权重上限（小账户容差1.5x → 实际0.525）
  max_leverage:     20    # 全局最大杠杆
  max_drawdown:     0.05  # 5% 回撤熔断
  max_funding_rate: 0.003 # 0.3%/8h 资金费率上限

strategies:
  momentum:
    window: 20, scale: 0.05, min_score: 0.20
  mean_reversion:
    window: 20, z_entry: 2.0, z_threshold: 3.0
  funding_rate_arb:
    threshold: 0.001, max_score: 0.6
```

## 四、关键数值示例（DOGEUSDT 实盘）

```
账户：USDT余额=6.93，持有50 DOGE多头@0.1001
NAV = 6.93 + 50 × 0.1001 = 11.935
max_weight = 0.35，小账户容差 max_allowed = 0.525

信号产出（假设价格从0.1000涨到0.1006）：
  momentum:       ret=+0.006, score=+0.12, confidence=0.06  ← 低于min_score=0.20
  mean_reversion: z=+2.0, score=-0.667, confidence=0.444

假设只有 mean_reversion 产出信号：
  combined_score = -0.667
  abs_score = 0.667 > min_score=0.30 ✓

  target_size = 0.667 × 0.35 × 11.935 / 0.1006 = 27.8
  round_qty → 27（或28）

  target_side = SELL（combined_score < 0）

当前净仓位：net_position = +50（多头50）
目标净仓位：target_position = -27（空头27）

delta = -27 - 50 = -77
→ SELL 77 DOGE, reduce_only = False（跨越零点）

币安执行：先平多50，再开空27 → 一步完成
```

## 五、数据流向图

```
WebSocket aggTrade ──TradeEvent──→ SignalService
                                       │ _append_price → cache["prices_window"]
                                       │ cache["price:{sym}"] = price
                                       │ strategy.on_trade() → list[Signal]
                                       │ → SignalEvent
                                       ▼
                                 PortfolioService
                                       │ 合并信号 → combined_score
                                       │ target_size = score × weight × nav / price
                                       │ net_position = signed(current + pending)
                                       │ target_position = signed(target)
                                       │ delta检查
                                       │ → TargetPositionEvent
                                       ▼
                                  RiskService
                                       │ delta = target - current
                                       │ BUY if delta>0, SELL if delta<0
                                       │ reduce_only = (纯减仓 & 不跨越零点)
                                       │ → Order → RiskPipeline.check()
                                       ▼
                                  RiskPipeline
                                       │ MinNotional: qty×price ≥ 5
                                       │ PositionLimit: weight ≤ 52.5%
                                       │ MaxLeverage: leverage ≤ 20
                                       │ Drawdown: dd ≤ 5%
                                       │ FundingRate: rate ≤ 0.3%
                                       │ → RiskApprovedEvent / RiskRejectedEvent
                                       ▼
                                  OMSService
                                       │ exchange.submit(order) → exchange_id
                                       │ → OrderSubmittedEvent
                                       ▼
                              Binance / PaperExchange
                                       │ → FillEvent
                                       ▼
                                  OMSService
                                       │ 滚动均价，状态更新
                                       │ FILLED → OrderFilledEvent
                                       ▼
                                 SettlementService
                                       │ 防腐层转换
                                       │ → SettlementEvent
                                       ▼
                                 AccountService
                                       │ _apply_buy / _apply_sell
                                       │ USDT变动 + 持仓更新
                                       │ → PositionUpdatedEvent
                                       │ → BalanceUpdatedEvent
                                       ▼
                                  MonitorService
                                       │ PnL / 回撤 / Sharpe / 胜率
                                       │ cache["drawdown:{id}"] = dd
                                       │ → PnLEvent / AlertEvent
```

## 六、关键设计约束

1. **delta 一步计算**：`delta = target_position - net_position`，方向翻转一步完成
2. **reduce_only 仅限纯减仓**：不跨越零点时设 reduce_only，方向翻转为 False
3. **小账户容差**：min_notional/nav > max_weight 时，允许 1.5× max_weight
4. **在途订单追踪**：pending_delta 计入 net_position，防止重复下单
5. **币安单向持仓**：不设 positionSide 参数，SELL 自动先平多再开空
6. **最小名义值**：由 MinNotionalMiddleware 在风控层检查
7. **Decimal 全局**：所有金融量使用 Decimal
8. **不可变值对象**：Event/Order frozen=True，通过 with_update 产生新实例
9. **三ID事件追踪**：event_id / correlation_id / causation_id
