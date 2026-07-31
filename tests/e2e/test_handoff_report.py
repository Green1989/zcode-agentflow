"""Task 8.4 — READY_TO_MERGE handoff report (Prompt §22; Review §六.10).

When ``workflow_complete_task`` reaches READY_TO_MERGE, its result must carry a
structured **handoff** the supervisor presents to the user: branch, head/base
commits, changed files, verification results, review result, SPEC/PLAN sha256.
The handoff is a side-effect-free read of state + artifacts; AgentFlow performs
NO push, NO merge, NO PR anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest

from zcode_agentflow import artifacts, engine, gitops, store
from zcode_agentflow.adapters.base import AdapterResult
from zcode_agentflow.handoff import build_handoff, HANDOFF_FIELDS

RUN_ID = "20260730120000-4ad0ff"


# ---------- fake adapters (reuse the Layer-1 shape) -------------------------


@dataclass
class FakeCodex:
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append(request.get("kind"))
        if request["kind"] == "draft_spec":
            raw = {
                "goal": "handoff e2e",
                "spec_markdown": "# Spec\n\nEdit a.txt.\n",
                "questions": [],
                "assumptions": [],
            }
        else:
            raw = {
                "status": "READY_FOR_EXECUTION",
                "plan": {
                    "plan_markdown": "# Plan\n\nT001: edit a.txt.\n",
                    "tasks": [{
                        "id": "T001", "title": "edit a.txt", "dependencies": [],
                        "declared_files": ["a.txt"],
                        "verification_commands": [
                            {"kind": "executable", "program": sys.executable,
                             "args": ["-c", "print('ok')"], "cwd": ".",
                             "timeout_sec": 30}],
                        "acceptance_criteria": ["a.txt edited"],
                        "planned_changes": {"may_add_dependencies": False,
                                            "may_run_migrations": False,
                                            "may_delete_files": []},
                    }],
                },
            }
        return AdapterResult(exit_code=0, stdout="", stderr="", artifacts={},
                             parsed=raw)


@dataclass
class FakeGrok:
    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        return AdapterResult(
            exit_code=0, stdout="", stderr="", artifacts={},
            parsed={"decision": "PASS", "spec_compliance": True,
                    "plan_compliance": True, "tests_sufficient": True,
                    "issues": [], "replan_required": False},
        )


# ---------- harness ---------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "h@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "H"], cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir()
    (pr / "AGENTS.md.template").write_bytes(
        (Path(__file__).resolve().parents[2] / "AGENTS.md.template").read_bytes()
    )
    return pr


def _engine(control, repo, wt, user_data, plugin):
    return engine.Engine(
        store.Store(control),
        roots={"plugin_root": str(plugin), "target_repo_root": str(repo),
               "run_worktree_root": str(wt), "control_root": str(control)},
        config={"verification": {"program_allowlist": [sys.executable],
                                 "interpreter_allowlist": ["bash", "cmd.exe"]}},
        effective_config={"effective_approval_mode": "zcode_tool_confirmation",
                          "confirmation_configured": True},
        user_data_root=user_data,
        adapters={"codex": FakeCodex(), "grok": FakeGrok()},
    )


def _drive_to_ready_to_merge(tmp_path):
    repo = _make_repo(tmp_path)
    plugin = _plugin(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    control = user_data / "runs" / RUN_ID / "control"
    wt = user_data / "runs" / RUN_ID / "worktree"
    control.mkdir(parents=True, exist_ok=True)
    wt.parent.mkdir(parents=True, exist_ok=True)

    e = _engine(control, repo, wt, user_data, plugin)
    e.workflow_init(target_repo=str(repo), goal="handoff e2e", run_id=RUN_ID)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    e.planner_draft_spec(goal="handoff e2e", expected_revision=st.revision)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    e.workflow_approve_spec(expected_revision=st.revision)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    e.planner_create_plan(approved_spec_sha256=st.spec_sha256, expected_revision=st.revision)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    e.workflow_start_task(task_id="T001", expected_revision=st.revision)
    # executor's real work
    env = {"PATH": os.environ.get("PATH", "")}
    (Path(wt) / "a.txt").write_text("edited\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "task"], cwd=wt, check=True, env=env)
    head = gitops.head(wt)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    e.workflow_verify_task(task_id="T001", expected_revision=st.revision)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    e.reviewer_review_task(task_id="T001", expected_revision=st.revision)
    st = store.State.load(control)
    e = _engine(control, repo, wt, user_data, plugin)
    complete = e.workflow_complete_task(task_id="T001", expected_revision=st.revision)
    return complete, control, repo, wt, head, plugin


# ============================================================================
# 1. complete emits a handoff with all required fields — and no push/merge
# ============================================================================


def test_complete_emits_handoff_with_no_push(tmp_path, monkeypatch):
    # fail loudly if ANY push/merge/remote git op is attempted during complete
    dangerous: list[str] = []
    real_run = subprocess.run

    def guarding_run(args, *a, **kw):
        flat = " ".join(str(x) for x in args)
        if any(d in flat for d in ("push", "merge", "pull", "rebase",
                                   "remote", "gh ", "create_pull")):
            # only flag git remote-ish ops, not e.g. "worktree"
            head = str(args[0])
            if head.endswith("git") or head.endswith("gh"):
                dangerous.append(flat)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarding_run)

    complete, control, repo, wt, head, plugin = _drive_to_ready_to_merge(tmp_path)

    # the complete result carries a structured handoff
    assert complete["state"] == "READY_TO_MERGE"
    handoff = complete["handoff"]
    # every required field is present and non-empty
    for field_name in HANDOFF_FIELDS:
        assert field_name in handoff, f"handoff missing {field_name}"
        assert handoff[field_name] not in (None, "", []), (
            f"handoff field {field_name} is empty"
        )
    # the handoff is accurate against real state
    st = store.State.load(control)
    assert handoff["base_commit"] == st.base_commit
    assert handoff["head_commit"] == head
    assert handoff["branch"] == f"agentflow/{RUN_ID}"
    assert handoff["changed_files"] == ["a.txt"]
    assert handoff["review_result"]["decision"] == "PASS"
    assert handoff["spec_sha256"] == st.spec_sha256
    assert handoff["plan_sha256"] == st.plan_sha256
    # test_results come from the execution-result verification list
    assert isinstance(handoff["test_results"], list)
    # the handoff explicitly declares no push/merge/PR was performed
    assert handoff["pushed"] is False
    assert handoff["merged"] is False
    assert handoff["pr_created"] is False

    # and critically: NO push/merge/remote git op was actually invoked
    assert dangerous == [], f"complete attempted a remote git op: {dangerous}"


# ============================================================================
# 2. build_handoff is a side-effect-free read (no state mutation)
# ============================================================================


def test_build_handoff_is_read_only(tmp_path):
    complete, control, repo, wt, head, plugin = _drive_to_ready_to_merge(tmp_path)
    st_before = store.State.load(control)
    before = json.dumps(st_before.__dict__, default=str, sort_keys=True)

    # reuse the SAME plugin dir the run used (build_handoff only reads roots)
    e = _engine(control, repo, wt, tmp_path / "ud2", plugin)
    handoff = build_handoff(e)

    # the read produced the same authoritative snapshot...
    assert handoff["head_commit"] == head
    assert handoff["state"] == "READY_TO_MERGE"
    # ...and mutated nothing on disk
    st_after = store.State.load(control)
    after = json.dumps(st_after.__dict__, default=str, sort_keys=True)
    assert before == after
    assert st_after.revision == st_before.revision
