# AGENTS.md — ZCode-AgentFlow

本文件是本仓库的 workspace 级指令，由 ZCode 在每个会话注入上下文。优先级低于用户级
`~/.zcode/AGENTS.md`，但覆盖其中的宽泛默认。**先读本文件，再读下方权威文档。**

| 项 | 值 |
|---|---|
| 项目 | **ZCode-AgentFlow** — ZCode 主控的 AgentFlow：ZCode(GLM-5.2) Supervisor+Executor、Codex(GPT) Planner、Grok(Grok-4.5) Reviewer，三者以版本化文件 + JSON Schema 交接 |
| 实现语言 | Python（src-layout 包 `zcode_agentflow`） |
| 部署形态 | 用户级插件，运行时 `target_repo` 传入，**不绑定特定业务仓库** |
| 当前阶段 | **v1 已实现并通过测试**（Phase 0–8 全部完成）。`src/zcode_agentflow/` 生产源码、9-tool MCP server、状态机、Codex/Grok adapters、hooks 均已落地；默认测试套件全绿。外部接口契约以 `docs/capabilities.json` 真实证据为准（mcp SDK 已升级到 1.29.0）。 |
| 主目标平台 | Windows / Git Bash |
| 人工门（v1） | SPEC 批准 + 最终合并（两道）；其余 FAIL/未授权/超限 → `NEEDS_HUMAN` |
| 终态（v1） | `READY_TO_MERGE`（合并在 AgentFlow 外手工完成，v1 不做合并动作） |

---

## 1. 当前仓库状态（务必先认清，避免假设不存在的东西）

- **v1 已实现并通过测试**：`src/zcode_agentflow/` 生产源码（engine / store / gitops /
  server / adapters 等）、`pyproject.toml`、`config.yaml`、9-tool MCP server、状态机
  （NEW → READY_TO_MERGE）、Codex/Grok adapters、两个 hooks 均已落地。默认测试套件
  全绿（`python -m pytest -q`）。
- **外部接口契约已固化并升级**：`docs/capabilities.json`（外部接口的唯一事实来源，
  见 §5）+ `requirements.lock`（已 OSV 全量重扫，39 个包零公告，mcp SDK 1.29.0）。
  外部接口（ZCode MCP/Hook、Codex、Grok）以真实运行证据为准。
- **仓库含文档 + 实现源码**：`docs/` 下 Specs / Plan / Review / 分阶段执行 Prompt；
  `docs/capabilities.json` + `requirements.lock` 是 Phase 0 产物。
- **执行入口**：通过 `docs/prompts/zcode-agentflow-execution-prompts-v1/` 的分阶段 Prompt
  逐 Phase 执行（见 §2、§3）。Phase 0 已过，下一入口是 Phase 1。

> 不要把"计划里写的合约"当成"已实现的代码"。合约用 `[PSEUDO]`/伪代码标注，实现要等执行 Phase 才写。Phase 0 已验证的接口以 `docs/capabilities.json` 为准。

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
工具级强制人工确认是否可用——**全部以 `docs/capabilities.json` 为准（Phase 0 已产出并提交）**。

- **`docs/capabilities.json` 已生成**（Phase 0 完成）：它的真实发现**优先于模型记忆、通用文档、Plan 中的示例**。
  与之冲突时，以它为准，并据此回头核对 Plan/Spec 是否需要修订（走 review，不私自改 Plan）。
- **关键已验证结论速览**（详见 capabilities.json，权威以文件为准）：
  - MCP：官方 `from mcp.server.fastmcp import FastMCP`（版本 1.8.1），stdio；config-file MCP server 用绝对路径（不展开 `${...}`），plugin MCP server 才展开模板。
  - Grok：**子进程必须显式注入 `HTTPS_PROXY`**（不读 `~/.grok/.env`）；`--cwd` 须绝对路径；`--json-schema` 只接内联 JSON。详见 G7/G8。
  - ZCode Hook：7 事件、stdin 双键（snake+camel）、`PermissionRequest` 在 yolo 模式不触发。详见 G9/G10。
  - Codex：stdin prompt / `-s read-only` / `-C repo` / `--output-schema` / `model_reasoning_effort=high` 均可用。
- `requirements.lock`：依赖版本已在 Phase 0 探测后冻结；安装用 `pip install -r requirements.lock && pip install -e . --no-deps`，避免拉到不同版本导致已验证行为漂移。
- **Spike 代码、raw 输出、token、认证文件、本机敏感绝对路径绝不进入提交**；临时产物放仓库外的 control_root（Phase 0 的 spike temp 已清理）。

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
| G7 | **Grok 子进程必须显式注入代理**（Phase 0 实测） | Grok CLI（Rust/reqwest）走**标准系统代理环境变量** `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`，但 grok **不会读取 `~/.grok/.env`**（它不是 codex；grok 的 Config Sources 只有 `config.toml` + OS 环境变量）。**需要出网代理的环境**（如内网经 Clash Verge 等本地代理），**Phase 4 `GrokAdapter` 启动子进程时必须在 `env` 里显式传这些代理变量**，否则调用会无限挂起；代理值来自用户在 `config.yaml` 的 `reviewer.proxy` 配置（**不随发布硬编码任何固定地址**）。codex 不受影响（`~/.codex/.env` 自动注入）。注意 grok 的 `proxy_endpoint`/`GROK_WEB_FETCH_PROXY`/`GROK_CLI_CHAT_PROXY_BASE_URL` 是**应用层 API 网关代理**，不是系统网络代理，不能用它们解决出网。 |
| G8 | **Grok `--cwd` 必须是绝对 Windows 路径；`--json-schema` 只接内联 JSON**（Phase 0 实测） | `grok -p` 的 `--cwd` 给相对路径会直接报 `Error: 系统找不到指定的文件。 (os error 2)`；Adapter 须传绝对路径（如 `C:\...\bundle`）。`--json-schema` 接收**内联 JSON 字符串**，**不支持 `@file` 语法**（`--json-schema @schema.json` → `invalid JSON` 报错）；正确写法 `--json-schema "$(cat schema.json)"`。结构化结果在 stdout envelope 的 `structuredOutput` 字段（已解析对象），权威于 `text` 字段里的 JSON。 |
| G9 | **ZCode hook stdin 是双键结构；工具确认非 GUI 开关**（Phase 0 实测） | ZCode 7 个 hook 事件的 stdin **同时提供 snake_case + camelCase 两份键**（`hook_event_name`/`hookEventName`、`tool_name`/`toolName`、`tool_input`/`toolInput`…），解析时任取其一即可。`PreToolUse` 还带 ZCode 自动算的 `riskLevel` + `sideEffectScope`。**工具确认不是 GUI 开关**，而是 `~/.claude/settings.json` 的 `permissions.defaultMode`（GUI 显示"完全访问/变更前确认"，内部值 `yolo`/非yolo）+ `allow`/`deny` 规则（如 `Bash(awk:*)`）。`PermissionRequest` 事件在 `yolo` 模式下**不触发**；要让它触发须切到非 yolo。 |
| G10 | **ZCode 主会话 hook runner 与 exec 子会话分离**（Phase 0 实测，探测教训） | ZCode 的 hook runner 跑在**交互主会话**；而 Agent 执行 Bash 工具的子进程是 **exec 子会话**（`~/.zcode/cli/exec/sess_<id>/`），**没有 hook runner**——从 exec 子会话里跑命令不会产生任何 hook 日志，也无法触发主会话的 `PermissionRequest`。探测/验证 hook 行为时，必须在主会话内闭环，不能指望 exec 子会话观察到 hook。Phase 7 写生产 hook 时同理：hook 的执行主体是 ZCode 主进程，不是 Adapter 的子进程。 |
| G11 | **生产 server/hooks 用 `bootstrap.process_config()` 解析上下文，不是 `ZCODE_PROJECT_DIR`，也不是 `AGENTFLOW_RUN_CONTEXT`**（Phase 7/8 审计修复，2026-07-31） | Plan v2.2 对 `user_data_root` 的**生产来源**保持沉默（Task 3.8 声明 `resume_active_run(user_data_root)` 但不说它从哪来）。早期实现只有 `AGENTFLOW_RUN_CONTEXT`（其 docstring 自承"test/subprocess path"）一条路——生产插件 manifest 不设它，于是真实 ZCode 启动时每个状态变更工具报 `no run context configured`；hook 又把 `ZCODE_PROJECT_DIR`（**项目目录**，见 capabilities.json `hook_cwd`）当成 `active-run.json` 的家，而真指针在 `<user_data_root>/active-run.json`，导致 protect/stop **fail-open**。**正确做法**（Plan erratum 9–12）：`bootstrap.process_config()` 从 `ZCODE_PLUGIN_ROOT`/`AGENTFLOW_USER_DATA_ROOT`(默认 `<plugin_root>/../agentflow-data`)/`AGENTFLOW_CONFIG`/`AGENTFLOW_CAPABILITIES` 解析 `RunContext`；`build_run_context()` 单一派发（`AGENTFLOW_RUN_CONTEXT` 在则走 test 路径，否则走生产路径）。server.py 与两个 hook 都调 `build_run_context()`，**绝不**直接调 `load_run_context()`，**绝不**用 `ZCODE_PROJECT_DIR` 当指针家。详见 `docs/review/2026-07-31-phase7-8-audit-fix-notes.md`。 |

> **Phase 0 已完成（5 个 spike commit，详见 `docs/capabilities.json` + `requirements.lock`）。** 以上 G7–G10 均有 Phase 0 真实运行证据支撑（非模型记忆），以 `docs/capabilities.json` 为唯一外部事实来源；与之冲突时以 capabilities.json 为准。唯一诚实缺口：`PermissionRequest` hook 事件的**真实 stdin payload** 未捕获（G10 所述架构限制；事件存在性 + 同源 stdin 结构已确认，不阻塞 Phase 7）。**G11** 来自 Phase 7/8 审计修复（2026-07-31），其根因与证据见 `docs/review/2026-07-31-phase7-8-audit-fix-notes.md` 与 Plan v2.2 的 `Execution Errata — audit-fix 2026-07-31`（errata 9–13）。

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
