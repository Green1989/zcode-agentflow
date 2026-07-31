"""Task 4.1 — redaction ordering (P1-4).

The load-bearing P1-4 rule: subprocess output is captured IN MEMORY, REDACTED,
and ONLY THEN written to ``operation/raw``. There must NEVER be a window where
unredacted secret bytes are persisted and then overwritten (a crash mid-write
would leak them). By default NO unredacted raw log exists anywhere; an
unredacted debug log requires an explicit opt-in flag and ships a security
warning.
"""

from __future__ import annotations

import sys
from pathlib import Path

from zcode_agentflow.adapters.base import run_subprocess


def _secret_printer() -> str:
    """Python source that prints a live-looking secret to stdout."""
    return (
        "import sys\n"
        "sys.stdout.write('Authorization: Bearer sk-live-LEAKME123\\n')\n"
        "sys.stdout.flush()\n"
    )


def test_raw_is_redacted_before_writing_to_disk(tmp_path: Path):
    """P1-4: the bytes on disk under ``operation/raw/stdout.txt`` contain the
    REDACTED marker and NEVER the secret value, even though the subprocess
    emitted the secret to its stdout."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    exit_code, out, _ = run_subprocess(
        [sys.executable, "-c", _secret_printer()],
        stdin_text=None, cwd=tmp_path, timeout=30, operation_dir=op_dir,
    )
    # the in-memory capture is ALSO already redacted (no raw-then-overwrite)
    assert "sk-live-LEAKME123" not in out
    assert "LEAKME123" not in out
    # and what landed on disk matches (redacted, never the secret)
    raw_on_disk = (op_dir / "raw" / "stdout.txt").read_text(encoding="utf-8")
    assert "LEAKME123" not in raw_on_disk
    from zcode_agentflow import redact
    assert redact.REDACTED_MARKER in raw_on_disk


def test_no_unredacted_raw_log_by_default(tmp_path: Path):
    """By default, NO unredacted raw log exists anywhere under operation_dir.

    We scan the whole operation_dir for any file whose content still contains
    the secret — none should."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    run_subprocess(
        [sys.executable, "-c", _secret_printer()],
        stdin_text=None, cwd=tmp_path, timeout=30, operation_dir=op_dir,
    )
    leaks = []
    for p in op_dir.rglob("*"):
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "LEAKME123" in content:
                leaks.append(str(p))
    assert leaks == [], f"unredacted secret found in: {leaks}"


def test_raw_debug_log_requires_opt_in_and_warns(tmp_path: Path):
    """An unredacted raw debug log may exist ONLY when ``raw_debug_log=True`` is
    explicitly passed, and enabling it emits a visible security warning.

    This is the single sanctioned path for unredacted content; it is off by
    default (see test_no_unredacted_raw_log_by_default)."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    import logging

    captured_warnings: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda rec: captured_warnings.append(rec.getMessage())  # type: ignore
    log = logging.getLogger("zcode_agentflow.adapters.base")
    prev_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    try:
        run_subprocess(
            [sys.executable, "-c", _secret_printer()],
            stdin_text=None, cwd=tmp_path, timeout=30, operation_dir=op_dir,
            raw_debug_log=True,
        )
    finally:
        log.removeHandler(handler)
        log.setLevel(prev_level)

    # the unredacted debug log now exists and DOES contain the secret
    debug_path = op_dir / "raw" / "stdout.debug.txt"
    assert debug_path.exists(), "raw_debug_log=True must create the debug file"
    assert "LEAKME123" in debug_path.read_text(encoding="utf-8")
    # and a security warning was emitted
    assert any(
        "security" in w.lower() or "unredacted" in w.lower() or "debug" in w.lower()
        for w in captured_warnings
    ), f"no security warning emitted; got {captured_warnings!r}"


def test_redaction_applied_to_stderr_too(tmp_path: Path):
    """stderr is redacted with the same ordering (capture -> redact -> write)."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    code = (
        "import sys\n"
        "sys.stderr.write('token=supersecret-TOKEN-XYZ\\n')\n"
    )
    _, _, err = run_subprocess(
        [sys.executable, "-c", code],
        stdin_text=None, cwd=tmp_path, timeout=30, operation_dir=op_dir,
    )
    assert "supersecret-TOKEN-XYZ" not in err
    raw_stderr = (op_dir / "raw" / "stderr.txt").read_text(encoding="utf-8")
    assert "supersecret-TOKEN-XYZ" not in raw_stderr
    from zcode_agentflow import redact
    assert redact.REDACTED_MARKER in raw_stderr
