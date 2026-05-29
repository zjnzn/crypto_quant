# 03 — Application Services Pipeline (F4-F9)

## MAIN PIPELINE: MarketEvent → PnLEvent

```mermaid
flowchart TD
    ME["MarketEvent (Trade/Book/FundingRate)"] --> SS

    subgraph F4_Signal["F4: SignalService"]
        SS["_on_trade/book/funding<br/>signal.py:67-92"]
        D["_dispatch → strategy.method(event, ctx)<br/>signal.py:96-106"]
        PS["_publish_signal → SignalEvent<br/>signal.py:108-117"]
        SS --> D --> PS
    end

    PS --> SE["SignalEvent"]

    subgraph F4_PF["F4: PortfolioService"]
        PO["_on_signal<br/>portfolio.py:68"]
        FU["Signal fusion: Σ(score×conf)/Σconf<br/>portfolio.py:84-89"]
        TC["Target: |score|×max_weight×nav/price<br/>portfolio.py:105-116"]
        DC["Delta + pending + cooldown<br/>portfolio.py:118-146"]
        TP["Publish TargetPositionEvent<br/>portfolio.py:148-156"]
        PO --> FU --> TC --> DC --> TP
    end

    SE --> PO
    TP --> TPE["TargetPositionEvent"]

    subgraph F5_Risk["F5: RiskService + Pipeline"]
        RS["_on_target → build Order+RiskContext<br/>risk.py:71-118"]
        PL["RiskPipeline.check → 6 middleware<br/>pipeline.py:60-63"]
        RS --> PL
        PL -->|pass| RAE["RiskApprovedEvent<br/>risk.py:124-136"]
        PL -->|reject| RRE["RiskRejectedEvent<br/>risk.py:137-146"]
    end

    TPE --> RS

    subgraph F6_OMS["F6: OMSService"]
        OA["_on_approved → exchange.submit<br/>oms.py:70-112"]
        OF["_on_fill → weighted avg, status update<br/>oms.py:117-169"]
        OA --> OSE["OrderSubmittedEvent"]
        OF --> OFE["OrderFilledEvent"]
    end

    RAE --> OA
    OSE --> FE["FillEvent (from Exchange)"]
    FE --> OF

    subgraph F6_STL["F6: SettlementService"]
        ST["_on_filled → SettlementEvent<br/>settlement.py:34-46"]
    end

    OFE --> ST
    ST --> SLE["SettlementEvent"]

    subgraph F7_ACCT["F7: AccountService"]
        AC["_on_settlement → apply_buy/sell<br/>account.py:92-126"]
        AC --> PUE["PositionUpdatedEvent"]
        AC --> BUE["BalanceUpdatedEvent"]
    end

    SLE --> AC

    subgraph F8_MON["F8: MonitorService"]
        MN["_on_position_updated<br/>monitor.py:95-119"]
        EP["_emit_pnl: drawdown, HWM<br/>monitor.py:137-167"]
        MN --> EP
        EP --> PLE["PnLEvent"]
        EP --> ALERT["AlertEvent (on breach)"]
    end

    PUE --> MN
    RRE -->|hard reject| MN_ALERT["_on_risk_rejected<br/>monitor.py:121-133"]
    MN_ALERT --> ALERT
```

## Risk Pipeline: 6 Middleware (in execution order)

```mermaid
flowchart LR
    IN["RiskPipeline.check(order, ctx)<br/>pipeline.py:60"] --> M1
    M1["1. PositionLimitMiddleware<br/>builtin.py:35-77<br/>weight > 10%?"] -->|pass| M2
    M2["2. MaxLeverageMiddleware<br/>builtin.py:87-101<br/>leverage > 100?"] -->|pass| M3
    M3["3. DrawdownMiddleware<br/>builtin.py:114-134<br/>dd > 5%?"] -->|pass| M4
    M4["4. FundingRateMiddleware<br/>builtin.py:148-160<br/>BUY & rate > 0.3%?"] -->|pass| M5
    M5["5. MinNotionalMiddleware<br/>builtin.py:167-186<br/>qty×price < 5?"] -->|pass| M6
    M6["6. OpenOrdersLimitMiddleware<br/>builtin.py:196-205<br/>count ≥ 20?"] -->|pass| OUT["RiskResult.approve()"]
    M1 & M2 & M3 & M4 & M5 & M6 -.->|reject| REJ["RiskResult.reject(reason, level)"]
```

> Note: `reduce_only=True` orders skip middleware 1-4 (only 5+6 apply).

## Signal Fusion Formula

```
combined_score = Σ(score_i × confidence_i) / Σ(confidence_i)

Example:
  momentum:      score=+0.6, conf=0.8 → +0.48
  mean_reversion: score=-0.3, conf=0.5 → -0.15
  combined = (0.48 - 0.15) / (0.8 + 0.5) = +0.254 → small long
```

## Target Position & Delta Formulas

```
target_size = |combined_score| × max_weight × nav / price    (portfolio.py:109)
effective_size = current_size + pending_delta                  (portfolio.py:133)
delta = |target_size - effective_size|                         (portfolio.py:134)
```

## Account NAV

```
nav = USDT_balance + Σ(position.size × mark_price)             (account.py:60-66)
```

## Monitor Key Metrics

```
drawdown = max(0, (HWM - nav) / HWM)                           (monitor.py:146)
Sharpe = mean(returns) / std(returns) × sqrt(8760)              (monitor.py:245)
win_rate = wins / (wins + losses)                               (monitor.py:219)
```
