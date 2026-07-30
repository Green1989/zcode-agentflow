# ZCode-AgentFlow v1 Implementation Plan v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a safe-to-execute plan that implements the Spec v3.1 v1 vertical slice: a single-task run driven from `NEW` to `READY_TO_MERGE` by a real MCP stdio server, with Codex planning, Grok reviewing, and two human gates.

**Architecture:** A Python package `zcode_agentflow` (src layout) exposing 9 MCP tools over FastMCP stdio. An `Engine` holds the workflow logic; a `Store` with a per-Run file lock holds recoverable state (`state.json` + append-only `events.jsonl` + idempotent `operations/`). Three adapters (Codex, Grok, Verification) run subprocess calls with raw capture. All artifacts are committed through operation staging under the four-roots model. No code is pre-written production logic — code blocks here are interface contracts, data structures, and key algorithm pseudocode only.

**Tech Stack:** Python 3.11+, `mcp` SDK (`FastMCP`, stdio), `jsonschema`, `pyyaml`, `portalocker`, `pytest`, `pytest-mock`. Subprocess calls to `codex exec` and `grok -p`. Windows/Git Bash primary; must stay cross-platform.

**Reference spec:** `docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md` (Approved for planning, v3.1).
**Review driving this rewrite:** `docs/review/2026-07-30-zcode-agentflow-review.md`.
**Do not modify:** the Spec, the original Plan (`2026-07-30-zcode-agentflow-v1.md`), or the Review.

---

## How to read this plan (IMPORTANT)

This plan is deliberately **not** a pile of copy-pasteable production code. The prior plan's failing was that its embedded implementations already carried architecture bugs (Codex received an empty stdin; Codex cwd was the control dir; manifest self-referenced its own hash; two JSON serializations diverged so recovery hashes mismatched). An executing agent copying those would ship latent bugs.

Therefore every task states, in order:
- **Goal / Depends on / Files** — what, why, where.
- **Tests first** — the exact test names and the behaviors they pin (TDD). Test bodies are given as behavioral specs, often with assertion pseudocode, because the executing agent must write tests that match real APIs discovered in Phase 0.
- **Implementation requirements** — the invariants the code must satisfy (interface contracts, data structures, key algorithms as pseudocode). The executing agent writes the real code to make the tests pass.
- **Verification command / expected result / done-when / commit message.**

Code blocks in this plan fall into exactly three categories, and each is labeled:
- `[CONTRACT]` — an interface or type the implementation must conform to (signature, data-structure shape).
- `[DATA]` — a literal file's required content (a schema, a config sample, a manifest).
- `[PSEUDO]` — an algorithm described as pseudocode to guide, not to copy verbatim.

If a task's behavior depends on an external product capability, that capability **must** be confirmed in the Phase 0 spike before the task runs. Unconfirmed capabilities are listed in the risk appendix and explicitly gate their dependents.

---

## File ownership

```
src/zcode_agentflow/                  ← the package (NOT mcp/ — avoids SDK name clash)
├── __init__.py                       version + public re-exports
├── config.py                         load config.yaml + capability flags (Spec §6)
├── paths.py                          four-roots model, path normalization, containment (Spec §2.2)
├── ids.py                            run_id / operation_id / branch / task_id validation & generation (Prompt §5)
├── canonical.py                      canonical_text_bytes / canonical_json_bytes / hash_file (Prompt §7)
├── schemas.py                        strict JSON Schema load + validate + cross-field invariants (Prompt §6, Spec §4.5)
├── locks.py                          per-Run file lock (portalocker) (Prompt §9)
├── store.py                          Run lock + state.json + events.jsonl + operations + commit_operation (Prompt §9,§10)
├── gitops.py                         git wrappers (worktree, HEAD, clean, diff -z) (Spec §2.2,§5.3)
├── adapters/
│   ├── __init__.py
│   ├── base.py                       Adapter protocol: invoke(request, repo_cwd, operation_dir, timeout) (Prompt §12)
│   ├── codex.py                      CodexAdapter (stdin prompt, read-only sandbox) (Prompt §12)
│   ├── grok.py                       GrokAdapter (Review Bundle, real read-only flags) (Prompt §13)
│   └── verification.py               VerificationAdapter (structured command, process-tree cleanup) (Prompt §16)
├── policy_change.py                  git change audit via git --name-status -z (Prompt §16)
├── approval.py                       trusted approval records + fallback warning (Prompt §17, Spec §5.4)
├── agents_context.py                 resolve user AGENTS.md + template → control_root (Prompt §19)
├── artifacts.py                      verify_locked_artifacts(state) rehash (Prompt §14)
├── redact.py                         secret redaction for all logged outputs (Prompt §20)
├── engine.py                         Engine: 9 tools, state-source table, artifact rehash, exactly-one-task (Prompt §15, Spec §3.1,§5.1)
├── server.py                         FastMCP stdio: 9 real tools, main() (Prompt §2, Spec §5.1)
└── bootstrap.py                      process config: plugin_root, user_data_root, config_path (Prompt §4)

schemas/                              ← plugin_root/schemas (strict; additionalProperties:false)
├── spec-envelope.schema.json
├── plan-envelope.schema.json         tasks minItems:1, maxItems:1 (Prompt §6)
├── execution-result.schema.json
└── review-result.schema.json

prompts/                              ← plugin_root/prompts
├── planner-draft.md
├── planner-plan.md
└── reviewer.md                       references the Review Bundle (Prompt §13)

hooks/                                ← plugin_root/hooks (real shape from Phase 0 spike)
├── <spike-confirmed name>.json
├── protect_artifacts.py
└── gate_on_stop.py                   calls engine read-only check_stop (Prompt §18)

tests/                                mirror of src/, plus integration + e2e
├── conftest.py                       tmp control/worktree/repo fixtures; real git seeds
├── unit/                             one file per module, fast, monkeypatch-free where possible
├── adapters/                         fake-executable integration tests (Prompt §12,§13)
├── store/                            fault-injection recovery matrix (Prompt §3,§10)
├── mcp/                              real stdio initialize/tools/list/tools/call (Prompt §2)
└── e2e/                              three-layer e2e (Prompt §21)

pyproject.toml                        src layout, deps, pytest config
config.yaml                           plugin config (Spec §6), network policy = advisory_only or removed
docs/capabilities.json                Phase 0 spike output (schemas/samples only; no secrets/paths)
.zcode-plugin/plugin.json             real manifest shape from Phase 0 spike
skills/agentflow/SKILL.md
commands/agentflow-start.md, agentflow-status.md
AGENTS.md.template                    → resolved-agent-context.md at runtime (Prompt §19)
```

**Decomposition principle:** one responsibility per file; the `engine.py` never touches git/subprocess directly (it composes `gitops` + adapters + `store`); the `store.py` is the *only* component that mutates `state.json`/`events.jsonl`/`operations/`, and only while holding the Run lock.

**Build order (why this order):** the most failure-prone external integrations (ZCode plugin, MCP stdio, Codex/Grok CLIs) are validated **first** (Phase 0), then the transport is stood up **before** deep logic (Phase 1), so that wrong assumptions are caught before thousands of lines are written. Strict schemas and canonical bytes (Phase 2) are prerequisites for the recoverable store (Phase 3), which is a prerequisite for adapters (Phase 4) and the engine (Phase 5). E2E (Phase 7) comes last but each phase has its own exit-gate tests.

---

## Phase exit / entry conditions (applies to all phases)

Every phase lists explicit **Entry conditions** (what must already be true) and **Exit conditions** (the verifiable state that unlocks the next phase). A phase is not done until its Exit conditions hold. Dependencies form a DAG — see the dependency graph in the appendix.


## Phase 0 — Real Capability Spike

> **Purpose:** prove every external product assumption before any logic is written. Findings here override anything assumed elsewhere in this plan. This phase is **exploration + a committed findings file**, not production code.

**Entry conditions:** local `codex` and `grok` CLIs are installed and authenticated (ChatGPT / xAI account login, per Spec); `git` works; a ZCode client is available to load a test plugin.

**Exit conditions (all must hold before Phase 1):**
1. A minimal MCP echo server (see Task 0.1) is loadable by ZCode, and one of its tools is callable over stdio.
2. The real ZCode plugin manifest format, MCP command field, and hook event names/payload/cwd/exit-code are captured in `docs/capabilities.json`.
3. It is confirmed (true/false) whether ZCode supports MCP tool-level mandatory human confirmation.
4. `codex exec` is observed reading a prompt from stdin, with cwd at a sample repo, and writing a JSON-shape output to an output dir.
5. `grok -p` is observed running read-only against a sample Review Bundle and emitting parseable JSON; the exact read-only flags in use are recorded.
6. A committed `docs/capabilities.json` holds the above (schemas / samples only — **no** account tokens, **no** absolute machine paths; real run logs live in a control_root that is gitignored).

> Spike scripts and captured raw outputs are written **outside** the repo (a temp control_root) and listed in `.gitignore`. Only `docs/capabilities.json` (sanitized) is committed.

### Task 0.1: Spike harness — minimal FastMCP echo server loadable by ZCode

**Goal:** determine the *actual* way ZCode loads an MCP server and calls a tool, and capture the manifest field names. Do **not** assume the SDK import path or the manifest JSON shape — confirm them.

**Depends on:** nothing.
**Files:**
- Create (throwaway, under a temp control_root, NOT committed): `spike/echo/server.py`, `spike/echo/plugin.json`
- Create (committed): `docs/capabilities.json` (incrementally filled across Phase 0)

**Tests first:** This is a spike; the "test" is a manual reproduction checklist recorded in `docs/capabilities.json`. No pytest here. The checklist:
- [ ] Launch the echo server directly via `python -m` / entrypoint and confirm it speaks MCP over stdio (responds to `initialize`).
- [ ] Point ZCode at it using the manifest shape discovered in the ZCode plugin docs; confirm the tool appears and returns a value.
- [ ] Record the **exact** manifest field names observed (e.g. is it `mcp.command`? `mcp.server`? an args array? env block?).

**Implementation requirements (the spike server):**
`[PSEUDO]` Build the smallest FastMCP server that exposes one tool `echo(text) -> {"echo": text}` and runs on stdio. Try the documented import path `from mcp.server.fastmcp import FastMCP`; if the installed SDK differs, record the real path in capabilities. Provide a `main()` and `if __name__ == "__main__": main()`.

**Verification command / expected:**
- Direct: start the server process, send an MCP `initialize` then `tools/call echo`, observe `{"echo": ...}`.
- In ZCode: the tool appears in the agent's tool list and is callable.
**Done when:** both observed, and the real manifest + SDK import path are written into `docs/capabilities.json` under a `zcode` object with fields: `plugin_loaded: bool`, `mcp_stdio: bool`, `manifest_fields: {...actual...}`, `sdk_import_path: str`.

**Commit message:** `spike: minimal FastMCP echo server loadable by ZCode (capabilities recorded)`

### Task 0.2: Spike — ZCode Hook payload, cwd, exit semantics, and tool-confirmation capability

**Goal:** capture the *real* hook contract so Phase 6 hooks aren't built on a guessed payload. Three unknowns: (a) which event names fire (PreToolUse / Stop / others) and their stdin JSON shape; (b) the cwd the hook command runs in and how to address the plugin root; (c) whether ZCode supports mandatory human confirmation for a specific MCP tool.

**Depends on:** Task 0.1 (a loadable plugin to attach hooks to).
**Files:** throwaway hook scripts under temp control_root; results → `docs/capabilities.json`.

**Tests first (manual checklist):**
- [ ] Register a hook on a known event; have it write its `sys.argv`, `os.getcwd()`, full stdin bytes, and any env vars (names only, redact values) to a temp file.
- [ ] Trigger the event from ZCode (e.g. an Edit) and from a Stop; capture both.
- [ ] Attempt to configure a tool requiring human confirmation; record whether ZCode enforces it (true/false) and the config field name if so.

**Implementation requirements:** `[PSEUDO]` tiny Python that dumps its execution context to a log file, exits 0, then variants that exit non-zero to learn the deny semantics; a second variant that emits JSON on a "feedback" channel if one exists.

**Verification / expected:** the capture files exist and are non-empty; their *structure* (keys present) is summarized (values redacted) into capabilities.
**Done when:** `docs/capabilities.json` `zcode.hooks` records: `events_observed: [..]`, `stdin_shape: {..keys..}`, `hook_cwd: <how plugin root is reachable>`, `exit_code_denies: <code>`, `feedback_channel: <field or none>`, `stop_reentry_limit: <n or unknown>`, `tool_confirmation_supported: bool`.
**Commit message:** `spike: capture ZCode hook payload/cwd/exit + tool-confirmation capability`

### Task 0.3: Spike — Codex CLI: stdin prompt, read-only, cwd, schema output

**Goal:** confirm Codex actually receives the prompt via stdin, runs read-only with cwd at the repo, and writes a JSON output we can validate. Resolves Review P0-3 and P0-4 at the source.

**Depends on:** Task 0.1 (need a sample repo + output dir convention).
**Files:** throwaway under temp control_root; results → capabilities.

**Tests first (manual checklist):**
- [ ] Create a tiny sample git repo with one file.
- [ ] Invoke `codex exec` with: prompt on **stdin** (the trailing `-`), `--sandbox read-only`, cwd = sample repo, `--output-schema` pointing at a tiny schema, `-o` an output path in an **operation dir** separate from the repo.
- [ ] Verify: (a) Codex's response reflects the *actual repo contents* (proves cwd + stdin both work); (b) the repo is unchanged (read-only held); (c) the output file is valid JSON matching the schema.
- [ ] Record the exact flags that worked and any flag that didn't (e.g. does `model_reasoning_effort=high` via `--config` take effect for this account/model?).

**Implementation requirements:** `[PSEUDO]` one shell invocation with the prompt piped on stdin; keep prompt, cwd, and `-o` output dir as three distinct concepts (this is the contract Phase 4 implements).

**Verification / expected:** Codex output references repo content; repo `git status` clean; output file parses.
**Done when:** `docs/capabilities.json` `codex` records: `command: "codex"`, `stdin_prompt_works: bool`, `read_only_held: bool`, `cwd_is_repo: bool`, `output_schema_works: bool`, `reasoning_effort_high_supported: bool`, `working_flags: {...}`.
**Commit message:** `spike: codex stdin prompt + read-only cwd + schema output confirmed`

### Task 0.4: Spike — Grok CLI: read-only Review Bundle + JSON output

**Goal:** confirm Grok can read a complete Review Bundle read-only and emit parseable JSON, and capture the *real* read-only flags (do not invent `--sandbox`/`--allow/--deny` — observe them).

**Depends on:** Task 0.1.
**Files:** throwaway under temp control_root; results → capabilities.

**Tests first (manual checklist):**
- [ ] Assemble a minimal Review Bundle dir: `SPEC.md`, `PLAN.md`, `task-manifest.json`, `execution-result.json`, `diff.patch`, `review-request.json`, `prompt.md`.
- [ ] Invoke `grok -p` pointing at the bundle, with whatever read-only flags the installed CLI actually supports, requesting a review-result JSON.
- [ ] Verify: Grok's JSON parses and references SPEC/PLAN content (proves it read the bundle, not just hashes); the bundle is unchanged; cwd and bundle path are distinct concepts.
- [ ] Capture the real flag names that produced read-only behavior.

**Implementation requirements:** `[PSEUDO]` the bundle layout above is the contract Phase 4's `GrokAdapter` will assemble; the spike only proves Grok can consume it read-only.

**Verification / expected:** Grok JSON parses and cites SPEC/PLAN; bundle files unchanged.
**Done when:** `docs/capabilities.json` `grok` records: `command: "grok"`, `readonly_flags: [...]`, `json_output_shape: "review-result"`, `bundle_consumed: bool`, `cwd_vs_bundle_separated: bool`.
**Commit message:** `spike: grok read-only Review Bundle + JSON output confirmed`

### Task 0.5: Lock the capability findings + gitignore spike artifacts

**Goal:** make `docs/capabilities.json` the single source of truth that gates every dependent task, and ensure no spike secrets/paths leak into the repo.

**Depends on:** Tasks 0.1–0.4.
**Files:**
- Create/extend: `.gitignore` (add spike temp dir + any control_root patterns)
- Finalize: `docs/capabilities.json`

**Implementation requirements:** `[DATA]` `docs/capabilities.json` must contain a top-level `gates` object whose boolean fields each downstream phase checks as an entry condition (e.g. `gates.phase1_ok = zcode.plugin_loaded && zcode.mcp_stdio`). Any capability recorded `false` or `unknown` becomes a tracked risk (see appendix) and blocks its dependents until resolved by the user.

**Verification / expected:** `git status` shows only `docs/capabilities.json` and `.gitignore` staged; `grep`-level check that no absolute path or token-looking string appears in the committed JSON.
**Done when:** capabilities file committed and the Phase 0 exit conditions above all hold.
**Commit message:** `spike: lock capability findings; gitignore spike artifacts`

---


## Phase 1 — Project skeleton + real MCP stdio transport

**Entry conditions:** Phase 0 exit conditions hold; `docs/capabilities.json` records the real `mcp` SDK import path and ZCode manifest fields.
**Exit conditions:**
1. `src/zcode_agentflow/` is an installed (editable) package; `from mcp.server.fastmcp import FastMCP` imports the **official** SDK, never a local shadow (no local `mcp/` exists anywhere).
2. A real FastMCP server starts on stdio and exposes at least one tool; an integration test performs `initialize` → `tools/list` → `tools/call` over stdio.
3. `workflow_init` accepts `target_repo` at runtime (no env-var repo binding); run_id / control_root / worktree_root are derived by the server.
4. Run model chosen and implemented (A or B, below) with a defined restart-recovery path.

### Task 1.1: src-layout package + pyproject + deps (resolves Review P0-1)

**Goal:** establish the package under a name that cannot clash with the `mcp` SDK, with real dependency pins including the file-lock library.

**Depends on:** Phase 0.
**Files:**
- Create: `pyproject.toml`, `src/zcode_agentflow/__init__.py`
- Create: `tests/__init__.py`, `tests/conftest.py`
- Extend: `.gitignore` (build/venv/`__pycache__`)

**Tests first:**
- [ ] `tests/unit/test_packaging.py`:
  - `test_no_local_mcp_shadow()` — assert there is **no** directory named `mcp` at the repo root (guards the original bug).
  - `test_official_mcp_sdk_imports()` — `import mcp` resolves to the installed SDK location (assert the resolved file path is under site-packages / the installed dist, not under the repo). Use `importlib.util.find_spec("mcp").origin` and assert the repo root is not a prefix.
  - `test_package_imports()` — `from zcode_agentflow import __version__` works and equals `"0.1.0"`.
  - `test_fastmcp_imports()` — `from mcp.server.fastmcp import FastMCP` succeeds (using the import path recorded in `docs/capabilities.json`; if the spike found a different path, use that).

**Implementation requirements:**
`[DATA]` `pyproject.toml`: `[build-system]` (setuptools), `[project] name="zcode-agentflow"`, version `0.1.0`, `requires-python >=3.11`, `dependencies = ["mcp>=1.2.0", "jsonschema>=4.21", "pyyaml>=6.0", "portalocker>=2.7"]`; `[project.scripts]` `zcode-agentflow = "zcode_agentflow.server:main"`; `[tool.setuptools.packages.find] where=["src"]`; pytest config under `[tool.pytest.ini_options]` with `testpaths=["tests"]` and `pythonpath=["src"]` (so tests import `zcode_agentflow` and the official `mcp` — **no `sys.path` hacks in test code**).
`[DATA]` `tests/conftest.py`: fixtures `control_root(tmp_path)`, `worktree_root(tmp_path)`, `fake_repo(tmp_path)` (a real `git init` + initial commit; shared by many phases), and a `capabilities` fixture loading `docs/capabilities.json`.

**Verification command:** `python -m pip install -e . && python -m pytest tests/unit/test_packaging.py -v`
**Expected:** 4 passed.
**Done when:** all four tests pass; `python -c "import mcp, zcode_agentflow; print(mcp.__file__)"` prints a site-packages path.
**Commit message:** `build: src-layout zcode_agentflow package, pyproject, file-lock dep (P0-1)`

### Task 1.2: Run model + bootstrap (resolves Prompt §4, Review §三)

**Goal:** decide and implement the run model. **Decision for v1: Model A — one active run per MCP process**, with `workflow_init` taking `target_repo` at runtime and the server deriving run_id/control_root/worktree_root under a `user_data_root`. Justification: v1 is a single-task vertical slice; a registry adds complexity with no v1 payoff. Restart recovery is defined (Task 1.3) so Model A is safe.

**Depends on:** Task 1.1.
**Files:** Create `src/zcode_agentflow/bootstrap.py`, `src/zcode_agentflow/ids.py` (run_id/operation_id generation + validation; full strict validation lands in Phase 2 Task 2.3, this task uses minimal generation), `src/zcode_agentflow/config.py`.

**Tests first (`tests/unit/test_bootstrap.py`):**
- `test_workflow_init_signature_takes_target_repo()` — the public `workflow_init` callable's parameters include `target_repo` and `goal`; assert `run_id`/`control_root`/`worktree_root` are **not** required inputs (they're derived).
- `test_derive_paths_under_user_data_root()` — given a `user_data_root` and a generated `run_id`, derived `control_root = user_data_root/runs/<run_id>/control` and `worktree_root = user_data_root/runs/<run_id>/worktree`; assert both are outside any passed-in `target_repo`.
- `test_no_env_var_repo_binding()` — bootstrap reads only `plugin_root`/`user_data_root`/`config_path` (assert it does **not** read `AGENTFLOW_TARGET_REPO`/`AGENTFLOW_CONTROL_ROOT`/`AGENTFLOW_WORKTREE_ROOT`).
- `test_run_id_format()` — generated run_id matches a strict pattern (timestamp + suffix), is filesystem- and git-safe.

**Implementation requirements:**
`[CONTRACT]`
```
# bootstrap.py
def process_config() -> ProcessConfig: ...   # plugin_root, user_data_root, config_path
def derive_run_paths(*, user_data_root, run_id) -> RunPaths: ...  # control_root, worktree_root, branch
```
`[PSEUDO]` `user_data_root` resolved from config (default `<plugin_root>/../agentflow-data` or a platform user-data dir); `run_id = f"{YYYYMMDDHHMMSS}-{6 hex}"`; `branch = f"agentflow/{run_id}"` (branch/dirname normalization is formalized in Phase 2).
`[DATA]` `config.py`: `load(config_path) -> dict` merges Spec §6 defaults; **network policy** is either `network_policy: advisory_only` or omitted — never a `network: deny` that does nothing (resolves Review §六.3 / Prompt §16). Add `capabilities` block sourced from `docs/capabilities.json`.

**Verification command:** `python -m pytest tests/unit/test_bootstrap.py -v`
**Expected:** 4 passed.
**Done when:** run model A implemented; target_repo is a runtime arg; no repo env-var binding.
**Commit message:** `feat: runtime target_repo + derived run paths (Model A active run) (§4, Review §三)`

### Task 1.3: Active-run recovery across process restart (Prompt §4)

**Goal:** define how Model A survives an MCP process restart (e.g. ZCode restarts the server mid-run). The server must locate and resume the active run, not start blank.

**Depends on:** Task 1.2.
**Files:** Extend `src/zcode_agentflow/bootstrap.py`; create `src/zcode_agentflow/store.py` **stub only** (the full Store is Phase 3 — here just an `active_run(user_data_root) -> RunPaths | None` reader that scans `runs/*/control/state.json` for the newest non-terminal run). Keep this thin; Phase 3 replaces internals.

**Tests first (`tests/unit/test_active_run_recovery.py`):**
- `test_no_active_run_returns_none()` — fresh `user_data_root` ⇒ `None`.
- `test_recovers_newest_non_terminal_run()` — seed two runs (one `READY_TO_MERGE` terminal, one `EXECUTING_TASK`); recovery returns the non-terminal one.
- `test_corrupt_state_is_skipped_not_crash()` — a `runs/x/control/state.json` with garbage is skipped; recovery returns the next viable run or None.

**Implementation requirements:** `[PSEUDO]` list `user_data_root/runs/*/control/state.json`; parse each defensively; pick the newest whose `state` ∉ terminal set `{READY_TO_MERGE, MERGED}`; on any parse error skip. (Full state schema lives in Phase 3; here treat it as opaque JSON with a `state` string.)

**Verification command:** `python -m pytest tests/unit/test_active_run_recovery.py -v`
**Expected:** 3 passed.
**Done when:** server can resume an in-flight run after restart.
**Commit message:** `feat: active-run recovery across MCP restart (§4)`

### Task 1.4: Real FastMCP stdio server with 1 live tool (resolves Review P0-2)

**Goal:** stand up a genuine FastMCP server over stdio and prove the MCP protocol works end-to-end, using `workflow_status` as the first real tool. No `dispatch()`-only layer.

**Depends on:** Tasks 1.2, 1.3.
**Files:** Create `src/zcode_agentflow/server.py`; create `tests/mcp/test_stdio_protocol.py`.

**Tests first (`tests/mcp/test_stdio_protocol.py`):** — these are **real subprocess** integration tests:
- `test_initialize_handshake()` — spawn `python -m zcode_agentflow.server` (or the console script), send a JSON-RPC `initialize`, assert a valid `InitializeResult` with `protocolVersion` and `serverInfo`.
- `test_tools_list_contains_workflow_status()` — `tools/list` returns a list containing `workflow_status` with an input schema.
- `test_tools_call_workflow_status()` — `tools/call workflow_status` (no active run) returns a structured result (e.g. `{"active_run": null}`) — proves the tool executes, not just that it's listed.
- `test_mcp_error_mapping()` — calling a tool with wrong-typed args returns an MCP error result (not a crash/traceback leak).

`[PSEUDO]` helper: a small client that writes newline-delimited JSON-RPC frames to the child's stdin, reads frames from stdout, with a timeout.

**Implementation requirements:**
`[CONTRACT]`
```
# server.py
from mcp.server.fastmcp import FastMCP     # OFFICIAL sdk path from capabilities
mcp_server = FastMCP("zcode-agentflow")

@mcp_server.tool()
def workflow_status() -> dict: ...          # returns active-run snapshot or {"active_run": None}

def get_engine() -> "Engine": ...           # wired in Phase 5; here returns a stub that knows active-run status
def main() -> None:
    mcp_server.run(transport="stdio")

if __name__ == "__main__":
    main()
```
`[PSEUDO]` tool registration uses FastMCP's decorator with typed Python signatures so the input schema is auto-derived; each tool delegates to `get_engine()` once the engine exists (Phase 5). Error mapping: catch `WorkflowError`/`CasError` and return them as MCP error results.

**Verification command:** `python -m pytest tests/mcp/test_stdio_protocol.py -v`
**Expected:** 4 passed.
**Done when:** a real stdio MCP round-trip works and `workflow_status` is live. The remaining 8 tools are registered in Phase 5, but the transport is proven now.
**Commit message:** `feat: real FastMCP stdio server + workflow_status tool (P0-2)`

### Task 1.5: Register the remaining 8 tool signatures (no logic yet)

**Goal:** lock the 9-tool surface and parameter schemas now so Phases 5 just fills bodies. Each tool is registered with its real signature incl. `expected_revision` where the spec requires CAS.

**Depends on:** Task 1.4.
**Files:** Extend `src/zcode_agentflow/server.py`; extend `tests/mcp/test_stdio_protocol.py`.

**Tests first:**
- `test_tools_list_has_all_nine()` — `tools/list` contains exactly: `workflow_status, workflow_init, planner_draft_spec, workflow_approve_spec, planner_create_plan, workflow_start_task, workflow_verify_task, reviewer_review_task, workflow_complete_task`.
- Per-tool param-schema assertions: e.g. `workflow_init` requires `target_repo`, `goal`, optional `run_id`; `planner_create_plan` requires `approved_spec_sha`, `expected_revision`; `workflow_complete_task` requires `task_id`, `expected_revision`. Assert presence/required-ness from each tool's `inputSchema`.

**Implementation requirements:** `[CONTRACT]` the exact signatures from Spec §5.1 / Prompt §15 (incl. `planner_draft_spec(goal, answers=None, expected_revision=None)` for re-entry). Each tool body currently raises `NotImplementedError` (or calls a not-yet-implemented engine method) — logic lands in Phase 5. **Do not** implement logic here.

**Verification command:** `python -m pytest tests/mcp/test_stdio_protocol.py -v`
**Expected:** previous 4 + 1 (nine tools) + param-schema tests all pass.
**Done when:** all nine tools are declared with correct parameter schemas over real stdio; Phase 1 exit conditions hold.
**Commit message:** `feat: declare all 9 MCP tool signatures (logic deferred to Phase 5)`

---


## Phase 2 — Strict schemas + canonical bytes + IDs/roots

**Entry conditions:** Phase 1 complete; FastMCP import path stable.
**Exit conditions:**
1. The four schemas exist and are strict (`additionalProperties:false`, full nested item schemas, `hash` = 64 lowercase hex, `decision` enum, `issues` fully defined, `verification_commands` structured, `tasks` minItems:1 **maxItems:1**, `change_audit` typed).
2. `canonical_text_bytes` / `canonical_json_bytes` / `hash_file` exist and pass the full edge-case suite (no/one/many trailing newlines, CRLF, non-ASCII, invalid UTF-8, pretty-vs-compact JSON must not be assumed equal).
3. `ids.py` validates run_id/operation_id/task_id and normalizes branch/dir names; `paths.py` implements the four-roots model with containment; the task manifest carries **no self-referential hash** (Prompt §8).

### Task 2.1: Strict four schemas (resolves Review P0-8, §六.9; Prompt §6)

**Goal:** author the four JSON Schemas to the strictness bar before any Engine code, so Engine validation is mechanical.

**Depends on:** Phase 1.
**Files:**
- Create `schemas/spec-envelope.schema.json`, `schemas/plan-envelope.schema.json`, `schemas/execution-result.schema.json`, `schemas/review-result.schema.json`
- Create `src/zcode_agentflow/schemas.py`
- Create `tests/unit/test_schemas.py`

**Tests first (`tests/unit/test_schemas.py`):** each schema gets a "valid sample passes" and several "invalid sample rejected" cases. Required rejections to assert:
- *spec-envelope:* missing `spec_markdown` rejected; extra unknown field rejected (additionalProperties:false).
- *plan-envelope:* `tasks` with 0 items rejected **and** with 2 items rejected (maxItems:1); a task missing `verification_commands` rejected; `verification_commands` item missing `program` rejected.
- *execution-result:* `status` outside enum rejected; `change_audit.dependency_files_changed` given a string when schema requires boolean rejected (this nails the Review §六.9 type-drift bug).
- *review-result:* `decision=PASS` with non-empty `issues` rejected **at the invariant layer** (not just schema); `decision=FAIL` with empty `issues` rejected; `decision=REPLAN` without `replan_required=true` rejected; a `spec_sha256` that isn't 64 lowercase hex rejected; an `issues[*].severity` outside the enum rejected.

**Implementation requirements:**
`[DATA]` schema details (all four set `"additionalProperties": false`):
- Shared definitions: `hash = {"type":"string","pattern":"^[a-f0-9]{64}$"}`; `commit = {"type":"string","pattern":"^[0-9a-f]{7,40}$"}`; `task_id = {"type":"string","pattern":"^T[0-9]{3}(-R[0-9]+)?$"}`.
- *plan-envelope* `tasks`: `{"type":"array","minItems":1,"maxItems":1,"items":{...}}` — **hard single-task at schema level**. Each task item requires: `id` (task_id pattern), `title`, `dependencies` (array of task_id), `declared_files` (array of strings), `verification_commands` (array of objects each requiring `program`(str), `args`(array str), `cwd`(str), `timeout_sec`(int ≥1)), `acceptance_criteria` (array str), `planned_changes` (object with `may_add_dependencies`(bool), `may_run_migrations`(bool), `may_delete_files`(array str)).
- *review-result* `issues[*]` requires: `id`(str), `severity`(enum high|medium|low), `category`(enum SPEC_VIOLATION|PLAN_VIOLATION|TEST_GAP|OTHER), `file`(str), `line`(int≥1), `description`(str), `required_action`(str).
- *execution-result* `change_audit` requires: `deleted_files`(array str), `dependency_files_changed`(bool), `migrations_added`(array str), `undeclared_files_changed`(array str) — **all fixed types** (no list-where-bool expected).
`[CONTRACT]`
```
# schemas.py
class SchemaError(Exception): ...
class InvariantError(Exception): ...
def validate_against(obj, schema_name) -> None       # jsonschema.validate, raise SchemaError on fail
def validate_spec(envelope) -> None                   # validate_against + envelope invariants
def validate_plan(envelope) -> None                   # + assert len(tasks)==1 (belt-and-suspenders)
def validate_execution(obj) -> None
def validate_review(obj) -> None                      # + cross-field invariants (Prompt §6 / Spec §4.5)
```
Cross-field invariants enforced in `validate_review`: PASS ⇒ issues empty ∧ all compliance flags true; FAIL ⇒ ≥1 issue; REPLAN ⇒ replan_required true. Engine also re-checks `len(tasks)==1` at the business layer (Phase 5).

**Verification command:** `python -m pytest tests/unit/test_schemas.py -v`
**Expected:** all valid-pass and invalid-reject cases green.
**Done when:** four strict schemas + validators land; the single-task and type-drift bugs are now structurally impossible.
**Commit message:** `feat: strict four schemas + cross-field invariants (P0-8, §六.9, Prompt §6)`

### Task 2.2: Canonical bytes (resolves Review §六.1; Prompt §7)

**Goal:** one canonicalization implementation used everywhere; hashing is always over the **actual written bytes**.

**Depends on:** Task 1.1.
**Files:** Create `src/zcode_agentflow/canonical.py`, `tests/unit/test_canonical.py`.

**Tests first (`tests/unit/test_canonical.py`):**
- `test_single_trailing_newline_added()` — `canonical_text_bytes(b"body") == b"body\n"`.
- `test_multiple_trailing_newlines_collapsed()` — `canonical_text_bytes(b"body\n\n\n") == b"body\n"`.
- `test_crlf_and_cr_to_lf()` — `"a\r\nb\rc\n"` canonicalizes to `"a\nb\nc\n"`.
- `test_non_ascii_utf8_preserved()` — Chinese/emoji round-trip exactly.
- `test_invalid_utf8_raises()` — bytes that aren't valid UTF-8 raise `UnicodeDecodeError` (canonicalization must **validate**, not silently replace).
- `test_pretty_and_compact_json_NOT_equal()` — given the same object, `canonical_json_bytes(obj)` is well-defined (deterministic), but assert that a *hand-pretty* serialization and a *compact* one fed to `canonical_text_bytes` are **not** assumed equal — the lesson is "always use canonical_json_bytes for JSON, never ad-hoc dumps".
- `test_hash_file_matches_canonical_of_written_bytes()` — write via the canonical writer, then `hash_file` equals `sha256` of exactly those bytes (no re-normalization surprise).

**Implementation requirements:**
`[CONTRACT]`
```
# canonical.py
def canonical_text_bytes(raw: bytes) -> bytes        # decode utf-8 strict; \r\n|\r -> \n; ensure exactly one trailing \n
def canonical_json_bytes(obj) -> bytes                # json.dumps(ensure_ascii=False, sort_keys=True, separators=(",",":")) then canonical_text_bytes
def write_canonical_text(path, data) -> None          # os.replace atomic; writes canonical_text_bytes
def write_canonical_json(path, obj) -> None           # writes canonical_json_bytes
def sha256_hex(data: bytes) -> str                    # hexdigest lower
def hash_file(path) -> str                            # sha256_hex(canonical_text_bytes(path.read_bytes())) — hashing ALWAYS canonical
```
`[PSEUDO]` ensure-trailing-newline: strip trailing `\n*` then append one `\n`. UTF-8 strict decode happens first (before any transform) so invalid bytes fail loudly. **Critical rule (Prompt §7):** JSON is *only* ever serialized via `canonical_json_bytes`; any code hashing JSON uses the bytes this function produces — closing the Review P0-6 mismatch.

**Verification command:** `python -m pytest tests/unit/test_canonical.py -v`
**Expected:** 7 passed.
**Done when:** single canonical implementation; all edge cases green; hashing rule established.
**Commit message:** `feat: canonical text/json bytes with strict UTF-8 + single trailing newline (§六.1, Prompt §7)`

### Task 2.3: IDs + branch/dirname normalization (Prompt §5)

**Goal:** all identifiers are generated/validated by MCP; untrusted model input is rejected; branch names and directory names are normalized independently.

**Depends on:** Task 1.1.
**Files:** Create `src/zcode_agentflow/ids.py`, `tests/unit/test_ids.py`.

**Tests first (`tests/unit/test_ids.py`):**
- `test_run_id_pattern()` — generated run_id matches `^\d{14}-[0-9a-f]{6}$` and is git-ref-safe (no `/`, `\`, `..`, `:`).
- `test_operation_id_encodes_run_tool_revision_attempt()` — `operation_id` for draft attempt 2 at revision 4 of run R encodes all four; same inputs ⇒ same id (deterministic); different attempt ⇒ different id.
- `test_task_id_pattern_strict()` — `T001`, `T004-R1` accepted; `t001`, `T01`, `T001;rm` rejected.
- `test_branch_name_rejects_path_separators_and_dotdot()` — `normalize_branch("agentflow/2026..x")` rejected/raised; valid run_id yields `refs/heads/agentflow/<run_id>`.
- `test_dirname_rejects_separators()` — a run_id cannot produce a dirname containing `/` or `\` on Windows.
- `test_id_rejects_traversal_substrings()` — any id/dirname containing `..`, path separators, NUL rejected.

**Implementation requirements:**
`[CONTRACT]`
```
# ids.py
def new_run_id() -> str
def operation_id(*, run_id, tool, revision, attempt) -> str   # e.g. f"{run_id}:{tool}:r{revision}:a{attempt}"
def validate_task_id(s) -> str          # raise on violation
def normalize_branch(prefix, run_id) -> str   # raise on unsafe
def normalize_dirname(run_id) -> str
```
`[PSEUDO]` reject patterns: any of `..`, `/`, `\`, `:`, NUL, control chars, spaces in dirnames; branch must additionally pass a `git check-ref-format`-equivalent rule (the implementation may shell out to `git check-ref-format --branch`).

**Verification command:** `python -m pytest tests/unit/test_ids.py -v`
**Expected:** 6 passed.
**Done when:** identifiers are safe and self-describing; traversal impossible.
**Commit message:** `feat: safe id generation + branch/dirname normalization (Prompt §5)`

### Task 2.4: Four-roots model + containment (Spec §2.2)

**Goal:** the four roots are typed, normalized to absolute, and every file op is containment-checked.

**Depends on:** Tasks 2.2, 2.3.
**Files:** Create `src/zcode_agentflow/paths.py`, `tests/unit/test_paths.py`.

**Tests first (`tests/unit/test_paths.py`):**
- `test_roots_normalize_to_absolute_and_distinct()` — relative inputs resolve absolute; the four roots are mutually non-containing (Prompt §11 preflight: "四根目录互不包含").
- `test_is_within_true_for_subpath_false_for_escape()` — nested file inside `run_worktree_root` passes; sibling dir and `..` attacks fail; case where `control_root` is inside `target_repo` is **rejected** at construction (roots must not nest).
- `test_worktree_and_control_outside_target_repo()` — asserting the Prompt §4 layout: worktree & control live under `user_data_root/runs/<run_id>`, never inside `target_repo`.

**Implementation requirements:**
`[CONTRACT]`
```
# paths.py
@dataclass(frozen=True) class Roots: plugin_root; target_repo_root; run_worktree_root; control_root
def make_roots(**) -> Roots           # raises if any root contains another
def is_within(path, base) -> bool     # resolve(strict=False) then relative_to
def assert_within(path, base) -> None # raise on escape
```

**Verification command:** `python -m pytest tests/unit/test_paths.py -v`
**Expected:** 3 passed.
**Done when:** roots are safe and mutually exclusive.
**Commit message:** `feat: four-roots model + containment (Spec §2.2)`

### Task 2.5: Task manifest without self-reference (Prompt §8)

**Goal:** the manifest hashed for review binding does **not** contain its own hash.

**Depends on:** Tasks 2.1, 2.2.
**Files:** Create `src/zcode_agentflow/manifest.py` (small helper), `tests/unit/test_manifest.py`.

**Tests first (`tests/unit/test_manifest.py`):**
- `test_manifest_has_no_self_hash_field()` — building a manifest from a plan-envelope yields an object with **no** `task_manifest_sha256` key.
- `test_hash_roundtrip()` — `hash_file(control_root/"task-manifest.json") == stored_manifest_sha` where the stored sha lives **in state / a sidecar**, not in the manifest file.
- `test_inject_for_review()` — `inject_manifest_sha(review_record, manifest_sha)` adds it to the review record (which is a *different* file).

**Implementation requirements:**
`[CONTRACT]`
```
# manifest.py
def build_manifest(plan_envelope) -> dict       # {plan_sha256, current_task, tasks:[...]}  NO self sha
def write_manifest(control_root, manifest) -> Path    # write_canonical_json
def manifest_sha_of(path) -> str                # hash_file (canonical)
```
The manifest sha is stored in `state.task_manifest_sha256` (Phase 3) and injected into the review record by the engine (Phase 5). It is never written into `task-manifest.json`.

**Verification command:** `python -m pytest tests/unit/test_manifest.py -v`
**Expected:** 3 passed.
**Done when:** self-reference bug structurally eliminated.
**Commit message:** `feat: self-reference-free task manifest (Prompt §8)`

---


## Phase 3 — Run lock + Store + operation staging + fault injection

**Entry conditions:** Phase 2 complete (canonical bytes, ids, schemas, paths, manifest).
**Exit conditions:**
1. A per-Run file lock (`portalocker`) guards **all** state mutation; in-memory revision CAS alone is insufficient.
2. `Store.commit_operation(...)` performs the **full** 12-step protocol under the lock; `state.json` is replaced **before** status=COMMITTED.
3. Event seq resumes from the last line (no restart reset); operations support PREPARED→RUNNING→COMMITTED/FAILED/UNKNOWN with idempotency on `(operation_id, request_sha256)`.
4. A fault-injection test matrix injects a crash at each step and asserts correct resume/continue/`NEEDS_HUMAN`.
5. `workflow_init` itself goes through this transactional path with preflight + safe compensation (no destructive auto-cleanup).

### Task 3.1: Per-Run file lock (resolves Review P0-7)

**Goal:** one lock per Run, held across every mutating operation.

**Depends on:** Task 2.4.
**Files:** Create `src/zcode_agentflow/locks.py`, `tests/unit/test_locks.py`.

**Tests first (`tests/unit/test_locks.py`):**
- `test_lock_is_exclusive_cross_process()` — spawn a second process that tries to acquire the same Run lock while the test holds it; the second acquires only after release (use a short timeout + a subprocess probe).
- `test_lock_released_on_exception()` — force an error inside the `with run_lock(...)` block; assert a subsequent acquire succeeds.
- `test_lock_path_under_control_root()` — lock file is `<control_root>/.agentflow.lock`.

**Implementation requirements:**
`[CONTRACT]`
```
# locks.py
@contextmanager
def run_lock(control_root, timeout=30):     # portalocker.Lock on .agentflow.lock, LOCK_EX
    ...
```
`[PSEUDO]` portalocker binary mode; raise `LockTimeout` on timeout; cleanup lock file on release if practical (portalocker handles cross-platform semantics). **Rule:** every state-mutating code path acquires `run_lock` first (enforced by code review + the Store being the sole mutator).

**Verification command:** `python -m pytest tests/unit/test_locks.py -v`
**Expected:** 3 passed.
**Done when:** cross-process exclusion proven.
**Commit message:** `feat: per-Run file lock via portalocker (P0-7)`

### Task 3.2: Events with resumable seq + state snapshot (resolves Review §六.2; Prompt §9)

**Goal:** `events.jsonl` seq resumes from disk; `state.json` is a snapshot only.

**Depends on:** Tasks 2.2, 3.1.
**Files:** Create `src/zcode_agentflow/store.py` (events + state portions), `tests/unit/test_events_state.py`.

**Tests first (`tests/unit/test_events_state.py`):**
- `test_seq_resumes_after_restart()` — append 3 events; construct a fresh EventLog on the same control_root; the next append gets seq=4 (not 1).
- `test_event_carries_recoverable_state_after()` — each event has `state_after_path` + `state_after_sha256` pointing at an immutable snapshot whose hash matches.
- `test_state_save_is_atomic_replace()` — concurrent readers never see a half-written `state.json` (assert via reading during a write in another thread/process).
- `test_load_state_roundtrips_all_fields()` — save → load equality over the full state shape (roots, hashes, commits, counters).

**Implementation requirements:**
`[CONTRACT]`
```
# store.py
class EventLog:
    def __init__(self, control_root): self._next_seq = self._last_seq_on_disk() + 1
    def append(..., *, state_after_path, state_after_sha) -> None   # atomic append + fsync, INSIDE run_lock
    def latest() -> dict | None
class State: ...                      # dataclass; save() uses write_canonical_json + os.replace
```
`[PSEUDO]` `_last_seq_on_disk()` reads the last non-empty line; seq increments **while holding run_lock**. State `save()` writes `state.tmp` → fsync → `os.replace`.

**Verification command:** `python -m pytest tests/unit/test_events_state.py -v`
**Expected:** 4 passed.
**Done when:** seq is durable; state is atomically replaced.
**Commit message:** `feat: resumable event seq + atomic state snapshot (§六.2, Prompt §9)`

### Task 3.3: Operation lifecycle + idempotency (Prompt §9, §10)

**Goal:** operations carry the full required file set and enforce idempotency rules.

**Depends on:** Tasks 2.2, 2.3, 3.1.
**Files:** Extend `src/zcode_agentflow/store.py` (operations), `tests/unit/test_operations.py`.

**Tests first (`tests/unit/test_operations.py`):**
- `test_prepare_creates_required_files()` — a prepared op dir contains: `request.json`, `request_sha256`, `status` (=PREPARED), and empty `raw/`.
- `test_idempotent_committed_same_request()` — same `(operation_id, request_sha256)` + COMMITTED ⇒ returns stored result without re-running.
- `test_conflict_different_request_rejected()` — same operation_id, different request_sha ⇒ `OperationConflict`.
- `test_running_without_result_is_unknown()` — RUNNING + no `result.json` ⇒ status UNKNOWN (no blind retry of side-effecting ops).
- `test_running_with_result_can_finish_commit()` — RUNNING + complete `result.json` ⇒ resume to COMMITTED.
- `test_full_file_set_present_on_commit()` — after commit, dir has `request.json, request_sha256, status, raw/stdout.txt, raw/stderr.txt, result.json, state_before.json, state_after.json, state_after_sha256`.

**Implementation requirements:**
`[CONTRACT]`
```
# store.py
class Operation:
    status: Literal["PREPARED","RUNNING","COMMITTED","FAILED","UNKNOWN"]
    def mark_running(); record_result(result); write_state_before(state); write_state_after(state) -> sha
class OperationManager:
    def prepare(operation_id, request) -> Operation
    def open_existing(operation_id) -> Operation     # classifies UNKNOWN vs resumable
```
`[PSEUDO]` request_sha = `sha256_hex(canonical_json_bytes(request))`. Recovery classification follows Prompt §10.

**Verification command:** `python -m pytest tests/unit/test_operations.py -v`
**Expected:** 6 passed.
**Done when:** idempotency + UNKNOWN semantics correct.
**Commit message:** `feat: operation lifecycle + idempotency + UNKNOWN (Prompt §9,§10)`

### Task 3.4: Store.commit_operation — the single transactional entry (resolves Review P0-6; Prompt §10)

**Goal:** exactly one method mutates durable state, performing the full 12-step protocol under the lock. This is where Review P0-6's split-protocol bug dies.

**Depends on:** Tasks 3.1, 3.2, 3.3.
**Files:** Extend `src/zcode_agentflow/store.py` (`Store.commit_operation`), `tests/store/test_commit_order.py`.

**Tests first (`tests/store/test_commit_order.py`):**
- `test_state_replaced_before_committed()` — using a fault injection point between "replace state.json" and "status=COMMITTED", simulate a crash; on resume the operation is re-committed and state reflects the new snapshot (no COMMITTED-but-stale-state window).
- `test_hash_computed_over_actual_written_bytes()` — `state_after_sha256` equals `hash_file(state_after.json)` using canonical hashing (closes P0-6's dual-serialization bug).
- `test_external_result_staged_then_promoted()` — an adapter writes a staged artifact under `operations/<id>/staging/`; on commit the artifact is atomically moved to its authoritative location (e.g. `control_root/SPEC.md`) **after** schema validation.
- `test_concurrent_commit_serializes()` — two threads attempting `commit_operation` on the same Run serialize via the lock; no interleaving.

**Implementation requirements:** `[PSEUDO]` the canonical 12-step sequence from Prompt §10, **all** JSON via `canonical_json_bytes`, all hashes via `hash_file` over actual bytes:
```
with run_lock(control_root):
    1. assert expected_revision == state.revision      # CAS under lock
    2. op = prepare(operation_id, request)
    3. op.mark_running()
    4. result = run_adapter_or_use_supplied(...)        # adapter writes to op staging dir
    5. validate staged artifacts (schemas)
    6. for each staged artifact: compute hash over actual bytes
    7. op.record_result(result); op.write_state_before(prev); op.write_state_after(next) -> sha
    8. event_log.append(state_after_path, sha)          # fsync
    9. promote staged artifacts to authoritative paths (os.replace)
    10. state.save()                                     # atomic replace
    11. op.status = COMMITTED
    12. return result
```
`[CONTRACT]` `Store.commit_operation(*, operation_id, request, expected_revision, prev_state, next_state, run_adapter=None, staged_artifacts=None) -> dict`. Adapters (Phase 4) receive the operation's staging dir and write there; promotion is the Store's job.

**Verification command:** `python -m pytest tests/store/test_commit_order.py -v`
**Expected:** 4 passed.
**Done when:** single transactional commit; ordering bug eliminated.
**Commit message:** `feat: Store.commit_operation single transactional commit (P0-6, Prompt §10)`

### Task 3.5: Fault-injection recovery matrix (Prompt §3, §10)

**Goal:** prove recovery for every crash point, including UNKNOWN→NEEDS_HUMAN for side-effecting ops.

**Depends:** Task 3.4.
**Files:** Create `tests/store/test_fault_injection.py` (a parametrized matrix).

**Tests first:** parametrize a "crash-after-step-N" across N=1..12; for each:
- Resume `Store` and assert: (a) final `state.json` consistent with `events.jsonl` latest snapshot; (b) the operation is either COMMITTED-and-idempotent (re-call returns same result) or classified UNKNOWN.
- For UNKNOWN operations flagged `has_side_effects=True` (e.g. verification that may have written files, or a codex/grok call already executed) ⇒ resume yields `NEEDS_HUMAN`, **not** an automatic retry.
- For UNKNOWN operations flagged `has_side_effects=False` (pure reads) ⇒ safe automatic replay.

**Implementation requirements:** `[PSEUDO]` a `crash_after_step` hook in `commit_operation` (test-only, behind an env flag) that raises after step N; the recovery routine `Store.recover(run_id)` rebuilds state from the latest event and classifies each non-COMMITTED operation.

**Verification command:** `python -m pytest tests/store/test_fault_injection.py -v`
**Expected:** full matrix green (12 crash points × expected outcomes).
**Done when:** every crash point has a defined, tested outcome.
**Commit message:** `test: fault-injection recovery matrix across 12 commit steps (Prompt §3,§10)`

### Task 3.6: Transactional workflow_init + preflight + safe compensation (Prompt §11)

**Goal:** init goes through `commit_operation` too; it cannot half-create a worktree and lie about state. Includes preflight and non-destructive recovery.

**Depends on:** Tasks 3.4, 2.4.
**Files:** Create `src/zcode_agentflow/preflight.py`, extend `src/zcode_agentflow/store.py` with an `init_run` path, `tests/unit/test_workflow_init_recovery.py`.

**Tests first (`tests/unit/test_workflow_init_recovery.py`):**
- `test_preflight_rejects_non_git_repo()` — `target_repo` without `.git` ⇒ clear error.
- `test_preflight_rejects_in_progress_merge()` — a repo with `.git/MERGE_HEAD` ⇒ rejected.
- `test_preflight_rejects_nesting_roots()` — control_root inside target_repo ⇒ rejected.
- `test_existing_control_root_same_run_is_idempotent()` — re-init of the same run_id whose state exists returns existing run, doesn't recreate worktree.
- `test_half_created_worktree_recovers_or_needs_human()` — simulate a crash after `git worktree add` but before COMMITTED; resume detects the worktree, claims it if branch ownership matches, else `NEEDS_HUMAN` (**never** auto-`git worktree remove`).
- `test_branch_already_exists_foreign()` — branch exists but doesn't belong to this run ⇒ `NEEDS_HUMAN`.

**Implementation requirements:**
`[CONTRACT]`
```
# preflight.py
def preflight(*, target_repo, control_root, worktree_root, branch) -> None   # raises PreflightError with reason
# checks: git available; target_repo is git repo; HEAD exists; no MERGE_HEAD/REBASE_HEAD/CHERRY_PICK_HEAD;
#         branch/worktree don't collide; four roots mutually non-containing; Windows path usability
```
`[PSEUDO]` init sequence: preflight → (under run_lock) prepare op → create worktree (journal the branch) → commit_operation with `next_state=INITIALIZING_WORKTREE` and `base_commit`. Recovery consults the journal + worktree list to decide claim vs NEEDS_HUMAN; **no destructive cleanup**.

**Verification command:** `python -m pytest tests/unit/test_workflow_init_recovery.py -v`
**Expected:** 6 passed.
**Done when:** init is transactional and recoverable without destructive automation.
**Commit message:** `feat: transactional workflow_init + preflight + safe recovery (Prompt §11)`

---


## Phase 4 — Codex / Grok / Verification adapters

**Entry conditions:** Phase 3 complete (Store provides `operation_dir` staging + commit path); Phase 0 confirmed real CLI flags in `docs/capabilities.json`.
**Exit conditions:**
1. Three adapters share one `invoke(request, repo_cwd, operation_dir, timeout)` protocol; none accept an arbitrary shell string.
2. Codex prompt travels via **stdin**, cwd = `run_worktree_root`, output dir = operation staging — three distinct concepts (Review P0-3, P0-4).
3. Grok receives a complete immutable **Review Bundle** (SPEC/PLAN/manifest/execution-result/diff/commits/prompt), not hashes; real read-only flags from the spike (Review P0-5, Prompt §13).
4. Verification runs structured commands (`shell=False`, cwd containment, allowlist, timeout, process-tree cleanup, output redaction).
5. Integration tests use **fake executables** (not just monkeypatched Python) asserting argv, stdin, cwd, output files, timeout.

### Task 4.1: Adapter protocol + base + secret redaction (Prompt §12, §20)

**Goal:** a shared contract and shared redaction so all three adapters are consistent and never log secrets.

**Depends on:** Phase 0, Task 2.2.
**Files:** Create `src/zcode_agentflow/adapters/__init__.py`, `src/zcode_agentflow/adapters/base.py`, `src/zcode_agentflow/redact.py`, `tests/unit/test_redact.py`, `tests/adapters/test_base_protocol.py`.

**Tests first:**
- `tests/unit/test_redact.py` — `redact("Authorization: Bearer sk-abc...")`, `redact("token=xyz")`, `redact("x-api-key: 123")`, `redact("password=hunter2")`, a Cookie header, a GitHub PAT pattern → each replaced with a `***REDACTED***` token; non-secret text unchanged; non-ASCII preserved.
- `tests/adapters/test_base_protocol.py` — a fake adapter's `invoke` is called with exactly `(request, repo_cwd, operation_dir, timeout)`; raw stdout/stderr are written to `operation_dir/raw/`; the returned `AdapterResult` has `{exit_code, stdout, stderr, artifacts, parsed}`.

**Implementation requirements:**
`[CONTRACT]`
```
# adapters/base.py
@dataclass class AdapterResult: exit_code:int; stdout:str; stderr:str; artifacts:dict[str,Path]; parsed:Any
class Adapter(Protocol):
    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult: ...
def run_subprocess(cmd, *, stdin_text, cwd, timeout, operation_dir) -> tuple[int,str,str]   # shell=False, capture, kill process tree on timeout, redact stdout/stderr before returning
```
`[PSEUDO]` process-tree kill on timeout: launch via a process group (Windows: `CREATE_NEW_PROCESS_GROUP` + `taskkill /T`; POSIX: `os.setsid` + `SIGKILL` to the group). Redaction patterns from Prompt §20 (Bearer, OpenAI/xAI/GitHub tokens, api[_-]?key, cookie, password/secret/token).

**Verification command:** `python -m pytest tests/unit/test_redact.py tests/adapters/test_base_protocol.py -v`
**Expected:** all pass.
**Done when:** shared protocol + redaction in place.
**Commit message:** `feat: adapter protocol + cross-platform process kill + secret redaction (Prompt §12,§20)`

### Task 4.2: Codex adapter — stdin prompt, read-only, separated cwd/output (resolves Review P0-3, P0-4)

**Goal:** Codex actually receives the prompt; reads the repo from `run_worktree_root`; writes JSON to the operation dir.

**Depends on:** Tasks 4.1, 2.1 (schemas), Phase 0.
**Files:** Create `src/zcode_agentflow/adapters/codex.py`, `tests/adapters/test_codex_fake_exe.py`, `tests/adapters/_fakes/fake_codex.py`.

**Tests first (fake-executable integration):** build a tiny `fake_codex.py` (committed under `tests/adapters/_fakes/`) that, when invoked, asserts: (a) it received non-empty **stdin** (writes the received prompt to `operation_dir/raw/stdin_received.txt`); (b) its cwd is the `repo_cwd` arg; (c) it writes a JSON file to the path given by `-o`. Then:
- `test_prompt_reaches_stdin()` — the captured stdin file equals the prompt (this would have caught Review P0-3).
- `test_cwd_is_run_worktree_root_not_control()` — fake_codex logs `os.getcwd()`; assert it's `run_worktree_root`, not control_root (catches P0-4).
- `test_output_dir_is_operation_staging()` — the `-o` path is under `operation_dir`, distinct from repo_cwd.
- `test_read_only_flag_present()` — argv contains the read-only sandbox flag recorded in capabilities.
- `test_timeout_kills_process_tree()` — fake_codex sleeps; adapter times out and the process (and any child it spawned) is gone afterwards.
- `test_result_validated_against_schema()` — adapter parses the `-o` JSON and validates it against the relevant schema (spec-envelope or plan-envelope) before returning.

**Implementation requirements:**
`[CONTRACT]`
```
# adapters/codex.py
class CodexAdapter:
    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        # request = {kind: "draft_spec"|"create_plan", prompt, schema_name, model, reasoning_effort, sandbox}
        # build argv from capabilities.working_flags; stdin_text = prompt; cwd = repo_cwd; -o = operation_dir/out.json
```
`[PSEUDO]` prompt/cwd/output_dir are three separate variables (never one `workdir`). argv flags come from `docs/capabilities.json` `codex.working_flags`; the adapter never hardcodes a flag the spike didn't confirm. `kind` is explicit (not inferred from prompt text) — closes Review §四.4.

**Verification command:** `python -m pytest tests/adapters/test_codex_fake_exe.py -v`
**Expected:** 6 passed.
**Done when:** Codex prompt/cwd/output bugs structurally impossible.
**Commit message:** `feat: codex adapter — stdin prompt, read-only, separated cwd/output (P0-3,P0-4)`

### Task 4.3: Grok adapter + immutable Review Bundle (resolves Review P0-5; Prompt §13)

**Goal:** Grok reviews against the full bundle; read-only enforced; HEAD/clean rechecked before and after.

**Depends:** Tasks 4.1, 2.1, Phase 0.
**Files:** Create `src/zcode_agentflow/adapters/grok.py`, `src/zcode_agentflow/review_bundle.py`, `tests/adapters/test_grok_fake_exe.py`, `tests/adapters/_fakes/fake_grok.py`.

**Tests first (fake-executable integration):** `fake_grok.py` reads the bundle dir from argv, asserts each expected file is present and non-empty, then writes a valid review-result JSON to stdout.
- `test_bundle_contains_all_required_files()` — after the engine assembles the bundle, it contains: `SPEC.md, PLAN.md, task-manifest.json, execution-result.json, diff.patch, review-request.json, prompt.md` (Prompt §13).
- `test_grok_output_cites_spec_and_plan()` — fake_grok echoes a marker proving it read SPEC/PLAN; assert the marker is present (proves Grok has real content, not hashes).
- `test_readonly_flags_from_capabilities()` — argv uses the read-only flags the spike confirmed.
- `test_bundle_unchanged_after_call()` — hash every bundle file before/after; identical.
- `test_head_and_clean_checked_pre_and_post()` — the adapter refuses to run if HEAD changed or worktree is dirty at call time, and re-checks after; on change ⇒ raise (becomes NEEDS_HUMAN in engine).
- `test_result_validated_against_review_schema_and_invariants()` — `validate_review` runs before the result is accepted.

**Implementation requirements:**
`[CONTRACT]`
```
# review_bundle.py
def assemble(*, operation_dir, control_root, run_worktree_root, base_commit, head_commit, task, exec_result) -> Path
    # writes the 7 files into operation_dir/bundle/ ; diff.patch = git diff base..head
# adapters/grok.py
class GrokAdapter.invoke(...) -> AdapterResult   # cwd = bundle dir or run_worktree_root per spike; flags from capabilities; validates review
```
`[PSEUDO]` the review record's hash fields (`spec_sha256` etc.) are **injected by the engine from state**, not trusted from Grok (Prompt §8 + Spec §4.5). The bundle is assembled in operation staging and promoted by the Store on commit.

**Verification command:** `python -m pytest tests/adapters/test_grok_fake_exe.py -v`
**Expected:** 6 passed.
**Done when:** Grok sees full context; review-not-just-diff.
**Commit message:** `feat: grok adapter + immutable Review Bundle (P0-5, Prompt §13)`

### Task 4.4: Verification adapter — structured, contained, redacted (Prompt §16)

**Goal:** run a task's `verification_commands` safely; record exit codes; no fake `network: deny`.

**Depends:** Tasks 4.1, 2.1.
**Files:** Create `src/zcode_agentflow/adapters/verification.py`, `tests/adapters/test_verification.py`.

**Tests first (`tests/adapters/test_verification.py`):**
- `test_runs_structured_command_shell_false()` — `{program:"python", args:["-V"], cwd:".", timeout_sec:10}` exits 0; assert it was launched with `shell=False` (no shell wrapper).
- `test_cwd_escape_rejected()` — `cwd:".."` ⇒ rejected before launch.
- `test_program_not_in_allowlist_rejected()` — `program:"rm"` with allowlist `["python"]` ⇒ rejected.
- `test_nonzero_exit_recorded_not_raised()` — `sys.exit(3)` ⇒ result `exit_code=3` (engine decides NEEDS_HUMAN).
- `test_secret_redaction_applied()` — command prints `token=secret` ⇒ captured stdout redacted.
- `test_network_policy_is_advisory_or_absent()` — **assert** the config has no `network: deny` pretending to work; if network control exists it's labeled `advisory_only`, otherwise absent (closes Review §六.3).
- `test_timeout_kills_process_tree()` — sleeping command; timeout; no orphan processes.

**Implementation requirements:**
`[CONTRACT]`
```
# adapters/verification.py
def run_verification(*, command, worktree_root, allowlist, timeout) -> dict   # {command, exit_code, stdout(redacted), stderr(redacted), timed_out}
```
`[PSEUDO]` reuse `run_subprocess`; cwd resolved under worktree_root and `assert_within`-checked; program must be in allowlist (allowlist sourced from `AGENTS.md`-resolved context in Phase 6, with a v1 fallback list).

**Verification command:** `python -m pytest tests/adapters/test_verification.py -v`
**Expected:** 7 passed.
**Done when:** verification is safe and honestly configured.
**Commit message:** `feat: verification adapter — structured, contained, redacted, honest network policy (Prompt §16, §六.3)`

---


## Phase 5 — Workflow Engine: the 9 tools

**Entry conditions:** Phases 2–4 complete (schemas, canonical bytes, ids, paths, Store-with-lock, three adapters).
**Exit conditions:**
1. `Engine` implements all 9 tools; each checks its `ALLOWED_FROM` state set and `expected_revision` CAS.
2. Every external call is enclosed in `Store.commit_operation` (prompt, cwd, output staging, validation, hash, promote, commit).
3. `verify_locked_artifacts(state)` re-hashes SPEC/PLAN/manifest from disk at every gate node (Prompt §14).
4. `planner_draft_spec` supports `NEEDS_USER_INPUT` → answers re-entry with a revision/attempt-bound operation id (Review §二, Prompt §15).
5. Exactly-one-task is enforced at schema **and** business layer; a multi-task plan ⇒ NEEDS_USER_INPUT/BLOCKED (Review P0-8).
6. `workflow_complete_task` performs the full hard-check set (reviewed-up to ≥11 checks), incl. on-disk artifact rehash.
7. The 9 FastMCP tool bodies (declared in Phase 1) now delegate to the engine; stdio tools/call for each works.

### Task 5.1: Engine skeleton + ALLOWED_FROM state table + artifact rehash (Prompt §14, §15; Review §五)

**Goal:** the engine scaffold: loads state via Store, enforces state-source legality, and re-hashes artifacts.

**Depends on:** Phases 2–4.
**Files:** Create `src/zcode_agentflow/engine.py`, `src/zcode_agentflow/artifacts.py`, `tests/unit/test_engine_skeleton.py`.

**Tests first (`tests/unit/test_engine_skeleton.py`):**
- `test_allowed_from_table_complete()` — every tool has an entry; assert each tool's allowed-source set matches Spec §3.1 / Prompt §15 (e.g. `planner_draft_spec ∈ {INITIALIZING_WORKTREE, NEEDS_USER_INPUT}`, `planner_create_plan ∈ {PLANNING}`, `reviewer_review_task ∈ {VERIFYING_TASK}`, `workflow_complete_task ∈ {REVIEWING_TASK}`).
- `test_calling_from_wrong_state_raises()` — e.g. calling `planner_create_plan` while state is `EXECUTING_TASK` ⇒ `WorkflowError` (tests illegal-state rejection across tools).
- `test_verify_locked_artifacts_rehashes_from_disk()` — mutate `SPEC.md` after approval, then `verify_locked_artifacts(state)` raises (this is the Review §五 fix: re-read the file, don't trust stored hash).
- `test_verify_passes_when_files_intact()` — no mutation ⇒ no raise.

**Implementation requirements:**
`[CONTRACT]`
```
# engine.py
ALLOWED_FROM = { "<tool>": {<states>}, ... }
class Engine:
    def __init__(self, store, adapters, roots, config): ...
    def _guard(self, tool, expected_revision) -> State      # checks ALLOWED_FROM + CAS under lock
# artifacts.py
def verify_locked_artifacts(state, control_root) -> None    # raise ArtifactMismatch if hash_file(SPEC/PLAN/manifest) != state.*
```
`[PSEUDO]` `_guard` acquires the run lock, loads state, asserts `state.state in ALLOWED_FROM[tool]`, asserts `expected_revision == state.revision`, then runs `verify_locked_artifacts` for the gate nodes. Rehash uses `canonical.hash_file` (always over canonical bytes — consistent with how hashes were stored).

**Verification command:** `python -m pytest tests/unit/test_engine_skeleton.py -v`
**Expected:** 4 passed.
**Done when:** state-source + rehash backbone present.
**Commit message:** `feat: engine scaffold — ALLOWED_FROM table + on-disk artifact rehash (Prompt §14,§15, Review §五)`

### Task 5.2: workflow_init + planner_draft_spec (with answers re-entry) (Prompt §11, §15; Review §二)

**Goal:** init runs (Phase 3 Store path) and draft_spec handles the questions/answers loop with correct operation ids.

**Depends:** Task 5.1, Phase 3.
**Files:** Extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_planner.py`.

**Tests first (`tests/unit/test_engine_planner.py`):** — use **fake adapters** injected into the engine (no real CLI).
- `test_draft_returns_needs_user_input_when_questions()` — fake Codex returns `status=NEEDS_USER_INPUT` with 2 questions ⇒ engine state becomes `NEEDS_USER_INPUT`, returns the questions.
- `test_reentry_with_answers_uses_new_operation_id()` — second `planner_draft_spec(goal, answers=...)` produces an operation id with a **higher attempt** number and the request contains `answers`, `state=NEEDS_USER_INPUT`, `expected_revision` (closes Review §二 idempotency bug).
- `test_reentry_is_not_treated_as_idempotent_replay()` — the second call actually re-invokes the adapter and returns the new SPEC (not the old committed result).
- `test_draft_then_ready_for_spec_approval()` — fake Codex returns no questions ⇒ state `READY_FOR_SPEC_APPROVAL`, `SPEC.md` written, `state.spec_sha256 = hash_file(SPEC.md)`.
- `test_spec_artifact_rehashed_on_entry()` — calling draft from `NEEDS_USER_INPUT` re-verifies any prior artifacts.

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_init(self, target_repo, goal, run_id=None) -> dict
def planner_draft_spec(self, goal, answers=None, expected_revision=None) -> dict
    # operation_id = ids.operation_id(run_id=, tool="draft_spec", revision=, attempt=)
    # request includes goal, answers, source_state, expected_revision
    # under Store.commit_operation: invoke Codex(kind="draft_spec"); validate spec-envelope;
    #   write SPEC.md to staging; promote; set state.spec_sha256 = hash_file(SPEC.md)
```
`[PSEUDO]` attempt counter derived from how many operations with this (run, tool, revision) prefix already exist on disk — so re-entry gets a fresh id automatically.

**Verification command:** `python -m pytest tests/unit/test_engine_planner.py -v`
**Expected:** 5 passed.
**Done when:** draft + re-entry correct.
**Commit message:** `feat: workflow_init + planner_draft_spec with answers re-entry (Prompt §11,§15, Review §二)`

### Task 5.3: workflow_approve_spec + planner_create_plan (exactly-one-task) (Review P0-8)

**Goal:** approval records + locks spec_sha; plan creation validates the approved sha, enforces one task.

**Depends:** Tasks 5.1, 5.2, Phase 6 Task 6.1 (approval) — *note the dependency*: approval lives in Phase 6, so either (a) implement approval here as a thin inline call and have Phase 6 Task 6.1 flesh out the record/fallback, or (b) sequence Phase 6 Task 6.1 before this task. The plan chooses **(b)**: Phase 6 Task 6.1 is a dependency of Task 5.3. (See dependency graph.)

**Depends on (corrected):** Phase 6 Task 6.1.
**Files:** Extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_plan.py`.

**Tests first (`tests/unit/test_engine_plan.py`):**
- `test_approve_records_and_locks_spec_sha()` — after approve, an approval record exists with `run_id, gate=SPEC, artifact_sha256`; `state.spec_sha256` is locked; re-entering approve from PLANNING is rejected (state moved on).
- `test_create_plan_requires_approved_spec_sha()` — wrong `approved_spec_sha` ⇒ `WorkflowError`.
- `test_create_plan_rejects_multi_task_at_business_layer()` — fake Codex returns 2 tasks ⇒ engine raises/returns `NEEDS_USER_INPUT`/BLOCKED (Prompt §6: "如果目标不能安全压缩为一个 Task … NEEDS_USER_INPUT/BLOCKED"); **never** silently takes task[0].
- `test_single_task_plan_accepted()` — 1 task ⇒ state `READY_FOR_EXECUTION`, `PLAN.md` + `task-manifest.json` written, `plan_sha256`/`task_manifest_sha256` set from `hash_file`.
- `test_plan_based_on_approved_spec_only()` — the Codex `create_plan` request carries `approved_spec_sha` equal to the locked value.

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_approve_spec(self, expected_revision) -> dict     # uses approval.Manager; transitions to PLANNING
def planner_create_plan(self, approved_spec_sha, expected_revision) -> dict
    # validate approved_spec_sha == state.spec_sha256
    # invoke Codex(kind="create_plan"); validate_plan; assert len(tasks)==1 (business layer)
    #   if !=1 -> record BLOCKED, state NEEDS_USER_INPUT
    #   write PLAN.md + task-manifest.json (no self-hash); set hashes from hash_file
```

**Verification command:** `python -m pytest tests/unit/test_engine_plan.py -v`
**Expected:** 5 passed.
**Done when:** single-task enforced both layers; plan tied to approved spec.
**Commit message:** `feat: approve_spec + create_plan with exactly-one-task (P0-8)`

### Task 5.4: workflow_start_task + workflow_verify_task + git change audit (Prompt §16)

**Goal:** start records task_start_commit; verify runs verification + change audit and routes failures to NEEDS_HUMAN.

**Depends:** Tasks 5.3, Phase 4 (verification adapter), Phase 6 Task 6.2 (change policy).
**Files:** Extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_verify.py`.

**Tests first (`tests/unit/test_engine_verify.py`):**
- `test_start_records_task_start_commit()` — after start, `state.task_start_commit == git HEAD` of worktree at that moment; `state.current_task == "T001"`.
- `test_verify_pass_when_green_clean_authorized()` — a committed, authorized change + passing verification ⇒ `execution-result.status=READY_FOR_REVIEW`, state `VERIFYING_TASK`.
- `test_verify_routes_to_needs_human_on_test_failure()` — verification exit≠0 ⇒ state `NEEDS_HUMAN`, execution-result `status=FAILED`.
- `test_verify_routes_to_needs_human_on_unauthorized_change()` — a delete/dependency/migration/undeclared-file not authorized in planned_changes ⇒ `change_audit.needs_human=True` ⇒ state `NEEDS_HUMAN`.
- `test_verify_routes_to_needs_human_on_dirty_worktree_or_untracked()` — uncommitted changes or untracked files ⇒ `NEEDS_HUMAN`.
- `test_authorized_delete_allowed()` — a delete listed in `planned_changes.may_delete_files` ⇒ allowed (not NEEDS_HUMAN).

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_start_task(self, task_id, expected_revision) -> dict
def workflow_verify_task(self, task_id, expected_revision) -> dict
    # worktree must be clean + no untracked at verify time
    # run verification adapter over task.verification_commands
    # change_audit = policy_change.audit(...) using git --name-status -z (handles renames/deletes/special names)
    # build execution-result (validate_execution); if not READY_FOR_REVIEW -> NEEDS_HUMAN
```
`[PSEUDO]` changed_files derived from `git diff --name-only -z base..head`; deleted/rename handling via `--name-status -z`; the execution-result is staged + promoted via Store.commit_operation.

**Verification command:** `python -m pytest tests/unit/test_engine_verify.py -v`
**Expected:** 6 passed.
**Done when:** verify + change audit honest.
**Commit message:** `feat: start_task + verify_task + git change audit routing (Prompt §16)`

### Task 5.5: reviewer_review_task (Review Bundle + hash injection) (Review P0-5)

**Goal:** assemble bundle, call Grok read-only, validate review with injected hashes.

**Depends:** Tasks 5.4, Phase 4 Task 4.3.
**Files:** Extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_review.py`.

**Tests first (`tests/unit/test_engine_review.py`):**
- `test_review_pass_advances_to_reviewing_task()` — fake Grok PASS (clean, invariant-valid) ⇒ state `REVIEWING_TASK`.
- `test_review_fail_routes_to_needs_human()` — fake Grok FAIL ⇒ `NEEDS_HUMAN`.
- `test_hashes_injected_from_state_not_trusted()` — the review record's `spec_sha256/plan_sha256/task_manifest_sha256` equal the values the engine read from `state`/disk, **not** whatever the fake Grok emitted.
- `test_bundle_assembled_before_call()` — before invoking Grok, all 7 bundle files exist in the operation staging dir.
- `test_head_and_clean_rechecked()` — if HEAD changed or worktree dirty between verify and review ⇒ error (NEEDS_HUMAN).

**Implementation requirements:**
`[CONTRACT]`
```
def reviewer_review_task(self, task_id, expected_revision) -> dict
    # verify_locked_artifacts; assert clean + HEAD == last_reviewed_commit
    # assemble Review Bundle (review_bundle.assemble) in staging
    # GrokAdapter.invoke; validate_review(parsed with injected hashes)
    # stage review-result; promote; if decision != PASS -> NEEDS_HUMAN
```

**Verification command:** `python -m pytest tests/unit/test_engine_review.py -v`
**Expected:** 5 passed.
**Done when:** review is context-complete and tamper-aware.
**Commit message:** `feat: reviewer_review_task with full bundle + hash injection (P0-5)`

### Task 5.6: workflow_complete_task — expanded hard checks + READY_TO_MERGE (Prompt §14; Review §五)

**Goal:** complete only when everything truly holds; terminal state is READY_TO_MERGE (no merge, no push).

**Depends:** Task 5.5.
**Files:** Extend `src/zcode_agentflow/engine.py`, `tests/unit/test_engine_complete.py`.

**Tests first (`tests/unit/test_engine_complete.py`):**
- `test_complete_succeeds_only_when_all_checks_hold()` — green path ⇒ `READY_TO_MERGE`; the returned checks dict is all-true.
- `test_complete_rejects_stale_review_commit()` — review.head_commit ≠ current HEAD ⇒ rejected.
- `test_complete_rejects_dirty_or_untracked_worktree()` ⇒ rejected.
- `test_complete_rejects_on_tampered_spec()` — edit `SPEC.md` after approval ⇒ on-disk rehash ≠ state.spec_sha256 ⇒ rejected (the Review §五 fix, now at completion too).
- `test_complete_rejects_on_tampered_plan_or_manifest()` — analogous.
- `test_complete_rejects_stale_cas()` — wrong `expected_revision` ⇒ rejected.
- `test_complete_does_not_merge_or_push()` — after success, assert **no** new branch on the target repo's remote, **no** `git push` invoked (assert the adapter/gitops call log has no push); state == `READY_TO_MERGE`.

**Implementation requirements:**
`[CONTRACT]`
```
def workflow_complete_task(self, task_id, expected_revision) -> dict
    # checks (>=11): exec_ready, decision_pass, review_head==HEAD, worktree_clean,
    #   no_untracked, spec_sha_on_disk==state, plan_sha_on_disk==state,
    #   manifest_sha_on_disk==state, review_hashes==state, cas, head!=task_start_commit,
    #   changed_files_nonempty_and_as_expected
    # if all -> state READY_TO_MERGE; append task to completed_tasks
    # NO merge, NO push anywhere
```
`[PSEUDO]` the on-disk rehash checks are the load-bearing difference vs. the original "8 checks" (Prompt §14 explicitly allows >8).

**Verification command:** `python -m pytest tests/unit/test_engine_complete.py -v`
**Expected:** 7 passed.
**Done when:** completion is genuinely guarded; v1 terminal state correct.
**Commit message:** `feat: workflow_complete_task — expanded rehash hard checks, READY_TO_MERGE, no merge/push (Prompt §14, Review §五)`

### Task 5.7: Wire engine into the FastMCP tools (closes Review P0-2 fully)

**Goal:** the 9 tool bodies declared in Phase 1 now call the engine; stdio `tools/call` works for all nine.

**Depends:** Tasks 5.2–5.6.
**Files:** Extend `src/zcode_agentflow/server.py`, `src/zcode_agentflow/bootstrap.py`, `tests/mcp/test_all_tools_stdio.py`.

**Tests first (`tests/mcp/test_all_tools_stdio.py`):** real subprocess MCP client driving a full happy-path run with **fake adapters** injected via a test engine factory:
- `test_stdio_full_happy_path_reaches_ready_to_merge()` — over stdio: `workflow_init → planner_draft_spec → workflow_approve_spec → planner_create_plan → workflow_start_task → (external commit by test) → workflow_verify_task → reviewer_review_task → workflow_complete_task` ⇒ final `workflow_status` shows `READY_TO_MERGE`.
- `test_stdio_error_returns_mcp_error_not_crash()` — illegal-state call returns an MCP error result; the server process stays alive.

**Implementation requirements:**
`[CONTRACT]`
```
# server.py — each @mcp_server.tool() body: return get_engine().<method>(**args)
# bootstrap.py — get_engine() builds Engine(store, adapters, roots, config) for the active run (Task 1.3 recovery)
```
`[PSEUDO]` allow a test-injectable engine factory (the production factory builds real adapters; tests inject fakes). Error mapping: `WorkflowError/CasError/ArtifactMismatch` → MCP error result with a structured message; never a bare traceback.

**Verification command:** `python -m pytest tests/mcp/test_all_tools_stdio.py -v`
**Expected:** 2 passed.
**Done when:** all 9 tools callable over real stdio end-to-end.
**Commit message:** `feat: wire engine into 9 FastMCP tools; stdio full happy path (P0-2)`

---


## Phase 6 — Approval + change policy + hooks + agent context

**Entry conditions:** Phase 2 (schemas, canonical) complete; Phase 0 hooks/tool-confirmation capability captured.
**Exit conditions:**
1. Approval records carry `run_id`; fallback=true emits a `security_warning` ZCode must surface (no silent downgrade); fallback=false + no tool-confirmation ⇒ fail loud (Prompt §17, Review §六.5).
2. Git change policy uses `--name-status -z`, handles rename/delete, configurable patterns; authorized changes allowed, unauthorized → NEEDS_HUMAN; irreversible migration → always NEEDS_HUMAN (Prompt §16).
3. Hooks use the **real** payload/cwd/exit semantics from the spike; the Stop hook calls a single read-only `check_stop` (no duplicated weak logic) (Prompt §18, Review §六.6/§六.7).
4. `AGENTS.md` is never overwritten: user's existing file + template → `resolved-agent-context.md` under control_root (Prompt §19, Review §六.4).

> **Cross-phase sequencing note:** Task 6.1 (approval) and Task 6.2 (change policy) are dependencies of Phase 5 Tasks 5.3 and 5.4 respectively. Implement this phase's 6.1 and 6.2 **before** those Phase 5 tasks. The phase numbers reflect logical grouping, not strict temporal order — see the dependency graph.

### Task 6.1: Approval records with run_id + non-silent fallback (Prompt §17; Review §六.5)

**Goal:** the SPEC gate is trustworthy *to the degree the threat model allows* (Spec §1.2), and never silently downgrades.

**Depends on:** Phase 0 (tool-confirmation capability), Phase 2.
**Files:** Create `src/zcode_agentflow/approval.py`, `tests/unit/test_approval.py`.

**Tests first (`tests/unit/test_approval.py`):**
- `test_record_includes_run_id_and_all_fields()` — a record has exactly: `approval_id, run_id, gate, artifact_sha256, approved_at, approval_method, commit_at_approval`.
- `test_strong_when_supported()` — `tool_confirmation_supported=True` ⇒ `approval_method="zcode_tool_confirmation"`.
- `test_unsupported_and_fallback_false_fails_loud()` — supported=False, `allow_non_adversarial_fallback=False` ⇒ raises `ApprovalUnavailableError` (the engine surfaces this; **no silent pass**).
- `test_fallback_true_returns_security_warning()` — supported=False, fallback=True ⇒ record has `approval_method="non_adversarial_convention"` **and** the returned dict carries `security_warning` (a non-empty string ZCode must display).

**Implementation requirements:**
`[CONTRACT]`
```
# approval.py
class ApprovalUnavailableError(Exception): ...
def record_approval(*, control_root, run_id, gate, artifact_sha256, commit_at_approval,
                    tool_confirmation_supported, allow_non_adversarial_fallback) -> dict
    # returns record; if fallback used, also returns {"security_warning": "..."}
```
`[PSEUDO]` `tool_confirmation_supported` sourced from `docs/capabilities.json` `zcode.tool_confirmation_supported`; `allow_non_adversarial_fallback` from config. Record written via `canonical.write_canonical_json` to `control_root/approvals/<approval_id>.json` (within a Store commit when called by the engine).

**Verification command:** `python -m pytest tests/unit/test_approval.py -v`
**Expected:** 4 passed.
**Done when:** approval honest; downgrade visible.
**Commit message:** `feat: approval records with run_id + non-silent fallback warning (Prompt §17, Review §六.5)`

### Task 6.2: Git change policy via --name-status -z (Prompt §16)

**Goal:** robust, special-character-safe change audit with configurable dependency/migration patterns.

**Depends on:** Phase 2, `gitops` (Phase 4 uses it; define `gitops.name_status_z` here or in Phase 3 — keep it in `gitops.py`).
**Files:** Create `src/zcode_agentflow/policy_change.py`, `tests/unit/test_policy_change.py`.

**Tests first (`tests/unit/test_policy_change.py`):** use the `fake_repo` fixture with real git:
- `test_authorized_delete_allowed()` — file in `may_delete_files` deleted ⇒ `needs_human=False`.
- `test_unauthorized_delete_needs_human()` — undeclared delete ⇒ `needs_human=True`, reason `unauthorized_delete`.
- `test_dependency_change_unauthorized_needs_human()` — edit `requirements.txt` with `may_add_dependencies=False` ⇒ `dependency_files_changed` reason.
- `test_undeclared_file_needs_human()` — new file not in `declared_files` ⇒ `undeclared_files_changed`.
- `test_rename_handled_correctly()` — `git mv` produces R status; the old path is removed, new path added; neither falsely flagged nor missed.
- `test_special_filename_with_spaces_and_quotes()` — a file named `weird "name".txt` parsed correctly via `-z` (NUL-separated), not split on spaces.
- `test_irreversible_migration_always_needs_human()` — a file matching the configurable irreversible-migration pattern ⇒ `needs_human=True` even if "authorized".
- `test_patterns_configurable()` — dependency/migration patterns read from config, not hardcoded.

**Implementation requirements:**
`[CONTRACT]`
```
# gitops.py
def name_status_z(repo, base, head) -> list[tuple[str,str,str]]   # (status_code, old_path, new_path), NUL-parsed
# policy_change.py
def audit(*, worktree, base_commit, head_commit, declared_files, planned_changes, config) -> dict
    # returns {needs_human, reasons, deleted_files, dependency_files_changed(bool),
    #          migrations_added, undeclared_files_changed, irreversible_migration(bool)}
```
`[PSEUDO]` split `git diff --name-status -z base..head` output by NUL; status codes: A/D/M/Rxx/Cxx; for R/C use both old+new paths. `dependency_files_changed` is a **bool** (matches the strict execution-result schema from Task 2.1, closing Review §六.9).

**Verification command:** `python -m pytest tests/unit/test_policy_change.py -v`
**Expected:** 8 passed.
**Done when:** change audit robust + configurable.
**Commit message:** `feat: git change policy via name-status -z, configurable patterns (Prompt §16)`

### Task 6.3: Hooks using real spike contract + shared check_stop (Prompt §18; Review §六.6/§六.7)

**Goal:** hooks built on the **observed** ZCode contract; Stop hook delegates to one engine read-only check (no logic drift).

**Depends on:** Phase 0 Task 0.2 (hook payload/cwd/exit), Phase 5 (`check_stop` read-only method).
**Files:** Create `hooks/<spike-confirmed-name>.json`, `hooks/protect_artifacts.py`, `hooks/gate_on_stop.py`, `src/zcode_agentflow/hook_logic.py`, `tests/unit/test_hook_logic.py`.

**Tests first (`tests/unit/test_hook_logic.py`):** — driven by the captured payload shape; if the spike found a different shape, adapt the assertions:
- `test_protect_artifacts_denies_edit_of_spec_plan_state_events_reviews_approvals()` — for each protected path ⇒ deny (exit code per spike).
- `test_protect_artifacts_allows_worktree_edits()` — `run_worktree_root/...` ⇒ allow.
- `test_protect_artifacts_resolves_plugin_root_via_spike_mechanism()` — the hook reaches its own scripts/`plugin_root` using whatever mechanism the spike confirmed (env var / cwd / manifest field), **not** a hardcoded relative path.
- `test_gate_on_stop_calls_engine_check_stop()` — the Stop hook invokes a single read-only entrypoint `check_stop(run_id)` and acts on `{allow_stop, reasons}`; it does **not** re-derive review/clean/HEAD logic.
- `test_gate_on_stop_blocks_when_check_fails()` — `allow_stop=False` ⇒ deny/feedback per spike; `allow_stop=True` ⇒ allow.
- `test_hook_respects_stop_reentry_limit()` — if the spike reports a reentry cap, the hook doesn't loop past it (escalates to NEEDS_HUMAN feedback instead).

**Implementation requirements:**
`[CONTRACT]`
```
# hook_logic.py  (shared, importable, testable — the thin hook scripts just call these)
def run_protect_artifacts(payload) -> int          # 0 allow / <spike-deny-code> deny
def run_gate_on_stop(payload) -> int
# engine.py (read-only, added in Phase 5 or here)
def check_stop(run_id) -> dict                       # {allow_stop: bool, reasons: [...]}
```
`[DATA]` `hooks/<name>.json`: the **actual** hook config shape from `docs/capabilities.json` (event names, command path resolved from plugin root, feedback field if any). `[PSEUDO]` hook scripts: `sys.path`-insert plugin root is replaced by whatever the spike showed works (e.g. the hook runs with a known cwd/env); the contract is "scripts call `hook_logic.run_*`", keeping logic in the package.

**Verification command:** `python -m pytest tests/unit/test_hook_logic.py -v`
**Expected:** 6 passed.
**Done when:** hooks grounded in real contract; single source of truth for stop checks.
**Commit message:** `feat: hooks on real spike contract + shared check_stop (Prompt §18, Review §六.6/§六.7)`

### Task 6.4: Agent context resolution — never overwrite AGENTS.md (Prompt §19; Review §六.4)

**Goal:** combine the target repo's existing `AGENTS.md` (if any) with the plugin template into a resolved context under control_root, passed to Codex/Grok/ZCode.

**Depends on:** Phase 2.
**Files:** Create `src/zcode_agentflow/agents_context.py`, `AGENTS.md.template`, `tests/unit/test_agents_context.py`.

**Tests first (`tests/unit/test_agents_context.py`):**
- `test_no_existing_agents_md_uses_template_only()` — target repo has none ⇒ resolved = template body.
- `test_existing_agents_md_preserved_and_merged()` — repo has its own ⇒ resolved = user file + an "## AgentFlow notes" section from the template; the **repo file is untouched** (assert mtime/content unchanged).
- `test_repeated_run_does_not_duplicate_section()` — resolving twice doesn't append the notes section twice.
- `test_non_ascii_content_handled()` — user AGENTS.md with Chinese/emoji ⇒ preserved verbatim in resolved output (UTF-8 strict).
- `test_resolved_written_under_control_root()` — output at `control_root/resolved-agent-context.md`, never in target_repo.

**Implementation requirements:**
`[CONTRACT]`
```
# agents_context.py
def resolve(*, target_repo, plugin_root, control_root) -> Path
    # read repo AGENTS.md if present (UTF-8 strict); read plugin AGENTS.md.template;
    # compose (user-first, append notes once); write_canonical_text to control_root; return path
```
`[DATA]` `AGENTS.md.template`: short, focused on AgentFlow's needs (verification command shape, v1 single-task, allowlist note). The resolved path is fed to adapters as part of their context (Codex cwd stays worktree; the resolved file path is referenced in prompts).

**Verification command:** `python -m pytest tests/unit/test_agents_context.py -v`
**Expected:** 5 passed.
**Done when:** user's AGENTS.md is safe; agents share one resolved context.
**Commit message:** `feat: agent context resolution, never overwrite user AGENTS.md (Prompt §19, Review §六.4)`

---


## Phase 7 — Three-layer E2E + docs + READY_TO_MERGE handoff

**Entry conditions:** Phases 1–6 complete; unit + adapter + store + mcp tests green.
**Exit conditions:**
1. Layer 1 (Engine E2E, fake adapters): a run that **really edits and commits code** reaches READY_TO_MERGE, plus the full rejection matrix (stale review, dirty worktree, multi-task plan, illegal state, recovery matrix, both approval paths).
2. Layer 2 (MCP transport E2E): the same run driven over real stdio.
3. Layer 3 (local smoke, real Codex/Grok): documented, **off by default**, manually invoked, gated on Phase 0 capabilities.
4. A `READY_TO_MERGE` handoff report (branch, HEAD, tests, review) — **no push, no merge, no PR** anywhere in the plan (Prompt §22, Review §六.10).

### Task 7.1: Layer 1 — Engine E2E with real commit + full rejection matrix (Prompt §21)

**Goal:** prove the v1 vertical slice as a *code-execution* test, not a state-machine-only test.

**Depends:** Phases 1–6.
**Files:** Create `tests/e2e/test_engine_e2e.py`.

**Tests first / the test IS the deliverable:**
- `test_happy_path_edits_commits_and_merges_state()` — using a real temp git repo + fake adapters, the run:
  `workflow_init → draft_spec → approve → create_plan(1 task) → start_task → **edit a.txt, git add, git commit** → verify → review(PASS) → complete → READY_TO_MERGE`.
  Assertions: `head_commit != task_start_commit`; `changed_files == ["a.txt"]`; worktree clean at complete; Review Bundle complete; `verify_locked_artifacts` passes; review.head_commit == HEAD.
- `test_stale_review_rejected_at_complete()` — after review, commit again; complete ⇒ rejected.
- `test_dirty_worktree_rejected_at_complete()` — leave an uncommitted change; complete ⇒ rejected.
- `test_multi_task_plan_rejected()` — fake Codex returns 2 tasks ⇒ NEEDS_USER_INPUT/BLOCKED, not silent task[0].
- `test_illegal_state_call_rejected()` — e.g. `complete` from `EXECUTING_TASK` ⇒ error.
- `test_recovery_matrix_on_restart()` — simulate MCP restart mid-run at several points; active-run recovery resumes correctly (reuses Phase 3 recovery).
- `test_approval_strong_path()` — `tool_confirmation_supported=True` ⇒ strong approval recorded.
- `test_approval_fallback_path()` — supported=False, fallback=True ⇒ record + `security_warning`; supported=False, fallback=False ⇒ `ApprovalUnavailableError` (NEEDS_HUMAN).

**Implementation requirements:** `[PSEUDO]` a shared `e2e_run` helper injecting fake adapters; the "edit + commit" step uses real `git` in the worktree to mirror what ZCode does. Fake adapters are deterministic (return canned spec/plan/review).

**Verification command:** `python -m pytest tests/e2e/test_engine_e2e.py -v`
**Expected:** 8 passed.
**Done when:** the vertical slice is proven with real code changes + rejections.
**Commit message:** `test: Layer-1 engine E2E — real commit, full rejection matrix (Prompt §21)`

### Task 7.2: Layer 2 — MCP transport E2E over real stdio (Prompt §21)

**Goal:** the same happy path + a couple of rejections, driven through the actual MCP protocol (not the engine directly).

**Depends:** Tasks 7.1, 5.7.
**Files:** Create `tests/e2e/test_mcp_transport_e2e.py`.

**Tests first:**
- `test_stdio_run_reaches_ready_to_merge()` — spawn the server, drive all 9 `tools/call` in order (with the test performing the real git commit between start and verify), assert final `workflow_status == READY_TO_MERGE`.
- `test_stdio_rejection_returns_mcp_error_and_keeps_server_alive()` — an illegal-state call returns an MCP error; a subsequent valid call still works.

**Implementation requirements:** `[PSEUDO]` reuse the stdio client helper from Phase 1; the test injects fake adapters by setting an env var / factory hook the server respects in test mode. The "edit + commit" between tools mirrors Layer 1.

**Verification command:** `python -m pytest tests/e2e/test_mcp_transport_e2e.py -v`
**Expected:** 2 passed.
**Done when:** the protocol path is proven, not just the engine.
**Commit message:** `test: Layer-2 MCP transport E2E over stdio (Prompt §21)`

### Task 7.3: Layer 3 — local smoke harness (real Codex/Grok), off by default (Prompt §21)

**Goal:** a documented, manually-run smoke that exercises the real CLIs; never runs in the default suite.

**Depends:** Tasks 7.1, 7.2, Phase 0.
**Files:** Create `tests/smoke/run_local_smoke.py` + `docs/smoke.md`; extend pytest config to **exclude** `tests/smoke` from default collection.

**Implementation requirements:** `[PSEUDO]` a standalone script (not a pytest) that runs a full run against a tiny sample repo using the **real** Codex/Grok adapters; gated behind an env flag (`AGENTFLOW_SMOKE=1`) and a check that `docs/capabilities.json` confirms the needed capabilities. `pytest.ini`/`pyproject` `testpaths`/`addopts: --ignore=tests/smoke` so it never runs by default. Outputs the READY_TO_MERGE handoff report.

**Verification command:** `python -m pytest -q` (default run) — assert smoke is **not** collected; `AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py` (manual) — reaches READY_TO_MERGE against real CLIs.
**Expected:** default suite unaffected; manual smoke documented.
**Done when:** real-CLI path is exercisable but opt-in.
**Commit message:** `test: Layer-3 local smoke harness (real CLIs), off by default (Prompt §21)`

### Task 7.4: READY_TO_MERGE handoff report + docs (Prompt §22; Review §六.10)

**Goal:** the run's terminal output is a handoff report; user decides push/PR/merge. Plus user-facing docs.

**Depends:** Tasks 7.1–7.3.
**Files:**
- Extend `workflow_complete_task` (or a `handoff` helper) to emit a `READY_TO_MERGE` report: `{branch, head_commit, base_commit, changed_files, test_results, review_result, spec_sha256, plan_sha256}`.
- Create `skills/agentflow/SKILL.md`, `commands/agentflow-start.md`, `commands/agentflow-status.md` (finalized content describing the 9 tools, the two gates, the READY_TO_MERGE handoff, and explicit "no auto-push").
- Create `docs/README.md` (install, configure, run, recover).
- Finalize `.zcode-plugin/plugin.json` per the Phase 0 manifest shape.

**Tests first (`tests/e2e/test_handoff_report.py`):**
- `test_complete_emits_handoff_with_no_push()` — after complete, a handoff report exists with the listed fields; assert **no** `git push`/`merge` was invoked (adapter/gitops call log empty of push/merge).

**Implementation requirements:** `[PSEUDO]` handoff is a side-effect-free read of state + artifacts; the SKILL/commands explicitly instruct the supervisor (GLM-5.2) to **stop at READY_TO_MERGE** and present the branch/commit to the user. `[DATA]` SKILL.md content covers: the 9-tool order, the two human gates, ALLOWED_FROM, "never edit SPEC/PLAN/state directly", "READY_TO_MERGE is terminal; user merges externally; do not push".

**Verification command:** `python -m pytest tests/e2e/test_handoff_report.py -v`
**Expected:** 1 passed.
**Done when:** terminal behavior is honest and documented.
**Commit message:** `feat: READY_TO_MERGE handoff report + skill/commands/docs; no auto-push (Prompt §22, Review §六.10)`

### Task 7.5: Full suite green + final commit (no push)

**Goal:** whole suite passes; final commit. **No `git push` in the plan.**

**Depends:** all prior tasks.
**Files:** none new.

**Verification command:** `python -m pytest -q`
**Expected:** all unit + adapter + store + mcp + e2e green; smoke excluded.
**Done when:** suite green; v1 vertical slice complete and locally committed.
**Commit message:** `chore: v1 vertical slice complete — full suite green`

> **Explicitly NOT in this plan:** any `git push origin main`, any merge, any PR creation. Whether to push/merge is the user's decision, made outside AgentFlow.

---

## Appendix A — File ownership table

| File | Owns | Mutates durable state? |
|---|---|---|
| `src/zcode_agentflow/config.py` | load config.yaml + capability flags | no |
| `paths.py` | four-roots, normalization, containment | no |
| `ids.py` | run/operation/task id gen + validation + branch/dir normalize | no |
| `canonical.py` | canonical text/json bytes, hashing, atomic write | no (writes via Store) |
| `schemas.py` | strict schema load + validate + cross-field invariants | no |
| `manifest.py` | build self-reference-free task manifest | no |
| `locks.py` | per-Run file lock | no |
| `store.py` | state.json + events.jsonl + operations + `commit_operation` | **YES (sole mutator)** |
| `artifacts.py` | `verify_locked_artifacts` rehash | no (read-only) |
| `gitops.py` | git wrappers incl. `name_status_z` | only via engine/store |
| `adapters/base.py` | Adapter protocol + subprocess + redaction | writes to operation staging only |
| `adapters/codex.py` `grok.py` `verification.py` | external CLI calls | staging only |
| `review_bundle.py` | assemble immutable Review Bundle | staging only |
| `policy_change.py` | git change audit | no |
| `approval.py` | approval records + fallback warning | via Store |
| `agents_context.py` | resolve AGENTS.md → control_root | writes resolved file only |
| `redact.py` | secret redaction | no |
| `engine.py` | 9 tools, ALLOWED_FROM, rehash, exactly-one-task | via Store |
| `bootstrap.py` | process config + active-run recovery | no |
| `server.py` | FastMCP stdio, 9 tools, main() | via engine |

Rule: **only `store.py` writes `state.json`/`events.jsonl`/`operations/`, and only while holding the Run lock.** Adapters write only to their operation's staging dir; promotion is the Store's job.

## Appendix B — Phase/Task dependency graph

```
Phase 0 (spike) ─────────────────────────────────┐
   └─ 0.1 .. 0.5                                 │
        │ capabilities.json gates everything     │
        ▼                                        │
Phase 1 (project + real MCP)                     │(capabilities feed adapters/hooks)
   1.1 ─> 1.2 ─> 1.3 ─> 1.4 ─> 1.5               │
                                   │             │
Phase 2 (schemas/canon/ids/roots/manifest)       │
   2.1  2.2  2.3  2.4  2.5  (2.1 needs 1.1; 2.2/2.4 need 2.2/2.3)
        │                                        │
Phase 3 (lock/store/ops/fault/init)              │
   3.1 ─> 3.2 ─> 3.3 ─> 3.4 ─> 3.5 ─> 3.6        │
        │                                        │
Phase 4 (adapters)  needs 3.4(staging) + 2.1 + Phase 0 ◄────┘
   4.1 ─> {4.2, 4.3, 4.4}
        │
Phase 6.1 (approval)  needs Phase 0 + 2.x        ──┐
Phase 6.2 (change policy) needs 2.x + gitops     ──┤
Phase 6.3 (hooks) needs Phase 0.2 + Phase 5 check_stop
Phase 6.4 (agents_context) needs 2.x             │
                                                  │
Phase 5 (engine 9 tools) needs 2,3,4 + 6.1,6.2 ◄─┘
   5.1 ─> 5.2 ─> 5.3(needs 6.1) ─> 5.4(needs 6.2+4.4) ─> 5.5(needs 4.3) ─> 5.6 ─> 5.7
        │
Phase 7 (3-layer E2E + docs) needs 5 + 6
   7.1 ─> 7.2 ─> 7.3 ─> 7.4 ─> 7.5
```

**Key cross-phase edges (not temporal):**
- 6.1 (approval) **before** 5.3 (approve_spec/create_plan).
- 6.2 (change policy) **before** 5.4 (verify_task).
- 6.3 hooks' `check_stop` needs a Phase 5 read-only method → implement that method in 5.x, then 6.3 wires the hook.
- Phase 0 (capabilities) gates 1.4 (MCP import path), all of 4 (CLI flags), and 6.1/6.3.

## Appendix C — Spec S1–S4 mapping

| Spec success criterion | Mapped to |
|---|---|
| **S1** full NEW→READY_TO_MERGE (全景, multi-task) | Out of v1 scope by design; v1 proves the spine. Multi-task/final-review = v1.3 (Spec §3.3). v1's contribution to S1: Phases 5+7 deliver the single-task spine that v1.3 extends. |
| **S2** structured JSON handoff, no chat-relay | 2.1 strict schemas; 4.2/4.3 adapters emit/parse schema JSON; 5.x inject hashes from state (not trust model output). |
| **S3** process-integrity checks (detect direct edits / stale review / stale manifest / hash mismatch) | 2.5 (no self-hash) + 5.1 `verify_locked_artifacts` rehash at every gate + 5.6 expanded complete checks + 3.5 recovery. (Per Spec §1.2, **not** same-privilege tamper-proof.) |
| **S4** v1 vertical slice: single task + one review + two gates, Windows/Git Bash | 7.1 (Layer 1, real commit) + 7.2 (Layer 2 stdio) + 7.3 (Layer 3 real CLIs). |

## Appendix D — Review P0/P1 close matrix

| Review item | Severity | Closed by |
|---|---|---|
| P0-1 local `mcp/` shadows SDK | P0 | 1.1 (`test_no_local_mcp_shadow`, src layout, pyproject) |
| P0-2 server not a real MCP server | P0 | 1.4 + 1.5 + 5.7 (FastMCP stdio, initialize/tools/list/tools/call) |
| P0-3 Codex prompt not sent (empty stdin) | P0 | 4.2 (`test_prompt_reaches_stdin`) |
| P0-4 Codex cwd = control_root | P0 | 4.2 (`test_cwd_is_run_worktree_root_not_control`) |
| P0-5 Grok sees only diff, not SPEC/PLAN | P0 | 4.3 (Review Bundle) + 5.5 |
| P0-6 split commit protocol + hash mismatch | P0 | 3.4 (single `commit_operation`, canonical bytes) + 3.5 |
| P0-7 no file lock | P0 | 3.1 (portalocker per-Run) |
| P0-8 single-task not enforced | P0 | 2.1 (maxItems:1) + 5.3 (business-layer check) |
| §二 draft re-entry vs operation idempotency | P1 | 5.2 (revision/attempt-bound op id; request carries answers/state/rev) |
| §三 static env-var repo binding | P1 | 1.2 (`workflow_init(target_repo)`; no env repo vars) |
| §四 E2E design bugs (factory, fallback, prompts, kind-guessing, no real commit) | P1 | 7.1 (real commit) + 5.7 (factory injection) + 4.2 (explicit kind) + e2e config sets fallback=true or capability=true |
| §五 hash checks trust stored value, not file | P1 | 5.1 `verify_locked_artifacts` + 5.6 on-disk rehash |
| §六.1 no single-trailing-newline canonicalization | P1 | 2.2 |
| §六.2 event seq resets on restart | P1 | 3.2 (resume from disk) |
| §六.3 fake `network: deny` | P1 | 1.2 (advisory_only or removed) + 4.4 |
| §六.4 AGENTS.md only template | P1 | 6.4 (resolve, never overwrite) |
| §六.5 approval missing run_id + silent downgrade | P1 | 6.1 |
| §六.6 Stop hook weaker than spec | P1 | 6.3 (shared `check_stop`) |
| §六.7 hook payload/relative path unverified | P1 | Phase 0.2 spike + 6.3 |
| §六.8 pytest 0-tests exit code | minor | 1.1 ships a real `test_package_version` |
| §六.9 schema too loose / type drift | P1 | 2.1 (additionalProperties:false, typed change_audit) |
| §六.10 auto `git push origin main` | P1 | 7.4 + 7.5 (no push/merge anywhere; handoff report) |

## Appendix E — This-prompt hardening close matrix (Prompt items 1–22)

| Prompt item | Closed by |
|---|---|
| 1 src layout / no mcp clash | 1.1 |
| 2 real MCP stdio server | 1.4, 1.5, 5.7 |
| 3 capability spike first | Phase 0 |
| 4 runtime target_repo + run model | 1.2 (Model A) + 1.3 (recovery) |
| 5 ID & path safety | 2.3, 2.4 |
| 6 strict schemas | 2.1 |
| 7 canonical bytes | 2.2 |
| 8 manifest no self-hash | 2.5 |
| 9 recoverable store + run lock | 3.1–3.5 |
| 10 external calls inside transaction | 3.4 |
| 11 recoverable workflow_init | 3.6 |
| 12 Codex adapter (stdin/cwd/output split) | 4.2 |
| 13 Grok Review Bundle | 4.3 |
| 14 artifact rehash | 5.1, 5.6 |
| 15 re-entry + ALLOWED_FROM + op id | 5.1, 5.2 |
| 16 verification + change policy | 4.4, 6.2 |
| 17 approval | 6.1 |
| 18 hooks | 6.3 |
| 19 AGENTS.md | 6.4 |
| 20 logging/secret redaction | 4.1 |
| 21 three-layer E2E | 7.1, 7.2, 7.3 |
| 22 no auto push | 7.4, 7.5 |

## Appendix F — Risks & capabilities still requiring real-environment confirmation

These are gated on the Phase 0 spike; until confirmed they block their dependents and must be resolved by the user in a real ZCode/CLI environment:

1. **ZCode plugin manifest exact shape** (field names for the MCP command). Blocks 1.4, server packaging. If the spike finds the shape differs from any assumption here, that task adapts.
2. **ZCode hook contract** (event names, stdin JSON keys, hook cwd, deny exit code, feedback channel, Stop reentry cap). Blocks 6.3.
3. **ZCode tool-level mandatory human confirmation** (supported? config field?). Blocks the strong-approval path in 6.1; if unsupported, v1 falls back per Spec §5.4.
4. **`mcp` SDK import path** (`from mcp.server.fastmcp import FastMCP` or other). Blocks 1.1/1.4.
5. **Codex flags** (read-only sandbox flag actually enforced; `model_reasoning_effort=high` available for the account/model; stdin `-` semantics). Blocks 4.2.
6. **Grok flags** (the real read-only flags; `--output-format json` shape; whether `--json-schema` exists on this CLI version). Blocks 4.3.
7. **portalocker behavior on Windows** under the test concurrency model. Blocks 3.1 (mitigation: if portalocker proves flaky on Windows, swap to a tested alternative within Task 3.1).
8. **Network control honesty** — v1 declares network policy advisory-only/absent; true network isolation is explicitly **out of v1 scope** (Spec §1.2 threat model; Prompt §16).

Each item above is a spike deliverable recorded in `docs/capabilities.json`; a `false`/`unknown` finding does not silently weaken the plan — it blocks the dependent task until resolved.

## Appendix G — Definition of Done (v1, maps to Spec §0.4 S4)

- [ ] `src/zcode_agentflow` package; **no** local `mcp/`; official `mcp` SDK import verified.
- [ ] Real FastMCP stdio server; `initialize`/`tools/list`/`tools/call` integration tests green.
- [ ] `workflow_init(target_repo, goal)`; run_id/control/worktree derived; no repo env-var binding; active-run recovery.
- [ ] Four strict schemas; `tasks` maxItems:1; business-layer single-task check.
- [ ] Canonical bytes (single trailing newline, strict UTF-8, deterministic JSON); hashing always over actual written bytes.
- [ ] Per-Run file lock; single `commit_operation`; state replaced before COMMITTED; fault-injection matrix green.
- [ ] Codex prompt via stdin; cwd = worktree; output = operation staging; read-only.
- [ ] Grok reads full immutable Review Bundle; read-only; HEAD/clean rechecked.
- [ ] Verification structured, contained, redacted; network policy honest.
- [ ] Approval: run_id present; fallback loud (warning) or fails; never silent.
- [ ] Hooks on real spike contract; Stop hook shares engine `check_stop`.
- [ ] AGENTS.md never overwritten; resolved context under control_root.
- [ ] Engine: 9 tools, ALLOWED_FROM, artifact rehash, answers re-entry, ≥11 complete checks.
- [ ] Three-layer E2E: engine (real commit + rejection matrix), stdio transport, opt-in real-CLI smoke.
- [ ] Terminal state `READY_TO_MERGE` with a handoff report; **no push/merge/PR** anywhere in the plan.

## Appendix H — Explicit declaration: no auto push / merge

This plan contains **no** `git push`, `git merge`, or PR creation, in any task, step, or commit message template. The v1 terminal state is `READY_TO_MERGE`. The run outputs a handoff report (branch, HEAD commit, changed files, test results, review result). Whether to push, open a PR, or merge is decided by the user **outside** AgentFlow. (Prompt §22; Review §六.10.)

## Appendix I — Plan self-review report

Run with fresh eyes against the Prompt's "写完后的自检" checklist:

| Check | Result |
|---|---|
| No local `mcp` package conflict | ✅ Appendix A; src `zcode_agentflow`; Task 1.1 asserts no `mcp/` and that `import mcp` resolves to site-packages |
| Real FastMCP stdio | ✅ Task 1.4 builds `FastMCP` + `run(transport="stdio")` + `main()`; stdio protocol tests; not a `dispatch()`-only layer |
| `workflow_init` receives `target_repo` | ✅ Task 1.2 signature + test `test_workflow_init_signature_takes_target_repo`; no repo env vars |
| exactly-one-task enforced (schema + business) | ✅ Task 2.1 `maxItems:1` + Task 5.3 business-layer rejection; multi-task ⇒ NEEDS_USER_INPUT/BLOCKED |
| prompt / cwd / output_dir separated | ✅ Task 4.2 three-distinct-concepts contract + fake-exe tests |
| Review Bundle complete | ✅ Task 4.3 seven required files + cites-content test |
| manifest hash no self-reference | ✅ Task 2.5 (no self-hash field) + hash stored in state/sidecar |
| all artifacts via staging commit | ✅ Task 3.4 `commit_operation` promotes staging → authoritative |
| file lock consistent with commit order | ✅ Task 3.1 lock held across Task 3.4's 12 steps |
| artifact rehash reads actual files | ✅ Task 5.1 `verify_locked_artifacts` uses `hash_file` |
| operation supports answers re-entry + UNKNOWN | ✅ Task 5.2 (revision/attempt op id) + Task 3.3 (UNKNOWN) |
| E2E really edits & commits code | ✅ Task 7.1 edits `a.txt`, `git add`, `git commit`; asserts head≠start |
| no auto push | ✅ Appendix H; Tasks 5.6/7.4/7.5 assert no push/merge invoked |
| no invented ZCode API | ✅ every ZCode-touching task depends on Phase 0 spike findings; flags/manifest/payload come from `docs/capabilities.json` |

No placeholder strings (TBD/TODO/fill-in/Similar-to) in task bodies — each task states goal, deps, files, tests, requirements, verification, done-when, and commit message. Code is limited to `[CONTRACT]`/`[DATA]`/`[PSEUDO]` blocks by design.

---

**End of Plan v2.**
