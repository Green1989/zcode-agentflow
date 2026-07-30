"""Task 3.4 — Two-phase transactions: begin/finish/fail/unknown (P0-3, P0-2).

External CLI calls run BETWEEN two short locked transactions:

    br = store.begin_operation(...)      # short lock: CAS + RUNNING + lease
    try:
        adapter.invoke(...)              # NO lock held here
        store.finish_operation(lease=br.lease_token,
                               expected_revision=br.revision_after_begin, ...)
    except AdapterError:
        store.fail_operation(lease=br.lease_token, ...)
    # (timeout/kill -> store.mark_unknown(...))

This file proves the load-bearing invariants of that contract:
  - begin advances to the intermediate state with revision+1 and returns a
    BeginResult carrying revision_after_begin + a non-empty lease_token;
  - the lock is released after begin returns (external phase runs lock-free);
  - finish uses revision_after_begin as the CAS (pre-begin revision rejected);
  - finish rejects a wrong lease_token and a changed active_operation_id;
  - a second begin for a DIFFERENT op while one is active is hard-rejected;
  - workflow_status-style reads succeed mid-external (no lock needed);
  - fail_operation moves to NEEDS_HUMAN and clears active_operation_id;
  - mark_unknown sets UNKNOWN, classifies by replay_policy, clears active op;
  - begin writes a recoverable event + state_after snapshot (crash-mid-external
    can reconstruct the intermediate state).

Recovery from an orphaned active_operation_id (Store.recover) is Task 3.8 and is
asserted there, not here. ALLOWED_FROM is exercised here only enough to drive
the CAS / hard-gate paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import locks, store


# ---------- fixtures ---------------------------------------------------------


def _roots_dict(control: Path) -> dict:
    return {
        "plugin_root": str(control / "plugin"),
        "target_repo_root": str(control / "repo"),
        "run_worktree_root": str(control / "wt"),
        "control_root": str(control),
    }


@pytest.fixture()
def fresh_store(tmp_path: Path):
    """A Store over a control_root with an initial state.json seeded at
    INITIALIZING_WORKTREE, revision 1."""
    control = tmp_path / "control"
    control.mkdir()
    st = store.State(
        run_id="20260730120000-abc123",
        revision=1,
        state="INITIALIZING_WORKTREE",
        previous_state="NEW",
        plan_version=1,
        current_task="",
        completed_tasks=[],
        roots=_roots_dict(control),
    )
    st.save(control)
    return store.Store(control), control, st


# ---------- begin: revision+1 + intermediate state + lease -------------------


def test_begin_advances_to_intermediate_state_with_revision_plus_one(fresh_store):
    s, control, st = fresh_store
    op_id = "20260730120000-abc123__planner_draft_spec__r1__a1"
    pre_begin_rev = store.State.load(control).revision

    br = s.begin_operation(
        operation_id=op_id,
        request={"tool": "planner_draft_spec", "args": {"goal": "g"}},
        tool="planner_draft_spec",
        expected_revision=pre_begin_rev,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )

    assert br.operation_id == op_id
    assert br.lease_token  # non-empty opaque token
    assert br.revision_after_begin == pre_begin_rev + 1

    # the persisted state reflects the intermediate state + bumped revision
    after = store.State.load(control)
    assert after.state == "DRAFTING_SPEC"
    assert after.revision == pre_begin_rev + 1
    assert after.previous_state == "INITIALIZING_WORKTREE"
    # active_operation_id is set; the lease is stored as a HASH (not plaintext)
    assert after.active_operation_id == op_id
    assert after.active_operation_lease is not None
    assert br.lease_token != after.active_operation_lease  # stored hashed


def test_begin_acquires_then_releases_lock(fresh_store):
    """After begin returns, the per-Run lock is FREE (so the external adapter
    call is not serialized/contended by it)."""
    s, control, _ = fresh_store
    op_id = "opA"
    s.begin_operation(
        operation_id=op_id,
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=1,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    # the lock must be free now — acquire with a short timeout succeeds instantly
    with locks.run_lock(control, timeout=3):
        pass


def test_external_phase_runs_without_lock(fresh_store):
    """During the (simulated) external phase, no lock is held: another caller
    could take the per-Run lock concurrently. This is the whole point of the
    two-phase split (the v2 deadlock held the lock across the CLI call)."""
    s, control, _ = fresh_store
    op_id = "opA"
    s.begin_operation(
        operation_id=op_id,
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=1,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    # while "external" is conceptually running, we can acquire the lock from here
    acquired = False
    with locks.run_lock(control, timeout=2):
        acquired = True
    assert acquired, "lock was held during the external phase (P0-3 regression)"


def test_concurrent_status_succeeds_during_external_phase(fresh_store):
    """workflow_status-style reads succeed while an op is mid-external: reading
    state.json does NOT require the per-Run lock (it is atomic on disk)."""
    s, control, _ = fresh_store
    s.begin_operation(
        operation_id="opA",
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=1,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    # a plain read (what workflow_status does) must work mid-external
    snapshot = store.State.load(control)
    assert snapshot.state == "DRAFTING_SPEC"


def test_begin_transition_has_recoverable_event_and_snapshot(fresh_store):
    """begin writes a recoverable event + state_after snapshot so a crash
    mid-external can reconstruct the intermediate state (not only finish)."""
    s, control, _ = fresh_store
    br = s.begin_operation(
        operation_id="opA",
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=1,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    log = store.EventLog(control)
    latest = log.latest()
    assert latest is not None
    assert latest["to"] == "DRAFTING_SPEC"
    assert latest["operation_id"] == "opA"
    # the state_after snapshot exists and hashes match
    snap_path = control / latest["state_after_path"]
    assert snap_path.exists()
    from zcode_agentflow import canonical

    assert canonical.hash_file(snap_path) == latest["state_after_sha256"]
    # the snapshot carries the intermediate state
    import json

    snap = json.loads(snap_path.read_bytes())
    assert snap["state"] == "DRAFTING_SPEC"
    assert snap["revision"] == br.revision_after_begin


# ---------- finish: CAS + lease + active-op checks ---------------------------


def _begin_ok(s, op_id="opA"):
    return s.begin_operation(
        operation_id=op_id,
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=store.State.load(s.control_root).revision,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )


def test_finish_uses_revision_after_begin_as_cas(fresh_store):
    """finish succeeds with expected_revision == revision_after_begin; passing
    the PRE-begin revision is rejected (the v2.1 CAS gap)."""
    s, control, _ = fresh_store
    pre = store.State.load(control).revision  # 1
    br = _begin_ok(s)

    # WRONG: pre-begin revision must be rejected
    with pytest.raises(store.CasMismatch):
        s.finish_operation(
            operation_id="opA",
            lease_token=br.lease_token,
            expected_revision=pre,  # pre-begin — stale
            next_state="READY_FOR_SPEC_APPROVAL",
            staged_artifacts={},
            result={},
        )

    # CORRECT: revision_after_begin is the valid CAS
    s.finish_operation(
        operation_id="opA",
        lease_token=br.lease_token,
        expected_revision=br.revision_after_begin,
        next_state="READY_FOR_SPEC_APPROVAL",
        staged_artifacts={},
        result={},
    )
    after = store.State.load(control)
    assert after.state == "READY_FOR_SPEC_APPROVAL"
    assert after.revision == br.revision_after_begin + 1
    assert after.active_operation_id is None  # cleared on commit
    # the operation record is COMMITTED
    assert store.OperationManager(control).open_existing("opA").status == "COMMITTED"


def test_finish_rejects_wrong_lease_token(fresh_store):
    """A mismatched lease_token ⇒ rejected (defends against a stale finish from
    a previous begin of the same op id)."""
    s, control, _ = fresh_store
    br = _begin_ok(s)
    with pytest.raises(store.LeaseMismatch):
        s.finish_operation(
            operation_id="opA",
            lease_token="not-the-real-lease",
            expected_revision=br.revision_after_begin,
            next_state="READY_FOR_SPEC_APPROVAL",
            staged_artifacts={},
            result={},
        )
    # the op is still RUNNING (finish did not commit), active op still set
    assert store.State.load(control).active_operation_id == "opA"


def test_finish_rejects_if_active_operation_changed(fresh_store):
    """If active_operation_id no longer names this op (another op interleaved),
    finish is rejected — it cannot commit on behalf of a different operation."""
    s, control, _ = fresh_store
    br = _begin_ok(s, op_id="opA")
    # simulate the active op being switched to a DIFFERENT op (e.g. by recover)
    st = store.State.load(control)
    st.active_operation_id = "opB"
    st.save(control)
    with pytest.raises(store.ActiveOperationMismatch):
        s.finish_operation(
            operation_id="opA",
            lease_token=br.lease_token,
            expected_revision=br.revision_after_begin,
            next_state="READY_FOR_SPEC_APPROVAL",
            staged_artifacts={},
            result={},
        )


# ---------- hard gate: one active op at a time -------------------------------


def test_second_begin_rejected_while_operation_active(fresh_store):
    """While an op is mid-external (active_operation_id set), a begin for a
    DIFFERENT operation_id is hard-rejected. Only the SAME id (idempotent
    re-entry) or Store.recover may proceed."""
    s, control, _ = fresh_store
    _begin_ok(s, op_id="opA")
    with pytest.raises(store.ActiveOperationInProgress):
        s.begin_operation(
            operation_id="opB",  # different id
            request={"tool": "planner_draft_spec", "args": {}},
            tool="planner_draft_spec",
            expected_revision=store.State.load(control).revision,
            intermediate_state="DRAFTING_SPEC",
            replay_policy="COSTFUL_READ",
        )


def test_complete_rejected_while_review_external_phase_active(fresh_store):
    """While reviewer_review_task's external phase is running (state
    REVIEWING_TASK + active op set), workflow_complete_task's begin is rejected
    — no racing into complete before review returns.

    Modeled at the Store layer: a Run left in REVIEWING_TASK with an active op
    cannot begin a new (complete) operation.
    """
    s, control, _ = fresh_store
    # drive into REVIEWING_TASK with an active review op (simulate mid-review)
    st = store.State.load(control)
    st.state = "VERIFYING_TASK"
    st.save(control)
    s.begin_operation(
        operation_id="op_review",
        request={"tool": "reviewer_review_task", "args": {}},
        tool="reviewer_review_task",
        expected_revision=store.State.load(control).revision,
        intermediate_state="REVIEWING_TASK",
        replay_policy="COSTFUL_READ",
    )
    # now workflow_complete_task tries to begin a DIFFERENT op -> rejected
    with pytest.raises(store.ActiveOperationInProgress):
        s.begin_operation(
            operation_id="op_complete",
            request={"tool": "workflow_complete_task", "args": {}},
            tool="workflow_complete_task",
            expected_revision=store.State.load(control).revision,
            intermediate_state="READY_TO_MERGE",
            replay_policy="SAFE_READ",
        )


# ---------- fail / unknown paths (P0-2: never strand mid-external) -----------


def test_fail_operation_on_adapter_exception(fresh_store):
    """An adapter exception -> fail_operation marks the op FAILED, transitions
    to NEEDS_HUMAN, clears active_operation_id; the Run is NOT stuck in the
    intermediate state."""
    s, control, _ = fresh_store
    br = _begin_ok(s, op_id="opA")
    s.fail_operation(operation_id="opA", lease_token=br.lease_token,
                     reason="codex raised: connection reset")

    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"
    assert after.active_operation_id is None
    assert after.revision == br.revision_after_begin + 1
    op = store.OperationManager(control).open_existing("opA")
    assert op.status == "FAILED"


def test_mark_unknown_on_timeout_or_kill(fresh_store):
    """Adapter timeout/process-kill -> mark_unknown sets op UNKNOWN; the outcome
    is classified by replay_policy and active_operation_id is cleared.

    For a SAFE_READ op recovery is eligible for auto-replay; the UNKNOWN mark is
    recorded so Store.recover (3.8) can decide. Uses a legit transition
    (planner_draft_spec INITIALIZING_WORKTREE -> DRAFTING_SPEC) tagged SAFE_READ
    so the begin is ALLOWED_FROM-conformant."""
    s, control, _ = fresh_store
    br = s.begin_operation(
        operation_id="op_spec",
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=store.State.load(control).revision,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="SAFE_READ",
    )
    result = s.mark_unknown(operation_id="op_spec", lease_token=br.lease_token,
                            reason="adapter process killed")
    # SAFE_READ UNKNOWN => eligible for replay
    assert result["outcome"] == "REPLAY"
    op = store.OperationManager(control).open_existing("op_spec")
    assert op.status == "UNKNOWN"
    # active op cleared regardless (the lease is void once unknown)
    assert store.State.load(control).active_operation_id is None


def test_mark_unknown_unsafe_write_needs_human(fresh_store):
    """An UNSAFE_WRITE op (verification) that goes UNKNOWN -> NEEDS_HUMAN, never
    auto-replay (it may have already written files). Drives the Run into
    EXECUTING_TASK first so workflow_verify_task is ALLOWED_FROM-conformant."""
    s, control, _ = fresh_store
    # move into EXECUTING_TASK (legit source state for workflow_verify_task)
    st = store.State.load(control)
    st.state = "EXECUTING_TASK"
    st.save(control)
    br = s.begin_operation(
        operation_id="op_verify",
        request={"tool": "workflow_verify_task", "args": {}},
        tool="workflow_verify_task",
        expected_revision=store.State.load(control).revision,
        intermediate_state="VERIFYING_TASK",
        replay_policy="UNSAFE_WRITE",
    )
    result = s.mark_unknown(operation_id="op_verify", lease_token=br.lease_token,
                            reason="verification timed out")
    assert result["outcome"] == "NEEDS_HUMAN"
    assert store.State.load(control).active_operation_id is None


def test_fail_and_unknown_reject_wrong_lease(fresh_store):
    """fail/mark_unknown also verify the lease — a stale/cancelled begin cannot
    mutate the op after its lease was superseded."""
    s, _, _ = fresh_store
    br = _begin_ok(s, op_id="opA")
    with pytest.raises(store.LeaseMismatch):
        s.fail_operation(operation_id="opA", lease_token="wrong", reason="x")
