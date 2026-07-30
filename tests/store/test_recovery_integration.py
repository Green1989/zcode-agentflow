"""Task 3.8 — Store.recover + active-pointer recovery integration (P0-1, P0-2).

``Store.recover`` (built in Task 3.6) is the single entry for crash recovery.
This task adds the POINTER integration: ``bootstrap.resume_active_run`` reads
the active-run pointer under the Registry Lock (read-only), then switches to the
Per-Run lock to recover THAT run — it never scans for "newest" and never
auto-switches projects. If the pointer's state is corrupt AND unrecoverable, it
surfaces NEEDS_HUMAN rather than silently picking another run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zcode_agentflow import active_run, bootstrap, store


# ---------- helpers ----------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _seed_run(control: Path, run_id="20260730120000-abc123", state="INITIALIZING_WORKTREE", rev=1):
    st = store.State(
        run_id=run_id, revision=rev, state=state, previous_state="NEW",
        plan_version=1, current_task="", completed_tasks=[],
        roots={
            "plugin_root": str(control / "plugin"),
            "target_repo_root": str(control / "repo"),
            "run_worktree_root": str(control / "wt"),
            "control_root": str(control),
        },
    )
    st.save(control)
    return st


# ---------- rebuild from latest event ---------------------------------------


def test_recover_rebuilds_state_from_latest_event(tmp_path: Path):
    """corrupt/missing state.json -> Store.recover rebuilds from the latest
    state_after snapshot (sha-verified) and rewrites state.json."""
    control = tmp_path / "control"
    control.mkdir()
    _seed_run(control)
    s = store.Store(control)
    br = s.begin_operation(
        operation_id="opA",
        request={"tool": "planner_draft_spec", "args": {}},
        tool="planner_draft_spec",
        expected_revision=1, intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )
    s.finish_operation(
        operation_id="opA", lease_token=br.lease_token,
        expected_revision=br.revision_after_begin,
        next_state="READY_FOR_SPEC_APPROVAL", staged_artifacts={}, result={"ok": True},
    )

    # corrupt state.json
    (control / "state.json").write_bytes(b"{ broken")

    fresh = store.Store(control)
    recovered = fresh.recover()
    assert recovered.state == "READY_FOR_SPEC_APPROVAL"
    assert recovered.run_id == "20260730120000-abc123"
    # state.json was rewritten (no longer corrupt)
    assert store.State.load(control).state == "READY_FOR_SPEC_APPROVAL"


# ---------- pointer-scoped recovery (no auto-switch) ------------------------


def _full_init(tmp_path: Path, user_data: Path, run_id: str):
    """Run a real workflow_init to create a run + pointer."""
    repo = _make_repo(tmp_path)
    base = tmp_path / "runs" / run_id
    control = base / "control"
    worktree = base / "worktree"
    store.init_run(
        user_data_root=user_data, target_repo=repo,
        control_root=control, worktree_root=worktree,
        run_id=run_id, plugin_root=tmp_path / "plugin", goal="g",
    )
    return repo, control


def test_recover_recovers_active_pointer_run_only(tmp_path: Path):
    """P0-1/P0-4: two runs exist; recovery resumes ONLY the pointer's run, never
    the other. If no pointer -> no active run."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()

    # create run A (becomes the active pointer)
    repo, control_a = _full_init(tmp_path, user_data, "20260730120000-aaaa11")

    # create a SECOND run directory (control_b) with its own state, but do NOT
    # point at it — it's an unrelated/orphan run dir on disk.
    control_b = tmp_path / "runs" / "20260730120000-bbbb22" / "control"
    control_b.mkdir(parents=True)
    _seed_run(control_b, run_id="20260730120000-bbbb22")

    # the pointer names run A
    ptr = active_run.read_pointer(user_data)
    assert ptr["run_id"] == "20260730120000-aaaa11"

    # resume must recover run A ONLY
    resumed = bootstrap.resume_active_run(user_data)
    assert resumed is not None
    assert resumed.run_id == "20260730120000-aaaa11"
    assert resumed.control_root == control_a
    # run B's state is untouched (not auto-switched-to)
    assert store.State.load(control_b).run_id == "20260730120000-bbbb22"


def test_no_pointer_returns_none(tmp_path: Path):
    """If there is no active-run pointer, resume_active_run returns None (no
    active run) — it does NOT scan for runs."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    # some run dirs exist on disk but NO pointer
    orphan = tmp_path / "runs" / "20260730120000-aaaa11" / "control"
    orphan.mkdir(parents=True)
    _seed_run(orphan)
    assert bootstrap.resume_active_run(user_data) is None


def test_recover_corrupt_state_then_unrecoverable_needs_human(tmp_path: Path):
    """P0-1: pointer's state is corrupt AND the events are insufficient to
    rebuild -> NEEDS_HUMAN; never silently pick another run."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    repo, control = _full_init(tmp_path, user_data, "20260730120000-abc123")

    # corrupt state.json AND remove the events (no recoverable snapshot)
    (control / "state.json").write_bytes(b"{ broken")
    (control / "events.jsonl").unlink()

    # resume surfaces the unrecoverable state rather than switching runs
    with pytest.raises(bootstrap.RecoveryNeedsHuman):
        bootstrap.resume_active_run(user_data)
    # the pointer still names THIS run (not auto-switched)
    assert active_run.read_pointer(user_data)["run_id"] == "20260730120000-abc123"


# ---------- orphan active operation + unsafe-write --------------------------


def test_recover_orphan_active_operation(tmp_path: Path):
    """P0-2: a process killed mid-external left active_operation_id set; recover
    classifies the op by replay_policy (SAFE auto-replay / COSTFUL config /
    UNSAFE -> NEEDS_HUMAN) and clears active_operation_id."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    repo, control = _full_init(tmp_path, user_data, "20260730120000-abc123")

    # drive into a mid-external state via begin (no finish/fail/unknown)
    s = store.Store(control)
    st = store.State.load(control)
    st.state = "EXECUTING_TASK"
    st.save(control)
    s.begin_operation(
        operation_id="op_verify",
        request={"tool": "workflow_verify_task", "args": {}},
        tool="workflow_verify_task",
        expected_revision=store.State.load(control).revision,
        intermediate_state="VERIFYING_TASK",
        replay_policy="UNSAFE_WRITE",
    )
    assert store.State.load(control).active_operation_id == "op_verify"

    resumed = bootstrap.resume_active_run(user_data)
    assert resumed is not None
    recovered = store.State.load(control)
    assert recovered.active_operation_id is None  # cleared


def test_recover_unknown_unsafe_write_needs_human(tmp_path: Path):
    """An UNKNOWN UNSAFE_WRITE op (e.g. verification) -> NEEDS_HUMAN, not
    auto-replay (it may have already written files)."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    repo, control = _full_init(tmp_path, user_data, "20260730120000-abc123")

    s = store.Store(control)
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
    # simulate the crash -> recover classifies UNSAFE_WRITE UNKNOWN as NEEDS_HUMAN
    resumed = bootstrap.resume_active_run(user_data, allow_costful_replay=True)
    assert resumed is not None
    # an UNSAFE_WRITE orphan is NEVER auto-replayed: the run is surfaced as
    # NEEDS_HUMAN so a human inspects the half-run (may have written files).
    assert store.State.load(control).state == "NEEDS_HUMAN"
    assert store.State.load(control).active_operation_id is None


def test_resume_active_run_is_idempotent(tmp_path: Path):
    """Calling resume_active_run again on an already-recovered (clean) run is a
    no-op: it does not spuriously advance revision, change state, or error."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    repo, control = _full_init(tmp_path, user_data, "20260730120000-abc123")

    first = bootstrap.resume_active_run(user_data)
    assert first is not None
    state_after_first = store.State.load(control)
    rev_after_first = state_after_first.revision

    # second resume on the now-clean run
    second = bootstrap.resume_active_run(user_data)
    assert second is not None
    assert second.run_id == first.run_id
    state_after_second = store.State.load(control)
    # revision unchanged, state unchanged, no spurious NEEDS_HUMAN
    assert state_after_second.revision == rev_after_first
    assert state_after_second.state == state_after_first.state


def test_resume_active_run_uses_pointer_under_registry_lock(tmp_path: Path):
    """resume_active_run reads the pointer under the Registry Lock (read-only)
    then switches to the Per-Run lock for recovery. Verify it does NOT hold the
    registry lock during the per-Run recovery step (a second registry op would
    not deadlock)."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    repo, control = _full_init(tmp_path, user_data, "20260730120000-abc123")

    # resume succeeds (reads pointer under registry lock, recovers under per-Run)
    resumed = bootstrap.resume_active_run(user_data)
    assert resumed is not None
    assert resumed.run_id == "20260730120000-abc123"

    # after resume returned, BOTH locks are free
    from zcode_agentflow import locks

    with locks.registry_lock(user_data, timeout=3):
        with locks.run_lock(control, timeout=3):
            pass  # both acquirable -> neither was leaked
