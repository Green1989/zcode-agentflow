"""Task 4.1 — Adapter base protocol (Prompt §12).

The shared adapter contract: a fake adapter is invoked with
``(request, repo_cwd, operation_dir, timeout)`` and returns an
``AdapterResult{exit_code, stdout, stderr, artifacts, parsed}``. Raw
stdout/stderr are written under ``operation_dir/raw/`` (the ONLY place adapters
persist subprocess output). ``run_subprocess`` is the shared helper: it always
uses ``shell=False``, captures stdout/stderr IN MEMORY, kills the process tree
on timeout, and applies redaction before writing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from zcode_agentflow.adapters import base
from zcode_agentflow.adapters.base import AdapterResult, run_subprocess


# ---------------------------------------------------------------------------
# AdapterResult shape
# ---------------------------------------------------------------------------


def test_adapter_result_has_required_fields():
    """AdapterResult carries exit_code/stdout/stderr/artifacts/parsed."""
    r = AdapterResult(
        exit_code=0, stdout="out", stderr="err",
        artifacts={"out.json": Path("/tmp/out.json")}, parsed={"x": 1},
    )
    assert r.exit_code == 0
    assert r.stdout == "out"
    assert r.stderr == "err"
    assert r.artifacts == {"out.json": Path("/tmp/out.json")}
    assert r.parsed == {"x": 1}


# ---------------------------------------------------------------------------
# run_subprocess — shell=False, capture, raw/ write
# ---------------------------------------------------------------------------


def test_run_subprocess_uses_shell_false_and_captures(tmp_path: Path):
    """run_subprocess launches with shell=False and captures stdout/stderr in
    memory, returning them on the AdapterResult-shaped tuple (exit, out, err)."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    # a python one-liner that prints to stdout and stderr
    code = (
        "import sys\n"
        "sys.stdout.write('hello-out\\n')\n"
        "sys.stderr.write('hello-err\\n')\n"
    )
    exit_code, out, err = run_subprocess(
        [sys.executable, "-c", code],
        stdin_text=None, cwd=tmp_path, timeout=30, operation_dir=op_dir,
    )
    assert exit_code == 0
    assert "hello-out" in out
    assert "hello-err" in err
    # raw/ received the (redacted) captured streams
    raw_stdout = (op_dir / "raw" / "stdout.txt").read_text(encoding="utf-8")
    raw_stderr = (op_dir / "raw" / "stderr.txt").read_text(encoding="utf-8")
    assert "hello-out" in raw_stdout
    assert "hello-err" in raw_stderr


def test_run_subprocess_passes_stdin(tmp_path: Path):
    """stdin_text reaches the child via its stdin (Codex contract: prompt via
    stdin, never via argv)."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    code = "import sys\n" "sys.stdout.write(sys.stdin.read())\n"
    exit_code, out, _ = run_subprocess(
        [sys.executable, "-c", code],
        stdin_text="PROMPT-VIA-STDIN", cwd=tmp_path, timeout=30,
        operation_dir=op_dir,
    )
    assert exit_code == 0
    assert "PROMPT-VIA-STDIN" in out


def test_run_subprocess_nonzero_exit_returned(tmp_path: Path):
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    code = "import sys\n" "sys.stderr.write('boom\\n')\n" "sys.exit(3)\n"
    exit_code, out, err = run_subprocess(
        [sys.executable, "-c", code],
        stdin_text=None, cwd=tmp_path, timeout=30, operation_dir=op_dir,
    )
    assert exit_code == 3
    assert "boom" in err


def test_run_subprocess_cwd_is_respected(tmp_path: Path):
    """cwd is the child's working directory (Codex: cwd=run_worktree_root)."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "marker.txt").write_text("iamhere\n", encoding="utf-8")
    code = (
        "import sys, os\n"
        "sys.stdout.write(open('marker.txt').read())\n"
    )
    exit_code, out, _ = run_subprocess(
        [sys.executable, "-c", code],
        stdin_text=None, cwd=workdir, timeout=30, operation_dir=op_dir,
    )
    assert exit_code == 0
    assert "iamhere" in out


# ---------------------------------------------------------------------------
# timeout kills the process tree (no hang, no orphan)
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_run_subprocess_timeout_kills_process_tree(tmp_path: Path):
    """On timeout, run_subprocess kills the process TREE (not just the root
    child): it returns within bounded time AND the orphaned grandchild is no
    longer alive. We prove tree-kill by recording the grandchild's PID to a file
    (stdout is not captured on the timeout path) and checking it is dead."""
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    pid_file = tmp_path / "gcpid.txt"
    # child spawns a grandchild that outlives the timeout, records its PID, sleeps
    code = (
        "import subprocess, sys, time, os\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(os.environ['GC_PID_FILE'], 'w').write(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    env = dict(os.environ, GC_PID_FILE=str(pid_file))
    start = __import__("time").perf_counter()
    # run_subprocess does not take an env param; emulate by setting it in the
    # current process for the duration of the call (the test process is private).
    saved = os.environ.get("GC_PID_FILE")
    os.environ["GC_PID_FILE"] = str(pid_file)
    try:
        with pytest.raises(base.AdapterTimeout):
            run_subprocess(
                [sys.executable, "-c", code],
                stdin_text=None, cwd=tmp_path, timeout=1, operation_dir=op_dir,
            )
    finally:
        if saved is None:
            os.environ.pop("GC_PID_FILE", None)
        else:
            os.environ["GC_PID_FILE"] = saved
    elapsed = __import__("time").perf_counter() - start
    # must return promptly after the timeout (well under the 60s sleep)
    assert elapsed < 15, f"timeout kill did not return promptly ({elapsed:.1f}s)"

    # THE tree-kill proof: wait for the child to have written the grandchild PID,
    # then assert that grandchild is no longer alive.
    import time as _t
    for _ in range(50):  # up to ~2.5s for the child's PID-file write
        if pid_file.exists():
            break
        _t.sleep(0.05)
    assert pid_file.exists(), "child never recorded the grandchild PID"
    gpid = int(pid_file.read_text().strip())
    _t.sleep(0.5)  # let the tree kill settle
    alive = False
    try:
        if sys.platform == "win32":
            # os.kill(pid, 0) on Windows still raises if the process is gone;
            # but taskkill is the source of truth. Use subprocess to probe.
            r = subprocess.run(
                ["taskkill", "/PID", str(gpid), "/T", "/F"],
                capture_output=True, text=True,
            )
            # exit 128 means "no such process" (already dead); 0 means it WAS
            # alive and we just killed it (cleanup) — a tree-kill regression.
            already_dead = r.returncode != 0
            alive = not already_dead
            if alive:
                # we killed it as cleanup; the assertion below will fail loudly
                pass
        else:
            os.kill(gpid, 0)
            alive = True
            # cleanup
            try:
                import signal
                os.kill(gpid, signal.SIGKILL)
            except Exception:
                pass
    except (ProcessLookupError, PermissionError, OSError):
        alive = False
    assert not alive, (
        f"grandchild pid {gpid} was STILL ALIVE after the timeout — process-tree "
        f"kill did not reach the grandchild (regression in _kill_process_tree)"
    )


# ---------------------------------------------------------------------------
# Adapter Protocol — a fake adapter implementing invoke()
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal Adapter: runs a trivial subprocess and returns an AdapterResult."""

    def invoke(self, *, request, repo_cwd, operation_dir, timeout):
        code = "import sys\n" "sys.stdout.write('fake-out\\n')\n"
        exit_code, out, err = run_subprocess(
            [sys.executable, "-c", code],
            stdin_text=request.get("prompt"), cwd=repo_cwd, timeout=timeout,
            operation_dir=operation_dir,
        )
        return AdapterResult(
            exit_code=exit_code, stdout=out, stderr=err,
            artifacts={}, parsed={"echoed": True},
        )


def test_fake_adapter_invoke_signature_and_result(tmp_path: Path):
    op_dir = tmp_path / "op"
    (op_dir / "raw").mkdir(parents=True)
    r = _FakeAdapter().invoke(
        request={"prompt": "hello"}, repo_cwd=tmp_path,
        operation_dir=op_dir, timeout=30,
    )
    assert isinstance(r, AdapterResult)
    assert r.exit_code == 0
    assert "fake-out" in r.stdout
    assert r.parsed == {"echoed": True}
    # raw/ was written by the shared helper
    assert (op_dir / "raw" / "stdout.txt").exists()


def test_adapter_protocol_is_runtime_checkable():
    """The Adapter Protocol can be used with isinstance/issubclass checks
    (runtime_checkable) so the engine can assert a given object is an Adapter."""
    from zcode_agentflow.adapters.base import Adapter
    assert isinstance(_FakeAdapter(), Adapter)
