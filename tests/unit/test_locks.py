"""Task 3.1 — Per-Run non-reentrant file lock (Review P0-7; P0-3 guard).

The Per-Run Lock guards a single Run's bookkeeping under
``<control_root>/.agentflow.lock``. It is:

- cross-process EXCLUSIVE (a second process waits until the first releases);
- NON-reentrant (re-acquiring from the same process while held must time out /
  raise — documents that the design must never nest it);
- released unconditionally on exception;
- acquired ONLY by ``Store.begin_operation`` / ``Store.finish_operation`` etc.
  (nothing else in the codebase takes it — that's the P0-3 deadlock guard).

The cross-process exclusivity test uses a REAL child process (spawn), not a
same-thread mock, mirroring the registry-lock test in Task 1.3a.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from zcode_agentflow import locks

RUN_LOCK_FILENAME = ".agentflow.lock"


# ---------- cross-process exclusivity ----------------------------------------


def _child_acquirer(control_root: str, ready, parent_holding, release_request, done, observed):
    """Child process: prove the lock is cross-process exclusive.

    1. wait for the start signal;
    2. attempt the per-Run lock with a SHORT timeout while the parent holds it
       -> expect LockTimeout (the child CANNOT acquire while held); record
       ``blocked``;
    3. tell the parent it may now release;
    4. wait for the parent's release confirmation;
    5. acquire again with a long timeout -> must succeed; record ``acquired``.
    """
    try:
        ready.wait(timeout=30)
        # Step 2: parent still holds the lock -> this must time out.
        try:
            with locks.run_lock(Path(control_root), timeout=1):
                observed.put(("not_blocked", None))  # BAD: acquired while held
        except locks.LockTimeout:
            observed.put(("blocked", None))  # GOOD: cross-process exclusive
        # Step 3 & 4: ask parent to release, wait for confirmation.
        release_request.set()
        parent_holding.wait(timeout=30)  # cleared by parent once released
        # Step 5: parent released -> this must now succeed.
        with locks.run_lock(Path(control_root), timeout=30):
            observed.put(("acquired", None))
        done.set()
    except Exception as exc:  # noqa: BLE001
        observed.put(("err", repr(exc)))
        done.set()


def test_lock_is_exclusive_cross_process(tmp_path: Path):
    """A second process CANNOT acquire the per-Run lock while the holder keeps
    it; it can only acquire after release.

    Uses a real spawned child to prove cross-process exclusivity (not a
    same-thread mock). The child reports ``blocked`` (its short-timeout acquire
    failed) while the parent holds the lock, then ``acquired`` once released.
    """
    control = tmp_path / "control_root"
    control.mkdir()

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release_request = ctx.Event()
    parent_holding = ctx.Event()  # SET means parent still holds; CLEARED on release
    done = ctx.Event()
    q = ctx.Queue()

    proc = ctx.Process(
        target=_child_acquirer,
        args=(str(control), ready, parent_holding, release_request, done, q),
    )
    with locks.run_lock(control, timeout=5):
        parent_holding.set()  # flag: parent holds the lock right now
        proc.start()
        time.sleep(0.3)  # let the child come up
        ready.set()  # child attempts the short-timeout acquire (must block)
        # wait until the child confirms it was blocked and asks us to release
        assert release_request.wait(timeout=30), (
            "child never reported being blocked — lock may not be exclusive"
        )
        # still holding here; release happens when we exit the `with` below
        parent_holding.clear()  # tell the child we've released

    assert done.wait(timeout=30), "child did not complete in time"
    proc.join(timeout=10)

    observations = []
    while not q.empty():
        observations.append(q.get())
    kinds = {o[0] for o in observations}
    errs = [o for o in observations if o[0] == "err"]
    assert not errs, f"child reported errors: {errs}"
    assert "blocked" in kinds, (
        f"child did not block while parent held the lock: {observations}"
    )
    assert "not_blocked" not in kinds, (
        f"child acquired the lock while the parent held it (NOT exclusive): "
        f"{observations}"
    )
    assert "acquired" in kinds, (
        f"child never acquired the lock after release: {observations}"
    )
    proc.join(timeout=5)


def test_lock_released_on_exception(tmp_path: Path):
    """An exception raised inside run_lock() releases the lock — a subsequent
    acquire with a short timeout succeeds (no leaked lock)."""
    control = tmp_path / "control_root"
    control.mkdir()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with locks.run_lock(control, timeout=5):
            raise Boom("simulate failure mid-critical-section")

    # lock must be free now
    with locks.run_lock(control, timeout=5):
        pass


def test_lock_path_under_control_root(tmp_path: Path):
    """The per-Run lock file lives at <control_root>/.agentflow.lock."""
    control = tmp_path / "control_root"
    control.mkdir()

    lock_path = control / RUN_LOCK_FILENAME
    assert not lock_path.exists()  # not created until acquired
    with locks.run_lock(control, timeout=5):
        assert lock_path.exists(), "lock file should exist while held"
    # portalocker keeps the lock file on disk after release (harmless); the
    # contract is the PATH, not presence-after-release.


def test_lock_not_reentrant(tmp_path: Path):
    """P0-3 guard: re-acquiring the same per-Run lock from the same process
    while it is held must NOT nest — it times out / raises. This documents the
    non-reentrancy the design relies on: ``begin_operation`` and
    ``finish_operation`` each acquire ONCE and never call each other while the
    lock is held (the v2 ``_guard`` deadlock came from violating this)."""
    control = tmp_path / "control_root"
    control.mkdir()

    nested_blocked = False
    with locks.run_lock(control, timeout=5):
        try:
            with locks.run_lock(control, timeout=1):
                nested_blocked = False  # would mean reentrant (a bug)
        except Exception:
            nested_blocked = True  # expected: non-reentrant
    assert nested_blocked, (
        "run_lock appears reentrant — would allow the P0-3 nested-lock deadlock"
    )
