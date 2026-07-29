# ZCode-AgentFlow 可行性设计文档（Specs）

| 项 | 值 |
|---|---|
| 文档状态 | **Approved for planning** (v3.1，已纳入复评通过后的三个小修订) |
| 创建日期 | 2026-07-29 |
| 方案路线 | 路线 B：ZCode 主控 + Codex 规划 + Grok 审核 |
| 实现语言 | Python |
| 人工门（v1） | SPEC 审批 + 最终批准（共两道） |
| 部署形态 | 用户级（任意项目可复用，不绑定特定仓库） |
| 覆盖范围 | 全景架构定义 + v1 最小可构建子集（垂直切片） |

> **v3.1 修订摘要**（相对 v3，复评通过后的三个小修订，不再评审）：①补全 `planner_draft_spec` 结果分支（questions 空→READY_FOR_SPEC_APPROVAL，非空→NEEDS_USER_INPUT 并携带 answers 重入）；②修正 operation 提交协议写入顺序（增加 RUNNING 态、明确 state_after.json 在 append event 前写入并 flush、RUNNING 崩溃不无条件重放而是按 result.json 完整性 / UNKNOWN 标记分流）；③批准机制降级改为显式配置 `allow_non_adversarial_fallback`（默认 false 即不支持时启动失败，true 才降级并提示用户保障等级降低）。文档状态变更为 **Approved for planning**。
>
> **v3 修订摘要**（相对 v2）：①v1 终态改为 `READY_TO_MERGE`（不维护 MERGED），消除合并闭环缺口；②诚实降级威胁模型——MCP 提供流程完整性校验，不防同权限恶意进程；③人工批准改为可信识别（ZCode 强制确认 + 批准记录）；④四根目录模型（加 `control_root`），worktree 移出 repo；⑤所有 hash 由 MCP 对规范字节计算，不信任模型提供值；⑥events.jsonl 含可恢复状态 + 完整 operation 提交协议；⑦命令策略拆成"验证命令策略"+"Git 变更策略"两层。
>
> **v2 保留**：版本切片 v1/v1.1/v1.2/v1.3、批准后生成 Plan、单 Task 强制、Review 绑定多摘要、跨字段不变式、worktree clean 检查、operation_id/revision/文件锁方向、CLI flag 实现期 capability check、Plugin Beta 风险显式化。

---

## 0. 目标与非目标

### 0.1 要解决的问题

用户在三个独立 Agent（Codex / ZCode / Grok Build）之间手工充当 orchestrator：复制目标 → 等待 → 确认需求 → 找 plan 文件 → 切到 ZCode 执行 → 中途反复确认 → 切到 Grok 审核 → 把结果复制回 ZCode。
痛点不是"缺更强的模型"，而是**三者之间没有统一的任务协议、状态机和自动交接机制**，全程人工驱动，自动化不足。

### 0.2 目标（What）

构建一个 **ZCode 驱动的 AgentFlow**：

- ZCode 主会话（GLM-5.2）作为 **Supervisor + Executor**：与用户沟通、执行代码任务、调度规划与审核、根据审核修复。
- Codex CLI（GPT，ChatGPT 账号登录）作为 **Planner**：需求分析、写 SPEC、写 PLAN、（v1.2+）Replan。
- Grok Build CLI（Grok-4.5，xAI 账号登录）作为 **Reviewer**：逐任务审核、（v1.3+）最终审核，输出结构化 PASS/FAIL。
- 三者通过**版本化文件 + JSON Schema 协议**交接，**不通过自然语言聊天记录交接**。
- 流程状态持久化在文件中，由状态机驱动，而非依赖 ZCode 聊天上下文。
- **用户级通用部署**：不绑定任何特定业务仓库，`target_repo` 作为运行时参数传入。

### 0.3 非目标（明确排除）

- ❌ 不重写 ZCode 的 Goal Mode / 权限 / 验证机制（复用 ZCode 原生能力）。
- ❌ 不追求零人工参与；v1 保留 SPEC 批准 + 最终批准两道门。
- ❌ v1 不维护 `MERGED` 终态；合并由用户在 AgentFlow 外手工完成。
- ❌ v1 不依赖 ZCode 的 headless CLI、不依赖 Bot Channel。
- ❌ v1 不引入 LangGraph / 重型编排框架；纯 Python 状态机 + append-only 事件日志。
- ❌ v1 不实现多任务、Replan、自动返修、Final Review（均 v1.1+）。
- ❌ 不实现并发/分布式；revision 仅做轻量防重复。
- ❌ **不防御拥有同一 OS 用户权限的恶意进程或主动伪造**（见 §1.2 威胁模型）。

### 0.4 成功标准（可验证）

- **S1（流程完整性，全景目标）**：给定明确目标，能跑通 `NEW → ... → READY_TO_MERGE` 全链路，无需中途人工救场（除两道门）。
- **S2（交接可靠性）**：A/B/C 三者 100% 通过结构化 JSON 交接；ZCode 不靠"读懂 Grok 说了什么"来推进。
- **S3（流程完整性校验）**：SPEC/PLAN/STATE/产物 的状态推进由 MCP 工具校验 hash/revision/worktree-clean，能发现常见的误操作、漏步骤、陈旧结果与直接修改（在非恶意模型假设下）。
- **S4（v1 最小切片）**：单任务 + 一次 Review + 两道门，在 Windows/Git Bash 下稳定跑通，FAIL/未授权变更 → `NEEDS_HUMAN`，无中途人工救场。

---

## 1. 可行性结论与技术前提核实

**结论：方案可行。** 三个承重点已核实：

| 承重点 | 核实结果 |
|---|---|
| `codex exec` 非交互调用 | ✅ 支持 `--json`（JSONL）、`--output-schema`（结构化约束）、`--sandbox read-only/workspace-write`。输出格式与 sandbox 独立解耦。ChatGPT 账号登录可用于 `exec`。 |
| `grok -p` 非交互调用 | ✅ `-p "<prompt>"` 是 headless 标准入口；支持 `--model`、`--cwd`、`--session-id`、`--output-format json` / `streaming-json`。本机 CLI 另支持 `--sandbox`/`--allow/--deny`/结构化 schema（**具体 flag 名按已安装版本在实现时核实**）。 |
| ZCode 扩展能力 | ✅ Plugin 可打包 Skill + Command + MCP + Hook + Subagent；适配 GLM-5.2 多步任务。**Plugin 管理当前为 Beta，API/目录约定可能变化，列入兼容性风险（见 §10.2）。** |

### 1.1 关键风险（决定 v1 设计的硬约束）

| 风险 | 说明 | v1 应对 |
|---|---|---|
| **R1 弱模型当总指挥** | GLM-5.2 既执行又决定"何时推进"，长任务上下文压缩后可能误判、辩解、跳步。 | MCP 工具内 hash/revision 绑定校验（流程完整性）；Hook 纵深防御。 |
| **R2 结构化 ≠ 正确** | schema 保证语法合法，不保证语义；`decision:PASS` 可能误判。 | 失败上限 → NEEDS_HUMAN；确定性测试第一道闸；review 跨字段不变式。 |
| **R3 Windows 长调用稳定性** | codex/grok 单次几十秒到数分钟，超时/挂起/stdout 截断/中文编码风险。 | "写 prompt 文件 → 调 CLI → 读结果文件 → 解析"稳态流程；超时、重试上限、原始输出落盘。 |
| **R4 三者读视图不对齐** | Codex 写的验证命令在 ZCode 里跑不通。 | `AGENTS.md` 检测+引用补充（绝不覆盖）；三者 `--cwd` 指向同一仓库根。 |

### 1.2 威胁模型（v1 的诚实边界）

> **v1 防止模型误操作、漏步骤和陈旧结果；不防御拥有同一 OS 用户权限的恶意进程或主动伪造。**

- ✅ v1 能发现：跳过 review、用陈旧 review、worktree 脏、产物被直接编辑后 hash 不匹配、重复/过期调用、未授权变更。
- ❌ v1 不防：GLM-5.2（或任何同 OS 用户进程）用 Bash **同时**改写业务代码、`state.json`、`review-result.json`、`events.jsonl` 及彼此 hash，伪造一个彼此一致的"伪 PASS"状态。同权限进程可篡改任何控制数据，CAS/revision 只能防过期调用，不能防篡改。
- **真正的不可绕过安全边界需要 control plane 处于 Executor 无写权限的位置**（独立进程 / 独立系统账户 / 受限沙箱 / 独立密钥）。这是 **v2+ 工作**，v1 仅在 §10.3 记录。
- 因此全文表述统一为：**"MCP 工具提供流程完整性校验"**，不再使用"不可绕过安全边界"。Hook 是纵深防御。

---

## 2. 系统架构（全景）

```text
┌──────────────────────────────────────────────────────────┐
│  用户                                                     │
│   ↓ 输入目标 / 回答澄清 / 批准 SPEC / 最终批准             │
└────────────────────────┬─────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────┐
│  ZCode 主会话  (GLM-5.2)  — Supervisor + Executor         │
│   · 与用户沟通、维护整体目标                               │
│   · 执行 PLAN 中的代码任务                                │
│   · 通过固定 MCP 工具调度 Planner / Reviewer             │
│   · 根据 Review 修复                                      │
└───────┬──────────────────────────────┬───────────────────┘
        │ MCP 工具调用                  │ MCP 工具调用
        ↓                              ↓
┌──────────────────────┐     ┌──────────────────────────┐
│  Codex Planner       │     │  Grok Reviewer           │
│  (GPT, codex exec)   │     │  (Grok-4.5, grok -p)     │
│   · draft_spec       │     │   · review_task          │
│   · create_plan      │     │   · final_review (v1.3+) │
│   · revise_plan      │     │   read-only sandbox      │
│   sandbox: read-only │     │                          │
│   仅输出 schema 包络  │     │                          │
└─────────┬────────────┘     └───────────┬──────────────┘
          │                              │
          ↓ 结构化 JSON 包络              ↓ 结构化 JSON
┌──────────────────────────────────────────────────────────┐
│  MCP Server (Python, 确定性工具层)                        │
│   · schema 校验 → 对规范字节重算 hash → 原子写产物        │
│   · hash/revision 绑定校验（流程完整性，非安全边界）       │
│   · operations/<id>/ 幂等 + 提交协议                      │
└────────────────────────┬─────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────┐
│  四根目录（见 §2.2）                                       │
│  plugin_root(只读) · target_repo_root(Git) ·              │
│  run_worktree_root(读写代码) · control_root(状态)         │
└──────────────────────────────────────────────────────────┘
```

### 2.1 核心设计原则

1. **主会话在 ZCode，但工作流状态不在聊天上下文里**——聊天负责"理解"，文件负责"记忆与约束"。
2. **A/B/C 不靠聊天交接**——通过版本化文件 + JSON Schema。
3. **ZCode 决策与执行，但 Codex/Grok 的调用参数由确定性工具固定**——不允许 ZCode 每次自己编 prompt。
4. **规划器只读、只输出包络**：Codex 以 `read-only` 运行，只返回符合 schema 的 JSON（内联 spec/plan markdown），**不输出被信任的 hash**；MCP 校验后对规范字节重算 hash 并原子写入。
5. **真实约束在 MCP 工具内（流程完整性），Hook 是纵深防御**：两者都不能防同权限恶意进程（见 §1.2）。
6. **没有 Review PASS 且绑定一致，任务不能标记完成**。

### 2.2 四根目录模型（v1 必须定义）

| 根 | 含义 | 路径建议 | MCP 访问 | ZCode Executor 访问 |
|---|---|---|---|---|
| `plugin_root` | 插件代码（用户级，跨项目复用） | 用户插件目录 | 只读 | 只读或不可见 |
| `target_repo_root` | 被开发的业务仓库 | 用户传入 | Git 管理操作 | 不直接修改 |
| `run_worktree_root` | 本次 run 的隔离工作区 | **repo 外**（见下） | 读写/校验业务代码 | 读写 |
| `control_root` | 状态与产物 | **repo 外**（见下） | 读写 | 最好只读 |

- **worktree 放业务仓库外部**（如 `<user_data>/agentflow/runs/<run_id>/worktree/` 或 `target_repo` 同级 `../<repo>-agentflow/<run_id>/`），避免 `.agentflow/worktrees` 污染主仓库状态。
- **control_root 也放业务仓库外部**（同上的 runs 目录内），控制数据与业务代码物理隔离。
- 三者路径在 `state.json` 中持久化**规范化绝对路径**。
- MCP 文件操作的根界定：业务代码操作限在 `run_worktree_root`；控制数据操作限在 `control_root`；插件资源读 `plugin_root`。路径逃逸检测（规范化后比较）。

### 2.3 部件职责矩阵

| 部件 | 角色 | 承担 |
|---|---|---|
| ZCode 主会话 (GLM-5.2) | Supervisor + Executor | 沟通、执行代码、调度、修复 |
| Codex (GPT) | Planner | Spec / Plan / Replan（只读，仅输出 schema 包络，不输出可信 hash） |
| Grok Build (Grok-4.5) | Reviewer | 逐任务 Review / 最终 Review（只读） |
| MCP Server (Python) | 确定性工具层 | schema 校验、重算 hash、原子写、调 Codex/Grok、流程完整性校验、状态转移 |
| Hook | 纵深防御 | 拦 Edit/Write 改产物；Stop 查未完成门。**非安全边界** |
| `state.json` + `events.jsonl` + `approvals/` | 状态机持久化 | 快照 + append-only 事件 + 批准记录 |
| Skill `agentflow` | 行为指导 | 告诉 GLM-5.2 工作方式（指导，非硬约束） |
| 用户 | 语义/不可逆决策 | 目标、重大澄清、批准 SPEC、最终批准 |

---

## 3. 状态机

### 3.1 v1 状态机（收缩版，单任务）

```text
NEW
 ↓ workflow_init
INITIALIZING_WORKTREE
 ↓ planner_draft_spec(goal[, answers])
DRAFTING_SPEC
 ├─ questions 非空 → NEEDS_USER_INPUT ──(用户回答)──┐
 │            携带 answers 重入 planner_draft_spec      │
 │                                                    │
 └─ questions 为空 → READY_FOR_SPEC_APPROVAL          │
        ↓ workflow_approve_spec                        │
        (用户批准 SPEC，可信批准记录)                   │
        ↓ ←────────────────────────────────────────────┘
 PLANNING  ← planner_create_plan(approved_spec_sha256)
 ↓
 READY_FOR_EXECUTION
 ↓ workflow_start_task
 EXECUTING_TASK
 ↓ workflow_verify_task
 VERIFYING_TASK
 ├─ 验证失败或未授权变更 ──→ NEEDS_HUMAN   (v1 不自动返修)
 └─ 验证通过（含 Git 变更策略检查）
        ↓ reviewer_review_task
 REVIEWING_TASK
 ├─ PASS    ──→ workflow_complete_task ──→ READY_TO_MERGE   ★ v1 终态
 ├─ FAIL    ──→ NEEDS_HUMAN
 └─ REPLAN  ──→ NEEDS_HUMAN             (v1.2 才接 replan)
```

**v1 终态 = `READY_TO_MERGE`**。之后用户在 AgentFlow 外手工合并（git merge / PR），v1 不维护 `MERGED`、不处理 merge conflict、不校验目标分支漂移——这些是 v2+（合并工具）工作。状态机到此为止，闭环清晰。

v1 强制：只有一个 Task、只有一次 Review；**所有状态变化只能由 MCP 工具完成**；批准/计划/执行/审核都绑定 hash 和 commit。

### 3.2 全景状态机（v1.3+ 目标，本 spec 仅标注）

加入 `FIXING_TASK`（自动返修，v1.1）、`REPLANNING`（v1.2）、`FINAL_VERIFY`/`FINAL_REVIEW`/`FINAL_REPAIR`（v1.3）、多任务循环、`MERGED`（合并工具，v2+）。

### 3.3 版本路线

| 版本 | 增量 | 成功标准 |
|---|---|---|
| **v1** | 单任务 + 一次 review + 两道门；终态 READY_TO_MERGE；FAIL/未授权变更→NEEDS_HUMAN | S4 |
| v1.1 | 自动返修循环（`FIXING_TASK`，带上限） | 单任务能自愈 |
| v1.2 | Replan（`planner_revise` + replan schema + 闭环） | 计划可演进 |
| v1.3 | 多任务 + Final Review + 全景状态机 | S1 |
| v2+ | 合并工具、独立 control plane（真正安全边界）、正式 Plugin/marketplace、Bot Channel/headless CLI 适配 | — |

### 3.4 `state.json`（快照）

```json
{
  "run_id": "20260729-001",
  "revision": 7,                          // 每次转移 +1，CAS 防过期/重复
  "state": "REVIEWING_TASK",
  "previous_state": "VERIFYING_TASK",
  "plan_version": 1,
  "current_task": "T001",
  "completed_tasks": [],
  "roots": {
    "plugin_root": "C:/Users/.../zcode-agentflow",
    "target_repo_root": "D:/some/project",
    "run_worktree_root": "D:/agentflow-runs/20260729-001/worktree",
    "control_root": "D:/agentflow-runs/20260729-001/control"
  },
  "spec_sha256": "ab12...",               // MCP 计算（非模型提供）
  "plan_sha256": "cd34...",               // MCP 计算
  "task_manifest_sha256": "ef56...",      // MCP 计算
  "base_commit": "abc123",
  "task_start_commit": "def456",
  "last_reviewed_commit": "def456",
  "review_round": 1,
  "repair_round": 0,
  "updated_at": "2026-07-29T11:30:00+08:00"
}
```

- 写入采用"临时文件 + flush + os.replace + 文件锁"。
- `revision` 用于轻量 CAS：工具入参携带 `expected_revision`，不匹配则拒绝。**v1 单线程顺序执行，不引入完整乐观锁；不防同权限篡改（见 §1.2）。**
- **所有 hash 由 MCP 对规范字节计算**（见 §4.7 canonicalization）。

### 3.5 `events.jsonl`（append-only，可恢复）

每条事件包含**可恢复状态**（指向不可变快照）：

```json
{
  "seq": 7,
  "at": "2026-07-29T11:30:00+08:00",
  "operation_id": "op_007",
  "from": "VERIFYING_TASK",
  "to": "REVIEWING_TASK",
  "reason": "reviewer_review_task",
  "revision": 7,
  "state_after_path": "operations/op_007/state_after.json",
  "state_after_sha256": "99ab..."
}
```

`state.json` 损坏时：取最后一条 `state_after_path` 指向的不可变快照，校验 `state_after_sha256`，重建。

### 3.6 `operations/<operation_id>/`（幂等 + 提交协议）

目录内容：`request.json`、`request_sha256`、`raw/stdout.txt`、`raw/stderr.txt`、`result.json`、`state_before.json`、`state_after.json`、`status`（PREPARED/RUNNING/COMMITTED/FAILED）。

**幂等校验**：
- 相同 `operation_id` + 相同 `request_sha256` + status=COMMITTED → 返回原结果（真正幂等）。
- 相同 `operation_id` + 不同 `request_sha256` → 冲突，拒绝。

**写入顺序（提交协议）**：
```text
1. 创建 operations/<id>/，写 request.json + request_sha256，status=PREPARED
2. status=RUNNING
3. 执行外部操作（调 Codex/Grok/验证命令）
4. 写 raw/* + result.json + state_after.json 并 flush
5. 计算 state_after_sha256
6. append events.jsonl 并 flush（含 state_after_path + state_after_sha256）
7. 原子替换 state.json（临时文件 + flush + os.replace）
8. status=COMMITTED
```

**崩溃恢复按 status 判定**：
- `COMMITTED` → 跳过（已完成）。
- `PREPARED`（未到 RUNNING）→ 安全重放。
- `RUNNING` 崩溃 → **不无条件重新执行**：
  - 若 `result.json` 已完整存在 → 继续提交（从步骤 5 续）。
  - 若无法判断外部操作是否完成 → 标记 status=`UNKNOWN`，再按操作类型决定是否重试：
    - **可安全重放**（如 read-only 的 Codex/Grok 调用、无副作用的测试）→ 重试。
    - **有副作用不可重放**（如验证命令可能已写过文件）→ `NEEDS_HUMAN`，由人确认实际状态。

### 3.7 失败上限（v1.1+，v1 简化为 NEEDS_HUMAN）

```yaml
limits:
  max_task_repair_rounds: 2
  max_replan_rounds: 2
  max_final_review_rounds: 2
  same_issue_limit: 1
```

---

## 4. 数据协议（A/B/C 交接契约）

所有交接走 JSON Schema 约束。Schema 存于 `plugin_root/schemas/`。

### 4.1 Spec 草稿包络 `spec-envelope.json`（Codex draft_spec 输出）

```json
{
  "run_id": "20260729-001",
  "status": "READY_FOR_SPEC_APPROVAL",     // NEEDS_USER_INPUT | READY_FOR_SPEC_APPROVAL
  "goal": "重构 DevSetPage，保持现有行为",
  "spec_markdown": "# SPEC\n...(完整正文)...",
  "questions": [],
  "assumptions": [
    { "id": "A1", "assumption": "...", "risk": "low", "default": "..." }
  ]
}
```

MCP 校验 schema → 对 `spec_markdown` 规范字节重算 `spec_sha256` → 原子写入 `control_root/SPEC.md` + `spec-envelope.json`。**Codex 不输出 hash（或输出仅诊断用，不被信任）。**

### 4.2 Plan 包络 `plan-envelope.json`（Codex create_plan 输出）

```json
{
  "run_id": "20260729-001",
  "status": "READY_FOR_EXECUTION",
  "plan_markdown": "# PLAN\n...(完整正文)...",
  "plan_version": 1,
  "base_commit": "abc123",
  "tasks": [
    {
      "id": "T001",
      "title": "建立现有行为基线测试",
      "dependencies": [],
      "declared_files": ["src/DevSetPage.cpp", "tests/DevSetPageTests.cpp"],
      "verification_commands": [
        { "program": "run-tests.bat", "args": ["DevSetPageTests"], "cwd": ".", "timeout_sec": 120 }
      ],
      "acceptance_criteria": ["现有行为测试通过", "未修改生产逻辑"],
      "planned_changes": {
        "may_add_dependencies": false,
        "may_run_migrations": false,
        "may_delete_files": ["tests/old_unused.cpp"]
      }
    }
  ]
}
```

**`create_plan` 入参必须携带已批准 SPEC 的 `approved_spec_sha256`，且等于 `state.spec_sha256`**——保证 PLAN 基于已批准 SPEC。
MCP 校验 schema → 重算 `plan_sha256` 和 `task_manifest_sha256` → 原子写入。
`verification_commands` 改为结构化（program+args+cwd+timeout），不再是任意 shell 字符串（见 §5.2）。

### 4.3 任务清单 `task-manifest.json`（MCP 派生，绑定 review 对象）

```json
{
  "plan_sha256": "cd34...",
  "current_task": "T001",
  "tasks": [
    {
      "id": "T001",
      "verification_commands": [ { "program": "...", "args": [...] } ],
      "acceptance_criteria": ["..."],
      "declared_files": ["src/DevSetPage.cpp", "tests/DevSetPageTests.cpp"],
      "planned_changes": { "may_add_dependencies": false, "may_run_migrations": false, "may_delete_files": ["..."] }
    }
  ],
  "task_manifest_sha256": "ef56..."
}
```

### 4.4 Execution 结果 `execution-result.json`（MCP 据 Git + 测试生成）

```json
{
  "run_id": "20260729-001",
  "task_id": "T001",
  "status": "READY_FOR_REVIEW",
  "base_commit": "abc123",
  "head_commit": "def456",
  "worktree_clean": true,
  "untracked_files": [],
  "verification": [
    { "command": "run-tests.bat DevSetPageTests", "exit_code": 0, "duration_ms": 1234 }
  ],
  "changed_files": ["tests/DevSetPageTests.cpp"],
  "change_audit": {
    "deleted_files": [],
    "dependency_files_changed": false,
    "migrations_added": [],
    "undeclared_files_changed": []
  }
}
```

**生成前提（硬约束）**：任务修改已提交、worktree clean、无未跟踪文件。
`change_audit` 由 Git 变更策略填充（见 §5.3）。

### 4.5 Review 结果 `review-result.json`（Grok 输出，含跨字段不变式）

```json
{
  "run_id": "20260729-001",
  "task_id": "T001",
  "base_commit": "abc123",
  "head_commit": "def456",
  "spec_sha256": "ab12...",               // MCP 注入（Grok 不自填，或仅诊断）
  "plan_sha256": "cd34...",
  "task_manifest_sha256": "ef56...",
  "plan_version": 1,
  "decision": "PASS",                      // PASS | FAIL | REPLAN | BLOCKED
  "spec_compliance": true,
  "plan_compliance": true,
  "tests_sufficient": true,
  "issues": [],
  "replan_required": false
}
```

**跨字段不变式（schema 或业务层强制校验）**：
```text
decision == PASS  ⇒ issues 为空 ∧ spec_compliance ∧ plan_compliance ∧ tests_sufficient
decision == FAIL  ⇒ issues 至少一个
decision == REPLAN ⇒ replan_required == true
```
否则视为非法 review，拒绝接受。
ZCode 只能依据 `decision` 推进，**不得主观解释为"问题不大"**。

### 4.6 批准记录 `approvals/<id>.json`（可信批准，见 §5.4）

```json
{
  "approval_id": "apr_001",
  "run_id": "20260729-001",
  "gate": "SPEC",                          // SPEC | FINAL_MERGE
  "artifact_sha256": "ab12...",
  "approved_at": "2026-07-29T11:00:00+08:00",
  "approval_method": "zcode_tool_confirmation",   // 或 "non_adversarial_convention"
  "commit_at_approval": "abc123"
}
```

### 4.7 Canonicalization（hash 规范化）

所有 hash 由 **MCP 对最终落盘字节**计算，模型不参与：
- 编码：UTF-8。
- 换行：**LF**（写入时统一转换）。
- 末尾：保留单个末尾换行。
- 算法：SHA-256，hex 小写。
- `state.json` 中的 `*_sha256` 字段只接受 MCP 计算值；模型输出中的 hash 字段（若有）仅作诊断日志，不参与校验。

### 4.8 Replan（v1.2，仅占位）

`replan-request.json`（ZCode 发起）+ `replan-result.json`（Codex 输出）。`SPEC_CHANGE_REQUIRED` → `NEEDS_HUMAN`。

---

## 5. 组件设计

### 5.1 MCP Server（Python，确定性工具层）

**v1 工具集（9 个，覆盖 v1 状态机全部状态转移，含完整结果分支）**：

| 工具 | 状态转移（完整分支） | 关键校验 |
|---|---|---|
| `workflow_status()` | — | 读 state，返回当前状态+可用动作 |
| `workflow_init(target_repo, goal)` | NEW→INITIALIZING_WORKTREE | 建 worktree（repo 外）；记四根目录；base_commit |
| `planner_draft_spec(goal, answers?)` | INITIALIZING_WORKTREE/NEEDS_USER_INPUT→DRAFTING_SPEC；questions 非空→NEEDS_USER_INPUT（携带 answers 重入）；questions 为空→READY_FOR_SPEC_APPROVAL | Codex read-only；输出 spec-envelope；MCP 重算 sha、原子写 SPEC.md |
| `workflow_approve_spec(expected_revision)` | READY_FOR_SPEC_APPROVAL→PLANNING | **用户门1**；写可信批准记录（§5.4）；锁定 spec_sha256 |
| `planner_create_plan(approved_spec_sha256, expected_revision)` | PLANNING→READY_FOR_EXECUTION | Codex read-only；approved_spec_sha256 必须匹配；MCP 重算 sha、原子写 PLAN.md + manifest |
| `workflow_start_task(task_id, expected_revision)` | READY_FOR_EXECUTION→EXECUTING_TASK | 记 task_start_commit |
| `workflow_verify_task(task_id, expected_revision)` | EXECUTING_TASK→VERIFYING_TASK；验证通过→保持 VERIFYING_TASK；**验证失败或未授权变更→NEEDS_HUMAN** | 跑验证命令（验证命令策略 §5.2）+ Git 变更策略（§5.3）；worktree clean |
| `reviewer_review_task(task_id, expected_revision)` | VERIFYING_TASK→REVIEWING_TASK；PASS→保持 REVIEWING_TASK；FAIL/REPLAN/BLOCKED→NEEDS_HUMAN | Grok read-only；输出 review；校验跨字段不变式；MCP 注入并校验 sha 绑定 |
| `workflow_complete_task(task_id, expected_revision)` | REVIEWING_TASK(PASS)→**READY_TO_MERGE（v1 终态）** | **8 项硬校验（见下）** |

**`workflow_complete_task` 的 8 项硬校验**：
1. `execution-result.status == READY_FOR_REVIEW`
2. `review.decision == PASS`
3. `review.head_commit == git HEAD`
4. **worktree clean 且无未跟踪文件**
5. `review.spec_sha256 == state.spec_sha256`
6. `review.plan_sha256 == state.plan_sha256`
7. `review.task_manifest_sha256 == state.task_manifest_sha256`
8. `expected_revision == state.revision`（CAS 防过期/重复）

任一不满足 → 抛错、不转移状态。

**所有 CLI 调用稳态流程（对应 R3）**：写 prompt 文件 → subprocess（带超时）→ 原始输出落 `operations/<id>/raw/` → schema 校验 → 解析 → operation_id 幂等。

### 5.2 验证命令策略 Runner（第一层）

PLAN 中 `verification_commands` 改为结构化，runner 强制：

- **不接受任意 shell 字符串**；schema 为 `program + args + cwd + timeout_sec`。
- **默认 `shell=False`**，直接 exec program。
- cwd 规范化后必须 ⊆ `run_worktree_root`（路径逃逸检测）。
- 可执行程序使用**仓库配置 allowlist**（`AGENTS.md` 或 config）。
- 网络策略支持 `deny / loopback / allowlist`（部分测试需 localhost）。
- stdout/stderr **密钥脱敏**（token/key/secret/password 模式）。
- 黑名单仅辅助，**不是安全边界**。

### 5.3 Git 变更策略 Runner（第二层，针对执行阶段）

危险变更（新增依赖/删除文件/migration）通常发生在 **ZCode 实现代码阶段**，不是验证命令阶段。`workflow_verify_task` 根据 `git diff task_start_commit..HEAD` 检测：

- 是否删除文件；
- 是否修改依赖清单/lockfile；
- 是否新增 migration；
- 是否修改 PLAN 未声明的文件（不在 `declared_files` 内）。

判定规则：
```text
已在批准的 SPEC/PLAN 中明确授权（planned_changes / declared_files）→ 允许
未计划的依赖 / migration / 文件删除 / 越界文件        → NEEDS_HUMAN
高风险或不可逆迁移                                     → 始终 NEEDS_HUMAN
```

**修正 v2 的过严**："任何文件删除→NEEDS_HUMAN" 改为"**未在 PLAN 中授权的**删除/依赖/migration→NEEDS_HUMAN"。重构、重命名、删除废弃测试只要 PLAN 声明了就允许。

### 5.4 人工批准的可信识别（修正"批准无法证明来自用户"）

`workflow_approve_spec` 是普通 MCP 工具，GLM 自己也能调。v1 采用**双层方案**，且降级必须显式（**不静默降级**）：

1. **首选**：利用 **ZCode 对该 MCP 工具的强制人工确认能力**（capability check 时探测是否支持；若支持，配置该工具必须人工确认）。
2. **降级（受 `allow_non_adversarial_fallback` 控制）**：若 ZCode 不支持工具级强制确认：
   - `allow_non_adversarial_fallback: false`（**默认**）→ **启动失败**，并说明原因（"ZCode 不支持工具级强制确认，且未授权降级"）。
   - `allow_non_adversarial_fallback: true` → 降级为**非对抗场景下的流程门**，依赖 GLM 遵守 Skill 指导"未获用户确认不调用"，**不防恶意模型**（与 §1.2 一致）；**同时必须向用户明确提示保障等级已降低**，批准记录写 `approval_method: non_adversarial_convention`。

无论哪种，批准记录都写入 `control_root/approvals/<id>.json`（§4.6），含 `approval_method` 字段标明实际机制。最终合并门同理（v1 终态 READY_TO_MERGE，合并动作本身在 AgentFlow 外，故 v1 只需 SPEC 门 + READY_TO_MERGE 提示用户）。

### 5.5 Skill `agentflow`（行为指导，非硬约束）

`SKILL.md` 告诉 GLM-5.2 完整工作方式（沟通、用工具调度、不自行改 SPEC/PLAN/STATE、按 decision 推进、未获用户确认不调批准工具、超限即停）。它是**指导**，真正约束在 MCP 工具 + Hook。

### 5.6 Hooks（纵深防御，非安全边界）

| Hook | 事件 | 作用 | 说明 |
|---|---|---|---|
| `protect-artifacts` | PreToolUse (Edit/Write 匹配 SPEC.md/PLAN.md/state.json/events.jsonl/reviews/*) | 拒绝 | 拦不住 Bash/Git，但挡常见误改 |
| `gate-on-stop` | Stop | 检查当前任务是否有测试/review PASS/HEAD==被审 commit | 不满足反馈 ZCode 继续；连续次数有上限 |

> **声明**：Hook 提供防呆，不提供防恶意。真正的约束来自 MCP 工具内的 hash/revision 绑定（流程完整性）；即便如此，也不防同权限进程篡改控制数据（见 §1.2）。

### 5.7 目录结构（用户级插件）

```text
<user_plugins>/zcode-agentflow/        ← plugin_root（用户级，跨项目复用）
├── .zcode-plugin/plugin.json
├── skills/agentflow/SKILL.md
├── commands/
│   ├── agentflow-start.md
│   └── agentflow-status.md
├── mcp/
│   ├── server.py                      ← MCP 工具入口
│   ├── codex_runner.py
│   ├── grok_runner.py
│   ├── verify_runner.py               ← 验证命令策略
│   ├── change_audit.py                ← Git 变更策略
│   ├── state.py                       ← state.json + events.jsonl + operations + approvals
│   ├── hashes.py                      ← 规范字节重算 (§4.7)
│   ├── schemas.py                     ← schema + 跨字段不变式校验
│   └── requirements.txt
├── hooks/
│   ├── hooks.json
│   ├── protect_artifacts.py
│   └── gate_on_stop.py
├── schemas/
│   ├── spec-envelope.schema.json
│   ├── plan-envelope.schema.json
│   ├── execution-result.schema.json
│   ├── review-result.schema.json
│   └── replan-result.schema.json      (v1.2)
├── prompts/
│   ├── planner-draft.md
│   ├── planner-plan.md
│   ├── reviewer.md
│   └── repair.md                      (v1.1+)
├── docs/superpowers/specs/
│   └── 2026-07-29-zcode-agentflow-design.md   ← 本文档
└── AGENTS.md.template                 ← 检测+引用补充，绝不覆盖
```

> **运行时产物**在 `control_root`（repo 外）：`SPEC.md`、`PLAN.md`、`state.json`、`events.jsonl`、`operations/`、`approvals/`、`reviews/`、`plan-history/`。
> worktree 在 repo 外的 `run_worktree_root`。

---

## 6. 配置（`config.yaml`，位于 plugin_root）

```yaml
version: 1

auth:                          # 账号登录（非 API token）
  codex: chatgpt_account
  grok: xai_account

workflow:
  worktree: true               # worktree 放 repo 外
  branch_prefix: agentflow
  max_task_repair_rounds: 2    # v1.1+
  max_replan_rounds: 2         # v1.2+

human_gates:
  spec_approval:
    required: true
    method: prefer_zcode_tool_confirmation   # §5.4 双层方案
    allow_non_adversarial_fallback: false    # false=不支持强制确认时启动失败；true=降级为非对抗约定并提示降级
  final_merge:
    required: true
    v1_terminal_state: READY_TO_MERGE        # v1 不做合并动作

planner:
  type: codex_cli
  command: codex
  mode: exec
  model: gpt-5.6
  model_reasoning_effort: high # 用户选择 high（非 xhigh）
  sandbox: read-only
  output_schema: schemas/spec-envelope.schema.json
  timeout_sec: 600

reviewer:
  type: grok_build_cli
  command: grok
  model: grok-4.5
  output_format: json
  sandbox: read-only           # 必须落成真实 flag（实现时按已安装版本核实）
  timeout_sec: 300

verification:                  # 第一层：验证命令策略
  command_policy:
    shell: false
    program_allowlist_from_agents_md: true
    cwd_must_be_within: run_worktree_root
    network: deny              # deny | loopback | allowlist
    secret_redaction: true

change_policy:                 # 第二层：Git 变更策略
  on_unauthorized_delete: NEEDS_HUMAN
  on_unauthorized_dependency: NEEDS_HUMAN
  on_unauthorized_migration: NEEDS_HUMAN
  on_undeclared_file: NEEDS_HUMAN
  irreversible_migration: always_need_human

review:
  require:
    - spec_compliance
    - plan_compliance
    - tests_passed
    - no_high_severity_issues
  cross_field_invariants: true
```

---

## 7. 人工门与可自动化项

### 7.1 v1 保留的人工门（两道）

| 门 | 触发状态 | 用户动作 | 批准机制 |
|---|---|---|---|
| SPEC 批准 | `READY_FOR_SPEC_APPROVAL` | 批准 SPEC.md → 锁定 spec_sha256 → PLANNING | ZCode 强制确认（首选）/ 非对抗流程门（降级） |
| 最终批准 | `READY_TO_MERGE` | 用户在 AgentFlow 外手工合并 | v1 终态提示，不做合并动作 |

`NEEDS_USER_INPUT`、`NEEDS_HUMAN`（验证失败/review FAIL/replan/未授权变更/超限）也请求人，属异常路径。

### 7.2 全自动项（v1）

读仓库、生成 Spec 草稿、生成 Plan、建 worktree、按 Plan 改代码（ZCode 执行）、跑验证、生成执行报告、调 Grok Review、据 review decision 推进状态。

---

## 8. v1 最小可构建子集（垂直切片）

### 8.1 v1 实现范围

| 组件 | v1 范围 | 延后 |
|---|---|---|
| MCP 工具 | **9 个**（§5.1，终态 READY_TO_MERGE） | `planner_revise`（v1.2）、自动返修（v1.1）、`reviewer_final_review`（v1.3）、合并工具（v2+） |
| Skill | `agentflow` SKILL.md | — |
| Hooks | `protect-artifacts` + `gate-on-stop` | — |
| 协议 | spec/plan/execution/review 四 schema + 跨字段不变式 + manifest + approvals | replan schema（v1.2） |
| 状态机 | v1 收缩版（§3.1，单任务，终态 READY_TO_MERGE） | FIXING/REPLANNING/FINAL/MERGED |
| Runner | codex/grok/verify（命令策略）/change_audit（Git 策略） | — |
| 持久化 | state.json + events.jsonl + operations 幂等 + approvals + 文件锁 | — |
| Roots | 四根目录 | — |

### 8.2 v1 验收 demo（S4）

```
$ /agentflow-start --repo <path> 重构 DevSetPage，保持行为不变
→ workflow_init 建 worktree（repo 外），记四根目录
→ planner_draft_spec → 生成 SPEC 草稿（MCP 重算 sha）
→ (若 NEEDS_USER_INPUT) 用户回答 ≤3 问
→ READY_FOR_SPEC_APPROVAL → 用户批准 SPEC（门1，可信批准记录），锁定 spec_sha256
→ planner_create_plan(approved_spec_sha256) → 生成 PLAN（基于已批准 SPEC，MCP 重算 sha）
→ workflow_start_task(T001)
→ ZCode 执行 T001 + 提交
→ workflow_verify_task(T001) → 跑验证（命令策略）+ Git 变更策略 → worktree clean
→ reviewer_review_task(T001) → Grok 返回 PASS（跨字段不变式通过）
→ workflow_complete_task(T001) → 8 项硬校验全过 → READY_TO_MERGE（v1 终态）
→ 用户在 AgentFlow 外手工合并
```

**v1 不验收**：replan、自动返修、final review、多任务、合并动作。FAIL/未授权变更/REPLAN 一律 NEEDS_HUMAN。

### 8.3 v1 之后（仅标注）

v1.1 自动返修；v1.2 replan；v1.3 多任务 + final review + 全景状态机；v2+ 合并工具 + 独立 control plane（真正安全边界）+ 正式 Plugin/marketplace + Bot Channel/headless CLI 适配。

---

## 9. 错误处理与可观测性

- **CLI 调用失败**：超时/非零退出/schema 校验失败 → MCP 返回结构化 error，不污染 state；operation 原始输出可诊断；ZCode 决定重试或 NEEDS_HUMAN。
- **确定性测试失败**：不进入 REVIEW，NEEDS_HUMAN（v1 不自动返修）。
- **Git 变更策略触发**：未授权删除/依赖/migration/越界 → NEEDS_HUMAN；不可逆迁移 → 始终 NEEDS_HUMAN。
- **review 跨字段不变式违反**：非法 review，拒绝，NEEDS_HUMAN。
- **CAS 失败**：`expected_revision` 不匹配 → 拒绝，提示当前 revision。
- **崩溃恢复**：`state.json` 损坏 → 从 `events.jsonl` 最后一条 `state_after_path` 重放；operation 按 status（PREPARED/RUNNING 重放，COMMITTED 跳过）；幂等校验 request_sha256。
- **可观测**：`/agentflow-status` 随时查进度；`events.jsonl` 全审计；`raw/` 全原始输出；`approvals/` 批准留痕。

---

## 10. 风险与开放项

### 10.1 已识别风险（持续跟踪）

| 风险 | 等级 | 缓解 |
|---|---|---|
| R1 弱模型当总指挥 | 高 | hash/revision 绑定（流程完整性）+ Hook 纵深防御；不防恶意 |
| R2 结构化≠正确 | 中 | 跨字段不变式 + 失败上限 |
| R3 Windows 长调用 | 中 | 稳态流程 + 超时 + 原始落盘 + 幂等 |
| R4 读视图不对齐 | 中 | AGENTS.md 检测+补充（绝不覆盖）+ `--cwd` 统一 |
| **R5 同权限篡改** | 高（已知不防） | 威胁模型显式排除；真正边界留 v2+ 独立 control plane |

### 10.2 兼容性风险

- **Plugin 管理 Beta**：ZCode Plugin API/目录约定可能变化。v1 启动 **capability check**（探测插件加载、MCP 注册、Hook 事件、**工具级强制人工确认**是否可用），失败降级提示。
- **Grok/Codex 具体 flag**：`--sandbox`/`--allow/--deny`/结构化 schema 真实 flag 名按已安装 CLI 版本实现期核实。

### 10.3 v2+ 候选（本 spec 仅记录）

- **独立 control plane**：将 state/operations/approvals 置于 Executor 无写权限的位置（独立进程/账户/密钥），实现真正不可绕过安全边界。
- **合并工具**：处理 `READY_TO_MERGE→MERGED`，含目标分支漂移、conflict、fast-forward/rebase。
- 正式 Plugin/marketplace、Bot Channel、headless CLI 适配。

### 10.4 待确认问题（实现期落实，不阻塞 spec）

1. 用户级插件具体安装路径（capability check 时确认）。
2. Codex `model_reasoning_effort` 对当前账号/模型可用值（已定 high，不可用回退 medium）。
3. target_repo 是否已纳入 git（worktree 依赖）。
4. ZCode 是否支持 MCP 工具级强制人工确认（决定 §5.4 走首选还是降级）。

---

## 11. 下一步

本 spec 经用户 review 通过后，进入 **writing-plans**，围绕 §8 的 v1 垂直切片（9 个 MCP 工具 + 2 Hook + 4 schema + 收缩状态机 + 四根目录 + 两层策略 runner + 可恢复持久化）产出分步实现计划。
