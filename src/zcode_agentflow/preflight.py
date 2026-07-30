"""Preflight checks for ``workflow_init`` (Task 3.7; Prompt §11).

Before a run is created we verify the target environment is safe:

  - ``target_repo`` is a real git repo with a HEAD (something to branch from);
  - no in-progress merge/rebase/cherry-pick (``.git/MERGE_HEAD`` etc.) — init
    would branch off a conflicted state;
  - the run's branch does not already exist in the target repo (collision);
  - the run's worktree path does not collide with an existing worktree;
  - the four roots are mutually non-containing (Spec §2.2) — control/worktree
    MUST live outside the target repo;
  - the worktree path is usable on this OS (Windows path sanity).

Preflight is read-only: it inspects, never mutates. The init transaction
(``store.init_run``) owns the worktree creation + state transitions.
"""

from __future__ import annotations

from pathlib import Path

from zcode_agentflow import gitops, paths as paths_mod

__all__ = ["PreflightError", "preflight"]


class PreflightError(RuntimeError):
    """Raised when the target environment is not safe for run creation."""


# markers under .git that indicate an in-progress merge/rebase/cherry-pick.
_IN_PROGRESS_MARKERS = (
    "MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
    "BISECT_LOG", "sequencer",
)


def preflight(*, target_repo, control_root, worktree_root, branch) -> None:
    """Validate the environment before creating a run. Raise
    :class:`PreflightError` on any unsafe condition. Read-only.

    Preflight checks the ENVIRONMENT (git repo, HEAD, no in-progress merge,
    roots non-nesting). It deliberately does NOT check branch/worktree path
    collisions: those are owned by ``store.init_run``'s claim-or-raise logic,
    which knows whether an existing worktree is this run's own (a crash-recovery
    claim) or a foreign collision (NEEDS_HUMAN). Preflight is unaware of
    run-creation intent and must not reject a recoverable same-run resume.
    """
    target_repo = Path(target_repo)
    control_root = Path(control_root)
    worktree_root = Path(worktree_root)

    # 1. git available + target is a git repo
    if not gitops.is_git_repo(target_repo):
        raise PreflightError(f"target_repo is not a git repository: {target_repo}")

    # 2. HEAD exists (something to branch the worktree from)
    try:
        gitops.head(target_repo)
    except gitops.GitError as exc:
        raise PreflightError(
            f"target_repo has no HEAD (empty repo?): {exc}"
        ) from exc

    # 3. no in-progress merge/rebase/cherry-pick
    gpath = gitops.git_path(target_repo)
    for marker in _IN_PROGRESS_MARKERS:
        if (gpath / marker).exists():
            raise PreflightError(
                f"target_repo has an in-progress operation marker "
                f"({marker}); refusing to init a run mid-merge/rebase"
            )

    # 4. roots non-nesting: control_root and worktree_root must live OUTSIDE
    #    target_repo, and none of the three may contain another.
    if paths_mod.is_within(control_root, target_repo):
        raise PreflightError(
            f"control_root {control_root} is inside target_repo {target_repo}; "
            f"control must live OUTSIDE the repo (Spec §2.2)"
        )
    if paths_mod.is_within(worktree_root, target_repo):
        raise PreflightError(
            f"worktree_root {worktree_root} is inside target_repo {target_repo}; "
            f"the worktree must live OUTSIDE the repo (Spec §2.2)"
        )
    if paths_mod.is_within(control_root, worktree_root) or paths_mod.is_within(worktree_root, control_root):
        raise PreflightError(
            f"control_root and worktree_root must be mutually non-containing "
            f"(got {control_root} vs {worktree_root})"
        )
