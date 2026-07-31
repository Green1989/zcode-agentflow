"""P0-4 RED: the verification script that is CONTAINMENT-CHECKED is not the
script that is EXECUTED, and the ``program`` string itself is not checked for
metacharacters.

Two sub-defects the reviewer named:

  (a) ``_resolve_script_path(program, worktree_root)`` resolves + containment-
      checks a path relative to the WORKTREE, but ``_build_cmd_argv``/
      ``_build_bash_argv`` build argv from the ORIGINAL ``program`` string,
      which the interpreter resolves relative to the command ``cwd``. When
      ``cwd != worktree_root`` the validated path and the executed path can be
      DIFFERENT files. The fix: argv executes the validated absolute path.

  (b) ``_check_cmd_arg`` / ``_check_bash_arg`` are applied to every ``arg`` but
      NEVER to ``program``. A program string carrying cmd.exe metacharacters
      (``&|<>^%!()``) or bash metacharacters (``;$|&` ``) is therefore not
      rejected. The fix: validate ``program`` with the same rules.

These tests reproduce both and are expected to FAIL (RED) on the current code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from zcode_agentflow.adapters import verification
from zcode_agentflow.adapters.verification import (
    run_verification, VerificationError,
    _build_cmd_argv, _build_bash_argv,
)


# --------------------------------------------------------------------------- #
# (a) validated path != executed path when cwd != worktree_root
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="cwd-offset script-exec divergence is exercised via cmd.exe on Windows",
)
def test_executed_script_is_the_validated_absolute_path(tmp_path: Path):
    """RED: with cwd != worktree_root (both UNDER the worktree), a relative
    ``program`` is containment-checked relative to the WORKTREE root, but on the
    buggy code argv passes the bare ``program`` to cmd.exe, which resolves it
    relative to cwd. So a HONEYPOT script at the cwd-relative location can be
    executed instead of the validated worktree-root script.

    Layout (all under the worktree, so cwd containment is satisfied):
      worktree/probe.bat      <- SAFE (validated path)
      worktree/sub/probe.bat  <- HONEYPOT (cwd-relative location)
      cwd = worktree/sub

    The validated (worktree-root) script must run, never the honeypot.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    sub = worktree / "sub"
    sub.mkdir()

    # SAFE script lives at the worktree root (the validated relative path).
    (worktree / "probe.bat").write_text("@echo SAFE-RAN\n", encoding="utf-8")
    # HONEYPOT lives at the cwd-relative location.
    (sub / "probe.bat").write_text("@echo HONEYPOT-RAN\n", encoding="utf-8")

    result = run_verification(
        command={"kind": "cmd_script", "program": "probe.bat", "args": [],
                 "cwd": "sub", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=[],
        interpreter_allowlist=["cmd.exe", "cmd"],
    )
    assert result["exit_code"] == 0, result
    # The validated (worktree-root) script must run — never the cwd-relative honeypot.
    assert "SAFE-RAN" in result["stdout"], (
        f"executed the cwd-relative script instead of the validated worktree "
        f"script (P0-4 path divergence): stdout={result['stdout']!r}"
    )
    assert "HONEYPOT-RAN" not in result["stdout"]


def test_build_cmd_argv_uses_absolute_script_path(tmp_path: Path):
    """_build_cmd_argv must reference the validated absolute ``script`` path so
    the interpreter cannot re-resolve it against a different cwd."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "probe.bat").write_text("@echo hi\n", encoding="utf-8")
    script = verification._resolve_script_path("probe.bat", worktree)
    assert script.is_absolute()

    argv = _build_cmd_argv(script=script, args=["a"])
    # argv must reference the absolute validated path, not a bare relative name.
    assert str(script) in argv, (
        f"_build_cmd_argv should execute the validated absolute script path, "
        f"got argv={argv!r} (P0-4)"
    )
    assert "probe.bat" not in [a for a in argv if a == "probe.bat"], (
        f"argv must not carry the bare relative program; got {argv!r}"
    )


def test_build_bash_argv_uses_absolute_script_path(tmp_path: Path):
    """Same invariant for the bash argv builder: argv must resolve to the
    VALIDATED script from cwd (via a POSIX relpath), not the bare program."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "probe.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    script = verification._resolve_script_path("probe.sh", worktree)
    assert script.is_absolute()

    argv = _build_bash_argv(script=script, cwd=worktree, args=["a"])
    # argv references the validated script (here relpath == "probe.sh" since cwd
    # is the worktree root), never an arbitrary cwd-resolved file.
    assert "probe.sh" in argv, f"argv should name the validated script: {argv!r}"
    assert argv[0] == "bash"


# --------------------------------------------------------------------------- #
# (b) program string is not checked for metacharacters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("prog", ["a&b.bat", "a|b.bat", "a>b.bat", "a^b.bat",
                                  "a%b.bat", "a(b).bat", "a!b.bat"])
def test_cmd_script_program_metachars_rejected(tmp_path: Path, prog: str):
    """RED: a cmd_script ``program`` carrying cmd.exe metacharacters must be
    rejected (same rule as args). Today only args are checked, so a metachar-
    laden program reaches cmd.exe /c and can inject a second command.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "cmd_script", "program": prog, "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=[],
            interpreter_allowlist=["cmd.exe", "cmd"],
        )


@pytest.mark.parametrize("prog", ["a;b.sh", "a$b.sh", "a|b.sh", "a&b.sh",
                                  "a`b`.sh"])
def test_bash_script_program_metachars_rejected(tmp_path: Path, prog: str):
    """RED: a bash_script ``program`` carrying bash metacharacters must be
    rejected (same rule as args)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "bash_script", "program": prog, "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=[],
            interpreter_allowlist=["bash", "sh"],
        )


def test_cmd_script_program_control_chars_rejected(tmp_path: Path):
    """RED: a cmd_script program with CR/LF/NUL is rejected (control chars)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "cmd_script", "program": "a\rb.bat", "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=[],
            interpreter_allowlist=["cmd.exe", "cmd"],
        )
