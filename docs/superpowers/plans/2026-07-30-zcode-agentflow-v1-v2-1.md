# ZCode-AgentFlow v1 Implementation Plan v2.1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a safe-to-execute plan that implements the Spec v3.1 v1 vertical slice: a single-task run driven from `NEW` to `READY_TO_MERGE` by a real MCP stdio server, with Codex planning, Grok reviewing, and two human gates.

**Architecture:** A Python package `zcode_agentflow` (src layout) exposing 9 MCP tools over FastMCP stdio. An `Engine` holds the workflow logic; a `Store` with a per-Run file lock holds recoverable state. External calls (Codex/Grok/verification) run **between** two short locked transactions (`begin_operation` → external → `finish_operation`), so the Run lock is never held across minutes-long CLI calls. Three adapters run subprocess calls with raw capture. All artifacts are promoted atomically (generation-dir + single pointer). No code is pre-written production logic — code blocks here are interface contracts, data structures, and key algorithm pseudocode only.

**Tech Stack:** Python 3.11+, `mcp` SDK (`FastMCP`, stdio — exact import path from Phase 0 spike), `jsonschema`, `pyyaml`, `portalocker`, `pytest`, `pytest-mock`. Subprocess calls to `codex exec` and `grok -p`. Windows/Git Bash primary; must stay cross-platform (incl. `.bat`/`.cmd` verification commands).

**Reference spec:** `docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md` (Approved for planning, v3.1).
**First review:** `docs/review/2026-07-30-zcode-agentflow-review.md`.
**v2 plan:** `docs/superpowers/plans/2026-07-30-zcode-agentflow-v1-v2.md` (superseded by this v2.1; kept for trace).
**v2 review driving this revision:** `docs/review/2026-07-30-zcode-agentflow-v1-v2-review.md`.
**Do not modify:** the Spec, the v2 plan, or either review.

---

## v2.1 revision summary (vs v2)

All 6 P0 + 7 P1 + 1 clarification from the v2 review are addressed:

- **P0-1** operation_id is now Windows-filename-safe (`__` separators, regex `^[A-Za-z0-9._-]+$`), with an explicit safe-filename test.
- **P0-2** added dedicated tasks for `gitops.py` (Task 2.6), the three prompt templates (Task 4.0), and a production `config.yaml` + config validation (Task 1.2).
- **P0-3** eliminated the `_guard`+`commit_operation` nested-lock deadlock: external calls run between two short locked transactions (`begin_operation` / `finish_operation`); `_guard` no longer acquires the lock. This also surfaces the Spec's intermediate states (`DRAFTING_SPEC`/`VERIFYING_TASK`/`REVIEWING_TASK`).
- **P0-4** replaced "scan newest non-terminal run" with a persistent `active-run.json` pointer; corrupt state is recovered (not silently skipped); never auto-switches projects.
- **P0-5** added a `planner-result` wrapper schema (READY_FOR_EXECUTION / NEEDS_USER_INPUT / BLOCKED) so a multi-task goal fails cleanly at validation, not after; the landed `plan-envelope` stays strict single-task.
- **P0-6** reordered phases to a true topological order (approval & change policy moved before the engine; hooks after the engine).
- **P1-1** added `validate_capabilities(config)` at startup; tool-confirmation must be actually *configured* (not just "supported"), tested to pop for `workflow_approve_spec` only; fallback=false+unsupported ⇒ fail at startup.
- **P1-2** capability gate classifies `required` / `optional` / `fallback-capable`; `false` no longer blanket-blocks.
- **P1-3** added model-output context invariants (run_id/goal/base_commit/plan_version/task=T001/dependencies empty/relative paths/allowlist).
- **P1-4** multi-artifact promotion is atomic via a generation dir + single pointer switch; fault injection fires after each promotion.
- **P1-5** verification supports `executable` / `cmd_script` / `bash_script` kinds; `.bat`/`.cmd` work on Windows while the outer launcher stays `shell=False`.
- **P1-6** server stdout is MCP-only (assertion test); all logging → stderr; subprocess stdout always captured; MCP framing taken from the SDK, not assumed.
- **P1-7** Phase 0 records pinned versions and emits a lock file.
- **Clarification** operation replay uses a three-valued `replay_policy` (`SAFE_READ` / `COSTFUL_READ` / `UNSAFE_WRITE`); read-only Codex/Grok are `COSTFUL_READ` (config-driven replay).

---

## How to read this plan (IMPORTANT)

This plan is deliberately **not** a pile of copy-pasteable production code. Each task states: **Goal / Depends on / Files**, then **Tests first** (TDD), then **Implementation requirements** (invariants the code must satisfy), then **Verification / Expected / Done-when / Commit message**.

Code blocks are labeled and limited to three kinds:
- `[CONTRACT]` — an interface/type the implementation must conform to.
- `[DATA]` — a literal file's required content (schema, config sample, manifest).
- `[PSEUDO]` — algorithm pseudocode to guide, not to copy verbatim.

Any task depending on an external product capability requires that capability to be confirmed in the Phase 0 spike first. Unconfirmed capabilities are tracked in the risk appendix and gate their dependents.

---

## Phase roadmap (topological order)

| Phase | Title | Key resolves |
|---|---|---|
| 0 | Real Capability Spike | all external assumptions; pinned versions (P1-7) |
| 1 | Project skeleton + real MCP stdio | P0-2(config), P0-4, P1-1, P1-6 |
| 2 | Strict schemas + canonical bytes + IDs/roots + git primitives | P0-1, P0-2(gitops), P0-5, P1-3 |
| 3 | Run lock + Store + two-phase transactions + fault injection | P0-3, P1-4, clarification(replay_policy) |
| 4 | Adapters + prompt templates | P0-2(prompts), P1-5 |
| 5 | Approval + change policy + agent context | (moved before engine: P0-6) |
| 6 | Workflow Engine: 9 tools | engine consumes 3,4,5 |
| 7 | Hooks + packaging | needs engine `check_stop` |
| 8 | Three-layer E2E + docs + READY_TO_MERGE handoff | S4 |

Phases are in dependency order; an executing agent may proceed linearly. The dependency graph is in Appendix B for reference.

---

## File ownership

```
src/zcode_agentflow/                  ← the package (NOT mcp/ — avoids SDK name clash)
├── __init__.py                       version + public re-exports
├── config.py                         load+validate config.yaml + validate_capabilities (Spec §6, P1-1)
├── active_run.py                     active-run.json pointer (P0-4)
├── paths.py                          four-roots, normalization, containment (Spec §2.2)
├── ids.py                            run/operation/task id gen + validation + branch/dir normalize; Windows-safe (P0-1)
├── canonical.py                      canonical_text/json bytes, hashing, atomic write (Prompt §7)
├── schemas.py                        strict schema load + validate + cross-field + context invariants (Prompt §6, P1-3)
├── manifest.py                       self-reference-free task manifest (Prompt §8)
├── locks.py                          per-Run file lock (portalocker) (Prompt §9)
├── store.py                          Run lock + state/events/operations + begin/finish_operation + atomic promotion (P0-3, P1-4)
├── gitops.py                         git primitives incl. name_status_z, worktree_list, branch_exists (Task 2.6)
├── artifacts.py                      verify_locked_artifacts(state) rehash (Prompt §14)
├── adapters/
│   ├── __init__.py
│   ├── base.py                       Adapter protocol + subprocess + redaction + process-tree kill (Prompt §12,§20)
│   ├── codex.py                      CodexAdapter (stdin prompt, read-only) (Prompt §12)
│   ├── grok.py                       GrokAdapter (Review Bundle, read-only) (Prompt §13)
│   └── verification.py               VerificationAdapter (executable/cmd_script/bash_script kinds) (P1-5)
├── policy_change.py                  git change audit via name-status -z (Prompt §16)
├── approval.py                       trusted approval records + fallback warning + config-of-confirmation (P1-1, Prompt §17)
├── agents_context.py                 resolve AGENTS.md + template → control_root (Prompt §19)
├── redact.py                         secret redaction (Prompt §20)
├── engine.py                         Engine: 9 tools, ALLOWED_FROM, two-phase calls, rehash, exactly-one-task, check_stop
├── server.py                         FastMCP stdio: 9 tools, main(); stdout MCP-only (P1-6)
└── bootstrap.py                      process config + active-run pointer recovery (P0-4)

schemas/                              ← strict; additionalProperties:false
├── spec-envelope.schema.json
├── planner-result.schema.json        NEW wrapper: READY/NEEDS_USER_INPUT/BLOCKED (P0-5)
├── plan-envelope.schema.json         tasks minItems:1, maxItems:1
├── execution-result.schema.json
└── review-result.schema.json

prompts/                              ← plugin_root/prompts (Task 4.0)
├── planner-draft.md
├── planner-plan.md
└── reviewer.md

hooks/                                ← real shape from Phase 0 spike
├── <spike-confirmed>.json
├── protect_artifacts.py
└── gate_on_stop.py                   calls engine check_stop (Prompt §18)

tests/                                unit / adapters / store / mcp / e2e + spike
pyproject.toml                        src layout, deps; lock file after Phase 0 (P1-7)
config.yaml                           production config (Task 1.2)
docs/capabilities.json                Phase 0 spike output (versions + classified gates) (P1-2, P1-7)
.zcode-plugin/plugin.json             real manifest shape from Phase 0
skills/agentflow/SKILL.md
commands/agentflow-start.md, agentflow-status.md
AGENTS.md.template                    → resolved-agent-context.md at runtime
```

**Decomposition rule:** only `store.py` writes `state.json`/`events.jsonl`/`operations/`, and only inside a locked `begin_operation`/`finish_operation`. Adapters write only to operation staging; promotion is the Store's job. The Run lock is held only across short bookkeeping steps, **never** across an external CLI call.

---

## Phase entry/exit conditions

Every phase lists explicit **Entry** (what must already be true) and **Exit** (verifiable state that unlocks the next) conditions. A phase is not done until its Exit conditions hold.


## Phase 0 — Real Capability Spike

> **Purpose:** prove every external product assumption before any logic is written, and **pin versions**. Findings here override anything assumed elsewhere. This phase is exploration + a committed findings file, not production code.

**Entry:** local `codex` and `grok` CLIs installed and authenticated (ChatGPT/xAI account login); `git` works; a ZCode client available to load a test plugin.
**Exit (all must hold):**
1. Minimal MCP echo server loadable by ZCode; one tool callable over stdio.
2. Real ZCode plugin manifest format, MCP command field, hook event names/payload/cwd/exit-code captured.
3. Confirmed (true/false) whether ZCode supports **and how to configure** MCP tool-level mandatory human confirmation (not just "supported").
4. `codex exec` observed reading a prompt from stdin, cwd at a sample repo, writing JSON to an output dir.
5. `grok -p` observed running read-only against a sample Review Bundle, emitting parseable JSON; exact read-only flags recorded.
6. `docs/capabilities.json` committed with: a **versions** block (python, mcp SDK, portalocker, codex/grok CLI, zcode) (P1-7), and a **classified gates** block (P1-2). Spike scripts/raw outputs live in a temp control_root that is gitignored; only sanitized schemas/samples are committed.

### Task 0.1: Spike harness — minimal FastMCP echo server loadable by ZCode

**Goal:** discover the *actual* way ZCode loads an MCP server and calls a tool. Do **not** assume the SDK import path or manifest shape.

**Depends on:** nothing.
**Files:** throwaway `spike/echo/server.py` + `plugin.json` (temp control_root, not committed); incrementally fill `docs/capabilities.json`.

**Tests first (manual checklist → capabilities):**
- [ ] Launch the echo server directly; confirm it answers an MCP `initialize`.
- [ ] Point ZCode at it via the discovered manifest shape; confirm `echo(text)` returns `{"echo": text}`.
- [ ] Record exact manifest field names and the **real** `mcp` SDK import path.

**Implementation requirements:** `[PSEUDO]` smallest FastMCP server exposing `echo`, with `main()` + `if __name__=="__main__"`. Try `from mcp.server.fastmcp import FastMCP`; if the installed SDK differs, record the real path.

**Done when:** both observed; `docs/capabilities.json` `zcode` object records `plugin_loaded`, `mcp_stdio`, `manifest_fields`, `sdk_import_path`.
**Commit message:** `spike: minimal FastMCP echo server loadable by ZCode`

### Task 0.2: Spike — ZCode hook payload/cwd/exit + tool-confirmation *configuration*

**Goal:** capture the real hook contract, and learn not just *whether* tool-confirmation is supported but **how to enable it for a specific tool** (P1-1).

**Depends on:** Task 0.1.
**Tests first (manual checklist):**
- [ ] Hook writes `sys.argv`, `os.getcwd()`, full stdin bytes, env var *names* (redact values) to a temp log; trigger Edit + Stop; capture both.
- [ ] Configure a tool to require human confirmation (per the real field name); confirm ZCode enforces it for that tool only.
- [ ] Record: the confirmation config field name, and whether it's per-tool or global.

**Implementation requirements:** `[PSEUDO]` context-dumper hook; variants exiting non-zero to learn deny semantics; a variant probing the feedback channel.

**Done when:** `docs/capabilities.json` `zcode.hooks` records `events_observed`, `stdin_shape`, `hook_cwd` (how plugin root is reachable), `exit_code_denies`, `feedback_channel`, `stop_reentry_limit`; and `zcode.tool_confirmation` records `supported: bool`, `config_field: <name or null>`, `scope: per-tool|global`.
**Commit message:** `spike: capture ZCode hook contract + tool-confirmation configuration`

### Task 0.3: Spike — Codex stdin prompt, read-only, cwd, schema output

**Goal:** confirm Codex receives the prompt via stdin, runs read-only with cwd at the repo, writes schema JSON to a separate output dir.

**Depends on:** Task 0.1.
**Tests first (manual checklist):**
- [ ] Tiny sample git repo; invoke `codex exec` with prompt on **stdin**, read-only sandbox, cwd = repo, `--output-schema` + `-o <operation dir>`.
- [ ] Verify Codex output references repo contents (cwd+stdin work); repo unchanged (read-only held); output valid JSON.
- [ ] Record working flags; does `model_reasoning_effort=high` work for this account/model? (if not, note `medium` works).

**Implementation requirements:** `[PSEUDO]` one invocation with prompt piped to stdin; prompt/cwd/output dir are three distinct concepts (the Phase 4 contract).

**Done when:** `docs/capabilities.json` `codex` records `command`, `stdin_prompt_works`, `read_only_held`, `cwd_is_repo`, `output_schema_works`, `reasoning_effort_high_supported`, `working_flags`.
**Commit message:** `spike: codex stdin prompt + read-only cwd + schema output confirmed`

### Task 0.4: Spike — Grok read-only Review Bundle + JSON output

**Goal:** confirm Grok consumes a full Review Bundle read-only and emits parseable JSON; capture real read-only flags.

**Depends on:** Task 0.1.
**Tests first (manual checklist):**
- [ ] Assemble minimal bundle: SPEC.md, PLAN.md, task-manifest.json, execution-result.json, diff.patch, review-request.json, prompt.md.
- [ ] Invoke `grok -p` against it with the real read-only flags; verify JSON cites SPEC/PLAN content; bundle unchanged.

**Implementation requirements:** `[PSEUDO]` bundle layout is the Phase 4 `GrokAdapter` contract.

**Done when:** `docs/capabilities.json` `grok` records `command`, `readonly_flags`, `json_output_shape`, `bundle_consumed`, `cwd_vs_bundle_separated`.
**Commit message:** `spike: grok read-only Review Bundle + JSON output confirmed`

### Task 0.5: Pin versions, emit lock file, classify capability gates

**Goal:** freeze the versions the spike validated (P1-7) and make capability gating accurate (P1-2). No blanket "false blocks everything".

**Depends on:** Tasks 0.1–0.4.
**Files:** finalize `docs/capabilities.json`; extend `.gitignore` (spike temp + control_root patterns); create `requirements.lock` (or pip-tools/uv lock) capturing pinned versions.

**Implementation requirements:**
`[DATA]` `docs/capabilities.json` must contain:
- `versions`: `{python, mcp, portalocker, jsonschema, pyyaml, codex_cli, grok_cli, zcode}` — exact versions observed.
- `gates`: each capability tagged `required` | `optional` | `fallback-capable` with its current value and, for fallback-capable ones, the fallback. Example:
  ```json
  "gates": {
    "planner_ok":            {"value": true,  "class": "required"},
    "reviewer_ok":           {"value": true,  "class": "required"},
    "tool_confirmation":     {"value": false, "class": "fallback-capable", "fallback": "non_adversarial_convention"},
    "reasoning_effort_high": {"value": false, "class": "fallback-capable", "fallback": "medium"},
    "network_isolation":     {"value": false, "class": "optional",         "note": "v1 advisory-only; true isolation out of scope"}
  }
  ```
`[PSEUDO]` gate semantics (consumed by `validate_capabilities` in Phase 1): `required` && value==false ⇒ startup fail; `optional` ⇒ never blocks; `fallback-capable` && value==false ⇒ allowed only if its fallback is acceptable per config (e.g. `allow_non_adversarial_fallback`). `unknown` ⇒ block (must be resolved).

**Done when:** versions pinned in a lock file; gates classified; Phase 0 exit conditions hold; `git status` shows only sanitized files.
**Commit message:** `spike: pin versions (lock file) + classify capability gates`

---


## Phase 1 — Project skeleton + real MCP stdio + active-run pointer

**Entry:** Phase 0 exit conditions hold; `docs/capabilities.json` records the real `mcp` SDK import path, ZCode manifest fields, and **pinned versions**.
**Exit:**
1. `src/zcode_agentflow/` is an installed (editable) package; `from mcp.server.fastmcp import FastMCP` imports the **official** SDK; no local `mcp/` anywhere.
2. A real FastMCP server starts on stdio; integration test does `initialize` → `tools/list` → `tools/call`; server stdout is MCP-only (P1-6).
3. `workflow_init` accepts `target_repo` at runtime; run_id/control/worktree derived; **active-run pointer** drives recovery (P0-4).
4. `validate_capabilities(config)` runs at startup; required-missing fails fast, fallback-capable degrades per config (P1-1, P1-2).
5. Production `config.yaml` + config validation exist (P0-2).

### Task 1.1: src-layout package + pyproject + pinned deps (resolves Review P0-1; P1-7)

**Goal:** package under a name that cannot clash with the `mcp` SDK; deps pinned to Phase 0 versions.

**Depends on:** Phase 0.
**Files:** `pyproject.toml`, `src/zcode_agentflow/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, extend `.gitignore`; consume `requirements.lock` from Phase 0.

**Tests first (`tests/unit/test_packaging.py`):**
- `test_no_local_mcp_shadow()` — no directory `mcp` at repo root.
- `test_official_mcp_sdk_imports()` — `importlib.util.find_spec("mcp").origin` is under site-packages/installed dist, **not** under the repo.
- `test_package_imports()` — `from zcode_agentflow import __version__` == `"0.1.0"`.
- `test_fastmcp_imports()` — `from mcp.server.fastmcp import FastMCP` (or the path the spike recorded).
- `test_deps_match_lock()` — installed `mcp`/`portalocker` versions equal those in `docs/capabilities.json` `versions` (guards drift; P1-7).

**Implementation requirements:**
`[DATA]` `pyproject.toml`: setuptools build-system; `name="zcode-agentflow"`, `0.1.0`, `requires-python>=3.11`; `dependencies` use **compatible release** bounds anchored to the pinned versions (e.g. `mcp ~=1.2`, `portalocker ~=2.7`, `jsonschema>=4.21`, `pyyaml>=6.0`) and a committed `requirements.lock` pins exact versions; `[project.scripts] zcode-agentflow = "zcode_agentflow.server:main"`; `[tool.setuptools.packages.find] where=["src"]`; `[tool.pytest.ini_options] testpaths=["tests"], pythonpath=["src"]`.
`[DATA]` `tests/conftest.py`: fixtures `control_root`, `worktree_root`, `fake_repo` (real `git init`+commit), and `capabilities` loading `docs/capabilities.json`.

**Verification:** `python -m pip install -e . && python -m pytest tests/unit/test_packaging.py -v`
**Expected:** 5 passed.
**Commit message:** `build: src-layout package, pinned deps, lock file (P0-1, P1-7)`

### Task 1.2: Production config.yaml + config loading + validation (P0-2; Spec §6)

**Goal:** a real `config.yaml` and a loader that **validates** (not just deep-merges) — resolves the Review/v2-review gap of "config only deep-merged".

**Depends on:** Task 1.1.
**Files:** `config.yaml`, `src/zcode_agentflow/config.py`, `tests/unit/test_config.py`.

**Tests first (`tests/unit/test_config.py`):**
- `test_load_valid_config()` — production `config.yaml` loads; key fields present and typed.
- `test_unknown_top_level_key_rejected()` — validation rejects unknown keys.
- `test_invalid_type_rejected()` — e.g. `workflow.worktree: "yes"` (string, not bool) ⇒ rejected.
- `test_network_policy_is_advisory_or_absent()` — assert no `network: deny` pretending to work; if present it is `network_policy: advisory_only` (Review §六.3).
- `test_defaults_applied_for_missing_optional()` — optional keys get defaults.
- `test_capabilities_block_sourced_from_findings()` — the `capabilities` block mirrors `docs/capabilities.json` `gates`.

**Implementation requirements:**
`[DATA]` `config.yaml` (production): Spec §6 fields, with `human_gates.spec_approval.{required, method, allow_non_adversarial_fallback}`, `verification.command_policy.network_policy: advisory_only` (not a fake `deny`), and a `capabilities:` reference.
`[CONTRACT]`
```
# config.py
CONFIG_SCHEMA = {...}                      # jsonschema for the config shape
def load(path) -> dict                     # yaml.safe_load -> validate against CONFIG_SCHEMA -> merge defaults
def validate_capabilities(config, capabilities_findings) -> None   # see Task 1.4
```

**Verification:** `python -m pytest tests/unit/test_config.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: production config.yaml + validated config loader (P0-2)`

### Task 1.3: Active-run pointer (resolves P0-4)

**Goal:** replace "scan newest non-terminal run" with a persistent pointer; corrupt state is *recovered*, never silently skipped; never auto-switches projects.

**Depends on:** Task 1.1.
**Files:** `src/zcode_agentflow/active_run.py`, `src/zcode_agentflow/bootstrap.py`, `tests/unit/test_active_run.py`.

**Tests first (`tests/unit/test_active_run.py`):**
- `test_no_pointer_returns_none()` — fresh `user_data_root` ⇒ no active run.
- `test_init_writes_pointer_atomically()` — `workflow_init` writes `active-run.json` with `{run_id, control_root, target_repo_root, updated_at}` via atomic replace.
- `test_restart_resumes_pointer_run()` — after a simulated restart, recovery resumes exactly the pointer's run (not "newest").
- `test_second_init_rejected_while_active_non_terminal()` — a second `workflow_init` while the active run is non-terminal ⇒ rejected unless explicit `abandon`.
- `test_corrupt_state_is_recovered_not_skipped()` — pointer's `state.json` corrupt ⇒ `Store.recover()` is invoked (Phase 3) before resuming; if unrecoverable ⇒ `NEEDS_HUMAN` (never auto-switch to another run).
- `test_terminal_run_clears_pointer()` — reaching `READY_TO_MERGE` clears/archives the pointer.
- `test_scan_is_manual_tool_only()` — a `scan_runs()` helper exists for manual recovery but is **not** the default resume path.

**Implementation requirements:**
`[CONTRACT]`
```
# active_run.py
def read_pointer(user_data_root) -> dict | None
def set_pointer(user_data_root, run_id, control_root, target_repo_root) -> None   # atomic
def clear_pointer(user_data_root) -> None
def scan_runs(user_data_root) -> list      # manual recovery tool only
# bootstrap.py
def resume_active_run(user_data_root) -> RunPaths | None    # pointer-based; recover-on-corrupt; never auto-switch
```
`[PSEUDO]` pointer written with `canonical.write_canonical_json` + `os.replace`. Resume: read pointer → load state (if corrupt, `Store.recover`; if still corrupt → `NEEDS_HUMAN`).

**Verification:** `python -m pytest tests/unit/test_active_run.py -v`
**Expected:** 7 passed.
**Commit message:** `feat: active-run pointer replaces scan-newest (P0-4)`

### Task 1.4: validate_capabilities at startup (P1-1, P1-2)

**Goal:** fail fast when a required capability is missing; honor fallback-capable ones; and verify tool-confirmation is actually *configured*, not just "supported".

**Depends on:** Tasks 1.1, 1.2.
**Files:** extend `src/zcode_agentflow/config.py`; `tests/unit/test_validate_capabilities.py`.

**Tests first (`tests/unit/test_validate_capabilities.py`):**
- `test_required_missing_fails()` — `planner_ok` false & required ⇒ `CapabilityError`.
- `test_optional_false_allowed()` — `network_isolation` false & optional ⇒ no error.
- `test_fallback_capable_false_with_fallback_allowed()` — `tool_confirmation` false & config `allow_non_adversarial_fallback=true` ⇒ OK, `effective_approval_mode="non_adversarial_convention"`.
- `test_fallback_capable_false_without_fallback_fails()` — same but `allow_non_adversarial_fallback=false` ⇒ `CapabilityError` (Spec: fail at startup, not at approval time).
- `test_reasoning_effort_high_false_degrades_to_medium()` — effective effort becomes `medium`.
- `test_unknown_blocks()` — any `unknown` ⇒ error.

**Implementation requirements:** `[PSEUDO]` iterate `capabilities` gates; apply the class semantics from Task 0.5; produce an `effective_config` (resolved approval mode, resolved reasoning effort). This runs in `server.py` startup and before `workflow_init`.

**Verification:** `python -m pytest tests/unit/test_validate_capabilities.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: startup capability validation with classified gates (P1-1, P1-2)`

### Task 1.5: Real FastMCP stdio server + 1 live tool + clean stdout (Review P0-2; P1-6)

**Goal:** genuine FastMCP server over stdio; MCP protocol proven; stdout reserved for MCP frames only.

**Depends on:** Tasks 1.3, 1.4.
**Files:** `src/zcode_agentflow/server.py`, `tests/mcp/test_stdio_protocol.py`.

**Tests first (`tests/mcp/test_stdio_protocol.py`):** — real subprocess tests:
- `test_initialize_handshake()` — spawn server, JSON-RPC `initialize`, valid `InitializeResult`.
- `test_tools_list_contains_workflow_status()`.
- `test_tools_call_workflow_status()` — returns `{"active_run": ...}` (proves execution).
- `test_mcp_error_mapping()` — wrong-typed args ⇒ MCP error result, not a crash.
- `test_server_stdout_contains_only_mcp_frames()` (P1-6) — capture the child's stdout over a full session; assert every line parses as a JSON-RPC frame (no stray prints/tracebacks).
- `test_uses_official_sdk_framing()` — drive the server with the **official MCP SDK client** (not a hand-rolled newline-JSON assumption); if the SDK client is unavailable in test, document the framing used and assert it matches `docs/capabilities.json`.

**Implementation requirements:**
`[CONTRACT]`
```
# server.py
from mcp.server.fastmcp import FastMCP          # official path from capabilities
mcp_server = FastMCP("zcode-agentflow")
@mcp_server.tool()
def workflow_status() -> dict: ...               # active-run snapshot or {"active_run": None}
def get_engine() -> "Engine": ...                # Phase 6
def main() -> None:
    validate_capabilities(...)                   # P1-1: fail fast
    mcp_server.run(transport="stdio")
if __name__ == "__main__": main()
```
`[PSEUDO]` **All** logging configures handlers on **stderr** only; no `print`; FastMCP owns stdout. Subprocess adapters (Phase 4) capture child stdout (never inherit). MCP framing comes from the SDK, not assumed.

**Verification:** `python -m pytest tests/mcp/test_stdio_protocol.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: real FastMCP stdio server, workflow_status, clean stdout (P0-2, P1-6)`

### Task 1.6: Declare remaining 8 tool signatures (no logic)

**Goal:** lock the 9-tool surface + param schemas now; Phase 6 fills bodies.

**Depends on:** Task 1.5.
**Files:** extend `src/zcode_agentflow/server.py`, `tests/mcp/test_stdio_protocol.py`.

**Tests first:**
- `test_tools_list_has_all_nine()` — exactly the 9 names.
- Per-tool param-schema assertions incl. `expected_revision` where the spec requires CAS; `workflow_init(target_repo, goal, run_id=None)`; `planner_draft_spec(goal, answers=None, expected_revision=None)`.

**Implementation requirements:** `[CONTRACT]` signatures per Spec §5.1; bodies raise `NotImplementedError` until Phase 6.

**Verification:** `python -m pytest tests/mcp/test_stdio_protocol.py -v`
**Expected:** previous + new pass.
**Commit message:** `feat: declare all 9 MCP tool signatures (logic deferred to Phase 6)`

---


## Phase 2 — Strict schemas + canonical bytes + IDs/roots + git primitives

**Entry:** Phase 1 complete; FastMCP import path stable; versions pinned.
**Exit:**
1. Five schemas strict (`additionalProperties:false`, nested item schemas, `hash`=64 lowercase hex, `decision` enum, `issues` fully defined, `verification_commands` structured with `kind`, `tasks` minItems:1 **maxItems:1**); plus the new `planner-result` wrapper (P0-5).
2. `canonical_text_bytes`/`canonical_json_bytes`/`hash_file` pass the full edge suite.
3. `ids.py`: Windows-safe operation_id (P0-1); branch/dirname normalization; task_id strict.
4. `paths.py`: four-roots containment.
5. Task manifest has no self-hash.
6. **`gitops.py` exists as a dedicated, fully-tested module** (P0-2).
7. Model-output **context invariants** enforced (run_id/goal/base_commit/plan_version/task=T001/empty deps/relative paths/allowlist) (P1-3).

### Task 2.1: Strict schemas incl. planner-result wrapper (Review P0-8, §六.9; Prompt §6; P0-5)

**Goal:** author strict schemas; the `planner-result` wrapper lets a multi-task goal fail cleanly instead of after landing (P0-5).

**Depends on:** Phase 1.
**Files:** `schemas/{spec-envelope,planner-result,plan-envelope,execution-result,review-result}.schema.json`, `src/zcode_agentflow/schemas.py`, `tests/unit/test_schemas.py`.

**Tests first (`tests/unit/test_schemas.py`):** valid-pass + invalid-reject:
- *spec-envelope:* missing `spec_markdown` rejected; unknown field rejected.
- *planner-result (NEW):* `status=READY_FOR_EXECUTION` requires `plan` present & valid (1 task) & `questions` empty; `status=NEEDS_USER_INPUT` requires `plan=null` & ≥1 question; `status=BLOCKED` requires `plan=null` & non-empty `block_reason`; a 2-task `plan` inside the wrapper ⇒ wrapper invalid at validation (this is the P0-5 resolution: Codex can't smuggle multi-task through).
- *plan-envelope:* 0 tasks rejected, 2 tasks rejected; task missing `verification_commands` rejected; verification item missing `program`/`kind` rejected.
- *execution-result:* bad `status` enum rejected; `change_audit.dependency_files_changed` as string rejected (Review §六.9).
- *review-result:* PASS-with-issues rejected (invariant); FAIL-no-issues rejected; REPLAN without flag rejected; non-64-hex `spec_sha256` rejected; bad `issues[*].severity` rejected.

**Implementation requirements:**
`[DATA]` shared defs: `hash`=`^[a-f0-9]{64}$`; `commit`=`^[0-9a-f]{7,40}$`; `task_id`=`^T[0-9]{3}(-R[0-9]+)?$`.
`[DATA]` *planner-result* (NEW): top-level `status` enum `{READY_FOR_EXECUTION, NEEDS_USER_INPUT, BLOCKED}`, optional `plan` (a strict single-task plan-envelope), optional `questions[]`, optional `block_reason`. Conditional constraints as above (use `oneOf`/`if-then`). The **landed** `PLAN.md`/`plan-envelope.json` still only accepts READY+1-task (unchanged Spec protocol).
`[DATA]` *plan-envelope* task item requires: `id`,`title`,`dependencies`,`declared_files`,`verification_commands`(each `{kind:enum[executable,cmd_script,bash_script], program, args[], cwd, timeout_sec}` — P1-5),`acceptance_criteria`,`planned_changes`.
`[DATA]` *review-result* `issues[*]` requires `id,severity(enum),category(enum),file,line,description,required_action`. *execution-result* `change_audit` requires fixed-typed `deleted_files[], dependency_files_changed(bool), migrations_added[], undeclared_files_changed[]`.
`[CONTRACT]`
```
# schemas.py
def validate_against(obj, schema_name); validate_spec(e); validate_planner_result(w); validate_plan(e)
def validate_execution(obj); validate_review(obj)        # + cross-field invariants
```

**Verification:** `python -m pytest tests/unit/test_schemas.py -v`
**Expected:** all pass.
**Commit message:** `feat: strict schemas + planner-result wrapper (P0-5, P0-8, §六.9, Prompt §6)`

### Task 2.2: Canonical bytes (Review §六.1; Prompt §7)

**Goal:** one canonicalization used everywhere; hashing over actual written bytes.

**Depends:** Task 1.1.
**Files:** `src/zcode_agentflow/canonical.py`, `tests/unit/test_canonical.py`.

**Tests first (`tests/unit/test_canonical.py`):**
- `test_single_trailing_newline_added` — `b"body"`→`b"body\n"`.
- `test_multiple_trailing_newlines_collapsed` — `b"body\n\n\n"`→`b"body\n"`.
- `test_crlf_and_cr_to_lf`.
- `test_non_ascii_utf8_preserved`.
- `test_invalid_utf8_raises`.
- `test_pretty_vs_compact_json_not_assumed_equal`.
- `test_hash_file_matches_canonical_of_written_bytes`.

**Implementation requirements:**
`[CONTRACT]`
```
def canonical_text_bytes(raw) -> bytes      # utf-8 strict decode; \r\n|\r->\n; exactly one trailing \n
def canonical_json_bytes(obj) -> bytes      # dumps(ensure_ascii=False, sort_keys=True, separators=(",",":")) then canonical_text_bytes
def write_canonical_text(path, data); write_canonical_json(path, obj)
def sha256_hex(data) -> str; def hash_file(path) -> str   # always canonical
```

**Verification:** `python -m pytest tests/unit/test_canonical.py -v`
**Expected:** 7 passed.
**Commit message:** `feat: canonical text/json bytes, strict UTF-8, single trailing newline (§六.1, Prompt §7)`

### Task 2.3: Windows-safe IDs + branch/dirname normalization (Prompt §5; P0-1)

**Goal:** operation_id is a legal Windows filename; ids generated/validated by MCP; traversal impossible.

**Depends:** Task 1.1.
**Files:** `src/zcode_agentflow/ids.py`, `tests/unit/test_ids.py`.

**Tests first (`tests/unit/test_ids.py`):**
- `test_run_id_pattern` — `^\d{14}-[0-9a-f]{6}$`, ref-safe.
- `test_operation_id_is_windows_filename_safe` (P0-1) — `assert not any(c in op_id for c in '<>:"/\\|?*')` and `".." not in op_id`; matches `^[A-Za-z0-9._-]+$`.
- `test_operation_id_format` — uses `__` separators, e.g. `<run_id>__<tool>__r<rev>__a<attempt>`; encodes run/tool/revision/attempt; deterministic; different attempt ⇒ different id.
- `test_task_id_pattern_strict`.
- `test_branch_name_rejects_separators_and_dotdot`; valid run_id → `refs/heads/agentflow/<run_id>` (passes `git check-ref-format`).
- `test_dirname_rejects_separators`.
- `test_id_rejects_traversal_substrings`.

**Implementation requirements:**
`[CONTRACT]`
```
def new_run_id() -> str
def operation_id(*, run_id, tool, revision, attempt) -> str   # f"{run_id}__{tool}__r{revision}__a{attempt}"
def validate_task_id(s) -> str
def normalize_branch(prefix, run_id) -> str                    # may shell to git check-ref-format --branch
def normalize_dirname(run_id) -> str
```
`[PSEUDO]` reject `..`,`/`,`\`,`:`,NUL,control chars, spaces in dirnames. **No colon anywhere in operation_id** (P0-1).

**Verification:** `python -m pytest tests/unit/test_ids.py -v`
**Expected:** 7 passed.
**Commit message:** `feat: Windows-safe operation_id + branch/dirname normalization (P0-1, Prompt §5)`

### Task 2.4: Four-roots containment (Spec §2.2)

**Goal:** typed, absolute, mutually non-containing roots.

**Depends:** Tasks 2.2, 2.3.
**Files:** `src/zcode_agentflow/paths.py`, `tests/unit/test_paths.py`.

**Tests first (`tests/unit/test_paths.py`):**
- `test_roots_normalize_absolute_and_distinct` — mutually non-containing (Prompt §11).
- `test_is_within_true_for_subpath_false_for_escape` — incl. `..`.
- `test_worktree_and_control_outside_target_repo` (Prompt §4 layout).

**Implementation requirements:**
`[CONTRACT]`
```
@dataclass(frozen=True) class Roots: plugin_root; target_repo_root; run_worktree_root; control_root
def make_roots(**) -> Roots          # raise if any root contains another
def is_within(path, base) -> bool; def assert_within(path, base) -> None
```

**Verification:** `python -m pytest tests/unit/test_paths.py -v`
**Expected:** 3 passed.
**Commit message:** `feat: four-roots model + containment (Spec §2.2)`

### Task 2.5: Task manifest without self-reference (Prompt §8)

**Goal:** manifest hashed for review binding has no self-hash field.

**Depends:** Tasks 2.1, 2.2.
**Files:** `src/zcode_agentflow/manifest.py`, `tests/unit/test_manifest.py`.

**Tests first (`tests/unit/test_manifest.py`):**
- `test_manifest_has_no_self_hash_field`.
- `test_hash_roundtrip` — `hash_file(manifest) == stored_sha` (stored in state/sidecar, not the file).
- `test_inject_for_review`.

**Implementation requirements:**
`[CONTRACT]`
```
def build_manifest(plan_envelope) -> dict      # {plan_sha256, current_task, tasks:[...]}  NO self sha
def write_manifest(control_root, manifest) -> Path
def manifest_sha_of(path) -> str
```

**Verification:** `python -m pytest tests/unit/test_manifest.py -v`
**Expected:** 3 passed.
**Commit message:** `feat: self-reference-free task manifest (Prompt §8)`

### Task 2.6: Git primitives — dedicated, fully-tested module (P0-2)

**Goal:** all git operations live in one tested module, consumed by store/adapters/engine/policy. Resolves the v2-review gap where `gitops` had no task.

**Depends:** Task 1.1.
**Files:** `src/zcode_agentflow/gitops.py`, `tests/unit/test_gitops.py`.

**Tests first (`tests/unit/test_gitops.py`):** — real temp git repos; cover tricky cases:
- `test_is_git_repo`, `test_head`, `test_is_clean`, `test_untracked_files`.
- `test_create_worktree_outside_repo` — worktree path outside the repo; returns base commit.
- `test_worktree_list` — enumerates worktrees (used by init recovery, Task 3.7).
- `test_branch_exists` — true/false; branch collision detection.
- `test_diff_patch` — `git diff base..head` as a patch string (for the Review Bundle).
- `test_changed_files_z` — NUL-separated file list (special chars safe).
- `test_name_status_z` — `--name-status -z`: parses A/D/M/Rxx/Cxx with old+new paths; **rename** (R) handled; **delete** (D) handled.
- `test_filename_with_spaces_and_unicode` — `weird "name".txt` and `中文.txt` parsed correctly via `-z` (not split on spaces).
- `test_git_path` — resolves the `.git` path (for MERGE_HEAD/rebase detection in preflight).

**Implementation requirements:**
`[CONTRACT]`
```
# gitops.py
def is_git_repo(path) -> bool
def head(repo) -> str
def is_clean(repo) -> bool
def untracked_files(repo) -> list[str]
def create_worktree(*, target_repo, worktree_path, branch) -> str       # base commit
def worktree_list(target_repo) -> list[dict]
def branch_exists(target_repo, branch) -> bool
def diff_patch(repo, base, head) -> str
def changed_files_z(repo, base, head) -> list[str]
def name_status_z(repo, base, head) -> list[tuple[str,str,str]]         # (status, old, new)
def git_path(repo) -> pathlib.Path
```
`[PSEUDO]` all via `subprocess.run(["git",...])`, `capture_output`, utf-8; `-z` output split on NUL (`\0`).

**Verification:** `python -m pytest tests/unit/test_gitops.py -v`
**Expected:** 11 passed.
**Done when:** git is a first-class tested module; no other phase re-implements git calls.
**Commit message:** `feat: dedicated git primitives module (P0-2)`

### Task 2.7: Model-output context invariants (P1-3)

**Goal:** beyond field types, validate that planner/review output is consistent with the *current run*; untrusted fields (esp. `base_commit`) are injected/checked, not trusted.

**Depends:** Tasks 2.1, 2.5.
**Files:** extend `src/zcode_agentflow/schemas.py`; `tests/unit/test_context_invariants.py`.

**Tests first (`tests/unit/test_context_invariants.py`):**
- `test_spec_context_matches_run` — `spec.run_id == state.run_id`, `spec.goal == state.goal`; `status==NEEDS_USER_INPUT ⇔ questions non-empty`; `READY_FOR_SPEC_APPROVAL ⇒ questions empty`.
- `test_plan_context_matches_run` — `plan.run_id == state.run_id`, `plan.base_commit == state.base_commit` (injected, not Codex's), `plan.plan_version == 1`; the single task has `id == "T001"`, `dependencies == []`.
- `test_declared_files_relative_no_traversal` — all relative, none containing `..`.
- `test_verification_cwd_relative` — `verification.cwd` relative (resolved under worktree at runtime).
- `test_verification_program_in_allowlist` — `program` passes the current allowlist (else reject at validation, not at exec).
- `test_base_commit_must_equal_state` — Codex-supplied `base_commit` ≠ state ⇒ rejected (must be injected by engine).

**Implementation requirements:**
`[CONTRACT]`
```
# schemas.py
def check_spec_context(spec_envelope, *, run_id, goal) -> None
def check_plan_context(plan_envelope, *, run_id, base_commit, allowlist) -> None
```
`[PSEUDO]` engine injects `run_id`/`goal`/`base_commit`/`plan_version` before validation (mirrors review-hash injection in Spec §4.5); model-supplied values for those fields are ignored/overwritten, then the invariant is checked.

**Verification:** `python -m pytest tests/unit/test_context_invariants.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: model-output context invariants; untrusted base_commit rejected (P1-3)`

---


## Phase 3 — Run lock + Store + two-phase transactions + atomic promotion + fault injection

**Entry:** Phase 2 complete (canonical bytes, ids, schemas, paths, manifest, gitops).
**Exit:**
1. Per-Run file lock (portalocker) guards all bookkeeping.
2. **No nested locking**: external CLI calls run *between* two short locked transactions (`begin_operation` → external → `finish_operation`). The Run lock is never held across a Codex/Grok/verification call (P0-3).
3. Multi-artifact promotion is atomic (generation dir + single pointer) (P1-4); fault injection fires after *each* promotion.
4. Event seq resumes from disk; operations PREPARED→RUNNING→COMMITTED/FAILED/UNKNOWN with idempotency on `(operation_id, request_sha256)`.
5. Operation `replay_policy` is three-valued (`SAFE_READ`/`COSTFUL_READ`/`UNSAFE_WRITE`) (clarification); `COSTFUL_READ` replay is config-driven.
6. `workflow_init` is itself transactional with preflight + safe compensation (no destructive auto-cleanup).
7. Fault-injection matrix proves correct resume/continue/NEEDS_HUMAN at every step.

### Task 3.1: Per-Run file lock (Review P0-7)

**Goal:** one lock per Run, non-reentrant; held only across short Store steps.

**Depends:** Task 2.4.
**Files:** `src/zcode_agentflow/locks.py`, `tests/unit/test_locks.py`.

**Tests first (`tests/unit/test_locks.py`):**
- `test_lock_is_exclusive_cross_process` — second process waits until release.
- `test_lock_released_on_exception`.
- `test_lock_path_under_control_root` — `<control_root>/.agentflow.lock`.
- `test_lock_not_reentrant` (P0-3 guard) — re-acquiring the same lock from the same process while held ⇒ `LockHeldError`/timeout (documents non-reentrancy; the design must never do this).

**Implementation requirements:**
`[CONTRACT]`
```
# locks.py
@contextmanager
def run_lock(control_root, timeout=30): ...    # portalocker LOCK_EX; raise LockTimeout
```
`[PSEUDO]` portalocker binary mode. **Rule:** the only callers are `Store.begin_operation` and `Store.finish_operation`; nothing else acquires it. (The v2 deadlock came from `_guard` also acquiring it — removed in Task 3.4.)

**Verification:** `python -m pytest tests/unit/test_locks.py -v`
**Expected:** 4 passed.
**Commit message:** `feat: per-Run non-reentrant file lock (P0-7, P0-3 guard)`

### Task 3.2: Events (resumable seq) + state snapshot (Review §六.2)

**Goal:** seq resumes from disk; state is atomically replaced.

**Depends:** Tasks 2.2, 3.1.
**Files:** `src/zcode_agentflow/store.py` (events+state), `tests/unit/test_events_state.py`.

**Tests first (`tests/unit/test_events_state.py`):**
- `test_seq_resumes_after_restart` — next seq continues (not resets to 1).
- `test_event_carries_recoverable_state_after` — `state_after_path` + matching `state_after_sha256`.
- `test_state_save_is_atomic_replace` — concurrent readers never see a half-write.
- `test_load_state_roundtrips_all_fields`.

**Implementation requirements:**
`[CONTRACT]`
```
class EventLog:
    def __init__(self, control_root): self._next_seq = self._last_seq_on_disk() + 1
    def append(..., *, state_after_path, state_after_sha)   # atomic append+fsync, inside run_lock
    def latest() -> dict | None
class State: ... ; save() uses write_canonical_json + os.replace
```

**Verification:** `python -m pytest tests/unit/test_events_state.py -v`
**Expected:** 4 passed.
**Commit message:** `feat: resumable event seq + atomic state snapshot (§六.2)`

### Task 3.3: Operation lifecycle + idempotency + replay_policy (Prompt §9,§10; clarification)

**Goal:** full required file set; idempotency; three-valued replay policy (clarification: read-only Codex/Grok are COSTFUL_READ, not "side-effecting").

**Depends:** Tasks 2.2, 2.3, 3.1.
**Files:** extend `src/zcode_agentflow/store.py`, `tests/unit/test_operations.py`.

**Tests first (`tests/unit/test_operations.py`):**
- `test_prepare_creates_required_files` — `request.json`, `request_sha256`, `status=PREPARED`, empty `raw/`.
- `test_idempotent_committed_same_request`.
- `test_conflict_different_request_rejected`.
- `test_replay_policy_SAFE_READ_auto_replays` (clarification) — git/status/hash op UNKNOWN+no result ⇒ auto replay.
- `test_replay_policy_COSTFUL_READ_config_driven` — Codex/Grok UNKNOWN+no result ⇒ replay only if `config.allow_costful_replay=true`, else NEEDS_HUMAN (not blanket UNSAFE).
- `test_replay_policy_UNSAFE_WRITE_needs_human` — verification UNKNOWN ⇒ NEEDS_HUMAN (may have written files).
- `test_running_with_result_can_finish_commit`.

**Implementation requirements:**
`[CONTRACT]`
```
class Operation:
    status: Literal["PREPARED","RUNNING","COMMITTED","FAILED","UNKNOWN"]
    replay_policy: Literal["SAFE_READ","COSTFUL_READ","UNSAFE_WRITE"]
class OperationManager:
    def prepare(operation_id, request, replay_policy) -> Operation
    def open_existing(operation_id) -> Operation
```
`[PSEUDO]` request_sha = `sha256_hex(canonical_json_bytes(request))`. Classification table (Prompt clarification): Codex planning/Grok review → `COSTFUL_READ`; git/status/hash → `SAFE_READ`; verification → `UNSAFE_WRITE`; worktree creation → `UNSAFE_WRITE` (but has its own recovery in Task 3.7).

**Verification:** `python -m pytest tests/unit/test_operations.py -v`
**Expected:** 7 passed.
**Commit message:** `feat: operation lifecycle + idempotency + 3-valued replay_policy (Prompt §9,§10, clarification)`

### Task 3.4: Two-phase transactions — begin/finish (no nested lock) (P0-3)

**Goal:** kill the `_guard`+`commit_operation` deadlock. External calls run between two short locked transactions; intermediate Spec states (DRAFTING_SPEC/VERIFYING_TASK/REVIEWING_TASK) are surfaced.

**Depends:** Tasks 3.1, 3.2, 3.3.
**Files:** extend `src/zcode_agentflow/store.py` (`begin_operation`/`finish_operation`); `tests/store/test_two_phase.py`.

**Tests first (`tests/store/test_two_phase.py`):**
- `test_begin_acquires_then_releases_lock` — after `begin_operation` returns, the lock is free (external phase can run).
- `test_external_phase_runs_without_lock` — assert no lock held during the simulated Codex call (the v2 bug held the lock for minutes).
- `test_finish_reacquires_and_commits` — `finish_operation` re-acquires, validates `active_operation_id`+revision, writes event/state, COMMITTED, clears active op.
- `test_concurrent_status_succeeds_during_external_phase` — `workflow_status` (a read) succeeds while an operation is mid-external (proves status isn't blocked; P0-3 secondary goal).
- `test_begin_advances_to_intermediate_state` — e.g. draft begin → state `DRAFTING_SPEC`; finish → `READY_FOR_SPEC_APPROVAL` (surfaces Spec's intermediate states).
- `test_finish_rejects_if_active_operation_changed` — another op interleaved ⇒ finish rejected.

**Implementation requirements:**
`[CONTRACT]`
```
# store.py
class Store:
    def begin_operation(self, *, operation_id, request, tool, expected_revision,
                        intermediate_state, replay_policy) -> BeginResult
        # under run_lock: ALLOWED_FROM + CAS + verify_locked_artifacts(gate nodes);
        # write request/PREPARED->RUNNING; set state=intermediate_state; set active_operation_id; RELEASE lock
        # returns: staging dir path + loaded state
    def finish_operation(self, *, operation_id, expected_revision, next_state,
                         staged_artifacts, result, promote=True) -> dict
        # under run_lock: assert active_operation_id==operation_id & CAS unchanged;
        # validate staged artifacts (schemas); compute hashes over actual bytes;
        # promote (Task 3.5); append event; replace state.json; COMMITTED; clear active_operation_id; RELEASE lock
```
`[PSEUDO]` **`_guard` does NOT acquire the lock** — its checks are folded into `begin_operation`. Engine methods: `begin → call adapter (no lock) → finish`. This is the canonical P0-3 fix.

**Verification:** `python -m pytest tests/store/test_two_phase.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: two-phase begin/finish transactions, no nested lock (P0-3)`

### Task 3.5: Atomic multi-artifact promotion (P1-4)

**Goal:** a single operation producing PLAN.md + plan-envelope.json + task-manifest.json promotes atomically.

**Depends:** Task 3.4.
**Files:** extend `src/zcode_agentflow/store.py`; `tests/store/test_promotion.py`.

**Tests first (`tests/store/test_promotion.py`):**
- `test_promotion_uses_generation_dir_and_pointer` — artifacts land in `artifacts/<operation_id>/` and a single `current.json` pointer switches; readers see either old or new set, never mixed.
- `test_promotion_manifest_drives_idempotent_recovery` — a `promotion-manifest.json` lists `{source,target,promoted}` per artifact; recovery completes unpromoted items idempotently.
- `test_crash_between_promotions_recovers_consistently` — inject a crash mid-set; on resume the pointer hasn't switched, so the partial set is invisible and recovery finishes or rolls back atomically.

**Implementation requirements:**
`[CONTRACT]`
```
# store.py
def promote_artifacts(*, operation_id, staged_artifacts, authoritative_map) -> None
    # 1. write all into artifacts/<operation_id>/
    # 2. write promotion-manifest.json (per-file promoted:false)
    # 3. mark each promoted:true atomically (os.replace per file) -- recoverable
    # 4. switch current.json pointer to the new generation (single atomic os.replace)
def current_generation(control_root) -> str   # reads pointer
```
`[PSEUDO]` fault-injection point **after each per-file promotion** (not only after the whole step) — closes P1-4.

**Verification:** `python -m pytest tests/store/test_promotion.py -v`
**Expected:** 3 passed.
**Commit message:** `feat: atomic multi-artifact promotion via generation pointer (P1-4)`

### Task 3.6: Fault-injection recovery matrix across the two-phase flow (Prompt §3,§10)

**Goal:** prove recovery for every crash point in begin/external/finish/promotion.

**Depends:** Tasks 3.4, 3.5.
**Files:** `tests/store/test_fault_injection.py` (parametrized).

**Tests first:** parametrize crash points across: begin steps, before-external, after-external (no result), after-result, each promotion, before/after event, before/after state replace, before COMMITTED. For each, resume `Store.recover(run_id)` and assert:
- final state consistent with events' latest snapshot;
- operation COMMITTED+idempotent OR classified UNKNOWN with the correct `replay_policy` outcome (SAFE auto-replay / COSTFUL config-driven / UNSAFE → NEEDS_HUMAN).

**Implementation requirements:** `[PSEUDO]` a `crash_after_step` hook (env-flag) in begin/finish/promote; `Store.recover` rebuilds from latest event, classifies non-COMMITTED ops by `replay_policy`.

**Verification:** `python -m pytest tests/store/test_fault_injection.py -v`
**Expected:** full matrix green.
**Commit message:** `test: two-phase fault-injection recovery matrix (Prompt §3,§10)`

### Task 3.7: Transactional workflow_init + preflight + safe compensation (Prompt §11)

**Goal:** init is itself a begin/finish transaction with preflight; half-created worktree recovers or NEEDS_HUMAN; never destructive.

**Depends:** Tasks 3.4, 2.4, 2.6 (gitops).
**Files:** `src/zcode_agentflow/preflight.py`, extend `store.py` (`init_run` via begin/finish), `tests/unit/test_workflow_init_recovery.py`.

**Tests first (`tests/unit/test_workflow_init_recovery.py`):**
- `test_preflight_rejects_non_git_repo`, `test_preflight_rejects_in_progress_merge` (`.git/MERGE_HEAD` etc. via `gitops.git_path`), `test_preflight_rejects_nesting_roots`.
- `test_existing_active_run_rejected` — active pointer non-terminal ⇒ reject unless `abandon` (ties to Task 1.3).
- `test_half_created_worktree_recovers_or_needs_human` — crash after `git worktree add` before finish; resume claims if branch ownership matches (via `gitops.worktree_list`), else NEEDS_HUMAN; **never** auto-`worktree remove`.
- `test_branch_foreign_collision_needs_human`.
- `test_init_writes_active_pointer` — pointer set on finish (ties to Task 1.3).

**Implementation requirements:**
`[CONTRACT]`
```
# preflight.py
def preflight(*, target_repo, control_root, worktree_root, branch) -> None
    # git available; target_repo is git repo; HEAD exists; no MERGE_HEAD/REBASE_HEAD/CHERRY_PICK_HEAD;
    # branch/worktree don't collide (gitops.branch_exists/worktree_list); roots non-nesting; Windows path usable
```
`[PSEUDO]` init: preflight → `begin_operation` → `gitops.create_worktree` (journal branch; replay_policy=UNSAFE_WRITE with dedicated recovery) → `finish_operation(next_state=INITIALIZING_WORKTREE, base_commit)`. Recovery consults journal + worktree list; no destructive cleanup.

**Verification:** `python -m pytest tests/unit/test_workflow_init_recovery.py -v`
**Expected:** 5 passed.
**Commit message:** `feat: transactional workflow_init + preflight + safe recovery (Prompt §11)`

---


## Phase 4 — Adapters + prompt templates

**Entry:** Phase 3 complete (Store provides begin/finish + staging + promotion); Phase 0 confirmed real CLI flags.
**Exit:**
1. Three adapters share `invoke(request, repo_cwd, operation_dir, timeout)`; none accept an arbitrary shell string.
2. Codex prompt via **stdin**, cwd = `run_worktree_root`, output dir = operation staging (three distinct concepts).
3. Grok receives the full immutable Review Bundle; read-only; HEAD/clean rechecked.
4. Verification supports `executable`/`cmd_script`/`bash_script` kinds; outer launcher stays `shell=False`; `.bat`/`.cmd` work on Windows (P1-5).
5. The three prompt templates exist, are tested for required placeholders, no leftover `{...}`, and correct content.
6. Integration tests use **fake executables** asserting argv/stdin/cwd/output/timeout.

### Task 4.0: Prompt templates (P0-2)

**Goal:** create + test the three prompt files the adapters/engine render. Resolves the v2-review gap where prompts had no task.

**Depends:** Tasks 2.1 (schema names), 2.7 (context invariants).
**Files:** `prompts/planner-draft.md`, `prompts/planner-plan.md`, `prompts/reviewer.md`, `src/zcode_agentflow/prompts.py`, `tests/unit/test_prompts.py`.

**Tests first (`tests/unit/test_prompts.py`):**
- `test_all_required_placeholders_present` — each template contains its required placeholders (draft: `{goal}`, `{answers}`; plan: `{approved_spec_sha256}`, `{spec_markdown}`; reviewer: references the bundle files).
- `test_render_leaves_no_unreplaced_placeholders` — after rendering with sample values, no residual `{...}` remains.
- `test_draft_template_emits_spec_only` — draft body instructs SPEC-only output (no plan).
- `test_plan_template_requires_exactly_one_task` — plan body states "produce exactly ONE task; if the goal can't be one task, return BLOCKED" (ties to P0-5 wrapper).
- `test_reviewer_references_bundle_files` — reviewer body names SPEC.md/PLAN.md/task-manifest.json/execution-result.json/diff.patch explicitly (P0-5/Grok context).
- `test_templates_reference_resolved_agent_context` — each references `resolved-agent-context.md` (ties to Task 5.3).

**Implementation requirements:**
`[DATA]` template contents per the assertions above (SPEC-only for draft; one-task-or-BLOCKED for plan; bundle-file-referencing for reviewer; all reference resolved agent context).
`[CONTRACT]`
```
# prompts.py
def render(template_name, **fields) -> str   # raise on missing required placeholder; raise on leftover {...}
```

**Verification:** `python -m pytest tests/unit/test_prompts.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: prompt templates with placeholder + content tests (P0-2)`

### Task 4.1: Adapter protocol + base + secret redaction (Prompt §12, §20)

**Goal:** shared contract + redaction; no adapter logs secrets.

**Depends:** Phase 0, Task 2.2.
**Files:** `src/zcode_agentflow/adapters/__init__.py`, `adapters/base.py`, `src/zcode_agentflow/redact.py`, `tests/unit/test_redact.py`, `tests/adapters/test_base_protocol.py`.

**Tests first:**
- `tests/unit/test_redact.py` — Bearer/OpenAI/xAI/GitHub tokens, api[_-]?key, cookie, password/secret/token patterns redacted; non-secret preserved; non-ASCII preserved.
- `tests/adapters/test_base_protocol.py` — fake adapter invoked with `(request, repo_cwd, operation_dir, timeout)`; raw stdout/stderr written to `operation_dir/raw/`; returns `AdapterResult{exit_code, stdout, stderr, artifacts, parsed}`.

**Implementation requirements:**
`[CONTRACT]`
```
# adapters/base.py
@dataclass class AdapterResult: exit_code:int; stdout:str; stderr:str; artifacts:dict[str,Path]; parsed:Any
class Adapter(Protocol):
    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult
def run_subprocess(cmd, *, stdin_text, cwd, timeout, operation_dir) -> tuple[int,str,str]   # shell=False; kill process tree on timeout; redact before return
```
`[PSEUDO]` process-tree kill (Windows `CREATE_NEW_PROCESS_GROUP`+`taskkill /T`; POSIX `setsid`+`SIGKILL` group). Redaction patterns per Prompt §20.

**Verification:** `python -m pytest tests/unit/test_redact.py tests/adapters/test_base_protocol.py -v`
**Expected:** all pass.
**Commit message:** `feat: adapter protocol + process-tree kill + redaction (Prompt §12,§20)`

### Task 4.2: Codex adapter — stdin prompt, read-only, separated cwd/output (Review P0-3, P0-4)

**Goal:** Codex truly receives the prompt; reads the repo from worktree; writes JSON to operation dir.

**Depends:** Tasks 4.0, 4.1, 2.1, Phase 0.
**Files:** `src/zcode_agentflow/adapters/codex.py`, `tests/adapters/test_codex_fake_exe.py`, `tests/adapters/_fakes/fake_codex.py`.

**Tests first (fake executable):** `fake_codex.py` asserts it got non-empty **stdin** (logs it), its cwd is `repo_cwd`, and writes JSON to the `-o` path.
- `test_prompt_reaches_stdin` (would have caught Review P0-3).
- `test_cwd_is_run_worktree_root_not_control` (P0-4).
- `test_output_dir_is_operation_staging`.
- `test_read_only_flag_present` (from capabilities).
- `test_timeout_kills_process_tree`.
- `test_result_validated_against_schema` — validates against spec-envelope **or** planner-result wrapper (P0-5) per the `kind`.

**Implementation requirements:**
`[CONTRACT]`
```
# adapters/codex.py
class CodexAdapter.invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult
    # request = {kind:"draft_spec"|"create_plan", prompt, schema_name, model, reasoning_effort, sandbox}
    # argv from capabilities.working_flags; stdin_text=prompt; cwd=repo_cwd; -o=operation_dir/out.json
```
`[PSEUDO]` prompt/cwd/output_dir separate variables; `kind` explicit (not inferred from prompt text — Review §四.4). For `create_plan`, validate against the **planner-result wrapper** (so multi-task ⇒ BLOCKED cleanly, P0-5).

**Verification:** `python -m pytest tests/adapters/test_codex_fake_exe.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: codex adapter — stdin prompt, read-only, separated cwd/output, planner-result (P0-3,P0-4,P0-5)`

### Task 4.3: Grok adapter + immutable Review Bundle (Review P0-5; Prompt §13)

**Goal:** Grok reviews against the full bundle; read-only; HEAD/clean rechecked before/after.

**Depends:** Tasks 4.0, 4.1, 2.1, 2.6 (gitops diff), Phase 0.
**Files:** `src/zcode_agentflow/adapters/grok.py`, `src/zcode_agentflow/review_bundle.py`, `tests/adapters/test_grok_fake_exe.py`, `tests/adapters/_fakes/fake_grok.py`.

**Tests first (fake executable):** `fake_grok.py` reads the bundle dir, asserts all files present+non-empty, writes a valid review-result.
- `test_bundle_contains_all_required_files` — SPEC/PLAN/manifest/execution-result/diff.patch/review-request/prompt.
- `test_grok_output_cites_spec_and_plan`.
- `test_readonly_flags_from_capabilities`.
- `test_bundle_unchanged_after_call`.
- `test_head_and_clean_checked_pre_and_post` — HEAD changed or dirty at call time ⇒ raise (→ NEEDS_HUMAN); re-check after.
- `test_result_validated_against_review_schema_and_invariants`.

**Implementation requirements:**
`[CONTRACT]`
```
# review_bundle.py
def assemble(*, operation_dir, control_root, run_worktree_root, base_commit, head_commit, task, exec_result, resolved_agent_context) -> Path
# adapters/grok.py
class GrokAdapter.invoke(...) -> AdapterResult   # flags from capabilities; validates review
```
`[PSEUDO]` review hash fields injected by engine from state (not trusted from Grok). Bundle assembled in staging, promoted by Store.

**Verification:** `python -m pytest tests/adapters/test_grok_fake_exe.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: grok adapter + immutable Review Bundle (P0-5, Prompt §13)`

### Task 4.4: Verification adapter — structured, contained, script kinds (P1-5; Prompt §16)

**Goal:** run a task's verification safely; support `.bat`/`.cmd` on Windows without an outer shell; honest network policy.

**Depends:** Tasks 4.1, 2.1.
**Files:** `src/zcode_agentflow/adapters/verification.py`, `tests/adapters/test_verification.py`.

**Tests first (`tests/adapters/test_verification.py`):**
- `test_executable_kind_shell_false` — `{kind:"executable", program:"python", args:["-V"], cwd:".", timeout_sec:10}` ⇒ exit 0; launched `shell=False`.
- `test_cmd_script_kind_on_windows` — `{kind:"cmd_script", program:"scripts/t.bat", args:["x"]}` ⇒ invoked as `cmd.exe /d /s /c <safe args>`; outer launcher still `shell=False`; works on Windows (P1-5).
- `test_bash_script_kind` — `{kind:"bash_script", program:"scripts/t.sh"}` ⇒ `bash <script> <args>`.
- `test_cwd_escape_rejected`, `test_program_not_in_allowlist_rejected`, `test_nonzero_exit_recorded`, `test_secret_redaction_applied`, `test_timeout_kills_process_tree`.
- `test_script_path_must_be_within_worktree` — script paths resolved under worktree (P1-5).
- `test_network_policy_is_advisory_or_absent` — assert no fake `deny`.

**Implementation requirements:**
`[CONTRACT]`
```
# adapters/verification.py
def run_verification(*, command, worktree_root, allowlist, timeout) -> dict
    # command.kind in {executable, cmd_script, bash_script}
    # executable -> [program, *args]; cmd_script -> ["cmd.exe","/d","/s","/c", program, *args]; bash_script -> ["bash", program, *args]
    # all via run_subprocess(shell=False); cwd resolved+assert_within; program/script in allowlist or under worktree
```
`[PSEUDO]` allowlist sourced from resolved agent context (Task 5.3) with v1 fallback; if v1 doesn't support scripts, the schema/config **explicitly rejects** script kinds rather than claiming support (P1-5 honesty).

**Verification:** `python -m pytest tests/adapters/test_verification.py -v`
**Expected:** 10 passed.
**Commit message:** `feat: verification adapter with executable/cmd_script/bash_script kinds (P1-5, Prompt §16)`

---


## Phase 5 — Approval + change policy + agent context

> **Moved before the engine** (resolves P0-6): the engine's `workflow_approve_spec` (6.3) needs approval, and `workflow_verify_task` (6.4) needs change policy. Implementing these here keeps the plan in topological order.

**Entry:** Phase 2 (schemas/canon) complete; Phase 0 captured tool-confirmation configuration; gitops (2.6) available.
**Exit:**
1. Approval records carry `run_id`; strong confirmation must be actually *configured* (not just "supported") and tested to pop for `workflow_approve_spec` only; fallback=true emits `security_warning`; fallback=false+unsupported fails at startup (already in 1.4) (P1-1, Prompt §17).
2. Git change policy uses `gitops.name_status_z`, handles rename/delete/special names, configurable patterns; authorized allowed, unauthorized → NEEDS_HUMAN; irreversible migration → always NEEDS_HUMAN (Prompt §16).
3. `AGENTS.md` never overwritten: user file + template → `resolved-agent-context.md` under control_root (Prompt §19).

### Task 5.1: Approval records — configured confirmation + run_id + non-silent fallback (Prompt §17; Review §六.5; P1-1)

**Goal:** "supported" is not enough — confirmation must be *configured on the specific tool* and proven to fire.

**Depends:** Phase 0 (confirmation config field), Task 1.4 (validate_capabilities).
**Files:** `src/zcode_agentflow/approval.py`, `tests/unit/test_approval.py`.

**Tests first (`tests/unit/test_approval.py`):**
- `test_record_includes_run_id_and_all_fields` — `{approval_id, run_id, gate, artifact_sha256, approved_at, approval_method, commit_at_approval}`.
- `test_strong_only_when_configured` — `tool_confirmation.supported=true` **and** `confirmation_config.enabled=true` ⇒ `approval_method="zcode_tool_confirmation"`; supported=true but **not configured** on the tool ⇒ treat as not-strong (do **not** falsely claim strong confirmation) (P1-1).
- `test_unsupported_and_fallback_false_fails_loud`.
- `test_fallback_true_returns_security_warning` — non-empty `security_warning` ZCode must display.

**Implementation requirements:**
`[CONTRACT]`
```
# approval.py
class ApprovalUnavailableError(Exception): ...
def record_approval(*, control_root, run_id, gate, artifact_sha256, commit_at_approval,
                    effective_approval_mode) -> dict
    # effective_approval_mode resolved by validate_capabilities ("zcode_tool_confirmation" | "non_adversarial_convention" | None)
    # if None -> ApprovalUnavailableError; if fallback -> include security_warning
```
`[PSEUDO]` "configured" = the plugin manifest/tool metadata actually sets the confirmation field found in the spike (Task 0.2); a Phase 7 packaging task verifies `workflow_approve_spec` pops confirmation while other tools don't. Record written via canonical writer within a Store finish.

**Verification:** `python -m pytest tests/unit/test_approval.py -v`
**Expected:** 4 passed.
**Commit message:** `feat: approval — configured confirmation, run_id, non-silent fallback (P1-1, Prompt §17)`

### Task 5.2: Git change policy via name-status -z (Prompt §16)

**Goal:** robust, special-character-safe, configurable change audit.

**Depends:** Tasks 2.6 (gitops), 2.1 (schema).
**Files:** `src/zcode_agentflow/policy_change.py`, `tests/unit/test_policy_change.py`.

**Tests first (`tests/unit/test_policy_change.py`):** — real git:
- `test_authorized_delete_allowed`, `test_unauthorized_delete_needs_human`, `test_dependency_change_unauthorized_needs_human`, `test_undeclared_file_needs_human`.
- `test_rename_handled_correctly` — `git mv` ⇒ R status; old removed, new added; neither missed nor falsely flagged.
- `test_special_filename_with_spaces_and_quotes` — `weird "name".txt` parsed via `-z`.
- `test_irreversible_migration_always_needs_human` — configurable pattern, always NEEDS_HUMAN even if "authorized".
- `test_patterns_configurable` — dependency/migration patterns from config.

**Implementation requirements:**
`[CONTRACT]`
```
# policy_change.py
def audit(*, worktree, base_commit, head_commit, declared_files, planned_changes, config) -> dict
    # uses gitops.name_status_z; returns {needs_human, reasons, deleted_files, dependency_files_changed(bool),
    #          migrations_added, undeclared_files_changed, irreversible_migration(bool)}
```
`[PSEUDO]` `dependency_files_changed` is a **bool** (matches strict execution-result schema; Review §六.9). Status codes A/D/M/Rxx/Cxx; for R/C use both paths.

**Verification:** `python -m pytest tests/unit/test_policy_change.py -v`
**Expected:** 8 passed.
**Commit message:** `feat: git change policy via name-status -z, configurable (Prompt §16)`

### Task 5.3: Agent context resolution — never overwrite AGENTS.md (Prompt §19; Review §六.4)

**Goal:** combine the repo's existing `AGENTS.md` + template → resolved context under control_root.

**Depends:** Task 2.2.
**Files:** `src/zcode_agentflow/agents_context.py`, `AGENTS.md.template`, `tests/unit/test_agents_context.py`.

**Tests first (`tests/unit/test_agents_context.py`):**
- `test_no_existing_agents_md_uses_template_only`.
- `test_existing_agents_md_preserved_and_merged` — repo file untouched; resolved = user file + "## AgentFlow notes" once.
- `test_repeated_run_does_not_duplicate_section`.
- `test_non_ascii_content_handled` — UTF-8 strict preserved.
- `test_resolved_written_under_control_root` — never in target_repo.

**Implementation requirements:**
`[CONTRACT]`
```
# agents_context.py
def resolve(*, target_repo, plugin_root, control_root) -> Path
    # read repo AGENTS.md if present (utf-8 strict); read plugin AGENTS.md.template;
    # compose (user-first, append notes once); write_canonical_text to control_root/resolved-agent-context.md
```
`[DATA]` `AGENTS.md.template`: verification command shape (executable/cmd_script/bash_script), v1 single-task, allowlist note. The resolved path feeds adapters (referenced in prompts, Task 4.0).

**Verification:** `python -m pytest tests/unit/test_agents_context.py -v`
**Expected:** 5 passed.
**Commit message:** `feat: agent context resolution, never overwrite AGENTS.md (Prompt §19)`

---


## Phase 6 — Workflow Engine: the 9 tools

**Entry:** Phases 2–5 complete (schemas, canonical bytes, ids, paths, gitops, Store two-phase, adapters, approval, change policy, agent context).
**Exit:**
1. `Engine` implements all 9 tools; each checks `ALLOWED_FROM` + `expected_revision` CAS **inside `begin_operation`** (no separate `_guard` lock — P0-3).
2. Every external call is `begin → adapter (no lock) → finish` (two-phase).
3. `verify_locked_artifacts(state)` re-hashes SPEC/PLAN/manifest from disk at every gate (Prompt §14).
4. `planner_draft_spec` supports `NEEDS_USER_INPUT` → answers re-entry with a revision/attempt-bound operation id (Review §二, Prompt §15).
5. Exactly-one-task enforced at schema **and** business layer via the planner-result wrapper (P0-5); a multi-task goal ⇒ NEEDS_USER_INPUT/BLOCKED (never silent task[0]).
6. `workflow_complete_task` performs the full hard-check set (≥11), incl. on-disk artifact rehash.
7. A read-only `check_stop(run_id)` exists (consumed by the Stop hook in Phase 7).
8. The 9 FastMCP tool bodies delegate to the engine; stdio `tools/call` works for each.

### Task 6.1: Engine skeleton — ALLOWED_FROM (inside begin) + artifact rehash + check_stop (Prompt §14, §15; Review §五, P0-3)

**Goal:** engine scaffold; guard logic folded into `begin_operation`; read-only `check_stop` for the hook.

**Depends:** Phases 2–5.
**Files:** `src/zcode_agentflow/engine.py`, `src/zcode_agentflow/artifacts.py`, `tests/unit/test_engine_skeleton.py`.

**Tests first (`tests/unit/test_engine_skeleton.py`):**
- `test_allowed_from_table_complete` — each tool's allowed-source set matches Spec §3.1 (e.g. `planner_draft_spec ∈ {INITIALIZING_WORKTREE, NEEDS_USER_INPUT}`, `reviewer_review_task ∈ {VERIFYING_TASK}`, `workflow_complete_task ∈ {REVIEWING_TASK}`).
- `test_guard_runs_inside_begin_not_separately` (P0-3) — asserting ALLOWED_FROM + CAS + rehash all happen within a single locked `begin_operation`; no second lock acquired (instrument the lock to count acquisitions == 1 per begin).
- `test_calling_from_wrong_state_raises`.
- `test_verify_locked_artifacts_rehashes_from_disk` — mutate SPEC.md post-approval ⇒ raise (Review §五).
- `test_check_stop_read_only` — `check_stop(run_id)` returns `{allow_stop, reasons}` without mutating state; blocks when review missing/non-PASS/HEAD mismatch/dirty/artifact-hash mismatch.

**Implementation requirements:**
`[CONTRACT]`
```
# engine.py
ALLOWED_FROM = {"<tool>": {<states>}, ...}
class Engine:
    def __init__(self, store, adapters, roots, config, effective_config): ...
    # tools call store.begin_operation(tool, expected_revision, intermediate_state, ...)
    #   which performs ALLOWED_FROM + CAS + verify_locked_artifacts under ONE lock acquisition
    def check_stop(self, run_id) -> dict    # read-only; reuses the same hard-check primitives as complete_task
# artifacts.py
def verify_locked_artifacts(state, control_root) -> None    # raise ArtifactMismatch if hash_file(SPEC/PLAN/manifest) != state.*
```
`[PSEUDO]` `verify_locked_artifacts` uses `canonical.hash_file` (consistent with how hashes were stored). `check_stop` shares the complete-task check primitives so hook and engine never drift (Review §六.6).

**Verification:** `python -m pytest tests/unit/test_engine_skeleton.py -v`
**Expected:** 5 passed.
**Commit message:** `feat: engine scaffold — ALLOWED_FROM inside begin, rehash, check_stop (Prompt §14,§15, Review §五, P0-3)`

### Task 6.2: workflow_init + planner_draft_spec (answers re-entry) (Prompt §11, §15; Review §二)

**Goal:** init via the Store two-phase path; draft handles the questions/answers loop with correct operation ids.

**Depends:** Task 6.1, Phase 3 (init_run), Phase 4 (codex adapter).
**Files:** extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_planner.py`.

**Tests first (`tests/unit/test_engine_planner.py`):** — fake adapters injected:
- `test_draft_returns_needs_user_input_when_questions` — fake Codex wrapper `status=NEEDS_USER_INPUT`+2 questions ⇒ state `NEEDS_USER_INPUT`, returns questions.
- `test_reentry_with_answers_uses_new_operation_id` — second `planner_draft_spec(goal, answers=...)` ⇒ higher attempt op id; request carries `answers`,`state=NEEDS_USER_INPUT`,`expected_revision` (Review §二).
- `test_reentry_is_not_idempotent_replay` — second call re-invokes the adapter, returns the new SPEC.
- `test_draft_then_ready_for_spec_approval` — no questions ⇒ `READY_FOR_SPEC_APPROVAL`; SPEC.md written; `state.spec_sha256 = hash_file(SPEC.md)` (canonical).
- `test_spec_context_invariants_checked` — run_id/goal injected then validated (Task 2.7).

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_init(self, target_repo, goal, run_id=None) -> dict
def planner_draft_spec(self, goal, answers=None, expected_revision=None) -> dict
    # op id = ids.operation_id(run_id, "draft_spec", revision, attempt)  (Windows-safe, Task 2.3)
    # begin_operation -> state DRAFTING_SPEC -> CodexAdapter(kind="draft_spec") [no lock] ->
    #   validate spec + context invariants -> finish_operation(next_state per questions)
    #   SPEC.md staged+promoted; state.spec_sha256 = hash_file(SPEC.md)
```
`[PSEUDO]` attempt counter = count of existing ops with this (run, tool, revision) prefix on disk → re-entry auto-gets a fresh id.

**Verification:** `python -m pytest tests/unit/test_engine_planner.py -v`
**Expected:** 5 passed.
**Commit message:** `feat: workflow_init + planner_draft_spec with answers re-entry (Prompt §11,§15, Review §二)`

### Task 6.3: workflow_approve_spec + planner_create_plan (exactly-one-task via wrapper) (Review P0-8; P0-5)

**Goal:** approval locks spec_sha; plan validates approved sha and enforces one task through the planner-result wrapper.

**Depends:** Tasks 6.2, 5.1 (approval), 4.2 (codex).
**Files:** extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_plan.py`.

**Tests first (`tests/unit/test_engine_plan.py`):**
- `test_approve_records_and_locks_spec_sha` — approval record (run_id, gate=SPEC, artifact_sha256); spec locked; re-approve from PLANNING rejected.
- `test_create_plan_requires_approved_spec_sha` — wrong sha ⇒ `WorkflowError`.
- `test_create_plan_rejects_multi_task_via_wrapper` — fake Codex returns wrapper `status=BLOCKED` (because 2 tasks can't fit one) ⇒ engine routes NEEDS_USER_INPUT/BLOCKED; **never** silently takes a task (P0-5).
- `test_single_task_plan_accepted` — wrapper `READY_FOR_EXECUTION` with 1 task ⇒ state `READY_FOR_EXECUTION`; PLAN.md + task-manifest.json written; hashes from `hash_file`.
- `test_plan_context_invariants_checked` — run_id/base_commit/plan_version/task=T001/deps empty/relative paths/allowlist (Task 2.7); `base_commit` injected from state, not trusted.
- `test_landed_plan_envelope_stays_strict` — the promoted `plan-envelope.json` is the strict single-task artifact (no wrapper fields).

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_approve_spec(self, expected_revision) -> dict    # approval.record_approval; -> PLANNING
def planner_create_plan(self, approved_spec_sha, expected_revision) -> dict
    # validate approved_spec_sha == state.spec_sha256
    # begin -> CodexAdapter(kind="create_plan") -> validate planner-result wrapper
    #   wrapper.status==READY_FOR_EXECUTION & len(plan.tasks)==1 -> proceed
    #   wrapper.status in {NEEDS_USER_INPUT, BLOCKED} -> route accordingly
    #   validate_plan + check_plan_context; write PLAN.md + task-manifest.json (no self-hash); hashes from hash_file
```

**Verification:** `python -m pytest tests/unit/test_engine_plan.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: approve_spec + create_plan via planner-result wrapper, exactly-one-task (P0-8, P0-5)`

### Task 6.4: workflow_start_task + workflow_verify_task + change audit (Prompt §16)

**Goal:** start records task_start_commit; verify runs verification + change audit and routes failures to NEEDS_HUMAN.

**Depends:** Tasks 6.3, 4.4 (verification), 5.2 (change policy), 2.6 (gitops).
**Files:** extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_verify.py`.

**Tests first (`tests/unit/test_engine_verify.py`):**
- `test_start_records_task_start_commit` — `state.task_start_commit == HEAD`; `state.current_task == "T001"`.
- `test_verify_pass_when_green_clean_authorized`.
- `test_verify_routes_to_needs_human_on_test_failure`.
- `test_verify_routes_to_needs_human_on_unauthorized_change` (delete/dependency/migration/undeclared).
- `test_verify_routes_to_needs_human_on_dirty_or_untracked`.
- `test_authorized_delete_allowed`.
- `test_cmd_script_verification_runs_on_windows` — a `.bat` verification command (kind=cmd_script) executes (P1-5).

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_start_task(self, task_id, expected_revision) -> dict
def workflow_verify_task(self, task_id, expected_revision) -> dict
    # worktree must be clean + no untracked at verify time (gitops)
    # VerificationAdapter over task.verification_commands (executable/cmd_script/bash_script)
    # change_audit = policy_change.audit(...) via gitops.name_status_z
    # build execution-result (validate_execution); if not READY_FOR_REVIEW -> NEEDS_HUMAN
    # staged+promoted via finish_operation
```

**Verification:** `python -m pytest tests/unit/test_engine_verify.py -v`
**Expected:** 7 passed.
**Commit message:** `feat: start_task + verify_task + change audit + cmd_script support (Prompt §16, P1-5)`

### Task 6.5: reviewer_review_task (Review Bundle + hash injection) (Review P0-5)

**Goal:** assemble bundle, call Grok read-only, validate review with injected hashes.

**Depends:** Tasks 6.4, 4.3 (grok).
**Files:** extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_review.py`.

**Tests first (`tests/unit/test_engine_review.py`):**
- `test_review_pass_advances_to_reviewing_task`.
- `test_review_fail_routes_to_needs_human`.
- `test_hashes_injected_from_state_not_trusted`.
- `test_bundle_assembled_before_call` — all 7 files in staging.
- `test_head_and_clean_rechecked`.

**Implementation requirements:**
`[CONTRACT]`
```
def reviewer_review_task(self, task_id, expected_revision) -> dict
    # verify_locked_artifacts; assert clean + HEAD == last_reviewed_commit
    # review_bundle.assemble in staging; GrokAdapter.invoke [no lock]; validate_review (injected hashes)
    # staged review-result promoted via finish_operation; decision != PASS -> NEEDS_HUMAN
```

**Verification:** `python -m pytest tests/unit/test_engine_review.py -v`
**Expected:** 5 passed.
**Commit message:** `feat: reviewer_review_task with full bundle + hash injection (P0-5)`

### Task 6.6: workflow_complete_task — expanded rehash hard checks + READY_TO_MERGE (Prompt §14; Review §五)

**Goal:** complete only when everything truly holds; terminal = READY_TO_MERGE; no merge/push.

**Depends:** Task 6.5.
**Files:** extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_complete.py`.

**Tests first (`tests/unit/test_engine_complete.py`):**
- `test_complete_succeeds_only_when_all_checks_hold` — green ⇒ READY_TO_MERGE; checks all true.
- `test_complete_rejects_stale_review_commit`.
- `test_complete_rejects_dirty_or_untracked_worktree`.
- `test_complete_rejects_on_tampered_spec` — edit SPEC.md post-approval ⇒ on-disk rehash ≠ state ⇒ rejected (Review §五).
- `test_complete_rejects_on_tampered_plan_or_manifest`.
- `test_complete_rejects_stale_cas`.
- `test_complete_does_not_merge_or_push` — no push/merge in the call log; state == READY_TO_MERGE.

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_complete_task(self, task_id, expected_revision) -> dict
    # checks (>=11): exec_ready, decision_pass, review_head==HEAD, worktree_clean, no_untracked,
    #   spec_sha_on_disk==state, plan_sha_on_disk==state, manifest_sha_on_disk==state,
    #   review_hashes==state, cas, head!=task_start_commit, changed_files_as_expected
    # all -> READY_TO_MERGE; NO merge, NO push
```

**Verification:** `python -m pytest tests/unit/test_engine_complete.py -v`
**Expected:** 7 passed.
**Commit message:** `feat: workflow_complete_task — expanded rehash hard checks, READY_TO_MERGE, no merge/push (Prompt §14, Review §五)`

### Task 6.7: Wire engine into FastMCP tools (closes Review P0-2 fully)

**Goal:** the 9 tool bodies (declared in Phase 1) now call the engine; stdio `tools/call` works for all nine.

**Depends:** Tasks 6.2–6.6.
**Files:** extend `src/zcode_agentflow/server.py`, `src/zcode_agentflow/bootstrap.py`, `tests/mcp/test_all_tools_stdio.py`.

**Tests first (`tests/mcp/test_all_tools_stdio.py`):** real subprocess MCP client, full happy path with **fake adapters**:
- `test_stdio_full_happy_path_reaches_ready_to_merge` — over stdio: init → draft → approve → create_plan → start_task → (test does real git commit) → verify → review → complete ⇒ `workflow_status == READY_TO_MERGE`.
- `test_stdio_error_returns_mcp_error_not_crash` — illegal-state call ⇒ MCP error; server stays alive.

**Implementation requirements:**
`[CONTRACT]`
```
# server.py — each @mcp_server.tool() body: return get_engine().<method>(**args)
# bootstrap.py — get_engine() builds Engine for the active run (pointer-based, Task 1.3)
```
`[PSEUDO]` test-injectable engine factory; `WorkflowError/CasError/ArtifactMismatch` → MCP error result (never a bare traceback).

**Verification:** `python -m pytest tests/mcp/test_all_tools_stdio.py -v`
**Expected:** 2 passed.
**Commit message:** `feat: wire engine into 9 FastMCP tools; stdio full happy path (P0-2)`

---


## Phase 7 — Hooks + plugin packaging

**Entry:** Phase 6 complete (engine provides `check_stop`); Phase 0 captured the real hook contract and confirmation config.
**Exit:**
1. Hooks use the **real** payload/cwd/exit semantics from the spike; the Stop hook calls the single read-only `check_stop` (no duplicated weak logic) (Prompt §18, Review §六.6/§六.7).
2. `.zcode-plugin/plugin.json` uses the real manifest shape; tool-confirmation is **configured** on `workflow_approve_spec` and tested to pop only for that tool (P1-1).
3. Skill + commands describe the 9-tool order, the two gates, ALLOWED_FROM, and "READY_TO_MERGE is terminal; do not push".

### Task 7.1: Hooks on real spike contract + shared check_stop (Prompt §18; Review §六.6/§六.7)

**Goal:** hooks built on the observed ZCode contract; Stop delegates to one engine read-only check.

**Depends:** Phase 0 (Task 0.2), Phase 6 (`check_stop`).
**Files:** `hooks/<spike-confirmed>.json`, `hooks/protect_artifacts.py`, `hooks/gate_on_stop.py`, `src/zcode_agentflow/hook_logic.py`, `tests/unit/test_hook_logic.py`.

**Tests first (`tests/unit/test_hook_logic.py`):** — driven by the captured payload shape (adapt if spike differed):
- `test_protect_artifacts_denies_edit_of_spec_plan_state_events_reviews_approvals`.
- `test_protect_artifacts_allows_worktree_edits`.
- `test_protect_artifacts_resolves_plugin_root_via_spike_mechanism` — reaches plugin root via the spike-confirmed mechanism (env/cwd/manifest), not a hardcoded relative path (Review §六.7).
- `test_gate_on_stop_calls_engine_check_stop` — invokes `check_stop(run_id)`, acts on `{allow_stop, reasons}`; does **not** re-derive review/clean/HEAD logic (Review §六.6).
- `test_gate_on_stop_blocks_when_check_fails`.
- `test_hook_respects_stop_reentry_limit` — escalates to NEEDS_HUMAN feedback past the cap instead of looping.

**Implementation requirements:**
`[CONTRACT]`
```
# hook_logic.py
def run_protect_artifacts(payload) -> int     # 0 allow / <spike-deny-code> deny
def run_gate_on_stop(payload) -> int
```
`[DATA]` `hooks/<name>.json`: the actual shape from `docs/capabilities.json` (event names, command path resolved from plugin root, feedback field). `[PSEUDO]` hook scripts are thin: they call `hook_logic.run_*`; plugin-root resolution uses the spike-confirmed mechanism.

**Verification:** `python -m pytest tests/unit/test_hook_logic.py -v`
**Expected:** 6 passed.
**Commit message:** `feat: hooks on real spike contract + shared check_stop (Prompt §18, Review §六.6/§六.7)`

### Task 7.2: Plugin manifest + tool-confirmation configuration + skill/commands (P1-1)

**Goal:** real manifest; confirmation wired so `workflow_approve_spec` actually pops (not just "supported").

**Depends:** Tasks 7.1, 6.7, Phase 0.
**Files:** `.zcode-plugin/plugin.json`, `skills/agentflow/SKILL.md`, `commands/agentflow-start.md`, `commands/agentflow-status.md`, `tests/packaging/test_plugin_loads.py`.

**Tests first (`tests/packaging/test_plugin_loads.py`:**
- `test_manifest_matches_spike_shape` — `plugin.json` fields equal those recorded in capabilities.
- `test_manifest_configures_confirmation_for_approve_spec_only` — the confirmation field (from Task 0.2) is set on `workflow_approve_spec` and **not** on the other 8 tools (P1-1).
- `test_skill_describes_workflow_and_no_push` — SKILL.md covers 9-tool order, two gates, ALLOWED_FROM, "never edit SPEC/PLAN/state directly", "READY_TO_MERGE terminal; user merges externally; do not push".
- `test_commands_reference_tools` — start/status commands exist and reference the tools.

**Implementation requirements:**
`[DATA]` `plugin.json`: real shape; confirmation configured per the spike field on `workflow_approve_spec`. `[DATA]` SKILL.md / commands content per the assertions. (If the spike found confirmation unsupported, this task documents the fallback path instead and asserts `security_warning` is surfaced — ties to Task 5.1/1.4.)

**Verification:** `python -m pytest tests/packaging/test_plugin_loads.py -v`
**Expected:** 4 passed.
**Commit message:** `feat: plugin manifest with confirmation on approve_spec + skill/commands (P1-1)`

---


## Phase 8 — Three-layer E2E + docs + READY_TO_MERGE handoff

**Entry:** Phases 1–7 complete; unit/adapter/store/mcp/packaging tests green.
**Exit:**
1. Layer 1 (Engine E2E, fake adapters): a run that **really edits and commits code** reaches READY_TO_MERGE, plus the full rejection matrix.
2. Layer 2 (MCP transport E2E): the same run over real stdio.
3. Layer 3 (local smoke, real Codex/Grok): documented, **off by default**, manually invoked, gated on Phase 0.
4. A READY_TO_MERGE handoff report (branch, HEAD, tests, review) — **no push/merge/PR anywhere** (Prompt §22, Review §六.10).

### Task 8.1: Layer 1 — Engine E2E with real commit + full rejection matrix (Prompt §21)

**Goal:** prove the v1 vertical slice as a *code-execution* test.

**Depends:** Phases 1–7.
**Files:** `tests/e2e/test_engine_e2e.py`.

**Tests first (the test IS the deliverable):**
- `test_happy_path_edits_commits_and_merges_state` — real temp git repo + fake adapters: init → draft → approve → create_plan(1 task) → start_task → **edit a.txt, git add, git commit** → verify → review(PASS) → complete → READY_TO_MERGE. Assertions: `head != task_start_commit`; `changed_files == ["a.txt"]`; clean at complete; bundle complete; rehash passes; review.head_commit == HEAD.
- `test_stale_review_rejected_at_complete`.
- `test_dirty_worktree_rejected_at_complete`.
- `test_multi_task_plan_routes_blocked` — wrapper BLOCKED (P0-5), not silent task[0].
- `test_illegal_state_call_rejected`.
- `test_recovery_matrix_on_restart` — restart mid-run at several points; active-run pointer resumes correctly (Tasks 1.3/3.6).
- `test_approval_strong_path` — confirmation supported+configured ⇒ strong record.
- `test_approval_fallback_path` — supported=false, fallback=true ⇒ record + `security_warning`; fallback=false ⇒ NEEDS_HUMAN.

**Implementation requirements:** `[PSEUDO]` shared `e2e_run` helper injecting fake adapters; the edit+commit step uses real git in the worktree.

**Verification:** `python -m pytest tests/e2e/test_engine_e2e.py -v`
**Expected:** 8 passed.
**Commit message:** `test: Layer-1 engine E2E — real commit + rejection matrix (Prompt §21)`

### Task 8.2: Layer 2 — MCP transport E2E over real stdio (Prompt §21)

**Goal:** same happy path + rejections, via the actual MCP protocol.

**Depends:** Tasks 8.1, 6.7.
**Files:** `tests/e2e/test_mcp_transport_e2e.py`.

**Tests first:**
- `test_stdio_run_reaches_ready_to_merge` — spawn server, drive all 9 `tools/call` in order (test performs the real git commit between start and verify), assert `workflow_status == READY_TO_MERGE`.
- `test_stdio_rejection_returns_mcp_error_and_keeps_server_alive`.

**Implementation requirements:** `[PSEUDO]` reuse the stdio client; fake adapters via a test factory hook; edit+commit mirrors Layer 1.

**Verification:** `python -m pytest tests/e2e/test_mcp_transport_e2e.py -v`
**Expected:** 2 passed.
**Commit message:** `test: Layer-2 MCP transport E2E over stdio (Prompt §21)`

### Task 8.3: Layer 3 — local smoke harness (real Codex/Grok), off by default (Prompt §21)

**Goal:** a documented, manually-run smoke using real CLIs; never in the default suite.

**Depends:** Tasks 8.1, 8.2, Phase 0.
**Files:** `tests/smoke/run_local_smoke.py`, `docs/smoke.md`; pytest config excludes `tests/smoke`.

**Implementation requirements:** `[PSEUDO]` standalone script (not pytest) running a full run against a tiny sample repo with the **real** Codex/Grok adapters; gated on `AGENTFLOW_SMOKE=1` + capability check. `pyproject`/`pytest.ini` `--ignore=tests/smoke`. Outputs the READY_TO_MERGE handoff report.

**Verification:** `python -m pytest -q` (default) — smoke **not** collected; `AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py` (manual) — reaches READY_TO_MERGE.
**Commit message:** `test: Layer-3 local smoke harness (real CLIs), off by default (Prompt §21)`

### Task 8.4: READY_TO_MERGE handoff report + docs (Prompt §22; Review §六.10)

**Goal:** terminal output is a handoff report; user decides push/PR/merge.

**Depends:** Tasks 8.1–8.3.
**Files:** extend `workflow_complete_task`/a `handoff` helper to emit `{branch, head_commit, base_commit, changed_files, test_results, review_result, spec_sha256, plan_sha256}`; `docs/README.md` (install/configure/run/recover); finalize skill/commands for the handoff.

**Tests first (`tests/e2e/test_handoff_report.py`:**
- `test_complete_emits_handoff_with_no_push` — after complete, a handoff report exists with the listed fields; assert **no** `git push`/`merge` invoked.

**Implementation requirements:** `[PSEUDO]` handoff is a side-effect-free read of state+artifacts; SKILL/commands instruct the supervisor to stop at READY_TO_MERGE and present branch/commit to the user.

**Verification:** `python -m pytest tests/e2e/test_handoff_report.py -v`
**Expected:** 1 passed.
**Commit message:** `feat: READY_TO_MERGE handoff report + docs; no auto-push (Prompt §22, Review §六.10)`

### Task 8.5: Full suite green + final commit (no push)

**Goal:** whole suite passes; final commit. **No `git push` in the plan.**

**Depends:** all prior.
**Verification:** `python -m pytest -q`
**Expected:** all unit/adapter/store/mcp/packaging/e2e green; smoke excluded.
**Commit message:** `chore: v1 vertical slice complete — full suite green`

> **Explicitly NOT in this plan:** any `git push`, `git merge`, or PR creation. The v1 terminal state is `READY_TO_MERGE`. Push/merge/PR is the user's decision, made outside AgentFlow.


## Appendix A — File ownership table

| File | Owns | Mutates durable state? |
|---|---|---|
| `config.py` | load+validate config; validate_capabilities | no |
| `active_run.py` | active-run.json pointer | pointer only (via atomic replace) |
| `paths.py` | four-roots, containment | no |
| `ids.py` | Windows-safe ids + branch/dir normalize | no |
| `canonical.py` | canonical text/json bytes, hashing, atomic write | no |
| `schemas.py` | strict schemas + cross-field + context invariants | no |
| `manifest.py` | self-reference-free manifest | no |
| `locks.py` | per-Run non-reentrant lock | no |
| `store.py` | state/events/operations + begin/finish_operation + atomic promotion | **YES (sole mutator)** |
| `gitops.py` | git primitives incl. name_status_z | only via store/engine |
| `artifacts.py` | verify_locked_artifacts rehash | no (read-only) |
| `adapters/base,codex,grok,verification` | external CLI calls | staging only |
| `review_bundle.py` | assemble immutable bundle | staging only |
| `policy_change.py` | git change audit | no |
| `approval.py` | approval records + configured-confirmation + fallback warning | via store |
| `agents_context.py` | resolve AGENTS.md | resolved file only |
| `redact.py` | secret redaction | no |
| `engine.py` | 9 tools, ALLOWED_FROM (inside begin), rehash, check_stop | via store |
| `bootstrap.py` | process config + pointer recovery | no |
| `server.py` | FastMCP stdio, 9 tools, main(); stdout MCP-only | via engine |

Rule: **only `store.py` writes `state.json`/`events.jsonl`/`operations/`, and only inside a locked `begin_operation`/`finish_operation`.** The Run lock is held only across short bookkeeping steps, never across an external CLI call (P0-3).

## Appendix B — Phase/Task dependency graph (topological)

```
Phase 0 (spike, pinned versions, classified gates) gates EVERYTHING external
   │
Phase 1 (project + real MCP + active-run pointer + validate_capabilities)
   1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6
   │
Phase 2 (schemas + planner-result wrapper + canonical + ids + roots + manifest + gitops + context invariants)
   2.1  2.2  2.3  2.4  2.5  2.6  2.7   (2.1 needs 1.1; 2.4 needs 2.2/2.3; 2.5 needs 2.1/2.2; 2.7 needs 2.1/2.5)
   │
Phase 3 (lock + events/state + operations + two-phase begin/finish + atomic promotion + fault injection + init)
   3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6 → 3.7   (3.7 needs 2.6)
   │
Phase 4 (adapters + prompt templates)
   4.0 → 4.1 → {4.2, 4.3, 4.4}    (4.3 needs 2.6 diff; 4.4 needs 2.1)
   │
Phase 5 (approval + change policy + agent context)   ◄── moved BEFORE engine (P0-6)
   5.1  5.2  5.3   (5.1 needs Phase 0 confirmation + 1.4; 5.2 needs 2.6; 5.3 needs 2.2)
   │
Phase 6 (engine 9 tools)   needs 2,3,4,5
   6.1 → 6.2 → 6.3(needs 5.1,4.2) → 6.4(needs 4.4,5.2) → 6.5(needs 4.3) → 6.6 → 6.7
   │
Phase 7 (hooks + packaging)   needs 6 (check_stop) + Phase 0
   7.1(needs 6.1 check_stop) → 7.2
   │
Phase 8 (three-layer E2E + docs)   needs 6,7
   8.1 → 8.2 → 8.3 → 8.4 → 8.5
```

Phases are now in **true topological order** (P0-6); an executing agent may proceed linearly without forward-jumps. The only within-phase cross-references are explicit in each task's **Depends on**.

## Appendix C — Spec S1–S4 mapping

| Spec criterion | Mapped to |
|---|---|
| **S1** full NEW→READY_TO_MERGE (multi-task, 全景) | Out of v1 scope (multi-task/final-review = v1.3, Spec §3.3). v1 delivers the single-task spine (Phases 6+8) that v1.3 extends. |
| **S2** structured JSON handoff, no chat-relay | 2.1 strict schemas + planner-result wrapper; 4.2/4.3 adapters parse/validate; 6.x inject hashes/ids from state (not trusted from model). |
| **S3** process-integrity (detect direct edits / stale review / stale manifest / hash mismatch) | 2.5 (no self-hash) + 6.1 `verify_locked_artifacts` at every gate + 6.6 expanded complete checks + 3.6 recovery. (Per Spec §1.2, **not** same-privilege tamper-proof.) |
| **S4** v1 vertical slice: single task + one review + two gates, Windows/Git Bash | 8.1 (Layer 1, real commit) + 8.2 (Layer 2 stdio) + 8.3 (Layer 3 real CLIs). |

## Appendix D — v2-review close matrix (this revision)

| v2-review item | Severity | Closed by |
|---|---|---|
| P0-1 operation_id colon not Windows-safe | P0 | 2.3 (`__` separators, `^[A-Za-z0-9._-]+$`, `test_operation_id_is_windows_filename_safe`) |
| P0-2 gitops/prompts/config have no task | P0 | 2.6 (gitops), 4.0 (prompts), 1.2 (config.yaml+validation) |
| P0-3 _guard + commit_operation nested-lock deadlock | P0 | 3.4 (two-phase begin/finish, external call unlocked) + 6.1 (guard folded into begin) |
| P0-4 scan-newest recovers wrong project / skips corrupt | P0 | 1.3 (active-run pointer; recover-on-corrupt; no auto-switch) |
| P0-5 schema maxItems:1 vs "BLOCKED" contradiction | P0 | 2.1 (planner-result wrapper: READY/NEEDS_USER_INPUT/BLOCKED) + 4.2/6.3 |
| P0-6 non-topological task order | P0 | phases reordered (approval/change-policy before engine; hooks after) |
| P1-1 "supported" ≠ "configured" confirmation | P1 | 5.1 + 7.2 (configure on approve_spec only; test pop) + 1.4 (startup fail) |
| P1-2 capability gate too absolute on false | P1 | 0.5 (classified gates) + 1.4 |
| P1-3 missing model-output context invariants | P1 | 2.7 + 6.2/6.3 (base_commit injected, not trusted) |
| P1-4 multi-file promotion not atomic | P1 | 3.5 (generation dir + pointer + per-file fault injection) |
| P1-5 .bat/.cmd can't shell=False on Windows | P1 | 4.4 (executable/cmd_script/bash_script kinds) |
| P1-6 MCP stdout must be clean | P1 | 1.5 (stdout MCP-only test; logging→stderr; SDK framing) |
| P1-7 versions should be pinned after spike | P1 | 0.5 (versions + lock file) + 1.1 (deps match lock) |
| Clarification: has_side_effects bool vs replay policy | — | 3.3 (SAFE_READ/COSTFUL_READ/UNSAFE_WRITE) |

## Appendix E — First-review (v1→v2) close matrix (still holds in v2.1)

| First-review item | Closed by (v2.1) |
|---|---|
| P0-1 local mcp/ shadows SDK | 1.1 |
| P0-2 not a real MCP server | 1.5, 1.6, 6.7 |
| P0-3 Codex empty stdin | 4.2 |
| P0-4 Codex cwd = control_root | 4.2 |
| P0-5 Grok sees only diff | 4.3 (Review Bundle) |
| P0-6 split commit protocol + hash mismatch | 3.4 (single two-phase transaction, canonical bytes) |
| P0-7 no file lock | 3.1 |
| P0-8 single-task not enforced | 2.1 (maxItems:1) + 6.3 (wrapper) |
| §二 draft re-entry vs op idempotency | 6.2 |
| §三 static env-var repo binding | 1.3 (pointer) + runtime target_repo |
| §四 E2E design bugs | 8.1 (real commit) + 6.7 (factory) + 4.2 (explicit kind) |
| §五 hash checks trust stored value | 6.1 `verify_locked_artifacts` |
| §六.1 no single-trailing-newline canonical | 2.2 |
| §六.2 event seq resets | 3.2 |
| §六.3 fake network deny | 1.2 (advisory_only) + 4.4 |
| §六.4 AGENTS.md only template | 5.3 |
| §六.5 approval missing run_id + silent downgrade | 5.1 |
| §六.6 Stop hook weak | 7.1 (shared check_stop) |
| §六.7 hook payload/relative path unverified | Phase 0.2 + 7.1 |
| §六.8 pytest 0-tests exit code | 1.1 ships real tests |
| §六.9 schema too loose / type drift | 2.1 |
| §六.10 auto git push | 8.4/8.5 (no push/merge) |

## Appendix F — Risks & capabilities requiring real-environment confirmation (Phase 0)

Gated on the spike; until confirmed they block dependents and must be resolved in a real ZCode/CLI environment:

1. **ZCode plugin manifest exact shape** — blocks 1.5, 7.2.
2. **ZCode hook contract** (events/stdin/cwd/exit/feedback/Stop reentry) — blocks 7.1.
3. **ZCode tool-level mandatory confirmation** (supported? **and the config field to enable it per tool**) — blocks 5.1, 7.2; if unsupported, v1 uses the Spec §5.4 fallback.
4. **`mcp` SDK import path** — blocks 1.1/1.5.
5. **Codex flags** (read-only enforced; `reasoning_effort=high` available; stdin `-` semantics) — blocks 4.2.
6. **Grok flags** (real read-only flags; JSON shape; `--json-schema` availability) — blocks 4.3.
7. **portalocker on Windows** under the test concurrency model — blocks 3.1 (mitigation: swap to a tested alternative within 3.1 if flaky).
8. **Network control honesty** — v1 declares advisory-only/absent; true isolation out of v1 scope (Spec §1.2).

Each is a spike deliverable in `docs/capabilities.json` (versions + classified gates). A `false` on a `fallback-capable`/`optional` capability degrades gracefully; `false` on a `required` capability or any `unknown` blocks its dependent.

## Appendix G — Definition of Done (v1, maps to Spec §0.4 S4)

- [ ] `src/zcode_agentflow` package; **no** local `mcp/`; official SDK import verified; deps pinned to lock file.
- [ ] Real FastMCP stdio; initialize/tools/list/tools/call green; server stdout MCP-only.
- [ ] `workflow_init(target_repo, goal)`; derived run paths; **active-run pointer** recovery; no repo env-var binding.
- [ ] `validate_capabilities` at startup; required-missing fails fast; confirmation **configured** on approve_spec.
- [ ] Five strict schemas incl. planner-result wrapper; `tasks` maxItems:1; business-layer single-task.
- [ ] Canonical bytes (single trailing newline, strict UTF-8, deterministic JSON); hashing over actual bytes.
- [ ] Per-Run non-reentrant lock; two-phase begin/finish (external call unlocked); state replaced before COMMITTED; atomic multi-artifact promotion; fault-injection matrix green.
- [ ] Windows-safe operation_id.
- [ ] Dedicated `gitops.py`; prompt templates; production config.yaml + validation.
- [ ] Codex stdin prompt; cwd=worktree; output=staging; read-only; planner-result wrapper.
- [ ] Grok full Review Bundle; read-only; HEAD/clean rechecked.
- [ ] Verification executable/cmd_script/bash_script; honest network policy.
- [ ] Approval: run_id; configured confirmation; fallback loud or fails.
- [ ] Hooks on real spike contract; Stop shares engine `check_stop`.
- [ ] AGENTS.md never overwritten; resolved context under control_root.
- [ ] Engine: 9 tools, ALLOWED_FROM inside begin, artifact rehash, answers re-entry, context invariants, ≥11 complete checks, `check_stop`.
- [ ] Three-layer E2E: engine (real commit + rejection matrix), stdio transport, opt-in real-CLI smoke.
- [ ] Terminal `READY_TO_MERGE` with handoff report; **no push/merge/PR** anywhere.

## Appendix H — Explicit declaration: no auto push / merge

This plan contains **no** `git push`, `git merge`, or PR creation in any task, step, or commit message template. The v1 terminal state is `READY_TO_MERGE`. The run outputs a handoff report (branch, HEAD commit, changed files, test results, review result). Push/PR/merge is the user's decision, made **outside** AgentFlow. (Prompt §22; Review §六.10.)

## Appendix I — Plan self-review report

Run against the v2-review's "整体评价" must-fix list and the Prompt self-check:

| Must-fix (v2-review) | Result |
|---|---|
| Windows-safe operation_id | ✅ 2.3 (`__`, regex, safe-filename test) |
| gitops/prompts/config tasks added | ✅ 2.6 / 4.0 / 1.2 |
| Engine/Store nested lock eliminated | ✅ 3.4 two-phase + 6.1 guard-in-begin |
| active-run pointer replaces scan | ✅ 1.3 |
| single-task & BLOCKED protocol unified | ✅ 2.1 planner-result wrapper |
| topological task order | ✅ phases reordered linearly |

| Prompt self-check | Result |
|---|---|
| No local `mcp` clash | ✅ 1.1 asserts |
| Real FastMCP stdio | ✅ 1.5 |
| workflow_init receives target_repo | ✅ 1.3/1.6 |
| exactly-one-task (schema + business) | ✅ 2.1 + 6.3 (wrapper) |
| prompt/cwd/output separated | ✅ 4.2 |
| Review Bundle complete | ✅ 4.3 |
| manifest no self-hash | ✅ 2.5 |
| artifacts via staging commit | ✅ 3.4/3.5 |
| file lock consistent with commit order | ✅ 3.1/3.4 (single acquisition per begin/finish) |
| artifact rehash reads files | ✅ 6.1 |
| operation answers re-entry + UNKNOWN | ✅ 6.2 + 3.3 |
| E2E really edits & commits | ✅ 8.1 |
| no auto push | ✅ Appendix H; 6.6/8.4/8.5 assert |
| no invented ZCode API | ✅ every ZCode task depends on Phase 0 |

No placeholder strings (TBD/TODO/fill-in/Similar-to) in task bodies. Code is limited to `[CONTRACT]`/`[DATA]`/`[PSEUDO]` blocks. Phases are in dependency order; each task lists **Depends on**, tests-first, requirements, verification, done-when, and commit message.

---

**End of Plan v2.1.**
