# Plan: Centralize Pending-Delta Calculation

## Summary

Add `compute_pending_delta(symbol: str) -> Decimal` to OMSService. Precompute in RiskService._build_context and pass via `ctx.extra["pending_delta"]`. Refactor 3 call sites.

---

## Phase 0: Documentation & Patterns (COMPLETED)

### Allowed APIs
| API | Source | Usage |
|-----|--------|-------|
| `OMSService.get_open_orders(symbol)` | `oms.py:173-183` | Returns `list[Order]`, thread-safe |
| `Order.side` | `core/domain/order.py:59` | Enum: `Side.BUY` or `Side.SELL` |
| `Order.remaining_qty` | `core/domain/order.py:102` | Decimal property |
| `RiskContext.extra` | `core/ports/risk.py:26` | Dict for arbitrary data |
| `Side` enum | `core/domain/order.py:17-23` | `Side.BUY`, `Side.SELL` |

### Existing Pattern to Copy
From `application/risk/builtin.py:45-52`:
```python
pending_delta = Decimal(0)
for pending in ctx.open_orders:
    if pending.instrument.symbol != order.instrument.symbol:
        continue
    if pending.side == Side.BUY:
        pending_delta += pending.remaining_qty
    else:
        pending_delta -= pending.remaining_qty
```

### Current RiskContext.extra Contents
From `risk.py:182`:
```python
extra = {"daily_drawdown": daily_drawdown, "price": price}
```

---

## Phase 1: Add compute_pending_delta to OMSService

### What to Implement
Add new method to OMSService that computes net pending delta for a symbol.

### File to Modify
`application/services/oms.py` — Insert after line 183 (after `get_open_orders`)

### Code to Add
```python
def compute_pending_delta(self, symbol: str) -> Decimal:
    """
    计算指定标的的在途订单净增量。

    BUY  → +remaining_qty
    SELL → -remaining_qty

    返回: 净增量（正数表示净买入，负数表示净卖出）
    """
    delta = Decimal(0)
    with self._lock:
        for order in self._pending.values():
            if order.instrument.symbol != symbol:
                continue
            if order.side == Side.BUY:
                delta += order.remaining_qty
            else:
                delta -= order.remaining_qty
    return delta
```

### Documentation Reference
- Copy pattern from `builtin.py:45-52`
- Use same `self._lock` pattern as `get_open_orders` (line 180)
- Import `Side` from `core.domain.order` (already imported at line 23)

### Verification
- Grep: `grep -n "compute_pending_delta" application/services/oms.py`
- Unit test: Add test in `tests/test_execution_pipeline.py`

### Anti-Pattern Guards
- DO NOT compute without `self._lock` — `_pending` dict is shared
- DO NOT filter by `order.instrument.symbol` outside lock — inconsistent with get_open_orders pattern

---

## Phase 2: Precompute pending_delta in RiskService._build_context

### What to Implement
Add `pending_delta` to RiskContext.extra in _build_context.

### File to Modify
`application/services/risk.py` — Modify `_build_context` around line 182

### Current Code (line 181-183)
```python
return RiskContext(
    account_id    = account_id,
    nav_usdt      = nav,
    positions     = positions,
    open_orders   = open_orders,
    funding_rates = funding_rates,
    extra         = {"daily_drawdown": daily_drawdown, "price": price},
)
```

### New Code
```python
# Precompute pending delta for middleware
pending_delta = Decimal(0)
if self._oms is not None:
    pending_delta = self._oms.compute_pending_delta(symbol)

return RiskContext(
    account_id    = account_id,
    nav_usdt      = nav,
    positions     = positions,
    open_orders   = open_orders,
    funding_rates = funding_rates,
    extra         = {
        "daily_drawdown": daily_drawdown,
        "price": price,
        "pending_delta": pending_delta,
    },
)
```

### Documentation Reference
- Follow existing pattern: `risk.py:151-183`
- `Decimal(0)` default when OMS is None — same pattern as `open_orders` init (line 163-164)

### Verification
- Grep: `grep -n "pending_delta" application/services/risk.py`
- Unit test: Verify `ctx.extra["pending_delta"]` is populated

### Anti-Pattern Guards
- DO NOT pass `oms` reference in ctx.extra — that couples middleware to implementation
- DO NOT compute multiple times — compute once here, reuse everywhere

---

## Phase 3: Refactor PositionLimitMiddleware

### What to Implement
Replace inline pending-delta loop with `ctx.extra.get("pending_delta")`.

### File to Modify
`application/risk/builtin.py` — Replace lines 45-52

### Current Code (lines 44-52)
```python
# 在途订单增量
pending_delta = Decimal(0)
for pending in ctx.open_orders:
    if pending.instrument.symbol != order.instrument.symbol:
        continue
    if pending.side == Side.BUY:
        pending_delta += pending.remaining_qty
    else:
        pending_delta -= pending.remaining_qty
```

### New Code
```python
# 在途订单增量（由 RiskService 预计算）
pending_delta = ctx.extra.get("pending_delta", Decimal(0))
```

### Documentation Reference
- Pattern: `ctx.extra.get("price")` at `builtin.py:62`

### Verification
- Grep: `grep -n "pending_delta" application/risk/builtin.py`
- Unit test: Verify PositionLimitMiddleware still rejects oversized positions

### Anti-Pattern Guards
- DO NOT remove the symbol filter check — wait, the new method DOES filter by symbol
- DO verify the behavior matches (same result for same open_orders)

---

## Phase 4: Refactor PortfolioService._on_signal

### What to Implement
Replace inline pending-delta loop with `self._oms.compute_pending_delta(sym)`.

### File to Modify
`application/services/portfolio.py` — Replace lines 124-130

### Current Code (lines 124-130)
```python
pending_delta = Decimal(0)
if self._oms is not None:
    for pending in self._oms.get_open_orders(sym):
        if pending.side == Side.BUY:
            pending_delta += pending.remaining_qty
        else:
            pending_delta -= pending.remaining_qty
```

### New Code
```python
pending_delta = Decimal(0)
if self._oms is not None:
    pending_delta = self._oms.compute_pending_delta(sym)
```

### Documentation Reference
- Follow pattern: `self._oms.get_open_orders(sym)` at line 126

### Verification
- Grep: `grep -n "compute_pending_delta" application/services/portfolio.py`
- Unit test: Verify portfolio rebalancing still accounts for pending orders

### Anti-Pattern Guards
- DO NOT remove `if self._oms is not None` guard — OMS injection is deferred

---

## Phase 5: Verify RiskService reduce_only Logic (DO NOT CHANGE)

### What to Verify
The code at `risk.py:98-100` uses inverted logic for reduce_only capping:

```python
if self._oms is not None:
    for pending in self._oms.get_open_orders(sym):
        if pending.side == Side.SELL:
            cur_size -= pending.remaining_qty
```

This is **NOT** the same as compute_pending_delta. This logic:
- Only considers SELL orders (reducing position)
- Subtracts from current size (not delta calculation)
- Purpose: Prevent over-reducing when there are already pending sell orders

### Decision
**DO NOT refactor this.** It's a different use case.

Add a code comment to clarify:
```python
# 注意：这里只减去在途卖单，与 compute_pending_delta 逻辑不同
# 因为 reduce_only 只关心已有的卖出挂单，避免超额平仓
```

---

## Phase 6: Add Unit Tests

### What to Implement
Add tests for `compute_pending_delta` method.

### File to Modify
`tests/test_execution_pipeline.py` — Add after existing OMS tests

### Test Cases
1. Empty pending orders → returns Decimal(0)
2. Single BUY order → returns +remaining_qty
3. Single SELL order → returns -remaining_qty
4. Multiple orders (mixed BUY/SELL) → returns net delta
5. Orders for different symbols → only counts matching symbol

### Documentation Reference
- Copy pattern from existing OMS tests in `test_execution_pipeline.py`
- Use `assert` with Decimal comparison

### Verification
- Run: `python -m pytest tests/test_execution_pipeline.py -v -k "pending_delta"`

---

## Phase 7: Final Verification

### Checklist
1. [ ] `grep -n "compute_pending_delta" application/services/oms.py` — new method exists
2. [ ] `grep -n "pending_delta" application/services/risk.py` — precomputed in _build_context
3. [ ] `grep -n "pending_delta" application/risk/builtin.py` — uses ctx.extra
4. [ ] `grep -n "compute_pending_delta" application/services/portfolio.py` — uses new method
5. [ ] `grep -n "for pending in.*open_orders" application/` — no more inline loops (except risk.py reduce_only)
6. [ ] Run tests: `python -m pytest tests/ -v`

### Anti-Pattern Check
```bash
# Should NOT find this pattern anymore (except in risk.py reduce_only)
grep -rn "for pending in.*open_orders" application/ --include="*.py" | grep -v "risk.py:99"
```

---

## Summary

| Phase | File | Lines Changed |
|-------|------|---------------|
| 1 | `oms.py` | +12 (new method) |
| 2 | `risk.py` | +4 (precompute) |
| 3 | `builtin.py` | -8, +2 (simplify) |
| 4 | `portfolio.py` | -7, +2 (simplify) |
| 5 | `risk.py` | +3 (comment only) |
| 6 | `test_execution_pipeline.py` | +30 (tests) |

**Net effect:** -11 lines of duplicate logic, +1 centralized method, +tests
