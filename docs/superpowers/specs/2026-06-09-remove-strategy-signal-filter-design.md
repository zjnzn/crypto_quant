# 设计文档：移除策略层信号过滤

**日期**: 2026-06-09
**作者**: Claude
**状态**: 待实施

## 概述

移除策略（MomentumStrategy）内部的信号过滤逻辑，实现关注点分离：策略专注信号生成，组合层（PortfolioService）统一负责信号过滤。

## 动机

**当前问题**：
- 策略内部过滤信号导致职责不清
- 信号过滤逻辑分散在策略层和组合层
- 策略参数 `min_score` 与全局配置重复

**目标**：
- 策略层专注信号生成和质量评分
- 组合层统一管理信号过滤和仓位计算
- 简化策略配置，减少冗余参数

## 架构设计

### 当前架构

```
MomentumStrategy.on_trade()
  ├─ 计算信号 (score, confidence)
  ├─ 过滤弱信号：if abs(score) < min_score: return []
  └─ 发送信号 → PortfolioService

PortfolioService._on_signal()
  ├─ 接收信号
  ├─ 合并多策略信号
  └─ 过滤弱信号（min_score 状态机）
```

### 变更后架构

```
MomentumStrategy.on_trade()
  ├─ 计算信号 (score, confidence)
  └─ 发送所有信号 → PortfolioService

PortfolioService._on_signal()
  ├─ 接收所有信号
  ├─ 合并多策略信号
  └─ 统一过滤（min_score 状态机）
```

### 设计原则

**关注点分离**：
- **策略层**：信号生成器，输出带质量评分的原始信号
- **组合层**：信号聚合器，负责过滤、合并、仓位计算

## 影响范围

### 代码变更

#### 1. MomentumStrategy 类

**文件**: `strategies/momentum.py`

变更内容：
- 移除 `__init__` 参数 `min_score: float = 0.15`（第52行）
- 移除实例变量 `self._min_score`（第56行）
- 移除 `on_trade` 中的信号过滤逻辑（第81-82行）：
  ```python
  # 删除以下代码
  if abs(score) < self._min_score:
      return []
  ```
- 更新 `on_start` 日志输出（第108行），移除 `min_score` 参数显示

#### 2. 配置文件

**文件**: `config_live.yaml`

变更内容：
- 移除策略参数中的 `min_score: 0.60`（第52行）
- 保留全局风险配置中的 `min_score: 0.60`（第41行），供 PortfolioService 使用

变更后配置：
```yaml
strategies:
  - module: "strategies.momentum.MomentumStrategy"
    params:
      window:    20
      scale:     0.001
      # min_score: 0.60  # 已移除，由组合层统一过滤
```

### 不受影响的部分

#### PortfolioService 的 min_score 逻辑

**文件**: `application/services/portfolio.py:135-155`

保持不变：
- `__init__` 的 `min_score` 参数
- `_on_signal` 中的状态机过滤逻辑：
  - 无持仓 + 弱信号 → 不开仓
  - 有持仓 + 弱信号 → 保持最低持仓

#### 其他策略类

- `MeanReversionStrategy`（如启用需同步移除 `min_score`）
- `FundingRateArbStrategy`（如启用需同步移除 `min_score`）

## 行为变更分析

### 功能等价性

| 场景 | 变更前 | 变更后 | 结果 |
|------|--------|--------|------|
| 无持仓 + 弱信号 | 策略不发出信号 | 策略发出弱信号，组合层过滤 | 相同（不开仓） |
| 有持仓 + 弱信号 | 策略不发出信号 | 策略发出弱信号，组合层保持最低持仓 | 相同 |
| 强信号 | 策略发出信号 | 策略发出信号 | 相同 |

**结论**: 最终仓位计算结果完全相同，功能等价。

### 性能影响

**信号量变化**：
- 变更前：策略仅发出强信号（过滤后）
- 变更后：策略发出所有信号（包括弱信号）

**影响分析**：
- 信号事件数量增加，但量级很小（每秒数十个交易信号）
- 事件总线性能开销可忽略
- 组合层过滤逻辑不变，计算开销相同

### 架构改进

**优点**：
- ✅ 关注点分离：策略职责更清晰
- ✅ 统一控制：组合层集中管理信号过滤
- ✅ 配置简化：移除策略层冗余参数
- ✅ 扩展性：未来可在组合层实现更复杂的信号筛选逻辑

**风险**：
- ⚠️ 信号量增加（已分析，影响可忽略）
- ⚠️ 其他策略类需同步修改（如有启用）

## 测试策略

### 1. 单元测试

**test_momentum.py** 更新：
- 移除测试弱信号过滤的用例
- 新增测试用例：
  ```python
  def test_emits_weak_signal():
      """验证策略发出弱信号"""
      strategy = MomentumStrategy(window=20, scale=0.05)
      # ... 模拟价格数据
      signals = strategy.on_trade(event, ctx)
      # 验证弱信号也被发出
      assert len(signals) == 1
      assert abs(signals[0].score) < 0.15  # 弱信号
  ```

**test_portfolio.py**：
- 保持不变，组合层过滤逻辑已有测试覆盖

### 2. 集成测试

验证端到端行为：
```python
def test_end_to_end_signal_flow():
    """验证策略发出弱信号 → 组合层正确过滤"""
    # 1. 策略发出弱信号
    # 2. PortfolioService 接收信号
    # 3. 验证仓位计算正确（无持仓 + 弱信号 = 不开仓）
```

### 3. 回归测试

运行完整测试套件，确保无功能回归：
```bash
pytest tests/ -v
```

## 实施计划

### 阶段 1：代码变更
1. 修改 `strategies/momentum.py`
2. 更新 `config_live.yaml`
3. 更新相关测试用例

### 阶段 2：验证
1. 运行单元测试
2. 运行集成测试
3. 回测验证（可选）

### 阶段 3：清理
1. 检查其他策略类（如有启用需同步修改）
2. 更新文档（如有）

## 风险评估

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| 其他策略类未同步修改 | 中 | 低 | 检查所有策略类，确保一致性 |
| 配置文件遗漏更新 | 低 | 低 | 提供明确的配置变更说明 |
| 测试覆盖不足 | 中 | 低 | 完整运行测试套件 |

## 参考资料

- 相关代码：
  - [strategies/momentum.py](../../strategies/momentum.py)
  - [application/services/portfolio.py](../../application/services/portfolio.py)
  - [config_live.yaml](../../config_live.yaml)
- 相关提交：
  - cc_merge3 分支已验证完整测试通过
