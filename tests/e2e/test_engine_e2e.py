"""Task 8.1 — Layer 1 Engine E2E (Prompt §21).

This is the *code-execution* proof of the v1 vertical slice: a run driven by
the real Engine against a REAL git repo + REAL worktree, with REAL file edits
and REAL git commits between start_task and verify_task. The Codex/Grok
adapters are fakes (Layer 3 exercises the real CLIs), but everything else —
state.json, artifact generations, the two-phase transactions, the rehash hard
checks, the active-run pointer — is the real implementation.

Happy path drives NEW → READY_TO_MERGE end to end; the rejection matrix then
proves each integrity gate fires and routes to NEEDS_HUMAN (never silent).

Implementation note: there is NO new production code in this task — it is a
test of the already-implemented engine (Phases 1–7). The shared ``e2e_run``
helper is the [PSEUDO] from the plan: it injects fake adapters and exposes the
per-step ``expected_revision`` so each tool call satisfies the Store CAS.
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
    active_run,
    artifacts,
    bootstrap,
    engine,
    gitops,
    manifest,
    store,
)
from zcode_agentflow.adapters.base import AdapterResult

RUN_ID = "20260730120000-e201aa"


# ============================================================================
# Fake adapters — scripted RAW outputs (Layer 1 isolates the engine from CLIs)
# ============================================================================


@dataclass
class FakeCodex:
    """Returns RAW draft/plan outputs. ``plan_status`` lets a test drive a
    BLOCKED wrapper (multi-task goal) without touching a real CLI."""

    goal: str = "e2e change"
    plan_status: str = "READY_FOR_EXECUTION"
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        kind = request["kind"]
        self.calls.append({"kind": kind, "repo_cwd": str(repo_cwd)})
        if kind == "draft_spec":
            raw = {
                "goal": self.goal,
                "spec_markdown": "# Spec\n\nE2E: add a.txt line.\n",
                "questions": [],
                "assumptions": [
                    {"id": "A1", "assumption": "single file", "risk": "low",
                     "default": "proceed"},
                ],
            }
        elif kind == "create_plan":
            if self.plan_status == "BLOCKED":
                return AdapterResult(
                    exit_code=0, stdout="", stderr="", artifacts={},
                    parsed={"status": "BLOCKED",
                            "block_reason": "multi-task goal — v1 supports one task",
                            "plan": None, "questions": []},
                )
            raw = {
                "status": "READY_FOR_EXECUTION",
                "plan": {
                    "plan_markdown": "# Plan\n\nSingle task T001: edit a.txt.\n",
                    "tasks": [
                        {
                            "id": "T001",
                            "title": "edit a.txt",
                            "dependencies": [],
                            "declared_files": ["a.txt"],
                            "verification_commands": [
                                {"kind": "executable", "program": sys.executable,
                                 "args": ["-c", "print('ok')"], "cwd": ".",
                                 "timeout_sec": 30},
                            ],
                            "acceptance_criteria": ["a.txt edited", "verify exits 0"],
                            "planned_changes": {
                                "may_add_dependencies": False,
                                "may_run_migrations": False,
                                "may_delete_files": [],
                            },
                        }
                    ],
                },
                # R2-1: strict-mode envelope — all keys always present.
                "questions": [],
                "block_reason": "",
            }
        else:
            raise RuntimeError(f"unknown codex kind {kind!r}")
        return AdapterResult(exit_code=0, stdout="", stderr="", artifacts={},
                             parsed=copy.deepcopy(raw))


@dataclass
class FakeGrok:
    """Returns a scripted review decision (PASS by default; FAIL on demand)."""

    decision: dict = field(default_factory=lambda: {
        "decision": "PASS", "spec_compliance": True, "plan_compliance": True,
        "tests_sufficient": True, "issues": [], "replan_required": False,
    })
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append({"bundle_dir": request.get("bundle_dir")})
        return AdapterResult(exit_code=0, stdout="", stderr="", artifacts={},
                             parsed=copy.deepcopy(self.decision))


# ============================================================================
# Shared e2e_run harness ([PSEUDO] from the plan)
# ============================================================================


def _make_real_repo(tmp_path: Path) -> Path:
    """A real minimal git repo with one committed file."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "e2e@example.com"],
                   cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "E2E"], cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin_root(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir(exist_ok=True)
    template = Path(__file__).resolve().parents[2] / "AGENTS.md.template"
    (pr / "AGENTS.md.template").write_bytes(template.read_bytes())
    return pr


class E2ERun:
    """A fully-wired in-process run against a real repo + real worktree.

    Holds the Engine, the fake adapters (so a test can script BLOCKED/FAIL),
    and convenience accessors for control_root / worktree. The Engine is
    rebuilt per call via :meth:`engine` so a test can simulate a process
    restart (new Engine, same control_root + pointer) and still reconnect.
    """

    def __init__(self, tmp_path: Path, *, goal="e2e change",
                 effective_config=None, plan_status="READY_FOR_EXECUTION"):
        self.tmp = tmp_path
        self.goal = goal
        self.repo = _make_real_repo(tmp_path)
        self.plugin = _plugin_root(tmp_path)
        self.user_data = tmp_path / "user_data"
        self.user_data.mkdir(parents=True, exist_ok=True)
        self.control = self.user_data / "runs" / RUN_ID / "control"
        self.worktree = self.user_data / "runs" / RUN_ID / "worktree"
        self.control.mkdir(parents=True, exist_ok=True)
        self.worktree.parent.mkdir(parents=True, exist_ok=True)

        self.codex = FakeCodex(goal=goal, plan_status=plan_status)
        self.grok = FakeGrok()
        self._effective = effective_config or {
            "effective_approval_mode": "zcode_tool_confirmation",
            "confirmation_configured": True,
        }
        self._config = {
            "verification": {
                "program_allowlist": [sys.executable, "python"],
                "interpreter_allowlist": ["cmd.exe", "bash"],
            },
        }

    def engine(self):
        """A fresh Engine bound to this run's roots + fake adapters.

        Constructing a new Engine mirrors what a NEW MCP process does on
        resume: it reads state.json and reconnects — no in-memory coupling to
        a previous Engine instance."""
        return engine.Engine(
            store.Store(self.control),
            roots={
                "plugin_root": str(self.plugin),
                "target_repo_root": str(self.repo),
                "run_worktree_root": str(self.worktree),
                "control_root": str(self.control),
            },
            config=self._config,
            effective_config=self._effective,
            user_data_root=self.user_data,
            adapters={"codex": self.codex, "grok": self.grok},
        )

    # ---- low-level convenience ------------------------------------------

    def state(self):
        return store.State.load(self.control)

    def commit_in_worktree(self, path, content, msg="task change"):
        """The executor's real work: edit + git add + git commit in the worktree."""
        env = {"PATH": os.environ.get("PATH", "")}
        p = Path(self.worktree) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.worktree, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", msg],
                       cwd=self.worktree, check=True, env=env)
        return gitops.head(self.worktree)


def _drive_happy_path(run: E2ERun):
    """Drive NEW → READY_TO_MERGE on ``run`` via real tool calls.

    Performs the real git commit between start_task and verify_task. Returns
    the final complete-task result dict."""
    eng = run.engine()
    # 1. init
    init = eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    assert init["run_id"] == RUN_ID
    assert gitops.is_git_repo(run.worktree)

    # 2. draft (no questions => READY_FOR_SPEC_APPROVAL)
    st = run.state()
    eng2 = run.engine()
    draft = eng2.planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    assert draft["status"] == "READY_FOR_SPEC_APPROVAL"

    # 3. approve (HUMAN GATE 1)
    st = run.state()
    eng3 = run.engine()
    approve = eng3.workflow_approve_spec(expected_revision=st.revision)
    assert approve["state"] == "PLANNING"

    # 4. create_plan (single task)
    st = run.state()
    eng4 = run.engine()
    plan = eng4.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                                    expected_revision=st.revision)
    assert plan["status"] == "READY_FOR_EXECUTION"

    # 5. start_task
    st = run.state()
    eng5 = run.engine()
    start = eng5.workflow_start_task(task_id="T001", expected_revision=st.revision)
    task_start = start["task_start_commit"]

    # --- the executor does REAL work: edit a.txt, git add, git commit ---
    new_head = run.commit_in_worktree("a.txt", "edited by executor\n", msg="task: edit a.txt")
    assert new_head != task_start
    assert gitops.is_clean(run.worktree)

    # 6. verify
    st = run.state()
    eng6 = run.engine()
    verify = eng6.workflow_verify_task(task_id="T001", expected_revision=st.revision)
    assert verify["status"] == "READY_FOR_REVIEW"

    # 7. review (Grok PASS)
    st = run.state()
    eng7 = run.engine()
    review = eng7.reviewer_review_task(task_id="T001", expected_revision=st.revision)
    assert review["decision"] == "PASS"

    # 8. complete (HUMAN GATE 2 implied by reaching READY_TO_MERGE)
    st = run.state()
    eng8 = run.engine()
    complete = eng8.workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert complete["state"] == "READY_TO_MERGE"
    return complete, new_head, task_start


# ============================================================================
# 1. Happy path: real edit + real commit reaches READY_TO_MERGE
# ============================================================================


def test_happy_path_edits_commits_and_merges_state(tmp_path):
    run = E2ERun(tmp_path)
    complete, new_head, task_start = _drive_happy_path(run)

    st = run.state()
    assert st.state == "READY_TO_MERGE"
    # the head really advanced past the task-start commit
    assert new_head != task_start
    assert st.verified_head_commit == new_head
    assert st.last_reviewed_commit == new_head
    # changed_files == ["a.txt"] (the declared, authorized change)
    resolver = artifacts.ArtifactResolver(run.control, st)
    exec_obj = json.loads(resolver.read("execution-result.json"))
    assert exec_obj["changed_files"] == ["a.txt"]
    assert exec_obj["worktree_clean"] is True
    # bundle is complete: all five artifacts present and rehash to their locked shas
    artifacts.verify_locked_artifacts(st, resolver)
    # review bound HEAD to the real commit
    assert st.last_reviewed_commit == gitops.head(run.worktree)
    # worktree clean at completion
    assert gitops.is_clean(run.worktree)
    # active-run pointer retained + marked terminal (NOT cleared at READY_TO_MERGE)
    ptr = active_run.read_pointer(run.user_data)
    assert ptr is not None
    assert ptr["run_id"] == RUN_ID
    assert ptr.get("terminal") is True
    assert ptr.get("state") == "READY_TO_MERGE"


# ============================================================================
# 2. complete rejects a stale review commit (HEAD advanced past reviewed commit)
# ============================================================================


def test_stale_review_rejected_at_complete(tmp_path):
    run = E2ERun(tmp_path)
    # drive to REVIEWING_TASK(PASS) but do NOT complete yet
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_approve_spec(expected_revision=st.revision)
    st = run.state(); run.engine().planner_create_plan(approved_spec_sha256=st.spec_sha256, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_start_task(task_id="T001", expected_revision=st.revision)
    run.commit_in_worktree("a.txt", "edited\n", msg="task")
    st = run.state(); run.engine().workflow_verify_task(task_id="T001", expected_revision=st.revision)
    st = run.state(); run.engine().reviewer_review_task(task_id="T001", expected_revision=st.revision)

    # now advance HEAD PAST the reviewed commit (a stray post-review commit)
    run.commit_in_worktree("a.txt", "edited again post-review\n", msg="stray")

    st = run.state()
    with pytest.raises(Exception):
        run.engine().workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert run.state().state == "NEEDS_HUMAN"


# ============================================================================
# 3. complete rejects a dirty / untracked worktree
# ============================================================================


def test_dirty_worktree_rejected_at_complete(tmp_path):
    run = E2ERun(tmp_path)
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_approve_spec(expected_revision=st.revision)
    st = run.state(); run.engine().planner_create_plan(approved_spec_sha256=st.spec_sha256, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_start_task(task_id="T001", expected_revision=st.revision)
    run.commit_in_worktree("a.txt", "edited\n", msg="task")
    st = run.state(); run.engine().workflow_verify_task(task_id="T001", expected_revision=st.revision)
    st = run.state(); run.engine().reviewer_review_task(task_id="T001", expected_revision=st.revision)

    # leave an UNTRACKED file (worktree no longer clean)
    (Path(run.worktree) / "stray.txt").write_text("oops\n", encoding="utf-8")

    st = run.state()
    with pytest.raises(Exception):
        run.engine().workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert run.state().state == "NEEDS_HUMAN"


# ============================================================================
# 4. multi-task plan routes BLOCKED (P0-5) — never silent tasks[0]
# ============================================================================


def test_multi_task_plan_routes_blocked(tmp_path):
    run = E2ERun(tmp_path, plan_status="BLOCKED")
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_approve_spec(expected_revision=st.revision)

    st = run.state()
    result = run.engine().planner_create_plan(
        approved_spec_sha256=st.spec_sha256, expected_revision=st.revision,
    )
    # the BLOCKED wrapper is surfaced — the run routes to NEEDS_HUMAN, NOT to
    # READY_FOR_EXECUTION with a silently-truncated single task.
    assert result["status"] == "BLOCKED"
    assert run.state().state == "NEEDS_HUMAN"
    # no task was committed (current_task stays empty — never a silent tasks[0])
    assert not run.state().current_task


# ============================================================================
# 5. illegal-state call rejected (wrong source state for the tool)
# ============================================================================


def test_illegal_state_call_rejected(tmp_path):
    run = E2ERun(tmp_path)
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state()
    # complete_task is illegal from INITIALIZING_WORKTREE (no task reviewed)
    with pytest.raises(store.TransitionNotAllowed):
        run.engine().workflow_complete_task(task_id="T001", expected_revision=st.revision)
    # the illegal call did NOT strand the run in an intermediate state
    assert run.state().state == "INITIALIZING_WORKTREE"


# ============================================================================
# 6. recovery matrix on restart (pointer resumes at several points)
# ============================================================================


def test_recovery_matrix_on_restart(tmp_path):
    run = E2ERun(tmp_path)

    # (a) restart right after init: a fresh process resumes the active run via
    #     the pointer (Task 1.3a primitives + Task 3.8 recovery).
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    paths = bootstrap.resume_active_run(run.user_data)
    assert paths is not None
    assert paths.run_id == RUN_ID
    assert paths.control_root == run.control
    assert run.state().state == "INITIALIZING_WORKTREE"

    # continue to READY_FOR_SPEC_APPROVAL then restart again
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    assert run.state().state == "READY_FOR_SPEC_APPROVAL"
    paths = bootstrap.resume_active_run(run.user_data)
    assert paths is not None and paths.run_id == RUN_ID
    assert run.state().state == "READY_FOR_SPEC_APPROVAL"

    # continue to EXECUTING_TASK, then restart mid-task: the active-run pointer
    # still points at this run, and recovery leaves the in-flight state intact.
    st = run.state(); run.engine().workflow_approve_spec(expected_revision=st.revision)
    st = run.state(); run.engine().planner_create_plan(approved_spec_sha256=st.spec_sha256, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_start_task(task_id="T001", expected_revision=st.revision)
    paths = bootstrap.resume_active_run(run.user_data)
    assert paths is not None
    assert run.state().state == "EXECUTING_TASK"
    assert run.state().current_task == "T001"

    # finishing the run after a restart still reaches READY_TO_MERGE — the
    # pointer-driven resume reconnects to the SAME run, not a new one.
    run.commit_in_worktree("a.txt", "post-restart edit\n", msg="task")
    st = run.state(); run.engine().workflow_verify_task(task_id="T001", expected_revision=st.revision)
    st = run.state(); run.engine().reviewer_review_task(task_id="T001", expected_revision=st.revision)
    st = run.state()
    complete = run.engine().workflow_complete_task(task_id="T001", expected_revision=st.revision)
    assert complete["state"] == "READY_TO_MERGE"
    # terminal pointer retained after restart-driven completion
    ptr = active_run.read_pointer(run.user_data)
    assert ptr is not None and ptr.get("terminal") is True


# ============================================================================
# 7. approval strong path: confirmation supported + configured => strong record
# ============================================================================


def test_approval_strong_path(tmp_path):
    run = E2ERun(tmp_path, effective_config={
        "effective_approval_mode": "zcode_tool_confirmation",
        "tool_confirmation_supported": True,
        "confirmation_configured": True,
    })
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    st = run.state()
    approve = run.engine().workflow_approve_spec(expected_revision=st.revision)
    # strong record: method is the strong tool-confirmation, NO security_warning
    assert approve["approval_method"] == "zcode_tool_confirmation"
    # the persisted approval record carries the strong method
    ap_dir = run.control / "approvals"
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in ap_dir.glob("*.json")]
    assert recs and recs[0]["approval_method"] == "zcode_tool_confirmation"
    assert "security_warning" not in recs[0]


# ============================================================================
# 8. approval fallback path: supported=false, fallback=true => warning;
#    fallback=false => NEEDS_HUMAN
# ============================================================================


def test_approval_fallback_path(tmp_path):
    # (a) tool-confirmation NOT configured + fallback allowed => FALLBACK record
    #     WITH a security_warning.
    run = E2ERun(tmp_path, effective_config={
        "effective_approval_mode": "prefer_zcode_tool_confirmation",
        "tool_confirmation_supported": True,
        "confirmation_configured": False,
        "allow_non_adversarial_fallback": True,
    })
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    st = run.state()
    approve = run.engine().workflow_approve_spec(expected_revision=st.revision)
    assert approve["approval_method"] == "non_adversarial_convention"
    recs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (run.control / "approvals").glob("*.json")]
    assert recs and "security_warning" in recs[0]
    assert recs[0]["security_warning"]

    # (b) tool-confirmation NOT configured + fallback=false => the approval is
    #     REFUSED (ApprovalUnavailableError), routing to NEEDS_HUMAN — never a
    #     silent weak approval.
    nofb = tmp_path / "nofb"
    nofb.mkdir(parents=True, exist_ok=True)
    run2 = E2ERun(nofb, effective_config={
        "effective_approval_mode": "prefer_zcode_tool_confirmation",
        "tool_confirmation_supported": True,
        "confirmation_configured": False,
        "allow_non_adversarial_fallback": False,
    })
    eng2 = run2.engine()
    eng2.workflow_init(target_repo=str(run2.repo), goal=run2.goal,
                       run_id="20260730120000-e201fb")
    st2 = run2.state(); run2.engine().planner_draft_spec(goal=run2.goal, expected_revision=st2.revision)
    st2 = run2.state()
    from zcode_agentflow.approval import ApprovalUnavailableError
    with pytest.raises(ApprovalUnavailableError):
        run2.engine().workflow_approve_spec(expected_revision=st2.revision)
    assert run2.state().state == "NEEDS_HUMAN"


# ============================================================================
# AF-6 (audit-fix) — completion of the Layer-1 rejection matrix (Prompt §21).
# The Phase 8 prompt requires: stale revision, artifact mutation, dirty/untracked,
# verification failure, unauthorized change, stale/mismatched review, restart.
# Stale review / dirty / restart / multi-task were already covered above; these
# add the remaining paths so the matrix is complete.
# ============================================================================


@dataclass
class FailingVerifyCodex:
    """A FakeCodex whose plan's verification command EXITS NON-ZERO, so
    workflow_verify_task routes to NEEDS_HUMAN (VERIFICATION_FAILED)."""

    goal: str = "e2e change"
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        kind = request["kind"]
        self.calls.append({"kind": kind})
        if kind == "draft_spec":
            raw = {
                "goal": self.goal,
                "spec_markdown": "# Spec\n\nFailing-verify case.\n",
                "questions": [],
                "assumptions": [],
            }
        elif kind == "create_plan":
            raw = {
                "status": "READY_FOR_EXECUTION",
                "plan": {
                    "plan_markdown": "# Plan\n\nT001 (verify fails).\n",
                    "tasks": [{
                        "id": "T001", "title": "failing verify", "dependencies": [],
                        "declared_files": ["a.txt"],
                        # a verification command that exits non-zero.
                        "verification_commands": [
                            {"kind": "executable", "program": sys.executable,
                             "args": ["-c", "import sys; sys.exit(2)"], "cwd": ".",
                             "timeout_sec": 30},
                        ],
                        "acceptance_criteria": ["verify fails"],
                        "planned_changes": {
                            "may_add_dependencies": False,
                            "may_run_migrations": False,
                            "may_delete_files": [],
                        },
                    }],
                },
            }
        else:
            raise RuntimeError(f"unknown codex kind {kind!r}")
        return AdapterResult(exit_code=0, stdout="", stderr="", artifacts={},
                             parsed=copy.deepcopy(raw))


def _drive_to_executing(run: E2ERun):
    """Drive NEW → EXECUTING_TASK (so verify-side rejection tests start there)."""
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state(); run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_approve_spec(expected_revision=st.revision)
    st = run.state(); run.engine().planner_create_plan(approved_spec_sha256=st.spec_sha256, expected_revision=st.revision)
    st = run.state(); run.engine().workflow_start_task(task_id="T001", expected_revision=st.revision)
    return run.state()


def test_stale_revision_cas_rejected(tmp_path):
    """A state-changing call with a STALE expected_revision (CAS mismatch) is
    rejected with store.CasMismatch — never silently applies to a moved state.
    (Prompt §21 'stale revision'.)"""
    run = E2ERun(tmp_path)
    eng = run.engine()
    eng.workflow_init(target_repo=str(run.repo), goal=run.goal, run_id=RUN_ID)
    st = run.state()
    stale_rev = st.revision
    # advance the revision so stale_rev no longer matches.
    run.engine().planner_draft_spec(goal=run.goal, expected_revision=st.revision)
    # now call with the STALE revision -> CasMismatch.
    with pytest.raises(store.CasMismatch):
        run.engine().workflow_approve_spec(expected_revision=stale_rev)


def test_verification_failure_routes_to_needs_human(tmp_path):
    """A verification command that exits non-zero => VERIFICATION_FAILED =>
    NEEDS_HUMAN (never a silent pass to review). (Prompt §21 'verification
    failure'.)"""
    run = E2ERun(tmp_path)
    run.codex = FailingVerifyCodex(goal=run.goal)
    _drive_to_executing(run)
    # the executor commits a change (clean worktree); the verification COMMAND
    # itself fails, so status must be VERIFICATION_FAILED -> NEEDS_HUMAN.
    run.commit_in_worktree("a.txt", "edited\n", msg="task")
    st = run.state()
    res = run.engine().workflow_verify_task(task_id="T001", expected_revision=st.revision)
    assert res["status"] == "VERIFICATION_FAILED"
    assert run.state().state == "NEEDS_HUMAN"


def test_unauthorized_change_routes_to_needs_human(tmp_path):
    """Committing a file NOT in declared_files (['a.txt']) => the change audit
    flags an undeclared change => VERIFICATION_FAILED => NEEDS_HUMAN (never a
    silent pass). (Prompt §21 'unauthorized change'.)"""
    run = E2ERun(tmp_path)
    _drive_to_executing(run)
    # commit an UNDECLARED file (b.txt is not in declared_files=['a.txt']).
    run.commit_in_worktree("b.txt", "sneaky undeclared change\n", msg="undeclared")
    st = run.state()
    res = run.engine().workflow_verify_task(task_id="T001", expected_revision=st.revision)
    assert res["status"] == "VERIFICATION_FAILED"
    assert run.state().state == "NEEDS_HUMAN"
    # the execution-result records the undeclared change honestly.
    resolver = artifacts.ArtifactResolver(run.control, run.state())
    exec_result = json.loads(resolver.read("execution-result.json"))
    assert exec_result["change_audit"]["undeclared_files_changed"]


def test_artifact_mutation_detected_at_complete(tmp_path):
    """Tampering with a locked artifact's bytes between verify and complete is
    caught by the on-disk rehash hard checks (Prompt §14 / Review §五) and the
    complete is refused — never a silent completion. (Prompt §21 'artifact
    mutation'.)

    The detection surfaces as a WorkflowError whose reasons carry the
    manifest-vs-on-disk sha mismatch evidence (the run is NOT completed)."""
    run = E2ERun(tmp_path)
    # happy path up to REVIEWING_TASK(PASS).
    _drive_to_executing(run)
    run.commit_in_worktree("a.txt", "edited\n", msg="task")
    st = run.state(); run.engine().workflow_verify_task(task_id="T001", expected_revision=st.revision)
    st = run.state(); run.engine().reviewer_review_task(task_id="T001", expected_revision=st.revision)
    assert run.state().state == "REVIEWING_TASK"
    # mutate a locked blob's bytes on disk (the execution-result blob).
    gen = artifacts.load_generation(run.control, run.state().artifact_generation)
    entry = gen.index.get("execution-result.json")
    assert entry, "execution-result.json must be a locked artifact"
    blob = run.control / entry["blob"]
    assert blob.exists()
    original = blob.read_bytes()
    blob.write_bytes(original + b"\n# TAMPERED\n")  # mutate the authoritative bytes
    # complete_task's hard-check set rehashes the locked artifacts and detects the
    # mismatch; the complete is refused (never silently completes to READY_TO_MERGE).
    from zcode_agentflow.engine import WorkflowError
    st = run.state()
    with pytest.raises(WorkflowError) as exc_info:
        run.engine().workflow_complete_task(task_id="T001", expected_revision=st.revision)
    msg = str(exc_info.value)
    assert "mismatch" in msg or "tamper" in msg.lower(), (
        f"the artifact mutation must be reported as a sha mismatch; got: {msg}"
    )
    # the run was NOT completed to READY_TO_MERGE (the tamper blocked it).
    assert run.state().state != "READY_TO_MERGE"
    # restore the bytes so the test does not leave the run half-tampered for any
    # later assertion (defensive; the run is already routed to failure).
    blob.write_bytes(original)
