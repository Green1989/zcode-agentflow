# Phase 6 执行 Prompt：Workflow Engine — 9 tools

你将只执行 **Phase 6**。这是工作流组装阶段：把已完成的 Store、Adapters、Approval、Change Policy 和 Agent Context 组合为严格状态机和恰好 9 个 FastMCP tools。不得重新实现这些底层模块或绕过它们。

## 入口

- 阅读 Plan Execution Errata、Phase 6 Task 6.1～6.8，以及 Specs v1状态机/9工具定义。
- Phase 2～5所有测试必须绿；发现底层缺陷时做最小修复并完整回归，不能在 Engine里复制一套替代逻辑。


## 全局执行纪律

1. **唯一执行依据**：实现计划 `D:\ZCode\zcode-agentflow\docs\superpowers\plans\2026-07-30-zcode-agentflow-v1-v2-2.md`。开始前完整阅读文档开头的 `Execution Errata`、本 Phase 的 Entry/Exit、全部 Task，以及相关 Appendix。Specs `D:\ZCode\zcode-agentflow\docs\superpowers\specs\2026-07-29-zcode-agentflow-design.md` 只作为架构事实来源。
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


## Engine 总不变量

1. `ALLOWED_FROM`、CAS、artifact rehash在 `Store.begin_operation` 的同一个锁临界区执行；Engine无第二把 Run Lock、无 `_guard`嵌套锁。
2. 每个外部调用严格 `begin → adapter无锁 → finish/fail/unknown`。
3. 权威 artifact全部经 ArtifactResolver；不得直接读写 `control_root/SPEC.md` 等路径。
4. 模型结果先 Raw Schema；Engine从 State注入run_id/commit/hash/version；再Final Schema。
5. v1只允许一个 Task；绝不使用 `tasks[0]`静默忽略多任务。
6. Review必须绑定 verified HEAD和所有hash；Review外部阶段未结束时complete被拒绝。
7. `workflow_complete_task` 至少执行 Plan列出的全部硬检查；任一失败不得 READY_TO_MERGE。
8. MCP surface恰好9个；abandon/archive是local admin command，不是第10个工具。
9. READY_TO_MERGE只标记 active pointer `terminal:true`；不push、不merge、不清除pointer。

## Task 顺序

- 6.1 Engine skeleton / ALLOWED_FROM / rehash / read-only check_stop。Commit：`feat: engine scaffold — ALLOWED_FROM inside begin, rehash, check_stop (Prompt §14,§15, Review §五, P0-3)`
- 6.2 workflow_init + draft_spec answers re-entry。Operation id绑定revision/attempt；agent context通过stdin内联。Commit：`feat: workflow_init + planner_draft_spec with answers re-entry (Prompt §11,§15, Review §二)`
- 6.3 approve_spec + create_plan wrapper；批准sha锁定，Raw/Final分层，恰好一个Task。Commit：`feat: approve_spec + create_plan via planner-result wrapper, exactly-one-task (P0-8, P0-5)`
- 6.4 start_task + verify_task + change audit；验证时worktree clean、真实commit、命令策略和变更策略。Commit：`feat: start_task + verify_task + change audit + cmd_script support (Prompt §16, P1-5)`
- 6.5 reviewer_review_task；Review前HEAD必须等于 execution_result.head_commit / state.verified_head_commit，完成后才设置last_reviewed_commit。Commit：`feat: reviewer_review_task with full bundle + hash injection (P0-5)`
- 6.6 complete_task；执行全硬门，进入READY_TO_MERGE，无push/merge。Commit：`feat: workflow_complete_task — expanded rehash hard checks, READY_TO_MERGE, no merge/push (Prompt §14, Review §五)`
- 6.7 把9 tool body接到Engine并做真实stdio调用。Commit：`feat: wire engine into 9 FastMCP tools; stdio full happy path (P0-2)`
- 6.8 pointer lifecycle；workflow_init委托Task3.7，不重复set_pointer；terminal mark和local admin abandon/archive。Commit：`feat: pointer lifecycle — terminal-mark, init delegates to init_run, abandon as local admin (errata, P0-1/P0-4, parallel P0-3, consistency #4)`

## 必测拒绝路径

- wrong state、stale revision、active operation并发；
- questions重入使用新operation id；
- multi-task/invalid raw schema；
- SPEC/PLAN/manifest落盘后被直接修改；
- dirty/untracked/verification fail/未授权变更；
- stale review、HEAD mismatch、PASS含issues；
- review运行中尝试complete；
- READY_TO_MERGE后禁止业务状态继续推进。

## Phase Exit

依次运行所有 `tests/unit/test_engine_*.py`、`tests/mcp/test_all_tools_stdio.py`，再运行 Phase 1～6 全部默认测试。输出9工具状态转换表、拒绝矩阵、每Task commit和Phase Exit证据。不要开始 Hook 或 packaging。
