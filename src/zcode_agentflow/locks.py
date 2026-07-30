"""Lock primitives.

Task 1.3a adds the **Registry Lock** — a cross-process, NON-reentrant exclusive
lock guarding run creation / active-run pointer / run_id allocation. The
Per-Run lock (`run_lock`) is added in Task 3.1.

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

__all__ = ["RegistryLockError", "registry_lock", "LOCK_FILENAME"]

LOCK_FILENAME = ".registry.lock"


class RegistryLockError(RuntimeError):
    """Raised when the registry lock cannot be acquired in time, or is held in
    a way that violates the single-owner / non-reentrant contract."""


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
