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
    approval,
    artifacts,
    canonical,
    gitops,
    ids,
    manifest,
    policy_change,
    prompts,
    review_bundle,
    schemas,
    store,
)
from zcode_agentflow.adapters.verification import run_verification
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

    # ==================================================================
    # Tool 3: workflow_approve_spec (HUMAN GATE 1; Spec §5.4; Task 5.1)
    # ==================================================================

    def workflow_approve_spec(self, *, expected_revision: int) -> dict:
        """READY_FOR_SPEC_APPROVAL → PLANNING. HUMAN GATE 1: write a trusted
        approval record (approval.record_approval), lock spec_sha256, advance to
        PLANNING. The approval method is resolved from the effective_config
        (strong zcode_tool_confirmation OR non_adversarial_convention fallback);
        ``None`` (no strong + no fallback) raises ApprovalUnavailableError —
        never silently degrades (Spec §5.4 / G6).
        """
        state = self._load_state()
        op_id = self._op_id("workflow_approve_spec", expected_revision)
        # resolve the approval method ONCE (the effective_config was fixed at
        # startup by validate_capabilities; do not re-derive here).
        mode = self.effective_config.get("effective_approval_mode")
        # the canonical strong method; the fallback string maps to the constant.
        if mode == "zcode_tool_confirmation" or mode == "prefer_zcode_tool_confirmation":
            effective = approval.STRONG
        elif mode == "non_adversarial_convention":
            effective = approval.FALLBACK
        else:
            effective = None  # triggers ApprovalUnavailableError below

        request = {
            "tool": "workflow_approve_spec",
            "args": {"expected_revision": expected_revision,
                     "spec_sha256": state.spec_sha256},
        }
        br = self._begin(
            tool="workflow_approve_spec", operation_id=op_id, request=request,
            expected_revision=expected_revision,
            intermediate_state="PLANNING", replay_policy="SAFE_READ",
        )
        try:
            # record_approval writes the trusted record (raises if mode is None).
            rec = approval.record_approval(
                control_root=self.roots.control_root,
                run_id=state.run_id, gate="SPEC",
                artifact_sha256=state.spec_sha256,
                commit_at_approval=state.base_commit or "",
                effective_approval_mode=effective,
            )
            committed = self.store.finish_operation(
                operation_id=op_id, lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state="PLANNING", staged_artifacts={}, result=rec,
            )
            return {
                "state": committed["state"], "revision": committed["revision"],
                "approval_method": rec["approval_method"],
                "spec_sha256": state.spec_sha256,
                "operation_id": op_id,
            }
        except BaseException:
            self.store.fail_operation(
                operation_id=op_id, lease_token=br.lease_token,
                reason="workflow_approve_spec failure",
            )
            raise

    # ==================================================================
    # Tool 4: planner_create_plan (exactly-one-task via wrapper; P0-8, P0-5)
    # ==================================================================

    def planner_create_plan(self, *, approved_spec_sha256: str,
                            expected_revision: int) -> dict:
        """PLANNING → READY_FOR_EXECUTION. Codex read-only produces the single-
        task plan; approved_spec_sha256 must match the locked spec. The RAW
        plan-proposal wrapper is validated (P0-5): a multi-task goal fails
        cleanly (BLOCKED/NEEDS_USER_INPUT or RAW-schema rejection) and is NEVER
        silently reduced to tasks[0]. PLAN.md + task-manifest.json are promoted
        as a new generation (inheriting SPEC); hashes from the resolver.
        """
        from zcode_agentflow.adapters.codex import CodexAdapter

        state = self._load_state()
        # validate the approved sha BEFORE beginning (do not spend an adapter call
        # on a caller that does not know the locked spec).
        if approved_spec_sha256 != state.spec_sha256:
            raise WorkflowError(
                f"approved_spec_sha256 {approved_spec_sha256!r} != locked "
                f"spec_sha256 {state.spec_sha256!r}"
            )
        op_id = self._op_id("planner_create_plan", expected_revision)
        agent_context = self._resolved_agent_context()
        spec_markdown = self._resolver(state).read_text("SPEC.md")
        prompt = prompts.render(
            "planner-plan.md",
            approved_spec_sha256=state.spec_sha256,
            spec_markdown=spec_markdown,
            agent_context=agent_context,
        )
        request = {
            "kind": "create_plan",
            "prompt": prompt,
            "approved_spec_sha256": approved_spec_sha256,
            "schema_path": str(schemas.SCHEMAS_DIR / "plan-proposal.schema.json"),
        }
        br = self._begin(
            tool="planner_create_plan", operation_id=op_id, request=request,
            expected_revision=expected_revision,
            intermediate_state="PLANNING", replay_policy="COSTFUL_READ",
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
                    f"planner_create_plan adapter failed (exit={result.exit_code})"
                )
            wrapper = result.parsed
            status = wrapper.get("status")
            # P0-5: a non-READY wrapper routes to NEEDS_HUMAN (BLOCKED) or
            # NEEDS_USER_INPUT — never silently takes a task.
            if status == "BLOCKED":
                self.store.finish_operation(
                    operation_id=op_id, lease_token=br.lease_token,
                    expected_revision=br.revision_after_begin,
                    next_state="NEEDS_HUMAN", staged_artifacts={}, result=wrapper,
                )
                return {"status": "BLOCKED", "block_reason": wrapper.get("block_reason"),
                        "operation_id": op_id}
            if status == "NEEDS_USER_INPUT":
                self.store.finish_operation(
                    operation_id=op_id, lease_token=br.lease_token,
                    expected_revision=br.revision_after_begin,
                    next_state="NEEDS_USER_INPUT", staged_artifacts={}, result=wrapper,
                )
                return {"status": "NEEDS_USER_INPUT",
                        "questions": wrapper.get("questions", []),
                        "operation_id": op_id}
            if status != "READY_FOR_EXECUTION":
                raise WorkflowError(f"unexpected wrapper status {status!r}")

            plan = wrapper["plan"]
            # P0-5 (defense-in-depth at the engine, NOT just the adapter's RAW
            # validation): a multi-task plan is NEVER silently reduced to
            # tasks[0]. The wrapper's RAW schema already enforces maxItems:1, but
            # an injected/test adapter could bypass it; the engine asserts here
            # too so the invariant holds regardless of how the wrapper arrived.
            if len(plan["tasks"]) != 1:
                raise WorkflowError(
                    f"v1 requires exactly one task; wrapper carried "
                    f"{len(plan['tasks'])} (never silently take tasks[0])"
                )
            raw_task = plan["tasks"][0]
            # assemble the FINAL plan envelope: inject run_id/base_commit/version.
            final = {
                "run_id": state.run_id,
                "status": "READY_FOR_EXECUTION",
                "plan_markdown": plan["plan_markdown"],
                "plan_version": 1,
                "base_commit": state.base_commit,
                "tasks": [raw_task],
            }
            schemas.check_plan_context(
                final, run_id=state.run_id, base_commit=state.base_commit,
                program_allowlist=self.config.get("verification", {}).get("program_allowlist", []),
                interpreter_allowlist=self.config.get("verification", {}).get("interpreter_allowlist", []),
            )
            (Path(br.staging_dir) / "out.json").write_text(
                json.dumps(final, ensure_ascii=False), encoding="utf-8"
            )

            # build the self-reference-free task manifest (manifest.py). The
            # manifest's plan_sha256 anchor = the canonical sha of PLAN.md (the
            # same value state.plan_sha256 will hold after promotion), so the
            # manifest binds to the plan bytes the run actually committed.
            plan_md_bytes = canonical.canonical_text_bytes(
                final["plan_markdown"].encode("utf-8")
            )
            plan_sha = canonical.sha256_hex(plan_md_bytes)
            man = manifest.build_manifest(final, plan_sha256=plan_sha)
            man_bytes = manifest.manifest_bytes(man)

            staged = {
                "PLAN.md": final["plan_markdown"].encode("utf-8"),
                "task-manifest.json": man_bytes,
            }
            committed = self.store.finish_operation(
                operation_id=op_id, lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state="READY_FOR_EXECUTION", staged_artifacts=staged,
                result=final,
            )
            st = self._load_state()
            # current_task = the single task id; plan/manifest shas from resolver.
            st.current_task = raw_task["id"]
            st.save(self.roots.control_root)
            assert st.plan_sha256 == self._resolver(st).hash_of("PLAN.md")
            assert st.task_manifest_sha256 == self._resolver(st).hash_of("task-manifest.json")
            return {
                "status": "READY_FOR_EXECUTION",
                "state": committed["state"], "revision": committed["revision"],
                "current_task": raw_task["id"],
                "plan_sha256": st.plan_sha256,
                "task_manifest_sha256": st.task_manifest_sha256,
                "operation_id": op_id,
            }
        except BaseException:
            self.store.fail_operation(
                operation_id=op_id, lease_token=br.lease_token,
                reason="planner_create_plan adapter/validate failure",
            )
            raise

    # ==================================================================
    # Tool 5: workflow_start_task (record task_start_commit == HEAD)
    # ==================================================================

    def workflow_start_task(self, *, task_id: str, expected_revision: int) -> dict:
        """READY_FOR_EXECUTION → EXECUTING_TASK. Records ``task_start_commit`` =
        the worktree HEAD at start time (the base the change audit diffs from).
        No adapter call — pure bookkeeping under one begin/finish transaction.
        """
        ids.validate_task_id(task_id)
        state = self._load_state()
        op_id = self._op_id("workflow_start_task", expected_revision)
        request = {"tool": "workflow_start_task",
                   "args": {"task_id": task_id}}
        br = self._begin(
            tool="workflow_start_task", operation_id=op_id, request=request,
            expected_revision=expected_revision,
            intermediate_state="EXECUTING_TASK", replay_policy="SAFE_READ",
        )
        try:
            start_commit = gitops.head(self.roots.run_worktree_root)
            committed = self.store.finish_operation(
                operation_id=op_id, lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state="EXECUTING_TASK", staged_artifacts={},
                result={"task_id": task_id, "task_start_commit": start_commit},
            )
            # record task_start_commit in state (authoritative base for the audit).
            st = self._load_state()
            st.task_start_commit = start_commit
            st.save(self.roots.control_root)
            return {
                "state": committed["state"], "revision": committed["revision"],
                "task_start_commit": start_commit, "operation_id": op_id,
            }
        except BaseException:
            self.store.fail_operation(
                operation_id=op_id, lease_token=br.lease_token,
                reason="workflow_start_task failure",
            )
            raise

    # ==================================================================
    # Tool 6: workflow_verify_task (verification + change audit; Prompt §16)
    # ==================================================================

    def workflow_verify_task(self, *, task_id: str, expected_revision: int) -> dict:
        """EXECUTING_TASK → VERIFYING_TASK (pass) / NEEDS_HUMAN (any failure).

        Two independent policy layers (Spec §5.2/§5.3):
          1. the verification command policy (run_verification over each
             task.verification_commands);
          2. the git change policy (policy_change.audit over
             task_start_commit..HEAD).

        The worktree MUST be clean + no untracked at verify time (gitops). Any
        failure (non-zero exit, unauthorized change, dirty tree) routes the run
        to NEEDS_HUMAN. On full success the execution-result is promoted as a new
        generation (inheriting SPEC/PLAN/manifest) with status READY_FOR_REVIEW.
        """
        ids.validate_task_id(task_id)
        state = self._load_state()
        op_id = self._op_id("workflow_verify_task", expected_revision)
        request = {"tool": "workflow_verify_task",
                   "args": {"task_id": task_id}}
        br = self._begin(
            tool="workflow_verify_task", operation_id=op_id, request=request,
            expected_revision=expected_revision,
            intermediate_state="VERIFYING_TASK", replay_policy="UNSAFE_WRITE",
        )
        try:
            wt = self.roots.run_worktree_root
            resolver = self._resolver(state)
            # read the task from the manifest (the review-relevant single task).
            man = json.loads(resolver.read("task-manifest.json"))
            task = man["tasks"][0]

            # worktree must be clean + no untracked at verify time.
            clean = gitops.is_clean(wt)
            untracked = gitops.untracked_files(wt)

            # run each verification command (run_verification; structured).
            ver_results = []
            ver_failed = False
            for cmd in task.get("verification_commands", []):
                vr = run_verification(
                    command=cmd, worktree_root=wt,
                    program_allowlist=self.config.get("verification", {}).get("program_allowlist", []),
                    interpreter_allowlist=self.config.get("verification", {}).get("interpreter_allowlist", []),
                    operation_dir=Path(br.staging_dir) / "raw",
                )
                ver_results.append({
                    "command": cmd["program"], "exit_code": vr["exit_code"],
                    "duration_ms": 0,
                })
                if vr["exit_code"] != 0:
                    ver_failed = True

            # change audit (policy_change) over task_start_commit..HEAD.
            base = state.task_start_commit or state.base_commit
            head = gitops.head(wt)
            audit = policy_change.audit(
                worktree=wt, base_commit=base, head_commit=head,
                declared_files=task.get("declared_files", []),
                planned_changes=task.get("planned_changes", {}),
                config=self.config,
            )

            # assemble the execution-result.
            changed = gitops.changed_files_z(wt, base, head)
            status = "READY_FOR_REVIEW"
            if ver_failed or not clean or untracked or audit["needs_human"]:
                status = "VERIFICATION_FAILED"
            exec_result = {
                "run_id": state.run_id, "task_id": task_id, "status": status,
                "base_commit": base, "head_commit": head,
                "worktree_clean": clean,
                "untracked_files": untracked,
                "verification": ver_results,
                "changed_files": changed,
                "change_audit": {
                    "deleted_files": audit["deleted_files"],
                    "dependency_files_changed": audit["dependency_files_changed"],
                    "migrations_added": audit["migrations_added"],
                    "undeclared_files_changed": audit["undeclared_files_changed"],
                },
            }
            # validate the FINAL execution-result structurally.
            schemas.validate_against(exec_result, "execution-result")
            (Path(br.staging_dir) / "out.json").write_text(
                json.dumps(exec_result, ensure_ascii=False), encoding="utf-8"
            )

            next_state = "VERIFYING_TASK" if status == "READY_FOR_REVIEW" else "NEEDS_HUMAN"
            committed = self.store.finish_operation(
                operation_id=op_id, lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state=next_state,
                staged_artifacts={"execution-result.json": canonical.canonical_json_bytes(exec_result)},
                result=exec_result,
            )
            st = self._load_state()
            assert st.execution_result_sha256 == self._resolver(st).hash_of("execution-result.json")
            # consistency #3: record the VERIFIED head so reviewer_review_task can
            # bind against it (HEAD == verified_head_commit pre-review).
            if status == "READY_FOR_REVIEW":
                st.verified_head_commit = head
                st.save(self.roots.control_root)
            return {
                "status": status, "state": committed["state"],
                "revision": committed["revision"],
                "head_commit": head, "operation_id": op_id,
            }
        except BaseException:
            self.store.fail_operation(
                operation_id=op_id, lease_token=br.lease_token,
                reason="workflow_verify_task failure",
            )
            raise

    # ==================================================================
    # Tool 7: reviewer_review_task (Review Bundle + hash injection; P0-5)
    # ==================================================================

    def reviewer_review_task(self, *, task_id: str, expected_revision: int) -> dict:
        """VERIFYING_TASK → REVIEWING_TASK (PASS) / NEEDS_HUMAN (FAIL/REPLAN/BLOCKED).

        consistency #3 (load-bearing): BEFORE the review call the engine asserts
        ``HEAD == execution_result.head_commit`` AND ``HEAD ==
        state.verified_head_commit``. It does NOT consult
        ``last_reviewed_commit`` pre-review — that field is set ONLY after a
        successful PASS. The Review Bundle is assembled (read-only artifacts via
        the resolver) and Grok reviews it; the RAW decision is validated, then
        authoritative context (run_id/task_id/commits/hashes) is INJECTED from
        state and the FINAL review-result is validated. On PASS,
        ``state.last_reviewed_commit = review head``.
        """
        from zcode_agentflow.adapters.grok import GrokAdapter

        ids.validate_task_id(task_id)
        state = self._load_state()
        op_id = self._op_id("reviewer_review_task", expected_revision)
        request = {"tool": "reviewer_review_task",
                   "args": {"task_id": task_id}}
        br = self._begin(
            tool="reviewer_review_task", operation_id=op_id, request=request,
            expected_revision=expected_revision,
            intermediate_state="REVIEWING_TASK", replay_policy="COSTFUL_READ",
        )
        try:
            wt = self.roots.run_worktree_root
            resolver = self._resolver(state)
            # consistency #3: bind HEAD to the verified head BEFORE review.
            head = gitops.head(wt)
            exec_obj = json.loads(resolver.read("execution-result.json"))
            exec_head = exec_obj["head_commit"]
            verified = getattr(state, "verified_head_commit", None)
            if head != exec_head or (verified is not None and head != verified):
                raise WorkflowError(
                    f"HEAD {head!r} != execution_result.head_commit {exec_head!r}"
                    f" / verified_head_commit {verified!r}; refusing to review a "
                    f"moved tree (route to NEEDS_HUMAN)"
                )
            # worktree must be clean at review time (GrokAdapter re-checks too).
            if not gitops.is_clean(wt):
                raise WorkflowError(
                    "worktree not clean at review time; route to NEEDS_HUMAN"
                )

            # read the task + assemble the immutable Review Bundle (read-only).
            man = json.loads(resolver.read("task-manifest.json"))
            task = man["tasks"][0]
            agent_context = self._resolved_agent_context()
            review_prompt = prompts.render("reviewer.md", agent_context=agent_context)
            bdir = review_bundle.assemble(
                operation_dir=br.staging_dir, resolver=resolver,
                run_worktree_root=wt, base_commit=state.base_commit,
                head_commit=head, task=task, exec_result=exec_obj,
                resolved_agent_context=agent_context, review_prompt=review_prompt,
                review_round=state.review_round + 1,
            )

            grok = self.adapters.get("grok") or GrokAdapter()
            grok_req = {
                "bundle_dir": str(bdir),
                "repo_root": str(self.roots.target_repo_root),
                "schema_path": str(schemas.SCHEMAS_DIR / "review-decision.schema.json"),
                "proxy": self.config.get("reviewer", {}).get("proxy", {}),
            }
            result = grok.invoke(
                request=grok_req, repo_cwd=bdir, operation_dir=br.staging_dir,
                timeout=self._adapter_timeout("reviewer"),
            )
            if result.exit_code != 0 or result.parsed is None:
                raise WorkflowError(
                    f"reviewer_review_task adapter failed (exit={result.exit_code})"
                )
            raw = result.parsed
            # inject authoritative context from state (P0-5: never trust model).
            final = {
                "run_id": state.run_id, "task_id": task_id,
                "base_commit": state.base_commit, "head_commit": head,
                "spec_sha256": state.spec_sha256, "plan_sha256": state.plan_sha256,
                "task_manifest_sha256": state.task_manifest_sha256,
                "plan_version": state.plan_version,
                "decision": raw["decision"],
                "spec_compliance": raw["spec_compliance"],
                "plan_compliance": raw["plan_compliance"],
                "tests_sufficient": raw["tests_sufficient"],
                "issues": raw.get("issues", []),
                "replan_required": raw["replan_required"],
            }
            schemas.validate_review(final)
            (Path(br.staging_dir) / "out.json").write_text(
                json.dumps(final, ensure_ascii=False), encoding="utf-8"
            )

            decision = final["decision"]
            next_state = "REVIEWING_TASK" if decision == "PASS" else "NEEDS_HUMAN"
            committed = self.store.finish_operation(
                operation_id=op_id, lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state=next_state,
                staged_artifacts={"review-result.json": canonical.canonical_json_bytes(final)},
                result=final,
            )
            st = self._load_state()
            assert st.review_result_sha256 == self._resolver(st).hash_of("review-result.json")
            # consistency #3: last_reviewed_commit is set ONLY after a PASS review.
            if decision == "PASS":
                st.last_reviewed_commit = head
                st.review_round = state.review_round + 1
                st.save(self.roots.control_root)
            return {
                "decision": decision, "state": committed["state"],
                "revision": committed["revision"], "head_commit": head,
                "operation_id": op_id,
            }
        except BaseException:
            self.store.fail_operation(
                operation_id=op_id, lease_token=br.lease_token,
                reason="reviewer_review_task failure",
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
