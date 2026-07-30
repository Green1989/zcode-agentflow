## 复审结论

这份 v2 Plan 相比上一版有了**实质性提升**。上一轮指出的 8 个 P0 问题基本都已经在设计层面得到回应：

* 本地包改为 `src/zcode_agentflow`，消除了 `mcp` SDK 包名冲突；
* 增加真实 FastMCP stdio 协议测试；
* Codex 明确区分 stdin、仓库 cwd、operation 输出目录；
* Grok 改为读取完整 Review Bundle；
* Store 收拢为单一事务入口；
* 引入 per-Run 文件锁和故障注入；
* Schema 和业务层双重限制单 Task；
* E2E 真实修改并提交代码；
* 删除自动 push/merge。

这些修订不是“文字上回应”，大部分已经落实到测试、依赖关系和完成条件中。

不过，我重新逐项检查后，仍发现 **6 个执行前必须修复的问题**。它们不需要再重写整份 Plan，但建议生成一个 **v2.1 小修订版**后再交给 Agent 执行。

当前建议状态：

```text
CHANGES_REQUIRED
```

不是架构方向错误，而是还存在少量会让执行 Agent 卡住、产生 Windows 故障或实现出事务死锁的问题。

---

# 一、必须修复的问题

## P0-1：`operation_id` 仍然不能在 Windows 上作为目录名使用

Task 2.3 给出的 operation ID 合约是：

```text
<run_id>:<tool>:r<revision>:a<attempt>
```

例如：

```text
20260730095000-ab12cd:draft_spec:r4:a2
```

但后续 operation 目录设计为：

```text
operations/<operation_id>/
```

Windows 文件名中不能包含冒号 `:`。这与文档声明的“Windows/Git Bash primary”直接冲突。

### 建议修改

改成：

```text
<run_id>__<tool>__r<revision>__a<attempt>
```

例如：

```text
20260730095000-ab12cd__draft-spec__r4__a2
```

补充测试：

```python
def test_operation_id_is_windows_filename_safe():
    op_id = operation_id(...)
    assert not any(c in op_id for c in '<>:"/\\|?*')
    assert ".." not in op_id
```

同时给 operation ID 明确正则：

```text
^[a-zA-Z0-9._-]+$
```

这是一个明确的阻塞问题。

---

## P0-2：三个核心文件虽然列在目录结构中，但没有对应实现任务

文档的 File ownership 中列出了：

```text
src/zcode_agentflow/gitops.py
prompts/planner-draft.md
prompts/planner-plan.md
prompts/reviewer.md
config.yaml
```

但逐个检查任务后：

* 没有独立任务负责实现完整的 `gitops.py`；
* 没有任务负责创建和测试三个 Prompt 模板；
* 没有任务真正创建生产用 `config.yaml`。

其中 `gitops.py` 后续被以下环节依赖：

* worktree 创建；
* HEAD、clean、untracked 检查；
* diff；
* `--name-status -z`；
* branch 检查；
* Review Bundle。

但当前只有 Task 6.2 顺带提到：

> define `gitops.name_status_z` here or in Phase 3

这对执行 Agent 来说仍然是模糊任务。

### 建议新增三个任务

#### Task 2.6：Git primitives

实现并测试：

```text
is_git_repo
head
is_clean
untracked_files
create_worktree
worktree_list
branch_exists
diff_patch
changed_files_z
name_status_z
git_path
```

必须用真实临时 Git 仓库测试，包括：

* 文件名有空格和中文；
* rename；
* delete；
* worktree 在仓库外；
* branch collision。

#### Task 4.0：Prompt templates

创建：

```text
prompts/planner-draft.md
prompts/planner-plan.md
prompts/reviewer.md
```

测试至少验证：

* 所需占位符都存在；
* 不允许遗留未替换的 `{placeholder}`；
* Planner Draft 明确只输出 Spec；
* Planner Plan 明确只允许一个 Task；
* Reviewer 明确引用 Bundle 中的文件；
* Prompt 包含 resolved Agent context 的引用。

#### Task 1.2 或新增 Task 1.3：生产配置文件

创建并验证：

```text
config.yaml
```

同时增加配置 Schema 或严格的配置验证，不能只做字典深度合并。

---

## P0-3：Engine `_guard` 和 Store 事务都加锁，存在自死锁风险

Task 5.1 规定：

```text
_guard acquires the run lock
```

Task 3.4 又规定：

```text
Store.commit_operation with run_lock(control_root)
```

典型调用会变成：

```text
Engine method
  → _guard() 获取 .agentflow.lock
  → Store.commit_operation()
       → 再次获取同一个 .agentflow.lock
```

`portalocker` 的跨平台文件锁不能被当作可重入锁使用。在 Windows 下尤其可能直接阻塞到超时。

此外，Task 3.4 还计划在持有文件锁期间执行 Codex/Grok，调用可能持续数分钟。这会让：

* `workflow_status`；
* Stop Hook；
* 重试调用；
* 故障诊断

长时间无法读取运行状态。

### 推荐方案

将一次外部调用拆成两个短事务：

```text
事务 A：begin_operation
  获取锁
  校验 state + revision
  写 PREPARED/RUNNING
  状态切到 DRAFTING_SPEC / VERIFYING_TASK / REVIEWING_TASK
  写 active_operation_id
  释放锁

外部阶段：
  调 Codex / Grok / verification
  写 operation staging
  不持有 Run 锁

事务 B：finish_operation
  获取锁
  校验 active_operation_id + revision
  校验结果和产物
  写 event/state/artifacts
  标记 COMMITTED
  清除 active_operation_id
  释放锁
```

这样也能正确体现 Specs 中的中间状态：

```text
DRAFTING_SPEC
VERIFYING_TASK
REVIEWING_TASK
```

原 Specs 明确定义了这些状态，而当前 Plan 倾向于在外部调用完成后一次性从起始状态跳到结果状态。

至少必须明确：

> `_guard()` 不单独获取文件锁；所有 guard、CAS 和状态提交由 Store 的同一个事务完成。

不能保留两层锁。

---

## P0-4：Active Run 恢复策略可能自动恢复到错误项目

Task 1.3 的策略是：

```text
扫描 runs/*/control/state.json
选择最新的非终态 Run
损坏的 state.json 直接跳过
```

这有两个风险。

### 风险一：多个非终态 Run

假设存在：

```text
Run A：项目甲，EXECUTING_TASK
Run B：项目乙，NEEDS_HUMAN
```

MCP 重启后按“最新”选择其中一个，可能把当前 ZCode会话绑定到错误项目。

### 风险二：最新 Run 的 state 损坏

当前策略会直接跳过损坏 Run，自动选择另一个旧 Run。

这与后面已经设计的：

```text
events.jsonl recovery
state_after snapshot
Store.recover()
```

相矛盾。损坏 state 应该先恢复，而不是静默跳过。

### 建议方案：持久化 active-run 指针

在：

```text
<user_data_root>/active-run.json
```

记录：

```json
{
  "run_id": "...",
  "control_root": "...",
  "target_repo_root": "...",
  "updated_at": "..."
}
```

规则：

1. `workflow_init` 创建新 Run 时原子更新 pointer；
2. 已存在非终态 active Run 时，拒绝创建另一个，除非显式 abandon；
3. 重启后只恢复 pointer 指定的 Run；
4. state 损坏时调用 `Store.recover()`；
5. 无法恢复则进入 `NEEDS_HUMAN`，绝不自动切换到其他 Run；
6. 到达 `READY_TO_MERGE` 后清除或归档 pointer。

可以保留扫描作为“人工恢复工具”，但不能作为默认选择逻辑。

---

## P0-5：Plan Schema 与“多 Task 时返回 BLOCKED”相互矛盾

Task 2.1 要求：

```json
"tasks": {
  "minItems": 1,
  "maxItems": 1
}
```

这是正确的 v1 单 Task 约束。

但 Task 5.3 又要求：

```text
Fake Codex 返回两个 tasks
→ Engine 路由 NEEDS_USER_INPUT / BLOCKED
```

问题是两个 Task 会先在 JSON Schema 校验阶段失败，Engine根本拿不到一个合法的 Plan Envelope，无法根据内容返回 `BLOCKED`。

同时当前 Plan Envelope 只描述：

```text
status = READY_FOR_EXECUTION
tasks = exactly one
```

没有定义：

```text
NEEDS_USER_INPUT
BLOCKED
questions
block_reason
```

### 推荐方案

新增一个 Planner Result Wrapper，而不修改最终 Plan Artifact：

```json
{
  "status": "READY_FOR_EXECUTION",
  "plan": {
    "...": "严格的单 Task plan-envelope"
  },
  "questions": [],
  "block_reason": null
}
```

允许三种结果：

```text
READY_FOR_EXECUTION
NEEDS_USER_INPUT
BLOCKED
```

条件约束：

```text
READY_FOR_EXECUTION：
  plan 存在
  plan.tasks 恰好一个
  questions 为空

NEEDS_USER_INPUT：
  plan 为 null
  questions 至少一个

BLOCKED：
  plan 为 null
  block_reason 非空
```

最终落盘的 `PLAN.md` 和 `plan-envelope.json` 仍然只接受 READY 状态，因此不需要改动已批准 Specs 的核心交接协议。

如果不增加 Wrapper，则应把 Task 5.3 改成：

```text
多 Task / schema invalid → NEEDS_HUMAN
```

不能同时声称“严格 maxItems=1”和“业务层接收两个任务后处理”。

---

## P0-6：任务顺序不是拓扑顺序，不适合直接交给 executing-plans

当前文档顺序是：

```text
Phase 5 Engine
Phase 6 Approval / Change Policy / Hooks
```

但：

```text
Task 5.3 depends on Task 6.1
Task 5.4 depends on Task 6.2
```

文档虽然增加了说明和依赖图，但开头又明确要求 Agent 使用：

```text
superpowers:subagent-driven-development
或
superpowers:executing-plans
```

这类执行方式通常按文档顺序逐项推进。执行到 5.3 时才发现依赖 6.1 尚未完成，很容易导致：

* Agent 临时跳转；
* commit 顺序混乱；
* 重复实现 approval/change policy；
* 计划状态勾选错乱。

### 建议重新排序

推荐：

```text
Phase 0  Capability Spike
Phase 1  Project + MCP
Phase 2  Schemas / IDs / Canonical / Git primitives
Phase 3  Store
Phase 4  Adapters
Phase 5  Approval + Change Policy + Agent Context
Phase 6  Engine
Phase 7  Hooks + Packaging
Phase 8  E2E
```

其中 Hook 确实依赖 Engine 的 `check_stop`，所以 Hook 放 Engine 后面；但 Approval 和 Change Policy 必须移到 Engine 前面。

> Dependency graph 可以作为辅助，但执行计划正文必须尽量采用拓扑顺序。

---

# 二、建议在执行前一并补强的问题

以下问题不一定让程序立即不能运行，但容易在实现中留下隐患。

## P1-1：工具确认能力“支持”不等于批准工具已经被配置为强制确认

Task 6.1 的逻辑大致是：

```text
tool_confirmation_supported=true
→ approval_method=zcode_tool_confirmation
```

但仅仅知道 ZCode支持这个功能，不能证明：

```text
workflow_approve_spec
```

这个具体工具已经配置为强制用户确认。

还必须有一项任务负责：

* 在真实 plugin manifest / MCP tool metadata 中启用确认；
* 用 Phase 0 发现的真实字段配置；
* 测试 `workflow_approve_spec` 调用会弹出用户确认；
* 普通工具不弹；
* 只有确认后的调用才写 `zcode_tool_confirmation`。

否则 approval record 可能错误声称已经获得强确认。

另外，Specs 要求当“不支持强确认且 fallback=false”时启动失败，而当前 Plan 更倾向于到审批调用时才失败。

建议增加：

```text
validate_capabilities(config)
```

在 MCP Server 初始化或 `workflow_init` 前执行。

---

## P1-2：Capability gate 对 `false` 的处理过于绝对

Task 0.5 写道：

> 任何 capability 为 false 或 unknown 都阻塞下游任务。

但有些 `false` 本来就有合法降级：

```text
tool_confirmation_supported=false
→ allow_non_adversarial_fallback=true 时允许运行

reasoning_effort_high_supported=false
→ Specs 允许回退 medium
```

所以应区分：

```text
required capability
optional capability
fallback-capable capability
```

例如：

```json
{
  "gates": {
    "planner_ok": true,
    "reviewer_ok": true,
    "approval_ok": true,
    "approval_mode": "fallback",
    "reasoning_effort_effective": "medium"
  }
}
```

`unknown` 通常阻塞；`false` 不一定阻塞。

---

## P1-3：缺少模型输出的上下文不变式

严格 Schema 只能保证字段类型，不保证字段与当前 Run 一致。

Engine还应强制：

```text
spec.run_id == state.run_id
spec.goal == state.goal
status == NEEDS_USER_INPUT ⇔ questions 非空
status == READY_FOR_SPEC_APPROVAL ⇒ questions 为空

plan.run_id == state.run_id
plan.base_commit == state.base_commit
plan.plan_version == 1
单 Task 的 dependencies 必须为空
task.id == T001
declared_files 必须为相对路径且不能包含 ..
verification.cwd 必须为相对路径
verification.program 必须能通过当前 allowlist
```

尤其 `base_commit` 不能信任 Codex提供，应由 Engine注入或严格比对。

Review字段已经明确由 Engine注入，Spec和Plan也应采用同样原则。

---

## P1-4：多文件 artifact promotion 不是原子的

Task 3.4 把：

```text
promote staged artifacts
```

视为一个步骤，但一次 Plan 操作可能同时产生：

```text
PLAN.md
plan-envelope.json
task-manifest.json
```

多个 `os.replace()` 之间崩溃，会出现部分文件已更新、部分未更新。

当前“12个步骤故障注入”如果只在整个 promotion 步骤后注入，捕捉不到这个问题。

### 方案

两种选择：

1. 每个 operation 使用不可变 generation 目录：

```text
artifacts/<operation_id>/
```

State只原子切换一个 generation pointer；

2. 为每个待推广文件写 promotion manifest：

```json
{
  "artifacts": [
    {"source": "...", "target": "...", "promoted": true},
    {"source": "...", "target": "...", "promoted": false}
  ]
}
```

恢复时逐项幂等完成。

故障测试应在“每一个 artifact promotion 后”注入，而不只是 step 9 后。

---

## P1-5：Windows/Git Bash 下 `.bat` 和 `.cmd` 可能无法直接 `shell=False` 执行

新 Plan 的 Verification Adapter 只测试：

```text
python -V
```

但原 Specs 的示例包含：

```text
run-tests.bat
```

在 Windows 上，`.bat/.cmd` 通常不能像 `.exe` 一样直接通过 `shell=False` 运行。

建议明确支持三种结构化类型：

```json
{
  "kind": "executable",
  "program": "python",
  "args": ["-m", "pytest"]
}
```

```json
{
  "kind": "cmd_script",
  "program": "scripts/run-tests.bat",
  "args": ["DevSetPageTests"]
}
```

```json
{
  "kind": "bash_script",
  "program": "scripts/test.sh",
  "args": []
}
```

内部仍然保持外层 `shell=False`：

```text
cmd_script  → cmd.exe /d /s /c <安全构造参数>
bash_script → bash <script> <args>
```

脚本路径必须位于 worktree 内，解释器必须在 allowlist 中。

如果 v1 不支持脚本，也必须明确拒绝，而不能宣称 Windows/Git Bash 通用。

---

## P1-6：MCP stdio 的 stdout 必须绝对干净

FastMCP Server 的 stdout 是协议通道。任何：

```text
print()
logging.StreamHandler(sys.stdout)
子进程输出直通
traceback
```

都可能破坏 MCP framing。

建议增加测试：

```text
test_server_stdout_contains_only_mcp_frames
```

并规定：

```text
所有日志写 stderr
所有 Codex/Grok stdout 必须 capture
不允许 adapter 继承父进程 stdout
```

MCP测试最好优先使用官方 SDK client，而不是在 Plan 中硬编码“newline-delimited JSON-RPC”假设；实际 framing 应以 Phase 0 和当前 SDK 为准。

---

## P1-7：依赖版本应在 Capability Spike 后冻结

当前 `pyproject.toml` 使用：

```text
mcp>=1.2.0
portalocker>=2.7
```

Phase 0 验证的是当时安装的某个版本，但 Task 1.1 执行 `pip install -e .` 时，可能升级到另一个版本，导致之前验证过的：

* FastMCP import；
* stdio；
* Tool metadata；
* portalocker 行为

发生变化。

建议在 Phase 0 记录：

```json
{
  "versions": {
    "python": "...",
    "mcp": "...",
    "portalocker": "...",
    "codex_cli": "...",
    "grok_cli": "...",
    "zcode": "..."
  }
}
```

然后生成 lock 文件，至少对关键依赖使用兼容范围或精确版本。

---

# 三、一个需要澄清的小矛盾

Task 3.5 把 Codex/Grok 调用列为可能有副作用、不能重放的操作例子，但 Specs 将只读 Codex/Grok 调用视为可安全重放类型。

建议不要只用：

```text
has_side_effects: true / false
```

改成：

```text
replay_policy:
  SAFE_READ          # 可自动重放
  COSTFUL_READ       # 技术上安全，但会重复耗费额度；按配置决定
  UNSAFE_WRITE       # 不自动重放，NEEDS_HUMAN
```

推荐分类：

| 操作                       | replay_policy           |
| ------------------------ | ----------------------- |
| Codex read-only planning | `COSTFUL_READ`          |
| Grok read-only review    | `COSTFUL_READ`          |
| Git/status/hash          | `SAFE_READ`             |
| verification command     | 默认 `UNSAFE_WRITE`       |
| worktree 创建              | `UNSAFE_WRITE`，但有专门恢复逻辑 |

这样恢复策略更准确。

---

# 四、整体评价

## 上一轮问题关闭情况

| 类别                | 复审结果                  |
| ----------------- | --------------------- |
| 包名冲突              | 已关闭                   |
| 假 MCP Server      | 已关闭                   |
| Codex空 stdin      | 已关闭                   |
| Codex cwd 错误      | 已关闭                   |
| Grok缺少 Spec/Plan  | 已关闭                   |
| Store提交顺序         | 方向已关闭，需补多文件 promotion |
| 无文件锁              | 已关闭，但要解决嵌套锁           |
| 单 Task 未强制        | 已关闭，但 BLOCKED 协议需统一   |
| draft重入幂等冲突       | 已关闭                   |
| 静态 repo 环境变量      | 已关闭                   |
| E2E不真实提交          | 已关闭                   |
| Artifact只比较旧 hash | 已关闭                   |
| Canonical newline | 已关闭                   |
| Event seq重启归零     | 已关闭                   |
| 假 network deny    | 已关闭                   |
| AGENTS.md覆盖风险     | 已关闭                   |
| 审批静默降级            | 基本关闭                  |
| Stop Hook重复弱逻辑    | 已关闭                   |
| 自动 push main      | 已关闭                   |

## 最终判断

这份 v2 已经从上一版的：

```text
设计合理，但不可安全执行
```

提升为：

```text
主体架构成熟，接近可执行
```

我不建议再次大改架构。只需做一次聚焦的 v2.1 修订，优先解决：

```text
1. Windows-safe operation_id
2. 补齐 gitops / prompts / config 三个遗漏任务
3. 消除 Engine/Store 嵌套锁
4. 用 active-run pointer 替代“扫描最新 Run”
5. 统一单 Task 与 BLOCKED 的协议
6. 将任务顺序改成真正的依赖拓扑顺序
```

这 6 项完成后，我会把 Plan 状态评为：

```text
APPROVED_FOR_EXECUTION
```

目前不建议直接让 Agent从 Phase 0 一路无人值守执行；否则最可能首先撞到的是 operation ID 的 Windows目录问题、缺少 gitops/Prompt任务，以及 Phase 5→Phase 6 的逆向依赖。