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
    agents_context,
    artifacts,
    gitops,
    ids,
    prompts,
    schemas,
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
    # Operation-id helpers (Task 6.2; Prompt §15; Review §二)
    # ------------------------------------------------------------------

    def _next_attempt(self, tool: str, revision: int) -> int:
        """Derive the next attempt counter for (run, tool, revision) from disk.

        The operations directory is the durable record of past attempts. We count
        existing op dirs whose id matches ``<run_id>__<tool>__r<revision>__a<n>``
        and return max+1, so a re-entry auto-gets a FRESH id (never a replay of a
        prior committed op). Starts at 1 when none exist.
        """
        prefix = f"{self._run_id()}__{tool}__r{revision}__a"
        ops_dir = Path(self.roots.control_root) / store.OPS_DIRNAME
        if not ops_dir.is_dir():
            return 1
        best = 0
        for p in ops_dir.iterdir():
            name = p.name
            if name.startswith(prefix):
                tail = name[len(prefix):]
                if tail.isdigit():
                    best = max(best, int(tail))
        return best + 1

    def _run_id(self) -> str:
        return self._load_state().run_id

    def _op_id(self, tool: str, revision: int, attempt: int | None = None) -> str:
        a = attempt if attempt is not None else self._next_attempt(tool, revision)
        return ids.operation_id(
            run_id=self._run_id(), tool=tool, revision=revision, attempt=a
        )

    # ------------------------------------------------------------------
    # Agent context (Task 5.3) — resolved once per run, read from control_root.
    # ------------------------------------------------------------------

    def _resolved_agent_context(self) -> str:
        """Resolve (or read) the agent context for this run and return its text.

        ``agents_context.resolve`` composes the repo AGENTS.md + the plugin
        template and writes ``control_root/resolved-agent-context.md``. It is
        idempotent (byte-identical on repeat). We resolve here so the inlined
        context is fresh for each planner/reviewer call (Prompt §19).
        """
        path = agents_context.resolve(
            target_repo=self.roots.target_repo_root,
            plugin_root=self.roots.plugin_root,
            control_root=self.roots.control_root,
        )
        return path.read_text(encoding="utf-8")

    # ==================================================================
    # Tool 1: workflow_init (wraps store.init_run — Task 3.7 owns the pointer)
    # ==================================================================

    def workflow_init(self, *, target_repo: str, goal: str,
                      run_id: str | None = None) -> dict:
        """NEW → INITIALIZING_WORKTREE. Delegates to ``store.init_run`` (Task 3.7),
        which owns the registry critical section + the initial active-run pointer
        write (errata: workflow_init does NOT write the pointer itself — no double
        write). Returns ``{run_id, base_commit, control_root, worktree_root,
        branch}``.

        ``run_id`` is generated when not supplied (Phase 1 ids.new_run_id). The
        worktree is created OUTSIDE the repo (Spec §2.2 four-roots).
        """
        rid = run_id or ids.new_run_id()
        # control_root / worktree_root live under a per-run subdir of the
        # control/worktree bases derived from the roots the Engine was built with.
        # init_run takes absolute control_root + worktree_root paths.
        return store.init_run(
            user_data_root=self.user_data_root,
            target_repo=target_repo,
            control_root=self.roots.control_root,
            worktree_root=self.roots.run_worktree_root,
            run_id=rid,
            plugin_root=self.roots.plugin_root,
            goal=goal,
        )

    # ==================================================================
    # Tool 2: planner_draft_spec (answers re-entry; Review §二; Prompt §15)
    # ==================================================================

    def planner_draft_spec(self, *, goal: str, answers: str | None = None,
                           expected_revision: int | None = None) -> dict:
        """INITIALIZING_WORKTREE/NEEDS_USER_INPUT → DRAFTING_SPEC →
        (questions non-empty → NEEDS_USER_INPUT | empty → READY_FOR_SPEC_APPROVAL).

        begin → CodexAdapter (no lock) → finish. The op id is revision/attempt-
        bound so re-entry with answers gets a FRESH id (never a replay). The raw
        spec-draft is RAW-validated by the adapter; here we inject run_id + status
        from state and validate the FINAL spec-envelope (Task 2.7). SPEC.md is
        promoted as a new generation; ``state.spec_sha256 = resolver.hash_of``.
        """
        from zcode_agentflow.adapters.codex import CodexAdapter

        state = self._load_state()
        rev = state.revision
        # expected_revision defaults to the current revision (the CAS the caller
        # must have observed). The Store compares it inside begin.
        if expected_revision is None:
            expected_revision = rev
        op_id = self._op_id("planner_draft_spec", rev)

        # resolve agent context (inlined into the stdin prompt — never a path).
        agent_context = self._resolved_agent_context()
        prompt = prompts.render(
            "planner-draft.md",
            goal=goal,
            answers=answers or "",
            agent_context=agent_context,
        )
        request = {
            "kind": "draft_spec",
            "prompt": prompt,
            "goal": goal,
            "answers": answers,
            "state": state.state,
            "expected_revision": expected_revision,
            # shipped RAW schema constrains the model's final answer
            "schema_path": str(schemas.SCHEMAS_DIR / "spec-draft-output.schema.json"),
        }

        br = self._begin(
            tool="planner_draft_spec", operation_id=op_id, request=request,
            expected_revision=expected_revision,
            intermediate_state="DRAFTING_SPEC", replay_policy="COSTFUL_READ",
        )
        try:
            codex = self.adapters.get("codex") or CodexAdapter()
            result = codex.invoke(
                request=request, repo_cwd=self.roots.run_worktree_root,
                operation_dir=br.staging_dir,
                timeout=self._adapter_timeout("planner"),
            )
            if result.exit_code != 0 or result.parsed is None:
                raise WorkflowError(
                    f"planner_draft_spec adapter failed (exit={result.exit_code})"
                )
            raw = result.parsed
            # build the FINAL spec envelope: inject run_id + status from the
            # questions (non-empty => NEEDS_USER_INPUT; empty => READY_FOR_SPEC_APPROVAL).
            questions = list(raw.get("questions", []))
            final_status = "NEEDS_USER_INPUT" if questions else "READY_FOR_SPEC_APPROVAL"
            final = {
                "run_id": state.run_id,
                "status": final_status,
                # the run's goal (the caller's) is authoritative, NOT the model's
                # echo — the FINAL envelope carries exactly the run goal so the
                # context invariant (final.goal == run goal) holds (Task 2.7).
                "goal": goal,
                "spec_markdown": raw["spec_markdown"],
                "questions": questions,
                "assumptions": raw.get("assumptions", []),
            }
            schemas.check_spec_context(final, run_id=state.run_id, goal=goal)
            # persist the FINAL envelope as the op's out.json (the adapter wrote
            # the RAW; the authoritative FINAL is what the engine assembles).
            (Path(br.staging_dir) / "out.json").write_text(
                json.dumps(final, ensure_ascii=False), encoding="utf-8"
            )

            next_state = "NEEDS_USER_INPUT" if questions else "READY_FOR_SPEC_APPROVAL"
            spec_bytes = final["spec_markdown"].encode("utf-8")
            committed = self.store.finish_operation(
                operation_id=op_id, lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state=next_state,
                staged_artifacts={"SPEC.md": spec_bytes},
                result=final,
            )
            # state.spec_sha256 is filled by prepare_generation (finish); assert
            # it equals the resolver hash (P0-3: read via resolver).
            st = self._load_state()
            assert st.spec_sha256 == self._resolver(st).hash_of("SPEC.md")
            return {
                "status": final_status,
                "questions": questions,
                "spec_sha256": st.spec_sha256,
                "state": committed["state"],
                "revision": committed["revision"],
                "operation_id": op_id,
            }
        except BaseException:
            # P0-2 abnormal path: route to NEEDS_HUMAN, never strand mid-external.
            self.store.fail_operation(
                operation_id=op_id, lease_token=br.lease_token,
                reason="planner_draft_spec adapter/validate failure",
            )
            raise

    # ------------------------------------------------------------------
    # config-derived adapter timeout
    # ------------------------------------------------------------------

    def _adapter_timeout(self, section: str) -> int:
        """Return the timeout (seconds) for an adapter section
        (``planner`` / ``reviewer``). Defaults to 120 if unset."""
        node = self.config.get(section, {}) if self.config else {}
        return int(node.get("timeout_sec", 120))

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
