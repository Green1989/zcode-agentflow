"""P0-2 RED: ``workflow_init`` wrote ``base_commit`` OUTSIDE the init
transaction.

The old ``init_run`` did::

    s.finish_operation(...)            # commits the INITIALIZING_WORKTREE state
    committed = State.load(control)    # re-open the JUST-committed state
    committed.base_commit = base_commit
    committed.save(control)            # a SECOND os.replace, no lock, no event

A crash between ``finish_operation`` and that second save leaves a run whose
init is COMMITTED but whose ``state.base_commit`` was never recorded. Nothing in
``recover()`` reconstructs it, so the run loses its authoritative base commit.

The fix is to fold ``base_commit`` into the state committed by the SINGLE
``finish_operation`` os.replace, so the two never diverge. These tests assert
that atomicity: the state_after snapshot that ``finish`` writes (the durable,
sha-anchored view recover rebuilds from) MUST already carry ``base_commit``.

Expected to FAIL (RED) on the current code (the snapshot lacks base_commit
because it is set only after finish returns) and PASS once base_commit is part
of the committed next-state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from zcode_agentflow import store


# --------------------------------------------------------------------------- #
# helpers (mirror tests/unit/test_workflow_init_recovery.py)
# --------------------------------------------------------------------------- #


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _paths(tmp_path: Path, run_id="20260730120000-abc123"):
    base = tmp_path / "runs"
    return base / run_id / "control", base / run_id / "worktree"


# --------------------------------------------------------------------------- #
# the atomicity property under test
# --------------------------------------------------------------------------- #


def test_base_commit_is_part_of_the_committed_init_state(tmp_path: Path):
    """RED: the state_after_finish snapshot (the durable view recover rebuilds
    from) MUST carry ``base_commit``. On the current code it does not, because
    base_commit is set in a separate save AFTER finish_operation returns.
    """
    repo = _make_repo(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control, worktree = _paths(tmp_path)

    result = store.init_run(
        user_data_root=user_data,
        target_repo=repo,
        control_root=control,
        worktree_root=worktree,
        run_id="20260730120000-abc123",
        plugin_root=tmp_path / "plugin",
        goal="sample goal",
    )
    expected_base = result["base_commit"]
    assert expected_base, "test harness: init should produce a base_commit"

    # Find the state_after_finish snapshot written by finish_operation (the
    # durable, sha-anchored view recover() rebuilds from). It MUST already
    # contain base_commit if base_commit is part of the atomic commit.
    snap = _finish_snapshot(control)
    assert snap is not None, "no state_after_finish snapshot found"
    assert snap.get("base_commit") == expected_base, (
        f"base_commit is NOT part of the committed init state: the finish "
        f"snapshot carries {snap.get('base_commit')!r} instead of "
        f"{expected_base!r}. base_commit is written in a separate save after "
        f"finish_operation commits — a crash there leaves a committed init "
        f"with no authoritative base_commit (P0-2)."
    )


def test_base_commit_survives_a_crash_right_after_finish(tmp_path: Path, monkeypatch):
    """RED: crash immediately AFTER finish_operation commits the init state but
    BEFORE the second base_commit save. A fresh recover() must still observe the
    authoritative base_commit (it was part of the atomic commit, not a later
    write). On the current code recover sees base_commit=None.
    """
    repo = _make_repo(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control, worktree = _paths(tmp_path)

    # finish_operation's last checkpoint is finish_after_state_replace. Crashing
    # there leaves the init committed; the post-finish base_commit save never
    # runs. (If the fix folds base_commit into finish, it IS in the committed
    # state, so this crash loses nothing.)
    monkeypatch.setenv("AGENTFLOW_CRASH_AFTER", "finish_after_state_replace")
    with pytest.raises(store.CrashInjected):
        store.init_run(
            user_data_root=user_data,
            target_repo=repo,
            control_root=control,
            worktree_root=worktree,
            run_id="20260730120000-abc123",
            plugin_root=tmp_path / "plugin",
            goal="sample goal",
        )

    # the init state is committed (state.json was replaced)
    st = store.State.load(control)
    assert st.state == "INITIALIZING_WORKTREE"

    # a fresh Store recovers; the authoritative base_commit must survive
    fresh = store.Store(control)
    recovered = fresh.recover()
    assert recovered.state == "INITIALIZING_WORKTREE"
    assert recovered.base_commit is not None, (
        "committed init lost its base_commit across the post-finish crash "
        "window (P0-2): base_commit was not part of the atomic init commit"
    )


# --------------------------------------------------------------------------- #


def _finish_snapshot(control: Path):
    """Return the dict written as the state_after_finish snapshot, or None."""
    ops_dir = control / "operations"
    if not ops_dir.is_dir():
        return None
    for opdir in ops_dir.iterdir():
        snap = opdir / "state_after_finish.json"
        if snap.exists():
            return json.loads(snap.read_bytes())
    return None
