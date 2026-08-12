# Phase 0 执行 Prompt：Real Capability Spike

你将执行 ZCode-AgentFlow v1 Implementation Plan 的 **Phase 0**。这是一轮真实环境能力验证，不是产品代码开发。目标是验证 ZCode、MCP SDK、Codex CLI、Grok CLI 和 Windows/Git Bash 的实际契约，并把脱敏结论固化为后续所有 Phase 的唯一外部事实来源。

## 强制阅读

- 唯一执行 Plan：`<repo-root>\docs\superpowers\plans\2026-07-30-zcode-agentflow-v1-v2-2.md`，完整阅读 `Execution Errata`、Phase 0、Task 0.1～0.5。
- Specs：`<repo-root>\docs\superpowers\specs\2026-07-29-zcode-agentflow-design.md`，重点理解威胁模型、四根目录和工具职责。
- 仓库：`<repo-root>`。


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


## Phase 0 特殊纪律

1. **不要先向用户询问可以自动检测的前置条件。** 你先自行运行并报告：`python --version`、`git --version`、`codex --version`、`grok --version`、相关认证/最小只读探测。只有必须在 ZCode GUI 中加载插件、触发 Hook、响应确认弹窗时，才给用户一组明确、最少的操作步骤。
2. **不得假设外部接口。** FastMCP import、manifest 字段、Hook payload/cwd/退出码、mandatory confirmation 配置、Codex/Grok flags 都必须有真实运行证据。
3. **Spike 代码和 raw 输出放在仓库外的临时 control_root。** 仓库内只提交脱敏后的 `docs/capabilities.json`、`requirements.lock` 和必要的 `.gitignore` 调整。绝不提交 token、认证文件、完整环境变量值和本机敏感绝对路径。
4. 若 `docs/capabilities.json` 已存在：先读取并核对其证据来源；保留已确认结果，以本次真实结果增量更新，不得无依据覆盖 true/false。
5. 每一项 capability 不只写布尔值，还要记录：使用的脱敏命令/版本、观察到的输出形状、证据摘要、结论和未知项。
6. Task 0.1～0.4 每完成一项，就增量更新并提交 capabilities；Task 0.5 最终固定版本、lock 和 gates。共 5 个独立 commit。

## 执行顺序与人工触点

### Task 0.1 — 最小 FastMCP echo

- 在仓库外创建最小 echo server 和临时 plugin manifest。
- 先直接验证 MCP initialize/tools/call。
- 准备好文件后，再请用户在 ZCode 中加载并调用 `echo("hi")`；给出精确路径和操作步骤。
- 由你根据实际文件、日志和用户观察归纳 manifest 字段；不要让用户替你猜字段。
- Commit：`spike: minimal FastMCP echo server loadable by ZCode`

### Task 0.2 — Hook 与工具确认

- 创建只记录脱敏结构的临时 Hook dumper。
- 请用户在 ZCode 中分别触发一次适当的 Edit/ToolUse 和 Stop，并响应一次工具确认探测。
- 捕获真实 event 名、stdin keys、cwd、退出码、反馈通道、stop reentry limit、confirmation 字段和作用域。
- Commit：`spike: capture ZCode hook contract + tool-confirmation configuration`

### Task 0.3 — Codex CLI

- 在最小 Git 仓库上验证 prompt 从 stdin 输入、cwd=repo、read-only 生效、schema JSON 写入独立 operation dir。
- 明确记录 reasoning effort high 是否支持；不支持时验证 medium，而不是假装 high 成功。
- Commit：`spike: codex stdin prompt + read-only cwd + schema output confirmed`

### Task 0.4 — Grok CLI

- 创建完整最小 Review Bundle；真实验证 Grok 读取 SPEC/PLAN 并输出可解析 JSON。
- 记录实际 read-only flags；调用前后比较 bundle 和 repo，确认没有修改。
- Commit：`spike: grok read-only Review Bundle + JSON output confirmed`

### Task 0.5 — 固定版本和 gates

- 写入精确版本；生成 `requirements.lock`。
- gates 分为 required / optional / fallback-capable；false 与 unknown 如实保留。
- 执行秘密和路径自查，可使用正则扫描 token-like 字符串与绝对路径。
- Commit：`spike: pin versions (lock file) + classify capability gates`

## Phase 0 硬门禁

以下任何情况都不得宣称 Phase 0 PASS：

- echo 工具未在真实 ZCode 中调用成功；
- Hook 合同或 confirmation 配置仍靠猜测；
- Codex stdin/cwd/read-only/schema 任一必需项未验证；
- Grok未真实消费 Review Bundle或无法证明只读；
- capabilities 中存在未解释的 required unknown/false；
- raw 输出、token 或敏感绝对路径进入提交；
- 少于 5 个 Task commit，或 commit message 不匹配 Plan。

完成后只报告 Phase 0，不继续 Phase 1。
