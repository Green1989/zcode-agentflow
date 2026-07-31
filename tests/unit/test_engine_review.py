"""Task 6.5 — reviewer_review_task (Review Bundle + hash injection; P0-5).

The engine assembles the immutable Review Bundle, calls Grok read-only, and
validates the review with hashes INJECTED from state (never trusted from the
model). HEAD is bound to execution_result.head_commit / state.verified_head_commit
BEFORE review (consistency #3); last_reviewed_commit is set only AFTER a PASS.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from zcode_agentflow import (
    artifacts,
    canonical,
    engine,
    gitops,
    manifest,
    store,
)
from zcode_agentflow.adapters.base import AdapterResult

RUN_ID = "20260730120000-abc123"


# ---------- fake grok --------------------------------------------------------


@dataclass
class FakeGrok:
    decision: dict
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append({"request": copy.deepcopy(request),
                           "bundle_dir": request.get("bundle_dir")})
        return AdapterResult(exit_code=0, stdout="", stderr="", artifacts={},
                             parsed=copy.deepcopy(self.decision))


def _pass_decision():
    return {
        "decision": "PASS", "spec_compliance": True, "plan_compliance": True,
        "tests_sufficient": True, "issues": [], "replan_required": False,
    }


def _fail_decision():
    return {
        "decision": "FAIL", "spec_compliance": False, "plan_compliance": True,
        "tests_sufficient": True,
        "issues": [{"id": "I1", "severity": "high", "category": "correctness",
                    "file": "src/x.py", "line": 1, "description": "bug",
                    "required_action": "fix"}],
        "replan_required": False,
    }


# ---------- helpers ----------------------------------------------------------


def _make_repo_verified(tmp_path: Path):
    """A run in VERIFYING_TASK with a committed change + execution-result gen."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    base = gitops.head(repo)

    control = tmp_path / "control"
    control.mkdir()
    wt = tmp_path / "worktree"
    gitops.create_worktree(target_repo=repo, worktree_path=wt, branch="agentflow/r1")
    # make a declared change + commit
    (Path(wt) / "src").mkdir()
    (Path(wt) / "src" / "x.py").write_text("print('x')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "task"], cwd=wt, check=True, env=env)
    head = gitops.head(wt)

    plan_task = {
        "id": "T001", "title": "do it", "dependencies": [],
        "declared_files": ["src/x.py"],
        "verification_commands": [
            {"kind": "executable", "program": sys.executable,
             "args": ["-c", "print('ok')"], "cwd": ".", "timeout_sec": 30}],
        "acceptance_criteria": ["tests pass"],
        "planned_changes": {"may_add_dependencies": False,
                            "may_run_migrations": False, "may_delete_files": []},
    }
    plan_sha = canonical.sha256_hex(canonical.canonical_text_bytes(b"# Plan\n\nP\n"))
    man = manifest.build_manifest({"tasks": [plan_task]}, plan_sha256=plan_sha)
    man_b = manifest.manifest_bytes(man)
    exec_obj = {
        "run_id": RUN_ID, "task_id": "T001", "status": "READY_FOR_REVIEW",
        "base_commit": base, "head_commit": head, "worktree_clean": True,
        "untracked_files": [], "verification": [],
        "changed_files": ["src/x.py"], "change_audit": {
            "deleted_files": [], "dependency_files_changed": False,
            "migrations_added": [], "undeclared_files_changed": []},
    }
    exec_b = canonical.canonical_json_bytes(exec_obj)

    st = store.State(
        run_id=RUN_ID, revision=14, state="VERIFYING_TASK",
        previous_state="EXECUTING_TASK", plan_version=1, current_task="T001",
        completed_tasks=[],
        roots={"plugin_root": "/plugin", "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        base_commit=base, task_start_commit=base, verified_head_commit=head,
    )
    st.save(control)
    nxt = copy.deepcopy(st)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_verify",
        changes={"SPEC.md": b"# Spec\n\nS\n", "PLAN.md": b"# Plan\n\nP\n",
                 "task-manifest.json": man_b, "execution-result.json": exec_b},
        prev_state=st, next_state=nxt,
    )
    nxt.save(control)
    return repo, wt, control, base, head, plan_task


def _plugin_root(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir(exist_ok=True)
    repo_template = Path(__file__).resolve().parents[2] / "AGENTS.md.template"
    (pr / "AGENTS.md.template").write_bytes(repo_template.read_bytes())
    return pr


def _engine(control, repo, wt, *, grok=None, config=None, plugin_root="/plugin"):
    s = store.Store(control)
    return engine.Engine(
        s,
        roots={"plugin_root": str(plugin_root), "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        config=config or {"verification": {"program_allowlist": [sys.executable],
                                            "interpreter_allowlist": ["bash", "cmd.exe"]}},
        adapters={"grok": grok} if grok else {},
    )


# ---------- 1. review PASS advances to REVIEWING_TASK ------------------------


def test_review_pass_advances_to_reviewing_task(tmp_path):
    repo, wt, control, base, head, _ = _make_repo_verified(tmp_path)
    grok = FakeGrok(decision=_pass_decision())
    eng = _engine(control, repo, wt, grok=grok, plugin_root=_plugin_root(tmp_path))
    st = store.State.load(control)

    result = eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)

    after = store.State.load(control)
    assert after.state == "REVIEWING_TASK"
    assert result["decision"] == "PASS"
    # the bundle was assembled before the call
    assert len(grok.calls) == 1
    bdir = Path(grok.calls[0]["bundle_dir"])
    for name in ("SPEC.md", "PLAN.md", "task-manifest.json", "execution-result.json",
                 "diff.patch", "review-request.json", "prompt.md"):
        assert (bdir / name).is_file(), name


# ---------- 2. review FAIL routes to NEEDS_HUMAN -----------------------------


def test_review_fail_routes_to_needs_human(tmp_path):
    repo, wt, control, base, head, _ = _make_repo_verified(tmp_path)
    grok = FakeGrok(decision=_fail_decision())
    eng = _engine(control, repo, wt, grok=grok, plugin_root=_plugin_root(tmp_path))
    st = store.State.load(control)

    eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"


# ---------- 3. hashes injected from state, not trusted ----------------------


def test_hashes_injected_from_state_not_trusted(tmp_path):
    repo, wt, control, base, head, _ = _make_repo_verified(tmp_path)
    grok = FakeGrok(decision=_pass_decision())
    eng = _engine(control, repo, wt, grok=grok, plugin_root=_plugin_root(tmp_path))
    st = store.State.load(control)

    eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)

    after = store.State.load(control)
    resolver = artifacts.ArtifactResolver(control, after)
    rev = json.loads(resolver.read("review-result.json"))
    # the injected shas equal the STATE's locked shas (not any model value)
    assert rev["spec_sha256"] == after.spec_sha256
    assert rev["plan_sha256"] == after.plan_sha256
    assert rev["task_manifest_sha256"] == after.task_manifest_sha256
    assert rev["run_id"] == RUN_ID
    assert rev["base_commit"] == base
    assert rev["head_commit"] == head


# ---------- 4. bundle assembled before call (all 7 files) -------------------


def test_bundle_assembled_before_call(tmp_path):
    repo, wt, control, base, head, _ = _make_repo_verified(tmp_path)
    grok = FakeGrok(decision=_pass_decision())
    eng = _engine(control, repo, wt, grok=grok, plugin_root=_plugin_root(tmp_path))
    st = store.State.load(control)
    eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)

    # the staged review bundle lives in the review operation's staging dir
    ops_dir = Path(control) / "operations"
    rev_op = next(p for p in ops_dir.iterdir() if "reviewer_review_task" in p.name)
    bdir = rev_op / "review-bundle"
    assert bdir.is_dir()
    for name in ("SPEC.md", "PLAN.md", "task-manifest.json", "execution-result.json",
                 "diff.patch", "review-request.json", "prompt.md"):
        assert (bdir / name).is_file()


# ---------- 5. HEAD bound to execution_result before review (consistency #3) -


def test_head_bound_to_execution_result_before_review(tmp_path):
    """Before review, the engine asserts HEAD == execution_result.head_commit AND
    HEAD == state.verified_head_commit; it does NOT consult last_reviewed_commit
    pre-review (that field is set only after a PASS). A moved HEAD => rejection."""
    repo, wt, control, base, head, _ = _make_repo_verified(tmp_path)
    grok = FakeGrok(decision=_pass_decision())
    eng = _engine(control, repo, wt, grok=grok, plugin_root=_plugin_root(tmp_path))
    # advance HEAD past the verified commit (simulate a post-verify change)
    env = {"PATH": os.environ.get("PATH", "")}
    (Path(wt) / "extra.txt").write_text("n\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "move head"], cwd=wt, check=True, env=env)

    st = store.State.load(control)
    with pytest.raises(Exception):
        eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"


# ---------- 6. last_reviewed_commit set AFTER review (consistency #3) --------


def test_last_reviewed_commit_set_after_review(tmp_path):
    repo, wt, control, base, head, _ = _make_repo_verified(tmp_path)
    grok = FakeGrok(decision=_pass_decision())
    eng = _engine(control, repo, wt, grok=grok, plugin_root=_plugin_root(tmp_path))
    # before review, last_reviewed_commit is unset
    assert store.State.load(control).last_reviewed_commit is None

    st = store.State.load(control)
    eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)

    after = store.State.load(control)
    # after a PASS, last_reviewed_commit == the reviewed head
    assert after.last_reviewed_commit == head
