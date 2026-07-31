"""Workflow Engine — the 9 tools over the Store/Adapters (Phase 6).

The Engine is the assembly layer: it wires the Store two-phase transaction,
the Adapters (Codex/Grok/Verification), the Approval module, the change policy,
and the AgentContext resolver into the strict v1 state machine (Spec §3.1) and
the fixed 9-tool MCP surface (Spec §5.1).

Engine总不变量 (Prompt §6):
  1. ALLOWED_FROM + CAS + artifact rehash run inside ``Store.begin_operation``'s
     single locked critical section. There is NO second Run Lock and no nested
     ``_guard`` lock (P0-3). ``begin_operation`` already enforces ALLOWED_FROM +
     CAS + the one-active-op gate under its lock; this module ADDS the on-disk
     artifact rehash by running ``verify_locked_artifacts`` right after a
     successful begin (still no extra lock — begin returned, so the rehash is a
     read of the generation the state points at).
  2. Every external call is strictly ``begin → adapter (no lock) → finish``.
  3. Authoritative artifacts are read/written ONLY via the ArtifactResolver /
     ``prepare_generation``; never direct ``control_root/SPEC.md`` writes.
  4. Model output is validated against the RAW schema first; the Engine injects
     authoritative fields (run_id/base_commit/hashes/version) from state, then
     validates the FINAL envelope.
  5. v1 allows exactly ONE task; a multi-task goal is never silently reduced to
     ``tasks[0]``.
  6. Review binds the verified HEAD and all hashes; complete is rejected while a
     review is in flight or stale.
  7. ``workflow_complete_task`` runs the full hard-check set (≥11).
  8. The MCP surface is exactly 9 tools; abandon/archive are local admin commands.
  9. READY_TO_MERGE marks the active pointer ``terminal:true``; no push, no merge.

Task 6.1 ships the scaffold ONLY: the authoritative ``ALLOWED_FROM`` table, the
``_begin`` helper that runs ALLOWED_FROM+CAS (via the Store) and rehashes the
locked artifacts, the shared hard-check primitives, and the read-only
``check_stop(run_id)`` consumed by the Phase-7 Stop hook.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from zcode_agentflow import (
    artifacts,
    gitops,
    store,
)
from zcode_agentflow.artifacts import ArtifactResolver

__all__ = [
    "ALLOWED_FROM",
    "WorkflowError",
    "HardCheckResult",
    "Engine",
    "run_hard_checks",
]

_log = logging.getLogger(__name__)

# ===========================================================================
# ALLOWED_FROM — the authoritative state-machine transition table (Spec §3.1).
#
# This is the SAME table the Store enforces at begin time (store.ALLOWED_FROM).
# It is re-declared here so the Engine is the single source a reader consults for
# "which tool may begin from which state", and so the Engine and Store cannot
# drift (the unit test asserts they are identical). workflow_status is read-only
# (no transition) and intentionally NOT in this table.
# ===========================================================================

ALLOWED_FROM: dict[str, frozenset[str]] = {
    "workflow_init": frozenset({"NEW"}),
    "planner_draft_spec": frozenset({"INITIALIZING_WORKTREE", "NEEDS_USER_INPUT"}),
    "workflow_approve_spec": frozenset({"READY_FOR_SPEC_APPROVAL"}),
    "planner_create_plan": frozenset({"PLANNING"}),
    "workflow_start_task": frozenset({"READY_FOR_EXECUTION"}),
    "workflow_verify_task": frozenset({"EXECUTING_TASK"}),
    "reviewer_review_task": frozenset({"VERIFYING_TASK"}),
    "workflow_complete_task": frozenset({"REVIEWING_TASK"}),
}


class WorkflowError(RuntimeError):
    """Raised by the Engine when a workflow precondition fails in a way that is
    NOT a low-level Store error (state/revision/lease). Used to route to
    NEEDS_HUMAN / NEEDS_USER_INPUT at the adapter layer. (Store-level
    TransitionNotAllowed / CasMismatch propagate as their own types.)"""


@dataclass
class _Roots:
    plugin_root: Path
    target_repo_root: Path
    run_worktree_root: Path
    control_root: Path


class Engine:
    """The workflow engine. Constructed per active run; the 9 tool methods
    (added in Tasks 6.2–6.6) drive the Store two-phase transactions + adapters.

    Task 6.1 ships the scaffold: the ``_begin`` helper (ALLOWED_FROM + CAS via
    the Store + on-disk artifact rehash), the shared hard-check primitives, and
    the read-only ``check_stop``.
    """

    def __init__(
        self,
        store_obj: "store.Store",
        roots: dict,
        *,
        config: dict | None = None,
        effective_config: dict | None = None,
        user_data_root: Path | str | None = None,
        adapters: dict | None = None,
    ):
        self.store = store_obj
        self.roots = _Roots(
            plugin_root=Path(roots["plugin_root"]),
            target_repo_root=Path(roots["target_repo_root"]),
            run_worktree_root=Path(roots["run_worktree_root"]),
            control_root=Path(roots["control_root"]),
        )
        self.config = config or {}
        self.effective_config = effective_config or {}
        self.user_data_root = Path(user_data_root) if user_data_root else None
        # adapters: a dict of {"codex": CodexAdapter, "grok": GrokAdapter} that
        # tests inject fakes into. Production builds them from config (Task 6.7).
        self.adapters = adapters or {}

    # ------------------------------------------------------------------
    # _begin — ALLOWED_FROM + CAS + artifact rehash (P0-3)
    #
    # begin_operation takes the per-Run lock and enforces ALLOWED_FROM + CAS +
    # the one-active-op gate INSIDE that single critical section. There is no
    # second lock. The artifact rehash (verify_locked_artifacts) reads the
    # generation the state points at; it is a read-only check that needs no
    # extra lock (state.json is atomic on disk, and the generation is immutable
    # once committed).
    # ------------------------------------------------------------------

    def _begin(
        self,
        *,
        tool: str,
        operation_id: str,
        request: dict,
        expected_revision: int,
        intermediate_state: str,
        replay_policy: str,
    ) -> "store.BeginResult":
        br = self.store.begin_operation(
            operation_id=operation_id,
            request=request,
            tool=tool,
            expected_revision=expected_revision,
            intermediate_state=intermediate_state,
            replay_policy=replay_policy,
        )
        # rehash the locked artifacts against the post-begin state. The state
        # did not change its artifact_generation during begin (only revision +
        # active_operation_id), so this validates the SAME committed generation.
        state = store.State.load(self.roots.control_root)
        artifacts.verify_locked_artifacts(
            state, ArtifactResolver(self.roots.control_root, state)
        )
        return br

    # ------------------------------------------------------------------
    # read-only helpers
    # ------------------------------------------------------------------

    def _load_state(self) -> "store.State":
        return store.State.load(self.roots.control_root)

    def _resolver(self, state=None) -> ArtifactResolver:
        st = state if state is not None else self._load_state()
        return ArtifactResolver(self.roots.control_root, st)

    # ------------------------------------------------------------------
    # Hard checks — shared by check_stop and workflow_complete_task
    # (Review §六.6: the hook and the engine must never drift).
    # ------------------------------------------------------------------

    def _hard_checks(self) -> list[tuple[str, bool, str]]:
        return _run_hard_checks(self)

    def check_stop(self, run_id: str | None = None) -> dict:
        """Read-only stop gate (Task 6.1; Phase 7 Stop hook consumer).

        Returns ``{"allow_stop": bool, "reasons": [str], "checks": [...]}``
        WITHOUT mutating state.json. ``allow_stop`` is True iff the shared
        hard-check set passes. This is the SINGLE stop decision logic — the
        Phase-7 hook delegates here and never re-derives review/clean/HEAD
        logic (Review §六.6).
        """
        result = run_hard_checks(self)
        return {
            "allow_stop": result.allow,
            "reasons": list(result.reasons),
            "checks": [list(c) for c in result.checks],
        }


# ===========================================================================
# Hard-check aggregation + implementation
# ===========================================================================


@dataclass
class HardCheckResult:
    """Aggregated result of the complete_task hard-check set.

    ``allow`` is True iff every check passed. ``reasons`` lists the human-readable
    cause of each failing check (empty when all pass). Consumed by both
    ``check_stop`` (read-only) and ``workflow_complete_task`` (the gate).
    """

    allow: bool
    reasons: list[str]
    checks: list[tuple[str, bool, str]]

    @classmethod
    def from_checks(cls, checks: list[tuple[str, bool, str]]) -> "HardCheckResult":
        reasons = [r for (_n, ok, r) in checks if not ok and r]
        return cls(allow=all(ok for (_n, ok, _r) in checks), reasons=reasons, checks=checks)


def run_hard_checks(engine: "Engine") -> HardCheckResult:
    """Run the shared hard-check set on ``engine`` and aggregate the result.

    This is the single source of truth for "is this run safe to complete / safe
    to stop" — both ``check_stop`` and ``workflow_complete_task`` go through here
    (Review §六.6: hook and engine never drift)."""
    return HardCheckResult.from_checks(_run_hard_checks(engine))


def _run_hard_checks(engine: "Engine") -> list[tuple[str, bool, str]]:
    """Run the full hard-check set on ``engine``.

    Each check is independent and returns (name, ok, reason). The set is the
    union of the Spec §5.1 complete set, the Prompt §14 on-disk rehash checks,
    and the freshness checks (review HEAD == current HEAD, HEAD !=
    task_start_commit, review hashes bound to state). check_stop (read-only) and
    workflow_complete_task both consume this aggregation.
    """
    reasons: list[tuple[str, bool, str]] = []
    try:
        state = engine._load_state()
    except store.StateError as exc:
        return [("_state_loadable", False, f"state.json not loadable: {exc}")]

    resolver = engine._resolver(state)
    wt = engine.roots.run_worktree_root

    # worktree clean + untracked
    try:
        clean = gitops.is_clean(wt)
    except gitops.GitError as exc:
        reasons.append(("worktree_clean", False, f"cannot read worktree status: {exc}"))
        clean = False
    else:
        reasons.append((
            "worktree_clean", clean,
            "" if clean else "worktree has staged/unstaged changes",
        ))
    try:
        untracked = gitops.untracked_files(wt)
    except gitops.GitError as exc:
        reasons.append(("no_untracked", False, f"cannot list untracked files: {exc}"))
        untracked = []
    else:
        ok_u = not untracked
        reasons.append((
            "no_untracked", ok_u,
            "" if ok_u else f"{len(untracked)} untracked file(s)",
        ))

    # current HEAD
    try:
        head = gitops.head(wt)
    except gitops.GitError as exc:
        reasons.append(("head_readable", False, f"cannot read HEAD: {exc}"))
        head = None

    # execution-result READY_FOR_REVIEW + head bound
    exec_ok = False
    exec_head = None
    try:
        exec_obj = json.loads(resolver.read("execution-result.json"))
        exec_head = exec_obj.get("head_commit")
        exec_ok = exec_obj.get("status") == "READY_FOR_REVIEW"
        reasons.append((
            "exec_ready", exec_ok,
            "" if exec_ok else f"execution-result.status={exec_obj.get('status')!r}",
        ))
    except (artifacts.ArtifactNotFound, artifacts.ArtifactCorrupt) as exc:
        reasons.append(("exec_ready", False, f"no execution-result: {exc}"))
    head_match_exec = head is not None and head == exec_head
    reasons.append((
        "head_matches_execution", head_match_exec,
        "" if head_match_exec else "HEAD != execution_result.head_commit",
    ))

    # review decision PASS + head/hashes bound to state
    review_ok = False
    review_head = None
    rev_obj: dict = {}
    try:
        rev_obj = json.loads(resolver.read("review-result.json"))
        review_head = rev_obj.get("head_commit")
        review_ok = rev_obj.get("decision") == "PASS"
        reasons.append((
            "decision_pass", review_ok,
            "" if review_ok else f"review decision={rev_obj.get('decision')!r}",
        ))
    except (artifacts.ArtifactNotFound, artifacts.ArtifactCorrupt) as exc:
        reasons.append(("decision_pass", False, f"no review-result: {exc}"))

    head_match_review = head is not None and review_head is not None and head == review_head
    reasons.append((
        "review_head_matches_head", head_match_review,
        "" if head_match_review else "HEAD != review_result.head_commit",
    ))
    for field in ("spec_sha256", "plan_sha256", "task_manifest_sha256"):
        review_sha = rev_obj.get(field)
        state_sha = getattr(state, field, None)
        ok = review_sha is not None and state_sha is not None and review_sha == state_sha
        reasons.append((
            f"review_{field}_matches_state", ok,
            "" if ok else f"review {field} != state ({review_sha!r} vs {state_sha!r})",
        ))

    # on-disk artifact rehash vs state (Prompt §14; Review §五)
    for name in artifacts.LOCKED_ARTIFACT_NAMES:
        field = artifacts.NAME_TO_STATE_SHA[name]
        locked = getattr(state, field, None)
        if locked is None:
            reasons.append((f"rehash_{name}", False, f"{name} not locked in state"))
            continue
        try:
            current = resolver.hash_of(name)
        except (artifacts.ArtifactNotFound, artifacts.ArtifactCorrupt) as exc:
            reasons.append((f"rehash_{name}", False, f"{name} unreadable/corrupt: {exc}"))
            continue
        ok = current == locked
        reasons.append((
            f"rehash_{name}", ok,
            "" if ok else f"{name} on-disk sha {current!r} != state {locked!r}",
        ))

    # state is REVIEWING_TASK (complete requires it)
    reasons.append((
        "state_reviewing", state.state == "REVIEWING_TASK",
        "" if state.state == "REVIEWING_TASK" else f"state={state.state!r}",
    ))

    # head != task_start_commit (a real change was made)
    tsc = getattr(state, "task_start_commit", None)
    head_changed = head is not None and tsc is not None and head != tsc
    reasons.append((
        "head_changed_since_start", head_changed,
        "" if head_changed else "HEAD == task_start_commit (no change made)",
    ))

    return reasons
