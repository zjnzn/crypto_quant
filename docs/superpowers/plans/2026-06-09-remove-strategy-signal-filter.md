# 移除策略层信号过滤实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除 MomentumStrategy 的信号过滤逻辑，实现关注点分离：策略专注信号生成，组合层统一过滤。

**Architecture:** 删除策略内部 `min_score` 参数和过滤逻辑，保留 PortfolioService 的 `min_score` 状态机。变更后策略发出所有信号，组合层统一过滤。

**Tech Stack:** Python 3.x, pytest, Decimal

---

## Task 1: 移除 MomentumStrategy 的 min_score 参数

**Files:**
- Modify: `strategies/momentum.py:48-56` (移除 `__init__` 参数)
- Modify: `strategies/momentum.py:81-82` (移除过滤逻辑)
- Modify: `strategies/momentum.py:108-109` (更新日志输出)

- [ ] **Step 1: 移除 __init__ 的 min_score 参数**

打开 `strategies/momentum.py`，定位到 `__init__` 方法（第48-56行），移除 `min_score` 参数和实例变量：

```python
def __init__(
    self,
    window:    int   = 20,
    scale:     float = 0.05,
    # min_score: float = 0.15,  # 已删除
) -> None:
    self._window    = window
    self._scale     = scale
    # self._min_score = min_score  # 已删除
    # deque 自动限制窗口长度（满后自动丢弃最旧值）
    self._prices: dict[str, deque[Decimal]] = {}
```

- [ ] **Step 2: 移除 on_trade 中的信号过滤逻辑**

定位到 `on_trade` 方法（第81-82行），删除过滤逻辑：

```python
# 删除以下两行
# if abs(score) < self._min_score:
#     return []
```

变更后的代码直接返回信号：

```python
# 3. 计算动量分数
prices = list(buf)
score, confidence = self._calc_score(prices)

# 4. 发出信号（不再过滤）
return [Signal(
    instrument  = event.instrument,
    score       = score,
    confidence  = confidence,
    strategy_id = self.name,
    meta        = {
        "ret":   float(prices[-1] / prices[0] - 1),
        "window": self._window,
    },
)]
```

- [ ] **Step 3: 更新 on_start 日志输出**

定位到 `on_start` 方法（第108-109行），移除 `min_score` 参数显示：

```python
def on_start(self, ctx: "StrategyContext") -> None:
    log.info("%s 启动  window=%d  scale=%.2f%%",
             self.name, self._window, self._scale * 100)
```

- [ ] **Step 4: 验证修改正确**

运行相关测试确认语法正确：

```bash
pytest tests/test_signal_pipeline.py -v -k "test_" --collect-only
```

预期：测试收集成功，无语法错误。

- [ ] **Step 5: 提交代码变更**

```bash
git add strategies/momentum.py
git commit -m "refactor: 移除 MomentumStrategy 的 min_score 参数和信号过滤逻辑"
```

---

## Task 2: 更新配置文件

**Files:**
- Modify: `config_live.yaml:52` (移除策略参数中的 min_score)

- [ ] **Step 1: 移除策略参数中的 min_score**

打开 `config_live.yaml`，定位到第52行，删除或注释 `min_score: 0.60`：

```yaml
strategies:
  - module: "strategies.momentum.MomentumStrategy"
    params:
      window:    20
      scale:     0.001  # scale 随杠杆调整：0.0020/5=0.0004，保持信号-仓位映射不变
      # min_score: 0.60  # 已移除，由 PortfolioService 统一过滤
```

**注意**：保留第41行全局风险配置中的 `min_score: 0.60`，供 PortfolioService 使用。

- [ ] **Step 2: 提交配置变更**

```bash
git add config_live.yaml
git commit -m "config: 移除策略层 min_score 配置参数"
```

---

## Task 3: 更新测试代码

**Files:**
- Modify: `tests/test_signal_pipeline.py:117` (移除 min_score 参数)
- Modify: `tests/test_phase5.py:415` (移除 min_score 参数)
- Modify: `tests/test_multi_strategy.py:多处` (移除 min_score 参数)
- Modify: `tests/test_multi_strategy.py:503-507` (更新 patched_init 函数)

- [ ] **Step 1: 更新 test_signal_pipeline.py**

定位到第117行，移除 `min_score=0.15`：

```python
strategy = MomentumStrategy(window=20, scale=0.05)
```

- [ ] **Step 2: 更新 test_phase5.py**

定位到第415行，移除 `min_score` 参数：

```python
{"module": "strategies.momentum.MomentumStrategy",
 "params": {"window": 20, "scale": 0.05}},
```

- [ ] **Step 3: 更新 test_multi_strategy.py**

搜索所有 `MomentumStrategy` 实例化，移除 `min_score` 参数：

- 第258行：`MomentumStrategy(window=20, scale=0.05)`
- 第300行：`MomentumStrategy(window=5, scale=0.02)`
- 第314行：`MomentumStrategy(window=5, scale=0.02)`
- 第354行：`MomentumStrategy(window=5, scale=0.02, name_override="m1")`
- 第356行：`MomentumStrategy(window=8, scale=0.03, name_override="m2")`
- 第373行：`MomentumStrategy(window=5, scale=0.02)`
- 第406行：`MomentumStrategy(window=20, scale=0.05)`
- 第480行：`MomentumStrategy(window=5, scale=0.02)`

- [ ] **Step 4: 更新 test_multi_strategy.py 的 patched_init 函数**

定位到第503-507行，移除 `min_score` 参数：

```python
def _patched_init(self, window=20, scale=0.05,
                  name_override=None):
    _orig_init(self, window=window, scale=scale)
    if name_override:
        self.name = name_override
```

- [ ] **Step 5: 验证测试语法正确**

```bash
pytest tests/ --collect-only
```

预期：所有测试收集成功，无语法错误。

- [ ] **Step 6: 提交测试变更**

```bash
git add tests/test_signal_pipeline.py tests/test_phase5.py tests/test_multi_strategy.py
git commit -m "test: 移除测试中的 MomentumStrategy min_score 参数"
```

---

## Task 4: 运行完整测试套件

**Files:**
- 无文件修改，仅验证

- [ ] **Step 1: 运行所有测试**

```bash
pytest tests/ -v
```

预期：所有测试通过，无失败。

- [ ] **Step 2: 检查测试输出**

验证关键测试：
- `test_signal_pipeline.py` - 信号管道完整性
- `test_position_strategy.py` - PortfolioService 的状态机逻辑
- `test_multi_strategy.py` - 多策略信号合并

如有失败，检查错误信息并修复。

- [ ] **Step 3: 确认无回归**

统计测试结果：

```bash
pytest tests/ --tb=no -q
```

预期输出类似：
```
215 passed in 12.34s
```

---

## Task 5: 验证功能等价性

**Files:**
- 无文件修改，仅验证

- [ ] **Step 1: 理解测试验证逻辑**

PortfolioService 的状态机测试 (`test_position_strategy.py`) 已验证：
- 无持仓 + 弱信号 → 不开仓
- 有持仓 + 弱信号 → 保持最低持仓

这些测试确保功能等价性。

- [ ] **Step 2: 运行状态机测试**

```bash
pytest tests/test_position_strategy.py -v
```

预期：所有测试通过，验证组合层过滤逻辑正确。

- [ ] **Step 3: 运行信号管道测试**

```bash
pytest tests/test_signal_pipeline.py -v
```

预期：所有测试通过，验证信号从策略到组合层的完整流程。

---

## Task 6: 检查其他策略类

**Files:**
- Modify: `strategies/mean_reversion.py` (如存在)
- Modify: `strategies/funding_rate_arb.py` (如存在)

- [ ] **Step 1: 检查 MeanReversionStrategy**

```bash
grep -n "min_score" strategies/mean_reversion.py
```

如果存在 `min_score` 参数，按照 Task 1 的步骤移除。

- [ ] **Step 2: 检查 FundingRateArbStrategy**

```bash
grep -n "min_score" strategies/funding_rate_arb.py
```

如果存在 `min_score` 参数，按照 Task 1 的步骤移除。

- [ ] **Step 3: 如有修改，运行测试**

```bash
pytest tests/ -v
```

- [ ] **Step 4: 如有修改，提交变更**

```bash
git add strategies/
git commit -m "refactor: 移除其他策略的 min_score 参数"
```

---

## Task 7: 最终验证和清理

**Files:**
- 无文件修改，仅验证

- [ ] **Step 1: 运行完整测试套件**

```bash
pytest tests/ -v --tb=short
```

预期：所有测试通过（215个测试）。

- [ ] **Step 2: 检查代码覆盖率**

确认关键代码路径已测试：
- MomentumStrategy.on_trade - 发出所有信号
- PortfolioService._on_signal - 状态机过滤逻辑

- [ ] **Step 3: 验证配置文件**

```bash
cat config_live.yaml | grep -A 3 "strategies:"
```

确认策略参数中无 `min_score`，全局风险配置中保留 `min_score`。

- [ ] **Step 4: 最终提交（如有遗漏）**

```bash
git status
```

确认所有变更已提交。

---

## 实施总结

**预期结果：**
1. ✅ MomentumStrategy 移除 `min_score` 参数和过滤逻辑
2. ✅ 策略发出所有信号（包括弱信号）
3. ✅ PortfolioService 的 `min_score` 状态机逻辑保持不变
4. ✅ 配置文件更新，移除策略层 `min_score`
5. ✅ 所有测试通过，功能等价性验证
6. ✅ 其他策略类同步更新（如存在）

**关键验证点：**
- 测试套件全部通过（215个测试）
- PortfolioService 状态机逻辑正确（`test_position_strategy.py`）
- 信号管道完整（`test_signal_pipeline.py`）
- 多策略合并正确（`test_multi_strategy.py`）

**回滚策略：**
如遇问题，可通过 `git revert` 回滚到上一个提交：
```bash
git log --oneline -5
git revert <commit-hash>
```
