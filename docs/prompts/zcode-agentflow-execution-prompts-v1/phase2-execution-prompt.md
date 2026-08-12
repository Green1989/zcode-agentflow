# Phase 2 执行 Prompt：Strict schemas + canonical bytes + IDs/roots + git primitives

你将只执行 **Phase 2**。本 Phase 的目的不是写工作流，而是先冻结所有结构化协议、规范字节、ID/路径边界、Manifest、Git primitive 和上下文不变量，使 Phase 3～6 不再边实现边修改契约。

## 入口

- 完整阅读 Plan 的 Execution Errata、Phase 2、Task 2.1～2.7。
- 复核 Phase 1 Exit 和提交；FastMCP 与版本必须稳定。


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


## Task 顺序和核心不变量

1. **2.1 Raw/Final 严格 Schema**
   - Raw：`spec-draft-output`、`plan-proposal`、`review-decision`，禁止 run_id/base_commit/hash/commit 等权威字段。
   - Engine 后续注入权威字段，再用 Final Schema 校验。
   - Plan必须恰好一个 Task；`additionalProperties:false` 深入所有嵌套对象。
   - Commit：`feat: strict schemas, planner-result wrapper, raw-vs-final split (P0-5, P0-8, §六.9, Prompt §6, parallel P0-4)`
2. **2.2 Canonical 全边界测试**
   - 只补充 Task 1.1b 的测试；不得新建第二套实现。
   - 覆盖 CRLF/CR、无/多末尾换行、UTF-8、非法 UTF-8、稳定 JSON。
   - Commit：`feat: canonical text/json bytes, strict UTF-8, single trailing newline (§六.1, Prompt §7)`
3. **2.3 Windows-safe IDs**
   - operation_id 可作为 Windows 文件名；禁止 `<>:"/\|?*`、`..`、路径分隔符；branch/dirname 规范化。
   - Commit：`feat: Windows-safe operation_id + branch/dirname normalization (P0-1, Prompt §5)`
4. **2.4 四根目录**
   - 规范绝对路径、路径逃逸检测、禁止不允许的嵌套；Windows路径可用。
   - Commit：`feat: four-roots model + containment (Spec §2.2)`
5. **2.5 Manifest**
   - `build_manifest`/staging 输出；Manifest 不含自己的 hash；不得直接写权威 control_root 文件。
   - Commit：`feat: self-reference-free task manifest (Prompt §8)`
6. **2.6 Git primitives**
   - 所有 Git命令集中在 `gitops.py`；使用参数数组；`--name-status -z` 正确处理空格、中文、引号、rename/delete。
   - 用真实临时 Git 仓库测试，禁止只 mock 字符串。
   - Commit：`feat: dedicated git primitives module (P0-2)`
7. **2.7 Context invariants**
   - Raw Schema 拒绝权威字段；Final对象的 run_id/goal/base_commit/plan_version 来自 State。
   - 单 Task 为 T001、dependencies 空、路径相对且无逃逸、verification 受结构化 allowlist约束。
   - Commit：`feat: model-output context invariants; untrusted base_commit rejected (P1-3)`

## 禁止越界

- 不实现 Store、Operation Lease、Adapter 或 Engine。
- 不信任模型提供 hash/commit；也不要“覆盖后再假装验证原字段”。
- 不把 Markdown AGENTS.md 当作安全 allowlist 权威来源。
- 不直接创建权威 SPEC/PLAN/review 文件。

## Phase Exit 复核

运行 Phase 2 全部专项测试，并至少运行 `python -m pytest tests/unit -v`。逐项证明：Raw/Final 分层、单 Task、canonical唯一实现、Windows-safe ID、四根目录、无自哈希 Manifest、Git特殊文件名、上下文不变量。报告后停止。
