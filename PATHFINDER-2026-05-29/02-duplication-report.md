# 02 — Duplication Report

## Executive Summary

| Priority | Concern | Locations | Type |
|----------|---------|-----------|------|
| 🔴 High | Pending-delta calculation | builtin.py:45, portfolio.py:126, risk.py:98 | Cross-feature |
| 🔴 High | WebSocket worker skeleton | websocket.py:169, user_data.py:143 | Within-feature |
| 🟡 Medium | Price buffer management | momentum.py:58,67, mean_reversion.py:59,66 | Within-feature |
| 🟡 Medium | Market-price estimation | builtin.py:59-69, 170-177 | Within-feature |
| 🟡 Medium | `reduce_only` early-return guard | builtin.py:37,89,116,150 | Within-feature |
| 🟢 Low | Empty protocol stubs | All 3 strategy files (on_book/on_fill/on_start/on_stop) | Within-feature |
| 🟢 Low | `set_oms()` deferred injection | portfolio.py:43,64, risk.py:55,67 | Cross-feature |

---

## 🔴 HIGH: Pending-Delta Calculation (3 locations)

**What:** Identical logic to compute net position impact from in-flight orders.

**Locations:**
| File:Line | Context |
|-----------|---------|
| `builtin.py:45-52` | PositionLimitMiddleware.process |
| `portfolio.py:126-130` | PortfolioService._on_signal |
| `risk.py:98-100` | RiskService._on_target (inverse logic) |

**Code pattern:**
```python
pending_delta = Decimal(0)
for pending in open_orders:
    if pending.side == Side.BUY:
        pending_delta += pending.remaining_qty
    else:
        pending_delta -= pending.remaining_qty
```

**Divergence:** Minor — risk.py subtracts SELL from cur_size instead of adding to delta. Same business logic, different framing.

**Verdict:** Accidental duplication. Business logic should live in ONE place.

**Recommendation:** Extract `_compute_pending_delta(open_orders: list[Order]) -> Decimal` into `application/services/oms.py` as a utility function. Call from all three sites.

---

## 🔴 HIGH: WebSocket Worker + Queue Pattern (2 locations)

**What:** Identical thread-based WebSocket consumer with queue, running flag, reconnection logic.

**Locations:**
| File:Line | Class |
|-----------|-------|
| `websocket.py:169-190` | BinanceWsFeed._ws_worker |
| `user_data.py:143-170` | BinanceUserDataStream._ws_worker |

**Code pattern (identical):**
```python
def _ws_worker(self, url):
    def on_message(ws, msg):
        event = self._parse(msg) / self._handle(raw)
        if event: self._queue.put_nowait(event)
    def on_error(ws, err): log.error(...)
    def on_close(ws, code, msg):
        if self._running: self._ws_worker(url)  # reconnect
    websocket.WebSocketApp(url, ...).run_forever(...)
```

**Queue + stop pattern:**
| websocket.py | user_data.py |
|--------------|--------------|
| `_queue = Queue(10_000)` | `_queue = Queue(1_000)` |
| `_running = False` | `_running = False` |
| `stop(): _running = False` | `stop(): _running = False, close_listen_key` |

**Divergence:**
- `BinanceUserDataStream` has listenKey lifecycle (legitimate specialization)
- Different ping intervals (30 vs 20)
- BinanceWsFeed uses factory pattern for test injection

**Verdict:** Accidental copy-paste. Core skeleton (queue+running+reconnect) is identical.

**Recommendation:** Create shared base class `_QueueWsFeed`:
```python
class _QueueWsFeed:
    _queue: queue.Queue
    _running: bool
    _thread: threading.Thread | None

    def stop(self) -> None: ...
    def _drain_queue(self, timeout: float) -> int: ...
    def _ws_worker(self, url: str, on_message: Callable) -> None: ...
```
Subclasses implement `_parse(raw) -> Event | None` and URL generation.

---

## 🟡 MEDIUM: Price Buffer Management (2 locations)

**What:** Identical deque-based rolling price window initialization and append.

**Locations:**
| File:Line | Strategy |
|-----------|----------|
| `momentum.py:58` | `self._prices: dict[str, deque] = {}` |
| `momentum.py:67-70` | Buffer init + append |
| `mean_reversion.py:59` | `self._prices: dict[str, deque] = {}` |
| `mean_reversion.py:66-69` | Buffer init + append (byte-identical) |

**Code pattern (byte-identical):**
```python
if sym not in self._prices:
    self._prices[sym] = deque(maxlen=self._window)
buf = self._prices[sym]
buf.append(event.price)
```

**Verdict:** Literal copy-paste. No divergence.

**Recommendation:** Extract `PriceBufferMixin`:
```python
class PriceBufferMixin:
    _prices: dict[str, deque]
    _window: int

    def _ensure_buffer(self, sym: str) -> deque: ...
    def _append_price(self, sym: str, price: Decimal) -> None: ...
    def _is_warmed_up(self, sym: str) -> bool: ...
```

---

## 🟡 MEDIUM: Market-Price Estimation (2 locations)

**What:** Identical block to resolve estimated price from order or cache.

**Locations:**
| File:Line | Class |
|-----------|-------|
| `builtin.py:59-69` | PositionLimitMiddleware |
| `builtin.py:170-177` | MinNotionalMiddleware |

**Code pattern:**
```python
est_price = order.limit_price
if est_price is None or est_price <= 0:
    cache_price = ctx.extra.get("price")
    if cache_price is not None and cache_price > 0:
        est_price = cache_price
    else:
        return RiskResult.reject(...) / call_next(...)
```

**Divergence:** Only the fallback action differs (reject vs skip).

**Verdict:** Copy-paste with one-line behavioral change.

**Recommendation:** Extract `_resolve_est_price(order, ctx) -> Decimal | None` static method. Caller decides fallback.

---

## 🟡 MEDIUM: `reduce_only` Early-Return Guard (4 locations)

**What:** Identical guard at the start of middleware `process()`.

**Locations:**
| File:Line | Class |
|-----------|-------|
| `builtin.py:37-38` | PositionLimitMiddleware |
| `builtin.py:89-90` | MaxLeverageMiddleware |
| `builtin.py:116-117` | DrawdownMiddleware |
| `builtin.py:150` | FundingRateMiddleware |

**Code pattern:**
```python
if order.reduce_only:
    return call_next(order, ctx)
```

**Gap:** `MinNotionalMiddleware` (line 167) and `OpenOrdersLimitMiddleware` (line 196) **lack** this guard. A reduce-only order should arguably skip these checks too.

**Verdict:** Incomplete pattern application. Latent bug.

**Recommendation:** Add class attribute `check_reduce_only: bool = True` on base `RiskMiddleware`. Default implementation checks it. Override in MinNotional and OpenOrdersLimit.

---

## 🟢 LOW: Empty Protocol Stubs (3 files × 4 methods each)

**What:** Identical no-op implementations of Strategy protocol methods.

**Locations:**
| Method | momentum.py | mean_reversion.py | funding_rate_arb.py |
|--------|-------------|-------------------|---------------------|
| `on_book` | 96-97 | 90-91 | 138 |
| `on_fill` | 104-105 | 115 | 141-142 |
| `on_start` | 107-109 | 117-119 | 144-147 |
| `on_stop` | 111-112 | 121-122 | 149-150 |

**Verdict:** Boilerplate required by Protocol pattern.

**Recommendation:** Create `BaseStrategy` class with default no-op implementations:
```python
class BaseStrategy(Strategy):
    def on_book(self, event, ctx) -> list[Signal]: return []
    def on_fill(self, event, ctx) -> None: pass
    def on_start(self) -> None: log.info(f"{self.name} started")
    def on_stop(self) -> None: log.info(f"{self.name} stopped")
```
Eliminates ~72 lines total.

---

## 🟢 LOW: Deferred Injection (`set_oms`)

**What:** Identical pattern to resolve circular dependency.

**Locations:**
| File:Line | Class |
|-----------|-------|
| `portfolio.py:43, 64-66` | PortfolioService |
| `risk.py:55, 67-69` | RiskService |

**Code pattern:**
```python
def __init__(self, ..., oms: object | None = None):
    self._oms = oms

def set_oms(self, oms: object) -> None:
    """延迟注入 OMS（container 中解决循环依赖）。"""
    self._oms = oms
```

**Verdict:** Architectural workaround for circular dependency.

**Recommendation:** Keep for now, but consider refactoring container to break the cycle (e.g., via a `PendingOrderRegistry` that both services depend on instead of each other).

---

## Items NOT Flagged as Duplication

These were investigated but determined to be legitimate separation or already unified:

| Concern | Verdict |
|---------|---------|
| Config defaults (RiskConfig.warn_drawdown vs MonitorConfig.warn_drawdown) | Legitimate — different semantic domains |
| Price caching key pattern (`price:{sym}`) | Convention, not duplication |
| Strategy score/confidence clamping | Idiomatic, not worth extracting |
