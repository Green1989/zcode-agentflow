---
description: Report the current AgentFlow run state, task, and next allowed tool.
---

# /agentflow-status

Report where the active AgentFlow run is and what to do next.

## Steps

1. Call `workflow_status` (read-only; safe any time).
2. Report:
   - the run id, current state, revision, and current task;
   - whether the run is terminal (`READY_TO_MERGE`) — if so, present the handoff
     (branch, HEAD, changed files, review) and remind the user the merge is done
     externally;
   - if there is no active run, say so.
3. State the **next allowed tool** from `ALLOWED_FROM` (see
   `skills/agentflow/SKILL.md`), and any blocker (`NEEDS_HUMAN` /
   `NEEDS_USER_INPUT`).

## Rules

- This command only **reads** state — it never mutates `state.json` or any
  artifact.
- Do not push, merge, or open a PR. v1 terminal state is `READY_TO_MERGE`.
- `abandon`/`archive` are local admin commands, not MCP tools.
