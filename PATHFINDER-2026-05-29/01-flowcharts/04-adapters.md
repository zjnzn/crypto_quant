# 04 — Adapters Layer (F10+F11+F12)

## Port-to-Adapter Mapping

| Port (`core/ports/`) | Adapter | File |
|---|---|---|
| `EventBusPort` | `SyncEventBus` | adapters/bus/sync.py:27 |
| `CachePort` | `MemoryCache` | adapters/cache/memory.py:15 |
| `ExecutionPort` | `PaperExchange` | adapters/exchange/paper.py:32 |
| `ExecutionPort` | `BinanceFuturesExchange` | adapters/exchange/binance.py:37 |
| *(none)* | `CsvFeed` | adapters/feed/csv_.py:44 |
| *(none)* | `BinanceWsFeed` | adapters/feed/websocket.py:38 |
| *(none)* | `BinanceUserDataStream` | adapters/feed/user_data.py:52 |

## SyncEventBus

```mermaid
flowchart TD
    A["subscribe(TradeEvent, handler)<br/>sync.py:37"] --> B["acquire RLock<br/>sync.py:38"]
    B --> C["_handlers[TradeEvent].append(handler)<br/>sync.py:39"]
    E["publish(event)<br/>sync.py:50"] --> F["acquire RLock, snapshot handlers<br/>sync.py:52-53"]
    F --> G["release RLock, call each handler<br/>sync.py:54-58"]
    G --> H{"Exception?"}
    H -->|yes| I["log.exception, continue<br/>sync.py:60-63"]
    H -->|no| J["next handler"]
    J --> G
```

## MemoryCache (TTL support)

```mermaid
flowchart TD
    subgraph SET["set(key, value, ttl)<br/>memory.py:32-39"]
        S1["acquire RLock<br/>memory.py:34"]
        S2["_data[key] = value<br/>memory.py:35"]
        S3{"ttl?"}
        S3 -->|yes| S4["_expiry[key] = monotonic()+ttl<br/>memory.py:37"]
        S3 -->|no| S5["clear expiry<br/>memory.py:39"]
    end
    subgraph GET["get(key)<br/>memory.py:25-30"]
        G1["acquire RLock<br/>memory.py:26"]
        G2{"expired?<br/>memory.py:55-57"}
        G2 -->|yes| G3["evict<br/>memory.py:59-61"]
        G2 -->|no| G4["return _data[key]"]
    end
```

## PaperExchange: Simulated Fill

```mermaid
flowchart TD
    OMS["OMS publishes OrderSubmittedEvent<br/>oms.py:107"] --> PS["PaperExchange._on_submitted<br/>paper.py:95"]
    PS --> P1["pop _pending[exchange_id]<br/>paper.py:101"]
    P1 --> P2["price = cache.get('price:'+sym)<br/>paper.py:108"]
    P2 --> P3["slippage: ±tick_size<br/>paper.py:116-120"]
    P3 --> P4["commission: 0.1% taker, 0.02% maker<br/>paper.py:123-125"]
    P4 --> P5["bus.publish(FillEvent.caused_by(event))<br/>paper.py:127-138"]
```

## BinanceFuturesExchange: HMAC Signing

```mermaid
flowchart TD
    subgraph SUBMIT["submit(order)<br/>binance.py:68-76"]
        S1["params = {
          symbol, side, quantity,
          type=MARKET/LIMIT/STOP_MARKET
        }<br/>binance.py:139-164"]
        S2["params['timestamp'] = int(time*1000)<br/>binance.py:240-241"]
        S3["params['signature'] = HMAC-SHA256(query, secret)<br/>binance.py:234-237"]
        S4["POST {base}/fapi/v1/order<br/>binance.py:217"]
        S5["return resp['orderId']<br/>binance.py:72"]
        S1 --> S2 --> S3 --> S4 --> S5
    end
```

## CsvFeed Row → Event

```mermaid
flowchart TD
    R1["for row in csv.DictReader<br/>csv_.py:96"] --> R2["sym, ts, close, volume<br/>csv_.py:100-117"]
    R2 --> R3["clock.advance(ts)<br/>csv_.py:112"]
    R3 --> R4["bus.publish(TradeEvent)<br/>csv_.py:123-129"]
    R4 --> R5{"emit_book?"}
    R5 -->|yes| R6["bus.publish(BookEvent)<br/>csv_.py:134-141"]
    R5 -->|no| R7{"funding_rate_fn?"}
    R6 --> R7
    R7 -->|yes| R8["_maybe_emit_funding at 00/08/16 UTC<br/>csv_.py:149-165"]
    R7 -->|no| R9["bars_read++"]
    R8 --> R9
```

## BinanceWsFeed: WebSocket → Queue → Bus

```mermaid
flowchart TD
    subgraph BG["Background Threads"]
        W1["_ws_worker(url_public)<br/>websocket.py:169<br/>bookTicker → /public/stream"]
        W2["_ws_worker(url_market)<br/>websocket.py:169<br/>aggTrade → /market/stream"]
        W1 --> W3["_parse(msg) → BookEvent<br/>websocket.py:192-232"]
        W2 --> W4["_parse(msg) → TradeEvent<br/>websocket.py:192-232"]
        W3 --> Q["queue.Queue(10_000)<br/>websocket.py:74"]
        W4 --> Q
    end

    subgraph MAIN["Main Thread (run)"]
        M1["while _running:<br/>websocket.py:121"]
        M2["event = _queue.get(timeout=0.05)<br/>websocket.py:123"]
        M3["bus.publish(event)<br/>websocket.py:124"]
        M1 --> M2 --> M3 --> M1
    end

    Q -.-> M2
```

## BinanceUserDataStream: listenKey → FillEvent

```mermaid
flowchart TD
    S["start()<br/>user_data.py:90"] --> LK["create_listen_key()<br/>POST /fapi/v1/listenKey<br/>binance.py:265"]
    LK --> URL["url = {private_ws}?listenKey={k}&events=ORDER_TRADE_UPDATE<br/>user_data.py:103"]
    URL --> WS["_ws_worker(url): WebSocketApp<br/>user_data.py:143-168"]
    WS --> PARSE["_handle(raw)<br/>user_data.py:191-259"]
    PARSE -->|"ORDER_TRADE_UPDATE"| FE["FillEvent → _queue.put_nowait()"]
    PARSE -->|"listenKeyExpired"| LK
    FE -.->|"drain() on main thread"| BUS["bus.publish(FillEvent)<br/>user_data.py:135"]
```
