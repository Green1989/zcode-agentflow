"""Task 2.3 — Windows-safe operation_id + branch/dirname normalization (P0-1).

operation_id is used as a directory/file name under control_root (operations/
<id>/), so on Windows it must be a legal filename: no ``<>:"/\\|?*``, no ``..``,
no path separators, no control chars (P0-1, AGENTS.md G3). run_id feeds a git
branch name and a directory, so it must also be ref-safe.

This task ships id generation + validation + branch/dirname normalization. It
does NOT touch the Store/Engine (those consume these primitives in Phase 3/6).
"""

from __future__ import annotations

import re
import subprocess

import pytest

from zcode_agentflow import ids

RUN_ID_PATTERN = re.compile(r"^\d{14}-[0-9a-f]{6}$")
OP_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
TASK_ID_PATTERN = re.compile(r"^T[0-9]{3}(-R[0-9]+)?$")

# Windows-reserved filename chars (P0-1, AGENTS.md G3)
WIN_BAD = '<>:"/\\|?*'


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------


def test_run_id_pattern():
    rid = ids.new_run_id()
    assert RUN_ID_PATTERN.match(rid), f"run_id {rid!r} does not match the pattern"
    # ref-safe: no separators / dots runs / windows-bad chars
    assert ".." not in rid
    assert not any(c in rid for c in WIN_BAD)


def test_new_run_id_is_unique_and_time_ordered():
    """Two generations differ; the 14-digit timestamp prefix is a plausible
    UTC timestamp YYYYMMDDhhmmss."""
    a = ids.new_run_id()
    b = ids.new_run_id()
    assert a != b
    ts = a[:14]
    assert ts.isdigit() and len(ts) == 14
    assert 1 <= int(ts[4:6]) <= 12  # month
    assert 1 <= int(ts[6:8]) <= 31  # day


# ---------------------------------------------------------------------------
# operation_id — the P0-1 Windows-filename-safe guarantee
# ---------------------------------------------------------------------------


def test_operation_id_is_windows_filename_safe():
    """P0-1: operation_id is a legal Windows filename. No reserved chars, no
    '..', matches the safe-char regex. It is used as operations/<id>/."""
    op = ids.operation_id(
        run_id="20260730120000-abc123", tool="planner_draft_spec",
        revision=3, attempt=2,
    )
    assert not any(c in op for c in WIN_BAD), f"op_id contains Windows-bad char: {op!r}"
    assert ".." not in op
    assert OP_ID_PATTERN.match(op), f"op_id {op!r} not in safe charset"
    # specifically NO COLON anywhere (P0-1 / G3)
    assert ":" not in op


def test_operation_id_format():
    """operation_id encodes run/tool/revision/attempt with '__' separators:
    <run_id>__<tool>__r<rev>__a<attempt>. Different attempt => different id."""
    op = ids.operation_id(
        run_id="20260730120000-abc123", tool="workflow_status",
        revision=7, attempt=1,
    )
    assert op == "20260730120000-abc123__workflow_status__r7__a1"
    # deterministic: same inputs => same id
    assert op == ids.operation_id(
        run_id="20260730120000-abc123", tool="workflow_status",
        revision=7, attempt=1,
    )
    # different attempt => different id
    op2 = ids.operation_id(
        run_id="20260730120000-abc123", tool="workflow_status",
        revision=7, attempt=2,
    )
    assert op != op2
    assert op2.endswith("__a2")


def test_operation_id_different_revision_or_tool_differ():
    base = ids.operation_id(run_id="20260730120000-abc123",
                            tool="workflow_status", revision=1, attempt=1)
    other_rev = ids.operation_id(run_id="20260730120000-abc123",
                                 tool="workflow_status", revision=2, attempt=1)
    other_tool = ids.operation_id(run_id="20260730120000-abc123",
                                  tool="reviewer_review_task", revision=1, attempt=1)
    assert base != other_rev
    assert base != other_tool


def test_operation_id_rejects_tool_with_separator():
    """A tool name containing a '__' or path char would corrupt the format /
    break filename-safety; it must be rejected."""
    with pytest.raises((ValueError, ids.IdError)):
        ids.operation_id(run_id="20260730120000-abc123",
                         tool="bad__tool", revision=1, attempt=1)
    with pytest.raises((ValueError, ids.IdError)):
        ids.operation_id(run_id="20260730120000-abc123",
                         tool="bad/tool", revision=1, attempt=1)


def test_operation_id_rejects_nonpositive_revision_or_attempt():
    with pytest.raises((ValueError, ids.IdError)):
        ids.operation_id(run_id="20260730120000-abc123",
                         tool="workflow_status", revision=0, attempt=1)
    with pytest.raises((ValueError, ids.IdError)):
        ids.operation_id(run_id="20260730120000-abc123",
                         tool="workflow_status", revision=1, attempt=0)


# ---------------------------------------------------------------------------
# task_id
# ---------------------------------------------------------------------------


def test_task_id_pattern_strict():
    assert ids.validate_task_id("T001") == "T001"
    assert ids.validate_task_id("T000") == "T000"
    assert ids.validate_task_id("T001-R3") == "T001-R3"  # repair round
    # rejects
    for bad in ["T01", "T0001", "001", "TABC", "T00a", "t001", "T001-", "T001-R",
                "T001-3", "", "T001_R3"]:
        with pytest.raises((ValueError, ids.IdError)):
            ids.validate_task_id(bad)


def test_task_id_allows_repair_round():
    """A repair round suffix -R<n> is valid (v1.1+); no suffix is also valid."""
    ids.validate_task_id("T001")
    ids.validate_task_id("T001-R1")
    ids.validate_task_id("T001-R12")


# ---------------------------------------------------------------------------
# branch name normalization (ref-safe; uses git check-ref-format)
# ---------------------------------------------------------------------------


def test_branch_name_rejects_separators_and_dotdot():
    """Branch components reject path separators, '..', and other ref-illegal
    substrings (git ref-format). normalize_branch validates run_id-derived
    components and raises on illegal input."""
    for bad in ["a/b", "a\\b", "a..b", "a:b", "a b", "a~b", "a^b"]:
        with pytest.raises((ValueError, ids.IdError)):
            ids.normalize_branch(prefix="agentflow", run_id=bad)


def test_branch_name_valid_run_id_passes_git_check_ref_format(tmp_path):
    """A valid run_id produces refs/heads/agentflow/<run_id> that passes
    `git check-ref-format --branch`."""
    rid = "20260730120000-abc123"
    ref = ids.normalize_branch(prefix="agentflow", run_id=rid)
    assert ref == "refs/heads/agentflow/20260730120000-abc123"
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", "refs/heads/agentflow/" + rid],
        capture_output=True, env=env,
    )
    assert result.returncode == 0, (
        f"git check-ref-format rejected the branch: {result.stderr!r}"
    )


def test_branch_rejects_dotdot_anywhere():
    """git rejects '..' anywhere in a ref name (not just as a component)."""
    with pytest.raises((ValueError, ids.IdError)):
        ids.normalize_branch(prefix="agentflow", run_id="20260730120000..abc123")


# ---------------------------------------------------------------------------
# dirname normalization
# ---------------------------------------------------------------------------


def test_dirname_rejects_separators():
    """A dirname must be a single path component: no separators, no '..', no
    NUL/control chars, no spaces (avoids path traversal and Windows issues)."""
    for bad in ["a/b", "a\\b", "..", "a..b", "a:b", "a b", "a\nb", "a\x00b"]:
        with pytest.raises((ValueError, ids.IdError)):
            ids.normalize_dirname(bad)


def test_dirname_accepts_valid_run_id():
    rid = "20260730120000-abc123"
    assert ids.normalize_dirname(rid) == rid


# ---------------------------------------------------------------------------
# traversal-substring rejection (cross-cutting)
# ---------------------------------------------------------------------------


def test_id_rejects_traversal_substrings():
    """No id-bearing function accepts a '..' traversal substring (defense in
    depth against control_root escape via crafted ids)."""
    for fn in [
        lambda r: ids.normalize_branch(prefix="agentflow", run_id=r),
        ids.normalize_dirname,
    ]:
        for bad in ["..", "../x", "x/..", "a..b", "..."]:
            with pytest.raises((ValueError, ids.IdError)):
                fn(bad)
