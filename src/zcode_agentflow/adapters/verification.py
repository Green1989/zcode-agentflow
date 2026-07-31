"""Verification adapter — structured, contained, Windows-safe (Task 4.4).

Runs a task's verification command. The command is ALWAYS structured
(``{kind, program, args, cwd, timeout_sec}``) — NEVER an arbitrary shell string.

Three execution kinds:
  - ``executable``    => ``[program, *args]`` launched directly (``shell=False``).
                         The ``program`` must be in ``program_allowlist``.
  - ``cmd_script``    => a ``.bat``/``.cmd`` script UNDER the worktree, run via
                         ``cmd.exe /d /s /c <program> <args>`` (token list). The
                         interpreter ``cmd.exe`` must be in
                         ``interpreter_allowlist``. EACH arg must pass the
                         metacharacter + whitelist check (P1-1) — cmd.exe
                         re-parses its command line, so the defense is REJECTION,
                         not quoting.
  - ``bash_script``   => a ``.sh`` script UNDER the worktree, run via
                         ``bash <program> <args>``. ``bash`` must be in
                         ``interpreter_allowlist``; shell metacharacters in args
                         are rejected.

Two DISTINCT allowlists (parallel P1-3):
  - ``program_allowlist``     governs ``kind=executable`` (the program itself).
  - ``interpreter_allowlist`` governs ``kind=cmd_script``/``bash_script`` (the
                              interpreter that runs the script).

cwd is resolved under ``worktree_root`` and ``assert_within``-checked (no escape).

Network policy is ADVISORY ONLY (NetworkPolicy.ADVISORY_ONLY) — the threat model
states the MCP layer provides process-integrity, NOT OS-level network isolation.
We never claim a ``deny`` that pretends to enforce isolation we cannot provide.

Output is captured IN MEMORY, REDACTED, then written to ``operation_dir/raw``
(P1-4 reuse via run_subprocess). Timeout kills the process tree.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path

from zcode_agentflow import paths
from zcode_agentflow.adapters.base import run_subprocess, AdapterTimeout

__all__ = [
    "NetworkPolicy",
    "VerificationError",
    "run_verification",
    # P1-1 helpers (unit-tested directly)
    "_CMD_META", "_CMD_ARG_OK", "_BASH_META",
    "_check_cmd_arg", "_check_bash_arg",
    "_build_cmd_argv", "_build_bash_argv",
]


class NetworkPolicy(Enum):
    """The honest network policy. v1 is ADVISORY_ONLY — we do not enforce a
    deny (the threat model says the MCP layer cannot provide OS-level network
    isolation). Config may omit this; absence is treated as advisory-only."""
    ADVISORY_ONLY = "advisory_only"


class VerificationError(ValueError):
    """Raised when a verification command is malformed, its program/interpreter
    is not allowlisted, its cwd or script path escapes the worktree, or a
    cmd/bash arg carries a forbidden metacharacter."""


# ---------------------------------------------------------------------------
# P1-1: cmd.exe injection defense (rejected metacharacters + arg whitelist)
# ---------------------------------------------------------------------------

# cmd.exe metacharacters that change command parsing. Rejecting ANY of these in
# an arg (plus CR/LF/NUL) means there is no path for a second command to run.
# (parallel P0-5 full set.)
_CMD_META = set("&|<>^%!()")

# the whitelist an arg must additionally pass (spaces off by default — a space
# policy would require a tested quoting helper, which v1 does not provide).
_CMD_ARG_OK = r"^[A-Za-z0-9._:/\\=-]+$"

# bash shell metacharacters rejected in bash_script args.
_BASH_META = set(";$|&`")


def _has_ctrl(arg: str) -> bool:
    """True if ``arg`` contains CR, LF, or NUL (control chars that break out of
    a single command)."""
    return any(c in arg for c in ("\r", "\n", "\x00"))


def _check_cmd_arg(arg: str) -> None:
    """P1-1: reject a cmd_script arg that carries a cmd.exe metacharacter, a
    control char (CR/LF/NUL), or fails the whitelist regex. This is the primary
    defense since ``cmd.exe /c`` re-parses its command line."""
    if not isinstance(arg, str) or arg == "":
        raise VerificationError("cmd_script args must be non-empty strings")
    if _has_ctrl(arg):
        raise VerificationError(
            f"cmd_script arg contains a control char (CR/LF/NUL): {arg!r}"
        )
    if any(ch in _CMD_META for ch in arg):
        bad = sorted(set(arg) & _CMD_META)
        raise VerificationError(
            f"cmd_script arg contains cmd.exe metacharacter(s) {bad}: {arg!r}"
        )
    if not re.match(_CMD_ARG_OK, arg):
        raise VerificationError(
            f"cmd_script arg fails the whitelist {_CMD_ARG_OK!r}: {arg!r} "
            f"(spaces and most punctuation are disallowed by default)"
        )


def _check_bash_arg(arg: str) -> None:
    """Reject a bash_script arg carrying a shell metacharacter or control char."""
    if not isinstance(arg, str) or arg == "":
        raise VerificationError("bash_script args must be non-empty strings")
    if _has_ctrl(arg):
        raise VerificationError(
            f"bash_script arg contains a control char (CR/LF/NUL): {arg!r}"
        )
    if any(ch in _BASH_META for ch in arg):
        bad = sorted(set(arg) & _BASH_META)
        raise VerificationError(
            f"bash_script arg contains shell metacharacter(s) {bad}: {arg!r}"
        )


def _check_cmd_program(program: str) -> None:
    """P0-4: the ``program`` (script path) for a cmd_script must pass the SAME
    metacharacter + control-char rejection as its args. The arg whitelist is NOT
    applied here because a script path legitimately contains path separators,
    spaces, and drive letters; the injection defense is the metacharacter
    rejection (a program carrying ``&|<>^%!()`` or CR/LF/NUL can break out of
    the ``cmd.exe /c <program>`` token)."""
    if not isinstance(program, str) or program == "":
        raise VerificationError("cmd_script program must be a non-empty string")
    if _has_ctrl(program):
        raise VerificationError(
            f"cmd_script program contains a control char (CR/LF/NUL): {program!r}"
        )
    if any(ch in _CMD_META for ch in program):
        bad = sorted(set(program) & _CMD_META)
        raise VerificationError(
            f"cmd_script program contains cmd.exe metacharacter(s) {bad}: "
            f"{program!r}"
        )


def _check_bash_program(program: str) -> None:
    """P0-4: the ``program`` (script path) for a bash_script must pass the SAME
    shell-metacharacter + control-char rejection as its args."""
    if not isinstance(program, str) or program == "":
        raise VerificationError("bash_script program must be a non-empty string")
    if _has_ctrl(program):
        raise VerificationError(
            f"bash_script program contains a control char (CR/LF/NUL): {program!r}"
        )
    if any(ch in _BASH_META for ch in program):
        bad = sorted(set(program) & _BASH_META)
        raise VerificationError(
            f"bash_script program contains shell metacharacter(s) {bad}: "
            f"{program!r}"
        )


def _build_cmd_argv(*, script: Path, args: list[str]) -> list[str]:
    """Build the cmd.exe token list. NEVER concatenate program+args into a
    string; pass a list to ``subprocess.run(shell=False)``. ``/d`` disables
    AutoRun; ``/s`` strips the first/last quotes; ``/c`` runs the command.

    ``script`` is the ALREADY-VALIDATED absolute script path (P0-4): the
    interpreter must execute exactly the path that was containment-checked
    under the worktree, not the bare ``program`` string that cmd.exe would
    re-resolve against the command cwd (which may differ from the worktree).
    """
    return ["cmd.exe", "/d", "/s", "/c", str(script), *args]


def _build_bash_argv(*, script: Path, cwd: Path, args: list[str]) -> list[str]:
    """Build the bash token list (no concatenation, shell=False).

    P0-4: bash must execute the VALIDATED script, not the bare ``program`` that
    bash would re-resolve against the command cwd (which may be a different
    directory than the worktree root the program was validated against). We pass
    a POSIX RELATIVE path from ``cwd`` to the validated ``script`` (forward
    slashes) so bash — whose MSYS build mishandles Windows absolute paths like
    ``C:\\...`` (exit 127) — resolves exactly the validated file from its own
    cwd. ``os.path.relpath(validated_abs, cwd)`` always points at the validated
    file regardless of where cwd sits within the worktree.
    """
    rel = os.path.relpath(str(script), str(cwd)).replace("\\", "/")
    return ["bash", rel, *args]


# ---------------------------------------------------------------------------
# cwd resolution + containment
# ---------------------------------------------------------------------------


def _resolve_cwd(command_cwd: str, worktree_root: Path) -> Path:
    """Resolve ``command_cwd`` against ``worktree_root`` and assert it stays
    within the worktree (no escape via ``..`` or an absolute outside path).

    A path escape is surfaced as :class:`VerificationError` (this module owns its
    error type; ``paths.assert_within`` raises ``RootsError``, which we wrap)."""
    if not command_cwd:
        raise VerificationError("verification cwd must not be empty")
    wt = Path(worktree_root).resolve()
    # a relative cwd is joined under the worktree; an absolute cwd must already
    # be under it (paths.is_within resolves and detects ``..`` escapes).
    raw = Path(command_cwd)
    candidate = (raw if raw.is_absolute() else wt / raw).resolve()
    try:
        paths.assert_within(candidate, wt)
    except paths.RootsError as exc:
        raise VerificationError(
            f"verification cwd escapes the worktree: {exc}"
        ) from exc
    return candidate


def _resolve_script_path(program: str, worktree_root: Path) -> Path:
    """Resolve a script ``program`` under the worktree and assert containment
    (wrapped as :class:`VerificationError` on escape)."""
    if not program:
        raise VerificationError("verification program must not be empty")
    wt = Path(worktree_root).resolve()
    raw = Path(program)
    candidate = (raw if raw.is_absolute() else wt / raw).resolve()
    try:
        paths.assert_within(candidate, wt)
    except paths.RootsError as exc:
        raise VerificationError(
            f"verification script path escapes the worktree: {exc}"
        ) from exc
    return candidate


# ---------------------------------------------------------------------------
# run_verification
# ---------------------------------------------------------------------------


def run_verification(
    *,
    command: dict,
    worktree_root,
    program_allowlist,
    interpreter_allowlist,
    timeout: int | None = None,
    operation_dir=None,
    network_policy: NetworkPolicy | None = NetworkPolicy.ADVISORY_ONLY,
) -> dict:
    """Run a structured verification command and return
    ``{exit_code, stdout, stderr, command}``.

    See module docstring for the kind/allowlist/cwd/quoting rules. The network
    policy is advisory-only (or absent) — it is consulted for honesty only and
    never enforces a deny.

    ``timeout`` overrides ``command["timeout_sec"]`` if both are given.
    ``operation_dir`` is where redacted raw output is written; if None, a temp
    subdir under the worktree is used.
    """
    kind = command.get("kind")
    program = command.get("program", "")
    args = list(command.get("args", []))
    cmd_timeout = timeout or int(command.get("timeout_sec", 60))

    worktree_root = Path(worktree_root).resolve()

    # resolve cwd once (shared by all kinds); raises on escape.
    cwd = _resolve_cwd(command.get("cwd", "."), worktree_root)

    # operation_dir for redacted raw output
    if operation_dir is None:
        operation_dir = worktree_root / ".agentflow-verify"
    operation_dir = Path(operation_dir)
    operation_dir.mkdir(parents=True, exist_ok=True)

    if kind == "executable":
        if program not in set(program_allowlist or []):
            raise VerificationError(
                f"verification program {program!r} is not in the program_allowlist "
                f"(allowed: {sorted(set(program_allowlist or []))})"
            )
        argv = [program, *args]
    elif kind == "cmd_script":
        # P0-4: validate the program string (metacharacters/control chars) — the
        # same injection defense applied to args, since the program becomes the
        # ``cmd.exe /c <program>`` token.
        _check_cmd_program(program)
        # script must be under the worktree + .bat/.cmd
        script = _resolve_script_path(program, worktree_root)
        if script.suffix.lower() not in (".bat", ".cmd"):
            raise VerificationError(
                f"cmd_script program must be a .bat/.cmd under the worktree; "
                f"got {program!r}"
            )
        # interpreter must be allowlisted (parallel P1-3)
        if not (set(interpreter_allowlist or []) & {"cmd.exe", "cmd"}):
            raise VerificationError(
                f"cmd_script requires cmd.exe in the interpreter_allowlist "
                f"(got {sorted(set(interpreter_allowlist or []))})"
            )
        # P1-1: reject metacharacters/control chars in EVERY arg
        for a in args:
            _check_cmd_arg(a)
        # P0-4: argv executes the VALIDATED absolute script path, not the bare
        # program string (which cmd.exe would re-resolve against cwd, possibly a
        # different file than the one containment-checked under the worktree).
        argv = _build_cmd_argv(script=script, args=args)
    elif kind == "bash_script":
        _check_bash_program(program)
        script = _resolve_script_path(program, worktree_root)
        if script.suffix.lower() != ".sh":
            raise VerificationError(
                f"bash_script program must be a .sh under the worktree; "
                f"got {program!r}"
            )
        if not (set(interpreter_allowlist or []) & {"bash", "sh", "bash.exe", "sh.exe"}):
            raise VerificationError(
                f"bash_script requires bash in the interpreter_allowlist "
                f"(got {sorted(set(interpreter_allowlist or []))})"
            )
        for a in args:
            _check_bash_arg(a)
        argv = _build_bash_argv(script=script, cwd=cwd, args=args)
    else:
        raise VerificationError(
            f"unknown verification command kind {kind!r}; expected one of "
            f"executable / cmd_script / bash_script (an arbitrary shell string "
            f"is never accepted)"
        )

    exit_code, stdout, stderr = run_subprocess(
        argv,
        stdin_text=None,
        cwd=cwd,
        timeout=cmd_timeout,
        operation_dir=operation_dir,
    )
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "command": {"kind": kind, "program": program, "args": args},
    }
