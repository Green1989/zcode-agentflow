"""Lock primitives.

Two NON-reentrant, cross-process exclusive locks:

- **Registry Lock** (Task 1.3a): ``<user_data_root>/.registry.lock`` — guards run
  creation / active-run pointer / run_id allocation across MCP processes.
- **Per-Run Lock** (Task 3.1): ``<control_root>/.agentflow.lock`` — guards ONE
  Run's state/events/operations bookkeeping. Its ONLY callers are
  ``Store.begin_operation`` / ``finish_operation`` / ``fail_operation`` /
  ``mark_unknown`` (and ``Store.recover``); nothing else acquires it, and those
  methods each acquire it ONCE and never call into each other while it is held.
  That single-owner rule is the P0-3 deadlock guard (the v2 ``_guard`` deadlock
  came from re-acquiring this lock across an external CLI call).

Owner pattern (errata #5): ``registry_lock`` is the ONLY acquire point for the
registry lock. ``ActiveRunRegistry.locked()`` wraps it; the ``*_locked`` methods
on ``ActiveRunRegistry`` assume the caller already holds it and NEVER re-acquire
(this prevents the non-reentrant self-deadlock the errata warns about).
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Iterator

import portalocker

__all__ = [
    "RegistryLockError",
    "LockTimeout",
    "registry_lock",
    "run_lock",
    "LOCK_FILENAME",
    "RUN_LOCK_FILENAME",
]

LOCK_FILENAME = ".registry.lock"
RUN_LOCK_FILENAME = ".agentflow.lock"


class RegistryLockError(RuntimeError):
    """Raised when the registry lock cannot be acquired in time, or is held in
    a way that violates the single-owner / non-reentrant contract."""


class LockTimeout(RuntimeError):
    """Raised when the per-Run lock cannot be acquired within the timeout.

    ``LockTimeout`` is the contract type named in Task 3.1; it is distinct from
    ``RegistryLockError`` so callers (and tests) can tell which lock layer
    timed out. It is raised both on a genuine cross-process contention timeout
    and on a same-process re-entrant attempt (the lock is non-reentrant, so a
    nested acquire from the holder times out rather than deadlocking)."""


@contextlib.contextmanager
def registry_lock(user_data_root, timeout: float = 30) -> Iterator[None]:
    """Acquire the cross-process registry lock (LOCK_EX, non-reentrant).

    - lock file: ``<user_data_root>/.registry.lock``
    - polls every ~0.05s up to ``timeout`` seconds; raises RegistryLockError on
      timeout (non-reentrant: a second acquire from the same process while the
      first is held will time out rather than silently nest).
    - released unconditionally on exit (including exceptions).
    """
    user_data = Path(user_data_root)
    user_data.mkdir(parents=True, exist_ok=True)
    lock_path = user_data / LOCK_FILENAME

    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                # LOCK_EX | LOCK_NB: fail immediately if held elsewhere
                # (including a re-entrant attempt from the same process).
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                acquired = True
                break
            except portalocker.LockException:
                if time.monotonic() >= deadline:
                    raise RegistryLockError(
                        f"timed out after {timeout}s acquiring registry lock {lock_path}"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                portalocker.unlock(handle)
            finally:
                handle.close()
        else:
            handle.close()


# ---------------------------------------------------------------------------
# Per-Run lock (Task 3.1) — guards a single Run's state/events/operations.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def run_lock(control_root, timeout: float = 30) -> Iterator[None]:
    """Acquire the per-Run EXCLUSIVE lock (LOCK_EX, non-reentrant).

    - lock file: ``<control_root>/.agentflow.lock``
    - polls every ~0.05s up to ``timeout`` seconds; raises :class:`LockTimeout`
      on timeout. Non-reentrant: a re-entrant attempt from the same process
      while held will time out rather than nest (the design must never do that).
    - released unconditionally on exit (including exceptions).

    The ONLY callers are ``Store.begin_operation`` / ``finish_operation`` /
    ``fail_operation`` / ``mark_unknown`` / ``Store.recover``. Nothing else may
    acquire it (P0-3 deadlock guard).
    """
    control = Path(control_root)
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / RUN_LOCK_FILENAME

    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                # LOCK_EX | LOCK_NB: fail immediately if held elsewhere,
                # INCLUDING a re-entrant attempt from the same process. This is
                # the non-reentrancy guarantee the design relies on.
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                acquired = True
                break
            except portalocker.LockException:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"timed out after {timeout}s acquiring per-Run lock "
                        f"{lock_path}"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                portalocker.unlock(handle)
            finally:
                handle.close()
        else:
            handle.close()
