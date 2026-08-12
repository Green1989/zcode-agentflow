# Phase 5 执行 Prompt：Approval + change policy + agent context

你将只执行 **Phase 5**。目标是实现 Engine 依赖的三项策略能力：可信批准记录、Git变更策略、Agent上下文解析。不得提前组装 Engine。

## 入口

- 阅读 Plan Phase 5 Task 5.1～5.3 和相关 Execution Errata。
- 核对 Phase 0 中 tool confirmation 的真实配置字段/作用域；Phase 2 gitops/schema/canonical 已完成。


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


## Task 要求

### 5.1 Approval

- “supported”不等于“configured”；必须使用 Phase 0发现的真实字段将 mandatory confirmation 配置在 `workflow_approve_spec` 上。
- Approval record包含 run_id、gate、artifact sha、method、时间和必要警告。
- fallback=false且不支持时应已在startup fail；fallback=true时必须返回并展示 `security_warning`，不能静默降级。
- 普通工具不得弹出同样确认。
- Commit：`feat: approval — configured confirmation, run_id, non-silent fallback (P1-1, Prompt §17)`

### 5.2 Git change policy

- 基于 `gitops.name_status_z`，正确解析 A/M/D/R/C 和特殊文件名。
- declared/planned/authorized 规则可配置；未授权删除、依赖变化、迁移和越界文件进入 NEEDS_HUMAN。
- 不可逆 migration 即使“已授权”也必须 NEEDS_HUMAN。
- Commit：`feat: git change policy via name-status -z, configurable (Prompt §16)`

### 5.3 Agent context

- 读取用户现有 `AGENTS.md`，绝不覆盖、编辑或格式化它。
- 合并模板后写入 control_root 的 `resolved-agent-context.md`；重复运行不重复追加 section。
- AGENTS.md是上下文，不是verification安全allowlist。
- Commit：`feat: agent context resolution, never overwrite AGENTS.md (Prompt §19)`

## 禁止越界

- 不实现 9 tools、Store 状态转移或 Hook。
- 不把 fallback approval记录成强确认。
- 不解析自由Markdown来生成可执行命令白名单。
- 不自动授权删除/迁移。

## Phase Exit

运行三组专项测试和 `python -m pytest tests/unit -v`。必要时，tool-confirmation真实弹窗测试需要用户在 ZCode UI配合；先准备完整插件/步骤，再请求最小操作。报告强/降级批准模式、变更策略矩阵、AGENTS.md未变化证据后停止。
