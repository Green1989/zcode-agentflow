"""Task 6.1 — Engine scaffold: ALLOWED_FROM (inside begin) + artifact rehash + check_stop.

The Engine总不变量 #1 (Prompt §6): ALLOWED_FROM + CAS + artifact rehash all run
inside the SAME locked ``Store.begin_operation`` critical section — there is no
second Run Lock and no nested ``_guard`` lock (P0-3). ``check_stop`` is the
read-only hook the Phase-7 Stop hook will call; it shares the hard-check
primitives with ``workflow_complete_task`` (Review §六.6) so engine and hook never
drift.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
from pathlib import Path

import pytest

from zcode_agentflow import artifacts, canonical, engine, gitops, locks, store
from zcode_agentflow.artifacts import (
    ArtifactCorrupt,
    ArtifactMismatch,
    ArtifactResolver,
    prepare_generation,
)

RUN_ID = "20260730120000-abc123"

# Spec §3.1 / §5.1 — the authoritative state-machine transitions each tool may
# begin from. workflow_status is read-only (no transition) and intentionally
# absent from this table.
SPEC_ALLOWED_FROM = {
    "workflow_init": frozenset({"NEW"}),
    "planner_draft_spec": frozenset({"INITIALIZING_WORKTREE", "NEEDS_USER_INPUT"}),
    "workflow_approve_spec": frozenset({"READY_FOR_SPEC_APPROVAL"}),
    "planner_create_plan": frozenset({"PLANNING"}),
    "workflow_start_task": frozenset({"READY_FOR_EXECUTION"}),
    "workflow_verify_task": frozenset({"EXECUTING_TASK"}),
    "reviewer_review_task": frozenset({"VERIFYING_TASK"}),
    "workflow_complete_task": frozenset({"REVIEWING_TASK"}),
}


# ---------- helpers ----------------------------------------------------------


def _seed_state(control_root, *, run_id=RUN_ID, state="INITIALIZING_WORKTREE",
                revision=5, **overrides) -> store.State:
    st = store.State(
        run_id=run_id, revision=revision, state=state, previous_state="",
        plan_version=1, current_task="", completed_tasks=[],
        roots={
            "plugin_root": "/plugin",
            "target_repo_root": "/repo",
            "run_worktree_root": "/wt",
            "control_root": str(control_root),
        },
    )
    for k, v in overrides.items():
        setattr(st, k, v)
    st.save(control_root)
    return st


def _engine(control_root, *, run_worktree_root="/wt", **kwargs) -> "engine.Engine":
    s = store.Store(control_root)
    return engine.Engine(
        s,
        roots={
            "plugin_root": "/plugin",
            "target_repo_root": "/repo",
            "run_worktree_root": str(run_worktree_root),
            "control_root": str(control_root),
        },
        **kwargs,
    )


def _commit_generation(control_root, prev_state, changes, *, gen_id=None):
    """Stage a complete generation (blobs + manifest) and return a next_state
    whose artifact_generation + per-artifact shas are filled in, saved to disk."""
    nxt = copy.deepcopy(prev_state)
    gid = gen_id or f"op_r{prev_state.revision}"
    prepare_generation(
        control_root=control_root, operation_id=gid, changes=changes,
        prev_state=prev_state, next_state=nxt,
    )
    nxt.save(control_root)
    return nxt


def _blob_path_for(control_root, gen_id, name) -> Path:
    gen = artifacts.load_generation(control_root, gen_id)
    return control_root / gen.index[name]["blob"]


# ---------- 1. ALLOWED_FROM table --------------------------------------------


def test_allowed_from_table_complete():
    # The 8 transition tools are present; workflow_status is read-only.
    assert set(engine.ALLOWED_FROM) == set(SPEC_ALLOWED_FROM)
    for tool, allowed in SPEC_ALLOWED_FROM.items():
        assert engine.ALLOWED_FROM[tool] == allowed, tool
    # The Engine's authoritative table MUST agree with the Store's enforcement
    # table — they are the same state machine (P0-3: enforced inside begin).
    assert engine.ALLOWED_FROM == store.ALLOWED_FROM


# ---------- 2. guard runs INSIDE begin, no second lock (P0-3) -----------------


def test_guard_runs_inside_begin_not_separately(control_root, monkeypatch):
    """ALLOWED_FROM + CAS + artifact rehash all happen within ONE locked
    begin_operation; the Engine acquires no second Run Lock."""
    _seed_state(control_root, state="INITIALIZING_WORKTREE", revision=5)
    eng = _engine(control_root)

    counter = {"n": 0}
    orig = locks.run_lock

    @contextlib.contextmanager
    def counting_run_lock(cr, timeout=30):
        counter["n"] += 1
        with orig(cr, timeout=timeout):
            yield

    monkeypatch.setattr(locks, "run_lock", counting_run_lock)

    br = eng._begin(
        tool="planner_draft_spec",
        operation_id=f"{RUN_ID}__planner_draft_spec__r5__a1",
        request={"tool": "planner_draft_spec", "args": {"goal": "g"}},
        expected_revision=5,
        intermediate_state="DRAFTING_SPEC",
        replay_policy="COSTFUL_READ",
    )

    # exactly ONE lock acquisition for the whole begin (ALLOWED_FROM + CAS + rehash
    # under that single critical section — no separate _guard lock).
    assert counter["n"] == 1
    # begin succeeded => state advanced to the intermediate, revision +1.
    st = store.State.load(control_root)
    assert st.state == "DRAFTING_SPEC"
    assert st.revision == 6
    assert br.revision_after_begin == 6


# ---------- 3. calling from a wrong state raises -----------------------------


def test_calling_from_wrong_state_raises(control_root):
    """planner_draft_spec may only begin from INITIALIZING_WORKTREE /
    NEEDS_USER_INPUT; from READY_FOR_EXECUTION it must raise."""
    _seed_state(control_root, state="READY_FOR_EXECUTION", revision=5)
    eng = _engine(control_root)
    with pytest.raises(store.TransitionNotAllowed):
        eng._begin(
            tool="planner_draft_spec",
            operation_id=f"{RUN_ID}__planner_draft_spec__r5__a1",
            request={"tool": "planner_draft_spec", "args": {"goal": "g"}},
            expected_revision=5,
            intermediate_state="DRAFTING_SPEC",
            replay_policy="COSTFUL_READ",
        )


def test_stale_revision_raises_cas(control_root):
    """A stale expected_revision is rejected inside begin (CAS)."""
    _seed_state(control_root, state="INITIALIZING_WORKTREE", revision=5)
    eng = _engine(control_root)
    with pytest.raises(store.CasMismatch):
        eng._begin(
            tool="planner_draft_spec",
            operation_id=f"{RUN_ID}__planner_draft_spec__r4__a1",
            request={"tool": "planner_draft_spec", "args": {"goal": "g"}},
            expected_revision=4,  # stale
            intermediate_state="DRAFTING_SPEC",
            replay_policy="COSTFUL_READ",
        )


# ---------- 4. verify_locked_artifacts rehashes from disk (Review §五) --------


def test_verify_locked_artifacts_rehashes_from_disk(control_root):
    """A SPEC committed then mutated on disk => rehash detects it (Review §五;
    P0-3: the read goes through the ArtifactResolver, not a control_root root file)."""
    prev = _seed_state(control_root, state="READY_FOR_SPEC_APPROVAL", revision=5)
    spec_bytes = b"# Spec\n\nApproved content.\n"
    nxt = _commit_generation(control_root, prev, {"SPEC.md": spec_bytes}, gen_id="op_spec")
    # state now records the locked spec sha (== the committed canonical sha).
    assert nxt.spec_sha256 == canonical.sha256_hex(canonical.canonical_text_bytes(spec_bytes))

    resolver = ArtifactResolver(control_root, nxt)
    artifacts.verify_locked_artifacts(nxt, resolver)  # OK — unchanged

    # tamper the SPEC blob's bytes on disk (post-commit mutation)
    blob = _blob_path_for(control_root, "op_spec", "SPEC.md")
    blob.write_bytes(b"# Spec\n\nTAMPERED\n")

    fresh = ArtifactResolver(control_root, nxt)  # don't reuse the cached generation
    with pytest.raises((ArtifactCorrupt, ArtifactMismatch)):
        artifacts.verify_locked_artifacts(nxt, fresh)


def test_verify_locked_artifacts_detects_rewritten_generation(control_root):
    """If a generation is rewritten (blob + manifest agree) to a NEW spec but
    state still holds the OLD locked sha, the rehash-vs-state comparison catches
    it (ArtifactMismatch)."""
    prev = _seed_state(control_root, state="READY_FOR_SPEC_APPROVAL", revision=5)
    original = _commit_generation(control_root, prev, {"SPEC.md": b"# Spec A\n"}, gen_id="g1")
    locked_sha = original.spec_sha256

    # a NEW generation rewrites SPEC to different content (state would normally be
    # updated too, but simulate a state that still holds the OLD locked sha — the
    # exact post-approval tamper window complete_task defends against).
    rewritten = copy.deepcopy(prev)
    prepare_generation(
        control_root=control_root, operation_id="g2", changes={"SPEC.md": b"# Spec B\n"},
        prev_state=original, next_state=rewritten,
    )
    # keep the OLD locked sha in the state view we verify against
    rewritten.spec_sha256 = locked_sha
    rewritten.save(control_root)

    resolver = ArtifactResolver(control_root, rewritten)
    with pytest.raises(ArtifactMismatch):
        artifacts.verify_locked_artifacts(rewritten, resolver)


def test_verify_locked_artifacts_skips_unset_artifacts(control_root):
    """A fresh run with no SPEC/PLAN yet must verify cleanly (all shas None)."""
    prev = _seed_state(control_root, state="INITIALIZING_WORKTREE", revision=2)
    resolver = ArtifactResolver(control_root, prev)
    # no artifacts committed => no-ops, no raise
    artifacts.verify_locked_artifacts(prev, resolver)


# ---------- 5. check_stop is read-only + blocks on every failure -------------


def _seed_reviewable_run(control_root, worktree_root, *, head_commit,
                         review_decision="PASS", review_present=True,
                         exec_head=None, task_start_commit=None):
    """Seed a run in REVIEWING_TASK with a full generation + a real worktree, so
    check_stop has something to verify against."""
    prev = _seed_state(
        control_root, state="REVIEWING_TASK", revision=11,
        run_worktree_root=str(worktree_root), last_reviewed_commit=head_commit,
        # task_start_commit differs from head by default (a real change was made)
        task_start_commit=task_start_commit or "0".rjust(40, "0"),
    )
    spec_b = b"# Spec\n\nS\n"
    plan_b = b"# Plan\n\nP\n"
    manifest_obj = {"plan_sha256": canonical.sha256_hex(plan_b), "current_task": "T001", "tasks": []}
    manifest_b = canonical.canonical_json_bytes(manifest_obj)
    exec_obj = {
        "run_id": RUN_ID, "task_id": "T001", "status": "READY_FOR_REVIEW",
        "base_commit": head_commit, "head_commit": exec_head or head_commit,
        "worktree_clean": True, "untracked_files": [], "verification": [],
        "changed_files": ["src/x.py"], "change_audit": {
            "deleted_files": [], "dependency_files_changed": False,
            "migrations_added": [], "undeclared_files_changed": [],
        },
    }
    exec_b = canonical.canonical_json_bytes(exec_obj)
    changes = {
        "SPEC.md": spec_b, "PLAN.md": plan_b, "task-manifest.json": manifest_b,
        "execution-result.json": exec_b,
    }
    if review_present:
        rev_obj = {
            "run_id": RUN_ID, "task_id": "T001", "base_commit": head_commit,
            "head_commit": head_commit,
            "spec_sha256": canonical.sha256_hex(spec_b),
            "plan_sha256": canonical.sha256_hex(plan_b),
            "task_manifest_sha256": canonical.sha256_hex(manifest_b),
            "plan_version": 1, "decision": review_decision,
            "spec_compliance": True, "plan_compliance": True,
            "tests_sufficient": True, "issues": [], "replan_required": False,
        }
        changes["review-result.json"] = canonical.canonical_json_bytes(rev_obj)
    nxt = _commit_generation(control_root, prev, changes, gen_id="op_verify")
    return nxt


def test_check_stop_read_only_allows_when_clean(control_root, fake_repo, tmp_path):
    wt = tmp_path / "wt"
    base = gitops.create_worktree(target_repo=fake_repo, worktree_path=wt, branch="agentflow/cs1")
    _seed_reviewable_run(control_root, wt, head_commit=base)

    before = canonical.hash_file(Path(control_root) / store.STATE_FILENAME)
    eng = _engine(control_root, run_worktree_root=wt)

    result = eng.check_stop(RUN_ID)
    # shape
    assert set(result) >= {"allow_stop", "reasons"}
    assert result["allow_stop"] is True
    assert result["reasons"] == []
    # read-only: state.json byte-identical
    after = canonical.hash_file(Path(control_root) / store.STATE_FILENAME)
    assert before == after


def test_check_stop_blocks_on_dirty_worktree(control_root, fake_repo, tmp_path):
    wt = tmp_path / "wt"
    base = gitops.create_worktree(target_repo=fake_repo, worktree_path=wt, branch="agentflow/cs2")
    _seed_reviewable_run(control_root, wt, head_commit=base)

    eng = _engine(control_root, run_worktree_root=wt)
    # dirty the worktree (untracked file)
    (wt / "stray.txt").write_text("oops\n", encoding="utf-8")
    result = eng.check_stop(RUN_ID)
    assert result["allow_stop"] is False
    assert result["reasons"]  # non-empty


def test_check_stop_blocks_when_no_passing_review(control_root, fake_repo, tmp_path):
    wt = tmp_path / "wt"
    base = gitops.create_worktree(target_repo=fake_repo, worktree_path=wt, branch="agentflow/cs3")
    # no review artifact at all
    _seed_reviewable_run(control_root, wt, head_commit=base, review_present=False)

    eng = _engine(control_root, run_worktree_root=wt)
    result = eng.check_stop(RUN_ID)
    assert result["allow_stop"] is False
    assert any("review" in r.lower() for r in result["reasons"])


def test_check_stop_blocks_on_head_mismatch(control_root, fake_repo, tmp_path):
    wt = tmp_path / "wt"
    base = gitops.create_worktree(target_repo=fake_repo, worktree_path=wt, branch="agentflow/cs4")
    _seed_reviewable_run(control_root, wt, head_commit=base)
    # advance the worktree HEAD with a new commit so it no longer matches the
    # reviewed commit
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    import subprocess as _sp
    (wt / "new.txt").write_text("n\n", encoding="utf-8")
    _sp.run(["git", "add", "."], cwd=wt, check=True, env=env)
    _sp.run(["git", "commit", "-q", "-m", "advance"], cwd=wt, check=True, env=env)

    eng = _engine(control_root, run_worktree_root=wt)
    result = eng.check_stop(RUN_ID)
    assert result["allow_stop"] is False
    assert any("head" in r.lower() for r in result["reasons"])


def test_check_stop_blocks_on_tampered_artifact(control_root, fake_repo, tmp_path):
    wt = tmp_path / "wt"
    base = gitops.create_worktree(target_repo=fake_repo, worktree_path=wt, branch="agentflow/cs5")
    _seed_reviewable_run(control_root, wt, head_commit=base)
    # tamper the SPEC blob
    blob = _blob_path_for(control_root, "op_verify", "SPEC.md")
    blob.write_bytes(b"# Spec\n\nTAMPERED\n")

    eng = _engine(control_root, run_worktree_root=wt)
    result = eng.check_stop(RUN_ID)
    assert result["allow_stop"] is False
    assert result["reasons"]
