"""Task 4.3 — GrokAdapter + immutable Review Bundle (fake executable; Review
P0-5; Prompt §13; parallel P0-4).

The GrokAdapter drives ``grok -p`` against an immutable Review Bundle. The
Phase-0 contract (docs/capabilities.json grok.*) is the authority:

  - the adapter ASSEMBLES the full Review Bundle under ``operation_dir`` (staging),
    reading SPEC/PLAN/manifest/execution-result via the ArtifactResolver (NOT
    control_root root files) + diff via gitops + a review-request.json + the
    rendered reviewer prompt.md + the resolved agent context;
  - ``--cwd`` is an ABSOLUTE Windows path to the bundle dir (G8);
  - ``--json-schema`` takes INLINE JSON (G8: ``@file`` unsupported);
  - the adapter EXPLICITLY injects HTTPS_PROXY/HTTP_PROXY/ALL_PROXY into the
    subprocess env (G7: grok has NO automatic proxy injection);
  - ``--permission-mode plan`` + ``--always-approve`` (read-only);
  - HEAD + clean are checked BEFORE and AFTER the call (changed/dirty => raise,
    routing to NEEDS_HUMAN); the bundle is byte-identical before vs after.

The adapter validates ONLY the RAW review-decision schema + its cross-field
invariants (parallel P0-4); the engine injects run_id/task_id/commits/hashes
and validates the FINAL review-result.

Tests use ``tests/adapters/_fakes/fake_grok.py`` (a real subprocess that reads
the bundle and emits a JSON envelope). No real grok CLI, no tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zcode_agentflow.adapters.grok import GrokAdapter, GrokAdapterError
from zcode_agentflow.adapters.base import AdapterTimeout

FAKE_GROK = str(Path(__file__).parent / "_fakes" / "fake_grok.py")
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"

# A synthetic proxy value for fixture use. Deliberately NOT a real operator
# address (port 1 is discarded; this is a placeholder so no machine-specific
# value ships in the repo). The adapter injects whatever proxy the request
# carries (G7); the value itself is opaque to the logic under test.
TEST_PROXY = "http://test-proxy.invalid:1"


# ---------------------------------------------------------------------------
# helpers: a fake git repo + a fake ArtifactResolver + bundle assembly args
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    """A real minimal git repo with a base commit + a one-commit task change, so
    gitops.head/is_clean/diff_patch work and HEAD/clean can be checked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, env=env)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, env=env
    ).stdout.strip()
    # task change commit
    (repo / "src").mkdir()
    (repo / "src" / "example.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "task"], cwd=repo, check=True, env=env)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, env=env
    ).stdout.strip()
    return repo, base, head


class _FakeResolver:
    """A minimal ArtifactResolver stand-in: serves logical-name -> bytes from a
    dict. The real ArtifactResolver (Task 3.5) reads through the generation
    pointer; for the bundle-assembly test we only need read_text/read."""

    def __init__(self, artifacts: dict[str, bytes]):
        self._artifacts = artifacts

    def read_text(self, name: str) -> str:
        return self._artifacts[name].decode("utf-8")

    def read(self, name: str) -> bytes:
        return self._artifacts[name]


def _bundle_inputs(repo: Path, base: str, head: str):
    """Build the resolver + the bundle-assembly kwargs for a review."""
    artifacts = {
        "SPEC.md": b"# Spec\n\nreturns a+b for two integers.\n",
        "PLAN.md": b"# Plan\n\nsingle task T001: add src/example.py.\n",
        "task-manifest.json": json.dumps({
            "tasks": [{"id": "T001", "declared_files": ["src/example.py"]}],
        }).encode("utf-8"),
        "execution-result.json": json.dumps({
            "verification": [{"exit_code": 0, "passed": True}],
        }).encode("utf-8"),
    }
    from zcode_agentflow import gitops
    diff = gitops.diff_patch(repo, base, head)
    resolver = _FakeResolver(artifacts)
    task = {"id": "T001", "title": "implement add", "declared_files": ["src/example.py"]}
    exec_result = json.loads(artifacts["execution-result.json"])
    return resolver, dict(
        run_worktree_root=repo, base_commit=base, head_commit=head,
        task=task, exec_result=exec_result,
        resolved_agent_context="# Agent context\nverification: python -m pytest\n",
    ), diff


def _assemble_and_run(tmp_path: Path, *, timeout: int = 30, fail_grok: bool = False,
                      monkeypatch=None):
    """Assemble a bundle and run GrokAdapter against the fake. Returns
    (result, diag, bundle_dir, repo, base, head)."""
    repo, base, head = _make_git_repo(tmp_path)
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    resolver, kwargs, _diff = _bundle_inputs(repo, base, head)

    from zcode_agentflow.review_bundle import assemble
    bundle_dir = assemble(
        operation_dir=op_dir, resolver=resolver,
        review_prompt="Review the task change against SPEC/PLAN.",
        **kwargs,
    )
    # snapshot bundle file hashes BEFORE the call (immutability check)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in bundle_dir.iterdir() if p.is_file()}

    adapter = GrokAdapter(command=sys.executable)
    request = {
        "executable": FAKE_GROK,
        "bundle_dir": str(bundle_dir),
        "repo_root": str(repo),
        "schema_path": str(SCHEMAS_DIR / "review-decision.schema.json"),
        "model": "grok-4.5",
        "sandbox": "read-only",
        "proxy": {"HTTPS_PROXY": TEST_PROXY,
                  "HTTP_PROXY": TEST_PROXY,
                  "ALL_PROXY": TEST_PROXY},
    }
    if monkeypatch is not None and fail_grok:
        monkeypatch.setenv("FAKE_GROK_DECISION", "FAIL")

    result = adapter.invoke(
        request=request, repo_cwd=repo, operation_dir=op_dir, timeout=timeout,
    )
    # the fake writes diagnostics OUTSIDE the bundle (into operation_dir), so
    # the bundle itself stays byte-pure for the immutability check.
    diag_path = op_dir / "fake_diag.json"
    diag = json.loads(diag_path.read_text(encoding="utf-8")) if diag_path.exists() else {}
    return result, diag, bundle_dir, repo, base, head, before


# ---------------------------------------------------------------------------
# 1. bundle contains all required files
# ---------------------------------------------------------------------------


def test_bundle_contains_all_required_files(tmp_path: Path):
    repo, base, head = _make_git_repo(tmp_path)
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    resolver, kwargs, _ = _bundle_inputs(repo, base, head)
    from zcode_agentflow.review_bundle import assemble
    bundle_dir = assemble(
        operation_dir=op_dir, resolver=resolver,
        review_prompt="Review the change.", **kwargs,
    )
    for name in ("SPEC.md", "PLAN.md", "task-manifest.json",
                 "execution-result.json", "diff.patch", "review-request.json",
                 "prompt.md"):
        p = bundle_dir / name
        assert p.is_file(), f"bundle missing {name}"
        assert p.stat().st_size > 0, f"bundle file {name} is empty"


# ---------------------------------------------------------------------------
# 2. grok output cites spec and plan (it read the bundle)
# ---------------------------------------------------------------------------


def test_grok_output_cites_spec_and_plan(tmp_path: Path):
    result, diag, _, _, _, _, _ = _assemble_and_run(tmp_path)
    assert result.exit_code == 0
    # the fake's `text` field quotes the SPEC and PLAN first lines (proving it
    # read the bundle files relative to --cwd). The fake records the first line
    # of each in diag, so assert both were read AND cited.
    assert "SPEC" in result.stdout  # the fake's text mentions SPEC
    assert "PLAN" in result.stdout  # and PLAN
    # the SPEC/PLAN content actually reached grok (the diag captured first lines)
    assert diag.get("spec_first_line"), "grok did not read SPEC.md"
    assert diag.get("plan_first_line"), "grok did not read PLAN.md"
    assert "# Spec" in diag["spec_first_line"]
    assert "# Plan" in diag["plan_first_line"]
    # the parsed RAW decision came from structuredOutput (the authoritative field)
    assert result.parsed is not None
    assert result.parsed["decision"] in ("PASS", "FAIL", "REPLAN", "BLOCKED")


# ---------------------------------------------------------------------------
# 3. readonly flags from capabilities (--permission-mode plan, --always-approve)
# ---------------------------------------------------------------------------


def test_readonly_flags_from_capabilities(tmp_path: Path):
    _, diag, _, _, _, _, _ = _assemble_and_run(tmp_path)
    assert diag["permission_mode"] == "plan"
    assert diag["always_approve"] is True
    # G7: proxy env was explicitly injected
    assert diag["proxy_injected"] is True


def test_grok_cwd_is_absolute_bundle_path(tmp_path: Path):
    """G8: --cwd is an ABSOLUTE path to the bundle dir."""
    _, diag, bundle_dir, _, _, _, _ = _assemble_and_run(tmp_path)
    assert diag["cwd_absolute"] is True
    assert Path(diag["cwd"]).resolve() == Path(bundle_dir).resolve()


def test_json_schema_passed_inline_not_as_file(tmp_path: Path):
    """G8: --json-schema takes INLINE JSON, not @file. Assert the argv value is
    parseable JSON (not a path) and matches the review-decision schema."""
    _, diag, _, _, _, _, _ = _assemble_and_run(tmp_path)
    argv = diag["argv"]
    i = argv.index("--json-schema")
    schema_arg = argv[i + 1]
    # it must be inline JSON (parseable), NOT an @file reference or a path
    assert not schema_arg.startswith("@")
    parsed_schema = json.loads(schema_arg)
    assert parsed_schema.get("title", "").startswith("review-decision")


# ---------------------------------------------------------------------------
# 4. bundle unchanged after call
# ---------------------------------------------------------------------------


def test_bundle_unchanged_after_call(tmp_path: Path):
    result, diag, bundle_dir, _, _, _, before = _assemble_and_run(tmp_path)
    assert result.exit_code == 0
    # the bundle files are byte-identical before vs after (grok read-only).
    # The fake writes its diagnostics OUTSIDE the bundle, so `before` and `after`
    # snapshot the pure bundle contents with no test-fixture to filter out.
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in bundle_dir.iterdir() if p.is_file()}
    assert before == after, "bundle files changed during the call"


# ---------------------------------------------------------------------------
# 5. HEAD + clean checked pre AND post (dirty/changed => raise)
# ---------------------------------------------------------------------------


def test_head_and_clean_checked_pre_and_post(tmp_path: Path):
    """A clean repo at a stable HEAD: the adapter runs and the post-checks
    pass (HEAD unchanged, tree clean)."""
    result, diag, bundle_dir, repo, base, head, _ = _assemble_and_run(tmp_path)
    assert result.exit_code == 0
    from zcode_agentflow import gitops
    # HEAD unchanged and tree clean after the call
    assert gitops.head(repo) == head
    assert gitops.is_clean(repo) is True


def test_dirty_repo_at_call_time_raises(tmp_path: Path):
    """If the repo is dirty (uncommitted change) at call time, the adapter
    raises GrokAdapterError rather than reviewing an uncommitted tree
    (routing to NEEDS_HUMAN via the engine)."""
    repo, base, head = _make_git_repo(tmp_path)
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    resolver, kwargs, _ = _bundle_inputs(repo, base, head)
    from zcode_agentflow.review_bundle import assemble
    bundle_dir = assemble(
        operation_dir=op_dir, resolver=resolver,
        review_prompt="Review.", **kwargs,
    )
    # dirty the repo AFTER bundle assembly (so the bundle is fine but the tree
    # is dirty at call time)
    (repo / "src" / "example.py").write_text("dirty change\n", encoding="utf-8")

    adapter = GrokAdapter(command=sys.executable)
    request = {
        "executable": FAKE_GROK, "bundle_dir": str(bundle_dir),
        "repo_root": str(repo),
        "schema_path": str(SCHEMAS_DIR / "review-decision.schema.json"),
        "model": "grok-4.5", "sandbox": "read-only",
        "proxy": {"HTTPS_PROXY": TEST_PROXY},
    }
    with pytest.raises(GrokAdapterError) as exc_info:
        adapter.invoke(request=request, repo_cwd=repo, operation_dir=op_dir, timeout=30)
    assert "clean" in str(exc_info.value).lower() or "dirty" in str(exc_info.value).lower() \
        or "head" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 6. result validated against RAW review-decision schema (parallel P0-4)
# ---------------------------------------------------------------------------


def test_result_validated_against_raw_review_schema(tmp_path: Path):
    result, diag, _, _, _, _, _ = _assemble_and_run(tmp_path)
    raw = result.parsed
    assert raw is not None
    # RAW decision must NOT carry authoritative fields (parallel P0-4)
    for forbidden in ("run_id", "task_id", "base_commit", "head_commit",
                      "plan_version", "spec_sha256", "plan_sha256"):
        assert forbidden not in raw, (
            f"RAW review-decision must NOT carry authoritative field {forbidden!r}"
        )
    # RAW-required fields present
    for required in ("decision", "spec_compliance", "plan_compliance",
                     "tests_sufficient", "issues", "replan_required"):
        assert required in raw


def test_fail_decision_cross_field_invariant_enforced(tmp_path: Path, monkeypatch):
    """The adapter validates the RAW cross-field invariants (decision/issues/
    replan consistency). A FAIL decision with issues passes; a PASS with issues
    would be rejected. Here we assert the adapter accepted a FAIL with one
    issue (cross-field consistent)."""
    result, _, _, _, _, _, _ = _assemble_and_run(
        tmp_path, fail_grok=True, monkeypatch=monkeypatch,
    )
    raw = result.parsed
    assert raw["decision"] == "FAIL"
    assert len(raw["issues"]) == 1  # FAIL requires >= 1 issue (satisfied)


def test_invalid_raw_review_rejected(tmp_path: Path, monkeypatch):
    """If grok emits a RAW decision violating the schema, the adapter raises
    (it does NOT pass garbage to the engine). We make the fake emit a decision
    missing required fields."""
    monkeypatch.setenv("FAKE_GROK_INVALID", "1")
    repo, base, head = _make_git_repo(tmp_path)
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    resolver, kwargs, _ = _bundle_inputs(repo, base, head)
    from zcode_agentflow.review_bundle import assemble
    bundle_dir = assemble(
        operation_dir=op_dir, resolver=resolver,
        review_prompt="Review.", **kwargs,
    )
    adapter = GrokAdapter(command=sys.executable)
    request = {
        "executable": FAKE_GROK, "bundle_dir": str(bundle_dir),
        "repo_root": str(repo),
        "schema_path": str(SCHEMAS_DIR / "review-decision.schema.json"),
        "model": "grok-4.5", "sandbox": "read-only",
        "proxy": {"HTTPS_PROXY": TEST_PROXY},
    }
    with pytest.raises(GrokAdapterError):
        adapter.invoke(request=request, repo_cwd=repo, operation_dir=op_dir, timeout=30)


def test_grok_timeout_kills_process_tree(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_GROK_HANG", "1")
    repo, base, head = _make_git_repo(tmp_path)
    op_dir = tmp_path / "operation"
    op_dir.mkdir(parents=True)
    resolver, kwargs, _ = _bundle_inputs(repo, base, head)
    from zcode_agentflow.review_bundle import assemble
    bundle_dir = assemble(
        operation_dir=op_dir, resolver=resolver,
        review_prompt="Review.", **kwargs,
    )
    adapter = GrokAdapter(command=sys.executable)
    request = {
        "executable": FAKE_GROK, "bundle_dir": str(bundle_dir),
        "repo_root": str(repo),
        "schema_path": str(SCHEMAS_DIR / "review-decision.schema.json"),
        "model": "grok-4.5", "sandbox": "read-only",
        "proxy": {"HTTPS_PROXY": TEST_PROXY},
    }
    import time
    start = time.perf_counter()
    with pytest.raises(AdapterTimeout):
        adapter.invoke(request=request, repo_cwd=repo, operation_dir=op_dir, timeout=2)
    assert time.perf_counter() - start < 15
