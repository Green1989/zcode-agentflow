"""Bootstrap / active-pointer recovery integration (Task 3.8; P0-1, P0-2).

``resume_active_run`` is the entry point a fresh MCP process calls at startup to
reconnect to the in-flight run (if any). It:

  1. reads the active-run pointer under the Registry Lock (read-only);
  2. if no pointer -> returns None (no active run);
  3. switches to the Per-Run lock and calls :meth:`Store.recover` to rebuild
     state / classify any orphan operation;
  4. if the pointer's state is corrupt AND unrecoverable (no usable event
     snapshot) -> raises :class:`RecoveryNeedsHuman` rather than silently
     picking another run.

It NEVER scans for "newest" and NEVER auto-switches projects — it trusts the
pointer and recovers THAT run only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zcode_agentflow import active_run, store

__all__ = ["RecoveryNeedsHuman", "RunPaths", "resume_active_run"]


class RecoveryNeedsHuman(RuntimeError):
    """Raised by :func:`resume_active_run` when the pointer's run state is
    corrupt and cannot be rebuilt from events. Surfaced so a human decides —
    never silently picks another run (P0-1)."""


@dataclass(frozen=True)
class RunPaths:
    """The recovered run's identity + root paths (what the caller needs to
    reconnect: which run, where its control_root is, where its worktree is)."""

    run_id: str
    control_root: Path
    target_repo_root: Path
    run_worktree_root: Path


def resume_active_run(user_data_root, *, allow_costful_replay: bool = False) -> RunPaths | None:
    """Reconnect to the in-flight run (if any) and recover its state.

    Returns the recovered :class:`RunPaths`, or None if there is no active-run
    pointer. Raises :class:`RecoveryNeedsHuman` if the pointer's state is
    corrupt beyond what events can rebuild.

    Lock discipline: the pointer is read under the Registry Lock (read-only,
    released before recovery); recovery runs under the Per-Run lock (inside
    ``Store.recover``). The two locks are never held simultaneously here.
    """
    # 1. read the pointer under the registry lock (read-only context).
    with active_run.ActiveRunRegistry.locked(user_data_root) as registry:
        ptr = registry.read_pointer_locked()
    # pointer read done — registry lock released.

    if ptr is None:
        return None  # no active run

    run_id = ptr["run_id"]
    control_root = Path(ptr["control_root"])

    # 2. recover THAT run under the per-Run lock. Store.recover rebuilds state
    #    from the latest event snapshot if state.json is corrupt, classifies any
    #    orphan active operation, and rewrites state.json.
    s = store.Store(control_root)
    try:
        s.recover(allow_costful_replay=allow_costful_replay)
    except store.StateCorruptError as exc:
        # the pointer's run is corrupt beyond recovery -> NEEDS_HUMAN. We do NOT
        # fall back to scanning for another run (P0-1: never auto-switch).
        raise RecoveryNeedsHuman(
            f"active run {run_id!r} at {control_root} has corrupt state that "
            f"cannot be rebuilt from events: {exc}"
        ) from exc

    # 3. return the recovered run's identity + paths.
    state = store.State.load(control_root)
    return RunPaths(
        run_id=run_id,
        control_root=control_root,
        target_repo_root=Path(state.roots["target_repo_root"]),
        run_worktree_root=Path(state.roots["run_worktree_root"]),
    )
