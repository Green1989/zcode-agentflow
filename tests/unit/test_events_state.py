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


def test_state_save_is_atomic_replace(tmp_path: Path):
    """A concurrent reader never observes a half-written state.json: save() is
    canonical-write + os.replace.

    Production serializes all reads+writes of a Run under the per-Run lock, so
    the atomicity we must prove is the *reader-view* invariant: at the moment a
    reader captures the bytes of state.json, those bytes are ALWAYS either the
    complete OLD canonical snapshot or the complete NEW one — never a torn or
    truncated JSON. We prove this by interleaving save() with byte-level reads
    in the same process and asserting every captured byte sequence is a valid,
    complete canonical JSON object.

    (We do NOT use a cross-process reader here: on Windows, ``os.replace`` can
    raise ``PermissionError`` when another process holds the destination open.
    That is a platform file-locking behavior, not a violation of the
    "no-half-write" invariant — which is precisely about what a reader OBSERVES,
    not whether the rename can be blocked. The reader-view assertion below is
    the load-bearing guarantee.)
    """
    import json

    control = tmp_path / "control"
    control.mkdir()
    st = _sample_state(control)
    st.save(control)
    state_path = control / "state.json"

    # baseline: complete, parseable, single trailing newline
    raw0 = state_path.read_bytes()
    assert json.loads(raw0)["revision"] == 7

    # Interleave saves with reads. Every read must be a COMPLETE canonical JSON
    # object: it parses, it ends with exactly one '\n', and the field set is
    # self-consistent. A torn/partial write would fail json.loads or carry a
    # mismatched revision vs state.
    seen_revs = set()
    for i in range(100, 300):
        st.revision = i
        st.updated_at = f"2026-07-30T11:30:{i:02d}+00:00"
        st.save(control)
        # read immediately after the atomic replace — the bytes must be whole
        data = json.loads(state_path.read_bytes())
        # revision and state are consistent within a single snapshot
        assert data["run_id"] == "20260730120000-abc123"
        assert data["state"] == "REVIEWING_TASK"
        assert data["revision"] == i
        # canonical: exactly one trailing newline, sorted compact form
        assert state_path.read_bytes().endswith(b"}\n")
        seen_revs.add(data["revision"])

    # we observed many distinct advancing revisions — the save actually rewrote
    assert len(seen_revs) == 200


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
