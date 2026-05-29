# 01 — Core Domain, Events & Environment (F1+F2+F14)

## Order Lifecycle

```mermaid
flowchart TD
    NEW["Order(status=NEW)<br/>order.py:95"] -->|"submit succeeds<br/>oms.py:97-100"| SUBMITTED
    NEW -->|"submit exception<br/>oms.py:85-88"| REJECTED["REJECTED (terminal)<br/>order.py:39"]
    SUBMITTED["SUBMITTED<br/>oms.py:97-100"] -->|"filled >= qty<br/>oms.py:138-148"| FILLED["FILLED (terminal)<br/>order.py:37"]
    SUBMITTED -->|"filled < qty<br/>oms.py:138-148"| PART_FILLED["PART_FILLED<br/>oms.py:138-148"]
    PART_FILLED -->|"next fill<br/>oms.py:138-148"| FILLED
    PART_FILLED -->|"more fills remain"| PART_FILLED
    FILLED -->|"SettlementService<br/>settlement.py:34"| SettlementEvent

    style NEW fill:#lightblue
    style SUBMITTED fill:#lightyellow
    style PART_FILLED fill:#lightyellow
    style FILLED fill:#lightgreen
    style REJECTED fill:#lightcoral
```

> **Gap:** `CANCELLED` (order.py:38) is defined but has no reachable code path.

## Event Causal Chain

```mermaid
flowchart TD
    CSV["CsvFeed._emit_row<br/>csv_.py:123"] --> TE["TradeEvent E1<br/>events.py:32"]
    TE -->|"SignalService<br/>signal.py:108"| SE["SignalEvent E2<br/>events.py:72"]
    SE -->|"PortfolioService<br/>portfolio.py:148"| TPE["TargetPositionEvent E3<br/>events.py:84"]
    TPE -->|"RiskService pass<br/>risk.py:129"| RAE["RiskApprovedEvent E4<br/>events.py:99"]
    TPE -->|"RiskService fail<br/>risk.py:138"| RRE["RiskRejectedEvent E4<br/>events.py:105"]
    RAE -->|"OMS<br/>oms.py:107"| OSE["OrderSubmittedEvent E5<br/>events.py:124"]
    OSE -->|"Exchange fill<br/>paper.py:127"| FE["FillEvent E6<br/>events.py:131"]
    FE -->|"OMS<br/>oms.py:162"| OFE["OrderFilledEvent E7<br/>events.py:147"]
    OFE -->|"SettlementService<br/>settlement.py:36"| SLE["SettlementEvent E8<br/>events.py:157"]
    SLE -->|"AccountService<br/>account.py:113"| PUE["PositionUpdatedEvent E9<br/>events.py:174"]
    SLE -->|"AccountService<br/>account.py:121"| BUE["BalanceUpdatedEvent E10<br/>events.py:183"]
    PUE -->|"MonitorService<br/>monitor.py:159"| PLE["PnLEvent E11<br/>events.py:193"]
```

All events share the same `correlation_id` via `Event.caused_by()` (event.py:33).

## Position Update Flow

```mermaid
flowchart TD
    SLE["SettlementEvent<br/>account.py:92"] --> CHECK{"side?"}
    CHECK -->|BUY| BUY["_apply_buy<br/>account.py:130"]
    CHECK -->|SELL| SELL["_apply_sell<br/>account.py:143"]
    BUY --> BUY_EMPTY{"pos empty?"}
    BUY_EMPTY -->|yes| BUY_NEW["New Position(LONG)<br/>account.py:132"]
    BUY_EMPTY -->|no| BUY_MERGE["Merge: size+=qty, entry=weighted avg<br/>account.py:134"]
    SELL --> SELL_EMPTY{"pos empty?"}
    SELL_EMPTY -->|yes| SELL_WARN["Warn, return empty<br/>account.py:145"]
    SELL_EMPTY -->|no| SELL_CLOSE["close=min(qty,size)<br/>pnl=close*(price-entry)<br/>account.py:152"]
    BUY_NEW & BUY_MERGE & SELL_CLOSE --> PUB["Publish PositionUpdated + BalanceUpdated<br/>account.py:113-126"]
```

> **Gap:** Position is always `LONG`. Short selling not implemented.

## Event Type Inheritance

```mermaid
flowchart TD
    Event["Event (frozen)<br/>event.py:27"] --> M["Market Events"]
    Event --> S["Signal Events"]
    Event --> P["Portfolio Events"]
    Event --> R["Risk Events"]
    Event --> O["Order Events"]
    Event --> E["Execution Events"]
    Event --> ST["Settlement Events"]
    Event --> A["Account Events"]
    Event --> MO["Monitor Events"]
    M --> TradeEvent["TradeEvent<br/>events.py:32"]
    M --> BookEvent["BookEvent<br/>events.py:41"]
    M --> FundingRateEvent["FundingRateEvent<br/>events.py:59"]
    S --> SignalEvent["SignalEvent<br/>events.py:72"]
    P --> TargetPositionEvent["TargetPositionEvent<br/>events.py:84"]
    R --> RiskApprovedEvent["RiskApprovedEvent<br/>events.py:99"]
    R --> RiskRejectedEvent["RiskRejectedEvent<br/>events.py:105"]
    O --> OrderReadyEvent["OrderReadyEvent<br/>events.py:115 ⚠️UNUSED"]
    O --> OrderSubmittedEvent["OrderSubmittedEvent<br/>events.py:124"]
    E --> FillEvent["FillEvent<br/>events.py:131"]
    E --> OrderFilledEvent["OrderFilledEvent<br/>events.py:147"]
    ST --> SettlementEvent["SettlementEvent<br/>events.py:157"]
    A --> PositionUpdatedEvent["PositionUpdatedEvent<br/>events.py:174"]
    A --> BalanceUpdatedEvent["BalanceUpdatedEvent<br/>events.py:183"]
    MO --> PnLEvent["PnLEvent<br/>events.py:193"]
    MO --> AlertEvent["AlertEvent<br/>events.py:202"]
```

## External Dependencies
Core domain (F1) + Env (F14): **stdlib only** — zero third-party imports.
Events (F2): imports only `core/domain/`.
