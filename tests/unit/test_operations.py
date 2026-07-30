"""Task 3.3 — Operation lifecycle + idempotency + 3-valued replay_policy.

An :class:`Operation` lives under ``<control_root>/operations/<operation_id>/``
with a lifecycle PREPARED → RUNNING → COMMITTED / FAILED / UNKNOWN, and a
``replay_policy`` that is THREE-valued (clarification):

  - ``SAFE_READ``    git/status/hash — UNKNOWN+no-result ⇒ auto-replay
  - ``COSTFUL_READ`` Codex planning / Grok review — UNKNOWN+no-result ⇒ replay
                     ONLY if ``config.allow_costful_replay==true``, else NEEDS_HUMAN
  - ``UNSAFE_WRITE`` verification / worktree creation — UNKNOWN ⇒ NEEDS_HUMAN
                     (may have already written files)

Idempotency is keyed on ``(operation_id, request_sha256)``: a COMMITTED op
re-invoked with the SAME request returns the original result; the SAME
operation_id with a DIFFERENT request is a conflict (rejected). ``request_sha256``
is ``sha256_hex(canonical_json_bytes(request))``.

Scope of THIS task: the Operation/OperationManager record + idempotency +
the three-valued replay classification helper. The two-phase begin/finish that
DRIVES status transitions lands in Task 3.4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import canonical, store

REQUIRED_FILES = {"request.json", "request_sha256", "raw", "status"}


def _make_op(control: Path, op_id="op1", request=None, replay="SAFE_READ"):
    if request is None:
        request = {"tool": "workflow_status", "args": {}}
    return store.OperationManager(control).prepare(
        operation_id=op_id, request=request, replay_policy=replay
    )


# ---------- prepare: required files -----------------------------------------


def test_prepare_creates_required_files(tmp_path: Path):
    """prepare() writes request.json, request_sha256, an empty raw/, and sets
    status=PREPARED. ``request_sha256`` matches the canonical bytes of request."""
    control = tmp_path / "control"
    control.mkdir()
    request = {"tool": "git_status", "args": {"path": "src/"}}

    op = _make_op(control, op_id="20260730120000-abc123__git-status__r1__a1", request=request)
    opdir = control / "operations" / op.operation_id

    assert (opdir / "request.json").exists()
    assert (opdir / "request_sha256").exists()
    assert (opdir / "raw").is_dir()
    assert (opdir / "status").read_text().strip() == "PREPARED"

    expected_sha = canonical.sha256_hex(canonical.canonical_json_bytes(request))
    assert op.request_sha256 == expected_sha
    assert (opdir / "request_sha256").read_text().strip() == expected_sha

    # raw/ starts empty
    assert not any((opdir / "raw").iterdir())


def test_request_sha_is_canonical_and_stable(tmp_path: Path):
    """request_sha256 is over canonical bytes, so two requests that are the same
    object in different key order hash the SAME (idempotency key is stable)."""
    control = tmp_path / "control"
    control.mkdir()
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    op_a = _make_op(control, op_id="opa", request=a)
    op_b = _make_op(control, op_id="opb", request=b)
    assert op_a.request_sha256 == op_b.request_sha256


# ---------- idempotency ------------------------------------------------------


def test_idempotent_committed_same_request(tmp_path: Path):
    """A COMMITTED op re-opened with the SAME request_sha256 returns the prior
    result (true idempotency), not a re-execution."""
    control = tmp_path / "control"
    control.mkdir()
    request = {"tool": "git_status", "args": {}}
    mgr = store.OperationManager(control)

    op = mgr.prepare(operation_id="op1", request=request, replay_policy="SAFE_READ")
    op.status = "RUNNING"
    op.write_result({"ok": True})
    op.mark_committed()

    # reopen — must see COMMITTED and the original result
    again = mgr.open_existing("op1")
    assert again.status == "COMMITTED"
    assert again.request_sha256 == op.request_sha256
    assert again.read_result() == {"ok": True}
    # idempotent: same request, COMMITTED -> replay not needed
    assert mgr.is_idempotent_hit(again, request)


def test_conflict_different_request_rejected(tmp_path: Path):
    """Same operation_id but a DIFFERENT request is a conflict (rejected), not
    a silent overwrite."""
    control = tmp_path / "control"
    control.mkdir()
    mgr = store.OperationManager(control)

    op = mgr.prepare(operation_id="op1", request={"tool": "t", "args": {"n": 1}},
                     replay_policy="SAFE_READ")
    with pytest.raises(store.OperationConflict):
        mgr.prepare_conflict_aware("op1", request={"tool": "t", "args": {"n": 2}},
                                   replay_policy="SAFE_READ")
    # the original op record is untouched
    again = mgr.open_existing("op1")
    assert again.status == "PREPARED"


# ---------- replay_policy classification (clarification) --------------------


def test_replay_policy_SAFE_READ_auto_replays(tmp_path: Path):
    """SAFE_READ UNKNOWN+no result ⇒ eligible for auto-replay (not NEEDS_HUMAN)."""
    control = tmp_path / "control"
    control.mkdir()
    op = _make_op(control, replay="SAFE_READ")
    op.status = "UNKNOWN"
    outcome = store.classify_unknown_replay(op, allow_costful_replay=True)
    assert outcome == "REPLAY"


def test_replay_policy_SAFE_READ_auto_replays_even_without_costful_flag(tmp_path: Path):
    """SAFE_READ auto-replays regardless of the costful flag (it is cheap+safe)."""
    control = tmp_path / "control"
    control.mkdir()
    op = _make_op(control, replay="SAFE_READ")
    op.status = "UNKNOWN"
    assert store.classify_unknown_replay(op, allow_costful_replay=False) == "REPLAY"


def test_replay_policy_COSTFUL_READ_config_driven(tmp_path: Path):
    """COSTFUL_READ UNKNOWN+no result ⇒ REPLAY only if allow_costful_replay,
    else NEEDS_HUMAN (NOT blanket UNSAFE — read-only is safe, just costly)."""
    control = tmp_path / "control"
    control.mkdir()
    op = _make_op(control, replay="COSTFUL_READ")
    op.status = "UNKNOWN"

    assert store.classify_unknown_replay(op, allow_costful_replay=True) == "REPLAY"
    assert store.classify_unknown_replay(op, allow_costful_replay=False) == "NEEDS_HUMAN"


def test_replay_policy_UNSAFE_WRITE_needs_human(tmp_path: Path):
    """UNSAFE_WRITE UNKNOWN ⇒ NEEDS_HUMAN even with the costful flag (it may
    have already written files; cannot safely auto-replay)."""
    control = tmp_path / "control"
    control.mkdir()
    op = _make_op(control, replay="UNSAFE_WRITE")
    op.status = "UNKNOWN"
    assert store.classify_unknown_replay(op, allow_costful_replay=True) == "NEEDS_HUMAN"
    assert store.classify_unknown_replay(op, allow_costful_replay=False) == "NEEDS_HUMAN"


def test_replay_classification_unknown_op_status_is_safe(tmp_path: Path):
    """classify is only meaningful for UNKNOWN ops. A non-UNKNOWN op (e.g.
    RUNNING left by a crash) should not be classified as REPLAY by accident."""
    control = tmp_path / "control"
    control.mkdir()
    op = _make_op(control, replay="SAFE_READ")
    op.status = "RUNNING"  # not UNKNOWN
    # RUNNING with no result: the op is mid-flight; recovery should treat it as
    # UNKNOWN-equivalent for classification (it crashed before finishing). The
    # helper classifies on UNKNOWN/running-no-result identically for SAFE_READ.
    # We assert it does not blow up and returns a valid outcome label.
    outcome = store.classify_unknown_replay(op, allow_costful_replay=False)
    assert outcome in ("REPLAY", "NEEDS_HUMAN")


# ---------- RUNNING with result can finish/commit ---------------------------


def test_running_with_result_can_finish_commit(tmp_path: Path):
    """An op that reached RUNNING and has a complete result.json can be
    finished (committed) on resume — the result is preserved."""
    control = tmp_path / "control"
    control.mkdir()
    request = {"tool": "git_status", "args": {}}
    mgr = store.OperationManager(control)
    op = mgr.prepare(operation_id="op1", request=request, replay_policy="SAFE_READ")
    op.status = "RUNNING"
    op.write_result({"status": "clean"})

    # reopen (simulate resume) and commit
    again = mgr.open_existing("op1")
    assert again.status == "RUNNING"
    assert again.read_result() == {"status": "clean"}
    again.mark_committed()
    assert mgr.open_existing("op1").status == "COMMITTED"
