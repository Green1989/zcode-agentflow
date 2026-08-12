# Phase 1 执行 Prompt：Project skeleton + real MCP stdio + active-run pointer

你将只执行 Implementation Plan 的 **Phase 1**。目标是建立可安装的 `zcode_agentflow` 包、真实 FastMCP stdio transport、严格配置、canonical 基础、ActiveRunRegistry primitives，并锁定 9-tool API surface；不要实现 Phase 6 的业务逻辑。

## 强制阅读与入口检查

- Plan：`<repo-root>\docs\superpowers\plans\2026-07-30-zcode-agentflow-v1-v2-2.md` 的 `Execution Errata`、Phase 1、Task 1.1、1.2、1.1b、1.3a、1.4、1.5、1.6。
- 核对 Phase 0 的 `docs/capabilities.json`、`requirements.lock` 和 5 个提交。
- required gate 不是 true 时停止；fallback 只能按 config 明确降级。


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


## 本 Phase 的严格任务顺序

1. Task 1.1 — src-layout、pyproject、lock-driven install。
   - 禁止创建本地 `mcp/` 包；官方 SDK 必须来自已锁版本。
   - Verification：`python -m pip install -r requirements.lock && python -m pip install -e . --no-deps && python -m pytest tests/unit/test_packaging.py -v`
   - Commit：`build: src-layout package, lock-driven install, pinned deps (P0-1, P1-7, P1-3)`
2. Task 1.2 — 生产 `config.yaml` 与 JSON Schema 校验。
   - 不得仅 deep merge；unknown keys 和错误类型必须拒绝；network 只能 advisory。
   - Commit：`feat: production config.yaml + validated config loader (P0-2)`
3. Task 1.1b — canonical 基础模块。
   - 项目只有这一套 canonicalization；Task 2.2 只补边界测试，不得另写第二实现。
   - Commit：`feat: canonical-bytes base module (errata: before pointer/config/store)`
4. Task 1.3a — ActiveRunRegistry primitives + Registry Lock。
   - 只做 primitives；不得实现 Store.recover、完整 workflow_init 或 READY_TO_MERGE 生命周期。
   - `ActiveRunRegistry.locked()` 是唯一锁拥有者；`*_locked` 方法不得再次加锁。
   - Commit：`feat: active-run pointer primitives + registry lock (P0-1, P0-4)`
5. Task 1.4 — startup capability validation。
   - required fail-fast；optional 不阻塞；fallback-capable 按配置；unknown 阻塞。
   - Commit：`feat: startup capability validation with classified gates (P1-1, P1-2)`
6. Task 1.5 — 真实 FastMCP stdio + workflow_status。
   - 使用 Phase 0 记录的实际 SDK import 和 framing；stdout 只能有 MCP frames，日志全部 stderr。
   - Commit：`feat: real FastMCP stdio server, workflow_status, clean stdout (P0-2, P1-6)`
7. Task 1.6 — 声明其余 8 个工具签名。
   - 只锁定名称和 input schema；不得提前实现 Engine 逻辑；MCP surface 必须恰好 9 个。
   - Commit：`feat: declare all 9 MCP tool signatures (logic deferred to Phase 6)`

## 禁止越界

- 不实现 Store 两阶段事务、ArtifactResolver、Adapter、Approval、Engine。
- 不把 repo 绑定到环境变量；`target_repo` 必须保留运行时参数。
- 不引入未经 Phase 0 验证的新 MCP/Plugin 假设。
- 不用 `print()` 污染 stdio。

## Phase Exit 验证

完成后至少运行本 Phase 全部专项测试和一次组合回归：

```bash
python -m pytest   tests/unit/test_packaging.py   tests/unit/test_config.py   tests/unit/test_canonical_smoke.py   tests/unit/test_active_run_primitives.py   tests/unit/test_validate_capabilities.py   tests/mcp/test_stdio_protocol.py -v
```

确认：官方 `mcp` 无 shadow、9 tools 恰好、stdout 干净、required gate fail-fast、工作区仅含预期提交。报告后停止。
