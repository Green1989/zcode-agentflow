---
name: agentflow
description: ZCode-supervised AgentFlow — drive a single task run from NEW to READY_TO_MERGE via 9 MCP tools, two human gates, versioned-file handoff to a Codex planner and Grok reviewer. Stop at READY_TO_MERGE and hand off to the user; never push or merge.
---

# AgentFlow — supervisor operating guide

AgentFlow is a **process-integrity** workflow: a ZCode supervisor drives one
task from `NEW` to the terminal state `READY_TO_MERGE`, handing structured
artifacts to a **Codex** planner and a **Grok** reviewer through versioned files
+ JSON schemas (no chat relay). The supervisor does **not** push, merge, or open
PRs — that is the user's decision, made **outside** AgentFlow.

## The 9 MCP tools — call them in this order

The MCP surface is **fixed at exactly 9 tools** (Spec §5.1). Call them in this
sequence for a single-task run; each transition is gated by `ALLOWED_FROM`
(the authoritative state a tool may begin from):

1. `workflow_status` — read-only; safe any time (the only read-only tool).
2. `workflow_init` (target_repo, goal) — `NEW → INITIALIZING_WORKTREE`. Creates
   the run worktree **outside** the repo, records the four roots, captures
   `base_commit`.
3. `planner_draft_spec` (goal, answers?) — `→ DRAFTING_SPEC → READY_FOR_SPEC_APPROVAL`
   (or `NEEDS_USER_INPUT` if the planner has questions; re-enter with answers).
4. `workflow_approve_spec` (expected_revision) — **HUMAN GATE 1**:
   `READY_FOR_SPEC_APPROVAL → PLANNING`. Locks `spec_sha256`.
5. `planner_create_plan` (approved_spec_sha256, expected_revision) —
   `PLANNING → READY_FOR_EXECUTION`. v1 allows **exactly one task**.
6. `workflow_start_task` (task_id, expected_revision) —
   `READY_FOR_EXECUTION → EXECUTING_TASK`. Records `task_start_commit`.
7. *(executor edits code, `git add`, `git commit` in the worktree — real work)*
8. `workflow_verify_task` (task_id, expected_revision) —
   `EXECUTING_TASK → VERIFYING_TASK`. Runs verification command policy + git
   change policy; unauthorized change → `NEEDS_HUMAN`.
9. `reviewer_review_task` (task_id, expected_revision) —
   `VERIFYING_TASK → REVIEWING_TASK`. Grok read-only review; PASS keeps the
   state, FAIL/REPLAN/BLOCKED → `NEEDS_HUMAN`.
10. `workflow_complete_task` (task_id, expected_revision) — **HUMAN GATE 2**:
    `REVIEWING_TASK(PASS) → READY_TO_MERGE`. Runs the full hard-check set
    (rehash, freshness, clean worktree). **The merge itself is performed by the
    human, outside AgentFlow.**

> `abandon` and `archive` are **local admin commands**, NOT MCP tools. Do not
> call them as tools — they are run by the operator at the control root.

`expected_revision` is a lightweight CAS: it must equal `state.revision`. A
mismatch means another call advanced the state; re-read `workflow_status`.

## Two human gates

- **Gate 1 — SPEC approval** (`workflow_approve_spec`): the SPEC must be
  approved by a human before planning begins.
- **Gate 2 — final merge** (`workflow_complete_task` → `READY_TO_MERGE`): the
  run stops at `READY_TO_MERGE`; the user decides push/PR/merge externally.

### Gate 1 confirmation is REDUCED in this environment (security warning)

`workflow_approve_spec` is an ordinary MCP tool, so the approval uses
**process-integrity** assurance, not a cryptographic guarantee. Per the Phase-0
findings (`docs/capabilities.json`), ZCode **supports** tool-level mandatory
confirmation but it is **NOT configured** here (`permissions.defaultMode = yolo`
in `~/.claude/settings.json`). Therefore the approval runs the
**non-adversarial convention** fallback:

> Security assurance is REDUCED: ZCode tool-level mandatory confirmation is not
> in effect for workflow_approve_spec. The approval used the non-adversarial
> convention flow, which relies on the model following guidance and does NOT
> defend against a malicious or coerced model. Verify this approval genuinely
> came from a human before proceeding.

**Convention rule (supervisor):** Do **not** call `workflow_approve_spec`
unless the user has explicitly approved the SPEC in this session. Surface the
SPEC, ask for explicit approval, and only then call the tool. This does not
defeat a malicious model, but it is the configured v1 assurance level. (To
upgrade: set `permissions.defaultMode` to confirm/ask and add an ask rule for
`mcp__zcode-agentflow__workflow_approve_spec` in `~/.claude/settings.json`.)

## ALLOWED_FROM — never call a tool from the wrong state

| Tool | Allowed from |
|---|---|
| `workflow_init` | `NEW` |
| `planner_draft_spec` | `INITIALIZING_WORKTREE`, `NEEDS_USER_INPUT` |
| `workflow_approve_spec` | `READY_FOR_SPEC_APPROVAL` |
| `planner_create_plan` | `PLANNING` |
| `workflow_start_task` | `READY_FOR_EXECUTION` |
| `workflow_verify_task` | `EXECUTING_TASK` |
| `reviewer_review_task` | `VERIFYING_TASK` |
| `workflow_complete_task` | `REVIEWING_TASK` |
| `workflow_status` | (any — read-only) |

Calling a tool from a disallowed state is rejected by the engine. When in doubt,
call `workflow_status` first.

## Never edit authoritative artifacts directly

Authoritative artifacts (SPEC.md, PLAN.md, task-manifest.json, state.json,
events.jsonl, and everything under `operations/`, `approvals/`, `reviews/`,
`artifacts/`, `blobs/`) are produced **only** by the AgentFlow tools, under
hash-verified atomic commits. **Never edit SPEC/PLAN/state directly** — not with
Write/Edit, not by shell. Edit code only in the run worktree; let the tools
produce the artifacts. A PreToolUse hook (`protect_artifacts`) denies direct
edits to control data, and the complete-task rehash will detect any tamper.

## READY_TO_MERGE is terminal — hand off, do not push

When `workflow_complete_task` returns `READY_TO_MERGE`:

- **STOP.** Present the handoff (branch, HEAD commit, base commit, changed
  files, test results, review result, SPEC/PLAN sha256) to the user.
- **Do NOT push. Do NOT merge. Do NOT open a PR.** Do not run `git push`,
  `git merge`, or any remote operation. v1 performs none of these.
- The user merges externally, outside AgentFlow.

## Threat model

AgentFlow provides **process-integrity** (it detects direct edits, stale
reviews, hash mismatches, and routes failures to `NEEDS_HUMAN`). It does **not**
defend against a same-privilege malicious process that edits bytes directly
(Spec §1.2), and v1 network isolation is advisory-only.
