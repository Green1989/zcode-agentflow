## 审核结论

这份 Plan **结构清晰、任务拆分细、TDD 意识也比较强**，但目前还不适合直接交给 Agent 按计划执行。

原因不是 Specs 有问题，而是 Plan 里的部分“示例实现代码”已经引入了若干架构级缺陷。其中有几项会导致：

* 插件根本无法作为 MCP Server 启动；
* Codex 收不到 Prompt 或看不到目标仓库；
* Grok 看不到 SPEC/PLAN；
* 恢复机制几乎必然校验失败；
* E2E 测试自身无法跑通；
* 表面到达 `READY_TO_MERGE`，实际却可能只执行了多个任务中的第一个。

Specs 对用户级部署、运行时传入 `target_repo`、单 Task、恢复协议、hash 校验、capability check 都定义得比较明确。
问题主要集中在 Implementation Plan 对这些设计的落地偏差。

我的建议是：

> **不要直接执行当前 Plan。先做一次 Plan v2 修订，解决下面的阻塞问题。**

---

# 一、必须先修改的阻塞级问题

## P0-1：本地包名 `mcp` 与官方 `mcp` SDK 冲突

Plan 同时做了两件事：

```text
requirements.txt:
mcp>=1.2.0
```

同时又创建：

```text
mcp/
├── __init__.py
├── server.py
├── engine.py
...
```

这会造成 Python 包名遮蔽。

当项目根目录位于 `sys.path` 前面时：

```python
import mcp
```

加载的是你自己的 `mcp/__init__.py`，而不是安装的 MCP SDK。后面想写：

```python
from mcp.server.fastmcp import FastMCP
```

很可能会因为本地 `mcp` 包覆盖官方包而失败。

而且当前测试专门把项目根目录插到 `sys.path` 最前面，进一步强化了这个冲突。

### 修改方案

把本地代码包改名为：

```text
zcode_agentflow/
```

或者：

```text
agentflow_mcp/
```

推荐目录：

```text
src/
└── zcode_agentflow/
    ├── engine.py
    ├── server.py
    ├── persistence.py
    └── ...
```

测试改成：

```python
from zcode_agentflow import engine
```

官方 SDK 保持：

```python
from mcp.server.fastmcp import FastMCP
```

同时建议增加 `pyproject.toml`，使用标准 `src` layout，而不是依靠测试中手动修改 `sys.path`。

---

## P0-2：当前 `server.py` 并不是一个真正可加载的 MCP Server

Plan 的 `server.py` 只实现了：

```python
tool_names()
dispatch()
set_engine_factory()
```

但没有：

* 创建 `FastMCP` 实例；
* 注册 9 个 MCP tools；
* 定义工具参数 schema；
* 启动 stdio transport；
* `main()`；
* `if __name__ == "__main__"`；
* 错误映射和 JSON 序列化；
* MCP 初始化与生命周期。

Task 5.3 的注释说 stdio entrypoint 会在 Phase 6 接上，但 Task 6.1 实际上只实现了 Engine factory，仍然没有启动 MCP stdio transport。

因此，即使全部代码都按计划完成：

```text
.zcode-plugin/plugin.json
→ mcp/server.py
```

ZCode 启动这个文件时，它大概率什么都不会做，随后进程退出。

### 修改方案

增加真实入口，例如：

```python
from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP("zcode-agentflow")


@mcp_server.tool()
def workflow_status() -> dict:
    return get_engine().status()


@mcp_server.tool()
def workflow_init(target_repo: str, goal: str, run_id: str | None = None) -> dict:
    return get_engine().workflow_init(
        target_repo=target_repo,
        goal=goal,
        run_id=run_id,
    )


def main() -> None:
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
```

并增加真正的协议测试，而不只是测试 `dispatch()`：

```text
启动 MCP 子进程
→ initialize
→ tools/list
→ tools/call workflow_status
→ 关闭进程
```

这应该提前到 Phase 0 或 Phase 1 做最小 spike，不能拖到最后才验证。

---

## P0-3：Codex Runner 实际没有把 Prompt 发送给 Codex

Plan 的 Runner 接收了：

```python
run(prompt="do it", ...)
```

但 `_invoke()` 中写的是：

```python
subprocess.run(
    cmd,
    input="",
    ...
)
```

也就是说，虽然命令末尾使用 `-` 表示从 stdin 读 Prompt，但真正送入 stdin 的是空字符串。

结果是：

> Codex不会收到 planner prompt。

现有测试没有检查 Prompt 是否真的进入 `_invoke()`，所以这个错误不会被测试发现。

### 修改方案

将接口改为：

```python
def _invoke(cmd, *, cwd, timeout, stdin_text):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
```

调用：

```python
_invoke(
    cmd,
    cwd=repo_cwd,
    timeout=timeout,
    stdin_text=prompt,
)
```

测试必须明确验证：

```python
assert captured["stdin_text"] == "do it"
```

---

## P0-4：Codex 的工作目录指向了 `control_root`，无法读取目标仓库

Specs 明确要求三者读取同一个仓库视图，四根目录中：

* `run_worktree_root` 是业务代码工作区；
* `control_root` 只存放状态和产物。

但 Engine 调用 Codex 时使用：

```python
workdir=self.control_root
```

与此同时 Planner Prompt 又要求：

```text
Read the repository at the working directory.
```

这两者矛盾。

Codex进入的是控制数据目录，而不是代码仓库，因此无法分析项目。

### 修改方案

Runner 应把“CLI 工作目录”和“输出目录”分开：

```python
runner_codex.run(
    prompt=prompt,
    cwd=run_worktree_root,
    output_dir=operation_dir,
    ...
)
```

例如：

```text
cwd:
  <run_worktree_root>

output:
  <control_root>/operations/<operation_id>/result.json

raw:
  <control_root>/operations/<operation_id>/raw/
```

不能用一个 `workdir` 参数同时表达这两个概念。

---

## P0-5：Grok只能看代码 Diff，看不到 SPEC、PLAN 和 Task Manifest

当前 Reviewer Prompt 只传了：

```text
Task
Base commit
Head commit
SPEC sha256
PLAN sha256
```

然后要求 Grok：

```text
Inspect the diff against the SPEC and PLAN acceptance criteria.
```

但 Grok 的 `cwd` 是 `run_worktree_root`，而 SPEC 和 PLAN 位于 repo 外的 `control_root`。

所以 Grok拥有的实际上只有：

* 代码；
* Git diff；
* 几个 hash。

它没有：

* SPEC 内容；
* PLAN 内容；
* 当前 Task 的 acceptance criteria；
* declared files；
* verification result；
* change audit。

这会导致 Review 退化成普通代码审查，而不是“按照 SPEC/PLAN 验收”。

### 修改方案

建议生成一个不可变的 Review Bundle：

```text
operations/<review-op-id>/
├── review-request.json
├── SPEC.md
├── PLAN.md
├── task-manifest.json
├── execution-result.json
├── diff.patch
└── prompt.md
```

Reviewer Prompt 明确引用这些文件，或者将必要内容直接内联。

推荐 Reviewer 的输入契约：

```json
{
  "run_id": "...",
  "task": { "...完整 task..." },
  "spec_markdown": "...",
  "plan_markdown": "...",
  "execution_result": { "..."},
  "base_commit": "...",
  "head_commit": "...",
  "diff_path": "diff.patch"
}
```

同时在 Grok 调用前后检查：

```text
HEAD 未变化
worktree clean 状态未变化
```

以确保 Reviewer 确实只读。

---

## P0-6：持久化提交协议与 Specs 不一致，而且崩溃恢复 hash 会失败

Specs 规定的顺序是：

```text
写 result/state_after 并 flush
→ 计算 state_after hash
→ append event 并 flush
→ 原子替换 state.json
→ status=COMMITTED
```

而 Plan 中的实际顺序是：

```text
op.commit()
  → 写 state_after
  → append event
  → status=COMMITTED

然后 Store 才：
  → next_state.save()
```

也就是：

> operation 已经标记为 COMMITTED，但 `state.json` 可能还没有更新。

如果在两者之间崩溃，就会出现：

```text
operation = COMMITTED
events.jsonl = 新状态
state.json = 旧状态
```

另外还有一个确定性的 hash 错误。

`state_after.json` 写入使用：

```python
json.dumps(
    state,
    indent=2,
    ensure_ascii=False,
    sort_keys=True,
)
```

但 hash 计算使用：

```python
json.dumps(
    state,
    sort_keys=True,
)
```

两者的空格、换行、缩进和非 ASCII 表达可能不同。

恢复时又直接 hash 文件内容。因此正常情况下：

```text
写入时记录的 hash
≠ 恢复时算出的文件 hash
```

恢复测试很可能失败。

### 修改方案

不要让 `Operation.commit()` 和 `Store.commit_transition()`分别负责半套协议。

由一个 `Store.commit_operation()` 持有文件锁并完成完整提交：

```text
with run_lock:
    1. 校验 expected_revision
    2. 写 request + PREPARED
    3. RUNNING
    4. 执行或接收 result
    5. canonical_json_bytes(state_after)
    6. 写 state_after.json
    7. hash 实际写入字节
    8. append event
    9. replace state.json
    10. COMMITTED
```

所有 JSON 都必须走同一个函数：

```python
def canonical_json_bytes(obj: object) -> bytes:
    text = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return canonical_text_bytes(text)
```

禁止一处使用 pretty JSON，另一处使用 compact JSON 后再认为 hash 相同。

---

## P0-7：Specs要求文件锁，Plan 的实现完全没有文件锁

Plan 文件结构说明中声称：

```text
io_atomic.py:
atomic write + file lock
```

Specs 的 v1 持久化范围也明确包括文件锁。

但实际 `io_atomic.py` 没有锁；CAS 只是：

```python
if expected_revision != self.revision:
```

这个比较发生在已经加载到内存的对象上，并不能阻止两个请求同时读取同一个 revision，然后分别提交。

即使 v1 设计为单线程，也可能存在：

* MCP 重复调用；
* Stop Hook 与主工具同时读取；
* ZCode重试；
* 崩溃恢复与新调用竞争。

### 修改方案

在依赖中加入：

```text
portalocker
```

或实现 Windows/Linux 双平台文件锁。

所有状态改变必须持有：

```text
<control_root>/.agentflow.lock
```

锁的粒度建议是整个 Run，而不是单文件。

---

## P0-8：v1“单 Task”没有被强制，可能只执行第一个 Task 就宣布完成

Specs 明确要求 v1：

> 单任务 + 一次 Review。

但 Plan 的 Prompt 写的是：

```text
For v1 this run will execute ONE task; still list all
```

Schema 又允许任意数量：

```json
"tasks": {
  "type": "array"
}
```

Engine 则直接：

```python
envelope["tasks"][0]
```

最后完成这个 Task 后进入：

```text
READY_TO_MERGE
```

这意味着 Codex只要输出 5 个任务，系统就可能：

```text
执行 T001
→ Review T001
→ READY_TO_MERGE
```

而 T002～T005 全部没做。

### 修改方案

v1 必须严格二选一：

### 方案 A：真正单 Task

推荐 v1 使用：

```json
"tasks": {
  "type": "array",
  "minItems": 1,
  "maxItems": 1
}
```

并在业务层再次检查：

```python
if len(envelope["tasks"]) != 1:
    raise WorkflowError("v1 requires exactly one task")
```

Prompt 改成：

```text
Produce exactly ONE independently executable task.
If the goal cannot be safely represented as one task,
return BLOCKED / NEEDS_USER_INPUT instead of multiple tasks.
```

### 方案 B：Plan可多 Task，但本次 Run 明确选择一个 Task

那就不能在这个 Task 完成后进入 `READY_TO_MERGE`，而应进入类似：

```text
TASK_READY
```

但这实际上已经接近 v1.3，不符合当前 v1 切片。

所以建议采用方案 A。

---

# 二、状态机和幂等实现还有一个严重缺陷

## `planner_draft_spec` 的提问重入会与 operation 幂等冲突

Specs v3.1 特别补充了：

```text
questions 非空
→ NEEDS_USER_INPUT
→ 携带 answers 重入 planner_draft_spec
```

但 Engine 为 draft 使用固定 operation ID：

```python
f"{run_id}-draft"
```

第一次调用后已经 COMMITTED。

第二次带 answers 重入时仍然使用同一个 operation ID，而且 request 里只有：

```python
{"goal": goal}
```

没有 answers。

可能出现两种错误：

1. 被识别为同一次幂等请求，直接返回旧结果；
2. SPEC 文件已经被覆盖，但状态没有按新结果提交。

此外，`planner_draft_spec()` 没有严格检查当前状态，理论上可从错误状态调用。

### 修改方案

每次外部操作的 ID 应绑定状态和 revision：

```text
<run_id>:draft-spec:r<revision>:attempt<n>
```

request 必须包含：

```json
{
  "goal": "...",
  "answers": {...},
  "state": "NEEDS_USER_INPUT",
  "expected_revision": 4
}
```

并对每个工具建立允许来源状态表：

```python
ALLOWED_FROM = {
    "planner_draft_spec": {
        "INITIALIZING_WORKTREE",
        "NEEDS_USER_INPUT",
    },
    "planner_create_plan": {
        "PLANNING",
    },
    ...
}
```

不能只在部分工具里检查状态。

---

# 三、用户级通用部署在 Plan 中被改成了静态环境变量

Specs 的核心要求之一是：

> 用户级通用部署，不绑定仓库；`target_repo` 作为运行时参数传入。

但 Plan 改成启动 MCP Server 前就必须设置：

```text
AGENTFLOW_TARGET_REPO
AGENTFLOW_CONTROL_ROOT
AGENTFLOW_WORKTREE_ROOT
```

这会产生问题：

* MCP进程启动时就绑定一个项目；
* `/agentflow-start --repo <path>` 无法真正把 repo 传给工具；
* 一个 ZCode 会话中难以创建不同项目的 run；
* `workflow_init()` 的签名也不再包含 `target_repo`；
* `control_root` 和 `worktree_root` 需要调用方提前计算。

这与“用户级通用插件”的目标不一致。

### 修改方案

MCP Server 启动时只需要知道：

```text
plugin_root
user_data_root
config_path
```

运行时：

```python
workflow_init(
    target_repo: str,
    goal: str,
    run_id: str | None = None,
)
```

由 MCP 内部计算：

```text
run_id
control_root
run_worktree_root
branch_name
```

例如：

```text
<user_data_root>/runs/<run_id>/control
<user_data_root>/runs/<run_id>/worktree
```

`workflow_status()` 则需要：

```python
workflow_status(run_id: str)
```

或者使用显式的 active-run session，而不是全局环境变量。

---

# 四、当前 E2E 测试本身无法按计划运行

Task 6.4 有多个测试设计问题。

## 1. monkeypatch 的 Engine 不是 Server 实际使用的 Engine

测试中先创建：

```python
eng = bootstrap.build_engine(...)
eng._call_planner = ...
```

但之前 Server factory 注册的是：

```python
lambda: bootstrap.build_engine(cfg)
```

每次 dispatch 都会创建一个新的 Engine。

因此 monkeypatch 的 `eng` 根本不会被 Server 使用。

### 修正

```python
eng = bootstrap.build_engine(cfg)
srv.set_engine_factory(lambda: eng)
```

---

## 2. 默认 fallback=false，会导致审批阶段失败

E2E 的 config 只写 auth，其他配置走默认值：

```text
tool_confirmation_supported = false
allow_non_adversarial_fallback = false
```

那么执行：

```python
workflow_approve_spec()
```

按设计应该直接抛出 `ApprovalUnavailableError`。

当前测试却期望继续通过。

### 修正

E2E 中显式配置：

```yaml
human_gates:
  spec_approval:
    allow_non_adversarial_fallback: true
```

或者模拟强制工具确认能力：

```python
capabilities.tool_confirmation_supported = True
```

---

## 3. 临时 plugin_root 中没有 prompts

测试把 `_plugin_root` 修改成 `tmp_path`，但没有把：

```text
prompts/planner-draft.md
prompts/planner-plan.md
```

复制到临时目录。

Engine 渲染 Prompt 时会找不到文件。

---

## 4. Planner 类型判断永远可能走错分支

Fake Planner 用：

```python
if "planner-plan" not in kw["prompt"]
```

但 `planner-plan.md` 的正文并不一定含字符串 `planner-plan`。

应显式传：

```python
_call_planner(kind="draft_spec", ...)
_call_planner(kind="create_plan", ...)
```

测试根据 `kind` 返回不同结果，不要从 Prompt 文本猜调用类型。

---

## 5. E2E 没有真正模拟 ZCode 修改和提交代码

当前成功路径：

```text
start_task
→ 不修改代码
→ 不提交 commit
→ verify
→ review
→ READY_TO_MERGE
```

这没有验证核心执行链：

```text
ZCode 修改代码
→ Git commit
→ clean worktree
→ diff
→ change audit
→ Review绑定新 commit
```

### 修正

E2E 必须在 `workflow_start_task` 后执行：

```text
修改 a.txt
git add
git commit
```

然后验证：

```text
head_commit != task_start_commit
changed_files == ["a.txt"]
review.head_commit == current HEAD
```

否则这个 E2E 只是状态机测试，不是 v1 垂直切片测试。

---

# 五、流程完整性校验还没有真正实现

Specs 的 S3 要求能发现：

* SPEC 被直接改动；
* PLAN 被直接改动；
* 陈旧 review；
* 陈旧 manifest；
* hash 不一致。

但 Plan 中多数检查只比较：

```text
review.spec_sha256
==
state.spec_sha256
```

问题在于 Review 的 hash 又是 Engine 从 State 中强制注入的。

也就是说，它比较的是：

```text
state 中的旧值
==
复制到 review 中的旧值
```

没有重新读取实际文件。

如果 `SPEC.md` 在批准后被直接改了：

```text
state.spec_sha256 = 旧 hash
SPEC.md = 新内容
review.spec_sha256 = 被注入的旧 hash
```

最终检查仍然全部通过。

### 修改方案

增加统一方法：

```python
def verify_locked_artifacts(state):
    assert hash_file(SPEC.md) == state.spec_sha256
    assert hash_file(PLAN.md) == state.plan_sha256
    assert hash_file(task-manifest.json) == state.task_manifest_sha256
```

至少在这些节点重新检查：

* `workflow_approve_spec`；
* `planner_create_plan`；
* `workflow_start_task`；
* `workflow_verify_task`；
* `reviewer_review_task`；
* `workflow_complete_task`。

特别是 `workflow_complete_task` 的硬校验应增加：

```text
当前 SPEC 文件 hash 匹配
当前 PLAN 文件 hash 匹配
当前 manifest 文件 hash 匹配
```

现有“8项硬校验”可以升级为 11 项，没必要为了保持数字 8 而遗漏真实保护。

---

# 六、其他重要问题

## 1. Canonicalization 没有实现“单个末尾换行”

Specs 要求：

```text
UTF-8
LF
保留单个末尾换行
```

当前实现只做 CRLF→LF：

```python
return regex.sub(b"\n", raw)
```

既不会：

* 为无换行文件添加末尾换行；
* 将多个末尾换行压缩为一个；
* 验证 UTF-8。

测试名称写了“preserves trailing single newline”，但没有测试：

```text
"body"
"body\n\n\n"
```

是否规范成相同字节。

建议增加：

```python
assert canonical_bytes(b"body") == b"body\n"
assert canonical_bytes(b"body\n\n") == b"body\n"
```

---

## 2. `events.jsonl` 的 seq 在进程重启后会从 1 重新开始

`Manager.__init__()` 中：

```python
self._seq = 0
```

每次 MCP Server 重启都会产生重复 seq。

应从 EventLog 最后一条记录加载：

```python
self._seq = event_log.last_seq()
```

并在同一个 Run 锁内递增。

---

## 3. Verification Runner 没实现配置声明的网络限制

Plan 的配置写了：

```yaml
network: deny
```

但 `policy_verify.run()` 没有任何网络隔离逻辑。

这属于“配置看起来支持，实际无效”。

v1 可选择：

* 真正实现；
* 或明确将配置改为 `network_policy: advisory_only`；
* 或从 v1 删除，留到后续版本。

不能保留一个没有执行效果的安全配置。

---

## 4. AGENTS.md 只创建模板，没有实现检测和追加

Specs 里的 R4 缓解方式是：

> 检测已有 AGENTS.md，引用补充，绝不覆盖。

当前 Plan 只创建 `AGENTS.md.template`，没有任何任务负责：

* 检测；
* 解析；
* 追加；
* 防重复；
* 保留原文件；
* 测试。

要么增加 `agents_context.py` 和对应任务，要么从 v1 成功标准中移除这一项。

---

## 5. Approval record 缺少 `run_id`

Specs 的批准记录包含：

```json
{
  "approval_id": "...",
  "run_id": "...",
  "gate": "SPEC"
}
```

Plan 的 `approval.py` 没有写 `run_id`。

此外，fallback 开启时 Specs 要求明确向用户提示保障等级降低；当前实现只是静默记录：

```text
non_adversarial_convention
```

建议返回：

```json
{
  "approval_method": "non_adversarial_convention",
  "security_warning": "Tool-level confirmation unavailable..."
}
```

由 ZCode必须显示给用户。

---

## 6. Stop Hook 的校验远弱于 Specs

Specs 要求 Stop Hook 检查：

* 测试结果；
* Review PASS；
* HEAD等于被审 commit。

当前 Hook 只检查：

```python
decision == "PASS"
```

即使：

* Review是旧的；
* Review对应不同 commit；
* 当前工作区又被修改；
* state 已进入 NEEDS_HUMAN；

Stop Hook 仍可能允许结束。

建议 Stop Hook 不自行重写判断逻辑，而是调用一个只读命令：

```text
agentflow check-stop --run-id ...
```

由 Engine 的统一校验函数返回：

```json
{
  "allow_stop": false,
  "reasons": [...]
}
```

避免 Hook 和 Engine 两套规则漂移。

---

## 7. Hook 输入格式和相对路径未经验证

当前 Hook 假设 ZCode 会传入：

```json
{
  "tool": "Edit",
  "path": "...",
  "control_root": "...",
  "current_task": "..."
}
```

但 Specs 已将 Plugin/Hook API Beta 和 capability check 列为风险。

Plan 没有真正做 capability check，只是配置了一个默认 false 字段。

此外：

```json
"command": "python hooks/protect_artifacts.py"
```

命令执行目录如果是目标仓库，而不是 plugin root，就找不到脚本。

第一阶段必须用真实 ZCode做一个 Hook payload capture spike，确认：

* 实际事件名；
* stdin JSON 格式；
* 退出码语义；
* 反馈字段；
* Hook命令 cwd；
* 插件根目录变量；
* Stop重入限制。

---

## 8. `pytest` 零测试不会按计划“退出 0”

Phase 0 写道：

```text
pytest collects 0 tests and exits 0
```

通常 pytest 在没有收集到测试时退出码是 5，而不是 0。

建议 Phase 0 就创建一个最小测试：

```python
def test_package_version():
    assert __version__ == "0.1.0"
```

或者只运行：

```text
python -c "import zcode_agentflow"
```

---

## 9. Schema 约束太宽松

当前多个 schema 只检查字段是 array/object，没有约束内部结构，也没有：

```json
"additionalProperties": false
```

例如：

* `verification_commands` 没定义 item schema；
* `issues` 没定义 severity/category/file/line；
* `change_audit` 没定义字段类型；
* hash 没定义 64 位小写 hex；
* commit 没定义格式；
* `tasks` 没有 maxItems=1。

已经出现了一个明显类型漂移：

示例里：

```json
"dependency_files_changed": false
```

实际 Policy 返回：

```python
"dependency_files_changed": dep_changed
```

也就是 list。

现有 schema 不会发现。

建议先把协议 schema 做严，再写 Engine。

---

## 10. 不建议自动执行 `git push origin main`

Plan 最后一步直接：

```bash
git push origin main
```

这与整个 AgentFlow 的人工批准思想不一致，也容易：

* 推错 remote；
* 推错分支；
* 绕过 PR；
* 修改共享 main；
* 在测试仓库中产生意外副作用。

建议改成：

```text
输出当前分支、commit、测试报告
→ READY_FOR_USER_REVIEW
```

是否 push，由用户或项目规则决定。

---

# 七、建议重排整个 Implementation Plan

当前顺序是：

```text
Foundation
→ Persistence
→ Git/Schema/Policy
→ Runner
→ Engine
→ Server
→ Packaging
→ E2E
```

这个顺序的问题是：最危险的外部集成——ZCode Plugin、MCP stdio、Codex CLI、Grok CLI——全部拖到后面。

如果早期对插件格式或 CLI 参数的假设不成立，前面大量代码可能需要重写。

## 推荐的新顺序

### Phase 0：Capability Spike

只做四件真实验证：

1. 最小 MCP echo server 能被 ZCode加载和调用；
2. 确认 ZCode Plugin manifest、Hook payload、tool confirmation；
3. Codex能从 `run_worktree_root` 收到 stdin Prompt 并输出 schema JSON；
4. Grok能以只读方式读取 Review Bundle 并输出可解析 JSON。

输出：

```text
docs/capabilities.json
```

内容包括：

```json
{
  "zcode": {
    "plugin_loaded": true,
    "mcp_stdio": true,
    "tool_confirmation": false,
    "hook_payload_version": "..."
  },
  "codex": {
    "command": "...",
    "reasoning_effort_high": true,
    "output_schema": true
  },
  "grok": {
    "command": "...",
    "json_shape": "...",
    "readonly_flags": [...]
  }
}
```

### Phase 1：项目骨架与实际 MCP Transport

* 改用 `src/zcode_agentflow/`；
* `pyproject.toml`；
* 真正的 FastMCP stdio server；
* `tools/list` 和 `tools/call` 集成测试；
* 运行时 `target_repo` 参数。

### Phase 2：严格协议与 Canonical Bytes

先完成四个严格 schema：

* spec；
* plan，强制一个 Task；
* execution；
* review。

然后完成统一：

```text
canonical_text_bytes
canonical_json_bytes
hash_file
```

### Phase 3：Store 和故障恢复

实现一个统一 Store：

* Run 文件锁；
* revision CAS；
* operations；
* event seq；
* 完整 commit order；
* FAILED/UNKNOWN；
* 自动恢复；
* fault injection tests。

这里建议增加崩溃点测试：

```text
在每一步后模拟崩溃
→ 重启 Store
→ 判断应继续提交、重放还是 NEEDS_HUMAN
```

### Phase 4：CLI Adapters

分别实现：

```text
CodexAdapter
GrokAdapter
VerificationAdapter
```

统一接口：

```python
invoke(
    request,
    repo_cwd,
    operation_dir,
    timeout,
)
```

使用假 CLI 可执行文件做集成测试，不只是 monkeypatch Python 函数。

### Phase 5：Engine

再实现 9 个工具：

* 严格状态来源；
* artifact rehash；
* exactly-one-task；
* answers 重入；
* operation ID；
* worktree/commit绑定。

### Phase 6：Approval、Policy、Hooks

* 强制确认 capability；
* fallback warning；
* Git `--name-status -z`；
* Stop Hook复用 Engine 校验；
* AGENTS.md 检测与追加。

### Phase 7：真正的 E2E

分三层：

1. **纯 Engine E2E**：Fake Adapters；
2. **MCP transport E2E**：真实 stdio；
3. **本机 smoke**：真实 Codex/Grok，可手工启用，不放默认单元测试。

成功 E2E 必须真实执行：

```text
创建仓库
→ 创建 worktree
→ Draft Spec
→ Approve
→ Plan
→ Start
→ 修改并提交文件
→ Verify
→ Review
→ Complete
→ READY_TO_MERGE
```

---

# 八、对当前 Plan 的具体修改清单

建议至少修改这些 Task：

| 原 Task       | 建议                                                             |
| ------------ | -------------------------------------------------------------- |
| Task 0.1     | 本地包从 `mcp` 改为 `zcode_agentflow`；增加 `pyproject.toml`、文件锁依赖和最小测试 |
| 新增 Task 0.2  | ZCode/MCP/Codex/Grok capability spike                          |
| Task 1.1–1.2 | 实现真正的单末尾换行和统一 canonical JSON bytes                             |
| Task 2.2–2.4 | 重写 event seq、文件锁、operation完整提交顺序、fault injection recovery      |
| Task 3.2/4.3 | Schema收紧，v1 `tasks.maxItems=1`                                 |
| Task 3.3     | 实现或删除虚假的 network deny；不要硬编码 allowlist 与 Specs 冲突               |
| Task 3.5     | Approval记录加入 run_id 和降级警告                                      |
| Task 4.1     | Codex Prompt真正传 stdin；cwd和output_dir分离                         |
| Task 4.2     | Grok读取完整 Review Bundle；落实真实只读 flag                             |
| Task 5.2     | 所有工具增加状态来源校验、artifact rehash、可重入 operation ID                  |
| Task 5.3/6.1 | 实现真实 MCP stdio server，而不是仅 dispatch                            |
| Task 6.2     | Hook复用统一 gate 校验，先确认真实 payload                                 |
| Task 6.3     | 插件 manifest 增加真实加载测试；实现 AGENTS.md追加逻辑                          |
| Task 6.4     | 完全重写 E2E fixture 和执行阶段                                         |
| Task 6.5     | 删除自动 `git push origin main`                                    |

---

## 最终评价

这份 Plan 的**设计方向是正确的**，而且它很好地继承了 Specs 的主要概念：

* 四根目录；
* 状态机；
* hash/revision；
* operations；
* Planner/Reviewer Adapter；
* 两层 Policy；
* 两道人工作用门；
* 单任务垂直切片。

但是，当前版本最大的问题是：

> **Plan 写得过于接近可复制实现代码，但这些实现代码中已经存在多处关键错误。**

如果只是一份高层计划，这些问题可以在编码时修正；但文档明确要求 Agent 使用 `subagent-driven-development` 按 Task 逐步执行，执行 Agent 很可能直接复制这些代码，并让局部测试“看起来通过”。

因此我对当前版本的结论是：

```text
Specs：可以继续沿用
Plans：需要修订后再执行
建议状态：CHANGES_REQUIRED
```

其中最优先的六项是：

```text
1. 解决 mcp 包名冲突
2. 做真实 MCP stdio Server
3. 修复 Codex prompt/cwd
4. 给 Grok完整 Review 上下文
5. 重写 Store 提交协议和 hash
6. 重写 E2E 测试
```

这六项不处理，后面的任务即使全部完成，也很难得到一个真正可由 ZCode加载并跑通的 `zcode-agentflow`。
