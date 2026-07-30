# ZCode-AgentFlow 第二阶段功能需求文档

| 项目 | 内容 |
|---|---|
| 文档类型 | 第二阶段需求收集 / 功能需求规格草案 |
| 创建日期 | 2026-07-30 |
| 第一阶段基线 | `2026-07-30-zcode-agentflow-v1-v2-2.md` |
| 建议目标版本 | v1.1 或独立的第二个 Milestone |
| 文档状态 | Draft for specification and planning |

---

## 1. 文档目的

第一版 ZCode-AgentFlow 仍严格按照已经批准的 v1 Plan 实施，不在当前阶段修改其 Spec、Plan、状态机或任务拆分。

第一版的主要目标是先验证一个可靠的最小闭环：

```text
需求 → SPEC → 用户批准 → PLAN → 执行 → 验证 → Grok 审核 → READY_TO_MERGE
```

第一版完成并验收后，第二阶段增加以下三项能力：

1. **自动返修循环**：审核失败后，在用户配置的次数范围内自动返修、重新验证和重新审核；
2. **流程可观测性**：关键状态变化自动汇报，用户可随时按需查询当前进度；
3. **模型额度可观测性与真实额度门禁**：每次调用 Codex 或 Grok 前后刷新额度，并根据真实额度执行用户配置的门禁。

本文件只负责收集和定义第二阶段需求，不替代第一版 Plan。

---

## 2. 第二阶段总体目标

第二阶段完成后，正常流程应当表现为：

```text
用户提出目标
    ↓
Codex 生成 SPEC
    ↓
用户批准 SPEC
    ↓
Codex 生成 PLAN
    ↓
ZCode 执行任务
    ↓
确定性验证
    ↓
Grok 审核
    ├─ PASS → 完成硬检查 → READY_TO_MERGE
    ├─ FAIL 且仍有返修次数
    │      ↓
    │   ZCode 自动返修
    │      ↓
    │   重新验证
    │      ↓
    │   重新审核
    └─ FAIL 且返修次数耗尽 → NEEDS_HUMAN
```

在整个过程中：

- 每次关键状态变化都会向用户自动汇报；
- 用户可以主动查询当前阶段、停留时间、返修轮次和下一步；
- 每次调用 Codex 或 Grok 前刷新额度；
- 每次调用结束后再次刷新额度；
- 真实剩余额度低于用户门限时，系统暂停并请求用户决策；
- **不增加基于本地调用次数的强制预算上限**；
- **不增加独立 Watch 模式**。

---

## 3. 范围与非目标

### 3.1 本阶段范围

- 自动返修循环；
- 可配置最大返修次数；
- 状态变化自动汇报；
- `/agentflow-status` / `workflow_status` 查询增强；
- Codex/Grok 调用前额度刷新；
- Codex/Grok 调用后额度刷新；
- 基于真实额度信息的预算门禁；
- 对应的 State、Event、Schema、Config、Store、Engine、测试和用户文档。

### 3.2 本阶段非目标

- 多任务 Plan；
- 自动 Replan；
- Final Review；
- 自动 Push、自动 PR、自动 Merge；
- 独立 Watch 模式；
- 定时或持续轮询额度；
- 基于本地调用次数的硬上限；
- 基于估算 Token 数的强制门禁；
- ZCode 原生模型额度采集；
- 更换 Planner / Reviewer Provider；
- 多模型投票；
- 独立系统账户或远程 Control Plane。

---

# 4. 功能一：自动返修循环

## 4.1 需求背景

代码第一次审核不通过是正常情况。如果每次 Grok 返回 FAIL 后都立即进入 `NEEDS_HUMAN`，用户就必须重复执行“查看问题、要求 ZCode 修复、再次验证、再次审核”等操作，自动化价值会明显下降。

第二阶段需要支持：

```text
审核失败 → 自动返修 → 重新验证 → 重新审核
```

只有在返修次数耗尽、出现不可自动处理的问题或触发安全门禁时，才进入 `NEEDS_HUMAN`。

## 4.2 配置要求

建议在 `config.yaml` 中增加：

```yaml
workflow:
  auto_repair:
    enabled: true
    max_repair_attempts: 1
```

字段定义：

| 字段 | 类型 | 默认值 | 含义 |
|---|---:|---:|---|
| `enabled` | bool | `true` | 是否允许自动返修 |
| `max_repair_attempts` | int | `1` | 一次 Run 最多允许的自动返修次数 |

### 次数语义

`max_repair_attempts: 1` 表示：

```text
首次执行
→ 首次审核 FAIL
→ 允许自动返修 1 次
→ 重新验证
→ 再次审核
```

如果第二次审核仍为 FAIL，则进入 `NEEDS_HUMAN`。

建议合法范围：

```text
最小值：0
默认值：1
最大值：5（后续 Spec 可调整）
```

- `0`：关闭自动返修，行为与第一版一致；
- `1`：默认允许一次自动返修；
- 大于 `1`：允许多轮返修；
- 最大值属于返修流程的防循环边界，不属于模型额度的本地预算限制。

## 4.3 状态机变更

建议新增状态：

```text
FIXING_TASK
```

推荐流程：

```text
REVIEWING_TASK
    ├─ PASS
    │    ↓
    │ workflow_complete_task
    │    ↓
    │ READY_TO_MERGE
    │
    ├─ FAIL 且 repair_round < max_repair_attempts
    │    ↓
    │ FIXING_TASK
    │    ↓ ZCode 根据结构化 Review Issues 修改代码
    │ VERIFYING_TASK
    │    ├─ 验证失败 → NEEDS_HUMAN
    │    └─ 验证通过 → REVIEWING_TASK
    │
    ├─ FAIL 且 repair_round >= max_repair_attempts
    │    ↓
    │ NEEDS_HUMAN
    │
    └─ REPLAN / BLOCKED / 高风险问题
         ↓
       NEEDS_HUMAN
```

`FIXING_TASK` 后是否直接进入 `VERIFYING_TASK`，还是先经过 `EXECUTING_TASK`，需要在后续正式 Spec 中统一定义。

## 4.4 返修输入

自动返修必须使用结构化 Repair Bundle，至少包含：

```text
SPEC
PLAN
Task Manifest
Execution Result
Review Result
Review Issues
当前 diff.patch
base_commit
当前 head_commit
repair_round
max_repair_attempts
允许修改的文件范围
验证命令
```

ZCode 的返修指令必须明确：

- 只修复当前 Review 指出的问题；
- 不得修改已批准 SPEC；
- 不得扩大 PLAN 范围；
- 不得修改控制产物；
- 不得引入未批准依赖；
- 修复后必须形成新 commit；
- 修复后必须重新运行验证；
- 新 commit 必须重新审核；
- 旧 Review 不能用于新 commit。

## 4.5 哪些结果可以自动返修

### 允许自动返修

- `decision == FAIL`；
- Issues 属于实现缺陷、测试不足、边界条件、错误处理或代码质量问题；
- `replan_required == false`；
- 不需要扩大任务范围；
- 不涉及未批准依赖或不可逆 migration；
- 当前返修次数未耗尽。

### 不允许自动返修

以下情况直接进入 `NEEDS_HUMAN`：

- `decision == REPLAN`；
- `decision == BLOCKED`；
- `replan_required == true`；
- SPEC 或 PLAN 本身存在问题；
- 需要扩大任务范围；
- 需要不可逆数据迁移；
- 需要未批准依赖；
- 出现安全或权限边界问题；
- Hash、Commit、Artifact绑定不一致；
- 验证命令无效；
- 返修次数已耗尽。

## 4.6 状态与事件记录

`state.json` 建议增加：

```json
{
  "repair_round": 1,
  "max_repair_attempts": 1,
  "last_failed_review_operation_id": "...",
  "last_repair_operation_id": "...",
  "last_repair_commit": "..."
}
```

`events.jsonl` 应记录：

- 审核失败；
- 是否允许自动返修；
- 当前返修轮次；
- 进入 `FIXING_TASK`；
- 返修完成；
- 新 commit；
- 重新验证结果；
- 再次审核结果；
- 返修次数耗尽。

## 4.7 防循环要求

必须满足：

1. `repair_round` 单调递增；
2. 每次返修必须生成新 commit；
3. 新 commit 必须不同于上一次审核绑定的 commit；
4. 每次返修后必须重新验证；
5. 每次重新审核必须绑定新 commit；
6. 达到上限后禁止继续自动返修；
7. 相同 Operation ID 不得创建新返修轮次；
8. 崩溃恢复后不得重复增加 `repair_round`；
9. Review Bundle 必须携带当前返修轮次；
10. 旧 PASS 或旧 FAIL 不得应用于新 commit。

## 4.8 用户可见行为

首次审核失败且允许自动返修：

```text
AgentFlow：审核未通过，准备自动返修

任务：T001
审核结果：FAIL
主要问题：3 项
返修轮次：1 / 1
下一步：ZCode 根据 Review Issues 修复，然后重新验证和审核
需要用户操作：否
```

返修次数耗尽：

```text
AgentFlow：自动返修次数已耗尽

任务：T001
已完成自动返修：1 / 1
最新审核结果：FAIL
剩余问题：2 项
当前状态：NEEDS_HUMAN
需要用户操作：是
```

## 4.9 验收标准

至少覆盖：

- 默认允许一次自动返修；
- 配置为 `0` 时第一次 FAIL 直接进入 `NEEDS_HUMAN`；
- 配置为 `1` 时第一次 FAIL 进入 `FIXING_TASK`；
- 一次返修后的再次 FAIL 进入 `NEEDS_HUMAN`；
- 返修生成新 commit；
- 新 Review绑定新 commit；
- 旧 Review不能完成新 commit；
- REPLAN和BLOCKED不进入自动返修；
- 验证失败不继续 Review；
- 崩溃恢复不会重复增加返修次数；
- 达到上限后不能再次返修；
- 最终 PASS 必须绑定当前 HEAD。

---

# 5. 功能二：状态变化自动汇报与用户按需查询

## 5.1 需求背景

用户在发出目标后，不应完全失去对流程的观察。系统应能回答：

- 当前在哪个阶段；
- 正在执行什么；
- 已经运行多久；
- 是否卡住；
- 是否正在调用 Codex 或 Grok；
- 是否处于返修循环；
- 当前第几轮返修；
- 是否需要用户操作；
- 下一步是什么。

本阶段不实现独立 Watch 模式。

## 5.2 两种观察方式

### 5.2.1 状态变化自动汇报

只有关键状态变化时自动汇报，不持续刷屏、不定时轮询。

应自动汇报的事件至少包括：

- Run创建；
- Worktree创建完成或失败；
- 开始生成 SPEC；
- Codex提出澄清问题；
- SPEC等待批准；
- SPEC批准完成；
- 开始创建 PLAN；
- PLAN创建完成；
- 开始执行 Task；
- 执行完成；
- 开始验证；
- 验证通过或失败；
- 开始 Grok Review；
- Review PASS / FAIL / REPLAN / BLOCKED；
- 开始自动返修；
- 返修完成；
- 返修后重新验证；
- 返修后重新审核；
- 进入 `NEEDS_HUMAN`；
- 到达 `READY_TO_MERGE`；
- 外部调用超时、异常或恢复；
- 额度门禁阻止调用。

### 5.2.2 用户按需查询

用户可随时执行：

```text
/agentflow-status
```

或调用：

```text
workflow_status
```

查询必须是只读操作：

- 不修改 State；
- 不调用 Codex；
- 不调用 Grok；
- 不刷新额度；
- 不产生额外模型消耗。

## 5.3 自动汇报格式

推荐简洁格式：

```text
AgentFlow 进度

目标：增加批量导出 CSV 功能
当前阶段：Grok 正在审核任务
状态：REVIEWING_TASK
任务：T001
审核轮次：2
返修轮次：1 / 1
已运行：28 分 14 秒
需要用户操作：否
下一步：等待 Grok 返回结构化审核结果
```

进入人工节点时：

```text
AgentFlow 需要用户操作

当前状态：NEEDS_HUMAN
原因：自动返修次数耗尽，最新审核仍为 FAIL
任务：T001
返修轮次：1 / 1
剩余问题：2
下一步：请查看审核报告并决定后续处理
```

## 5.4 查询内容

`workflow_status` 至少返回：

```json
{
  "active_run": {
    "run_id": "...",
    "goal": "...",
    "state": "REVIEWING_TASK",
    "revision": 18,
    "started_at": "...",
    "updated_at": "...",
    "state_entered_at": "...",
    "state_age_sec": 126,
    "total_runtime_sec": 1724,
    "current_task": {
      "id": "T001",
      "title": "...",
      "task_start_commit": "...",
      "verified_head_commit": "..."
    },
    "current_operation": {
      "operation_id": "...",
      "tool": "reviewer_review_task",
      "provider": "grok",
      "status": "RUNNING",
      "attempt": 2,
      "started_at": "...",
      "elapsed_sec": 126,
      "timeout_sec": 600
    },
    "progress": {
      "worktree": "PASS",
      "spec": "PASS",
      "spec_approval": "PASS",
      "plan": "PASS",
      "execution": "PASS",
      "verification": "PASS",
      "review": "RUNNING",
      "completion": "PENDING"
    },
    "rounds": {
      "review_round": 2,
      "repair_round": 1,
      "max_repair_attempts": 1
    },
    "need_user_action": false,
    "next_expected_action": "等待 Grok 审核结果"
  }
}
```

## 5.5 状态停留与卡住提示

系统应计算：

```text
state_age = 当前时间 - state_entered_at
operation_elapsed = 当前时间 - operation_started_at
```

可以显示：

```text
NORMAL
LONG_RUNNING
POSSIBLY_STUCK
TIMED_OUT
```

建议配置：

```yaml
observability:
  report_state_changes: true
  long_running_warning_seconds: 300
```

`long_running_warning_seconds` 只用于提示，不负责终止；真正终止由 Adapter timeout控制。

## 5.6 持久化要求

建议持久化：

```json
{
  "started_at": "...",
  "state_entered_at": "...",
  "last_progress_report_at": "...",
  "last_progress_event": "...",
  "current_operation_started_at": "...",
  "current_operation_provider": "grok"
}
```

要求：

- 同一状态事件不得因重启重复汇报；
- 恢复 Operation 不得伪造新的开始事件；
- 汇报失败不能影响状态提交；
- 状态提交成功但汇报失败时，查询仍必须正确；
- 汇报属于展示层，不得成为 Store提交前置条件。

## 5.7 信息安全

自动汇报不得包含：

- API Key；
- OAuth Token；
- 完整环境变量值；
- 未脱敏 stdout/stderr；
- 完整 Prompt；
- 大段源代码；
- 非必要敏感绝对路径。

## 5.8 验收标准

至少覆盖：

- 每个关键状态变化产生进度事件；
- 同一状态事件不会重复汇报；
- 按需查询不修改 State；
- 查询不调用 Codex/Grok；
- 查询显示当前 Operation；
- 查询显示状态停留时间；
- 查询显示返修轮次；
- 查询显示是否需要用户；
- 查询显示下一步；
- 重启恢复后查询正确；
- 汇报失败不影响状态提交；
- 敏感信息不会出现在汇报中；
- READY_TO_MERGE输出最终摘要；
- NEEDS_HUMAN清楚说明原因。

---

# 6. 功能三：调用前后刷新模型额度与使用情况提示

## 6.1 需求背景

Codex 和 Grok 的额度有限。用户需要在调用前知道当前额度是否接近门限，调用后看到最新额度和使用变化。

本阶段采用：

```text
调用前刷新 + 调用后刷新
```

不采用：

- 每秒刷新；
- 定时轮询；
- 独立 Watch；
- 基于本地调用次数的硬预算限制。

## 6.2 Provider范围

只处理：

```text
Codex CLI
Grok Build CLI
```

不采集 ZCode 原生模型额度。

## 6.3 Capability Spike

正式实现前必须验证：

### Codex

- `/status` 是否只能交互使用；
- 是否有非交互或机器可读方式；
- 是否支持 JSON；
- 是否需要 PTY；
- 输出包含哪些额度窗口；
- 是否包含剩余百分比、已用比例或重置时间；
- 查询是否触发模型调用；
- 输出格式是否稳定；
- 是否包含敏感信息。

### Grok

- `/usage` 是否只能交互使用；
- 是否有非交互或机器可读方式；
- 是否支持 JSON；
- 是否需要 PTY；
- 输出包含哪些额度或 Credits；
- 是否包含重置时间；
- 查询是否触发模型调用；
- 输出格式是否稳定；
- 是否包含敏感信息。

不得假设机器可读接口一定存在。

## 6.4 Usage Provider

建议新增：

```text
CodexUsageProvider
GrokUsageProvider
```

统一输出示例：

```json
{
  "provider": "codex",
  "available": true,
  "captured_at": "...",
  "source": "official_cli_json",
  "reliability": "official",
  "windows": [
    {
      "name": "weekly",
      "used_percent": 59,
      "remaining_percent": 41,
      "resets_at": "..."
    }
  ],
  "error": null
}
```

如果无法自动读取：

```json
{
  "provider": "grok",
  "available": false,
  "captured_at": "...",
  "source": "interactive_only",
  "reliability": "manual",
  "windows": [],
  "error": "当前 CLI 只能通过交互式 /usage 查看"
}
```

## 6.5 调用前刷新

每次准备调用 Codex 或 Grok 前：

```text
1. 刷新对应 Provider额度；
2. 脱敏并保存 Before Snapshot；
3. 根据用户配置执行真实额度门禁；
4. 向用户显示调用前额度摘要；
5. 门禁通过后才调用模型。
```

示例：

```text
AgentFlow：准备调用 Grok 审核

额度刷新时间：16:20:05
Grok 剩余额度：53%
用户门限：15%
额度状态：OK
下一步：开始 reviewer_review_task
```

## 6.6 调用后刷新

模型调用完成后，无论成功、失败还是超时，都尝试再次刷新额度：

```text
模型调用结束
→ 刷新额度
→ 保存 After Snapshot
→ 展示最新额度
```

示例：

```text
AgentFlow：Grok 审核调用结束

调用结果：成功
调用前剩余：53%
调用后剩余：49%
观察变化：-4%
最后刷新：16:23:41
```

“观察变化”必须标记为近似值，不能宣称是本次调用的精确消耗，因为服务端统计可能延迟，也可能存在其他会话。

## 6.7 真实额度门禁

用户需要硬预算门禁，但不需要本地调用次数上限。

建议配置：

```yaml
usage:
  enabled: true
  refresh_before_call: true
  refresh_after_call: true
  on_usage_unavailable: warn_and_continue

  providers:
    codex:
      enabled: true
      gate_window: weekly
      minimum_remaining_percent: 15

    grok:
      enabled: true
      gate_window: weekly
      minimum_remaining_percent: 15
```

`on_usage_unavailable` 支持：

```text
warn_and_continue
require_user_confirmation
block
```

建议默认值：

```text
warn_and_continue
```

因为用户明确不希望在无法读取额度时，再引入本地调用次数限制。

## 6.8 门禁行为

### 额度充足

```text
remaining >= threshold
→ 允许调用
```

### 额度不足

```text
remaining < threshold
→ 不调用模型
→ 进入 NEEDS_HUMAN，reason=USAGE_BUDGET_GATE
→ 告知用户当前额度、门限和原计划动作
```

提示：

```text
AgentFlow 已暂停模型调用

Provider：Grok
当前剩余额度：12%
用户门限：15%
原计划动作：reviewer_review_task
本次 Grok 调用：尚未发生

请选择：
1. 本次临时允许继续；
2. 修改额度门限；
3. 等待额度重置；
4. 终止本次 Run。
```

## 6.9 无法自动读取额度

如果 Usage Provider不可用：

- 不得伪造额度；
- 不得把陈旧数据当作当前数据；
- 显示 `available: false`；
- 显示最后成功刷新时间；
- 按 `on_usage_unavailable` 处理；
- 不增加本地调用次数上限；
- 不使用 OCR抓取 TUI；
- 不依赖屏幕坐标自动化。

## 6.10 Usage持久化

建议保存：

```text
usage/
├── codex/
│   ├── before-<operation_id>.json
│   └── after-<operation_id>.json
└── grok/
    ├── before-<operation_id>.json
    └── after-<operation_id>.json
```

每个 Snapshot 至少包含：

```json
{
  "provider": "codex",
  "phase": "before",
  "related_operation_id": "...",
  "captured_at": "...",
  "available": true,
  "source": "...",
  "reliability": "...",
  "windows": [],
  "error": null
}
```

## 6.11 状态查询中的额度

`workflow_status` 返回最近一次缓存：

```json
{
  "usage": {
    "codex": {
      "available": true,
      "captured_at": "...",
      "remaining_percent": 41,
      "window": "weekly",
      "threshold_percent": 15,
      "gate_status": "OK"
    },
    "grok": {
      "available": false,
      "captured_at": "...",
      "gate_status": "UNAVAILABLE",
      "policy": "warn_and_continue"
    }
  }
}
```

用户按需查询只读取缓存，不触发新的额度刷新。

## 6.12 验收标准

至少覆盖：

- Codex调用前刷新；
- Codex调用后刷新；
- Grok调用前刷新；
- Grok调用后刷新；
- 调用失败后仍尝试 After刷新；
- 调用超时后仍尝试 After刷新；
- 低于真实门限时不发起模型调用；
- 额度充足时允许调用；
- Usage不可用时按配置处理；
- `warn_and_continue` 不阻止调用；
- `require_user_confirmation` 等待用户；
- `block` 阻止调用；
- 不存在本地调用次数硬上限；
- 状态查询显示最近 Usage Snapshot；
- 状态查询本身不刷新额度；
- Usage输出落盘前脱敏；
- Before/After绑定正确 Operation ID；
- 观察变化不会被声明为精确 Token消耗。

---

# 7. 三项功能组合后的完整流程

```text
准备调用 Codex
    ↓
刷新 Codex额度（Before）
    ↓
真实额度门禁
    ├─ 不通过 → NEEDS_HUMAN
    └─ 通过
         ↓
      调用 Codex
         ↓
      刷新 Codex额度（After）
         ↓
      自动汇报状态变化
         ↓
      用户批准 SPEC
         ↓
      再次执行 Codex Before → 门禁 → Create Plan → After
         ↓
      ZCode执行
         ↓
      确定性验证
         ↓
      Grok Before → 门禁 → Review → After
         ↓
      自动汇报审核结果
         ├─ PASS → READY_TO_MERGE
         ├─ FAIL且有返修次数
         │      ↓
         │   FIXING_TASK
         │      ↓
         │   重新验证
         │      ↓
         │   再次执行 Grok Before → 门禁 → Review → After
         └─ FAIL且次数耗尽 → NEEDS_HUMAN
```

---

# 8. 配置草案

```yaml
workflow:
  auto_repair:
    enabled: true
    max_repair_attempts: 1

observability:
  report_state_changes: true
  long_running_warning_seconds: 300
  status_query_reads_cached_usage: true

usage:
  enabled: true
  refresh_before_call: true
  refresh_after_call: true
  on_usage_unavailable: warn_and_continue

  providers:
    codex:
      enabled: true
      gate_window: weekly
      minimum_remaining_percent: 15

    grok:
      enabled: true
      gate_window: weekly
      minimum_remaining_percent: 15
```

明确不加入：

```yaml
max_codex_calls_per_run:
max_grok_calls_per_run:
max_total_external_calls:
estimated_token_budget:
```

---

# 9. 对第一版架构的影响

第二阶段不推翻第一版架构，而是复用：

- Engine；
- Store；
- 两阶段 Operation事务；
- ArtifactResolver；
- Raw/Final Schema分层；
- Git Worktree；
- `workflow_status`；
- `/agentflow-status`；
- Adapter基类；
- `events.jsonl`；
- State Revision；
- Operation Lease；
- Hook与Skill。

主要新增：

```text
状态：
  FIXING_TASK

Engine：
  自动返修路由
  返修次数控制
  Usage门禁

Adapters：
  CodexUsageProvider
  GrokUsageProvider

Store / State：
  repair_round
  state_entered_at
  operation_started_at
  Usage Snapshot引用

UI / Command：
  状态变化自动汇报
  增强 /agentflow-status

Schema：
  Repair Bundle
  Usage Snapshot
  扩展 State/Event字段

Config：
  auto_repair
  observability
  usage
```

---

# 10. 建议实施顺序

第一版完成后，建议第二阶段按以下顺序进行：

## Phase A：Capability Spike

验证：

- Codex额度是否可机器读取；
- Grok额度是否可机器读取；
- ZCode主会话中状态变化消息的可靠展示方式；
- ZCode长任务期间能否接受 `/agentflow-status`；
- 返修操作如何驱动 ZCode继续执行并形成新 commit。

## Phase B：正式 Spec

定义：

- `FIXING_TASK`；
- 自动返修状态转移；
- 次数语义；
- Review分类；
- Usage Snapshot；
- 额度门禁；
- 状态汇报事件；
- 查询格式。

## Phase C：Implementation Plan

建议拆为：

1. Schema和Config；
2. Store/State扩展；
3. Usage Provider；
4. Usage Gate；
5. 状态自动汇报；
6. 状态查询增强；
7. Repair Bundle；
8. 自动返修 Engine；
9. Hook/Skill/Command更新；
10. E2E和真实 Smoke。

---

# 11. 第二阶段完成标准

第二阶段完成需要同时满足：

1. 默认允许一次自动返修；
2. 用户可以配置返修次数；
3. FAIL在允许范围内自动返修；
4. 返修次数耗尽后进入 `NEEDS_HUMAN`；
5. REPLAN、BLOCKED和高风险问题不会错误返修；
6. 每次返修后重新验证；
7. 每次审核绑定新 commit；
8. 关键状态变化自动汇报；
9. 用户可按需查询状态；
10. 查询显示阶段、停留时间、Operation、返修轮次、下一步和是否需要用户；
11. 每次 Codex/Grok调用前刷新额度；
12. 每次 Codex/Grok调用后刷新额度；
13. 低于真实额度门限时阻止调用；
14. Usage不可用时按用户配置降级；
15. 不增加本地调用次数硬上限；
16. 不提供独立 Watch模式；
17. 所有新状态和证据可在重启后恢复；
18. E2E覆盖：首次审核 FAIL → 自动返修一次 → 再次审核 PASS → `READY_TO_MERGE`。

---

# 12. 待正式 Spec 确认的问题

1. 自动返修是否新增 MCP Tool，还是复用现有工具；
2. `FIXING_TASK` 后直接进入 `VERIFYING_TASK`，还是先回到 `EXECUTING_TASK`；
3. 额度不足时使用新状态，还是复用 `NEEDS_HUMAN`；
4. Usage Snapshot是否进入Artifact Generation；
5. 自动汇报通过Tool返回、Command消息还是Hook反馈通道完成；
6. Codex/Grok额度是否存在稳定机器可读接口；
7. 门限是否只支持百分比，还是同时支持 Credits；
8. 多个额度窗口存在时，门禁使用指定窗口还是最紧张窗口；
9. 用户是否可以针对某次调用临时越过额度门禁；
10. 自动返修过程中额度不足时，恢复后回到哪个状态。

---

## 13. 最终需求摘要

### 自动化

```text
审核失败
→ 在用户配置范围内自动返修
→ 重新验证
→ 重新审核
```

默认自动返修一次。

### 可观测性

```text
关键状态变化自动汇报
+
用户随时按需查询
```

不增加独立 Watch模式。

### 额度透明与门禁

```text
模型调用前刷新额度
→ 执行真实额度门禁
→ 模型调用
→ 调用后再次刷新额度
→ 展示使用情况变化
```

不增加基于本地调用次数的硬预算上限。
