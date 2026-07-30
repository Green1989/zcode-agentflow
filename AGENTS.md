# AGENTS.md — ZCode-AgentFlow

本文件是本仓库的 workspace 级指令，由 ZCode 在每个会话注入上下文。优先级低于用户级
`~/.zcode/AGENTS.md`，但覆盖其中的宽泛默认。**先读本文件，再读下方权威文档。**

| 项 | 值 |
|---|---|
| 项目 | **ZCode-AgentFlow** — ZCode 主控的 AgentFlow：ZCode(GLM-5.2) Supervisor+Executor、Codex(GPT) Planner、Grok(Grok-4.5) Reviewer，三者以版本化文件 + JSON Schema 交接 |
| 实现语言 | Python（src-layout 包 `zcode_agentflow`） |
| 部署形态 | 用户级插件，运行时 `target_repo` 传入，**不绑定特定业务仓库** |
| 当前阶段 | **设计/计划已定稿，实现尚未开始**（仓库现为 docs-only；Phase 0 真实能力探测尚未执行） |
| 主目标平台 | Windows / Git Bash |
| 人工门（v1） | SPEC 批准 + 最终合并（两道）；其余 FAIL/未授权/超限 → `NEEDS_HUMAN` |
| 终态（v1） | `READY_TO_MERGE`（合并在 AgentFlow 外手工完成，v1 不做合并动作） |

---

## 1. 当前仓库状态（务必先认清，避免假设不存在的东西）

- **尚无源码**：没有 `src/zcode_agentflow/`、`pyproject.toml`、`config.yaml`、`requirements*.txt`。
  这些都属于 Phase 1+ 产物。不要假设它们已存在或可 import。
- **仓库目前只含文档**：`docs/` 下的 Specs / Plan / Review / 分阶段执行 Prompt。
- **`docs/capabilities.json` 尚未生成**：它是 Phase 0 的真实环境能力探测结果，是**外部
  接口契约的唯一事实来源**（见 §5）。在它出现之前，任何关于 ZCode/Codex/Grok/MCP SDK 真实
  字段、flag、行为的断言都只是**猜测**，必须标注为未验证。
- **执行入口**：通过 `docs/prompts/zcode-agentflow-execution-prompts-v1/` 的分阶段 Prompt
  逐 Phase 执行（见 §2、§3）。

> 不要把"计划里写的合约"当成"已实现的代码"。合约用 `[PSEUDO]`/伪代码标注，实现要等执行 Phase 才写。

---

## 2. 权威文档索引（按此顺序读取事实）

| 文档 | 路径 | 角色 |
|---|---|---|
| **Specs** | `docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md` | 架构事实来源（状态机、四根目录、威胁模型、9 个 MCP 工具、数据协议、配置） |
| **Plan v2.2** | `docs/superpowers/plans/2026-07-30-zcode-agentflow-v1-v2-2.md` | **唯一执行依据**。状态 `APPROVED_FOR_EXECUTION`。开头的 `Execution Errata` 已内联应用 |
| Plan 历史归档 | `docs/superpowers/plans/...-v1.md` / `-v1-v2.md` / `-v1-v2-1.md` | 仅参考演进，**不再执行** |
| Review | `docs/review/*.md` | 已采纳/已拒绝的评审意见，历史溯源 |
| 执行 Prompt 包 | `docs/prompts/zcode-agentflow-execution-prompts-v1/` | 逐 Phase 复制给执行 Agent 的指令（Phase 0→8 + 复核 Prompt） |
| Background | `docs/background/background.md` | 原始问题陈述与需求来源 |

冲突优先级：**真实运行结果（`capabilities.json`）> Plan 的 Errata > Plan 正文 > Specs > 历史 Plan**。

---

## 3. 工作纪律（适用于本仓库的每一个会话）

以下规则在所有 Phase Prompt 中重复出现，是本项目的硬约束，**任何会话都不得违反**：

1. **唯一执行依据**：Plan v2.2（含 Errata）。Specs 只作架构参考。不要凭记忆实现。
2. **只执行当前 Phase**：不得提前实现后续 Phase、不得顺手重构无关代码、不得修改 Spec/Plan/历史 Plan/任何 Review。
3. **TDD 强制**：先写/补测试，运行确认它因目标行为缺失而失败（RED）；再写最小实现；再让专项测试变绿（GREEN）。**禁止先写实现再补"配合实现"的测试。**
4. **一个 Task 一个 Commit**：专项测试绿、范围复核过之后，用 Plan 指定的**确切 commit message** 提交。不得合并多个 Task，不得 amend 或重写用户已有提交（除非用户明确要求）。
5. **先检查再行动**：动手前先 `git status --short --branch` + 最近提交 + 上一 Phase 的 Exit 证据；工作区不干净时先说明变更归属，**不得覆盖或丢弃用户改动**。
6. **双层复核**：每个 Task 完成后先自检，再用独立 review subagent 只读复查（是否满足 Task、测试是否有效、是否越界、是否破坏既有不变量）；发现问题先修复重跑再提交。
7. **报告必须可核验**：所有"通过"必须附实际命令与结果摘要；不泄漏 token、账号、本机敏感绝对路径或未经脱敏的 raw 输出。
8. **失败不掩盖**：测试失败时定位根因，**不得删除/弱化测试换取绿灯**。发现必需能力缺失、Plan 与真实环境重大冲突、或需破坏性操作时，停在安全状态并报告证据；`false`/`unknown` 如实记录，不伪造。

---

## 4. Git 与提交策略

- **只做本地 commit。禁止 `git push`、自动 merge、自动建 PR、`git reset --hard`、删除用户分支、破坏性清理**——除非用户明确授权。
- 当前分支为 `main`，远程 `origin/main` 已同步。本地提交不自动推送是项目策略（Plan/Errata 的后续修订均为本地提交）。
- commit message 必须与 Plan 任务中给定的一致（执行 Phase 时），或遵循 `docs:`/`spike:`/`feat:` 等前缀约定（文档/探测阶段）。
- 默认分支 `main`：按 ZCode 规范，**在默认分支上不要直接开始大块实现**；实现阶段应在用户要求时先开分支。

---

## 5. 外部能力事实来源（capabilities.json 优先）

ZCode / Codex CLI / Grok CLI / MCP SDK 的真实 flag、字段、payload、退出码、Hook 合同、
工具级强制人工确认是否可用——**全部以 Phase 0 产出的 `docs/capabilities.json` 为准**。

- `docs/capabilities.json` 生成前：不得假设任何外部接口；如必须涉及，标注"待 Phase 0 验证"。
- `docs/capabilities.json` 生成后：**它的真实发现优先于模型记忆、通用文档、Plan 中的示例**。
  与之冲突时，以它为准，并据此回头核对 Plan/Spec 是否需要修订（走 review，不私自改 Plan）。
- Phase 0 还会产出 `requirements.lock`：依赖版本在探测后冻结，避免 `pip install -e .` 拉到不同版本导致已验证行为漂移。
- **Spike 代码、raw 输出、token、认证文件、本机敏感绝对路径绝不进入提交**；临时产物放仓库外的 control_root。

---

## 6. 已知陷阱（Gotchas）

| # | 陷阱 | 说明 / 处理 |
|---|---|---|
| G1 | **不要重新引入 broad `prompts/` 忽略规则**（已修复，勿回退） | 曾有一条 `prompts/` 规则过宽，会同时吞掉运行时插件的 `prompts/` 和 `docs/prompts/`，导致整个执行 Prompt 包**在磁盘上存在却既未跟踪、又被忽略**（已删除该规则修复）。prompts 在本项目是**源文件**（Task 4.0 的 `prompts/*.md` 模板、`docs/prompts/`），不是运行时产物——运行时 SPEC.md/PLAN.md 落在 repo 外的 `control_root`，本就不进仓库。不要为"清理"重新加回 `prompts/` 或 `/prompts/`，否则 Task 4.0 模板提交不了。 |
| G2 | **包名陷阱** | 严禁用本地 `mcp/` 作为包名——它会 shadow 官方 MCP SDK。包必须是 `src/zcode_agentflow`。 |
| G3 | **Windows 非法字符** | operation_id 含 `:` 等在 Windows 不能做目录名。ID 正则 `^[A-Za-z0-9._-]+$`，分隔用 `__`（如 `<run_id>__draft-spec__r4__a2`）。 |
| G4 | **MCP stdout 是协议通道** | FastMCP stdio 下任何 `print()` / `logging` 到 stdout / 子进程 stdout 直通 / traceback 都会破坏 framing。日志走 stderr，子进程 stdout 必须 capture。 |
| G5 | **Codex stdin / cwd / 输出分离** | Codex 的 prompt 经 stdin 输入、`cwd=repo`、read-only 生效、schema 输出写到独立 operation 目录；不要把 prompt 拼进命令行或让 cwd 指向 control_root。 |
| G6 | **诚实降级** | 批准机制：`allow_non_adversarial_fallback: false`（默认）→ 不支持工具级强制确认时**启动失败**而非静默降级；`true` 才降级并明确提示保障等级降低。威胁模型：MCP 只提供**流程完整性**，不防同权限恶意进程篡改。 |

---

## 7. 代码组织约定（实现阶段开始后适用）

- 包结构按 Plan 的 `File ownership` 与 `§5.7 目录结构` 落地：`src/zcode_agentflow/` 下 `server.py` / `engine.py` / `store.py` / `gitops.py` / `hashes.py` / `schemas.py` / `adapters/` 等。
- **四根目录**（Spec §2.2）：`plugin_root`(只读) · `target_repo_root`(Git) · `run_worktree_root`(读写代码，repo 外) · `control_root`(状态/产物，repo 外)。所有路径在 `state.json` 存规范化绝对路径，MCP 文件操作做根界定与逃逸检测。
- **唯一状态提交点**：`state.json.artifact_generation` 是产物指针；`state.json` 由 `finish_operation` 内单次 `os.replace` 原子替换（含临时文件 + flush）。`prepare_generation` 只写 blob/manifest 并填 next_state 字段，**不替换 state.json**。
- **两阶段事务**（避免嵌套锁死锁）：`begin_operation`（短锁：校验+CAS+中间态+lease）→ 外部 CLI（不持锁）→ `finish_operation`/`fail_operation`/`mark_unknown`（重锁+lease/revision 校验+单次 state 替换）。
- **MCP 工具固定 9 个**（Spec §5.1）：`workflow_status` / `workflow_init` / `planner_draft_spec` / `workflow_approve_spec` / `planner_create_plan` / `workflow_start_task` / `workflow_verify_task` / `reviewer_review_task` / `workflow_complete_task`。`abandon`/`archive` 是本地 admin 命令，**不是 MCP 工具**。
- **hash 规范化**（Spec §4.7）：MCP 对最终落盘字节重算（UTF-8 / LF / 单末尾换行 / SHA-256 hex 小写）；模型输出的 hash 仅诊断用，不参与校验。
- **raw vs final schema**：模型只 emit raw；Engine 注入权威字段后再校验 final envelope；禁止"先写后校验"。

---

## 8. 执行 Prompt 包使用方式（Phase 0→8）

- 位置：`docs/prompts/zcode-agentflow-execution-prompts-v1/`（`00-使用说明.md` 有完整说明与清单）。
- **每个 Phase 用一个新会话更稳妥**：把对应 `phaseN-execution-prompt.md` 全文复制给执行 Agent；它报告 Phase Exit 全满足、附提交与测试证据后，再复制下一 Phase Prompt。
- 质量门：Phase 0（能力探测）优先级最高；Phase 3（持久化/恢复）、Phase 6（状态机）、Phase 8（最终验收）为关键门。建议在 Phase 0/3/6/8 完成后用 `phase-completion-review-prompt.md` 交另一 Agent 做只读复核。
- **不要一次把 9 个 Phase 交给同一 Agent 无人值守执行。**
- Prompt 中的 Windows 路径沿用当前仓库配置；仓库迁移后先统一替换路径再使用。

---

## 附：开新会话时的第一步

1. `git status --short --branch` + `git log --oneline -5`，确认当前分支与最近提交。
2. 确认 `docs/capabilities.json` 是否已存在（决定外部接口是否已验证）。
3. 按本次任务定位权威文档（§2）：架构查 Specs，执行查 Plan v2.2 的对应 Phase。
4. 若任务是执行某 Phase：完整读该 `phaseN-execution-prompt.md` + Plan 中对应 Phase 的 Entry/Exit 与全部 Task。
5. 任何"外部接口已支持 X"的断言，若 `capabilities.json` 无证据，视为未验证。
