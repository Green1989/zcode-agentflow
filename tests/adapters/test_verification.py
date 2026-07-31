"""Task 4.4 — Verification adapter (P1-1, P1-2; Prompt §16).

Runs a task's structured verification command safely:

  - ``kind=executable`` => ``[program, *args]`` with ``shell=False``; the program
    must be in the ``program_allowlist`` (structured config, NOT Markdown — P1-2);
  - ``cmd_script``/``bash_script`` => the script must be UNDER the worktree AND
    the interpreter must be in ``interpreter_allowlist`` (parallel P1-3: two
    distinct allowlists);
  - cwd is resolved and ``assert_within`` the worktree (no escape);
  - network policy is ADVISORY ONLY (never a fake ``deny`` that claims
    isolation the threat model says the MCP layer cannot provide);
  - secret redaction applies to the captured output (P1-4 reuse);
  - timeout kills the process tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zcode_agentflow.adapters.verification import (
    run_verification, VerificationError, NetworkPolicy,
)
from zcode_agentflow.adapters.base import AdapterTimeout


# ---------------------------------------------------------------------------
# executable kind — shell=False, allowlist, cwd, exit code
# ---------------------------------------------------------------------------


def test_executable_kind_shell_false(tmp_path: Path):
    """{kind:executable, program:python, args:[-V]} => exit 0, launched
    shell=False (no outer shell)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = run_verification(
        command={"kind": "executable", "program": sys.executable, "args": ["-V"],
                 "cwd": ".", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=[sys.executable, "python"],
        interpreter_allowlist=["cmd.exe", "bash"],
    )
    assert result["exit_code"] == 0
    assert "Python" in result["stdout"]


def test_program_not_in_config_allowlist_rejected(tmp_path: Path):
    """P1-2: the program must be in the structured config allowlist (NOT parsed
    from Markdown). A program not in the allowlist is rejected."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(VerificationError) as exc_info:
        run_verification(
            command={"kind": "executable", "program": "evil-exe", "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],  # evil-exe not allowed
            interpreter_allowlist=["cmd.exe", "bash"],
        )
    assert "allowlist" in str(exc_info.value).lower()


def test_cwd_escape_rejected(tmp_path: Path):
    """A cwd that escapes the worktree (via ..) is rejected."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "executable", "program": "python", "args": [],
                     "cwd": "../escape", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=["cmd.exe", "bash"],
        )


def test_cwd_absolute_outside_worktree_rejected(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "executable", "program": "python", "args": [],
                     "cwd": str(other), "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=["cmd.exe", "bash"],
        )


def test_nonzero_exit_recorded(tmp_path: Path):
    """A non-zero exit is recorded, not raised (the caller routes on exit_code)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    code = "import sys\n" "sys.exit(7)\n"
    result = run_verification(
        command={"kind": "executable", "program": sys.executable,
                 "args": ["-c", code], "cwd": ".", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=[sys.executable, "python"],
        interpreter_allowlist=["cmd.exe", "bash"],
    )
    assert result["exit_code"] == 7


def test_secret_redaction_applied(tmp_path: Path):
    """Captured output is redacted (P1-4 reuse via run_subprocess)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    code = (
        "import sys\n"
        "sys.stdout.write('Authorization: Bearer sk-live-LEAKME999\\n')\n"
    )
    result = run_verification(
        command={"kind": "executable", "program": sys.executable,
                 "args": ["-c", code], "cwd": ".", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=[sys.executable, "python"],
        interpreter_allowlist=["cmd.exe", "bash"],
    )
    assert "LEAKME999" not in result["stdout"]
    from zcode_agentflow import redact
    assert redact.REDACTED_MARKER in result["stdout"]


def test_timeout_kills_process_tree(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    code = "import time\n" "time.sleep(60)\n"
    import time as _t
    start = _t.perf_counter()
    with pytest.raises(AdapterTimeout):
        run_verification(
            command={"kind": "executable", "program": sys.executable,
                     "args": ["-c", code], "cwd": ".", "timeout_sec": 2},
            worktree_root=worktree,
            program_allowlist=[sys.executable, "python"],
            interpreter_allowlist=["cmd.exe", "bash"],
        )
    assert _t.perf_counter() - start < 15


# ---------------------------------------------------------------------------
# script kinds — path must be within worktree; interpreter allowlist
# ---------------------------------------------------------------------------


def test_script_path_must_be_within_worktree(tmp_path: Path):
    """A script path outside the worktree is rejected."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "evil.sh"
    outside.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "bash_script", "program": str(outside), "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=["bash"],
        )


def test_interpreter_not_in_allowlist_rejected(tmp_path: Path):
    """parallel P1-3: a script kind requires its interpreter in the
    interpreter_allowlist (distinct from program_allowlist)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    script = worktree / "run.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    with pytest.raises(VerificationError) as exc_info:
        run_verification(
            command={"kind": "bash_script", "program": "run.sh", "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=[],  # bash NOT allowed
        )
    assert "interpreter" in str(exc_info.value).lower() or "allowlist" in str(exc_info.value).lower()


def test_unknown_kind_rejected(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(VerificationError):
        run_verification(
            command={"kind": "shell_string", "program": "rm -rf /", "args": [],
                     "cwd": ".", "timeout_sec": 30},
            worktree_root=worktree,
            program_allowlist=["python"],
            interpreter_allowlist=["cmd.exe", "bash"],
        )


# ---------------------------------------------------------------------------
# network policy — advisory only, never a fake deny
# ---------------------------------------------------------------------------


def test_network_policy_is_advisory_or_absent(tmp_path: Path):
    """The network policy is ADVISORY ONLY (or absent). It never claims to
    enforce a deny the threat model says the MCP layer cannot provide."""
    # the enum exists and is advisory-only
    assert hasattr(NetworkPolicy, "ADVISORY_ONLY")
    # running a verification does NOT consult a network deny; it just runs
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = run_verification(
        command={"kind": "executable", "program": sys.executable, "args": ["-V"],
                 "cwd": ".", "timeout_sec": 30},
        worktree_root=worktree,
        program_allowlist=[sys.executable, "python"],
        interpreter_allowlist=["cmd.exe", "bash"],
        network_policy=NetworkPolicy.ADVISORY_ONLY,
    )
    assert result["exit_code"] == 0
