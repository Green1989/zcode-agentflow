"""Task 3.6 — Fault-injection recovery matrix across the two-phase flow
(Prompt §3,§10).

Proves that recovery is correct at EVERY crash point in
begin / external / finish / promotion. For each crash point:

  1. run a two-phase operation up to the injected fault and STOP (simulate a
     process kill at exactly that step);
  2. construct a FRESH ``Store`` (no shared in-memory state) and call
     ``recover()``;
  3. assert the recovered state is consistent with the latest event's snapshot,
     and the operation is either COMMITTED+idempotent OR classified UNKNOWN with
     the correct ``replay_policy`` outcome.

Crash injection is driven by the ``AGENTFLOW_CRASH_AFTER`` env var, which the
Store consults at named checkpoints (``_crash_checkpoint``). Setting it to a
checkpoint name raises ``CrashInjected`` at that point, mid-transaction. The
test then recovers from disk as a new process would.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zcode_agentflow import artifacts, store


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


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


@pytest.fixture()
def crash_env(monkeypatch):
    """Helper: set the crash checkpoint, then clear it on teardown."""
    saved = {}

    def set_checkpoint(name):
        monkeypatch.setenv("AGENTFLOW_CRASH_AFTER", name)
        saved["name"] = name

    yield set_checkpoint
    monkeypatch.delenv("AGENTFLOW_CRASH_AFTER", raising=False)


def _begin_op(s, *, op_id="opA", tool="planner_draft_spec",
              inter="DRAFTING_SPEC", replay="COSTFUL_READ"):
    return s.begin_operation(
        operation_id=op_id,
        request={"tool": tool, "args": {}},
        tool=tool,
        expected_revision=store.State.load(s.control_root).revision,
        intermediate_state=inter,
        replay_policy=replay,
    )


def _finish_op(s, br, *, next_state="READY_FOR_SPEC_APPROVAL",
               staged=None, result=None):
    s.finish_operation(
        operation_id=br.operation_id,
        lease_token=br.lease_token,
        expected_revision=br.revision_after_begin,
        next_state=next_state,
        staged_artifacts=staged or {},
        result=result,
    )


# --------------------------------------------------------------------------- #
# crash matrix: parametrized over every checkpoint
# --------------------------------------------------------------------------- #

# Each checkpoint: (name, does_begin_succeed, expected_recovered_state_substring,
#                    expected_op_status_or_outcome)
# - "begin_*" checkpoints fire DURING begin_operation (begin raises).
# - "finish_*" checkpoints fire DURING finish_operation (finish raises).
CRASH_POINTS = [
    # --- begin phase ---
    "begin_after_request_written",     # after request.json + PREPARED->RUNNING
    "begin_after_event",               # after the begin event is appended
    "begin_after_state_save",          # after the intermediate state.json save
    # --- finish / promotion phase ---
    "finish_after_blobs",              # after prepare_generation writes blobs
    "finish_after_manifest",           # after the generation manifest is written
    "finish_after_result",             # after op result is written
    "finish_after_snapshot",           # after the state_after snapshot
    "finish_after_event",              # after the finish event append
    "finish_after_state_replace",      # AFTER state.json replace (committed)
]


@pytest.mark.parametrize("crash_point", CRASH_POINTS)
def test_crash_then_recover_is_consistent(tmp_path: Path, crash_env, crash_point):
    """At every crash checkpoint, a fresh Store.recover() produces a state that
    is consistent with the events' latest snapshot, and the op ends up COMMITTED
    (if the crash was after the state replace) or classified UNKNOWN with the
    correct replay outcome."""
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    crash_env(crash_point)

    s = store.Store(control)
    committed_before_crash = False
    # Stage a SPEC so promotion has something to write (for finish checkpoints).
    staged = {"SPEC.md": b"# Spec\n"}

    # Drive begin -> external (no-op) -> finish, expecting a crash at the point.
    try:
        br = _begin_op(s)
        # external phase: a real caller would run the adapter here; we skip it
        # (the crash points exercise the bookkeeping, not the adapter).
        _finish_op(s, br, staged=staged, result={"ok": True})
        committed_before_crash = True
    except store.CrashInjected:
        pass  # the simulated kill

    # A FRESH Store (new process): recover from disk only.
    fresh = store.Store(control)
    recovered = fresh.recover()

    # the recovered state must be parseable and self-consistent
    assert isinstance(recovered, store.State)
    assert recovered.run_id == "20260730120000-abc123"
    # active_operation_id is ALWAYS cleared by recover (no stranded op), whether
    # the crash left one set or not.
    assert recovered.active_operation_id is None

    op = store.OperationManager(control).open_existing("opA")

    if crash_point in ("finish_after_state_replace", "finish_after_event"):
        # crash AFTER the single state.json replace (the transition committed):
        # the run state advanced, the op is reconciled to COMMITTED, and the new
        # generation is visible (state points at it).
        assert recovered.state == "READY_FOR_SPEC_APPROVAL"
        assert op.status == "COMMITTED"
        resolver = artifacts.ArtifactResolver(control, recovered)
        assert resolver.read_text("SPEC.md") == "# Spec\n"
    elif crash_point == "begin_after_request_written":
        # crash DURING begin BEFORE any state transition: the op record exists
        # (PREPARED/RUNNING) but no transition landed and active_operation_id
        # was never set — a discarded prepare, not an orphan. State is unchanged.
        assert recovered.state == "INITIALIZING_WORKTREE"
        assert op.status in ("PREPARED", "RUNNING")  # discarded, not committed
        assert fresh.last_recover_outcome() is None  # nothing to classify
    else:
        # crash mid-flight with the run in the intermediate (or pre-begin) state:
        # the transition did NOT commit, so the op is classified UNKNOWN and the
        # COSTFUL_READ replay outcome is NEEDS_HUMAN (no auto-replay config).
        assert recovered.state in ("INITIALIZING_WORKTREE", "DRAFTING_SPEC")
        assert op.status != "COMMITTED"
        assert fresh.last_recover_outcome() == "NEEDS_HUMAN"


# --------------------------------------------------------------------------- #
# targeted crash semantics
# --------------------------------------------------------------------------- #


def test_crash_before_state_replace_keeps_old_artifacts(tmp_path, crash_env):
    """Crash after blobs written but before state replace: on recover, the OLD
    generation is visible (old SPEC), never new+old-state mix. The partial new
    generation is discarded (state does not point at it)."""
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)

    # first commit a gen1 SPEC v1 (no crash)
    s = store.Store(control)
    br = _begin_op(s)
    _finish_op(s, br, staged={"SPEC.md": b"# Spec v1\n"}, result={"ok": 1})

    # now begin a SECOND op and crash during its finish, after blobs (v2)
    crash_env("finish_after_blobs")
    s2 = store.Store(control)
    # second op needs a legit source state; move to PLANNING then begin create_plan
    st = store.State.load(control)
    st.state = "PLANNING"
    st.save(control)
    br2 = s2.begin_operation(
        operation_id="opB",
        request={"tool": "planner_create_plan", "args": {}},
        tool="planner_create_plan",
        expected_revision=store.State.load(control).revision,
        intermediate_state="READY_FOR_EXECUTION",
        replay_policy="COSTFUL_READ",
    )
    try:
        s2.finish_operation(
            operation_id="opB", lease_token=br2.lease_token,
            expected_revision=br2.revision_after_begin,
            next_state="READY_FOR_EXECUTION",
            staged_artifacts={"PLAN.md": b"# Plan v2\n"}, result={"ok": 2},
        )
    except store.CrashInjected:
        pass

    fresh = store.Store(control)
    recovered = fresh.recover()
    resolver = artifacts.ArtifactResolver(control, recovered)
    # OLD SPEC v1 visible; the partial PLAN v2 generation is invisible
    assert resolver.read_text("SPEC.md") == "# Spec v1\n"
    with pytest.raises(artifacts.ArtifactNotFound):
        resolver.read("PLAN.md")


def test_recover_rebuilds_state_from_latest_event(tmp_path):
    """Corrupt/missing state.json => recover rebuilds from the latest event's
    state_after snapshot (verified by sha)."""
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)
    br = _begin_op(s)
    _finish_op(s, br, staged={"SPEC.md": b"# Spec\n"}, result={"ok": True})

    # corrupt state.json (truncated garbage)
    (control / "state.json").write_bytes(b"{ not json")

    fresh = store.Store(control)
    recovered = fresh.recover()
    assert recovered.state == "READY_FOR_SPEC_APPROVAL"
    assert recovered.run_id == "20260730120000-abc123"


def test_recover_clears_orphan_active_operation(tmp_path):
    """A process killed mid-external left active_operation_id set; recover
    detects the orphan, classifies it by replay_policy, and clears it."""
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)
    br = _begin_op(s)  # leaves active_operation_id="opA", mid-external

    # simulate a kill mid-external (do NOT finish/fail/unknown)
    assert store.State.load(control).active_operation_id == "opA"

    fresh = store.Store(control)
    recovered = fresh.recover()
    assert recovered.active_operation_id is None
    op = store.OperationManager(control).open_existing("opA")
    assert op.status == "UNKNOWN"
    # COSTFUL_READ -> NEEDS_HUMAN (no config)
    assert fresh.last_recover_outcome() == "NEEDS_HUMAN"


def test_recover_safe_read_orphan_eligible_for_replay(tmp_path):
    """A SAFE_READ orphan op -> recover marks it UNKNOWN but eligible for
    auto-replay (outcome REPLAY)."""
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)
    s.begin_operation(
        operation_id="op_git",
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=store.State.load(control).revision,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="SAFE_READ",
    )
    fresh = store.Store(control)
    fresh.recover()
    assert fresh.last_recover_outcome() == "REPLAY"
    assert store.OperationManager(control).open_existing("op_git").status == "UNKNOWN"
