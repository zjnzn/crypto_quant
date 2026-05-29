# 计划 02：提取 `_resolve_est_price` 共享辅助方法

> **目标**：把 `PositionLimitMiddleware` 与 `MinNotionalMiddleware` 中重复的「估算订单价格」逻辑，提取为 `RiskMiddleware` 基类上的一个 `@staticmethod`，消除重复。**纯重构，不改变可观察行为。**
>
> **来源**：PATHFINDER-2026-05-29 重复度分析 / `02-duplication-report.md`
> **相关流程图**：`PATHFINDER-2026-05-29/01-flowcharts/03-application-pipeline.md`（Risk Pipeline 段）
> **关联计划**：`01-pending-delta-plan.md`（同样改动 `RiskService._build_context` 的 `extra` 字典）
>
> 本计划已对照真实代码逐行核实（行号、签名、消息文本、测试夹具）。**Phase 0 是核实后的事实基线，请以它为准，而非任务简报中的假设。**

---

## ⚠️ 与原始任务简报的偏差（已核实，必须采纳）

执行前请注意：原始简报中有 3 处与真实代码不符，本计划已修正。

| # | 简报中的说法 | 真实代码 | 处理方式 |
|---|---|---|---|
| 1 | PositionLimit 价格块在 `builtin.py:59-69` | 实际在 **`builtin.py:51-61`** | 按真实行号 / 用下方 verbatim 块匹配 |
| 1 | MinNotional 价格块在 `builtin.py:170-177` | 实际在 **`builtin.py:162-169`** | 按真实行号 / 用下方 verbatim 块匹配 |
| 2 | PositionLimit 拒绝写 `RiskResult.reject("无法确定预估价格", level="hard")` | 现有代码是 `RiskResult.reject(f"{order.instrument.symbol} 无法估算价格，拒绝 market order")`，**不传 `level=`**（默认即 `"hard"`） | **保留现有消息文本与默认 level**。纯重构不改可观察行为，且现有消息含 symbol 更有用 |
| 3 | 测试写到 `test_execution_pipeline.py` | 该文件存在但**只做集成测试**，无中间件单元测试。真正的单元测试在 **`tests/test_core.py::TestRiskPipeline`** | 新单元测试加到 `tests/test_core.py::TestRiskPipeline` |

> 修正 2 是核心：本任务是「提取共享方法」的纯重构。**不要**改动拒绝消息、**不要**新增 `level=` 参数 —— 这会改变 `result.reason` 这一可观察行为。

---

## Phase 0：核实后的事实基线（Allowed APIs）

> 全部来自对真实文件的完整阅读，置信度 HIGH。执行各 Phase 时以此为唯一事实来源。

### 0.1 `application/risk/pipeline.py`（基类所在）

```python
# 第 17-24 行：现有 imports（注意 Decimal 未导入！）
from __future__ import annotations
import logging
from typing import Callable
from core.domain.order import Order
from core.ports.risk import RiskContext, RiskResult

# 第 27 行：中间件链类型别名
Next = Callable[[Order, RiskContext], RiskResult]

# 第 30-38 行：基类，仅有 process() 一个方法
class RiskMiddleware:
    """风控中间件基类（也满足 RiskPort 结构化子类型）。"""
    name: str = "base"

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        raise NotImplementedError(
            f"{type(self).__name__} 必须实现 process()"
        )

# 第 41 行：紧接的下一个类
class RiskPipeline:
```

- **`Decimal` 未在本文件导入** → Phase 1 必须新增 `from decimal import Decimal`。
- 新 `@staticmethod` 的插入点：**`process` 方法之后、`class RiskPipeline` 之前**（即第 38 行与第 41 行之间，仍缩进在 `RiskMiddleware` 内）。
- `Order` 来自 `core.domain.order`；`RiskContext`、`RiskResult` 来自 `core.ports.risk`（均已导入，无需再加）。

### 0.2 `core/ports/risk.py`（RiskContext / RiskResult 定义）

```python
# RiskContext（第 16-26 行）—— extra 是普通 dict
@dataclass
class RiskContext:
    account_id:    str
    nav_usdt:      Decimal
    positions:     dict[str, Position]
    open_orders:   list[Order]
    funding_rates: dict[str, Decimal]
    extra:         dict = field(default_factory=dict)   # 第 26 行

# RiskResult（第 36-46 行）—— 注意 reject 的默认 level 就是 "hard"
@classmethod
def approve(cls) -> RiskResult:        return cls(passed=True)
@classmethod
def reject(cls, reason: str, level: str = "hard") -> RiskResult:
    return cls(passed=False, reason=reason, level=level)
@classmethod
def warn(cls, reason: str) -> RiskResult:
    return cls(passed=True, reason=reason, level="soft")
```

- `RiskResult` 字段：`.passed`(bool) / `.reason`(str) / `.level`(str: `"hard"`|`"soft"`) / `.amended_order`。
- 断言模式：`assert result.passed` / `assert not result.passed` / `"xxx" in result.reason`。

### 0.3 `core/domain/order.py`

```python
limit_price:  Decimal | None = None     # 第 89 行，可为 None
qty:          Decimal                   # 第 83 行（注意是 qty 不是 quantity）
side:         Side                      # 第 82 行
```

### 0.4 价格来源链（`ctx.extra["price"]`）

- 读取：`builtin.py:54`、`builtin.py:164` → `ctx.extra.get("price")`
- 写入：`application/services/risk.py:188` → `extra = {"daily_drawdown": ..., "price": price, "pending_delta": ...}`，其中 `price = self._cache.get(f"price:{symbol}")`（`risk.py:175`）
- 原始来源：`signal.py:71` / `monitor.py:93` 把 `TradeEvent.price`（类型 **`Decimal`**）写入缓存。
- 结论：`ctx.extra.get("price")` 运行时为 `Decimal` 或 `None`，`cache_price > 0` 比较合法。

### 0.5 `builtin.py` 两处调用点 verbatim（用于精确匹配 / 替换）

**PositionLimitMiddleware（第 51-61 行，拒绝语义）：**
```python
        # ★ market order 无 limit_price，必须从 cache 获取最新价格
        est_price = order.limit_price
        if est_price is None or est_price <= 0:
            # 从风控上下文获取最新成交价
            cache_price = ctx.extra.get("price")
            if cache_price is not None and cache_price > 0:
                est_price = cache_price
            else:
                # 无法获取价格，拒绝订单（保守策略）
                return RiskResult.reject(
                    f"{order.instrument.symbol} 无法估算价格，拒绝 market order"
                )
```

**MinNotionalMiddleware（第 162-169 行，跳过语义）：**
```python
        # ★ market order 无 limit_price，从 cache 获取最新价格
        est_price = order.limit_price
        if est_price is None or est_price <= 0:
            cache_price = ctx.extra.get("price")
            if cache_price is not None and cache_price > 0:
                est_price = cache_price
            else:
                # 无法获取价格，跳过最小名义价值检查（不阻塞交易）
                return call_next(order, ctx)
```

- 两处 `process` 签名相同：`def process(self, order, ctx, call_next: Next) -> RiskResult`，续传参数名就是 `call_next`。
- 替换后，下游仍使用 `est_price`（PositionLimit 后续算 weight，MinNotional 第 170 行 `notional = order.qty * est_price`）→ **替换块必须保证 `est_price` 已被赋值**。
- `builtin.py` 顶部已导入 `Decimal`、`Order`、`Side`、`RiskContext`、`RiskResult`、`RiskMiddleware`、`Next`（第 14-19 行），**无需新增 import**。

### 0.6 测试现状（`tests/test_core.py`）

- 单元测试类：`TestRiskPipeline`（第 380-521 行）。**无 `conftest.py`**，夹具均在 test_core.py 模块/类级定义。
- 可复用夹具：`ctx`（第 383-392 行，**未设 `extra` → 空 dict → 无 "price" 键**）、`btc_perp`（`Instrument`，模块级，第 438 行已用）、`buy_order`（`limit_price=65000`，**不适合**无价测试）。
- `Order` / `Side` / `Decimal` / `Instrument` 已在模块顶部导入。
- 运行方式：`pytest`（无 pytest.ini / pyproject 配置）。
- **现状缺口**：PositionLimit「无价拒绝」、MinNotional「无价跳过」两条路径目前都**没有测试覆盖** → Phase 3 补上。

---

## Phase 1：在 `RiskMiddleware` 上新增 `_resolve_est_price`

**文件**：`application/risk/pipeline.py`

### 步骤 1.1 — 新增 `Decimal` 导入

把（第 18-19 行附近）：
```python
import logging
from typing import Callable
```
改为：
```python
import logging
from decimal import Decimal
from typing import Callable
```

### 步骤 1.2 — 在 `process` 方法之后插入静态方法

在第 38 行 `process` 方法体结束后、`class RiskPipeline`（第 41 行）之前，插入（仍缩进于 `RiskMiddleware` 类内）：

```python
    @staticmethod
    def _resolve_est_price(order: Order, ctx: RiskContext) -> Decimal | None:
        """估算订单价格：优先用 limit_price，其次取风控上下文缓存价；都没有则返回 None。"""
        est_price = order.limit_price
        if est_price is not None and est_price > 0:
            return est_price
        cache_price = ctx.extra.get("price")
        if cache_price is not None and cache_price > 0:
            return cache_price
        return None
```

> 该逻辑与两处现有内联块**完全等价**：`limit_price` 优先（>0），否则取缓存价（>0），都没有则 `None`。

### Phase 1 验证清单
- [ ] `grep -n "from decimal import Decimal" application/risk/pipeline.py` → 命中 1 行
- [ ] `grep -n "_resolve_est_price" application/risk/pipeline.py` → 命中 1 行（定义）
- [ ] `python -c "from application.risk.pipeline import RiskMiddleware; from decimal import Decimal; print(RiskMiddleware._resolve_est_price)"` → 无报错，打印出函数对象

### Phase 1 反模式守卫
- ❌ 不要把 `_resolve_est_price` 做成独立模块/独立函数 —— 必须挂在 `RiskMiddleware` 上（子类通过 `self._resolve_est_price(...)` 调用）。
- ❌ 不要改 `process` 的签名或 `Next` 别名。
- ❌ 不要给方法加 `self` 参数（它是 `@staticmethod`）。

---

## Phase 2：改写 `builtin.py` 两处调用点

**文件**：`application/risk/builtin.py`。两处都是「用辅助方法替换内联块」，**保持各自原有的发散语义**。

### 步骤 2.1 — PositionLimitMiddleware（拒绝语义）

把 Phase 0.5 中第 51-61 行的 verbatim 块，整体替换为：

```python
        # ★ market order 无 limit_price，从 cache 估算价格；无法估算则拒绝（保守策略）
        est_price = self._resolve_est_price(order, ctx)
        if est_price is None:
            return RiskResult.reject(
                f"{order.instrument.symbol} 无法估算价格，拒绝 market order"
            )
```

> 拒绝消息文本与原代码**逐字一致**，不传 `level=`（默认 `"hard"`）。

### 步骤 2.2 — MinNotionalMiddleware（跳过语义）

把 Phase 0.5 中第 162-169 行的 verbatim 块，整体替换为：

```python
        # ★ market order 无 limit_price，从 cache 估算价格；无法估算则跳过检查（不阻塞交易）
        est_price = self._resolve_est_price(order, ctx)
        if est_price is None:
            return call_next(order, ctx)
```

> 续传语义不变：无价时 `call_next(order, ctx)` 放行，不拒绝。

### Phase 2 验证清单
- [ ] `grep -n "est_price = order.limit_price" application/risk/builtin.py` → **0 命中**（内联块已全部移除）
- [ ] `grep -n "cache_price" application/risk/builtin.py` → **0 命中**（已移入辅助方法）
- [ ] `grep -n "_resolve_est_price" application/risk/builtin.py` → **2 命中**（两处调用）
- [ ] `grep -n "无法估算价格" application/risk/builtin.py` → 仍命中 PositionLimit 的拒绝消息（文本未变）
- [ ] 人工确认替换后 `est_price` 仍被赋值，且下游 `notional = order.qty * est_price`（MinNotional 第 170 行）等用法不报 NameError。

### Phase 2 反模式守卫
- ❌ **不要统一两处的发散**：PositionLimit 拒绝、MinNotional 跳过 —— 这是有意为之，必须保留。
- ❌ 不要改 PositionLimit 的拒绝消息文本，不要新增 `level=` 参数。
- ❌ 不要在 MinNotional 处改成拒绝（那会阻塞交易，属于行为回归）。

---

## Phase 3：在 `tests/test_core.py::TestRiskPipeline` 补单元测试

**文件**：`tests/test_core.py`（**不是** `test_execution_pipeline.py`）。在 `TestRiskPipeline` 类内新增两个测试方法。复用现有 `ctx`、`btc_perp` 夹具（`ctx` 的 `extra` 为空 → 无 "price"）。**无需新增 import**。

### 步骤 3.1 — PositionLimit 无价时拒绝

```python
    def test_position_limit_rejects_when_no_price(self, btc_perp: Instrument,
                                                  ctx: "RiskContext") -> None:
        """market order 无 limit_price 且 ctx 无缓存价时，仓位上限中间件拒绝（保守策略）。"""
        from application.risk.pipeline import RiskPipeline
        from application.risk.builtin  import PositionLimitMiddleware

        order = Order(
            instrument = btc_perp,
            account_id = "main",
            side       = Side.BUY,
            qty        = Decimal("0.01"),
            # 无 limit_price → market order；ctx 夹具 extra 为空 → 无 "price"
        )
        pipeline = RiskPipeline([PositionLimitMiddleware(max_weight=0.05)])
        result   = pipeline.check(order, ctx)
        assert not result.passed
        assert "无法估算价格" in result.reason
```

### 步骤 3.2 — MinNotional 无价时跳过（放行）

```python
    def test_min_notional_skips_when_no_price(self, btc_perp: Instrument,
                                              ctx: "RiskContext") -> None:
        """market order 无 limit_price 且 ctx 无缓存价时，最小名义价值中间件跳过检查（放行，不阻塞）。"""
        from application.risk.pipeline import RiskPipeline
        from application.risk.builtin  import MinNotionalMiddleware

        order = Order(
            instrument = btc_perp,
            account_id = "main",
            side       = Side.BUY,
            qty        = Decimal("0.001"),   # 若有价会低于最小名义价值
            # 无 limit_price → market order；ctx 夹具 extra 为空 → 无 "price"
        )
        pipeline = RiskPipeline([MinNotionalMiddleware()])   # 无构造参数，阈值取自 instrument.min_notional
        result   = pipeline.check(order, ctx)
        assert result.passed   # 无价 → 跳过检查 → 放行
```

> **可选增强**（让「跳过」更可信，证明放行是因无价而非别的原因）：可再加一条「ctx.extra={"price": Decimal("65000")} + 同样 0.001 qty → 被 MinNotional 拒绝」的对照测试。非必需，视时间而定。

### Phase 3 验证清单
- [ ] `grep -n "test_position_limit_rejects_when_no_price\|test_min_notional_skips_when_no_price" tests/test_core.py` → 各 1 命中，且都在 `TestRiskPipeline` 类内
- [ ] `python -m pytest tests/test_core.py -k "no_price" -v` → 2 passed

### Phase 3 反模式守卫
- ❌ 不要把测试加到 `test_execution_pipeline.py`（那是集成测试，且无对应夹具）。
- ❌ 不要给 `MinNotionalMiddleware()` 传构造参数（它没有 `__init__`，阈值来自 `order.instrument.min_notional`）。
- ❌ 不要用 `buy_order` 夹具做无价测试（它带 `limit_price=65000`）。

---

## Phase 4：整体验证

### 4.1 运行测试（无回归）
```bash
# 风控管线单元测试
python -m pytest tests/test_core.py -k "RiskPipeline" -v
# 使用到这两个中间件的集成测试（防回归）
python -m pytest tests/test_execution_pipeline.py tests/test_signal_pipeline.py tests/test_multi_strategy.py -v
# 全量（最终）
python -m pytest tests/ -q
```
预期：全部通过，含 Phase 3 两条新测试。

### 4.2 反模式 grep 终检
```bash
# 内联价格块应已彻底消失
grep -n "est_price = order.limit_price" application/risk/builtin.py   # 期望 0
grep -n "cache_price" application/risk/builtin.py                      # 期望 0
# 辅助方法：1 定义 + 2 调用 = 3 处
grep -rn "_resolve_est_price" application/risk/                        # 期望 3
# Decimal 已导入 pipeline.py
grep -n "from decimal import Decimal" application/risk/pipeline.py     # 期望 1
# 发散语义保留：PositionLimit 仍含拒绝消息
grep -n "无法估算价格" application/risk/builtin.py                     # 期望 1（仅 PositionLimit）
```

### 4.3 行为等价性人工核对
- [ ] PositionLimit：无价 → `RiskResult.reject(...)`（消息含 symbol，level 默认 hard）—— 与重构前一致。
- [ ] MinNotional：无价 → `call_next(order, ctx)` 放行 —— 与重构前一致。
- [ ] 有价路径（limit_price>0 或缓存价>0）：`est_price` 取值与重构前一致，下游 notional/weight 计算不变。

---

## 执行顺序小结
1. **Phase 1** → `pipeline.py` 加 import + 静态方法
2. **Phase 2** → `builtin.py` 替换两处调用点（保留发散语义）
3. **Phase 3** → `test_core.py` 加两条单元测试
4. **Phase 4** → 跑测试 + grep 终检 + 行为核对

> 这是一次低风险纯提取重构。判定成功的硬指标：**全量测试通过**，且 `grep "cache_price" builtin.py` 与 `grep "est_price = order.limit_price" builtin.py` 均为 0 命中。
