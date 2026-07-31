"""Task 6.6 — workflow_complete_task (expanded rehash hard checks + READY_TO_MERGE).

complete runs the full hard-check set (≥11), incl. on-disk artifact rehash
(Prompt §14). On full success the run reaches READY_TO_MERGE (v1 terminal). No
merge, no push (Prompt §22 / Review §六.10).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zcode_agentflow import (
    active_run,
    artifacts,
    canonical,
    engine,
    gitops,
    manifest,
    store,
)

RUN_ID = "20260730120000-abc123"


# ---------- helpers ----------------------------------------------------------


def _make_repo_reviewed(tmp_path: Path, *, review_decision="PASS"):
    """A run in REVIEWING_TASK with SPEC/PLAN/manifest/execution-result/review-result
    all committed, worktree clean, HEAD == verified head. Returns
    (repo, wt, control, base, head)."""
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
    gitops.create_worktree(target_repo=repo, worktree_path=wt, branch="agentflow/c1")
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
    spec_b = b"# Spec\n\nS\n"
    plan_b = b"# Plan\n\nP\n"
    rev_obj = {
        "run_id": RUN_ID, "task_id": "T001", "base_commit": base, "head_commit": head,
        "spec_sha256": canonical.sha256_hex(canonical.canonical_text_bytes(spec_b)),
        "plan_sha256": canonical.sha256_hex(canonical.canonical_text_bytes(plan_b)),
        "task_manifest_sha256": canonical.sha256_hex(man_b),
        "plan_version": 1, "decision": review_decision,
        "spec_compliance": True, "plan_compliance": True, "tests_sufficient": True,
        "issues": [], "replan_required": False,
    }
    rev_b = canonical.canonical_json_bytes(rev_obj)

    st = store.State(
        run_id=RUN_ID, revision=18, state="REVIEWING_TASK",
        previous_state="VERIFYING_TASK", plan_version=1, current_task="T001",
        completed_tasks=[],
        roots={"plugin_root": "/plugin", "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        base_commit=base, task_start_commit=base, verified_head_commit=head,
        last_reviewed_commit=head if review_decision == "PASS" else None,
    )
    st.save(control)
    nxt = copy.deepcopy(st)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_review",
        changes={"SPEC.md": spec_b, "PLAN.md": plan_b, "task-manifest.json": man_b,
                 "execution-result.json": exec_b, "review-result.json": rev_b},
        prev_state=st, next_state=nxt,
    )
    nxt.save(control)
    return repo, wt, control, base, head


def _engine(control, repo, wt):
    s = store.Store(control)
    return engine.Engine(
        s,
        roots={"plugin_root": "/plugin", "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        config={"verification": {"program_allowlist": [sys.executable],
                                  "interpreter_allowlist": ["bash", "cmd.exe"]}},
    )


def _blob(control, gen_id, name) -> Path:
    gen = artifacts.load_generation(control, gen_id)
    return control / gen.index[name]["blob"]


# ---------- 1. complete succeeds only when all checks hold -------------------


def test_complete_succeeds_only_when_all_checks_hold(tmp_path):
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)

    result = eng.workflow_complete_task(task_id="T001", expected_revision=st.revision)

    after = store.State.load(control)
    assert after.state == "READY_TO_MERGE"
    assert result["state"] == "READY_TO_MERGE"
    # all checks reported true
    assert result["checks"]
    assert all(ok for (_n, ok, _r) in result["checks"])


# ---------- 2. complete rejects stale review commit --------------------------


def test_complete_rejects_stale_review_commit(tmp_path):
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    # advance HEAD past the reviewed commit
    env = {"PATH": os.environ.get("PATH", "")}
    (Path(wt) / "extra.txt").write_text("n\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "advance"], cwd=wt, check=True, env=env)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    with pytest.raises(Exception):
        eng.workflow_complete_task(task_id="T001", expected_revision=st.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"


# ---------- 3. complete rejects dirty / untracked worktree --------------------


def test_complete_rejects_dirty_or_untracked_worktree(tmp_path):
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    (Path(wt) / "stray.txt").write_text("oops\n", encoding="utf-8")  # untracked
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    with pytest.raises(Exception):
        eng.workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert store.State.load(control).state == "NEEDS_HUMAN"


# ---------- 4. complete rejects on tampered SPEC -----------------------------


def test_complete_rejects_on_tampered_spec(tmp_path):
    """A SPEC rewritten as a NEW generation post-approval (so state still holds
    the OLD locked sha) => rehash detects the drift (Review §五; P0-3)."""
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    st = store.State.load(control)
    locked_spec = st.spec_sha256
    # rewrite SPEC as a new generation (simulating a post-approval tamper that
    # updated the blob but left state's locked sha stale)
    new_spec = b"# Spec\n\nTAMPERED\n"
    nxt = copy.deepcopy(st)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_tamper",
        changes={"SPEC.md": new_spec}, prev_state=st, next_state=nxt,
    )
    # keep the OLD locked sha in the state view complete checks against
    nxt.spec_sha256 = locked_spec
    nxt.save(control)

    eng = _engine(control, repo, wt)
    with pytest.raises(Exception):
        eng.workflow_complete_task(task_id="T001", expected_revision=nxt.revision)
    assert store.State.load(control).state == "NEEDS_HUMAN"


# ---------- 5. complete rejects on tampered PLAN / manifest ------------------


def test_complete_rejects_on_tampered_plan_or_manifest(tmp_path):
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    # mutate the PLAN blob's bytes on disk (post-commit tamper)
    blob = _blob(control, "op_review", "PLAN.md")
    blob.write_bytes(b"# Plan\n\nTAMPERED\n")
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    with pytest.raises(Exception):
        eng.workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert store.State.load(control).state == "NEEDS_HUMAN"


# ---------- 6. complete rejects stale CAS ------------------------------------


def test_complete_rejects_stale_cas(tmp_path):
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    with pytest.raises(store.CasMismatch):
        eng.workflow_complete_task(task_id="T001", expected_revision=st.revision - 1)


# ---------- 7. complete does not merge or push -------------------------------


def test_complete_does_not_merge_or_push(tmp_path, monkeypatch):
    """No git push / git merge is invoked during complete; state == READY_TO_MERGE."""
    repo, wt, control, base, head = _make_repo_reviewed(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)

    calls = []
    real_run = subprocess.run

    def spy_run(args, *a, **kw):
        # record any git invocation whose first verb is push/merge
        if args and args[0] == "git" and len(args) > 1 and args[1] in ("push", "merge"):
            calls.append(args[1])
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy_run)
    eng.workflow_complete_task(task_id="T001", expected_revision=st.revision)

    assert calls == []  # no push/merge
    after = store.State.load(control)
    assert after.state == "READY_TO_MERGE"
