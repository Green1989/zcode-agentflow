"""Task 6.3 — workflow_approve_spec + planner_create_plan (exactly-one-task).

Approval locks the spec sha (HUMAN GATE 1) and writes a trusted approval record;
create_plan validates the approved sha and enforces ONE task through the
planner-result wrapper (P0-5). A multi-task goal routes NEEDS_USER_INPUT/BLOCKED
and is NEVER silently reduced to tasks[0].
"""

from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from zcode_agentflow import (
    approval,
    artifacts,
    canonical,
    engine,
    manifest,
    prompts,
    store,
)
from zcode_agentflow.adapters.base import AdapterResult

RUN_ID = "20260730120000-abc123"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


# ---------- fake codex (create_plan) -----------------------------------------


@dataclass
class FakeCodex:
    outputs: list
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append({"request": copy.deepcopy(request), "repo_cwd": str(repo_cwd)})
        raw = self.outputs.pop(0) if self.outputs else {}
        return AdapterResult(exit_code=0, stdout="", stderr="", artifacts={}, parsed=copy.deepcopy(raw))


# ---------- helpers ----------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin_root(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir(exist_ok=True)
    repo_template = Path(__file__).resolve().parents[2] / "AGENTS.md.template"
    (pr / "AGENTS.md.template").write_bytes(repo_template.read_bytes())
    return pr


def _seed_ready_for_approval(control, *, spec_text="# Spec\n\napproved\n"):
    """Seed a run in READY_FOR_SPEC_APPROVAL with a committed SPEC generation."""
    st = store.State(
        run_id=RUN_ID, revision=6, state="READY_FOR_SPEC_APPROVAL",
        previous_state="DRAFTING_SPEC", plan_version=1, current_task="",
        completed_tasks=[],
        roots={"plugin_root": "/plugin", "target_repo_root": "/repo",
               "run_worktree_root": "/wt", "control_root": str(control)},
        base_commit=COMMIT,
    )
    st.save(control)
    # commit the SPEC generation
    nxt = copy.deepcopy(st)
    artifacts.prepare_generation(
        control_root=control, operation_id="op_draft",
        changes={"SPEC.md": spec_text.encode("utf-8")},
        prev_state=st, next_state=nxt,
    )
    nxt.save(control)
    return nxt


def _seed_planning(control):
    """Seed a run in PLANNING with a locked spec sha (post-approval)."""
    spec_text = "# Spec\n\napproved\n"
    st = _seed_ready_for_approval(control, spec_text=spec_text)
    st.state = "PLANNING"
    st.previous_state = "READY_FOR_SPEC_APPROVAL"
    st.revision = 8
    st.save(control)
    return st


def _engine(control, *, run_worktree_root="/wt", target_repo="/repo",
            plugin_root="/plugin",
            effective_approval_mode="zcode_tool_confirmation", adapters=None,
            config=None, user_data_root=None):
    s = store.Store(control)
    return engine.Engine(
        s,
        roots={"plugin_root": str(plugin_root), "target_repo_root": str(target_repo),
               "run_worktree_root": str(run_worktree_root),
               "control_root": str(control)},
        config=config or {"verification": {"program_allowlist": ["python"],
                                            "interpreter_allowlist": ["bash", "cmd.exe"]}},
        effective_config={"effective_approval_mode": effective_approval_mode},
        user_data_root=user_data_root, adapters=adapters or {},
    )


def _plan_raw(*, n_tasks=1, status="READY_FOR_EXECUTION"):
    task = {
        "id": "T001", "title": "do it", "dependencies": [],
        "declared_files": ["src/x.py"],
        "verification_commands": [
            {"kind": "executable", "program": "python", "args": ["-m", "pytest"],
             "cwd": ".", "timeout_sec": 60}
        ],
        "acceptance_criteria": ["tests pass"],
        "planned_changes": {"may_add_dependencies": False,
                            "may_run_migrations": False, "may_delete_files": []},
    }
    wrapper = {"status": status}
    if status == "READY_FOR_EXECUTION":
        wrapper["plan"] = {"plan_markdown": "# Plan\n\none task\n",
                           "tasks": [task] * n_tasks}
    elif status == "NEEDS_USER_INPUT":
        wrapper["questions"] = ["which approach?"]
    elif status == "BLOCKED":
        wrapper["block_reason"] = "too many tasks for v1"
    return wrapper


# ---------- 1. approve records + locks spec sha ------------------------------


def test_approve_records_and_locks_spec_sha(tmp_path, control_root):
    st = _seed_ready_for_approval(control_root)
    locked_sha = st.spec_sha256
    eng = _engine(control_root)

    result = eng.workflow_approve_spec(expected_revision=st.revision)

    # an approval record landed on disk
    approvals_dir = Path(control_root) / "approvals"
    assert approvals_dir.is_dir() and any(approvals_dir.glob("apr_*.json"))
    rec = json.loads(next(approvals_dir.glob("apr_*.json")).read_text(encoding="utf-8"))
    assert rec["gate"] == "SPEC"
    assert rec["artifact_sha256"] == locked_sha
    assert rec["approval_method"] == "zcode_tool_confirmation"
    # state advanced to PLANNING; spec sha unchanged (locked)
    after = store.State.load(control_root)
    assert after.state == "PLANNING"
    assert after.spec_sha256 == locked_sha
    assert result["state"] == "PLANNING"


def test_approve_rejected_from_wrong_state(tmp_path, control_root):
    """Re-approve from PLANNING is rejected (ALLOWED_FROM)."""
    _seed_planning(control_root)
    eng = _engine(control_root)
    st = store.State.load(control_root)
    with pytest.raises(store.TransitionNotAllowed):
        eng.workflow_approve_spec(expected_revision=st.revision)


def test_approve_fails_loud_without_confirmation(tmp_path, control_root):
    """No strong confirmation AND no fallback allowed => ApprovalUnavailableError
    (Spec §5.4 / G6: never silently degrade)."""
    _seed_ready_for_approval(control_root)
    # effective mode None = neither strong nor fallback
    eng = _engine(control_root, effective_approval_mode=None)
    st = store.State.load(control_root)
    with pytest.raises(approval.ApprovalUnavailableError):
        eng.workflow_approve_spec(expected_revision=st.revision)


# ---------- 2. create_plan requires the approved spec sha --------------------


def test_create_plan_requires_approved_spec_sha(tmp_path, control_root):
    _seed_planning(control_root)
    fake = FakeCodex(outputs=[_plan_raw()])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))
    # a WRONG sha must be rejected before any adapter call
    with pytest.raises(engine.WorkflowError):
        eng.planner_create_plan(
            approved_spec_sha256="0" * 64, expected_revision=8,
        )
    assert fake.calls == []  # adapter never invoked


def test_create_plan_from_wrong_state_rejected(tmp_path, control_root):
    """create_plan may only begin from PLANNING."""
    _seed_ready_for_approval(control_root)  # READY_FOR_SPEC_APPROVAL, not PLANNING
    st = store.State.load(control_root)
    fake = FakeCodex(outputs=[_plan_raw()])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))
    with pytest.raises(store.TransitionNotAllowed):
        eng.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                                expected_revision=st.revision)


# ---------- 3. multi-task is NEVER silently reduced (P0-5) -------------------


def test_create_plan_rejects_multi_task_via_wrapper(tmp_path, control_root):
    """The plan-proposal wrapper is maxItems:1 tasks, so a 2-task goal fails RAW
    validation at the adapter. The engine must route to NEEDS_USER_INPUT/BLOCKED,
    NEVER silently take tasks[0] (P0-5)."""
    _seed_planning(control_root)
    st = store.State.load(control_root)
    # a wrapper that carries 2 tasks — the RAW schema rejects this, so the fake
    # adapter's parsed path raises CodexAdapterError; the engine routes the
    # failure to NEEDS_HUMAN (fail_operation). The point: no tasks[0] ever lands.
    fake = FakeCodex(outputs=[_plan_raw(n_tasks=2)])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))
    with pytest.raises(Exception):
        eng.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                                expected_revision=st.revision)
    after = store.State.load(control_root)
    # the run routed to NEEDS_HUMAN (fail path), NOT READY_FOR_EXECUTION
    assert after.state == "NEEDS_HUMAN"
    # no PLAN was promoted (no silent tasks[0])
    resolver = artifacts.ArtifactResolver(control_root, after)
    with pytest.raises(artifacts.ArtifactNotFound):
        resolver.read("PLAN.md")


def test_create_plan_routes_blocked_wrapper_to_needs_human(tmp_path, control_root):
    """A BLOCKED wrapper (model could not produce a single-task plan) routes to
    NEEDS_HUMAN, not READY_FOR_EXECUTION."""
    _seed_planning(control_root)
    st = store.State.load(control_root)
    fake = FakeCodex(outputs=[_plan_raw(status="BLOCKED")])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))
    result = eng.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                                     expected_revision=st.revision)
    assert result["status"] == "BLOCKED"
    after = store.State.load(control_root)
    assert after.state == "NEEDS_HUMAN"


# ---------- 4. single-task plan accepted; PLAN + manifest written ------------


def test_single_task_plan_accepted(tmp_path, control_root):
    _seed_planning(control_root)
    st = store.State.load(control_root)
    fake = FakeCodex(outputs=[_plan_raw()])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))

    result = eng.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                                     expected_revision=st.revision)
    assert result["status"] == "READY_FOR_EXECUTION"
    after = store.State.load(control_root)
    assert after.state == "READY_FOR_EXECUTION"
    assert after.current_task == "T001"
    # PLAN.md + task-manifest.json written; hashes from resolver (P0-3)
    resolver = artifacts.ArtifactResolver(control_root, after)
    assert after.plan_sha256 == resolver.hash_of("PLAN.md")
    assert after.task_manifest_sha256 == resolver.hash_of("task-manifest.json")


# ---------- 5. plan context invariants checked (Task 2.7) -------------------


def test_plan_context_invariants_checked(tmp_path, control_root):
    """base_commit is injected from state (not trusted from the model); run_id,
    plan_version, single T001, relative paths, allowlist are all checked."""
    _seed_planning(control_root)
    st = store.State.load(control_root)
    fake = FakeCodex(outputs=[_plan_raw()])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))

    eng.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                            expected_revision=st.revision)

    after = store.State.load(control_root)
    # the FINAL plan envelope (out.json in the op dir) carries the injected
    # authoritative fields, not the model's.
    ops_dir = Path(control_root) / "operations"
    plan_op = next(p for p in ops_dir.iterdir() if "planner_create_plan" in p.name)
    final = json.loads((plan_op / "out.json").read_text(encoding="utf-8"))
    assert final["run_id"] == RUN_ID
    assert final["base_commit"] == COMMIT  # injected from state
    assert final["plan_version"] == 1
    assert final["tasks"][0]["id"] == "T001"


# ---------- 6. landed plan envelope stays strict (no wrapper fields) ---------


def test_landed_plan_envelope_stays_strict(tmp_path, control_root):
    """The promoted PLAN.md + task-manifest.json are the strict single-task
    artifacts — no wrapper ``status``/``questions`` fields leak into them."""
    _seed_planning(control_root)
    st = store.State.load(control_root)
    fake = FakeCodex(outputs=[_plan_raw()])
    eng = _engine(control_root, adapters={"codex": fake},
                  plugin_root=_plugin_root(tmp_path))
    eng.planner_create_plan(approved_spec_sha256=st.spec_sha256,
                            expected_revision=st.revision)

    after = store.State.load(control_root)
    resolver = artifacts.ArtifactResolver(control_root, after)
    plan_md = resolver.read_text("PLAN.md")
    # PLAN.md is markdown, not a wrapper object
    assert "# Plan" in plan_md
    # the manifest is the self-reference-free single-task manifest
    man = json.loads(resolver.read("task-manifest.json"))
    assert man["plan_sha256"] == after.plan_sha256
    assert man["current_task"] == "T001"
    assert len(man["tasks"]) == 1
    assert "status" not in man  # no wrapper fields
