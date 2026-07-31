# ZCode-AgentFlow — Operator Guide (install / configure / run / recover)

AgentFlow is a ZCode-supervised workflow that drives a **single task** from
`NEW` to the terminal state `READY_TO_MERGE`, handing structured artifacts to a
**Codex (GPT)** planner and a **Grok (Grok-4.5)** reviewer through versioned
files + JSON schemas (no chat relay). The supervisor **never** pushes, merges,
or opens a PR — that is the user's decision, made **outside** AgentFlow.

> Architecture, the 9-tool MCP surface, the state machine, the threat model, and
> the four-roots layout live in `docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md`.
> This guide is operational: how to install, configure, drive a run, and recover.

---

## 1. Install

Per the Phase-0 contract (P1-3) — pinned versions first, then the package with
`--no-deps` so dependency versions cannot drift past the spike-validated freeze:

```bash
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
```

Verify the installed versions match the spike freeze:

```bash
python -c "import mcp, portalocker, jsonschema, yaml; print('mcp', mcp.__version__)"
```

### External dependencies (the real CLIs)

AgentFlow shells out to two CLIs (Phase 0 marked both as **required** gates):

- **codex** (`codex exec`) — the Planner. Logged in via ChatGPT (`~/.codex`).
  `~/.codex/.env` auto-injects proxy env vars, so codex needs no explicit proxy.
- **grok** (`grok -p`) — the Reviewer. Logged in via xAI. **Grok has NO automatic
  proxy injection** (unlike codex) — the Adapter passes
  `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` explicitly in the subprocess env (see
  `config.yaml` `reviewer.proxy`), else the call hangs (gates G7/G8).

Both CLIs are surfaced through `zcode-agentflow` as an MCP stdio server.

---

## 2. Configure

`config.yaml` is the production config, loaded + validated by
`src/zcode_agentflow/config.py` (unknown keys / wrong types are rejected — it is
a validator, not a silent deep-merge). The `capabilities:` block mirrors
`docs/capabilities.json` (the Phase-0 source of truth) and feeds
`validate_capabilities()` at startup.

Key sections:

| Section | Purpose |
|---|---|
| `auth` | codex/grok account login (NOT API tokens) |
| `human_gates` | Gate 1 (SPEC approval) + Gate 2 (`READY_TO_MERGE`, v1 does not merge) |
| `planner` | codex command, model, reasoning effort, read-only sandbox, timeout |
| `reviewer` | grok command, model, **proxy** (G7/G8), timeout |
| `verification` | program/interpreter allowlists (the security source for verification commands) |
| `change_policy` | git change audit policy (delete/dependency/migration/undeclared → `NEEDS_HUMAN`) |
| `capabilities` | Phase-0 gate classifications (required / optional / fallback-capable) |

The MCP server is registered with ZCode via the plugin manifest
(`.zcode-plugin/plugin.json`). The 9-tool surface, the two human-gate skill, and
the slash commands live in `skills/agentflow/SKILL.md` and `commands/`.

---

## 3. Run a single-task run

The supervisor (ZCode, driving the 9 MCP tools) calls them in this order. See
`skills/agentflow/SKILL.md` for the full `ALLOWED_FROM` table.

```
workflow_status          (read-only; safe any time)
workflow_init            NEW → INITIALIZING_WORKTREE  (creates the run worktree)
planner_draft_spec       → DRAFTING_SPEC → READY_FOR_SPEC_APPROVAL
                         (or NEEDS_USER_INPUT — re-enter with answers)
workflow_approve_spec    HUMAN GATE 1: → PLANNING  (locks spec_sha256)
planner_create_plan      → READY_FOR_EXECUTION  (v1: exactly one task)
workflow_start_task      → EXECUTING_TASK  (records task_start_commit)
  *executor edits code, git add, git commit in the worktree*  ← real work
workflow_verify_task     → VERIFYING_TASK  (verification + change audit)
reviewer_review_task     → REVIEWING_TASK  (Grok read-only review; PASS keeps state)
workflow_complete_task   HUMAN GATE 2: → READY_TO_MERGE  (full rehash hard checks)
```

`expected_revision` is a lightweight CAS: it must equal `state.revision`. A
mismatch means another call advanced the state; re-read `workflow_status`.

### Ready-to-merge handoff

When `workflow_complete_task` reaches `READY_TO_MERGE`, its result carries a
structured **handoff** (`branch`, `head_commit`, `base_commit`, `changed_files`,
`test_results`, `review_result`, `spec_sha256`, `plan_sha256`, and
`pushed`/`merged`/`pr_created` all `false`). The supervisor **stops** and
presents it to the user; the user merges externally.

---

## 4. Recover

### A fresh MCP process resumes the in-flight run

Each MCP process, on startup, reads the **active-run pointer**
(`<user_data_root>/active-run.json`) and calls `Store.recover()` under the
per-Run lock. Recovery rebuilds state from the latest event snapshot if
`state.json` is corrupt, classifies any orphan operation (a process killed
mid-external-call), and routes to `NEEDS_HUMAN` when the orphan is not safely
replayable. It **never** scans for "newest" and **never** auto-switches
projects — it trusts the pointer and recovers THAT run only (P0-1).

So: if the ZCode process crashes mid-run, just restart it; `workflow_status`
will reconnect to the same run at its last-committed state.

### `NEEDS_HUMAN` / `NEEDS_USER_INPUT`

Most integrity failures (stale review, dirty worktree, tampered artifact,
unauthorized change, verification failure, non-PASS review, multi-task goal)
route to `NEEDS_HUMAN`. The run stops safely; a human inspects the operation
record under `<control_root>/operations/` and decides. `NEEDS_USER_INPUT` means
the planner asked questions — re-enter `planner_draft_spec` with `answers`.

### abandon / archive (local admin, NOT MCP tools)

`abandon` and `archive` are **local admin commands**, not MCP tools (the surface
stays exactly 9). Run them at the control root, outside the model loop:

```bash
zcode-agentflow admin abandon --run-id <id> --user-data-root <path>
zcode-agentflow admin archive  --run-id <id> --user-data-root <path>
```

`abandon` clears the active-run pointer (a new run may then be started);
`archive` clears the pointer (v1: pointer-clear only; a future phase may relocate
control_root under `archives/`). Both run under the Registry Lock.

### `READY_TO_MERGE` is terminal

`READY_TO_MERGE` marks the pointer `terminal: true` — it is **retained** (not
cleared) so `workflow_status` can still report the final handoff. The pointer is
cleared only on: a new `workflow_init`, explicit `abandon`, or explicit `archive`.

---

## 5. v1 limitations

- **Single task, one review, two gates.** v1 supports exactly one task per run,
  one Grok review (PASS keeps the state), and two human gates (SPEC approval +
  final merge). Multi-task plans route to `NEEDS_HUMAN` (BLOCKED), never silent
  `tasks[0]`.
- **FAIL → `NEEDS_HUMAN`.** Any integrity failure routes to `NEEDS_HUMAN`; there
  is no automatic repair, no replan, and no final review in v1.
- **No auto merge/push/PR.** The terminal state is `READY_TO_MERGE`; the merge
  is performed by the human, outside AgentFlow.
- **Process-integrity, not tamper-proof.** AgentFlow detects direct edits,
  stale reviews, and hash mismatches, but does not defend against a
  same-privilege malicious process that edits bytes directly (Spec §1.2); v1
  network isolation is advisory-only.
- **Gate 1 confirmation is environment-dependent.** If ZCode tool-level
  mandatory confirmation is not configured, the approval uses the
  non-adversarial convention fallback (with a `security_warning`); if fallback
  is disabled, approval is refused → `NEEDS_HUMAN` (never silent).
