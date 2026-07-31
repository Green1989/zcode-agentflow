"""Task 6.4 — workflow_start_task + workflow_verify_task + change audit.

start records task_start_commit == HEAD; verify runs the verification command
policy + the git change policy (Prompt §16) and routes failures / unauthorized
changes / dirty worktrees to NEEDS_HUMAN. cmd_script verification runs on Windows.
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
    artifacts,
    canonical,
    engine,
    gitops,
    manifest,
    store,
)

RUN_ID = "20260730120000-abc123"


# ---------- helpers ----------------------------------------------------------


def _make_repo_with_plan(tmp_path: Path):
    """Build a real repo, a worktree, and seed a run in READY_FOR_EXECUTION with
    a committed SPEC/PLAN/manifest generation. Returns (repo, worktree, control,
    base_commit, plan_task)."""
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
    gitops.create_worktree(target_repo=repo, worktree_path=wt, branch="agentflow/v1")

    plan_task = {
        "id": "T001", "title": "do it", "dependencies": [],
        "declared_files": ["src/x.py"],
        "verification_commands": [
            {"kind": "executable", "program": sys.executable,
             "args": ["-c", "print('ok')"], "cwd": ".", "timeout_sec": 30},
        ],
        "acceptance_criteria": ["tests pass"],
        "planned_changes": {"may_add_dependencies": False,
                            "may_run_migrations": False, "may_delete_files": []},
    }
    spec_b = b"# Spec\n\nS\n"
    plan_md = "# Plan\n\none task\n"
    plan_sha = canonical.sha256_hex(canonical.canonical_text_bytes(plan_md.encode("utf-8")))
    man = manifest.build_manifest(
        {"run_id": RUN_ID, "tasks": [plan_task]}, plan_sha256=plan_sha
    )
    man_b = manifest.manifest_bytes(man)

    st = store.State(
        run_id=RUN_ID, revision=10, state="READY_FOR_EXECUTION",
        previous_state="PLANNING", plan_version=1, current_task="T001",
        completed_tasks=[],
        roots={"plugin_root": "/plugin", "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        base_commit=base,
    )
    st.save(control)
    nxt = copy.deepcopy(st)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_plan",
        changes={"SPEC.md": spec_b, "PLAN.md": plan_md.encode("utf-8"),
                 "task-manifest.json": man_b},
        prev_state=st, next_state=nxt,
    )
    nxt.save(control)
    return repo, wt, control, base, plan_task


def _engine(control, repo, wt, *, config=None):
    s = store.Store(control)
    return engine.Engine(
        s,
        roots={"plugin_root": "/plugin", "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        config=config or {"verification": {
            "program_allowlist": [sys.executable, "python"],
            "interpreter_allowlist": ["cmd.exe", "bash"]}},
    )


def _commit_change(wt, path, content, msg="task change"):
    env = {"PATH": os.environ.get("PATH", "")}
    p = Path(wt) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=wt, check=True, env=env)
    return gitops.head(wt)


# ---------- 1. start records task_start_commit -------------------------------


def test_start_records_task_start_commit(tmp_path):
    repo, wt, control, base, _ = _make_repo_with_plan(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)

    result = eng.workflow_start_task(task_id="T001", expected_revision=st.revision)

    after = store.State.load(control)
    assert after.state == "EXECUTING_TASK"
    # task_start_commit == HEAD at start time (the base, before any task change)
    assert after.task_start_commit == base
    assert result["task_start_commit"] == base


def test_start_from_wrong_state_rejected(tmp_path):
    repo, wt, control, _, _ = _make_repo_with_plan(tmp_path)
    eng = _engine(control, repo, wt)
    # force to a wrong state
    st = store.State.load(control)
    st.state = "EXECUTING_TASK"
    st.save(control)
    with pytest.raises(store.TransitionNotAllowed):
        eng.workflow_start_task(task_id="T001", expected_revision=st.revision)


# ---------- 2. verify pass when green + clean + authorized --------------------


def test_verify_pass_when_green_clean_authorized(tmp_path):
    repo, wt, control, base, _ = _make_repo_with_plan(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    # make an AUTHORIZED change (declared file src/x.py) + commit
    _commit_change(wt, "src/x.py", "print('x')\n")

    st2 = store.State.load(control)
    result = eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)

    after = store.State.load(control)
    assert after.state == "VERIFYING_TASK"
    assert result["status"] == "READY_FOR_REVIEW"
    # execution-result.json promoted; head bound
    resolver = artifacts.ArtifactResolver(control, after)
    exec_obj = json.loads(resolver.read("execution-result.json"))
    assert exec_obj["status"] == "READY_FOR_REVIEW"
    assert exec_obj["worktree_clean"] is True
    assert exec_obj["head_commit"] == gitops.head(wt)


# ---------- 3. verify routes to NEEDS_HUMAN on test failure ------------------


def test_verify_routes_to_needs_human_on_test_failure(tmp_path):
    repo, wt, control, base, plan_task = _make_repo_with_plan(tmp_path)
    # override the verification command to one that FAILS
    plan_task["verification_commands"] = [
        {"kind": "executable", "program": sys.executable,
         "args": ["-c", "import sys; sys.exit(1)"], "cwd": ".", "timeout_sec": 30}
    ]
    # rewrite the manifest with the failing command
    plan_sha = canonical.sha256_hex(canonical.canonical_text_bytes(b"# Plan\n\none task\n"))
    man = manifest.build_manifest({"run_id": RUN_ID, "tasks": [plan_task]}, plan_sha256=plan_sha)
    st_cur = store.State.load(control)
    nxt = copy.deepcopy(st_cur)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_plan2",
        changes={"task-manifest.json": manifest.manifest_bytes(man)},
        prev_state=st_cur, next_state=nxt,
    )
    nxt.save(control)

    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    _commit_change(wt, "src/x.py", "print('x')\n")

    st2 = store.State.load(control)
    result = eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"
    assert result["status"] == "VERIFICATION_FAILED"


# ---------- 4. verify routes to NEEDS_HUMAN on unauthorized change -----------


def test_verify_routes_to_needs_human_on_unauthorized_change(tmp_path):
    repo, wt, control, base, _ = _make_repo_with_plan(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    # UNDECLARED file (not in declared_files src/x.py) — out of scope
    _commit_change(wt, "src/other.py", "print('o')\n", msg="undeclared")

    st2 = store.State.load(control)
    eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"
    resolver = artifacts.ArtifactResolver(control, after)
    exec_obj = json.loads(resolver.read("execution-result.json"))
    assert "src/other.py" in exec_obj["change_audit"]["undeclared_files_changed"]


def test_verify_routes_to_needs_human_on_unauthorized_delete(tmp_path):
    repo, wt, control, base, plan_task = _make_repo_with_plan(tmp_path)
    # authorize NO deletes
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    # delete the README (not authorized)
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "rm", "-q", "README.md"], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "rm readme"], cwd=wt, check=True, env=env)

    st2 = store.State.load(control)
    eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"


# ---------- 5. verify routes to NEEDS_HUMAN on dirty / untracked --------------


def test_verify_routes_to_needs_human_on_dirty_or_untracked(tmp_path):
    repo, wt, control, base, _ = _make_repo_with_plan(tmp_path)
    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    _commit_change(wt, "src/x.py", "print('x')\n")
    # leave an UNTRACKED file (worktree not clean)
    (Path(wt) / "stray.txt").write_text("oops\n", encoding="utf-8")

    st2 = store.State.load(control)
    eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)
    after = store.State.load(control)
    assert after.state == "NEEDS_HUMAN"


# ---------- 6. authorized delete allowed -------------------------------------


def test_authorized_delete_allowed(tmp_path):
    repo, wt, control, base, plan_task = _make_repo_with_plan(tmp_path)
    # authorize the delete of README.md
    plan_task["planned_changes"]["may_delete_files"] = ["README.md"]
    plan_sha = canonical.sha256_hex(canonical.canonical_text_bytes(b"# Plan\n\none task\n"))
    man = manifest.build_manifest({"run_id": RUN_ID, "tasks": [plan_task]}, plan_sha256=plan_sha)
    st_cur = store.State.load(control)
    nxt = copy.deepcopy(st_cur)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_plan2",
        changes={"task-manifest.json": manifest.manifest_bytes(man)},
        prev_state=st_cur, next_state=nxt,
    )
    nxt.save(control)

    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "rm", "-q", "README.md"], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "authorized rm"], cwd=wt, check=True, env=env)

    st2 = store.State.load(control)
    result = eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)
    after = store.State.load(control)
    # authorized delete => READY_FOR_REVIEW (not NEEDS_HUMAN)
    assert after.state == "VERIFYING_TASK"
    assert result["status"] == "READY_FOR_REVIEW"


# ---------- 7. cmd_script verification runs (P1-5) ---------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="cmd_script is a Windows kind")
def test_cmd_script_verification_runs_on_windows(tmp_path):
    repo, wt, control, base, plan_task = _make_repo_with_plan(tmp_path)
    # write a .bat verification script into the worktree + declare it + use it
    Path(wt / "verify.bat").write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    plan_task["declared_files"] = ["src/x.py", "verify.bat"]
    plan_task["verification_commands"] = [
        {"kind": "cmd_script", "program": "verify.bat", "args": [],
         "cwd": ".", "timeout_sec": 30}
    ]
    plan_sha = canonical.sha256_hex(canonical.canonical_text_bytes(b"# Plan\n\none task\n"))
    man = manifest.build_manifest({"run_id": RUN_ID, "tasks": [plan_task]}, plan_sha256=plan_sha)
    st_cur = store.State.load(control)
    nxt = copy.deepcopy(st_cur)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_plan2",
        changes={"task-manifest.json": manifest.manifest_bytes(man)},
        prev_state=st_cur, next_state=nxt,
    )
    nxt.save(control)

    eng = _engine(control, repo, wt)
    st = store.State.load(control)
    eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
    # commit src/x.py AND verify.bat together (both declared) so the tree is clean
    env = {"PATH": os.environ.get("PATH", "")}
    (Path(wt) / "src").mkdir(exist_ok=True)
    (Path(wt) / "src" / "x.py").write_text("print('x')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "task change + bat"], cwd=wt, check=True, env=env)

    st2 = store.State.load(control)
    result = eng.workflow_verify_task(task_id="T001", expected_revision=st2.revision)
    after = store.State.load(control)
    # the .bat ran (exit 0) => READY_FOR_REVIEW
    assert result["status"] == "READY_FOR_REVIEW"
    assert after.state == "VERIFYING_TASK"
    resolver = artifacts.ArtifactResolver(control, after)
    exec_obj = json.loads(resolver.read("execution-result.json"))
    assert exec_obj["verification"]  # the cmd_script command ran
    assert exec_obj["verification"][0]["exit_code"] == 0
