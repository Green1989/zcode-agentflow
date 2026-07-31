"""Task 1.3a — active-run pointer primitives + Registry Lock (P0-1, P0-4).

PRIMITIVES ONLY — no Store.recover, no workflow_init lifecycle, no auto-resume
(those are Task 3.8 / 6.8). This task ships:
- read/set/clear/mark_terminal of the pointer file (<user_data_root>/active-run.json)
- a Registry Lock (<user_data_root>/.registry.lock) with ONE owner pattern:
  ActiveRunRegistry.locked() is the sole lock acquirer; the *_locked methods run
  INSIDE that context and NEVER re-acquire the lock (errata #5 — prevents
  non-reentrant self-deadlock).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from zcode_agentflow import active_run, locks

POINTER_FILENAME = "active-run.json"
LOCK_FILENAME = ".registry.lock"


# ---------- read / set / clear primitives ------------------------------------


def test_no_pointer_returns_none(tmp_path: Path):
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    assert active_run.read_pointer(user_data) is None
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        assert reg.read_pointer_locked() is None


def test_set_pointer_atomic_replace(tmp_path: Path):
    """set_pointer writes {run_id, control_root, target_repo_root, updated_at}
    via canonical.write_canonical_json + os.replace (atomic)."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control = tmp_path / "control"
    target = tmp_path / "target_repo"
    control.mkdir()
    target.mkdir()

    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.set_pointer_locked(
            run_id="20260730120000-abc123",
            control_root=control,
            target_repo_root=target,
        )

    pointer_path = user_data / POINTER_FILENAME
    assert pointer_path.exists()
    import json

    data = json.loads(pointer_path.read_bytes())
    assert data["run_id"] == "20260730120000-abc123"
    assert data["control_root"] == str(control)
    assert data["target_repo_root"] == str(target)
    assert "updated_at" in data and data["updated_at"]

    # readable back through the registry primitive
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        again = reg.read_pointer_locked()
    assert again is not None
    assert again["run_id"] == "20260730120000-abc123"


def test_clear_pointer_removes_file(tmp_path: Path):
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.set_pointer_locked(
            run_id="r1", control_root=tmp_path / "c", target_repo_root=tmp_path / "t"
        )
    assert (user_data / POINTER_FILENAME).exists()

    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.clear_pointer_locked()
    assert not (user_data / POINTER_FILENAME).exists()
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        assert reg.read_pointer_locked() is None


def test_mark_terminal_rejects_a_different_run_id(tmp_path: Path):
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.set_pointer_locked(
            run_id="run-a", control_root=tmp_path / "c", target_repo_root=tmp_path / "t"
        )

    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        with pytest.raises(RuntimeError, match="different run_id"):
            reg.mark_terminal_locked("run-b")

    pointer = active_run.read_pointer(user_data)
    assert pointer["run_id"] == "run-a"
    assert pointer.get("terminal") is not True


# ---------- Registry Lock serialization (P0-4) -------------------------------


def _child_setter(user_data: str, run_id: str, ready_event, done_event, errors):
    """Child process: wait for the signal, then try to set a pointer under the
    registry lock. Records whether it observed the OTHER run's pointer first."""
    try:
        # spin until the parent has taken+released once (coordination)
        ready_event.wait(timeout=30)
        udata = Path(user_data)
        with active_run.ActiveRunRegistry.locked(udata, timeout=30) as reg:
            existing = reg.read_pointer_locked()
            # record whether we saw the parent's pointer
            errors.put(("saw", existing["run_id"] if existing else None))
            reg.set_pointer_locked(
                run_id=run_id,
                control_root=udata.parent / "c",
                target_repo_root=udata.parent / "t",
            )
        done_event.set()
    except Exception as exc:  # noqa: BLE001
        errors.put(("err", repr(exc)))
        done_event.set()


def test_registry_lock_serializes_concurrent_setters(tmp_path: Path):
    """P0-4: two setters under <user_data_root>/.registry.lock serialize; the
    second observes the first's pointer (no double-create). Uses a real child
    process to prove cross-process exclusivity (not just same-thread)."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    (tmp_path / "c").mkdir()
    (tmp_path / "t").mkdir()

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    done = ctx.Event()
    q = ctx.Queue()

    # parent takes the lock first, sets its pointer, THEN signals the child
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.set_pointer_locked(
            run_id="PARENT_RUN",
            control_root=tmp_path / "c",
            target_repo_root=tmp_path / "t",
        )
        proc = ctx.Process(
            target=_child_setter,
            args=(str(user_data), "CHILD_RUN", ready, done, q),
        )
        proc.start()
        # hold the lock a moment so the child must block on it
        time.sleep(0.4)
        ready.set()  # child may now attempt to acquire (must wait until we release)

    # parent released; child acquires, observes PARENT_RUN, sets CHILD_RUN
    assert done.wait(timeout=30), "child did not complete in time"
    proc.join(timeout=10)

    observations = []
    while not q.empty():
        observations.append(q.get())

    # the child MUST have seen the parent's pointer (serialization worked)
    saw = [o for o in observations if o[0] == "saw"]
    errs = [o for o in observations if o[0] == "err"]
    assert not errs, f"child reported errors: {errs}"
    assert saw and saw[0][1] == "PARENT_RUN", (
        f"child did not observe parent's pointer under the lock: {observations}"
    )
    # final pointer is the child's (the last writer wins, serialized)
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        final = reg.read_pointer_locked()
    assert final["run_id"] == "CHILD_RUN"
    proc.join(timeout=5)


def test_registry_lock_released_on_exception(tmp_path: Path):
    """If an exception is raised inside ActiveRunRegistry.locked(), the registry
    lock is released — a subsequent acquire must succeed without timeout."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with active_run.ActiveRunRegistry.locked(user_data, timeout=5):
            raise Boom("simulate failure mid-critical-section")

    # lock must be free now — re-acquire with a short timeout and it must work
    with active_run.ActiveRunRegistry.locked(user_data, timeout=5):
        pass  # if this deadlocks/times out, the lock was leaked


def test_registry_lock_is_non_reentrant(tmp_path: Path):
    """Errata #5 guard: re-entering ActiveRunRegistry.locked() from within an
    already-held context must NOT silently nest (the lock is non-reentrant).
    The *_locked methods must therefore NEVER re-acquire. This documents the
    invariant by asserting a nested acquire does not both succeed as independent
    holders — either it raises/times out, or it's the same lock object. Either
    way the design's rule is: don't nest."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    # Direct registry_lock() re-entry: non-reentrant lock should fail/timeout,
    # proving the *_locked methods (which assume the lock is already held) must
    # not call back into a locker.
    nested_failed_or_blocked = False
    with locks.registry_lock(user_data, timeout=5):
        try:
            with locks.registry_lock(user_data, timeout=1):
                nested_failed_or_blocked = False  # would mean reentrant (a bug)
        except Exception:
            nested_failed_or_blocked = True  # expected: non-reentrant
    assert nested_failed_or_blocked, (
        "registry_lock appears reentrant — would allow the deadlock errata #5 warns against"
    )


# ---------- helper-only scan_runs --------------------------------------------


def test_scan_runs_is_a_helper_only(tmp_path: Path):
    """scan_runs() lists runs but is NOT wired into any auto-resume here."""
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    # nothing yet
    assert active_run.scan_runs(user_data) == []

    # create a couple of pointer-shaped artifacts to confirm it lists them
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.set_pointer_locked(
            run_id="r1", control_root=tmp_path / "c1", target_repo_root=tmp_path / "t1"
        )
    runs = active_run.scan_runs(user_data)
    assert isinstance(runs, list)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "r1"
    # IMPORTANT: there must be NO resume/init logic introduced here —
    # scan_runs returns data only; nothing auto-resumes from it in this phase.
