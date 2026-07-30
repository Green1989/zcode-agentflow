## 最终收敛结论

**这份 v2.2 已经可以批准进入实施阶段。**

建议状态标记为：

```text
APPROVED_FOR_EXECUTION_WITH_ERRATA
```

这里的 `ERRATA` 不是要求再生成一份 v2.3，更不是要求重新设计架构，而是在执行前对文档做一次**小范围勘误**。主体架构已经收敛：

* Registry Lock 与 Per-Run Lock 分层合理；
* `begin → external → finish/fail` 两阶段调用基本闭环；
* Operation Lease、revision 和单 active operation 已明确；
* `state.json` 作为产物提交指针的方向正确；
* ArtifactResolver 统一读取；
* Raw Model Output 与 Final Authoritative Artifact 分层；
* Windows 命令参数安全策略明显加强；
* E2E、故障注入、恢复和真实 CLI Smoke 分层合理。

没有必要再让 Agent 完整重写 Plan。下面几处直接修改到 v2.2，或在文档开头添加一节“Execution Errata”，然后就可以开始 Phase 0。

---

# 一、执行前必须写入的 4 条勘误

## 1. 清除所有残留的 `current.json` 和“终态清除 Pointer”描述

Task 3.5 已经明确采用：

```text
state.json.artifact_generation
```

作为唯一提交指针，不再存在 `current.json`。但文档前部和附录中仍有旧描述：

* Architecture 仍写“single `current.json` pointer”；
* v2.2 revision summary 仍写“single `current.json` switch”；
* Appendix D 仍有类似描述；
* Task 1.3a 的说明仍写 READY_TO_MERGE 时“clears”；
* Task 6.6 仍写 Task 6.8 会清除 active-run pointer；
* Task 6.8 标题说明中仍有“clear/archive pointer”。

这些文字与 Task 3.5、Task 6.8 的最终设计相反。

统一替换为：

```text
权威 Artifact 提交指针：
state.json.artifact_generation

READY_TO_MERGE 时：
active-run.json 标记 terminal=true，不清除。

active-run.json 仅在以下情况被替换或清除：
1. 用户开始新的 Run；
2. 用户显式 abandon；
3. 用户显式 archive。
```

这是纯文档修正，但必须做，因为执行 Agent 通常会优先读取 Architecture 和 revision summary。

---

## 2. 修复两处残留的前向依赖

文档宣称可以线性执行，但还有两处实际前向依赖。

### Task 1.3a → Task 2.2

Task 1.3a 使用：

```python
canonical.write_canonical_json(...)
```

但 `canonical.py` 到 Task 2.2 才实现。

推荐把 Task 2.2 的 canonical 基础设施提前到 Task 1.3a 之前。Canonical JSON、原子写入和 Hash 本来就是 Store、Pointer、Config 都会使用的基础模块，提前更合理。

不建议在 Task 1.3a 临时再实现一套 JSON 写入逻辑，否则会产生两套 canonical 规则。

### Task 4.0 → Task 5.3

Task 4.0 写了：

```text
Depends: 2.1, 2.7, 5.3
```

但 Task 5.3 位于后面的 Phase 5。

实际上，创建 Prompt 模板并不需要 `agents_context.py` 已经实现；测试时用一段 sample agent context 即可。真正读取 `resolved-agent-context.md` 并传入 renderer，是 Engine 集成阶段的职责。

因此直接改为：

```text
Task 4.0 Depends: 2.1, 2.7

Task 6.2/6.3 Depends: 4.0, 5.3
```

完成这两处后，计划才真正符合“可按顺序线性执行”。

---

## 3. 明确 Registry Lock 的唯一持有者，避免再次出现嵌套锁

当前接口写成：

```python
def set_pointer(...):
    # UNDER registry_lock
```

但 Task 3.7 又规定：

```text
workflow_init 已经持有 Registry Lock
→ 调用 set_pointer(...)
```

Task 6.8 也有类似描述。

这里的“UNDER”存在歧义：

* 是 `set_pointer()` 自己获取 Registry Lock；
* 还是调用者必须已经持有 Registry Lock？

如果两者都做，由于 Registry Lock 被定义为 non-reentrant，会再次出现自死锁。

同时，Task 3.7 和 Task 6.8 都声称负责 init 时写 Pointer，职责重复。

### 推荐最终协议

定义一个 Registry 事务对象：

```python
with ActiveRunRegistry.locked(user_data_root) as registry:
    registry.assert_new_run_allowed()
    # 创建 Run、Control Root、Worktree
    registry.set_pointer_locked(pointer)
```

规则：

```text
ActiveRunRegistry.locked()
    负责获取和释放 Registry Lock。

set_pointer_locked()
clear_pointer_locked()
mark_terminal_locked()
    只能在 locked() 上下文内调用；
    自身绝不再次获取锁。

普通代码不得直接调用底层 pointer 写入函数。
```

职责建议这样收口：

```text
Task 3.7：
负责 workflow_init 的 Registry 临界区、Run 分配、
worktree 创建和初始 pointer 写入。

Task 6.8：
Engine 的 workflow_init 委托 Task 3.7 的 init_run，
不再重复 set_pointer；
只补 READY_TO_MERGE 的 terminal 标记和 abandon/archive 生命周期。
```

这样既不会嵌套 Registry Lock，也不会写两次 Pointer。

---

## 4. 明确 Finish Commit 的唯一顺序

Task 3.4 和 Task 3.5 当前还有一处职责重叠：

Task 3.4 看起来是：

```text
promote
→ append event
→ replace state.json
```

但 Task 3.5 的 `promote_artifacts()` 本身又负责：

```text
os.replace(state.json)
```

这会让执行 Agent 不确定究竟由谁完成最终提交。

### 推荐唯一协议

将 `promote_artifacts` 改名或定义为：

```python
prepare_generation(...)
```

它只负责：

```text
1. 写 content-addressed blobs；
2. 写完整 generation manifest；
3. 生成 next_state.artifact_generation；
4. 计算 next_state 中的各项 hash；
5. 不修改当前 state.json。
```

`finish_operation()` 独占最终提交：

```text
在 Per-Run Lock 内：

1. 校验 Operation Lease 和 revision_after_begin；
2. 校验 staged model/result schemas；
3. prepare_generation；
4. 写 immutable state_after snapshot；
5. append finish event 并 fsync；
6. 单次 os.replace(state.json)；
7. 将 operation 标记为 COMMITTED；
8. 清除 active_operation_id 和 lease。
```

崩溃语义：

```text
崩溃在步骤 6 之前：
运行时仍看到旧 state；
recover 可根据 event/state_after 决定是否前滚。

崩溃在步骤 6 之后、步骤 7 之前：
state 已提交；
recover 将 Operation 补标为 COMMITTED。

崩溃在 generation blob/manifest 写入期间：
state 未切换；
新 generation 不可见，可安全丢弃。
```

这不会改变当前架构，只是把已经设计好的机制明确成一个不可误解的提交协议。

---

# 二、建议同步修正的 4 个一致性问题

这些问题不值得再开启一轮架构审核，但应与上面的勘误一并修改。

## 1. 删除 Task 2.7 中与 Raw Schema 分层冲突的测试

Task 2.1 已规定 Raw `plan-proposal` 不允许包含 `base_commit`，但 Task 2.7 仍写：

```text
Codex-supplied base_commit 不匹配时拒绝
model-supplied fields ignored/overwritten
```

这两种策略不能同时存在。

既然 v2.2 已选择 Raw/Final Schema 分层，最终规则应是：

```text
Raw Schema：
根本不允许 base_commit、run_id、hash 等权威字段。

Engine：
从 State 注入权威字段。

Final Schema：
验证注入后的最终 Envelope。
```

因此删除：

```text
test_base_commit_must_equal_state
model-supplied values ignored/overwritten
```

改成：

```text
test_raw_plan_rejects_authoritative_fields
test_final_plan_uses_state_base_commit
```

---

## 2. `manifest.py` 不应直接写权威 control_root 文件

Task 2.5 仍定义：

```python
write_manifest(control_root, manifest)
```

这与“所有权威 Artifact 只能通过 Store staging + generation 提交”冲突。

改成：

```python
def build_manifest(plan_envelope) -> dict
def manifest_bytes(manifest) -> bytes
```

或：

```python
def write_manifest(staging_dir, manifest) -> Path
```

最终权威落盘仍由 Store 的 generation commit 完成。

---

## 3. Review 前的 HEAD 应绑定 Execution Result，而不是 `last_reviewed_commit`

Task 6.5 在 Review 尚未发生前写：

```text
assert HEAD == last_reviewed_commit
```

此时通常还没有 `last_reviewed_commit`。

应改为：

```text
assert HEAD == execution_result.head_commit
assert HEAD == state.verified_head_commit
```

Grok Review 完成后，Engine 再设置：

```text
state.last_reviewed_commit = review_result.head_commit
```

这也与 Spec 中 Review 绑定当前验证后 commit 的设计一致。

---

## 4. 明确 `abandon_run` 不是第 10 个 MCP Tool

Spec 和 Plan 都明确固定为 9 个 MCP Tools。

但 Task 6.8 新增：

```python
def abandon_run(self, run_id)
```

如果将它注册为 MCP Tool，就会破坏 9-tool surface；如果不注册，用户又无法触发“explicit abandon”。

推荐将它定义成**本地维护命令**，不属于 MCP Tool：

```text
zcode-agentflow admin abandon --run-id <id>
```

或者：

```text
/agentflow-status
→ 显示人工恢复命令
```

文档明确：

```text
MCP tools：仍然严格为 9 个。
abandon/archive：本地管理命令，不参与业务状态机。
```

---

# 三、不再阻塞执行的事项

以下细节已经有足够测试保护，可以交给实现阶段处理，不需要继续修订 Plan：

* CMD Script 最终 quoting 细节；
* portalocker 在具体 Windows 版本上的行为；
* FastMCP 的准确 import 和 framing；
* ZCode Hook payload；
* ZCode mandatory confirmation 的真实配置字段；
* Grok的只读参数和 JSON 输出形态；
* Codex `reasoning_effort=high` 是否可用；
* Bash在不同 Windows环境下是否存在。

这些本来就是 Phase 0 Capability Spike 的职责。Plan正确地没有假设它们必然成立，而是要求先实际验证。

---

# 四、上一轮核心问题最终状态

| 项目                       | 最终判断                        |
| ------------------------ | --------------------------- |
| State/Artifact 单一提交指针    | 已解决，需清理残留 `current.json` 文字 |
| Operation Lease/revision | 已解决                         |
| 单 active operation       | 已解决                         |
| Adapter异常/超时退出路径         | 已解决                         |
| Registry Lock            | 已解决，需明确非嵌套调用协议              |
| Active-run终态保留           | 已解决，需清理“clear pointer”旧文字   |
| Raw/Final Schema分层       | 已解决，需删除 Task 2.7 旧测试        |
| CMD注入防护                  | v1范围内已足够                    |
| ArtifactResolver统一读取     | 已解决                         |
| Prompt内联Agent context    | 已解决                         |
| Lock文件真实安装               | 已解决                         |
| Redact-before-write      | 已解决                         |
| E2E真实commit              | 已解决                         |
| 自动push/merge             | 已排除                         |

---

# 五、批准意见

建议不要再生成一份完整的 `2026-07-30-zcode-agentflow-v1-v2-3.md`。

最合理的收敛方式是：

```text
1. 保留 v2.2 作为正式 Implementation Plan；
2. 在文档开头增加 “Execution Errata”；
3. 写入上述 8 项勘误；
4. 将状态标记为 APPROVED_FOR_EXECUTION；
5. 开始执行 Phase 0 Capability Spike。
```

最终状态：

```text
Plan v2.2
Architecture: APPROVED
Execution readiness: APPROVED AFTER ERRATA
Further full-plan review: NOT REQUIRED
```

换句话说，**现在已经不应该继续围绕 Plan 本身反复迭代了**。接下来的不确定性必须通过 Phase 0 的真实 ZCode、Codex CLI、Grok CLI 和 Windows 环境实验解决，而不是继续靠纸面推演。