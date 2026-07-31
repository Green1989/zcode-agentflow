"""P1 (same-operation-id re-entry): the review noted that idempotent re-entry
with the SAME operation_id is nominally allowed (the hard gate at
``begin_operation`` lets the same id through) but in practice a mid-external
crash leaves the run in the INTERMEDIATE state, which is usually NOT in the
tool's ``ALLOWED_FROM`` set — so the retry is rejected by the ALLOWED_FROM check
before the hard gate's idempotent branch can help.

This file documents the ACTUAL behavior and pins the contract. The two tests
below encode what the system guarantees today:

  1. A SECOND operation with a DIFFERENT id while one is mid-external is rejected
     (the hard gate) — already covered, restated here for the contract.
  2. The SAME operation_id retried after a mid-external crash: today this is
     rejected because the intermediate state is not a valid source state. This
     test ASSERTS that current behavior so a future change to allow idempotent
     same-id re-entry from the intermediate state is a DELIBERATE, tested change
     rather than an accidental one.

If/when idempotent same-id re-entry from the intermediate state is added, test
(2) should be FLIPPED to assert success (the retry resumes the op) and a comment
referencing the design decision added.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import store


def _seed(control: Path, *, state="INITIALIZING_WORKTREE", rev=1) -> store.State:
    st = store.State(
        run_id="20260730120000-abc123",
        revision=rev,
        state=state,
        previous_state="NEW",
        plan_version=1,
        current_task="",
        completed_tasks=[],
        roots={
            "plugin_root": str(control / "plugin"),
            "target_repo_root": str(control / "repo"),
            "run_worktree_root": str(control / "wt"),
            "control_root": str(control),
        },
    )
    st.save(control)
    return st


def test_different_operation_id_while_active_is_rejected(tmp_path: Path):
    """While opA is mid-external (active_operation_id set), a begin for a
    DIFFERENT id opB is hard-rejected with ActiveOperationInProgress."""
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)

    br = s.begin_operation(
        operation_id="run__planner_draft_spec__r1__a1",
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=store.State.load(control).revision,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    # opA is now mid-external (active set, state DRAFTING_SPEC)
    assert store.State.load(control).active_operation_id == br.operation_id

    with pytest.raises(store.ActiveOperationInProgress):
        s.begin_operation(
            operation_id="run__planner_draft_spec__r1__a2",  # different id
            request={"tool": "planner_draft_spec", "args": {}},
            tool="planner_draft_spec",
            expected_revision=store.State.load(control).revision,
            intermediate_state="DRAFTING_SPEC",
            replay_policy="COSTFUL_READ",
        )


def test_same_operation_id_reentry_after_midexternal_crash_current_behavior(
    tmp_path: Path,
):
    """After a mid-external crash leaves the run in the INTERMEDIATE state
    (DRAFTING_SPEC) and recover clears the orphan, retrying the SAME operation
    id is — per the reviewer's note — rejected by ALLOWED_FROM, because the
    intermediate state is not a valid source state for planner_draft_spec.

    This test PINS that current behavior. It is the documented contract today;
    flipping it to assert success is a deliberate future change (and must then
    also update ALLOWED_FROM or add an explicit re-entry path).
    """
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)
    op_id = "run__planner_draft_spec__r1__a1"

    br = s.begin_operation(
        operation_id=op_id,
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=store.State.load(control).revision,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    # simulate the crash: do NOT finish; recover clears the orphan.
    fresh = store.Store(control)
    recovered = fresh.recover()
    # The orphan was COSTFUL_READ (not safely replayable), so recover advances
    # the run to NEEDS_HUMAN for a human to inspect the half-run op. The run is
    # NOT silently left in the intermediate state.
    assert recovered.state == "NEEDS_HUMAN"
    assert recovered.active_operation_id is None  # orphan cleared

    # The same operation id retried from NEEDS_HUMAN is still not a valid
    # planner_draft_spec source state (allowed: INITIALIZING_WORKTREE,
    # NEEDS_USER_INPUT). The retry is rejected — NEEDS_HUMAN is a human-gate
    # terminal, not an auto-retry source. This PINS the contract: recovery of a
    # COSTFUL mid-external crash is a HUMAN action, not an idempotent same-id
    # resume. Flipping this to assert success is a deliberate future design
    # change (e.g. an explicit re-entry path from NEEDS_HUMAN).
    with pytest.raises(store.TransitionNotAllowed):
        s.begin_operation(
            operation_id=op_id,
            request={"tool": "planner_draft_spec", "args": {}},
            tool="planner_draft_spec",
            expected_revision=store.State.load(control).revision,
            intermediate_state="DRAFTING_SPEC",
            replay_policy="COSTFUL_READ",
        )
