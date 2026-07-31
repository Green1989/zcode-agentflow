---
description: Start a new AgentFlow run for the current repo + goal, then report next step.
---

# /agentflow-start

Start a new AgentFlow single-task run. You (the ZCode supervisor) will drive the
run through the 9 MCP tools in order, stopping at `READY_TO_MERGE`.

## Steps

1. Read the user's goal. Confirm `target_repo` (the repo under change).
2. Call `workflow_status` to check there is no conflicting active run.
3. Call `workflow_init` with `target_repo` and the `goal`.
4. Call `planner_draft_spec` with the `goal` to draft the SPEC.
   - If it returns `NEEDS_USER_INPUT`, ask the user the questions, then
     re-enter `planner_draft_spec` with the `answers`.
5. When the state is `READY_FOR_SPEC_APPROVAL`, surface the SPEC to the user and
   request **explicit approval** (Gate 1). The approval uses the non-adversarial
   convention flow (see `skills/agentflow/SKILL.md`); do not call
   `workflow_approve_spec` until the user approves.
6. Continue through `planner_create_plan` → `workflow_start_task` → edit+commit
   in the worktree → `workflow_verify_task` → `reviewer_review_task` →
   `workflow_complete_task`.

## Rules

- Follow the 9-tool order and `ALLOWED_FROM` exactly (see the SKILL).
- Never edit SPEC/PLAN/state.json or anything under `operations/`, `approvals/`,
  `reviews/`, `artifacts/`, `blobs/` directly — only the tools produce those.
- Stop at `READY_TO_MERGE`; do **not** push, merge, or open a PR.
- `abandon`/`archive` are local admin commands, not MCP tools — do not call them
  as tools.
