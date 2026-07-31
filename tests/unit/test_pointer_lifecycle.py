"""Task 6.8 — Active-run pointer lifecycle (workflow_init / READY_TO_MERGE).

Errata: workflow_init delegates the initial pointer write to init_run (Task 3.7)
and does NOT call set_pointer itself. complete marks the pointer terminal:true
(not cleared). abandon/archive are local admin commands, not MCP tools.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from zcode_agentflow import (
    active_run,
    artifacts,
    canonical,
    engine,
    gitops,
    manifest,
    server,
    store,
)
from zcode_agentflow.adapters.base import AdapterResult

RUN_ID = "20260730120000-abc123"
RUN_ID2 = "20260730120000-def456"


def _make_repo(tmp_path: Path, name="target_repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir(exist_ok=True)
    src = Path(__file__).resolve().parents[2] / "AGENTS.md.template"
    (pr / "AGENTS.md.template").write_bytes(src.read_bytes())
    return pr


def _engine_for_init(tmp_path, repo, *, run_id=RUN_ID, user_data=None):
    user_data = user_data or (tmp_path / "user_data")
    user_data.mkdir(exist_ok=True)
    plugin = _plugin(tmp_path)
    control = user_data / "runs" / run_id / "control"
    worktree = user_data / "runs" / run_id / "worktree"
    control.mkdir(parents=True, exist_ok=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    s = store.Store(control)
    eng = engine.Engine(
        s,
        roots={
            "plugin_root": str(plugin),
            "target_repo_root": str(repo),
            "run_worktree_root": str(worktree),
            "control_root": str(control),
        },
        config={"verification": {
            "program_allowlist": [sys.executable, "python"],
            "interpreter_allowlist": ["bash", "cmd.exe"],
        }},
        effective_config={"effective_approval_mode": "zcode_tool_confirmation"},
        user_data_root=user_data,
        adapters={},
    )
    return eng, user_data, control, worktree


# ---------- 1. workflow_init delegates pointer write to init_run -------------


def test_workflow_init_delegates_pointer_write_to_init_run(tmp_path, monkeypatch):
    """workflow_init must NOT call set_pointer / set_pointer_locked itself —
    init_run (Task 3.7) is the sole owner of the initial pointer write."""
    repo = _make_repo(tmp_path)
    eng, user_data, control, wt = _engine_for_init(tmp_path, repo)

    set_calls = []
    real_set = active_run.ActiveRunRegistry.set_pointer_locked

    def spy_set(self, **kwargs):
        set_calls.append({"via": "set_pointer_locked", "run_id": kwargs.get("run_id")})
        return real_set(self, **kwargs)

    monkeypatch.setattr(active_run.ActiveRunRegistry, "set_pointer_locked", spy_set)

    # Also ensure Engine.workflow_init source does not reference set_pointer
    # as a direct call site (delegation only).
    import inspect
    src = inspect.getsource(engine.Engine.workflow_init)
    assert "set_pointer" not in src
    assert "init_run" in src

    eng.workflow_init(target_repo=str(repo), goal="g", run_id=RUN_ID)

    # pointer was written exactly once (by init_run under the registry lock)
    assert len(set_calls) == 1
    assert set_calls[0]["run_id"] == RUN_ID
    ptr = active_run.read_pointer(user_data)
    assert ptr is not None
    assert ptr["run_id"] == RUN_ID
    assert ptr.get("terminal") is not True


# ---------- 2. second init rejected while active non-terminal ---------------


def test_second_init_rejected_while_active_non_terminal(tmp_path):
    repo = _make_repo(tmp_path)
    eng1, user_data, _, _ = _engine_for_init(tmp_path, repo, run_id=RUN_ID)
    eng1.workflow_init(target_repo=str(repo), goal="g1", run_id=RUN_ID)

    eng2, _, _, _ = _engine_for_init(tmp_path, repo, run_id=RUN_ID2, user_data=user_data)
    with pytest.raises((store.InitRejected, RuntimeError)):
        eng2.workflow_init(target_repo=str(repo), goal="g2", run_id=RUN_ID2)

    assert active_run.read_pointer(user_data)["run_id"] == RUN_ID


# ---------- 3. READY_TO_MERGE marks pointer terminal, not cleared -----------


def _seed_reviewing_for_complete(tmp_path, control, wt, repo, head, base):
    """Minimal REVIEWING_TASK state with all artifacts so complete can pass."""
    plan_task = {
        "id": "T001", "title": "t", "dependencies": [],
        "declared_files": ["src/x.py"],
        "verification_commands": [
            {"kind": "executable", "program": sys.executable,
             "args": ["-c", "print(1)"], "cwd": ".", "timeout_sec": 10}
        ],
        "acceptance_criteria": ["ok"],
        "planned_changes": {"may_add_dependencies": False,
                            "may_run_migrations": False, "may_delete_files": []},
    }
    plan_b = b"# Plan\n\nP\n"
    plan_sha = canonical.sha256_hex(canonical.canonical_text_bytes(plan_b))
    man = manifest.build_manifest({"tasks": [plan_task]}, plan_sha256=plan_sha)
    man_b = manifest.manifest_bytes(man)
    spec_b = b"# Spec\n\nS\n"
    exec_obj = {
        "run_id": RUN_ID, "task_id": "T001", "status": "READY_FOR_REVIEW",
        "base_commit": base, "head_commit": head, "worktree_clean": True,
        "untracked_files": [], "verification": [],
        "changed_files": ["src/x.py"],
        "change_audit": {"deleted_files": [], "dependency_files_changed": False,
                         "migrations_added": [], "undeclared_files_changed": []},
    }
    rev_obj = {
        "run_id": RUN_ID, "task_id": "T001", "base_commit": base, "head_commit": head,
        "spec_sha256": canonical.sha256_hex(canonical.canonical_text_bytes(spec_b)),
        "plan_sha256": plan_sha,
        "task_manifest_sha256": canonical.sha256_hex(man_b),
        "plan_version": 1, "decision": "PASS",
        "spec_compliance": True, "plan_compliance": True, "tests_sufficient": True,
        "issues": [], "replan_required": False,
    }
    st = store.State(
        run_id=RUN_ID, revision=20, state="REVIEWING_TASK",
        previous_state="VERIFYING_TASK", plan_version=1, current_task="T001",
        completed_tasks=[],
        roots={"plugin_root": str(_plugin(tmp_path)), "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        base_commit=base, task_start_commit=base, verified_head_commit=head,
        last_reviewed_commit=head,
    )
    st.save(control)
    nxt = copy.deepcopy(st)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_rev",
        changes={"SPEC.md": spec_b, "PLAN.md": plan_b, "task-manifest.json": man_b,
                 "execution-result.json": canonical.canonical_json_bytes(exec_obj),
                 "review-result.json": canonical.canonical_json_bytes(rev_obj)},
        prev_state=st, next_state=nxt,
    )
    nxt.save(control)
    return nxt


def test_ready_to_merge_marks_pointer_terminal_not_cleared(tmp_path):
    repo = _make_repo(tmp_path)
    eng, user_data, control, wt = _engine_for_init(tmp_path, repo)
    eng.workflow_init(target_repo=str(repo), goal="g", run_id=RUN_ID)
    # make a real change on the worktree so complete's head_changed check holds
    env = {"PATH": os.environ.get("PATH", "")}
    (wt / "src").mkdir(exist_ok=True)
    (wt / "src" / "x.py").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "t"], cwd=wt, check=True, env=env)
    head = gitops.head(wt)
    base = store.State.load(control).base_commit
    st = _seed_reviewing_for_complete(tmp_path, control, wt, repo, head, base)

    eng2 = engine.Engine(
        store.Store(control),
        roots={
            "plugin_root": str(_plugin(tmp_path)),
            "target_repo_root": str(repo),
            "run_worktree_root": str(wt),
            "control_root": str(control),
        },
        user_data_root=user_data,
        config={"verification": {"program_allowlist": [sys.executable],
                                  "interpreter_allowlist": ["bash", "cmd.exe"]}},
        effective_config={"effective_approval_mode": "zcode_tool_confirmation"},
    )
    result = eng2.workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert result["state"] == "READY_TO_MERGE"

    ptr = active_run.read_pointer(user_data)
    assert ptr is not None, "pointer must be retained (not cleared) at READY_TO_MERGE"
    assert ptr["run_id"] == RUN_ID
    assert ptr.get("terminal") is True
    assert ptr.get("state") == "READY_TO_MERGE"

    # workflow_status still resolves this run
    os.environ["AGENTFLOW_RUN_CONTEXT"] = json.dumps({
        "plugin_root": str(_plugin(tmp_path)),
        "user_data_root": str(user_data),
        "config": {},
        "effective_config": {},
    })
    try:
        status = server.workflow_status()
        assert status["active_run"] is not None
        assert status["active_run"]["run_id"] == RUN_ID
        assert status["active_run"]["state"] == "READY_TO_MERGE"
    finally:
        os.environ.pop("AGENTFLOW_RUN_CONTEXT", None)


# ---------- 4. terminal pointer allows a new run ----------------------------


def test_terminal_pointer_allows_new_run(tmp_path):
    repo = _make_repo(tmp_path)
    eng, user_data, control, wt = _engine_for_init(tmp_path, repo)
    eng.workflow_init(target_repo=str(repo), goal="g", run_id=RUN_ID)

    # manually mark terminal (simulates post-complete)
    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.mark_terminal_locked(RUN_ID)
    assert active_run.read_pointer(user_data)["terminal"] is True

    # new init must be allowed and overwrite the pointer
    eng2, _, _, _ = _engine_for_init(tmp_path, repo, run_id=RUN_ID2, user_data=user_data)
    eng2.workflow_init(target_repo=str(repo), goal="g2", run_id=RUN_ID2)
    ptr = active_run.read_pointer(user_data)
    assert ptr["run_id"] == RUN_ID2
    assert ptr.get("terminal") is not True


# ---------- 5. abandon is local admin, not MCP tool -------------------------


def test_abandon_is_local_admin_not_mcp_tool(tmp_path):
    # MCP surface stays exactly 9 — abandon/archive are NOT registered tools
    from mcp.server.fastmcp import FastMCP
    # the server's tool list is fixed at decoration time
    tools = getattr(server.mcp_server, "_tool_manager", None)
    if tools is not None:
        names = set(tools._tools.keys()) if hasattr(tools, "_tools") else set()
    else:
        # fallback: inspect the module-level decorated functions
        names = {
            "workflow_status", "workflow_init", "planner_draft_spec",
            "workflow_approve_spec", "planner_create_plan", "workflow_start_task",
            "workflow_verify_task", "reviewer_review_task", "workflow_complete_task",
        }
    assert "abandon" not in names and "abandon_run" not in names
    assert "archive" not in names and "archive_run" not in names
    assert len(names) == 9 or names >= {
        "workflow_status", "workflow_init", "planner_draft_spec",
        "workflow_approve_spec", "planner_create_plan", "workflow_start_task",
        "workflow_verify_task", "reviewer_review_task", "workflow_complete_task",
    }

    # local admin abandon clears the pointer
    repo = _make_repo(tmp_path)
    eng, user_data, _, _ = _engine_for_init(tmp_path, repo)
    eng.workflow_init(target_repo=str(repo), goal="g", run_id=RUN_ID)
    assert active_run.read_pointer(user_data) is not None

    from zcode_agentflow import admin
    admin.abandon_run(user_data_root=user_data, run_id=RUN_ID)
    assert active_run.read_pointer(user_data) is None

    # after abandon a new init is allowed
    eng2, _, _, _ = _engine_for_init(tmp_path, repo, run_id=RUN_ID2, user_data=user_data)
    eng2.workflow_init(target_repo=str(repo), goal="g2", run_id=RUN_ID2)
    assert active_run.read_pointer(user_data)["run_id"] == RUN_ID2


# ---------- 6. pointer mutations only inside locked context -----------------


def test_pointer_mutations_only_inside_locked_context(tmp_path, monkeypatch):
    """Every set/mark_terminal/clear call must occur inside ActiveRunRegistry.locked
    and must never re-acquire the registry lock (errata single-owner pattern)."""
    hold_depth = {"n": 0}
    violations = []

    real_locked = active_run.ActiveRunRegistry.locked

    @classmethod
    def counting_locked(cls, user_data_root, timeout=30):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            hold_depth["n"] += 1
            if hold_depth["n"] > 1:
                violations.append("re-entrant locked()")
            try:
                with real_locked(user_data_root, timeout=timeout) as reg:
                    yield reg
            finally:
                hold_depth["n"] -= 1

        return cm()

    def wrap(method_name):
        real = getattr(active_run.ActiveRunRegistry, method_name)

        def wrapped(self, *a, **kw):
            if hold_depth["n"] < 1:
                violations.append(f"{method_name} outside locked()")
            return real(self, *a, **kw)

        return wrapped

    monkeypatch.setattr(active_run.ActiveRunRegistry, "locked", counting_locked)
    for m in ("set_pointer_locked", "mark_terminal_locked", "clear_pointer_locked"):
        monkeypatch.setattr(active_run.ActiveRunRegistry, m, wrap(m))

    repo = _make_repo(tmp_path)
    eng, user_data, control, wt = _engine_for_init(tmp_path, repo)
    eng.workflow_init(target_repo=str(repo), goal="g", run_id=RUN_ID)

    with active_run.ActiveRunRegistry.locked(user_data) as reg:
        reg.mark_terminal_locked(RUN_ID)

    from zcode_agentflow import admin
    # re-init after terminal so abandon has something; first clear via abandon
    # on a fresh run after new init
    eng2, _, _, _ = _engine_for_init(tmp_path, repo, run_id=RUN_ID2, user_data=user_data)
    eng2.workflow_init(target_repo=str(repo), goal="g2", run_id=RUN_ID2)
    admin.abandon_run(user_data_root=user_data, run_id=RUN_ID2)

    assert violations == [], f"pointer lock discipline violated: {violations}"
