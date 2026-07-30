"""Active-run pointer primitives + Registry Lock owner (Task 1.3a; P0-1, P0-4).

PRIMITIVES ONLY:
- read / set / clear / mark_terminal of the pointer file
  ``<user_data_root>/active-run.json``
- the Registry Lock (``<user_data_root>/.registry.lock``) is owned by
  ``ActiveRunRegistry.locked()`` — the sole acquire point. The ``*_locked``
  methods run INSIDE that context and NEVER re-acquire the lock (errata #5).

NOT here (later tasks): Store.recover (3.8), workflow_init pointer lifecycle
(3.7 owns the registry critical section + initial write; 6.8 does NOT repeat
it), corrupt-state recovery, READY_TO_MERGE marking lifecycle (6.8 — only the
low-level ``mark_terminal_locked`` primitive ships here).
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from zcode_agentflow import canonical
from zcode_agentflow.locks import registry_lock

__all__ = ["POINTER_FILENAME", "ActiveRunRegistry", "read_pointer", "scan_runs"]

POINTER_FILENAME = "active-run.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pointer_path(user_data_root) -> Path:
    return Path(user_data_root) / POINTER_FILENAME


class ActiveRunRegistry:
    """Active-run pointer operations guarded by the Registry Lock.

    Single-owner pattern (errata #5): the ONLY way to acquire the registry lock
    is ``ActiveRunRegistry.locked(...)``. Every ``*_locked`` method below assumes
    the caller already holds it and must NEVER re-acquire (the lock is
    non-reentrant; re-entering would self-deadlock).
    """

    @classmethod
    @contextlib.contextmanager
    def locked(cls, user_data_root, timeout: float = 30) -> Iterator["ActiveRunRegistry"]:
        """Acquire the registry lock for the duration of the block and yield an
        ActiveRunRegistry whose ``*_locked`` methods are safe to call."""
        with registry_lock(user_data_root, timeout=timeout):
            yield cls(user_data_root)

    def __init__(self, user_data_root):
        self.user_data_root = Path(user_data_root)

    # ---- read-only inside the lock ----------------------------------------

    def read_pointer_locked(self) -> dict | None:
        """Return the pointer dict, or None if no pointer file exists."""
        p = _pointer_path(self.user_data_root)
        if not p.exists():
            return None
        import json

        return json.loads(p.read_bytes())

    def assert_new_run_allowed_locked(self) -> None:
        """Raise if a NON-terminal pointer exists (a second workflow_init would
        collide). A terminal pointer (READY_TO_MERGE) does NOT block."""
        ptr = self.read_pointer_locked()
        if ptr is None:
            return
        if ptr.get("terminal"):
            return  # terminal runs may be replaced by a new run (6.8 lifecycle)
        raise RuntimeError(
            f"a non-terminal active run already exists: {ptr.get('run_id')}"
        )

    # ---- mutating inside the lock (assume caller holds it) ----------------

    def set_pointer_locked(self, *, run_id, control_root, target_repo_root) -> None:
        """Atomically write/replace the pointer file. ``control_root`` and
        ``target_repo_root`` are stored as normalized absolute paths."""
        payload = {
            "run_id": str(run_id),
            "control_root": str(Path(control_root)),
            "target_repo_root": str(Path(target_repo_root)),
            "updated_at": _utc_now_iso(),
        }
        canonical.write_canonical_json(_pointer_path(self.user_data_root), payload)

    def mark_terminal_locked(self, run_id) -> None:
        """Mark the current pointer terminal (READY_TO_MERGE). Errata #2: this
        does NOT clear the pointer — it is replaced/cleared only on a new run,
        explicit abandon, or explicit archive (6.8)."""
        ptr = self.read_pointer_locked()
        if ptr is None:
            raise RuntimeError("cannot mark terminal: no active-run pointer exists")
        ptr["run_id"] = str(run_id)
        ptr["state"] = "READY_TO_MERGE"
        ptr["terminal"] = True
        ptr["updated_at"] = _utc_now_iso()
        canonical.write_canonical_json(_pointer_path(self.user_data_root), ptr)

    def clear_pointer_locked(self) -> None:
        """Remove the pointer file (abandon / archive). Caller holds the lock."""
        p = _pointer_path(self.user_data_root)
        if p.exists():
            p.unlink()


# ---- module-level convenience helpers ----------------------------------------


def read_pointer(user_data_root) -> dict | None:
    """Convenience: open the registry's own short read context and return the
    pointer (or None). Used by workflow_status etc. that only need a read."""
    with ActiveRunRegistry.locked(user_data_root, timeout=30) as reg:
        return reg.read_pointer_locked()


def scan_runs(user_data_root) -> list:
    """Manual recovery helper: return the list of known runs (currently the
    active-run pointer if present). NOT wired into any auto-resume in this
    phase — full recovery is Task 3.8."""
    with ActiveRunRegistry.locked(user_data_root, timeout=30) as reg:
        ptr = reg.read_pointer_locked()
    return [ptr] if ptr else []
