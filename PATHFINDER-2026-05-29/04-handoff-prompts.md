# 04 — Handoff Prompts (Copy-Pasteable `/make-plan`)

Each prompt below is ready to paste directly into `/make-plan`. It specifies the target, cites Phase 2 evidence, and includes anti-pattern guards.

---

## Prompt 1: Centralize Pending-Delta Calculation

```
/make-plan

## Target
Add `compute_pending_delta(symbol: str) -> Decimal` method to OMSService (application/services/oms.py).

## Call Sites to Rewrite (from Phase 2)
1. `builtin.py:45-52` — PositionLimitMiddleware inline loop → read `ctx.extra["pending_delta"]`
2. `portfolio.py:126-130` — PortfolioService inline loop → call `self._oms.compute_pending_delta(sym)`
3. `risk.py:98-100` — RiskService inverted logic → use precomputed delta

## Implementation Steps
1. Add method to OMSService at line ~180:
   ```python
   def compute_pending_delta(self, symbol: str) -> Decimal:
       delta = Decimal(0)
       for order in self.get_open_orders(symbol):
           if order.side == Side.BUY:
               delta += order.remaining_qty
           else:
               delta -= order.remaining_qty
       return delta
   ```
2. In RiskService._build_context (risk.py:162), add:
   ```python
   ctx.extra["pending_delta"] = self._oms.compute_pending_delta(symbol)
   ```
3. In PositionLimitMiddleware (builtin.py:45-52), replace inline loop with:
   ```python
   pending_delta = ctx.extra.get("pending_delta", Decimal(0))
   ```
4. In PortfolioService._on_signal (portfolio.py:126-130), replace inline loop with:
   ```python
   pending_delta = self._oms.compute_pending_delta(sym)
   ```
5. Update tests in test_execution_pipeline.py and test_multi_strategy.py to verify new behavior.

## Relevant Flowchart
PATHFINDER-2026-05-29/01-flowcharts/03-application-pipeline.md

## Anti-Pattern Guards
- DO NOT pass `oms` reference into middleware via `ctx.extra["oms"]` — that couples middleware to OMS implementation
- DO NOT compute pending_delta multiple times per event — compute once in RiskService._build_context, pass precomputed
- DO NOT change the sign convention: BUY adds, SELL subtracts (current convention)
```

---

## Prompt 2: Shared WebSocket Queue-Feed Base

```
/make-plan

## Target
Create `adapters/feed/base.py` with `_QueueWsFeed` base class. Refactor `BinanceWsFeed` and `BinanceUserDataStream` to inherit from it.

## Call Sites to Rewrite (from Phase 2)
1. `websocket.py:169-190` — BinanceWsFeed._ws_worker → inherit from base, override _parse
2. `user_data.py:143-170` — BinanceUserDataStream._ws_worker → inherit from base, override _handle
3. `websocket.py:74-76, 100-102` — queue/running/stop → provided by base
4. `user_data.py:83-85, 118-122` — queue/running/stop → provided by base

## Implementation Steps
1. Create `adapters/feed/base.py`:
   ```python
   class _QueueWsFeed:
       def __init__(self, bus: EventBusPort, queue_size: int = 10_000): ...
       def stop(self) -> None: ...
       def drain(self, timeout: float = 0.05) -> int: ...
       def _ws_worker(self, url: str, parser: Callable[[str], Event | None]) -> None: ...
   ```
2. Modify BinanceWsFeed:
   - Inherit from `_QueueWsFeed`
   - Delete `_queue`, `_running`, `_thread`, `stop()` (use base)
   - Keep `_build_urls`, `_parse`, `start()` (call base._ws_worker)
   - Pass `self._parse` as parser to base._ws_worker
3. Modify BinanceUserDataStream:
   - Inherit from `_QueueWsFeed` (queue_size=1_000)
   - Delete `_queue`, `_running`, `_ws_thread`, `stop()` (use base, extend for listenKey)
   - Keep `_handle`, `start()` (listenKey lifecycle, keepalive thread)
   - Pass `self._handle` as parser to base._ws_worker
4. Update tests in test_core.py (any WS-related mocks) to use new base class interface.

## Relevant Flowchart
PATHFINDER-2026-05-29/01-flowcharts/04-adapters.md

## Anti-Pattern Guards
- DO NOT create a `WsFeedPort` port interface — feeds are application-created, not domain-injected
- DO NOT move listenKey lifecycle into base — that is legitimate specialization
- DO NOT change ping_interval/queue_size values — pass as constructor kwargs to base
```

---

## Prompt 3: PriceBufferMixin for Strategies

```
/make-plan

## Target
Create `strategies/mixin.py` with `PriceBufferMixin`. Refactor `MomentumStrategy` and `MeanReversionStrategy` to use it.

## Call Sites to Rewrite (from Phase 2)
1. `momentum.py:58, 67-70` — buffer init + append → mixin._init_buffer, _append_price
2. `mean_reversion.py:59, 66-69` — buffer init + append → mixin._init_buffer, _append_price
3. `momentum.py:73` — warm-up guard → mixin._is_warmed_up
4. `mean_reversion.py:71` — warm-up guard → mixin._is_warmed_up

## Implementation Steps
1. Create `strategies/mixin.py`:
   ```python
   class PriceBufferMixin:
       _prices: dict[str, deque[Decimal]]
       _window: int
       def _init_buffer(self, window: int) -> None: ...
       def _ensure_buffer(self, sym: str) -> deque: ...
       def _append_price(self, sym: str, price: Decimal) -> None: ...
       def _is_warmed_up(self, sym: str) -> bool: ...
       def _prices_list(self, sym: str) -> list[Decimal]: ...
   ```
2. Modify MomentumStrategy:
   - Add `PriceBufferMixin` to inheritance
   - Replace `self._prices = {}` with `self._init_buffer(window)`
   - Replace buffer append block with `self._append_price(sym, event.price)`
   - Replace warm-up guard with `if not self._is_warmed_up(sym): return []`
   - Use `self._prices_list(sym)` instead of `list(self._prices[sym])`
3. Modify MeanReversionStrategy — same as above
4. Update tests in test_signal_pipeline.py and test_multi_strategy.py to verify warm-up behavior unchanged.

## Relevant Flowchart
PATHFINDER-2026-05-29/01-flowcharts/02-strategy-framework.md

## Anti-Pattern Guards
- DO NOT change `_window` default values — strategies configure differently
- DO NOT move scoring formulas into mixin — those are strategy-specific
- DO NOT apply mixin to FundingRateArbStrategy — it uses bar counters, not price buffers
```

---

## Prompt 4: Resolve Market-Price Estimation Helper

```
/make-plan

## Target
Add `_resolve_est_price(order, ctx) -> Decimal | None` static method to `RiskMiddleware` base class (application/risk/pipeline.py).

## Call Sites to Rewrite (from Phase 2)
1. `builtin.py:59-69` — PositionLimitMiddleware → call helper, reject if None
2. `builtin.py:170-177` — MinNotionalMiddleware → call helper, skip if None

## Implementation Steps
1. In `application/risk/pipeline.py`, add to RiskMiddleware:
   ```python
   @staticmethod
   def _resolve_est_price(order: Order, ctx: RiskContext) -> Decimal | None:
       est_price = order.limit_price
       if est_price is not None and est_price > 0:
           return est_price
       cache_price = ctx.extra.get("price")
       if cache_price is not None and cache_price > 0:
           return cache_price
       return None
   ```
2. In PositionLimitMiddleware (builtin.py:59-69), replace inline block with:
   ```python
   est_price = self._resolve_est_price(order, ctx)
   if est_price is None:
       return RiskResult.reject("无法确定预估价格", level="hard")
   ```
3. In MinNotionalMiddleware (builtin.py:170-177), replace inline block with:
   ```python
   est_price = self._resolve_est_price(order, ctx)
   if est_price is None:
       return call_next(order, ctx)  # lenient skip
   ```
4. Update tests in test_execution_pipeline.py to verify both behaviors.

## Relevant Flowchart
PATHFINDER-2026-05-29/01-flowcharts/03-application-pipeline.md (Risk Pipeline section)

## Anti-Pattern Guards
- DO NOT change the divergence: PositionLimit rejects, MinNotional skips — this is intentional
- DO NOT make `_resolve_est_price` a standalone module — keep it on RiskMiddleware where context is available
```

---

## Prompt 5: Standardize reduce_only Guard

```
/make-plan

## Target
Extend `RiskMiddleware` base class to include default `check_reduce_only` behavior. Override in classes that should NOT skip.

## Call Sites to Rewrite (from Phase 2)
1. `builtin.py:37-38` — PositionLimitMiddleware guard → base default
2. `builtin.py:89-90` — MaxLeverageMiddleware guard → base default
3. `builtin.py:116-117` — DrawdownMiddleware guard → base default
4. `builtin.py:150` — FundingRateMiddleware guard → base default (with extra condition)
5. `builtin.py:167` — MinNotionalMiddleware LACKS guard → set `check_reduce_only = False`
6. `builtin.py:196` — OpenOrdersLimitMiddleware LACKS guard → set `check_reduce_only = False`

## Implementation Steps
1. In `application/risk/pipeline.py`, modify RiskMiddleware:
   ```python
   class RiskMiddleware:
       name: str = "unnamed"
       check_reduce_only: bool = True

       def process(self, order: Order, ctx: RiskContext, call_next: Next) -> RiskResult:
           if self.check_reduce_only and order.reduce_only:
               return call_next(order, ctx)
           return self._do_check(order, ctx, call_next)

       def _do_check(self, order, ctx, call_next) -> RiskResult:
           raise NotImplementedError
   ```
2. In each middleware, rename `process` → `_do_check` (no other changes needed)
3. In MinNotionalMiddleware and OpenOrdersLimitMiddleware, add:
   ```python
   check_reduce_only = False
   ```
4. FundingRateMiddleware keeps its extra `or order.side == Side.SELL` condition — add to `_do_check`:
   ```python
   if order.reduce_only or order.side == Side.SELL:
       return call_next(order, ctx)
   ```
5. Update tests in test_execution_pipeline.py to verify reduce-only orders pass through MinNotional/OpenOrders.

## Relevant Flowchart
PATHFINDER-2026-05-29/01-flowcharts/03-application-pipeline.md (Risk Pipeline section)

## Anti-Pattern Guards
- DO NOT remove FundingRateMiddleware's extra `side == SELL` condition — that is legitimate specialization
- DO NOT change the default — most middleware SHOULD skip for reduce-only orders
```

---

## Prompt 6: BaseStrategy with Default Stubs

```
/make-plan

## Target
Extend `Strategy` protocol or create `BaseStrategy` class with default no-op implementations. Refactor all 3 strategies to inherit.

## Call Sites to Rewrite (from Phase 2)
1. `momentum.py:96-97` — on_book return [] → delete (use default)
2. `momentum.py:104-105` — on_fill pass → delete (use default)
3. `momentum.py:107-112` — on_start/on_stop logs → delete (use default)
4. `mean_reversion.py:90-91` — on_book → delete
5. `mean_reversion.py:115` — on_fill → delete
6. `mean_reversion.py:117-122` — on_start/on_stop → delete
7. `funding_rate_arb.py:138` — on_book → delete
8. `funding_rate_arb.py:141-142` — on_fill → delete
9. `funding_rate_arb.py:144-150` — on_start/on_stop → delete

## Implementation Steps
1. In `application/strategy/base.py`, add BaseStrategy class:
   ```python
   class BaseStrategy(Strategy):
       def on_book(self, event, ctx) -> list[Signal]: return []
       def on_fill(self, event, ctx) -> None: pass
       def on_start(self) -> None: log.info("%s v%s 启动", self.name, self.version)
       def on_stop(self) -> None: log.info("%s v%s 停止", self.name, self.version)
   ```
2. Modify each strategy:
   - Change `class XStrategy(Strategy)` → `class XStrategy(BaseStrategy)`
   - Delete `on_book`, `on_fill`, `on_start`, `on_stop` methods
   - Keep `on_trade` and any strategy-specific methods
3. Update tests in test_signal_pipeline.py to verify startup logs still appear.

## Relevant Flowchart
PATHFINDER-2026-05-29/01-flowcharts/02-strategy-framework.md

## Anti-Pattern Guards
- DO NOT change the Strategy protocol itself — keep it minimal for future non-BaseStrategy implementors
- DO NOT move scoring logic into BaseStrategy — that is per-strategy implementation
```

---

## Execution Order

Recommended order (dependencies):

1. **Prompt 5** (reduce_only guard) — changes base class, affects all middleware
2. **Prompt 4** (est-price helper) — adds method to same base class
3. **Prompt 1** (pending-delta) — independent, but RiskService change affects middleware context
4. **Prompt 2** (WS base) — independent adapter refactor
5. **Prompt 3** (PriceBufferMixin) — independent strategy refactor
6. **Prompt 6** (BaseStrategy) — independent strategy refactor

Can run 1+4+5 in parallel, then 2+3+6 after base class changes settle.