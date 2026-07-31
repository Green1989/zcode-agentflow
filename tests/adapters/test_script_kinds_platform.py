"""Task 4.4 — script-kind platform markers (P1-5).

``cmd_script`` is Windows-only (it runs under ``cmd.exe``); ``bash_script`` runs
wherever bash is available. Cross-platform tests are marked with
``@pytest.mark.skipif`` so platform-inappropriate tests are SKIPPED, not failed.

A capability marker (sourced from docs/capabilities.json) decides per platform
whether each script kind is required / optional / unsupported. Here we encode
the v1 decision: cmd_script required on win32; bash_script required where bash
exists.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from zcode_agentflow.adapters.verification import (
    run_verification, VerificationError,
)

BASH_AVAILABLE = shutil.which("bash") is not None
ON_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# cmd_script — Windows only (P1-5)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ON_WINDOWS, reason="cmd_script runs under cmd.exe (win32)")
def test_cmd_script_runs_on_windows(tmp_path: Path):
    """On Windows, a whitelist-conformant .bat/.cmd script under the worktree
    runs and its output is captured. The interpreter cmd.exe is allowlisted."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    bat = worktree / "run.cmd"
    # a benign .cmd that echoes a marker
    bat.write_text("@echo CMD_OUTPUT_OK\n", encoding="utf-8")
    result = run_verification(
        command={"kind": "cmd_script", "program": "run.cmd",
                 "args": [], "cwd": ".", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=["python"],
        interpreter_allowlist=["cmd.exe"],
    )
    assert result["exit_code"] == 0
    assert "CMD_OUTPUT_OK" in result["stdout"]


@pytest.mark.skipif(not ON_WINDOWS, reason="cmd_script runs under cmd.exe (win32)")
def test_cmd_script_metachar_arg_rejected_on_windows(tmp_path: Path):
    """On Windows, a cmd_script arg with an injection metacharacter is rejected
    before launch (the integration-level P1-1 check, not just the unit check)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    bat = worktree / "run.cmd"
    bat.write_text("@echo %1\n", encoding="utf-8")
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "cmd_script", "program": "run.cmd",
                     "args": ["T1 & del x"], "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=["cmd.exe"],
        )


@pytest.mark.skipif(not ON_WINDOWS, reason="cmd_script runs under cmd.exe (win32)")
def test_cmd_script_interpreter_not_allowlisted_rejected(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    bat = worktree / "run.cmd"
    bat.write_text("@echo hi\n", encoding="utf-8")
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "cmd_script", "program": "run.cmd",
                     "args": [], "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=[],  # cmd.exe NOT allowed
        )


# ---------------------------------------------------------------------------
# bash_script — where bash exists (P1-5)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash_script requires bash")
def test_bash_script_runs_where_bash_exists(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    sh = worktree / "run.sh"
    sh.write_text("#!/bin/bash\necho BASH_OUTPUT_OK\n", encoding="utf-8")
    result = run_verification(
        command={"kind": "bash_script", "program": "run.sh",
                 "args": [], "cwd": ".", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=["python"],
        interpreter_allowlist=["bash"],
    )
    assert result["exit_code"] == 0
    assert "BASH_OUTPUT_OK" in result["stdout"]


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash_script requires bash")
def test_bash_script_metachar_arg_rejected(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    sh = worktree / "run.sh"
    sh.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "bash_script", "program": "run.sh",
                     "args": ["a;rm -rf /"], "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=["bash"],
        )
