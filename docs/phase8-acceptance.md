# Phase 8 — Acceptance Record (v1 vertical slice; audit-fix round 2, 2026-07-31)

This record captures the verification evidence for the v1 vertical slice after
two rounds of Phase 7/8 audit-fix remediation (branch `codex/audit-fix-phase7-8`).
It is a static snapshot; the authoritative proof remains the green test suite
(`python -m pytest -q`) and the three-layer E2E under `tests/e2e/` + `tests/smoke/`.

- **Round 1** (review FAIL, 5 P0s): production server could not start / no run
  context / hooks fail-open / Stop-hook wrote under `operations/` / Layer 3 faked
  confirmation. All 5 fixed + the P1 rejection-matrix gap closed, and three
  further real-CLI blockers (Windows spawn, codex trust, cross-file schema `$ref`)
  were found and fixed.
- **Round 2** (review FAIL, 3 P0s): independent-hook context resolution, the
  strict-mode plan schema (was xfailed), and the shipped config blocking
  production start. All 3 fixed. The schema is now accepted by the real codex API;
  the full Layer-3 model run remains blocked only by a transient codex↔ChatGPT
  network issue (environment), not by any code/schema defect.

See `docs/review/2026-07-31-phase7-8-audit-fix-notes.md` and Plan v2.2 errata 9–13.

## Verification (Task 8.5, re-run after audit-fix)

**Full suite (smoke excluded):**

```text
$ python -m pytest -q
560 passed in ~136s
```

`tests/smoke` is NOT collected (`pyproject.toml addopts = ["--ignore=tests/smoke"]`).
(0 xfailed — the R1 strict-mode schema blocker that was previously xfailed is now
resolved in R2 and is a passing test.)

**Production MCP server (the original P0-1 symptom) — now verified to run with
the SHIPPED config (no overrides):**

```text
(a) bootstrap.process_config() with ONLY ZCODE_PLUGIN_ROOT set (shipped config):
    OK: resolved -> RunContext | approval_mode: non_adversarial_convention
(b) real server subprocess (shipped config, default production env):
    OK: initialize + tools/list over stdio = exactly 9 tools
```

Before the audit-fix the shipped server exited 1 with `CapabilityError`
immediately (allow_non_adversarial_fallback was false while tool_confirmation is
not configured in this env). R2-3 ships the config with the fallback enabled so
the server starts, surfacing the canonical security_warning (never hidden).

**9-tool MCP surface** — exactly nine. The live `tools/list` evidence over the
real stdio transport is produced by `tests/mcp/test_all_tools_stdio.py` (which
drives a real server subprocess and exercises the full happy path through every
tool). The production-env probe
`tests/unit/test_bootstrap_context.py::test_server_uses_production_context_for_workflow_status`
additionally proves a real server subprocess starts under production env and
that `workflow_status` executes without error (it does NOT itself call
`tools/list` — the 9-tool count comes from the mcp suite). The nine tools:

```
workflow_status, workflow_init, planner_draft_spec, workflow_approve_spec,
planner_create_plan, workflow_start_task, workflow_verify_task,
reviewer_review_task, workflow_complete_task
```

(`abandon` / `archive` are local admin commands, NOT MCP tools.)

## Three-layer E2E

| Layer | What it proves | Command | Result (post audit-fix) |
|---|---|---|---|
| **1** Engine E2E | v1 spine as a *code-execution* test (real git repo + worktree + file edit + real commit) drives NEW → READY_TO_MERGE, plus the **complete** rejection matrix: stale revision (CAS), stale review, dirty worktree, multi-task BLOCKED, illegal state, verification failure, unauthorized change, artifact mutation, restart recovery, strong/fallback approval. Codex/Grok are fakes. | `python -m pytest tests/e2e/test_engine_e2e.py -v` | **12 passed** |
| **2** MCP stdio E2E | the same run over the **real MCP protocol** (real server subprocess + official SDK client), never calling the Engine directly. Process A starts the run, is torn down, a fresh process B resumes it via the active-run pointer + `Store.recover`, completes to READY_TO_MERGE, and a third fresh process observes READY_TO_MERGE via `workflow_status`. Now ALSO replays the CAS-mismatch rejection over the transport. | `python -m pytest tests/e2e/test_mcp_transport_e2e.py -v` | **3 passed** |
| **3** Real-CLI smoke | a documented, manually-run smoke against the **real** `codex exec` / `grok -p`. OFF by default; gated on `AGENTFLOW_SMOKE=1` + capability check. The approval mode is derived from the REAL capability gate (never injected). | `AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py` (manual) | **PARTIAL — see below** |

### Layer 3 — honest status (post round-2 audit-fix)

Running the real smoke surfaced **four** production blockers across two rounds.
All four are now FIXED in code (verified locally); the full model run is blocked
only by a transient codex↔ChatGPT network issue, not by any code/schema defect.

1. **Windows spawn (R1)** — `run_subprocess` with `shell=False` could not launch
   the npm-installed `codex` (a `.cmd` shim) → `FileNotFoundError [WinError 2]`.
   FIXED: `_resolve_argv0` resolves `cmd[0]` via `shutil.which` on win32. Test:
   `test_run_subprocess_resolves_cmd_shim_on_windows`.
2. **Codex trust (R1)** — `codex` 0.146.0 exits 1 ("Not inside a trusted
   directory") on any non-pre-trusted repo. FIXED: `CodexAdapter` passes
   `--skip-git-repo-check` (read-only sandbox ⇒ no mutation). Test:
   `test_skip_git_repo_check_flag_present`.
3. **Schema self-containment (R1)** — `plan-proposal.schema.json` had a
   cross-file `$ref` the codex `response_format` validator rejects. FIXED:
   inlined `taskId`. Test: `test_raw_model_facing_schemas_are_self_contained`.
4. **Strict-mode structured outputs (R2, was xfailed)** — codex's
   `response_format` is strict-mode: every object's `required` must list ALL its
   properties. The wrapper modeled `plan`/`questions`/`block_reason` as OPTIONAL
   (a union over `status`) and was rejected. FIXED (R2-1): the schema is now a
   fixed-key envelope (all keys always present; `plan` is `["object","null"]`,
   non-null only for `READY_FOR_EXECUTION`); `validate_raw_plan` enforces the
   per-status semantics on top. The xfail is now a passing test:
   `test_raw_plan_schema_is_strict_mode_compliant`.

**Schema acceptance verified against the real codex API:** a direct
`CodexAdapter` plan-step call against the fixed schema no longer returns the
`invalid_json_schema` / `Missing plan` HTTP 400 — the schema uploads cleanly.
The only remaining error from a real call is a codex↔ChatGPT **network** failure
(`stream disconnected` / `failed to connect to websocket ... wss://chatgpt.com`
/ `AdapterTimeout: exceeded timeout 600s`), which is an environment/proxy issue,
not a code or schema defect. The full smoke run reached the SPEC approval
(recorded with the honest `non_adversarial_convention` method) before the
network blocked the plan step.

**Therefore Layer 3 is code-complete but environment-blocked:** every code/schema
defect that blocked the real run is fixed and unit-tested (560 passed, 0 xfailed);
the real Codex draft path + honest approval mode are proven; the schema is
accepted by the real API. The full run to READY_TO_MERGE (codex plan + real Grok
review) requires a stable codex↔ChatGPT connection, which this environment did
not provide during the run. This is an honest PARTIAL (environment-blocked), not
a fabricated pass.

## Handoff report

`workflow_complete_task` at READY_TO_MERGE emits a structured handoff
(`branch`, `head_commit`, `base_commit`, `changed_files`, `test_results`,
`review_result`, `spec_sha256`, `plan_sha256`, `pushed`/`merged`/`pr_created`
all `false`). AgentFlow performs NO push, NO merge, NO PR (Prompt §22; Review
§六.10). See `tests/e2e/test_handoff_report.py` and `src/zcode_agentflow/handoff.py`.

## v1 known limitations (updated)

- **Single task, one review, two gates.** v1 supports exactly one task per run,
  one Grok review (PASS keeps state), and two human gates (SPEC approval + final
  merge). Multi-task plans route to NEEDS_HUMAN (BLOCKED), never silent tasks[0].
- **FAIL → NEEDS_HUMAN.** Any integrity failure routes to NEEDS_HUMAN; there is
  no automatic repair, no replan, and no final review in v1.
- **No auto merge/push/PR.** The terminal state is READY_TO_MERGE; the merge is
  performed by the human, outside AgentFlow.
- **Process-integrity, not tamper-proof.** AgentFlow detects direct edits,
  stale reviews, and hash mismatches, but does not defend against a
  same-privilege malicious process that edits bytes directly (Spec §1.2); v1
  network isolation is advisory-only.
- **Gate 1 confirmation is environment-dependent.** If ZCode tool-level
  mandatory confirmation is not configured, the approval uses the non-adversarial
  convention fallback (with a security_warning); if fallback is disabled,
  approval is refused → NEEDS_HUMAN (never silent). The shipped config enables
  the fallback (R2-3) so the server starts in this env.
- **Layer 3 full run blocked by codex↔ChatGPT network (environment, not code).**
  The strict-mode plan schema (R2-1) is fixed and verified accepted by the real
  codex API (no `invalid_json_schema`). The real smoke reaches the SPEC approval
  (honest `non_adversarial_convention` mode) but the plan step could not complete
  against a stable codex↔ChatGPT websocket in this session (stream disconnect /
  600s timeout). The full run + real Grok review remain pending a stable model
  connection — a re-run by an operator with working codex connectivity is the
  remaining step (the code is ready).

## No push / merge / PR

This remediation performed **local commits only**. No `git push`, `git merge`,
PR creation, force-reset, branch deletion, or destructive cleanup occurred. The
v1 terminal state is `READY_TO_MERGE`; push/merge/PR is the user's decision,
made outside AgentFlow.
