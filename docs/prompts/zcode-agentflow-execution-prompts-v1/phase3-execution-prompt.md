# Phase 3 执行 Prompt：Run lock + Store + two-phase transactions + recovery

你将只执行 **Phase 3**。这是整个项目的核心可靠性阶段。目标是构建可恢复 Store、Operation Lease、两阶段事务、唯一 artifact commit pointer、故障注入、事务化 workflow_init 和 active-pointer recovery。不得接入真实 Codex/Grok业务调用。

## 入口

- 完整阅读 Plan 的 Execution Errata，尤其：`state.json.artifact_generation` 是唯一指针、`finish_operation` 是 state.json 唯一提交者、Registry Lock唯一拥有者模式。
- 阅读 Phase 3 Task 3.1～3.8；确认 Phase 2 全绿。


## 全局执行纪律

1. **唯一执行依据**：实现计划 `<repo-root>\docs\superpowers\plans\2026-07-30-zcode-agentflow-v1-v2-2.md`。开始前完整阅读文档开头的 `Execution Errata`、本 Phase 的 Entry/Exit、全部 Task，以及相关 Appendix。Specs `<repo-root>\docs\superpowers\specs\2026-07-29-zcode-agentflow-design.md` 只作为架构事实来源。
2. **只执行当前 Phase**：不得提前实现后续 Phase；不得顺手重构无关代码；不得修改 Spec、Plan、旧 Plan 或任何 Review。
3. **先检查再行动**：先运行 `git status --short --branch`、检查当前分支、最近提交和上一 Phase 的 Exit 证据。工作区不干净时先说明已有变更归属，不得覆盖或丢弃用户改动。
4. **TDD 强制**：每个 Task 必须先写/补测试，运行并确认它因目标行为缺失而失败；再写最小实现；再运行专项测试变绿。不得先写实现再补“配合实现”的测试。
5. **一个 Task 一个 Commit**：专项测试通过、范围复核通过后，使用 Plan 指定的**确切 commit message**提交。不得合并多个 Task；不得 amend 或重写用户已有提交，除非用户明确要求。
6. **双层复核**：每个 Task 完成后先做实现自查，再用一个独立 review subagent（若环境支持）只读检查：是否满足 Task、测试是否有效、是否越界、是否破坏既有不变量。发现问题先修复并重跑测试，再提交。
7. **外部事实以 Phase 0 为准**：`docs/capabilities.json` 的真实发现优先于模型记忆、通用文档和 Plan 中的示例。不得伪造或绕过 capability gate。
8. **不自动 push/merge/PR**：本 Phase 只做本地 commit。禁止 `git push`、自动 merge、自动创建 PR、强制 reset、删除用户分支或破坏性清理。
9. **失败处理**：测试失败时先定位根因，不得删除/弱化测试来换取绿灯。发现必需能力缺失、Plan 与真实环境发生重大冲突、或需破坏性操作时，停止在安全状态并报告证据。
10. **报告必须可核验**：所有“通过”必须附实际命令和结果摘要；不能只说“应该可以”。不泄漏 token、账号信息、本机敏感绝对路径或未经脱敏的 raw 输出。

## 每个 Task 的固定执行循环

1. 阅读该 Task 的 Goal、Depends、Files、Tests first、Implementation requirements、Verification、Expected、Commit message。
2. 检查依赖是否已由提交和测试满足；不满足则停止，不得跳过。
3. 列出本 Task 允许修改的文件和明确不允许修改的文件。
4. 写测试并运行，记录预期的 RED 证据。
5. 写最小实现，避免提前实现后续能力。
6. 运行 Task 指定的 Verification；得到 GREEN。
7. 运行与本 Task 相关的既有回归测试。
8. 检查 `git diff --check`、`git status --short`、实际 diff 和未跟踪文件。
9. 做独立复核；修复后重新验证。
10. 使用 Plan 的确切 commit message 提交；记录 commit hash。

## 当前 Phase 完成报告格式

完成全部 Task 后，用以下结构报告，不得继续下一个 Phase：

```text
Phase: <编号和名称>
结论: PASS / BLOCKED / PARTIAL
完成的 Tasks:
- Task x.x — commit <hash> — verification <command/result>

Phase Exit 条件:
- [x]/[ ] 条件 1：证据
- [x]/[ ] 条件 2：证据

全量/阶段回归:
- 命令：...
- 结果：...

偏差与风险:
- 无；或逐项列出事实、影响、处理建议

工作区状态:
- git status 摘要
- 未 push / 未 merge

是否建议进入下一 Phase: 是 / 否（理由）
```


## 不可破坏的架构不变量

1. **锁分层**：Registry Lock 只保护 run registry；Per-Run Lock 只保护单 Run。两者均不可重入。
2. **Store是唯一写者**：只有 Store 修改 `state.json`、`events.jsonl`、`operations/` 和权威 generation。
3. **外部阶段绝不持锁**：`begin_operation` 短锁 → external无锁 → `finish/fail/unknown` 短锁。
4. **Operation Lease**：finish 必须验证 operation_id、lease token hash、`revision_after_begin`；不同 active op 的 begin 必须拒绝。
5. **唯一提交点**：不存在 `current.json`。`prepare_generation` 只写 blob/index并填 next_state；只有 `finish_operation` 单次 `os.replace(state.json)`提交。
6. **全部读取走 ArtifactResolver**：不得直接拼 `control_root/SPEC.md` 等逻辑路径。
7. **恢复不猜测**：SAFE_READ可自动重放；COSTFUL_READ按配置；UNSAFE_WRITE未知时进入 NEEDS_HUMAN。
8. **无破坏性补偿**：半创建 worktree 不自动删除；所有归属不明冲突进入 NEEDS_HUMAN。

## Task 顺序

- 3.1 Per-Run non-reentrant lock。Commit：`feat: per-Run non-reentrant file lock (P0-7, P0-3 guard)`
- 3.2 resumable event seq + atomic state。Commit：`feat: resumable event seq + atomic state snapshot (§六.2)`
- 3.3 operation lifecycle/idempotency/replay_policy。Commit：`feat: operation lifecycle + idempotency + 3-valued replay_policy (Prompt §9,§10, clarification)`
- 3.4 begin/finish/fail/unknown + Lease。Commit：`feat: two-phase begin/finish with lease+revision semantics + fail/unknown paths (P0-3, P0-2)`
- 3.5 Artifact Index + Resolver + state sole pointer。Commit：`feat: state.json as sole commit pointer + Artifact Index + ArtifactResolver (P0-3, P1-4, parallel P0-1)`
- 3.6 全故障注入矩阵。Commit：`test: two-phase fault-injection recovery matrix (Prompt §3,§10)`
- 3.7 transaction workflow_init/preflight/Registry critical section。Commit：`feat: transactional workflow_init + preflight + registry-locked run creation (Prompt §11, P0-4, errata)`
- 3.8 Store.recover + pointer recovery。Commit：`feat: Store.recover + active-pointer recovery integration (P0-1, P0-2)`

每个 Task 使用 Plan 中的确切 Verification 命令。3.4、3.5、3.6 完成后必须额外做一次只读架构复核，检查是否出现隐藏的第二写者、第二指针、嵌套锁或 direct artifact read。

## Phase 3 特别测试要求

- 使用真实跨进程锁测试，不只在线程内 mock。
- 故障点覆盖 begin、external前后、result、每个 blob、manifest、event、state replace、COMMITTED 前后。
- 每个故障点重启新的 Store 实例，不能复用内存状态假装恢复。
- 明确测试 status/read在 external阶段可用。
- 明确测试 review external 尚未完成时 complete begin 被拒绝。
- 明确测试 state损坏后仅恢复 active pointer指向的 Run，不扫描切换到其他项目。

## Phase Exit

运行：

```bash
python -m pytest tests/unit/test_locks.py tests/unit/test_events_state.py tests/unit/test_operations.py -v
python -m pytest tests/store -v
python -m pytest tests/unit/test_workflow_init_recovery.py -v
```

随后运行 Phase 1～3 的全部非外部CLI测试。报告完整故障矩阵、commit列表和不变量审计后停止，不进入 Phase 4。
