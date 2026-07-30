"""Task 3.2 — Events (resumable seq) + state snapshot (Review §六.2).

``EventLog`` appends recoverable events to ``<control_root>/events.jsonl``;
its next seq is derived from disk (so a restart CONTINUES the seq rather than
resetting to 1). Each event carries a ``state_after_path`` +
``state_after_sha256`` pointing at an immutable state snapshot, so a corrupted
``state.json`` can be rebuilt from the latest event.

``State`` is the in-memory state.json model; ``save()`` is an atomic
``write_canonical_json`` + ``os.replace`` (concurrent readers never see a
half-write), and ``load`` round-trips every field.

Scope of THIS task: the events + state snapshot primitives only. The full
operation lifecycle / two-phase transactions land in 3.3–3.4.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from zcode_agentflow import canonical, store


# ---------- EventLog: resumable seq ------------------------------------------


def test_seq_resumes_after_restart(tmp_path: Path):
    """After a restart, the next seq CONTINUES from disk — it does not reset
    to 1. This is what makes events.jsonl a recovery source."""
    control = tmp_path / "control"
    control.mkdir()

    log1 = store.EventLog(control)
    assert log1.next_seq == 1  # fresh
    log1.append(
        operation_id="op1",
        from_state="INITIALIZING_WORKTREE",
        to_state="DRAFTING_SPEC",
        reason="planner_draft_spec",
        state_after_path="operations/op1/state_after.json",
        state_after_sha="a" * 64,
    )
    log1.append(
        operation_id="op2",
        from_state="DRAFTING_SPEC",
        to_state="READY_FOR_SPEC_APPROVAL",
        reason="planner_draft_spec",
        state_after_path="operations/op2/state_after.json",
        state_after_sha="b" * 64,
    )
    assert log1.next_seq == 3

    # simulate a restart: a NEW EventLog reading the same disk
    log2 = store.EventLog(control)
    assert log2.next_seq == 3, "seq must resume from disk, not reset to 1"
    log2.append(
        operation_id="op3",
        from_state="READY_FOR_SPEC_APPROVAL",
        to_state="PLANNING",
        reason="planner_create_plan",
        state_after_path="operations/op3/state_after.json",
        state_after_sha="c" * 64,
    )
    assert log2.next_seq == 4
    assert log2.latest()["seq"] == 3
    assert log2.latest()["operation_id"] == "op3"


def test_event_carries_recoverable_state_after(tmp_path: Path):
    """Each event carries state_after_path + a matching state_after_sha256, so a
    later crash can rebuild state.json from the latest event."""
    control = tmp_path / "control"
    control.mkdir()

    # write an immutable snapshot and hash its canonical bytes
    snapshot = {"run_id": "20260730120000-abc123", "state": "DRAFTING_SPEC", "revision": 2}
    snap_path = control / "operations" / "op1" / "state_after.json"
    snap_path.parent.mkdir(parents=True)
    canonical.write_canonical_json(snap_path, snapshot)
    expected_sha = canonical.hash_file(snap_path)

    log = store.EventLog(control)
    log.append(
        operation_id="op1",
        from_state="INITIALIZING_WORKTREE",
        to_state="DRAFTING_SPEC",
        reason="planner_draft_spec",
        state_after_path="operations/op1/state_after.json",
        state_after_sha=expected_sha,
    )

    ev = log.latest()
    assert ev is not None
    assert ev["state_after_path"] == "operations/op1/state_after.json"
    assert ev["state_after_sha256"] == expected_sha
    # the sha matches the canonical bytes of the file it points at
    assert canonical.hash_file(control / ev["state_after_path"]) == expected_sha


def test_eventlog_latest_none_when_empty(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    assert store.EventLog(control).latest() is None


# ---------- State: atomic save + round-trip ----------------------------------


def _sample_state(control: Path) -> store.State:
    return store.State(
        run_id="20260730120000-abc123",
        revision=7,
        state="REVIEWING_TASK",
        previous_state="VERIFYING_TASK",
        plan_version=1,
        current_task="T001",
        completed_tasks=[],
        roots={
            "plugin_root": str(control / "plugin"),
            "target_repo_root": str(control / "repo"),
            "run_worktree_root": str(control / "wt"),
            "control_root": str(control),
        },
        spec_sha256="a" * 64,
        plan_sha256="b" * 64,
        task_manifest_sha256="c" * 64,
        base_commit="abc1234",
        task_start_commit="def5678",
        last_reviewed_commit="def5678",
        review_round=1,
        repair_round=0,
        updated_at="2026-07-30T11:30:00+00:00",
    )


def _state_writer(control_root: str, stop_event):
    """Child writer: repeatedly rewrite state.json with a fresh revision so the
    parent reader races the atomic replace. Module-level (spawn-picklable)."""
    from zcode_agentflow import store

    st = store.State.load(control_root)
    i = 100
    while not stop_event.is_set():
        st.revision = i
        st.updated_at = f"2026-07-30T11:30:{i:02d}+00:00"
        st.save(control_root)
        i += 1


def _state_reader(control_root: str, stop_event, observed):
    """Child reader: read state.json in a tight loop; record every observed
    revision and whether any read was a torn/partial JSON. Module-level
    (spawn-picklable)."""
    import json
    import time

    from zcode_agentflow import store

    seen = set()
    torn = False
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and not stop_event.is_set():
        try:
            st = store.State.load(control_root)
            seen.add(st.revision)
        except store.StateError:
            # a torn read would surface as a parse failure here
            torn = True
    observed.put(("seen", sorted(seen)))
    observed.put(("torn", torn))


def test_state_save_is_atomic_replace(tmp_path: Path):
    """A concurrent reader never observes a half-written state.json: save() is
    canonical-write + os.replace. While the writer hammers state.json, a reader
    in a SEPARATE process must see only COMPLETE states (never a torn/partial
    JSON) and must observe the revision advancing.

    (The reader uses State.load in its own process; on the rare Windows tick
    where os.replace races an open reader handle, the reader either sees the
    old complete file or the new complete file — never a partial — which is
    exactly the atomicity guarantee being asserted.)
    """
    control = tmp_path / "control"
    control.mkdir()
    st = _sample_state(control)
    st.save(control)

    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    q = ctx.Queue()
    writer = ctx.Process(target=_state_writer, args=(str(control), stop))
    reader = ctx.Process(target=_state_reader, args=(str(control), stop, q))
    writer.start()
    reader.start()
    try:
        reader.join(timeout=10)
    finally:
        stop.set()
        writer.join(timeout=10)

    seen = None
    torn = None
    while not q.empty():
        kind, val = q.get()
        if kind == "seen":
            seen = val
        elif kind == "torn":
            torn = val
    assert torn is False, (
        "reader observed a torn/partial state.json — save() is not atomic"
    )
    assert seen and len(seen) > 1, (
        f"reader never observed advancing revisions: {seen}"
    )


def test_load_state_roundtrips_all_fields(tmp_path: Path):
    """Every field set on a State survives save -> load with no loss."""
    control = tmp_path / "control"
    control.mkdir()
    st = _sample_state(control)
    st.save(control)

    loaded = store.State.load(control)
    assert loaded.run_id == st.run_id
    assert loaded.revision == 7
    assert loaded.state == "REVIEWING_TASK"
    assert loaded.previous_state == "VERIFYING_TASK"
    assert loaded.plan_version == 1
    assert loaded.current_task == "T001"
    assert loaded.completed_tasks == []
    assert loaded.roots == st.roots
    assert loaded.spec_sha256 == "a" * 64
    assert loaded.plan_sha256 == "b" * 64
    assert loaded.task_manifest_sha256 == "c" * 64
    assert loaded.base_commit == "abc1234"
    assert loaded.task_start_commit == "def5678"
    assert loaded.last_reviewed_commit == "def5678"
    assert loaded.review_round == 1
    assert loaded.repair_round == 0
    assert loaded.updated_at == "2026-07-30T11:30:00+00:00"
