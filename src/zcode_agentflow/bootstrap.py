"""Bootstrap / active-pointer recovery integration (Task 3.8; P0-1, P0-2).

``resume_active_run`` is the entry point a fresh MCP process calls at startup to
reconnect to the in-flight run (if any). It:

  1. reads the active-run pointer under the Registry Lock (read-only);
  2. if no pointer -> returns None (no active run);
  3. switches to the per-Run lock and calls :meth:`Store.recover` to rebuild
     state / classify any orphan operation;
  4. if the pointer's state is corrupt AND unrecoverable (no usable event
     snapshot) -> raises :class:`RecoveryNeedsHuman` rather than silently
     picking another run.

It NEVER scans for "newest" and NEVER auto-switches projects — it trusts the
pointer and recovers THAT run only.

``build_engine`` (Task 6.7) constructs an :class:`engine.Engine` for the active
run from the pointer + a run-context (plugin_root / user_data_root / config).
``workflow_init`` builds an Engine for the NEW run directly; the other 7 tools
build one for the active run via the pointer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from zcode_agentflow import active_run, store

__all__ = ["RecoveryNeedsHuman", "RunPaths", "resume_active_run", "build_engine",
           "RunContext", "load_run_context"]


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


# ===========================================================================
# Engine construction for the active run (Task 6.7)
# ===========================================================================


@dataclass(frozen=True)
class RunContext:
    """The deployment-wide context an Engine needs that is NOT in state.json:
    the plugin_root, the user_data_root (active-run pointer home), the resolved
    config, and the effective_config (from validate_capabilities).

    In production these come from the plugin's resolved config; in tests they
    are injected via the ``AGENTFLOW_RUN_CONTEXT`` env var (a JSON blob) so a
    spawned server can build an Engine without a real production config.
    """

    plugin_root: Path
    user_data_root: Path
    config: dict
    effective_config: dict
    adapters: dict | None = None  # tests inject fakes here


def load_run_context() -> RunContext | None:
    """Load the run context from the ``AGENTFLOW_RUN_CONTEXT`` env var (JSON).

    Returns None when the env var is unset (production resolves the context from
    the plugin config elsewhere; this loader is the test/subprocess path). The
    JSON shape: ``{"plugin_root", "user_data_root", "config", "effective_config",
    "adapters_module"?}``. ``adapters_module`` (optional) is a dotted module path
    exposing ``build_adapters() -> dict`` so a subprocess test can inject fakes.
    """
    raw = os.environ.get("AGENTFLOW_RUN_CONTEXT")
    if not raw:
        return None
    d = json.loads(raw)
    adapters = None
    amod = d.get("adapters_module")
    if amod:
        import importlib
        mod = importlib.import_module(amod)
        adapters = mod.build_adapters()
    return RunContext(
        plugin_root=Path(d["plugin_root"]),
        user_data_root=Path(d["user_data_root"]),
        config=d.get("config", {}),
        effective_config=d.get("effective_config", {}),
        adapters=adapters,
    )


def build_engine_for_active_run(ctx: RunContext):
    """Build an :class:`engine.Engine` for the ACTIVE run (pointer-based resume).

    Reads the active-run pointer, recovers the run's state, and constructs an
    Engine bound to the run's four roots. Used by the 7 non-init MCP tools.
    Raises if there is no active run (the caller must have called workflow_init
    first). """
    from zcode_agentflow import engine as _engine

    paths = resume_active_run(ctx.user_data_root)
    if paths is None:
        raise _engine.WorkflowError("no active run; call workflow_init first")
    state = store.State.load(paths.control_root)
    roots = {
        "plugin_root": str(ctx.plugin_root),
        "target_repo_root": str(paths.target_repo_root),
        "run_worktree_root": str(paths.run_worktree_root),
        "control_root": str(paths.control_root),
    }
    return _engine.Engine(
        store.Store(paths.control_root), roots, config=ctx.config,
        effective_config=ctx.effective_config, user_data_root=ctx.user_data_root,
        adapters=ctx.adapters or {},
    )


def build_engine_for_init(ctx: RunContext, *, target_repo: str, run_id: str | None,
                          control_root: str | None = None,
                          worktree_root: str | None = None):
    """Build an :class:`engine.Engine` for a NEW run (workflow_init).

    The run's control_root + worktree_root are derived from user_data_root when
    not supplied (a per-run subdir), matching init_run's expectations. When
    ``run_id`` is None a fresh id is generated HERE so the allocated roots and
    the id passed to ``Engine.workflow_init`` cannot diverge.
    """
    from zcode_agentflow import engine as _engine
    from zcode_agentflow import ids as _ids

    rid = run_id or _ids.new_run_id()
    base = Path(ctx.user_data_root) / "runs" / rid
    cr = Path(control_root) if control_root else base / "control"
    wr = Path(worktree_root) if worktree_root else base / "worktree"
    cr.mkdir(parents=True, exist_ok=True)
    # worktree must NOT pre-exist with content for a fresh init; create_worktree
    # creates it. Only ensure the parent exists.
    wr.parent.mkdir(parents=True, exist_ok=True)
    roots = {
        "plugin_root": str(ctx.plugin_root),
        "target_repo_root": str(target_repo),
        "run_worktree_root": str(wr),
        "control_root": str(cr),
    }
    eng = _engine.Engine(
        store.Store(cr), roots, config=ctx.config,
        effective_config=ctx.effective_config, user_data_root=ctx.user_data_root,
        adapters=ctx.adapters or {},
    )
    # stash the resolved run_id so the MCP tool can pass the SAME id to
    # workflow_init (avoids a second new_run_id() that would desync paths).
    eng._resolved_run_id = rid  # type: ignore[attr-defined]
    return eng

