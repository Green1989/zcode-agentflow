# Phase 8 执行 Prompt：Three-layer E2E + docs + READY_TO_MERGE handoff

你将只执行 **Phase 8**，完成最终验收。目标是用三层E2E证明v1垂直切片真实可用，输出READY_TO_MERGE handoff；禁止通过削弱测试或跳过真实commit来制造“通过”。

## 入口

- 阅读 Plan Phase 8 Task 8.1～8.5。
- Phase 1～7 的unit/adapter/store/mcp/packaging测试必须绿；工作区干净。


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


## Task 8.1 — Layer 1 Engine E2E

- Fake Codex/Grok，但使用真实Git仓库、真实worktree、真实文件修改和真实commit。
- Happy path必须从NEW到READY_TO_MERGE。
- 覆盖完整拒绝矩阵：stale revision、artifact mutation、dirty/untracked、verification failure、unauthorized change、stale/mismatched review、restart recovery等。
- Commit：`test: Layer-1 engine E2E — real commit + rejection matrix (Prompt §21)`

## Task 8.2 — Layer 2 MCP stdio E2E

- 同一 happy/rejection路径通过真实MCP client和server subprocess完成，不直接调用Engine。
- 验证进程重启后active pointer恢复和最终workflow_status可见READY_TO_MERGE。
- Commit：`test: Layer-2 MCP transport E2E over stdio (Prompt §21)`

## Task 8.3 — Layer 3 real CLI smoke

- 默认pytest不得收集/运行真实Codex/Grok smoke；仅 `AGENTFLOW_SMOKE=1`手工启动。
- 使用最小、低成本、结果可验证的测试repo；真实读写边界、两道人工作用门和Grok PASS均要观察。
- 登录/UI所需步骤精确请求用户配合；任何失败如实记录，不能改成Fake后宣称Layer3通过。
- Commit：`test: Layer-3 local smoke harness (real CLIs), off by default (Prompt §21)`

## Task 8.4 — Handoff

- 报告包含branch、base/HEAD、commit范围、tests、review decision/hashes、未授权变化检查和用户下一步。
- 不执行push/PR/merge；不声称已合并。
- Commit：`feat: READY_TO_MERGE handoff report + docs; no auto-push (Prompt §22, Review §六.10)`

## Task 8.5 — Full suite

- 运行 `python -m pytest -q`；smoke必须被排除。
- 检查git diff、未跟踪文件、秘密扫描、9-tool surface、pointer terminal状态。
- Commit：`chore: v1 vertical slice complete — full suite green`

## 最终验收报告

除通用Phase报告外，必须给出：

- 三层E2E各自是否PASS和实际命令；
- Layer3真实模型/CLI版本与脱敏观察；
- v1已知限制（单Task、一次Review、FAIL→NEEDS_HUMAN、无自动修复/Replan/Final Review/merge）；
- READY_TO_MERGE分支和HEAD；
- 所有提交列表；
- 明确声明“未push、未merge、未创建PR”。

完成后停止，不做任何发布或合并操作。
