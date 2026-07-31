# Phase 7/8 Audit-Fix Notes (2026-07-31)

This is a read-only review record (per the `docs/review/` convention) accompanying
the Phase 7/8 audit-fix remediation on branch `codex/audit-fix-phase7-8`. It
captures: (A) the P0/P1 findings from the FAIL review, verbatim where cited;
(B) the verified codebase evidence (file:line) that confirms each finding; and
(C) the decisions taken (which are recorded as errata 9–13 in
`docs/superpowers/plans/2026-07-30-zcode-agentflow-v1-v2-2.md`).

It is a **record**, not an instruction: the plan errata are the authoritative
revision; this file exists so a reviewer can trace each fix back to the finding
and the evidence without re-deriving it.

## A. Findings (from the Phase 7/8 FAIL review, 2026-07-31)

### P0 (must fix)

1. **Production plugin cannot start the MCP server.** With `.zcode-plugin/plugin.json` +
   `config.yaml`, `python -m zcode_agentflow.server` exits 1 with `CapabilityError`
   (`tool_confirmation=false` AND `allow_non_adversarial_fallback=false`). The manifest
   itself documents this (`plugin.json:31`, `config.yaml:31`). → Phase 7 "real plugin
   load / tools/list / approve confirmation / Stop gate" cannot pass.

2. **Even past the start gate, the manifest provides no run context.** All state-change
   tools require `AGENTFLOW_RUN_CONTEXT`; unset → `server.py:133` / `bootstrap.py:123`
   raise. The manifest only sets `AGENTFLOW_CONFIG` + `AGENTFLOW_CAPABILITIES`. Layer 2
   tests pass only because they inject the var (`test_mcp_transport_e2e.py:96`).

3. **Two hooks fail-open in the real layout.** The hooks treat `ZCODE_PROJECT_DIR` as the
   `active-run.json` home (`hook_logic.py:103`, `:123`), but the real pointer is at
   `<user_data_root>/active-run.json` and `ZCODE_PROJECT_DIR` is the project dir. →
   protect allows protected edits; Stop builds no Engine and allows stop. Tests pass
   only because the fixture places the pointer under `ZCODE_PROJECT_DIR`
   (`test_hook_logic.py:52`), masking the production layout.

4. **Stop Hook introduces a second write path to control data.** It writes a counter
   under `control_root/operations/` via `write_text` (`hook_logic.py:293`, `:320`),
   bypassing Store / per-run lock / event log / atomic commit — violating the
   "Store is the sole writer of `operations/`" invariant (Plan line 164 & 1757).

5. **Layer 3 never actually ran; the harness faked strong confirmation.**
   `phase8-acceptance.md:45` records real-CLI smoke as "deferred". The smoke script
   injects `effective_approval_mode="zcode_tool_confirmation"` +
   `confirmation_configured=True` (`run_local_smoke.py:162`), not a real observation.

### P1 (should fix)

- Layer 1 rejection matrix has 8 scenarios; Phase 8 prompt also requires stale revision,
  artifact mutation, verification failure, unauthorized change, mismatched review
  (`test_engine_e2e.py:293`).
- Layer 2 rejection tests cover illegal-state + liveness, not the full matrix over MCP
  transport (`test_mcp_transport_e2e.py:251`).
- `test_plugin_loads.py` only parses JSON; no real plugin load; custom top-level
  `toolConfirmation` field acceptance is unverified.
- Commits prove one-task-per-commit but no RED evidence is retained in the repo.

## B. Verified codebase evidence (confirmed against the source, not the review's summary)

- **P0-1 (start gate):** `config.py:319-327` raises `CapabilityError` when
  `tool_confirmation` is false and `allow_non_adversarial_fallback` is false.
  `config.yaml:31` = `allow_non_adversarial_fallback: false`. This is the intended G6
  fail-loud behavior (correct security stance), but the two resolutions (set the flag
  true, or configure `~/.claude/settings.json`) were never verified in a real host.
- **P0-2 (no production context):** `bootstrap.load_run_context()` (bootstrap.py:122-147)
  reads ONLY `AGENTFLOW_RUN_CONTEXT`; its own docstring (lines 110-112, 125) declares it
  the "test/subprocess path". Every server.py entry calls it (:84, :131, :147, :256).
  `process_config` / `RunContext` / `AGENTFLOW_RUN_CONTEXT` appear **nowhere** in Plan
  v2.2 (0 hits). The earlier plan `-v1-v2.md` Task 1.2 specified a production
  `bootstrap.process_config()` (resolving plugin_root/user_data_root/config_path,
  default `<plugin_root>/../agentflow-data`); that contract was dropped in `-v2-2` and
  never implemented. `AGENTFLOW_CONFIG`/`AGENTFLOW_CAPABILITIES` are read by 0 sites in
  `src/` (grep-confirmed). **Confirmed real gap.**
- **P0-3 (hook layout):** `hook_logic._resolve_control_root()` reads
  `${ZCODE_PROJECT_DIR}/active-run.json` (hook_logic.py:123). capabilities.json:82 =
  "`hook_cwd`: the project root … Plugin root reachable via env var ZCODE_PROJECT_DIR".
  Real pointer home = `<user_data_root>/active-run.json` (Spec §2.2 / Plan line 401).
  Test fixture sets `ZCODE_PROJECT_DIR = control_tree.parent` AND writes the pointer
  there (test_hook_logic.py:106, 243-247, 294), hiding the mismatch. **Confirmed.**
- **P0-4 (sole-writer violation):** Plan v2.2 line 164 & 1757 (verbatim): "only
  `store.py` writes `state.json`/`events.jsonl`/`operations/`." `hook_logic._stop_counter_path`
  returns `<control_root>/operations/.stop_counter__*.json` (hook_logic.py:293);
  `_bump_continuation` writes it via `Path.write_text` (hook_logic.py:318-320) — no run
  lock, no `os.replace`, no event log. Nuance: `OperationManager.open_existing` opens by
  `operation_id` (does not glob `operations/*`), so recovery is not corrupted by the
  counter — but it is still a literal invariant violation + non-atomic unlocked write.
  Host `stopHookActive` (capabilities.json hooks.stop_reentry_limit) already enforces the
  3-continuation cap. **Confirmed.**
- **P0-5 (Layer 3 fake):** `phase8-acceptance.md:45` = "harness ready; real CLI run
  deferred to manual operator execution". `run_local_smoke.py:162-163` hardcodes the two
  confirmation fields. **Confirmed.**

## C. Decisions (recorded as Plan errata 9–13)

- **9 — production context path:** restore `bootstrap.process_config()` +
  `build_run_context()` dispatcher. `AGENTFLOW_RUN_CONTEXT` stays test-only. Closes P0-2.
- **10 — user_data_root location:** default `<plugin_root>/../agentflow-data`, overridable
  via `AGENTFLOW_USER_DATA_ROOT`; pointer/lock paths unchanged.
- **11 — Stop-hook counter removed:** rely on host `stopHookActive`; hook writes no files.
  Closes P0-4.
- **12 — hook pointer resolution:** use `build_run_context()` → `user_data_root`, NOT
  `ZCODE_PROJECT_DIR` (kept as secondary dev fallback). Closes P0-3.
- **13 — Layer 3 honest approval mode:** derive from `process_config()`, never inject.
  Closes P0-5.

The P1 items are addressed by tasks AF-5 (packaging/subprocess test), AF-6 (rejection
matrix completion). RED-evidence retention is out of scope for a remediation (cannot
back-fill genuine RED output for already-merged commits); future tasks should save a RED
snippet per AGENTS.md §3.

## D. What this remediation does NOT do

- Does not edit Specs or any historical Review.
- Does not push / merge / create a PR.
- Does not change `AGENTFLOW_RUN_CONTEXT` JSON shape or break the 7 tests using it.
- Does not truly confirm the ZCode GUI confirmation dialog (needs human eyes — Phase 7
  Exit "approve_spec confirmation" may remain `[ ]` until a human observes it).

## E. Round-2 audit-fix (re-review FAIL, 2026-07-31) — 3 further P0s

The round-2 re-review found the round-1 fixes left three gaps. All three are fixed
(TDD) and the schema is verified accepted by the real codex API.

- **R2-1 / P0-2 (strict-mode plan schema, was xfailed):** codex `response_format`
  is strict-mode — every object's `required` must list ALL its properties. The
  `plan-proposal` RAW schema modeled `plan`/`questions`/`block_reason` as OPTIONAL
  (a union over `status`) and was rejected (`'required ... Missing plan'`). FIX:
  the schema is now a fixed-key envelope (status/plan/questions/block_reason ALL
  always present; `plan` is `["object","null"]`, non-null only for
  READY_FOR_EXECUTION); `validate_raw_plan` enforces the per-status semantics on
  top. The xfail is now a passing test. Verified: a direct CodexAdapter plan call
  against the fixed schema returns a **network** error (codex↔ChatGPT websocket),
  NOT `invalid_json_schema` — the schema uploads cleanly.
- **R2-2 / P0-1 (independent hook process context):** hook scripts inherit
  `ZCODE_PLUGIN_ROOT` but NOT `AGENTFLOW_CONFIG`/`AGENTFLOW_CAPABILITIES` (the
  hook `.json` files have no env block; only `mcpServers.env` does), and the
  hook cwd is the target project — so `process_config()` looked for
  cwd-relative `config.yaml` → `FileNotFoundError` → fail-open. FIX:
  `process_config()` now defaults config to `<plugin_root>/config.yaml` and caps
  to `<plugin_root>/docs/capabilities.json` (they ship WITH the plugin, Spec §6)
  when the env vars are absent. Verified by reproducing the review's hook-env
  simulation: `ZCODE_PLUGIN_ROOT` + `ZCODE_PROJECT_DIR` only, pointer at default
  `agentflow-data`, cwd = target project → the hook now DENIES a control-data
  edit (rc=2), no fail-open.
- **R2-3 / P0-3 (shipped config blocks production start):** shipped
  `config.yaml` had `allow_non_adversarial_fallback: false` while capabilities
  `tool_confirmation=false`, so every production start exited 1 (CapabilityError).
  FIX: shipped config now `allow_non_adversarial_fallback: true` (the env reality
  — tool_confirmation is supported but not configured, defaultMode=yolo). The
  server STARTS in the `non_adversarial_convention` fallback WITH the canonical
  security_warning surfaced (never hidden). Verified: shipped-config server
  completes MCP `initialize` + `tools/list` = 9 + `workflow_status` executes,
  with only `ZCODE_PLUGIN_ROOT` set (default production env). Updated
  `plugin.json` `toolConfirmation.runtime_gate` + `test_config.py` +
  `test_validate_capabilities.py::test_validate_against_real_config_and_findings`
  (its own comment predicted this flip).

**Round-2 tests:** +1 schema strict-mode (was xfail, now passing),
+1 hook-env default-resolution; 2 existing tests updated for the new shipped
config. Full suite: **560 passed, 0 xfailed**. The earlier `toolConfirmation`
manifest field and the real-ZCode-load observation remain P1 (not P0): the field
is still only locally JSON-tested, and a real ZCode client load + GUI confirm
dialog still needs human eyes (not auto-verifiable in this environment).
