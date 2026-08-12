# Phase 4 执行 Prompt：Adapters + prompt templates

你将只执行 **Phase 4**。目标是实现稳定、可测试、只写 operation staging 的 Prompt Renderer 和三个 Adapter。默认测试使用 fake executables；不要在单元测试中反复调用真实 Codex/Grok或消耗额度。

## 入口

- 阅读 Plan Execution Errata、Phase 4、Task 4.0～4.4。
- 核对 Phase 0 的实际 CLI flags 和版本；核对 Phase 3 的 Store staging/ArtifactResolver 合同。


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


## Task 与硬约束

1. **4.0 Prompt templates**
   - 创建 planner-draft/planner-plan/reviewer；变量使用明确模板语法和允许变量集合。
   - 测试必须检查必要指令、单 Task、Raw Schema、Review Bundle、无未替换变量。
   - 测试使用 sample agent context；不得依赖尚未执行的 Phase 5.3。
   - Commit：`feat: prompt templates — {{ }} placeholders, inlined agent context (P0-2, parallel P1-2, P1-5)`
2. **4.1 Adapter base + redaction**
   - 参数数组、`shell=False`、显式 UTF-8、timeout、进程树终止。
   - raw输出先在内存捕获，再脱敏，再写 staging；绝不先落原始秘密再覆盖。
   - Adapter不得写 state/events/权威 artifacts。
   - Commit：`feat: adapter protocol + process-tree kill + redact-before-write ordering (Prompt §12,§20, P1-4)`
3. **4.2 CodexAdapter**
   - Prompt必须进入 stdin；cwd=run_worktree_root；output=operation staging，三者严格分离。
   - 使用 Phase 0 的真实 read-only/schema flags；验证 repo不变。
   - 只验证 Raw输出 schema，不注入权威字段。
   - Commit：`feat: codex adapter — stdin prompt, read-only, separated cwd/output, planner-result (P0-3,P0-4,P0-5)`
4. **4.3 GrokAdapter + Review Bundle**
   - Bundle包含完整 SPEC/PLAN/manifest/execution/diff/request/prompt和agent context副本。
   - 调用前后验证 HEAD、clean和bundle hashes；Raw review-decision不含权威字段。
   - Commit：`feat: grok adapter + immutable Review Bundle, raw-decision validation (P0-5, Prompt §13, parallel P0-4)`
5. **4.4 VerificationAdapter**
   - 只接受 structured command；不接受任意 shell string。
   - allowlist来自 config，不来自 Markdown。
   - executable/cmd_script/bash_script 分流；`.bat/.cmd`拒绝注入元字符和换行；平台测试正确skip。
   - network policy保持 advisory，不伪装隔离。
   - Commit：`feat: verification adapter — Windows-safe cmd quoting, config allowlist, platform markers (P1-1, P1-2, P1-5)`

## 禁止越界

- 不实现 Engine 状态转移、Approval、Change Policy。
- 不让 Adapter 获取 Run Lock。
- 不直接读取权威逻辑文件路径；Bundle输入来自 ArtifactResolver/调用方提供的已解析内容。
- 不使用真实 CLI替代 fake executable 测试；真实 CLI仅做一个受控 smoke，且结果脱敏。

## Phase Exit

运行 `python -m pytest tests/adapters -v`、Prompt专项测试，并回归 Phase 1～3。检查子进程 argv/stdin/cwd/output/timeout/kill/redaction、Windows quoting攻击用例和 repo/bundle不变性。报告后停止。
