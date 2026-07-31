"""Adapter base protocol + shared subprocess helper (Task 4.1; Prompt §12, §20).

All three adapters (Codex, Grok, Verification) share:

  - :class:`AdapterResult` — the uniform return type
    ``{exit_code, stdout, stderr, artifacts, parsed}``.
  - :class:`Adapter` — the Protocol each adapter implements:
    ``invoke(*, request, repo_cwd, operation_dir, timeout) -> AdapterResult``.
  - :func:`run_subprocess` — the shared subprocess launcher.

:func:`run_subprocess` enforces the cross-adapter invariants:

  - ``shell=False`` always (no ``cmd /c <concatenated string>``); the command
    is a token list.
  - stdout/stderr captured IN MEMORY, never inherited (P1-6 clean-stdout;
    P1-4 capture-before-write).
  - ORDER (P1-4, load-bearing): capture -> redact -> write the REDACTED bytes to
    ``operation_dir/raw/{stdout,stderr}.txt``. There is no raw-then-overwrite
    window: a crash never persists the unredacted secret. An unredacted raw
    debug log exists ONLY behind the explicit ``raw_debug_log=True`` opt-in,
    which emits a visible security warning (default off).
  - timeout kills the process TREE (Windows ``CREATE_NEW_PROCESS_GROUP`` +
    ``taskkill /T /PID``; POSIX ``os.setsid`` + ``SIGKILL`` on the group), so a
    grandchild does not outlive the killed child.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zcode_agentflow import redact

__all__ = [
    "Adapter",
    "AdapterResult",
    "AdapterTimeout",
    "run_subprocess",
]

_log = logging.getLogger(__name__)
_RAW_DIRNAME = "raw"


class AdapterTimeout(TimeoutError):
    """Raised by :func:`run_subprocess` when the subprocess exceeds ``timeout``
    seconds. The process tree has already been killed when this is raised."""


@dataclass
class AdapterResult:
    """Uniform return type for every adapter invocation.

    ``exit_code`` / ``stdout`` / ``stderr`` come from the (redacted) subprocess
    capture. ``artifacts`` maps a logical name to the path the adapter wrote
    (e.g. ``{"out.json": Path(...)}``); it is the adapter's record of what it
    produced under ``operation_dir``. ``parsed`` is the adapter's parsed model
    output (the RAW object, validated against the RAW schema) — authoritative
    fields are injected by the engine later, NOT here.
    """

    exit_code: int
    stdout: str
    stderr: str
    artifacts: dict[str, Path] = field(default_factory=dict)
    parsed: Any = None


@runtime_checkable
class Adapter(Protocol):
    """The shared adapter contract. Each adapter implements ``invoke`` with the
    fixed keyword signature so the engine calls them uniformly."""

    def invoke(
        self, *, request: dict, repo_cwd, operation_dir, timeout: int
    ) -> AdapterResult:  # pragma: no cover - protocol body
        ...


# ---------------------------------------------------------------------------
# process-tree kill (Windows: taskkill /T; POSIX: setsid + killpg)
# ---------------------------------------------------------------------------


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the process TREE rooted at ``proc``.

    On Windows, children are not in the parent's process group by default; we
    launched the child with ``CREATE_NEW_PROCESS_GROUP`` so ``taskkill /T /PID``
    walks the tree. On POSIX, we launched with ``start_new_session=True`` so the
    child is a session leader and ``os.killpg(os.getpgid(pid), SIGKILL)``
    reaches the whole group.
    """
    pid = proc.pid
    if sys.platform == "win32":
        # /T kills the process AND any child processes it started.
        # /F forces termination (we are already past the timeout).
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,  # never let taskkill write to our stdout
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), 9)  # SIGKILL the whole group
        except (ProcessLookupError, PermissionError):
            pass
    # ensure the Popen handle is reaped
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _creation_flags() -> int:
    """Windows: create the child in its own process group so a timeout can kill
    the whole tree. POSIX returns 0 (uses start_new_session instead)."""
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        return 0x00000200
    return 0


def _popen_kwargs(cwd) -> dict:
    """Build the subprocess.run kwargs that give us a killable, captured,
    shell=False child on both platforms."""
    kwargs: dict[str, Any] = dict(
        shell=False,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = _creation_flags()
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _write_raw(operation_dir: Path, name: str, content: str) -> None:
    """Write ``content`` to ``operation_dir/raw/<name>`` (UTF-8). The caller has
    already redacted; we just persist."""
    raw_dir = operation_dir / _RAW_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / name).write_text(content, encoding="utf-8")


def run_subprocess(
    cmd: list[str],
    *,
    stdin_text: str | None,
    cwd,
    timeout: int,
    operation_dir,
    raw_debug_log: bool = False,
) -> tuple[int, str, str]:
    """Run ``cmd`` (token list, ``shell=False``) and return
    ``(exit_code, stdout, stderr)`` with the P1-4 redaction ordering.

    Steps:
      1. launch with capture (stdout/stderr in memory; never inherited);
      2. on timeout, kill the process TREE and raise :class:`AdapterTimeout`;
      3. REDACT the captured stdout/stderr in memory;
      4. write the REDACTED bytes to ``operation_dir/raw/{stdout,stderr}.txt``;
      5. if ``raw_debug_log=True`` (opt-in), ALSO write the UNREDACTED bytes to
         ``operation_dir/raw/{stdout,stderr}.debug.txt`` and emit a visible
         SECURITY WARNING. Default off — no unredacted raw log exists anywhere.
      6. return the redacted ``(exit_code, stdout, stderr)``.

    The return values are already redacted, so a caller that persists them (or
    logs them) cannot leak the secret. A crash between step 3 and step 4 leaves
    NO secret on disk (nothing was written yet); a crash during step 4 leaves
    at most a partial redacted file. The unredacted form exists only behind the
    explicit opt-in (step 5), which warns.
    """
    operation_dir = Path(operation_dir)
    if raw_debug_log:
        # Visible security warning: unredacted raw logging is being enabled.
        _log.warning(
            "SECURITY WARNING: raw_debug_log=True — UNREDACTED subprocess output "
            "will be persisted to %s/raw/*.debug.txt. Use only for local debugging; "
            "never enable in production or CI that handles real credentials.",
            operation_dir,
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            **_popen_kwargs(cwd),
        )
    except FileNotFoundError as exc:
        # missing executable -> surface as a normal failure (non-zero), no hang
        _write_raw(operation_dir, "stderr.txt", f"executable not found: {cmd[0]}\n")
        raise

    try:
        out_bytes, err_bytes = proc.communicate(
            input=stdin_text, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        # persist a redacted marker that we timed out (no secret leak)
        _write_raw(
            operation_dir, "stderr.txt",
            f"[adapter] timeout after {timeout}s; process tree killed\n",
        )
        raise AdapterTimeout(
            f"subprocess {cmd[0]!r} exceeded timeout {timeout}s; tree killed"
        ) from None

    exit_code = proc.returncode
    stdout_raw = out_bytes or ""
    stderr_raw = err_bytes or ""

    # P1-4 ORDER: redact in memory FIRST.
    stdout_red = redact.redact(stdout_raw)
    stderr_red = redact.redact(stderr_raw)

    # then persist the REDACTED bytes (the only default raw log).
    _write_raw(operation_dir, "stdout.txt", stdout_red)
    _write_raw(operation_dir, "stderr.txt", stderr_red)

    # opt-in unredacted debug log (default off; warned above).
    if raw_debug_log:
        _write_raw(operation_dir, "stdout.debug.txt", stdout_raw)
        _write_raw(operation_dir, "stderr.debug.txt", stderr_raw)

    return exit_code, stdout_red, stderr_red
