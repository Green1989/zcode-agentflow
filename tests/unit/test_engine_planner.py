"""Task 6.2 — workflow_init + planner_draft_spec (answers re-entry).

The draft tool drives the questions/answers loop:
  - questions non-empty => NEEDS_USER_INPUT (re-enter with answers);
  - questions empty     => READY_FOR_SPEC_APPROVAL (SPEC promoted as a new gen).

Re-entry uses a revision/attempt-bound operation id and is NOT an idempotent
replay — it re-invokes the adapter and returns the NEW spec. The op id binds to
the current revision + a fresh attempt counter (Review §二; Prompt §15).
"""

from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from zcode_agentflow import (
    agents_context,
    artifacts,
    canonical,
    engine,
    gitops,
    prompts,
    store,
)
from zcode_agentflow.adapters.base import AdapterResult

RUN_ID = "20260730120000-abc123"


# ---------- a fake Codex adapter the engine can call in-process --------------


@dataclass
class FakeCodex:
    """Records each invocation and returns a scripted RAW spec-draft-output.

    ``outputs`` is a list of RAW payloads; each call pops the front. This lets a
    test script the first call (questions) then the second (answers => spec).
    """
    outputs: list
    calls: list = None  # populated per invoke

    def __post_init__(self):
        if self.calls is None:
            self.calls = []

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append({
            "request": copy.deepcopy(request),
            "repo_cwd": str(repo_cwd),
            "operation_dir": str(operation_dir),
        })
        raw = self.outputs.pop(0) if self.outputs else {"goal": "g", "spec_markdown": "# S\n", "questions": [], "assumptions": []}
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
    """A minimal plugin_root that has AGENTS.md.template (agents_context needs it)."""
    pr = tmp_path / "plugin"
    pr.mkdir(exist_ok=True)
    # reuse the repo's real template so resolve() works
    repo_template = Path(__file__).resolve().parents[2] / "AGENTS.md.template"
    (pr / "AGENTS.md.template").write_bytes(repo_template.read_bytes())
    return pr


def _init_run(tmp_path, repo, *, run_id=RUN_ID, goal="build feature X"):
    """Run store.init_run to reach INITIALIZING_WORKTREE, return the engine + roots."""
    user_data = tmp_path / "user_data"
    user_data.mkdir(exist_ok=True)
    plugin = _plugin_root(tmp_path)
    control = tmp_path / "runs" / run_id / "control"
    worktree = tmp_path / "runs" / run_id / "worktree"
    result = store.init_run(
        user_data_root=user_data, target_repo=repo, control_root=control,
        worktree_root=worktree, run_id=run_id, plugin_root=plugin, goal=goal,
    )
    roots = {
        "plugin_root": str(plugin),
        "target_repo_root": str(repo),
        "run_worktree_root": str(worktree),
        "control_root": str(control),
    }
    return result, roots, user_data


def _engine(roots, *, config=None, effective_config=None, user_data_root=None, adapters=None):
    s = store.Store(roots["control_root"])
    return engine.Engine(
        s, roots, config=config or {}, effective_config=effective_config or {},
        user_data_root=user_data_root, adapters=adapters or {},
    )


def _draft_raw(*, questions=None, goal="build feature X", spec="# Spec\n\nbody\n"):
    return {
        "goal": goal,
        "spec_markdown": spec,
        "questions": list(questions or []),
        "assumptions": [
            {"id": "A1", "assumption": "x", "risk": "low", "default": "proceed"}
        ],
    }


# ---------- 1. draft returns NEEDS_USER_INPUT when questions -----------------


def test_draft_returns_needs_user_input_when_questions(tmp_path):
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo)
    fake = FakeCodex(outputs=[_draft_raw(questions=["what version?", "which db?"])])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})

    state_before = store.State.load(roots["control_root"])
    rev = state_before.revision
    result = eng.planner_draft_spec(goal="build feature X")

    st = store.State.load(roots["control_root"])
    assert st.state == "NEEDS_USER_INPUT"
    assert st.revision == rev + 2  # begin +1, finish +1
    # the questions surfaced back to the caller
    assert result["questions"] == ["what version?", "which db?"]
    assert result["status"] == "NEEDS_USER_INPUT"
    # the adapter was actually called once
    assert len(fake.calls) == 1
    # the prompt was rendered + the agent context inlined (non-empty stdin)
    assert "goal" in fake.calls[0]["request"]["prompt"]
    assert fake.calls[0]["repo_cwd"] == roots["run_worktree_root"]


# ---------- 2. re-entry with answers uses a NEW operation id -----------------


def test_reentry_with_answers_uses_new_operation_id(tmp_path):
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo)
    fake = FakeCodex(outputs=[
        _draft_raw(questions=["q1"]),
        _draft_raw(questions=[]),  # answered -> clean spec
    ])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})

    first = eng.planner_draft_spec(goal="build feature X")
    assert first["status"] == "NEEDS_USER_INPUT"

    # second call with answers
    second = eng.planner_draft_spec(goal="build feature X", answers="q1 => postgres")
    assert second["status"] == "READY_FOR_SPEC_APPROVAL"

    # two distinct draft operation ids on disk (revision/attempt-bound). The op
    # id binds to the revision AT begin time — the first call began at revision 3,
    # the re-entry at revision 5 (begin+finish advanced it), so each is a fresh
    # r-revision-bound id (never a replay of the first).
    ops_dir = Path(roots["control_root"]) / "operations"
    draft_ids = sorted(
        p.name for p in ops_dir.iterdir() if "planner_draft_spec" in p.name
    )
    assert len(draft_ids) == 2
    # the two ids are DISTINCT (re-entry is not an idempotent replay of op 1)
    assert draft_ids[0] != draft_ids[1]
    # each is revision-bound; the re-entry began from a HIGHER revision
    assert "__r3__" in draft_ids[0] and "__r5__" in draft_ids[1], draft_ids


def test_reentry_request_carries_answers_and_expected_revision(tmp_path):
    """The re-entry operation request records answers + the NEEDS_USER_INPUT
    source state + the pre-call revision (Review §二)."""
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo)
    fake = FakeCodex(outputs=[
        _draft_raw(questions=["q1"]),
        _draft_raw(questions=[]),
    ])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})
    eng.planner_draft_spec(goal="build feature X")  # NEEDS_USER_INPUT

    eng.planner_draft_spec(goal="build feature X", answers="q1 => postgres")

    # the second call's request carries the answers
    second_req = fake.calls[1]["request"]
    assert second_req.get("answers") == "q1 => postgres"
    # the prompt inlines the answers section
    assert "postgres" in second_req["prompt"]


# ---------- 3. re-entry is NOT an idempotent replay --------------------------


def test_reentry_is_not_idempotent_replay(tmp_path):
    """Re-entry re-invokes the adapter and returns the NEW spec — it must NOT
    short-circuit on the first op's committed result."""
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo)
    fake = FakeCodex(outputs=[
        _draft_raw(questions=["q1"], spec="# old\n"),
        _draft_raw(questions=[], spec="# NEW SPEC\n"),
    ])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})
    eng.planner_draft_spec(goal="g")  # questions
    second = eng.planner_draft_spec(goal="g", answers="a")

    # the NEW spec content is what landed, not the first
    st = store.State.load(roots["control_root"])
    resolver = artifacts.ArtifactResolver(roots["control_root"], st)
    landed = resolver.read_text("SPEC.md")
    assert "NEW SPEC" in landed


# ---------- 4. draft then READY_FOR_SPEC_APPROVAL; SPEC promoted; sha locked


def test_draft_then_ready_for_spec_approval(tmp_path):
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo)
    fake = FakeCodex(outputs=[_draft_raw(questions=[], spec="# Spec\n\nfinal\n")])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})

    result = eng.planner_draft_spec(goal="build feature X")
    assert result["status"] == "READY_FOR_SPEC_APPROVAL"

    st = store.State.load(roots["control_root"])
    assert st.state == "READY_FOR_SPEC_APPROVAL"
    # SPEC promoted as a NEW generation; state.spec_sha256 == resolver hash (P0-3)
    resolver = artifacts.ArtifactResolver(roots["control_root"], st)
    assert st.spec_sha256 == resolver.hash_of("SPEC.md")
    # the canonical bytes of SPEC.md round-trip through the resolver
    spec_bytes = resolver.read("SPEC.md")
    assert st.spec_sha256 == canonical.sha256_hex(canonical.canonical_text_bytes(spec_bytes))


# ---------- 5. spec context invariants checked (run_id/goal injected) --------


def test_spec_context_invariants_checked(tmp_path):
    """The engine injects run_id from state and validates the FINAL spec-envelope
    (Task 2.7): run_id==state.run_id, goal==state.goal. A raw output whose goal
    drifts from the run goal must be rejected (the FINAL envelope carries the
    run's goal, not a model-invented one)."""
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo, goal="the real goal")
    # the fake emits a DIFFERENT goal than the run's; the engine must inject the
    # run's goal into the FINAL envelope, so the landed SPEC carries the run goal.
    fake = FakeCodex(outputs=[_draft_raw(questions=[], goal="WRONG model goal")])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})

    eng.planner_draft_spec(goal="the real goal")

    st = store.State.load(roots["control_root"])
    # the FINAL spec envelope (staged as out.json in the op dir) carries the
    # injected run_id + run goal, not the model's.
    ops_dir = Path(roots["control_root"]) / "operations"
    draft_op = next(p for p in ops_dir.iterdir() if "planner_draft_spec" in p.name)
    final = json.loads((draft_op / "out.json").read_text(encoding="utf-8"))
    assert final["run_id"] == RUN_ID
    assert final["goal"] == "the real goal"
    assert final["status"] == "READY_FOR_SPEC_APPROVAL"


# ---------- 6. workflow_init delegates to init_run + reaches draftable state


def test_workflow_init_creates_run_and_reaches_initializing(tmp_path):
    """Engine.workflow_init wraps store.init_run (Task 3.7): creates the worktree,
    records base_commit, and lands in INITIALIZING_WORKTREE ready for draft."""
    repo = _make_repo(tmp_path)
    plugin = _plugin_root(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir(exist_ok=True)
    control = tmp_path / "runs" / RUN_ID / "control"
    worktree = tmp_path / "runs" / RUN_ID / "worktree"
    roots = {
        "plugin_root": str(plugin),
        "target_repo_root": str(repo),
        "run_worktree_root": str(worktree),
        "control_root": str(control),
    }
    eng = _engine(roots, user_data_root=user_data)
    result = eng.workflow_init(target_repo=str(repo), goal="g", run_id=RUN_ID)

    assert result["run_id"] == RUN_ID
    assert result["base_commit"]
    st = store.State.load(control)
    assert st.state == "INITIALIZING_WORKTREE"
    assert gitops.is_git_repo(worktree)


def test_draft_from_wrong_state_rejected(tmp_path):
    """planner_draft_spec may not begin from READY_FOR_EXECUTION."""
    repo = _make_repo(tmp_path)
    _, roots, user_data = _init_run(tmp_path, repo)
    fake = FakeCodex(outputs=[_draft_raw(questions=[])])
    eng = _engine(roots, user_data_root=user_data, adapters={"codex": fake})
    # force the state to a wrong source
    st = store.State.load(roots["control_root"])
    st.state = "READY_FOR_EXECUTION"
    st.save(roots["control_root"])
    with pytest.raises(store.TransitionNotAllowed):
        eng.planner_draft_spec(goal="g")
