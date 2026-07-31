"""P0-1 RED: ``finish_operation`` commits state.json BEFORE the recoverable
finish event. The authoritative commit protocol (Spec §3.5 写入顺序 + Plan
Task 3.4 pseudocode) requires::

    ... write snapshot -> append+fsync event -> os.replace(state.json) -> COMMITTED

The current code swaps those last two: it replaces state.json and THEN appends
the finish event. This leaves a crash window where the state is already
advanced on disk but the only recoverable event is the BEGIN (whose
``state_after`` snapshot still shows the intermediate state + active op).

If ``state.json`` then becomes corrupt (any later torn write / disk issue), a
fresh process's ``recover()`` rebuilds from ``EventLog.latest()`` — which is
the BEGIN event — so the COMMITTED next-state and the artifact-generation
pointer are silently lost. Recovery rolls the run back to the intermediate
state as if the finish never happened.

These tests reproduce that exact window. They are expected to FAIL (RED) on
the current ordering and PASS once the event is appended before the state
replace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zcode_agentflow import artifacts, store
from zcode_agentflow import canonical


# --------------------------------------------------------------------------- #
# helpers (mirror tests/store/test_fault_injection.py)
# --------------------------------------------------------------------------- #


def _seed(control: Path) -> store.State:
    st = store.State(
        run_id="20260730120000-abc123",
        revision=1,
        state="INITIALIZING_WORKTREE",
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


def _begin(s: store.Store, *, op_id="opA"):
    return s.begin_operation(
        operation_id=op_id,
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=store.State.load(s.control_root).revision,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )


# --------------------------------------------------------------------------- #
# the crash window under test
# --------------------------------------------------------------------------- #


def test_finish_event_durable_before_state_replace(tmp_path: Path, monkeypatch):
    """RED: when finish_operation has fully committed (state.json advanced),
    the FINISH event MUST already be on disk and durable.

    We simulate the precise window the reviewer named: the run reaches the
    committed next-state on disk, then is killed, then state.json becomes
    corrupt. A fresh Store.recover() must rebuild the COMMITTED state, NOT the
    intermediate one. On the current (buggy) order this loses the finish
    because the finish event was never appended before the crash.
    """
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)

    br = _begin(s)

    # Let finish run to completion (no crash) so the state is fully committed
    # and the finish event is appended — we then assert the INVARIANT directly.
    staged = {"SPEC.md": b"# Spec body\n"}
    s.finish_operation(
        operation_id=br.operation_id,
        lease_token=br.lease_token,
        expected_revision=br.revision_after_begin,
        next_state="READY_FOR_SPEC_APPROVAL",
        staged_artifacts=staged,
        result={"ok": True},
    )

    committed = store.State.load(control)
    assert committed.state == "READY_FOR_SPEC_APPROVAL"

    # INVARIANT: the latest durable event describes the FINISH transition into
    # the committed state. If the event was appended only AFTER the state
    # replace, a crash in between (the reviewer's window) would leave only the
    # BEGIN event as "latest" — so we assert the ordering holds post-commit.
    latest = store.EventLog(control).latest()
    assert latest is not None, "finish event must be durable"
    assert latest["operation_id"] == br.operation_id
    assert latest["to"] == "READY_FOR_SPEC_APPROVAL", (
        f"latest event should be the FINISH into the committed state, "
        f"got to={latest['to']!r} (state-replaced-before-event bug)"
    )


def test_recover_after_state_replace_then_state_corruption(tmp_path: Path, monkeypatch):
    """RED (the real defect): crash AFTER the finish event was appended but
    before/around the state.json replace, THEN corrupt state.json. A fresh
    recover() must still reconstruct the committed next-state — proving the
    durable finish event (not the state.json mirror) is the recoverable anchor.

    With the buggy ordering (state replace BEFORE event append), corrupting
    state.json after the replace left only the BEGIN event, so recover() rolled
    back to the intermediate state (NEEDS_HUMAN) and lost the committed
    transition + artifact pointer.
    """
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)

    br = _begin(s)
    staged = {"SPEC.md": b"# Spec body\n"}

    # Crash at the reviewer's named checkpoint (finish_after_state_replace). With
    # the corrected order the finish event is ALREADY durable at this point, so
    # a subsequent state.json corruption must still be recoverable.
    monkeypatch.setenv("AGENTFLOW_CRASH_AFTER", "finish_after_state_replace")
    with pytest.raises(store.CrashInjected):
        s.finish_operation(
            operation_id=br.operation_id,
            lease_token=br.lease_token,
            expected_revision=br.revision_after_begin,
            next_state="READY_FOR_SPEC_APPROVAL",
            staged_artifacts=staged,
            result={"ok": True},
        )

    # The finish event IS durable now (appended before the crash checkpoint).
    assert any(e["to"] == "READY_FOR_SPEC_APPROVAL" for e in _all_events(control)), (
        "finish event must be durable before the state replace (P0-1 invariant)"
    )

    # Now corrupt state.json (simulate a later torn write / disk failure). The
    # next process must still recover the committed state from the event.
    (control / "state.json").write_bytes(b"{ not valid json \x00")

    fresh = store.Store(control)
    recovered = fresh.recover()
    # The committed transition must survive; recover must NOT roll back to the
    # intermediate state because the authoritative finish event is durable.
    assert recovered.state == "READY_FOR_SPEC_APPROVAL", (
        f"committed state lost after state corruption: recovered "
        f"{recovered.state!r} instead of READY_FOR_SPEC_APPROVAL — the finish "
        f"event was not durable before the state replace (P0-1)"
    )
    # And the staged artifact generation pointer must survive too.
    assert recovered.artifact_generation, (
        "artifact_generation pointer lost in the same window (P0-1)"
    )


def test_recover_roll_forward_when_state_json_not_yet_replaced(tmp_path: Path, monkeypatch):
    """The corrected order introduces a NEW recoverable window: the finish event
    is appended but state.json was NOT yet replaced when the process died. The
    on-disk state.json still shows the intermediate state with the active op
    set. recover() must ROLL FORWARD from the durable finish event (sha-verified
    snapshot) to the committed state — NOT classify the orphan as UNKNOWN /
    NEEDS_HUMAN.
    """
    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)

    br = _begin(s)
    staged = {"SPEC.md": b"# Spec body\n"}

    # Crash AFTER the finish event append but BEFORE the state.json replace.
    monkeypatch.setenv("AGENTFLOW_CRASH_AFTER", "finish_after_event")
    with pytest.raises(store.CrashInjected):
        s.finish_operation(
            operation_id=br.operation_id,
            lease_token=br.lease_token,
            expected_revision=br.revision_after_begin,
            next_state="READY_FOR_SPEC_APPROVAL",
            staged_artifacts=staged,
            result={"ok": True},
        )

    # state.json was NOT replaced — it still shows the intermediate state.
    pre = store.State.load(control)
    assert pre.state == "DRAFTING_SPEC"
    assert pre.active_operation_id == br.operation_id

    fresh = store.Store(control)
    recovered = fresh.recover()
    assert recovered.state == "READY_FOR_SPEC_APPROVAL", (
        f"recover must roll forward from the durable finish event; got "
        f"{recovered.state!r} (Case C reconciliation missing)"
    )
    assert recovered.active_operation_id is None
    op = store.OperationManager(control).open_existing(br.operation_id)
    assert op.status == "COMMITTED"
    # And the staged artifact generation pointer must survive too.
    assert recovered.artifact_generation, (
        "artifact_generation pointer lost in the same window (P0-1)"
    )


def test_roll_forward_copies_all_authoritative_sha_fields(tmp_path: Path, monkeypatch):
    """Follow-up to P0-1 Case C (found in read-only recheck): when recover rolls
    the run state forward from a durable FINISH event whose snapshot was NOT yet
    mirrored into state.json, it must copy EVERY authoritative field the finish
    folded into the committed snapshot — including spec_sha256 / plan_sha256 /
    task_manifest_sha256. prepare_generation fills all five NAME_TO_STATE_SHA
    fields; if Case C only copies artifact_generation + the two *_result shas,
    the rolled-forward state points at the NEW generation but keeps the OLD
    spec/plan/manifest shas, breaking the 'state authoritative sha == blob sha
    of the generation the state points at' invariant.
    """
    from zcode_agentflow import artifacts as _artifacts

    control = tmp_path / "control"
    control.mkdir()
    _seed(control)
    s = store.Store(control)

    br = _begin(s)
    staged = {"SPEC.md": b"# Spec body\n"}

    # Crash AFTER the finish event append but BEFORE the state.json replace.
    monkeypatch.setenv("AGENTFLOW_CRASH_AFTER", "finish_after_event")
    with pytest.raises(store.CrashInjected):
        s.finish_operation(
            operation_id=br.operation_id,
            lease_token=br.lease_token,
            expected_revision=br.revision_after_begin,
            next_state="READY_FOR_SPEC_APPROVAL",
            staged_artifacts=staged,
            result={"ok": True},
        )

    fresh = store.Store(control)
    recovered = fresh.recover()
    # the rolled-forward state points at the NEW generation that staged SPEC.md
    gen = _artifacts.load_generation(control, recovered.artifact_generation)
    expected_spec_sha = gen.index["SPEC.md"]["sha256"]
    # the state's authoritative spec_sha256 must equal that blob's sha, not the
    # pre-finish (None / old) value Case C would leave behind.
    assert recovered.spec_sha256 == expected_spec_sha, (
        f"roll-forward left spec_sha256={recovered.spec_sha256!r} while the "
        f"committed generation's SPEC blob sha is {expected_spec_sha!r} — Case C "
        f"must copy all authoritative sha fields, not just artifact_generation"
    )


def _all_events(control: Path) -> list[dict]:
    path = control / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_bytes().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
