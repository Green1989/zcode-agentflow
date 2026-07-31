"""Task 8.4 — READY_TO_MERGE handoff report (Prompt §22; Review §六.10).

``build_handoff`` is a **side-effect-free read** of the run's committed state +
artifacts. It assembles the structured handoff the supervisor presents to the
user at READY_TO_MERGE: branch, head/base commits, changed files, verification
results, review result, and the SPEC/PLAN sha256 anchors. It performs NO git
operation and writes nothing — AgentFlow never pushes, merges, or opens a PR
(the v1 terminal state is READY_TO_MERGE; push/merge/PR is the user's decision,
made outside AgentFlow).

The handoff is wired into ``workflow_complete_task``'s result so the MCP tool
that reaches READY_TO_MERGE is the one that emits the handoff (the supervisor
then stops and surfaces it).
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["HANDOFF_FIELDS", "build_handoff"]

# The required handoff fields (Prompt §22). Every field must be present and
# non-empty at READY_TO_MERGE; tests assert each one.
HANDOFF_FIELDS = (
    "run_id", "branch", "state",
    "base_commit", "head_commit",
    "changed_files", "test_results", "review_result",
    "spec_sha256", "plan_sha256",
    "pushed", "merged", "pr_created",
)


def build_handoff(eng) -> dict:
    """Assemble the READY_TO_MERGE handoff from committed state + artifacts.

    ``eng`` is an ``engine.Engine`` bound to the run's four roots. The function
    reads ``state.json`` + the committed execution-result / review-result via
    the ArtifactResolver (never a direct control_root file read). It mutates
    nothing on disk.

    The branch is derived deterministically from the run id
    (``agentflow/{run_id}``, matching ``store.init_run``). ``pushed`` /
    ``merged`` / ``pr_created`` are always False — AgentFlow performs none of
    these; they are stated explicitly so a downstream consumer cannot mistake a
    missing key for "unknown".
    """
    # imported lazily to avoid an import cycle at module load (engine imports
    # this module's name via the package __init__).
    from zcode_agentflow import artifacts as _artifacts, store as _store

    state = _store.State.load(eng.roots.control_root)
    resolver = _artifacts.ArtifactResolver(eng.roots.control_root, state)

    exec_obj = json.loads(resolver.read("execution-result.json"))
    review_obj = json.loads(resolver.read("review-result.json"))

    return {
        "run_id": state.run_id,
        "branch": f"agentflow/{state.run_id}",
        "state": state.state,
        "base_commit": state.base_commit,
        "head_commit": exec_obj["head_commit"],
        "changed_files": list(exec_obj.get("changed_files", [])),
        "test_results": list(exec_obj.get("verification", [])),
        "review_result": {
            "decision": review_obj.get("decision"),
            "spec_compliance": review_obj.get("spec_compliance"),
            "plan_compliance": review_obj.get("plan_compliance"),
            "tests_sufficient": review_obj.get("tests_sufficient"),
            "issues": list(review_obj.get("issues", [])),
        },
        "spec_sha256": state.spec_sha256,
        "plan_sha256": state.plan_sha256,
        # AgentFlow NEVER pushes/merges/opens a PR (Prompt §22; Review §六.10).
        # Stated explicitly so the absence of an action is unambiguous.
        "pushed": False,
        "merged": False,
        "pr_created": False,
    }
