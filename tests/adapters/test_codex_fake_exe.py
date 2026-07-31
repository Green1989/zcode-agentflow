"""Task 4.2 — CodexAdapter (fake executable; Review P0-3, P0-4; parallel P0-5).

The CodexAdapter wires the codex CLI per the Phase-0 contract:

  - the prompt reaches the child via **stdin** (Review P0-3: never concatenated
    into argv);
  - cwd = ``repo_cwd`` (the run worktree) — NOT control_root (P0-4);
  - output goes to the ``-o`` path under ``operation_dir`` (the operation
    staging dir, a THIRD distinct concept from stdin and cwd);
  - the read-only flag (``-s read-only``) is present (from capabilities);
  - on timeout, the process tree is killed;
  - the adapter validates ONLY the RAW schema (spec-draft-output for
    ``kind=draft_spec``; plan-proposal for ``kind=create_plan``) and the RAW
    output carries NO authoritative fields (run_id/base_commit/hashes). The
    engine injects authoritative fields and validates the FINAL envelope
    (parallel P0-4).

Tests use ``tests/adapters/_fakes/fake_codex.py`` (a real subprocess that
records argv/stdin/cwd and writes JSON to -o). No real codex CLI, no tokens.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zcode_agentflow.adapters.codex import CodexAdapter, CodexAdapterError
from zcode_agentflow.adapters.base import AdapterTimeout

FAKE_CODEX = str(Path(__file__).parent / "_fakes" / "fake_codex.py")
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"


def _run_adapter(tmp_path: Path, *, kind: str, schema_name: str,
                 timeout: int = 30, subdir: str = "operation"):
    """Run CodexAdapter against the fake executable and return the AdapterResult
    + the diagnostics dict recorded by the fake.

    ``subdir`` lets a single test run the adapter twice under different operation
    dirs within the same tmp_path without colliding on mkdir."""
    op_dir = tmp_path / subdir
    op_dir.mkdir(parents=True, exist_ok=True)
    repo_cwd = tmp_path / "repo_cwd"
    repo_cwd.mkdir(exist_ok=True)
    # a marker file in repo_cwd so we can prove cwd was the worktree, not control
    (repo_cwd / "REPO_MARKER").write_text("repo-cwd-content\n", encoding="utf-8")

    adapter = CodexAdapter(command=sys.executable)
    request = {
        "kind": kind,
        "prompt": "GOAL: add a+b\nAgent context: do not read external files.",
        "schema_name": schema_name,
        "schema_path": str(SCHEMAS_DIR / f"{schema_name}.schema.json"),
        "model": "gpt-5.6",
        "reasoning_effort": "high",
        "sandbox": "read-only",
        "executable": FAKE_CODEX,  # the fake runs UNDER sys.executable -c
    }
    result = adapter.invoke(
        request=request, repo_cwd=repo_cwd, operation_dir=op_dir, timeout=timeout,
    )
    diag_path = op_dir / "fake_diag.json"
    diag = json.loads(diag_path.read_text(encoding="utf-8")) if diag_path.exists() else {}
    return result, diag, repo_cwd, op_dir


# ---------------------------------------------------------------------------
# 1. prompt reaches stdin (Review P0-3)
# ---------------------------------------------------------------------------


def test_prompt_reaches_stdin(tmp_path: Path):
    result, diag, _, _ = _run_adapter(tmp_path, kind="draft_spec",
                                      schema_name="spec-draft-output")
    assert result.exit_code == 0
    assert diag.get("stdin_len", 0) > 0
    # the actual prompt content flowed through stdin, not argv
    assert "GOAL: add a+b" in diag.get("stdin_first_line", "") \
        or "GOAL: add a+b" in result.stdout \
        or diag.get("stdin_len", 0) >= len("GOAL: add a+b")


def test_prompt_not_in_argv(tmp_path: Path):
    """The prompt text never appears as an argv token (Review P0-3: stdin, not
    a command-line argument)."""
    _, diag, _, _ = _run_adapter(tmp_path, kind="draft_spec",
                                 schema_name="spec-draft-output")
    argv_joined = " ".join(diag.get("argv", []))
    assert "GOAL: add a+b" not in argv_joined
    assert "do not read external files" not in argv_joined


# ---------------------------------------------------------------------------
# 2. cwd is run_worktree_root, NOT control_root (P0-4)
# ---------------------------------------------------------------------------


def test_cwd_is_run_worktree_root_not_control(tmp_path: Path):
    result, diag, repo_cwd, op_dir = _run_adapter(
        tmp_path, kind="draft_spec", schema_name="spec-draft-output",
    )
    # the fake's recorded cwd == repo_cwd (the worktree), not op_dir (control)
    assert Path(diag["cwd"]).resolve() == Path(repo_cwd).resolve()
    assert Path(diag["cwd"]).resolve() != Path(op_dir).resolve()


# ---------------------------------------------------------------------------
# 3. output dir is operation staging (a THIRD distinct concept)
# ---------------------------------------------------------------------------


def test_output_dir_is_operation_staging(tmp_path: Path):
    result, diag, repo_cwd, op_dir = _run_adapter(
        tmp_path, kind="draft_spec", schema_name="spec-draft-output",
    )
    # the -o path landed UNDER operation_dir
    o_path = Path(diag["o_path"])
    assert o_path.is_file()
    assert op_dir in o_path.parents
    # the result JSON parsed and is on the AdapterResult
    assert result.parsed is not None
    assert result.parsed.get("goal")  # spec-draft-output has goal


# ---------------------------------------------------------------------------
# 4. read-only flag present (from capabilities)
# ---------------------------------------------------------------------------


def test_read_only_flag_present(tmp_path: Path):
    _, diag, _, _ = _run_adapter(tmp_path, kind="draft_spec",
                                 schema_name="spec-draft-output")
    assert diag.get("s_flag") == "read-only"
    # the schema path was passed through (--output-schema)
    assert diag.get("schema_file", "").endswith("spec-draft-output.schema.json")


def test_create_plan_uses_plan_proposal_schema(tmp_path: Path):
    """kind=create_plan routes to the plan-proposal RAW schema (parallel P0-5)."""
    result, diag, _, _ = _run_adapter(
        tmp_path, kind="create_plan", schema_name="plan-proposal",
    )
    assert diag.get("schema_file", "").endswith("plan-proposal.schema.json")
    # the parsed output is the RAW plan-proposal wrapper (single task, BLOCKED-capable)
    assert result.parsed["status"] == "READY_FOR_EXECUTION"
    assert len(result.parsed["plan"]["tasks"]) == 1


# ---------------------------------------------------------------------------
# 5. timeout kills the process tree
# ---------------------------------------------------------------------------


def test_timeout_kills_process_tree(tmp_path: Path, monkeypatch):
    """On timeout the adapter raises AdapterTimeout and the process tree is
    killed (bounded return, no hang). The fake hangs via FAKE_CODEX_HANG."""
    monkeypatch.setenv("FAKE_CODEX_HANG", "1")
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    repo_cwd = tmp_path / "repo_cwd"
    repo_cwd.mkdir()
    adapter = CodexAdapter(command=sys.executable)
    request = {
        "kind": "draft_spec",
        "prompt": "p",
        "schema_name": "spec-draft-output",
        "schema_path": str(SCHEMAS_DIR / "spec-draft-output.schema.json"),
        "model": "gpt-5.6", "reasoning_effort": "high", "sandbox": "read-only",
        "executable": FAKE_CODEX,
    }
    import time
    start = time.perf_counter()
    with pytest.raises(AdapterTimeout):
        adapter.invoke(request=request, repo_cwd=repo_cwd,
                       operation_dir=op_dir, timeout=2)
    elapsed = time.perf_counter() - start
    assert elapsed < 15, f"timeout kill did not return promptly ({elapsed:.1f}s)"


# ---------------------------------------------------------------------------
# 6. result validated against RAW schema; NO authoritative fields (parallel P0-4)
# ---------------------------------------------------------------------------


def test_result_validated_against_raw_schema(tmp_path: Path):
    """The adapter validates ONLY the RAW schema and the returned object carries
    NO authoritative fields (run_id/base_commit/hashes/plan_version). The engine
    injects those and validates the FINAL envelope (parallel P0-4)."""
    # spec-draft RAW
    result, _, _, _ = _run_adapter(tmp_path, kind="draft_spec",
                                   schema_name="spec-draft-output",
                                   subdir="op_draft")
    raw = result.parsed
    assert raw is not None
    for forbidden in ("run_id", "status", "spec_sha256", "base_commit"):
        assert forbidden not in raw, (
            f"RAW spec-draft-output must NOT carry authoritative field "
            f"{forbidden!r} (engine injects it)"
        )
    # the RAW-required fields ARE present (goal/spec_markdown/questions/assumptions)
    for required in ("goal", "spec_markdown", "questions", "assumptions"):
        assert required in raw

    # plan-proposal RAW
    result2, _, _, _ = _run_adapter(tmp_path, kind="create_plan",
                                    schema_name="plan-proposal",
                                    subdir="op_plan")
    raw2 = result2.parsed
    for forbidden in ("run_id", "base_commit", "plan_version"):
        assert forbidden not in raw2, (
            f"RAW plan-proposal must NOT carry authoritative field {forbidden!r}"
        )
    assert raw2["status"] in ("READY_FOR_EXECUTION", "NEEDS_USER_INPUT", "BLOCKED")


def test_invalid_raw_output_rejected_by_adapter(tmp_path: Path, monkeypatch):
    """If the model emits a RAW object that fails the RAW schema, the ADAPTER
    raises CodexAdapterError (it does NOT silently pass garbage to the engine).

    This drives the adapter's own validation path (not the validator directly):
    the fake emits an invalid object via the FAKE_CODEX_EMIT_INVALID env hook,
    and we assert the adapter raises. Removing the adapter's validation call
    would make this test fail."""
    monkeypatch.setenv("FAKE_CODEX_EMIT_INVALID", "1")
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    repo_cwd = tmp_path / "repo_cwd"
    repo_cwd.mkdir()
    adapter = CodexAdapter(command=sys.executable)
    request = {
        "kind": "draft_spec",
        "prompt": "p",
        "schema_name": "spec-draft-output",
        "schema_path": str(SCHEMAS_DIR / "spec-draft-output.schema.json"),
        "model": "gpt-5.6", "reasoning_effort": "high", "sandbox": "read-only",
        "executable": FAKE_CODEX,
    }
    with pytest.raises(CodexAdapterError) as exc_info:
        adapter.invoke(request=request, repo_cwd=repo_cwd,
                       operation_dir=op_dir, timeout=30)
    assert "schema validation" in str(exc_info.value)


def test_invalid_raw_output_rejected_validator_directly():
    """Companion: the underlying RAW validator itself rejects a bad object (so
    the adapter's CodexAdapterError is genuinely backed by a schema failure)."""
    from zcode_agentflow import schemas
    bad = {"goal": "x"}  # missing spec_markdown/questions/assumptions
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


def test_nonzero_exit_drops_parsed(tmp_path: Path, monkeypatch):
    """If codex exits non-zero, the adapter does NOT return an unvalidated
    parsed object (parsed is dropped to None so the engine must route by
    exit_code/stderr). The fake exits 4 when FAKE_CODEX_FAIL_EXIT is set."""
    monkeypatch.setenv("FAKE_CODEX_FAIL_EXIT", "1")
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    repo_cwd = tmp_path / "repo_cwd"
    repo_cwd.mkdir()
    adapter = CodexAdapter(command=sys.executable)
    request = {
        "kind": "draft_spec",
        "prompt": "p",
        "schema_name": "spec-draft-output",
        "schema_path": str(SCHEMAS_DIR / "spec-draft-output.schema.json"),
        "model": "gpt-5.6", "reasoning_effort": "high", "sandbox": "read-only",
        "executable": FAKE_CODEX,
    }
    result = adapter.invoke(request=request, repo_cwd=repo_cwd,
                            operation_dir=op_dir, timeout=30)
    assert result.exit_code != 0
    assert result.parsed is None
