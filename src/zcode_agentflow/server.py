"""FastMCP stdio server — the 9-tool MCP surface (Task 1.5 wires workflow_status;
Task 1.6 declares the other 8 signatures; Phase 6 fills the bodies).

Critical invariants:
- stdout is MCP-FRAMES-ONLY (P1-6): every log handler is on stderr; NO print;
  the official FastMCP SDK owns stdout framing. Subprocess adapters (Phase 4)
  capture child stdout and never inherit it here.
- validate_capabilities runs at startup (P1-1) and fails fast — except in test
  mode, where there is no real production config to gate on.
"""

from __future__ import annotations

import logging
import os
import sys

# Configure logging to STDERR ONLY — stdout is the MCP protocol channel (P1-6).
# Do this at import time so any later logging.* call lands on stderr, never stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("AGENTFLOW_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("zcode_agentflow.server")

# Official mcp SDK import path, recorded by the Phase 0 spike
# (docs/capabilities.json versions.mcp_sdk_import_path).
from mcp.server.fastmcp import FastMCP  # noqa: E402

from zcode_agentflow.config import (  # noqa: E402
    CapabilityError,
    validate_capabilities,
)
from zcode_agentflow import bootstrap  # noqa: E402
from zcode_agentflow.store import (  # noqa: E402
    CasMismatch,
    TransitionNotAllowed,
)

__all__ = ["mcp_server", "workflow_status", "get_engine", "main"]

mcp_server = FastMCP("zcode-agentflow")

# How the server discovers the Phase 0 findings + production config at startup.
# In a real plugin run these are resolved from plugin_root; in tests the
# capability gate is bypassed via AGENTFLOW_TEST_MODE.
_CAPABILITIES_PATH = os.environ.get(
    "AGENTFLOW_CAPABILITIES", os.path.join("docs", "capabilities.json")
)
_CONFIG_PATH = os.environ.get("AGENTFLOW_CONFIG", "config.yaml")


def _startup_capability_check() -> None:
    """Run validate_capabilities at server startup (P1-1). Skipped in test mode,
    where no real production config/findings are present."""
    if os.environ.get("AGENTFLOW_TEST_MODE") == "1":
        logger.warning("AGENTFLOW_TEST_MODE=1 — skipping startup capability gate")
        return
    import json
    from pathlib import Path

    from zcode_agentflow import config as config_mod

    cap_path = Path(_CAPABILITIES_PATH)
    cfg_path = Path(_CONFIG_PATH)
    if not cap_path.exists() or not cfg_path.exists():
        # No config to gate on yet — cannot validate; do not silently start.
        raise CapabilityError(
            f"cannot validate capabilities: missing {_CAPABILITIES_PATH!r} or {_CONFIG_PATH!r}"
        )
    findings = json.loads(cap_path.read_text(encoding="utf-8"))["gates"]
    cfg = config_mod.load(cfg_path)
    validate_capabilities(cfg, findings)


@mcp_server.tool()
def workflow_status() -> dict:
    """Return a snapshot of the active run, or {"active_run": None}.

    Reads the active-run pointer (read-only) and surfaces the run's state, task,
    and revision. No Engine is constructed (this is the read-only status probe).
    """
    ctx = bootstrap.load_run_context()
    if ctx is None:
        return {"active_run": None}
    from zcode_agentflow import active_run as _active_run
    from zcode_agentflow import store as _store

    ptr = _active_run.read_pointer(ctx.user_data_root)
    if ptr is None:
        return {"active_run": None}
    try:
        state = _store.State.load(ptr["control_root"])
    except _store.StateError:
        return {"active_run": {"run_id": ptr["run_id"], "unrecoverable": True}}
    return {
        "active_run": {
            "run_id": state.run_id,
            "state": state.state,
            "revision": state.revision,
            "current_task": state.current_task,
            "control_root": ptr["control_root"],
        }
    }


# --- The 8 transition tools delegate to the Engine (Task 6.7). Each builds an
# Engine (for the active run, or for a new run on workflow_init) and calls the
# matching engine method. WorkflowError / CasMismatch / TransitionNotAllowed /
# ArtifactMismatch are ALLOWED to propagate: FastMCP converts an unhandled tool
# exception into an MCP error RESULT (isError=True with the message) — never a
# bare traceback past the protocol, and never a server crash (P0-2).
#
# test-injectable engine factory: tests set AGENTFLOW_RUN_CONTEXT (JSON) so a
# subprocess server builds an Engine bound to a temp plugin_root + fake adapters.


class _EngineCallError(RuntimeError):
    """Surfaced through FastMCP as an MCP error result (isError=True). Used for
    configuration/pointer problems that are not Engine/Store exceptions."""


def _call_engine(method: str, **kwargs):
    """Build the Engine for the active run and call ``method``; return its dict.

    Engine/Store exceptions (WorkflowError / CasMismatch / TransitionNotAllowed
    / ArtifactMismatch / ArtifactCorrupt) propagate so FastMCP maps them to MCP
    error results. A missing run context / no active run is surfaced via
    :class:`_EngineCallError` (also an MCP error result, never a crash)."""
    ctx = bootstrap.load_run_context()
    if ctx is None:
        raise _EngineCallError("no run context configured (AGENTFLOW_RUN_CONTEXT unset)")
    eng = bootstrap.build_engine_for_active_run(ctx)
    return getattr(eng, method)(**kwargs)


@mcp_server.tool()
def workflow_init(target_repo: str, goal: str, run_id: str | None = None) -> dict:
    """NEW -> INITIALIZING_WORKTREE. Create the run worktree (outside the repo),
    record the four roots, capture base_commit. (Phase 3.7/6.8.)

    target_repo: absolute path to the repo under change.
    goal:        the user's natural-language goal for this run.
    run_id:      optional caller-supplied run id (else generated).
    """
    ctx = bootstrap.load_run_context()
    if ctx is None:
        raise _EngineCallError("no run context configured (AGENTFLOW_RUN_CONTEXT unset)")
    eng = bootstrap.build_engine_for_init(ctx, target_repo=target_repo, run_id=run_id)
    # use the run_id resolved by the factory (generated when None) so the
    # allocated control/worktree roots match the id init_run records.
    rid = getattr(eng, "_resolved_run_id", run_id)
    return eng.workflow_init(target_repo=target_repo, goal=goal, run_id=rid)


@mcp_server.tool()
def planner_draft_spec(
    goal: str, answers: str | None = None, expected_revision: int | None = None
) -> dict:
    """-> DRAFTING_SPEC. Codex read-only drafts the SPEC; questions non-empty =>
    NEEDS_USER_INPUT (re-enter with answers); questions empty =>
    READY_FOR_SPEC_APPROVAL. (Phase 6.)

    goal:              the run goal.
    answers:           answers to a prior round of questions (re-entry).
    expected_revision: lightweight CAS (Spec §4 / line 250).
    """
    return _call_engine("planner_draft_spec", goal=goal, answers=answers,
                        expected_revision=expected_revision)


@mcp_server.tool()
def workflow_approve_spec(expected_revision: int) -> dict:
    """READY_FOR_SPEC_APPROVAL -> PLANNING. HUMAN GATE 1: write a trusted
    approval record (Spec §5.4), lock spec_sha256. (Phase 6.)

    expected_revision: CAS — must equal state.revision.
    """
    return _call_engine("workflow_approve_spec", expected_revision=expected_revision)


@mcp_server.tool()
def planner_create_plan(
    approved_spec_sha256: str, expected_revision: int
) -> dict:
    """PLANNING -> READY_FOR_EXECUTION. Codex read-only produces the single-task
    plan; approved_spec_sha256 must match the locked spec. (Phase 6.)

    approved_spec_sha256: the spec sha locked at approval.
    expected_revision:    CAS — must equal state.revision.
    """
    return _call_engine("planner_create_plan",
                        approved_spec_sha256=approved_spec_sha256,
                        expected_revision=expected_revision)


@mcp_server.tool()
def workflow_start_task(task_id: str, expected_revision: int) -> dict:
    """READY_FOR_EXECUTION -> EXECUTING_TASK. Record task_start_commit. (Phase 6.)

    task_id:           the single task id (T001 in v1).
    expected_revision: CAS — must equal state.revision.
    """
    return _call_engine("workflow_start_task", task_id=task_id,
                        expected_revision=expected_revision)


@mcp_server.tool()
def workflow_verify_task(task_id: str, expected_revision: int) -> dict:
    """EXECUTING_TASK -> VERIFYING_TASK; verification pass keeps VERIFYING_TASK;
    failure or unauthorized change -> NEEDS_HUMAN. Runs verification command
    policy (Spec §5.2) + git change policy (§5.3); worktree must be clean.
    (Phase 6.)

    task_id:           the task id.
    expected_revision: CAS — must equal state.revision.
    """
    return _call_engine("workflow_verify_task", task_id=task_id,
                        expected_revision=expected_revision)


@mcp_server.tool()
def reviewer_review_task(task_id: str, expected_revision: int) -> dict:
    """VERIFYING_TASK -> REVIEWING_TASK; PASS keeps REVIEWING_TASK; FAIL/REPLAN/
    BLOCKED -> NEEDS_HUMAN. Grok read-only reviews; cross-field invariants +
    sha binding are checked. (Phase 6.)

    task_id:           the task id.
    expected_revision: CAS — must equal state.revision.
    """
    return _call_engine("reviewer_review_task", task_id=task_id,
                        expected_revision=expected_revision)


@mcp_server.tool()
def workflow_complete_task(task_id: str, expected_revision: int) -> dict:
    """REVIEWING_TASK(PASS) -> READY_TO_MERGE (v1 terminal state). Enforces the
    full hard-check set (Spec §5.1), incl. expected_revision == state.revision.
    The merge itself is performed by the human, outside AgentFlow, in v1.
    (Phase 6.)

    task_id:           the task id.
    expected_revision: CAS — must equal state.revision.
    """
    return _call_engine("workflow_complete_task", task_id=task_id,
                        expected_revision=expected_revision)


def get_engine():
    """Return the workflow Engine for the active run (Task 6.7).

    Builds the Engine from the active-run pointer + the run context
    (``AGENTFLOW_RUN_CONTEXT``). Returns None when there is no active run or no
    run context configured (used by tests that only exercise workflow_status)."""
    ctx = bootstrap.load_run_context()
    if ctx is None:
        return None
    try:
        return bootstrap.build_engine_for_active_run(ctx)
    except Exception:
        return None


def main() -> None:
    """Server entry point: validate capabilities, then run FastMCP on stdio."""
    _startup_capability_check()
    # FastMCP owns stdout (MCP framing). All our logging is on stderr (P1-6).
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
