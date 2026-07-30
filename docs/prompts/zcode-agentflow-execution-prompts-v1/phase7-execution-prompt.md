# Phase 7 执行 Prompt：Hooks + plugin packaging

你将只执行 **Phase 7**。目标是依据 Phase 0 的真实 ZCode contract 完成 Hook、Plugin manifest、mandatory confirmation配置、Skill和Commands；不得基于记忆猜测字段。

## 入口

- 阅读 Plan Phase 7 Task 7.1～7.2。
- 读取 `docs/capabilities.json` 中实际 manifest、Hook event/payload/cwd/exit/feedback、confirmation field/scope。
- Engine `check_stop` 和9 tools必须已完成。


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


## Task 7.1 — Hooks

- Hook脚本只按真实payload解析，不支持的字段不得臆造。
- Stop Hook调用唯一的只读 `engine.check_stop(run_id)`；不得复制一套较弱判断。
- protect Hook覆盖真实权威控制路径和 Artifact generation区域，但必须符合v1威胁模型，不宣称防同权限恶意进程。
- 所有日志stderr/脱敏；错误反馈使用spike确认的通道。
- Commit：`feat: hooks on real spike contract + shared check_stop (Prompt §18, Review §六.6/§六.7)`

## Task 7.2 — Packaging

- `.zcode-plugin/plugin.json` 使用真实manifest shape和MCP command。
- `workflow_approve_spec`实际配置mandatory confirmation；验证只该工具弹窗。若仅全局确认或不支持，按capabilities/config明确降级或BLOCKED，不能伪装。
- Skill/commands明确9-tool顺序、两道人工作用门、ALLOWED_FROM、READY_TO_MERGE终态、禁止push/merge。
- `abandon/archive`写成本地admin命令，不注册成MCP tool。
- Commit：`feat: plugin manifest with confirmation on approve_spec + skill/commands (P1-1)`

## 人工操作原则

当必须在ZCode UI加载插件或验证弹窗时：先完成文件和自动测试，再给用户最少步骤、预期现象和需要反馈的具体字段；不要提出宽泛问题，也不要在未观察成功前打勾。

## Phase Exit

运行 Hook逻辑、packaging和MCP回归测试；在真实ZCode中完成一次插件加载、tools/list、普通工具无确认、approve_spec有确认、Stop gate行为验证。提交脱敏证据摘要，报告后停止。
