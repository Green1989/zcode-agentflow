# Phase 8 — Acceptance Record (v1 vertical slice complete)

This record captures the Task 8.5 verification evidence for the v1 vertical
slice. It is a static snapshot of the checks that passed at completion; the
authoritative proof remains the green test suite (`python -m pytest -q`) and the
three-layer E2E under `tests/e2e/` + `tests/smoke/`.

## Verification (Task 8.5)

**Full suite (smoke excluded):**

```text
$ python -m pytest -q
532 passed in ~114s
```

The default suite does **not** collect `tests/smoke` (`pyproject.toml`
`addopts = ["--ignore=tests/smoke"]`). Layer 3 (real Codex/Grok) is a standalone
script run manually with `AGENTFLOW_SMOKE=1`.

**9-tool MCP surface** — exactly nine, confirmed by inspection of
`src/zcode_agentflow/server.py`:

```
workflow_status, workflow_init, planner_draft_spec, workflow_approve_spec,
planner_create_plan, workflow_start_task, workflow_verify_task,
reviewer_review_task, workflow_complete_task
```

(`abandon` / `archive` are local admin commands, NOT MCP tools — the surface
stays exactly 9.)

**Secret scan** — no token / API-key / private-key patterns in any Phase 8
deliverable (`src/zcode_agentflow/handoff.py`, `tests/e2e/`, `tests/smoke/`,
`docs/README.md`, `docs/smoke.md`).

**Working tree** — clean; no uncommitted changes.

## Three-layer E2E

| Layer | What it proves | Command | Result |
|---|---|---|---|
| **1** Engine E2E | v1 spine as a *code-execution* test (real git repo + worktree + file edit + real commit) drives NEW → READY_TO_MERGE, plus the full rejection matrix (stale review, dirty worktree, multi-task BLOCKED, illegal state, restart recovery, strong/fallback approval). Codex/Grok are fakes. | `python -m pytest tests/e2e/test_engine_e2e.py -v` | **8 passed** |
| **2** MCP stdio E2E | the same run over the **real MCP protocol** (real server subprocess + official SDK client), never calling the Engine directly. Distinctively: process A starts the run, is torn down, a fresh process B resumes it via the active-run pointer + `Store.recover`, completes to READY_TO_MERGE, and a third fresh process observes READY_TO_MERGE via `workflow_status`. | `python -m pytest tests/e2e/test_mcp_transport_e2e.py -v` | **2 passed** |
| **3** Real-CLI smoke | a documented, manually-run smoke against the **real** `codex exec` / `grok -p`. OFF by default; gated on `AGENTFLOW_SMOKE=1` + capability check. | `AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py` (manual) | **harness ready; real CLI run deferred to manual operator execution** (consumes real model tokens + needs login/network/proxy) |

## Handoff report

`workflow_complete_task` at READY_TO_MERGE emits a structured handoff
(`branch`, `head_commit`, `base_commit`, `changed_files`, `test_results`,
`review_result`, `spec_sha256`, `plan_sha256`, `pushed`/`merged`/`pr_created`
all `false`). AgentFlow performs NO push, NO merge, NO PR (Prompt §22; Review
§六.10). See `tests/e2e/test_handoff_report.py` and `src/zcode_agentflow/handoff.py`.

## v1 known limitations

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
  approval is refused → NEEDS_HUMAN (never silent).

## No push / merge / PR

This phase performed **local commits only**. No `git push`, `git merge`, PR
creation, force-reset, branch deletion, or destructive cleanup occurred. The
v1 terminal state is `READY_TO_MERGE`; push/merge/PR is the user's decision,
made outside AgentFlow.
