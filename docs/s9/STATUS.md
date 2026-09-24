# S9 阶段：统一摘要 + 智能自动压缩

更新：2026-09-24。**分支 `s9`，从 `s8` 干净状态创建。**

## 工作区

- 主目录：`F:\exercise\2026\kamaclaude - v1\KamaClaude`
- 分支起点：`s8` 分支 HEAD，提交 `cc352fe`
- 新分支：`s9`（本地，未推送）
- 解释器：Python 3.12.14，`.venv` 复用

## S8 留到 S9 的尾巴

| # | 尾巴 | 在 S8 里的状态 |
|---|------|--------------|
| 1 | `/compress`（compactor）和 `/summarize`（skill）两套摘要各自为政，不共享存储 | 确认重叠 |
| 2 | `CompactionConfig.auto_threshold=0.0` 默认关闭，context_pct 百分比阈值对长/短上下文模型一刀切 | 确认不生效 |
| 3 | `tool_result_limit/keep` 配置存在但 loop.py 没接线 | 确认未使用 |
| 4 | 无人值守自动恢复为 0 场景（STATUS.md 明确写了） | 保守优先策略保留 |

---

## S9.1 统一摘要系统

### 问题

- **compactor**（`compactor.py`）→ handoff 格式硬编码 prompt → 写 `summaries` 表 → 给模型恢复上下文用
- **summarize skill**（`summarize.md`）→ 人类可读格式 Markdown → 用 `note_save` 写 session notes → 给人看
- 两者不共享、不关联：summaries 表里永远只有 handoff 格式，session notes 里永远只有人类可读格式

### 设计

```
                    ┌────────────────────┐
                    │   summaries 表     │
                    │  (新增 kind 列)    │
                    └────────┬───────────┘
                             │
              ┌──────────────┼───────────────┐
              │              │               │
    handoff 格式       human_readable    human_readable + handoff
    (compactor         (skill 执行完)     (compactor 执行完派生)
     直接写)            自动写)
```

1. **schema v3 → v4**：`summaries` 表加 `summary_kind TEXT NOT NULL DEFAULT 'handoff'`
2. **compactor** 执行完，自动派生人类可读版本（一个额外的 LLM call，轻量）
3. **summarize skill** 跑完，SessionManager hook 把生成的人类可读文本也写一份到 summaries 表
4. 两种摘要共存：恢复时优先用 handoff 格式（最完整），人类阅读时看 human_readable

### 验收标准

- [ ] summaries 表 schema v4，有 `summary_kind` 列，旧数据自动迁移
- [ ] `/summarize` 执行完后，summaries 表多一条 `kind='human_readable'` 的记录
- [ ] compactor 执行完后，summaries 表多两条（handoff + human_readable 派生版）
- [ ] S8.5 故障矩阵 20/20 全绿（schema migration 不破坏已有流程）
- [ ] 380 单元测试全绿

---

## S9.2 智能自动压缩

### 问题

1. `loop.py:204` 只看绝对百分比 `context_pct >= 0.80`，一刀切
2. `tool_result_limit=8000/keep=4000` 配置存在但 loop.py 没接线（工具调用结果会原样塞进 context，爆 token 主要原因）
3. 触发逻辑只有 tool_use 之后才检查，run 继续但下次 LLM 调用可能已经爆了

### 设计（分两步，从低风险到高风险）

#### Step 1：接线 tool_result_limit/keep（立即生效）

在 `loop.py` 里，每次 append tool_result 消息时：
```
if len(result_text) > tool_result_limit:
    result_text = result_text[:tool_result_keep] + f"\n... (截断，总长 {len(result_text)})"
```

#### Step 2：动态阈值启发式

```python
# 记录最近 N 步的 token 使用
token_history: list[int] = []  # 最近 5 步的 output_tokens

# 增长速率
growth_per_step = (token_history[-1] - token_history[0]) / max(1, len(token_history) - 1)

# 预测：再 3 步会到多少
projected = current_context_pct + growth_per_step * 3 / (context_window)

# 触发策略（默认 balanced）
if projected >= 0.80:  # 提前 3 步触发
    触发分级压缩()
```

#### Step 3：分级压缩策略

| 策略 | 触发阈值 | 做什么 | 代价 |
|------|---------|--------|------|
| light | projected ≥ 0.70 | 只截断 tool_result（已有 limit/keep） | 0 LLM call |
| medium | projected ≥ 0.80 | 只保留最近 K=5 轮消息 | 0 LLM call |
| heavy | projected ≥ 0.90 | handoff 格式完整压缩（compactor） | 1 LLM call |

### 验收标准

- [ ] `loop.py` 里 tool_result 超过 limit 时自动截断
- [ ] 记录 token_history，动态计算增长速率
- [ ] 自动压缩默认开启（`auto_threshold=0.0` 改成 smart 策略）
- [ ] 分级压缩：light / medium / heavy
- [ ] 配置化策略：`CompactionConfig.policy = "balanced"` / `"aggressive"` / `"conservative"`
- [ ] 单元测试全绿
- [ ] 真实运行验证：step=9 的长对话不爆 token

---

## 不在 S9 范围

- S9.3 无人值守自动恢复（STATUS.md 保守优先策略，不做）
- S9.4 子任务完整生命周期
- S9.5 SQLite 性能优化（分区 / 归档）
- S9.6 故障矩阵 CI 集成
