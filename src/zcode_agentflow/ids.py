"""Windows-safe id generation + branch/dirname normalization (Task 2.3; P0-1).

operation_id is used as a directory/file name under control_root
(``operations/<id>/``), so on Windows it must be a legal filename: no
``<>:"/\\|?*``, no ``..``, no path separators, no control chars — and NO COLON
anywhere (P0-1 / AGENTS.md G3, which calls out ``:`` explicitly). run_id feeds
a git branch name AND a directory, so it must additionally be ref-safe.

This module owns id generation/validation ONLY. The Store/Engine consume these
primitives in Phase 3/6; nothing here touches the active-run pointer, state, or
operations on disk.
"""

from __future__ import annotations

import re
import secrets
import subprocess
from datetime import datetime, timezone

__all__ = [
    "IdError",
    "new_run_id",
    "operation_id",
    "validate_task_id",
    "validate_run_id",
    "normalize_branch",
    "normalize_dirname",
]

# Windows-reserved filename characters (P0-1 / G3). operation_id / dirname must
# contain NONE of these — operation_id is a directory name on Windows.
_WIN_BAD_CHARS = set('<>:"/\\|?*')

# Safe charset for operation_id and tool components (AGENTS.md G3). No colon,
# no separators, no whitespace.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# run_id: 14-digit UTC timestamp + 6 lowercase hex chars (ref-safe, no separators).
_RUN_ID_RE = re.compile(r"^\d{14}-[0-9a-f]{6}$")

# task_id: T000 .. T999 with an optional -R<n> repair-round suffix.
_TASK_ID_RE = re.compile(r"^T[0-9]{3}(-R[0-9]+)?$")


class IdError(ValueError):
    """Raised when an id fails generation/validation (pattern, Windows-safety,
    ref-safety, or a traversal substring)."""


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Generate a fresh run id: ``<UTC YYYYMMDDhhmmss>-<6 hex>``.

    Ref-safe and Windows-filename-safe (only digits, '-', and lowercase hex).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(3)  # 6 lowercase hex chars
    return f"{ts}-{suffix}"


def validate_run_id(s: str) -> str:
    """Validate a run_id against ``^\\d{14}-[0-9a-f]{6}$``; raise IdError if not."""
    if not isinstance(s, str) or not _RUN_ID_RE.match(s):
        raise IdError(f"invalid run_id: {s!r} (expected <14digits>-<6hex>)")
    return s


# ---------------------------------------------------------------------------
# operation_id — Windows-filename-safe (P0-1)
# ---------------------------------------------------------------------------


def operation_id(*, run_id: str, tool: str, revision: int, attempt: int) -> str:
    """Build a Windows-filename-safe operation id.

    Format: ``<run_id>__<tool>__r<revision>__a<attempt>``.

    Guarantees (P0-1 / G3):
      - only chars in ``[A-Za-z0-9._-]`` (no colon, no separators, no spaces);
      - no ``..`` substring;
      - ``run_id`` validated; ``tool`` must be a safe single component (no
        ``__``, no separators) so it cannot corrupt the format;
      - ``revision`` and ``attempt`` are positive integers.
    """
    validate_run_id(run_id)
    if not isinstance(tool, str) or not _SAFE_ID_RE.match(tool):
        raise IdError(
            f"invalid tool name {tool!r}: must match {_SAFE_ID_RE.pattern}"
        )
    if "__" in tool:
        # the format's separator is '__'; a tool containing it would be ambiguous
        raise IdError(f"tool name {tool!r} must not contain '__'")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise IdError(f"revision must be a positive int, got {revision!r}")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise IdError(f"attempt must be a positive int, got {attempt!r}")

    op = f"{run_id}__{tool}__r{revision}__a{attempt}"

    # final defense-in-depth: re-assert every guarantee on the assembled string
    if not _SAFE_ID_RE.match(op):
        raise IdError(f"assembled operation_id failed charset: {op!r}")
    if ".." in op or any(c in op for c in _WIN_BAD_CHARS):
        raise IdError(f"assembled operation_id is not Windows-safe: {op!r}")
    return op


# ---------------------------------------------------------------------------
# task_id
# ---------------------------------------------------------------------------


def validate_task_id(s: str) -> str:
    """Validate a task id (``T000``..``T999`` with optional ``-R<n>`` repair
    suffix). Raise IdError on any deviation."""
    if not isinstance(s, str) or not _TASK_ID_RE.match(s):
        raise IdError(
            f"invalid task_id: {s!r} (expected T[0-9]{{3}} with optional -R<n>)"
        )
    return s


# ---------------------------------------------------------------------------
# branch name normalization (ref-safe)
# ---------------------------------------------------------------------------


def normalize_branch(*, prefix: str, run_id: str) -> str:
    """Build ``refs/heads/<prefix>/<run_id>`` and validate it via
    ``git check-ref-format --branch`` (real git semantics, not a regex guess).

    Raises IdError if ``run_id`` contains a separator, ``..``, or any other
    ref-illegal substring. ``prefix`` is validated against the safe charset
    first (it is a fixed config value, not user input, but we check anyway).
    """
    if not isinstance(prefix, str) or not _SAFE_ID_RE.match(prefix):
        raise IdError(f"invalid branch prefix: {prefix!r}")
    # Fast pre-checks before shelling out: reject separators / '..' / colon /
    # whitespace that git would also reject, so we give a clear error.
    _assert_no_traversal_or_separator(run_id, what="run_id (branch component)")

    ref = f"refs/heads/{prefix}/{run_id}"
    # Delegate to git for the authoritative ref-format check. git is a stated
    # prerequisite of the workflow; if it is somehow not on PATH we surface a
    # clear IdError rather than a bare FileNotFoundError.
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", ref],
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError) as exc:
        raise IdError(
            f"cannot validate branch {ref!r}: git not available ({exc})"
        ) from exc
    if result.returncode != 0:
        raise IdError(
            f"branch name {ref!r} is not a valid git ref "
            f"(git check-ref-format failed): {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return ref


# ---------------------------------------------------------------------------
# dirname normalization
# ---------------------------------------------------------------------------


def normalize_dirname(name: str) -> str:
    """Validate that ``name`` is a single safe path component usable as a
    directory name (e.g. a run_id under control_root).

    Rejects path separators (``/`` ``\\``), ``..``, NUL/control chars, spaces,
    and the Windows-reserved chars. A safe dirname cannot escape its parent.
    """
    if not isinstance(name, str) or not name:
        raise IdError(f"invalid dirname: {name!r}")
    _assert_no_traversal_or_separator(name, what="dirname")
    if not _SAFE_ID_RE.match(name):
        raise IdError(
            f"invalid dirname {name!r}: must match {_SAFE_ID_RE.pattern}"
        )
    return name


# ---------------------------------------------------------------------------
# shared internal guard
# ---------------------------------------------------------------------------


def _assert_no_traversal_or_separator(value: str, *, what: str) -> None:
    """Raise IdError if ``value`` contains a path separator, ``..``, NUL, a
    control char, whitespace, or any Windows-reserved char."""
    if not isinstance(value, str):
        raise IdError(f"{what} must be a string, got {type(value).__name__}")
    if "/" in value or "\\" in value:
        raise IdError(f"{what} contains a path separator: {value!r}")
    if ".." in value:
        raise IdError(f"{what} contains a '..' traversal substring: {value!r}")
    if "\x00" in value:
        raise IdError(f"{what} contains a NUL byte: {value!r}")
    if any(ord(ch) < 0x20 for ch in value):
        raise IdError(f"{what} contains a control char: {value!r}")
    if any(ch.isspace() for ch in value):
        raise IdError(f"{what} contains whitespace: {value!r}")
    if any(ch in _WIN_BAD_CHARS for ch in value):
        raise IdError(f"{what} contains a Windows-reserved char: {value!r}")
