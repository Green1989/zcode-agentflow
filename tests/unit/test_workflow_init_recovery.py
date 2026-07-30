"""Task 3.7 — Transactional workflow_init + preflight + registry-locked run
creation (Prompt §11, P0-4, errata).

``workflow_init`` is itself a begin/finish transaction with preflight. A
half-created worktree (crash after ``git worktree add`` before finish) recovers
or NEEDS_HUMAN; it is NEVER auto-removed (no destructive compensation). Init
owns the run-creation critical section inside ONE
``with ActiveRunRegistry.locked(...)`` block — this task is the SOLE owner of
the initial pointer write (Task 6.8 delegates here and does NOT repeat it).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zcode_agentflow import active_run, gitops, preflight, store


# ---------- helpers ----------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """A real clean git repo with one commit."""
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


# ---------- preflight --------------------------------------------------------


def test_preflight_rejects_non_git_repo(tmp_path: Path):
    notrepo = tmp_path / "notrepo"
    notrepo.mkdir()
    control = tmp_path / "control"
    control.mkdir()
    wt = tmp_path / "wt"
    with pytest.raises(preflight.PreflightError):
        preflight.preflight(
            target_repo=notrepo, control_root=control,
            worktree_root=wt, branch="refs/heads/agentflow/r1",
        )


def test_preflight_rejects_in_progress_merge(tmp_path: Path):
    repo = _make_repo(tmp_path)
    control = tmp_path / "control"
    control.mkdir()
    wt = tmp_path / "wt"
    # simulate an in-progress merge: drop a MERGE_HEAD under .git
    gpath = gitops.git_path(repo)
    (gpath / "MERGE_HEAD").write_text("abc\n", encoding="utf-8")
    with pytest.raises(preflight.PreflightError):
        preflight.preflight(
            target_repo=repo, control_root=control,
            worktree_root=wt, branch="refs/heads/agentflow/r1",
        )


def test_preflight_rejects_nesting_roots(tmp_path: Path):
    """roots must be mutually non-containing (Spec §2.2); control inside the
    target repo is rejected (it would be committed/gitignored chaos)."""
    repo = _make_repo(tmp_path)
    nested_control = repo / "agentflow_control"  # INSIDE the repo
    nested_control.mkdir()
    wt = tmp_path / "wt"
    with pytest.raises(preflight.PreflightError):
        preflight.preflight(
            target_repo=repo, control_root=nested_control,
            worktree_root=wt, branch="refs/heads/agentflow/r1",
        )


def test_preflight_accepts_clean_repo(tmp_path: Path):
    repo = _make_repo(tmp_path)
    control = tmp_path / "control"
    control.mkdir()
    wt = tmp_path / "wt"
    # no exception
    preflight.preflight(
        target_repo=repo, control_root=control,
        worktree_root=wt, branch="refs/heads/agentflow/r1",
    )


def test_preflight_rejects_existing_branch_collision(tmp_path: Path):
    """NOTE: branch/worktree path collisions are NOT a preflight concern — they
    are owned by init_run's claim-or-raise logic (a same-run worktree is a
    crash-recovery CLAIM, not a collision). Preflight stays environment-only.
    This test documents that an existing same-named branch is surfaced by
    init_run (via gitops.create_worktree failing), not by preflight."""
    repo = _make_repo(tmp_path)
    control = tmp_path / "control"
    control.mkdir()
    wt = tmp_path / "wt"
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(["git", "branch", "agentflow/r1"], cwd=repo, check=True, env=env)
    # preflight does NOT reject on the branch (it would let init_run decide)
    preflight.preflight(
        target_repo=repo, control_root=control,
        worktree_root=wt, branch="refs/heads/agentflow/r1",
    )


# ---------- run creation + active pointer -----------------------------------


def _paths(tmp_path: Path, run_id="20260730120000-abc123"):
    base = tmp_path / "runs"
    control = base / run_id / "control"
    worktree = base / run_id / "worktree"
    return control, worktree


def test_init_writes_active_pointer(tmp_path: Path):
    """A successful init writes the active-run pointer under the Registry Lock
    (set_pointer_locked inside the ActiveRunRegistry.locked block)."""
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

    # the worktree was created outside the repo
    assert worktree.exists()
    assert gitops.is_git_repo(worktree)
    # base_commit recorded
    assert result["base_commit"]
    # state is INITIALIZING_WORKTREE, revision advanced
    st = store.State.load(control)
    assert st.state == "INITIALIZING_WORKTREE"
    assert st.base_commit == result["base_commit"]
    # the active-run pointer was written
    ptr = active_run.read_pointer(user_data)
    assert ptr is not None
    assert ptr["run_id"] == "20260730120000-abc123"
    assert ptr["control_root"] == str(control)
    assert ptr["target_repo_root"] == str(repo)


def test_existing_active_run_rejected(tmp_path: Path):
    """A non-terminal active pointer => a second init is rejected (P0-4). The
    user must abandon first."""
    repo = _make_repo(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control1, wt1 = _paths(tmp_path, run_id="20260730120000-aaa111")
    store.init_run(user_data_root=user_data, target_repo=repo,
                   control_root=control1, worktree_root=wt1,
                   run_id="20260730120000-aaa111",
                   plugin_root=tmp_path / "plugin", goal="g1")

    # a second init (different run_id) must be rejected while the first is active
    control2, wt2 = _paths(tmp_path, run_id="20260730120000-bbb222")
    with pytest.raises(store.InitRejected):
        store.init_run(user_data_root=user_data, target_repo=repo,
                       control_root=control2, worktree_root=wt2,
                       run_id="20260730120000-bbb222",
                       plugin_root=tmp_path / "plugin", goal="g2")

    # the pointer still names the first run
    assert active_run.read_pointer(user_data)["run_id"] == "20260730120000-aaa111"


# ---------- half-created worktree recovery (no destructive cleanup) ----------


def test_half_created_worktree_recovers_or_needs_human(tmp_path: Path, monkeypatch):
    """Crash after `git worktree add` but before finish: on resume, init claims
    the worktree if its branch ownership matches the run, else NEEDS_HUMAN.
    NEVER auto `git worktree remove` (no destructive compensation)."""
    repo = _make_repo(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control, worktree = _paths(tmp_path)

    # Drive init but inject a crash right after the worktree is created, before
    # finish_operation commits.
    monkeypatch.setenv("AGENTFLOW_CRASH_AFTER", "init_after_worktree_add")
    with pytest.raises(store.CrashInjected):
        store.init_run(user_data_root=user_data, target_repo=repo,
                       control_root=control, worktree_root=worktree,
                       run_id="20260730120000-abc123",
                       plugin_root=tmp_path / "plugin", goal="g")
    # the worktree was created on disk (half-created) ...
    assert worktree.exists()
    # ... but no pointer was written (finish did not commit)
    assert active_run.read_pointer(user_data) is None
    # the worktree must NOT have been auto-removed by the crash
    assert gitops.is_git_repo(worktree)

    # resume: init_run again with the SAME run_id should CLAIM the existing
    # worktree (branch ownership matches) and complete, not fail.
    monkeypatch.delenv("AGENTFLOW_CRASH_AFTER", raising=False)
    result = store.init_run(user_data_root=user_data, target_repo=repo,
                            control_root=control, worktree_root=worktree,
                            run_id="20260730120000-abc123",
                            plugin_root=tmp_path / "plugin", goal="g")
    assert result["base_commit"]
    assert active_run.read_pointer(user_data)["run_id"] == "20260730120000-abc123"


def test_branch_foreign_collision_needs_human(tmp_path: Path):
    """If the worktree path already exists and is owned by a DIFFERENT branch
    (foreign collision), init must surface NEEDS_HUMAN, not overwrite."""
    repo = _make_repo(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control, worktree = _paths(tmp_path)

    # pre-create a worktree at the target path on a FOREIGN branch
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(
        ["git", "worktree", "add", "-b", "foreign/branch", str(worktree)],
        cwd=repo, check=True, env=env,
    )

    with pytest.raises(store.InitNeedsHuman):
        store.init_run(user_data_root=user_data, target_repo=repo,
                       control_root=control, worktree_root=worktree,
                       run_id="20260730120000-abc123",
                       plugin_root=tmp_path / "plugin", goal="g")
    # the foreign worktree is untouched
    assert gitops.is_git_repo(worktree)
