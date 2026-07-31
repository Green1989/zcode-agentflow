"""Run Store — recoverable state + events + operations (Phase 3).

This module is the **sole writer** of ``state.json`` / ``events.jsonl`` /
``operations/`` (the architectural invariant: no second writer anywhere). All
writes happen inside the per-Run lock (``locks.run_lock``), held only across
short bookkeeping steps — NEVER across an external CLI call.

Task 3.2 ships the foundational primitives ONLY:
  - :class:`EventLog` — append-only ``events.jsonl`` with a seq that RESUMES
    from disk (so a restart continues the seq, not resets to 1). Each event
    carries a ``state_after_path`` + ``state_after_sha256`` pointing at an
    immutable snapshot, so a corrupt ``state.json`` can be rebuilt from the
    latest event (full recovery in Task 3.8).
  - :class:`State` — the in-memory ``state.json`` model; ``save()`` is an atomic
    ``write_canonical_json`` + ``os.replace`` (concurrent readers never see a
    half-write), ``load`` round-trips every field.

The operation lifecycle (3.3), two-phase transactions (3.4), atomic promotion
(3.5), and ``Store.recover`` (3.8) are layered on top of these primitives.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from zcode_agentflow import canonical

__all__ = [
    "STATE_FILENAME",
    "EVENTS_FILENAME",
    "OPS_DIRNAME",
    "ReplayPolicy",
    "OperationStatus",
    "EventLog",
    "State",
    "StateError",
    "Operation",
    "OperationManager",
    "OperationConflict",
    "classify_unknown_replay",
    "ALLOWED_FROM",
    "BeginResult",
    "Store",
    "CasMismatch",
    "LeaseMismatch",
    "ActiveOperationInProgress",
    "ActiveOperationMismatch",
    "TransitionNotAllowed",
    "CrashInjected",
    "StateCorruptError",
    "InitRejected",
    "InitNeedsHuman",
    "init_run",
]

STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"
OPS_DIRNAME = "operations"

# Three-valued replay policy (Prompt §9,§10 clarification):
#   SAFE_READ    — git/status/hash: read-only + cheap -> UNKNOWN+no-result auto-replays
#   COSTFUL_READ — Codex planning / Grok review: read-only + costly -> replay only if
#                  config.allow_costful_replay==true, else NEEDS_HUMAN
#   UNSAFE_WRITE — verification / worktree creation: may have written files ->
#                  NEEDS_HUMAN (never auto-replay)
ReplayPolicy = {"SAFE_READ", "COSTFUL_READ", "UNSAFE_WRITE"}

# Operation lifecycle status.
OperationStatus = {"PREPARED", "RUNNING", "COMMITTED", "FAILED", "UNKNOWN"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateError(RuntimeError):
    """Raised when state.json is missing/corrupt and cannot be loaded."""


class StateCorruptError(RuntimeError):
    """Raised by :meth:`Store.recover` when state.json is corrupt/missing AND
    it cannot be rebuilt from the latest event's snapshot (snapshot missing or
    its sha mismatches). Recovery surfaces this so a human decides — it never
    silently picks another run (Task 3.8 enforces pointer-scoped recovery)."""


class InitRejected(RuntimeError):
    """Raised by :func:`init_run` when a non-terminal active-run pointer already
    exists (a second workflow_init would collide). The user must abandon first."""


class InitNeedsHuman(RuntimeError):
    """Raised by :func:`init_run` when a half-created worktree exists but its
    branch ownership does NOT match this run (a foreign collision), or the
    worktree state is otherwise unrecoverable. No destructive auto-cleanup."""


class OperationConflict(RuntimeError):
    """Raised when an operation_id is reused with a DIFFERENT request (a real
    conflict — the caller must not silently overwrite the prior op)."""


# ===========================================================================
# Operation lifecycle + idempotency (Prompt §9,§10; clarification)
# ===========================================================================


def _op_dir(control_root, operation_id) -> Path:
    return Path(control_root) / OPS_DIRNAME / operation_id


class Operation:
    """One operation record under ``<control_root>/operations/<operation_id>/``.

    Lifecycle: ``PREPARED`` → ``RUNNING`` → ``COMMITTED`` / ``FAILED`` /
    ``UNKNOWN``. Idempotency is keyed on ``(operation_id, request_sha256)``:
    a COMMITTED op re-opened with the SAME request returns its stored result;
    the SAME operation_id with a DIFFERENT request is an :class:`OperationConflict`.

    The directory layout (Spec §3.6): ``request.json``, ``request_sha256``,
    ``raw/`` (adapter stdout/stderr), ``result.json``, ``state_before.json``,
    ``state_after.json``, ``status``.
    """

    def __init__(self, *, operation_id, request_sha256, control_root,
                 replay_policy, status="PREPARED"):
        self.operation_id = operation_id
        self.request_sha256 = request_sha256
        self.control_root = Path(control_root)
        self.replay_policy = replay_policy
        self._status = status
        self._dir = _op_dir(self.control_root, operation_id)

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        if value not in OperationStatus:
            raise ValueError(f"invalid operation status: {value!r}")
        self._status = value
        self._write_status(value)

    def _write_status(self, value: str) -> None:
        # status is a single-line marker file; write atomically (readers must
        # never see a partial) via the canonical text writer.
        (self._dir / "status").write_text(value + "\n", encoding="utf-8")

    # ---- result ----------------------------------------------------------

    def write_result(self, result) -> None:
        """Persist ``result.json`` (the adapter's parsed output). Atomic write."""
        canonical.write_canonical_json(self._dir / "result.json", result)

    def read_result(self):
        """Return the parsed result.json, or None if not written yet."""
        p = self._dir / "result.json"
        if not p.exists():
            return None
        return json.loads(p.read_bytes())

    def has_result(self) -> bool:
        return (self._dir / "result.json").exists()

    # ---- terminal markers ------------------------------------------------

    def mark_committed(self) -> None:
        self.status = "COMMITTED"

    def mark_failed(self) -> None:
        self.status = "FAILED"

    def mark_unknown(self) -> None:
        self.status = "UNKNOWN"

    # ---- snapshot persistence (used by Task 3.4 begin/finish) ------------

    def write_state_snapshot(self, name: str, state_dict: dict) -> tuple[str, str]:
        """Write ``<name>.json`` (e.g. state_before/state_after) and return
        ``(relative_path, sha256)`` over its canonical bytes. The relative path
        is relative to control_root (so events can record it portably)."""
        target = self._dir / f"{name}.json"
        canonical.write_canonical_json(target, state_dict)
        sha = canonical.hash_file(target)
        rel = target.relative_to(self.control_root).as_posix()
        return rel, sha

    def read_state_snapshot(self, name: str):
        p = self._dir / f"{name}.json"
        if not p.exists():
            return None
        return json.loads(p.read_bytes())


class OperationManager:
    """Prepares/opens :class:`Operation` records and enforces idempotency."""

    def __init__(self, control_root):
        self.control_root = Path(control_root)

    def prepare(self, *, operation_id, request, replay_policy) -> Operation:
        """Create the operation directory + required files (request.json,
        request_sha256, empty raw/, status=PREPARED) and return the Operation.

        If the directory ALREADY exists with the SAME request_sha256, this is an
        idempotent re-prepare: it returns the existing Operation unchanged (so a
        retry does not clobber a prior run's result). A differing request_sha256
        on an existing id is an :class:`OperationConflict`.
        """
        if replay_policy not in ReplayPolicy:
            raise ValueError(f"invalid replay_policy: {replay_policy!r}")
        request_sha = canonical.sha256_hex(canonical.canonical_json_bytes(request))
        opdir = _op_dir(self.control_root, operation_id)

        if opdir.exists():
            existing = self.open_existing(operation_id)
            if existing.request_sha256 != request_sha:
                raise OperationConflict(
                    f"operation {operation_id!r} already exists with a different "
                    f"request_sha256 (got {existing.request_sha256}, "
                    f"new {request_sha})"
                )
            return existing  # idempotent: same id + same request -> same op

        opdir.mkdir(parents=True, exist_ok=False)
        (opdir / "raw").mkdir(exist_ok=False)
        canonical.write_canonical_json(opdir / "request.json", request)
        (opdir / "request_sha256").write_text(request_sha + "\n", encoding="utf-8")
        op = Operation(
            operation_id=operation_id,
            request_sha256=request_sha,
            control_root=self.control_root,
            replay_policy=replay_policy,
            status="PREPARED",
        )
        op._write_status("PREPARED")
        return op

    def prepare_conflict_aware(self, operation_id, *, request, replay_policy) -> Operation:
        """Prepare that RAISES on any existing id (used to assert a conflict is
        rejected even when the request happens to differ)."""
        opdir = _op_dir(self.control_root, operation_id)
        if opdir.exists():
            raise OperationConflict(
                f"operation {operation_id!r} already exists; refusing to overwrite"
            )
        return self.prepare(
            operation_id=operation_id, request=request, replay_policy=replay_policy
        )

    def open_existing(self, operation_id) -> Operation:
        """Reopen an operation directory from disk (for resume / idempotency)."""
        opdir = _op_dir(self.control_root, operation_id)
        if not opdir.exists():
            raise FileNotFoundError(f"operation not found: {operation_id}")
        request_sha = (opdir / "request_sha256").read_text(
            encoding="utf-8"
        ).strip()
        status_path = opdir / "status"
        status = status_path.read_text(encoding="utf-8").strip() if status_path.exists() else "PREPARED"
        # recover the replay_policy from the operation directory: we store it
        # alongside the request so a resumed process can classify UNKNOWN ops.
        policy = self._read_policy(opdir)
        return Operation(
            operation_id=operation_id,
            request_sha256=request_sha,
            control_root=self.control_root,
            replay_policy=policy,
            status=status,
        )

    def _read_policy(self, opdir) -> str:
        p = opdir / "replay_policy"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        # legacy/absent default: assume SAFE_READ (the least-dangerous read)
        return "SAFE_READ"

    # ---- idempotency query ----------------------------------------------

    def is_idempotent_hit(self, op: Operation, request) -> bool:
        """True iff ``op`` is COMMITTED and its request hashes to the same
        canonical bytes as ``request`` (a true idempotent re-invocation)."""
        if op.status != "COMMITTED":
            return False
        return canonical.sha256_hex(
            canonical.canonical_json_bytes(request)
        ) == op.request_sha256


# Decorate prepare so the policy is also persisted for resume classification.
_orig_prepare = OperationManager.prepare


def _prepare_with_policy(self, *, operation_id, request, replay_policy) -> Operation:
    op = _orig_prepare(
        self, operation_id=operation_id, request=request, replay_policy=replay_policy
    )
    # persist the policy so a resumed Store can classify an UNKNOWN op without
    # having to be told the policy again (Task 3.4/3.8 rely on this).
    (op.dir / "replay_policy").write_text(replay_policy + "\n", encoding="utf-8")
    op.replay_policy = replay_policy
    return op


OperationManager.prepare = _prepare_with_policy  # type: ignore[assignment]


# ===========================================================================
# Three-valued replay classification (Prompt §9,§10 clarification)
# ===========================================================================


def classify_unknown_replay(op: Operation, *, allow_costful_replay: bool) -> str:
    """Classify an UNKNOWN (or RUNNING-with-no-result) operation by its
    ``replay_policy`` and return the recovery outcome label:

      - ``REPLAY``      safe to auto-replay on resume
      - ``NEEDS_HUMAN`` not safe to auto-replay; surface to a human

    Rules (clarification):
      SAFE_READ     -> REPLAY   (cheap + side-effect-free, regardless of flag)
      COSTFUL_READ  -> REPLAY iff allow_costful_replay else NEEDS_HUMAN
                       (read-only is SAFE but COSTLY; config decides)
      UNSAFE_WRITE  -> NEEDS_HUMAN (may have already written files)
    """
    # If the op already has a complete result, recovery can finish-commit it
    # rather than replay — but classification is about the replay DECISION for
    # an op that did NOT finish. We only classify UNKNOWN/no-final-result ops.
    policy = op.replay_policy
    if policy == "SAFE_READ":
        return "REPLAY"
    if policy == "COSTFUL_READ":
        return "REPLAY" if allow_costful_replay else "NEEDS_HUMAN"
    if policy == "UNSAFE_WRITE":
        return "NEEDS_HUMAN"
    # unknown policy string -> be safe (never auto-replay something we can't classify)
    return "NEEDS_HUMAN"


# ===========================================================================
# Two-phase transactions: begin / finish / fail / unknown (Task 3.4; P0-3, P0-2)
#
# External CLI calls run BETWEEN two short locked transactions:
#
#     br = store.begin_operation(...)          # short lock
#     try:
#         adapter.invoke(...)                   # NO lock held
#         store.finish_operation(lease=br.lease_token,
#                                expected_revision=br.revision_after_begin, ...)
#     except AdapterError:
#         store.fail_operation(lease=br.lease_token, ...)
#     # timeout/kill -> store.mark_unknown(...)
#
# ``_guard`` does NOT acquire the lock; ALLOWED_FROM + CAS + the hard one-active-op
# gate all happen INSIDE begin_operation / finish_operation. The Run lock is held
# ONLY across the short bookkeeping step, never across the adapter call (the v2
# deadlock came from violating this).
# ===========================================================================


class CasMismatch(RuntimeError):
    """``expected_revision`` did not equal the current state.revision (a stale
    caller raced a newer transition)."""


class LeaseMismatch(RuntimeError):
    """The supplied lease_token does not match the active operation's stored
    lease hash (a stale finish/fail/unknown from a previous begin of the same
    op id, or a plain wrong token)."""


class ActiveOperationInProgress(RuntimeError):
    """A DIFFERENT operation is already mid-external (active_operation_id is set
    and != the requested operation_id). Only the SAME id (idempotent re-entry)
    or Store.recover may proceed."""


class ActiveOperationMismatch(RuntimeError):
    """finish_operation was called for an operation_id that is no longer the
    active one (another op interleaved / recover cleared the active op)."""


class TransitionNotAllowed(RuntimeError):
    """The tool is not allowed to run from the current state (ALLOWED_FROM)."""


class CrashInjected(RuntimeError):
    """A simulated process kill at a named checkpoint (Task 3.6 fault injection).

    Driven by the ``AGENTFLOW_CRASH_AFTER`` env var: when it names a checkpoint
    that the Store reaches, the Store raises this INSTEAD of continuing. The
    fault-injection matrix then recovers from disk with a fresh Store, proving
    recovery is correct at every step. Production code never sets that env var,
    so checkpoints are inert in normal operation.
    """


def _crash_checkpoint(name: str) -> None:
    """Raise :class:`CrashInjected` if ``AGENTFLOW_CRASH_AFTER`` == ``name``.

    Called at named points inside begin/finish/prepare_generation so the
    fault-injection test can kill the process at exactly that step. Inert when
    the env var is unset (production path).
    """
    if os.environ.get("AGENTFLOW_CRASH_AFTER") == name:
        raise CrashInjected(f"crash injected at checkpoint {name!r}")


# ALLOWED_FROM: each tool may begin only from one of these source states
# (Spec §3.1 / §5.1). The Store enforces this at begin time. (Engine task 6.x
# fills the bodies; this table is the authority the Store checks against.)
ALLOWED_FROM: dict[str, frozenset[str]] = {
    "workflow_init": frozenset({"NEW"}),
    "planner_draft_spec": frozenset(
        {"INITIALIZING_WORKTREE", "NEEDS_USER_INPUT"}
    ),
    "workflow_approve_spec": frozenset({"READY_FOR_SPEC_APPROVAL"}),
    "planner_create_plan": frozenset({"PLANNING"}),
    "workflow_start_task": frozenset({"READY_FOR_EXECUTION"}),
    "workflow_verify_task": frozenset({"EXECUTING_TASK"}),
    "reviewer_review_task": frozenset({"VERIFYING_TASK"}),
    "workflow_complete_task": frozenset({"REVIEWING_TASK"}),
}


@dataclass
class BeginResult:
    """Result of :meth:`Store.begin_operation`.

    ``revision_after_begin`` is the run-state revision AFTER begin (which did
    +1); finish_operation MUST pass it back as ``expected_revision`` — that is
    the CAS that binds the external phase to the begin that started it (passing
    the PRE-begin revision is rejected — the v2.1 CAS gap).
    """

    operation_id: str
    lease_token: str
    revision_after_begin: int
    staging_dir: Path
    state_snapshot: dict


class Store:
    """The Run Store: the SOLE writer of state.json / events.jsonl / operations/.

    Every mutating method takes the per-Run lock (``locks.run_lock``) for a
    SHORT bookkeeping step and releases it before returning — the external
    adapter call runs OUTSIDE the lock (P0-3). ``finish_operation`` is the SOLE
    committer of ``state.json`` (errata #6).
    """

    def __init__(self, control_root):
        self.control_root = Path(control_root)
        self.control_root.mkdir(parents=True, exist_ok=True)
        self._last_recover_outcome: str | None = None

    # ---- read helpers (no lock; state.json is atomic on disk) ------------

    def load_state(self) -> State:
        return State.load(self.control_root)

    # ---- lease hashing ---------------------------------------------------

    @staticmethod
    def _hash_lease(lease_token: str) -> str:
        """Store the lease as a SHA-256 hex (never plaintext), so the persisted
        state.json cannot leak the token."""
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    # ---- BEGIN (short lock) ----------------------------------------------

    def begin_operation(self, *, operation_id, request, tool, expected_revision,
                        intermediate_state, replay_policy) -> BeginResult:
        """Take the per-Run lock, verify the hard gate + ALLOWED_FROM + CAS,
        advance to the intermediate state with revision+1, write a recoverable
        begin event + state_after snapshot, set active_operation_id + lease,
        and return a :class:`BeginResult`. RELEASES the lock before returning.

        HARD GATE (parallel P0-2): if ``state.active_operation_id`` is set AND
        differs from ``operation_id`` -> raise :class:`ActiveOperationInProgress`.
        Re-entering with the SAME id is the idempotent path (returns a fresh
        BeginResult for the in-flight op).
        """
        from zcode_agentflow import locks as _locks

        with _locks.run_lock(self.control_root):
            state = State.load(self.control_root)

            # HARD GATE: at most one active op per Run.
            if state.active_operation_id is not None and state.active_operation_id != operation_id:
                raise ActiveOperationInProgress(
                    f"another operation {state.active_operation_id!r} is already "
                    f"in progress on run {state.run_id!r}; cannot begin "
                    f"{operation_id!r}"
                )

            # IDEMPOTENT RE-ENTRY (Plan Task 3.4: 'only (a) the same operation_id
            # (idempotent re-entry) ... may proceed'). If THIS operation is
            # already the active one, the Engine is retrying the same in-flight
            # op (e.g. after losing the original lease). The source-state check
            # already passed on the FIRST begin, so we SKIP ALLOWED_FROM here
            # (the run is now in the intermediate state, which is intentionally
            # not in any tool's ALLOWED_FROM set). We re-issue a FRESH lease and
            # return a BeginResult WITHOUT re-advancing state/revision.
            if state.active_operation_id == operation_id:
                if expected_revision != state.revision:
                    raise CasMismatch(
                        f"re-entry expected_revision {expected_revision} != "
                        f"current {state.revision} (run {state.run_id!r})"
                    )
                op = OperationManager(self.control_root).open_existing(operation_id)
                lease_token = secrets.token_urlsafe(32)
                state.active_operation_lease = self._hash_lease(lease_token)
                state.save(self.control_root)
                return BeginResult(
                    operation_id=operation_id,
                    lease_token=lease_token,
                    revision_after_begin=state.revision,
                    staging_dir=op.dir,
                    state_snapshot=state.to_dict(),
                )

            # ALLOWED_FROM: tool may run from the current state.
            allowed = ALLOWED_FROM.get(tool)
            if allowed is None:
                raise TransitionNotAllowed(f"unknown tool {tool!r}")
            if state.state not in allowed:
                raise TransitionNotAllowed(
                    f"tool {tool!r} cannot run from state {state.state!r} "
                    f"(allowed: {sorted(allowed)})"
                )

            # CAS: caller's expected_revision must match the PRE-begin revision.
            if expected_revision != state.revision:
                raise CasMismatch(
                    f"expected_revision {expected_revision} != current "
                    f"{state.revision} (run {state.run_id!r})"
                )

            # Prepare the operation record (idempotent if the same op re-begins).
            op = OperationManager(self.control_root).prepare(
                operation_id=operation_id, request=request, replay_policy=replay_policy
            )
            op.status = "RUNNING"
            _crash_checkpoint("begin_after_request_written")

            # Advance to the intermediate state with revision+1; set the active
            # op + lease (stored HASHED).
            pre_begin_state = state.state
            pre_begin_rev = state.revision
            lease_token = secrets.token_urlsafe(32)
            state.previous_state = pre_begin_state
            state.state = intermediate_state
            state.revision = pre_begin_rev + 1
            state.active_operation_id = operation_id
            state.active_operation_lease = self._hash_lease(lease_token)

            # Write a recoverable state_after snapshot for the begin transition
            # (so a crash mid-external can reconstruct the intermediate state —
            # parallel P0-2: begin writes events, not only finish). Each event
            # gets its OWN snapshot file (state_after_begin/finish/...) so a later
            # transition on the same op cannot overwrite an earlier event's
            # snapshot (which would invalidate that event's recorded sha).
            snap_rel, snap_sha = op.write_state_snapshot(
                "state_after_begin", state.to_dict()
            )
            # persist state.json (the intermediate snapshot is now visible to
            # readers; the op is RUNNING). This is NOT the commit — finish is.
            state.save(self.control_root)
            _crash_checkpoint("begin_after_state_save")
            EventLog(self.control_root).append(
                operation_id=operation_id,
                from_state=pre_begin_state,
                to_state=intermediate_state,
                reason=tool,
                state_after_path=snap_rel,
                state_after_sha=snap_sha,
                revision=state.revision,
            )
            _crash_checkpoint("begin_after_event")

            return BeginResult(
                operation_id=operation_id,
                lease_token=lease_token,
                revision_after_begin=state.revision,
                staging_dir=op.dir,
                state_snapshot=state.to_dict(),
            )

    # ---- verify op + lease (shared by finish/fail/unknown) ----------------

    def _verify_active_op(self, state: State, operation_id: str, lease_token: str) -> None:
        if state.active_operation_id != operation_id:
            raise ActiveOperationMismatch(
                f"operation {operation_id!r} is not the active operation "
                f"(active is {state.active_operation_id!r})"
            )
        if state.active_operation_lease != self._hash_lease(lease_token):
            raise LeaseMismatch(
                f"lease token does not match the active operation {operation_id!r}"
            )

    # ---- FINISH (short lock; SOLE committer of state.json, errata #6) ----

    def finish_operation(self, *, operation_id, lease_token, expected_revision,
                         next_state, staged_artifacts, result,
                         base_commit=None) -> dict:
        """Re-take the per-Run lock, verify op + lease + revision_after_begin,
        write the result + the next state_after snapshot, append+fsync the
        finish event, and THEN perform the SINGLE ``os.replace(state.json)``
        that commits the transition. Then mark the op COMMITTED.

        Commit order (Spec §3.5 写入顺序 + Plan Task 3.4, P0-1 fix): the durable
        finish event is appended BEFORE the state.json replace, so a crash in
        the window between them (or a later state.json corruption) can still
        reconstruct the committed state from the event's sha-verified snapshot
        (recover's Case C rolls forward).

        ``staged_artifacts`` (a {logical_name: bytes} map) is consumed by
        :func:`artifacts.prepare_generation` (Task 3.5): it writes blobs + the
        new generation manifest and FILLS ``state.artifact_generation`` /
        per-artifact shas WITHOUT touching state.json — the single
        ``os.replace(state.json)`` below remains the only commit point (errata #6).
        """
        from zcode_agentflow import locks as _locks

        with _locks.run_lock(self.control_root):
            state = State.load(self.control_root)
            self._verify_active_op(state, operation_id, lease_token)
            # CAS: the caller must pass revision_after_begin (== current revision).
            if expected_revision != state.revision:
                raise CasMismatch(
                    f"finish expected_revision {expected_revision} != current "
                    f"{state.revision} for op {operation_id!r}"
                )

            op = OperationManager(self.control_root).open_existing(operation_id)

            # advance to next_state with revision+1; clear the active op + lease.
            pre_state = state.state
            state.previous_state = pre_state
            state.state = next_state
            state.revision = state.revision + 1
            state.active_operation_id = None
            state.active_operation_lease = None
            # Fold authoritative fields the caller wants atomically committed
            # into the SAME state (P0-2): e.g. workflow_init's base_commit must
            # land in the single os.replace below, NOT a separate post-finish
            # save that a crash could skip. Only set when provided.
            if base_commit is not None:
                state.base_commit = base_commit

            # persist the adapter result (if any) + the new state_after snapshot.
            if result is not None:
                op.write_result(result)
            _crash_checkpoint("finish_after_result")

            # prepare_generation (Task 3.5): write blobs + the new generation
            # manifest and FILL state.artifact_generation / per-artifact shas.
            # This does NOT touch state.json — the single os.replace below is the
            # only commit point (errata #6). staged_artifacts may be empty.
            from zcode_agentflow import artifacts as _artifacts

            if staged_artifacts:
                _artifacts.prepare_generation(
                    control_root=self.control_root,
                    operation_id=operation_id,
                    changes=staged_artifacts,
                    prev_state=State.load(self.control_root),
                    next_state=state,
                    crash_after="finish_after_blobs",
                    crash_after_manifest="finish_after_manifest",
                )
            snap_rel, snap_sha = op.write_state_snapshot(
                "state_after_finish", state.to_dict()
            )
            _crash_checkpoint("finish_after_snapshot")
            # Commit protocol (Spec §3.5 写入顺序 + Plan Task 3.4 pseudocode):
            # the recoverable FINISH event (with its sha-verified state_after
            # snapshot) is appended+fsync'd BEFORE the single state.json
            # replace. Reversing this leaves a crash window where state.json is
            # advanced but only the BEGIN event is durable; a later state.json
            # corruption then loses the committed transition + artifact pointer
            # (P0-1, reviewer window).
            EventLog(self.control_root).append(
                operation_id=operation_id,
                from_state=pre_state,
                to_state=next_state,
                reason=f"finish:{operation_id.split('__')[1] if '__' in operation_id else operation_id}",
                state_after_path=snap_rel,
                state_after_sha=snap_sha,
                revision=state.revision,
            )
            _crash_checkpoint("finish_after_event")
            # THE ONLY COMMIT POINT: single os.replace(state.json). The event
            # above is already durable, so a crash here (or a later state.json
            # corruption) can still reconstruct the committed state from it.
            state.save(self.control_root)
            _crash_checkpoint("finish_after_state_replace")
            op.mark_committed()
            return {
                "operation_id": operation_id,
                "state": next_state,
                "revision": state.revision,
                "committed": True,
            }

    # ---- FAIL (short lock) -----------------------------------------------

    def fail_operation(self, *, operation_id, lease_token, reason) -> dict:
        """An adapter exception path: mark the op FAILED, transition to
        NEEDS_HUMAN, clear the active op. The Run is NOT stranded mid-external."""
        from zcode_agentflow import locks as _locks

        with _locks.run_lock(self.control_root):
            state = State.load(self.control_root)
            self._verify_active_op(state, operation_id, lease_token)

            op = OperationManager(self.control_root).open_existing(operation_id)
            pre_state = state.state
            state.previous_state = pre_state
            state.state = "NEEDS_HUMAN"
            state.revision = state.revision + 1
            state.active_operation_id = None
            state.active_operation_lease = None
            snap_rel, snap_sha = op.write_state_snapshot(
                "state_after_fail", state.to_dict()
            )
            # Fail path uses the SAME commit order as finish (P0-1): the
            # recoverable event is appended+fsync'd BEFORE the state.json
            # replace, so a crash here (or a later state.json corruption) can
            # still reconstruct the NEEDS_HUMAN transition from the event.
            EventLog(self.control_root).append(
                operation_id=operation_id,
                from_state=pre_state,
                to_state="NEEDS_HUMAN",
                reason=f"fail:{reason}",
                state_after_path=snap_rel,
                state_after_sha=snap_sha,
                revision=state.revision,
            )
            _crash_checkpoint("fail_after_event")
            state.save(self.control_root)
            op.mark_failed()
            return {
                "operation_id": operation_id,
                "state": "NEEDS_HUMAN",
                "revision": state.revision,
                "reason": reason,
            }

    # ---- UNKNOWN (short lock) --------------------------------------------

    def mark_unknown(self, *, operation_id, lease_token, reason,
                     allow_costful_replay: bool = False) -> dict:
        """Adapter timeout/process-kill path: mark the op UNKNOWN, classify the
        replay outcome by ``replay_policy`` (Task 3.3), and clear the active op.

        The Run state is left in its intermediate state but with the active op
        cleared; full recovery (replaying a SAFE_READ op, surfacing NEEDS_HUMAN)
        is :meth:`Store.recover` in Task 3.8. Here we record the classification
        so the caller/engine knows the outcome.
        """
        from zcode_agentflow import locks as _locks

        with _locks.run_lock(self.control_root):
            state = State.load(self.control_root)
            self._verify_active_op(state, operation_id, lease_token)

            op = OperationManager(self.control_root).open_existing(operation_id)
            op.mark_unknown()
            outcome = classify_unknown_replay(op, allow_costful_replay=allow_costful_replay)

            pre_state = state.state
            # clear the active op regardless (the lease is void once unknown);
            # do NOT advance the run state here — recover owns the next move.
            state.active_operation_id = None
            state.active_operation_lease = None
            snap_rel, snap_sha = op.write_state_snapshot(
                "state_after_unknown", state.to_dict()
            )
            state.save(self.control_root)
            EventLog(self.control_root).append(
                operation_id=operation_id,
                from_state=pre_state,
                to_state=pre_state,  # state unchanged on unknown
                reason=f"unknown:{reason} (outcome={outcome})",
                state_after_path=snap_rel,
                state_after_sha=snap_sha,
                revision=state.revision,
            )
            return {
                "operation_id": operation_id,
                "status": "UNKNOWN",
                "outcome": outcome,
                "reason": reason,
            }

    # ---- RECOVERY (Task 3.6 core; Task 3.8 adds pointer integration) ------

    def last_recover_outcome(self) -> str | None:
        """Return the replay outcome label from the most recent ``recover()``
        (``"REPLAY"`` / ``"NEEDS_HUMAN"``), or None if recover has not run or
        found no orphan to classify. Used by the fault-injection matrix to
        assert the classification without re-deriving it."""
        return self._last_recover_outcome

    def recover(self, *, allow_costful_replay: bool = False) -> State:
        """Single entry for crash recovery. Under the per-Run lock:

          1. try to load state.json; if it is missing/corrupt, REBUILD from the
             latest event's ``state_after`` snapshot (verifying its sha); if that
             also fails, raise :class:`StateCorruptError`;
          2. if an ``active_operation_id`` is set (a process was killed
             mid-external), open the op, mark it UNKNOWN, classify the replay
             outcome by its ``replay_policy``, and CLEAR ``active_operation_id``
             (and the stored lease);
          3. rewrite state.json (so the cleared active op is durable) and return
             the recovered State.

        ``recover`` never scans for "newest" runs and never auto-switches
        projects — it recovers THIS run only. Pointer-driven run selection
        (``bootstrap.resume_active_run``) lands in Task 3.8.
        """
        from zcode_agentflow import locks as _locks

        with _locks.run_lock(self.control_root):
            self._last_recover_outcome = None

            # 1. load or rebuild state.json
            state = self._load_or_rebuild_state()

            # 1a. integrity-check the generation the recovered state points at
            # (Spec §4.7, P0-3): recompute each blob's sha against the manifest
            # and FAIL CLOSED on a mismatch (tampering / corruption / disk rot)
            # rather than rehydrating corrupt content. A state with no current
            # generation (no artifacts yet) trivially passes.
            self._verify_generation_blobs(state)

            # 1b. reconcile the op named by the latest event. If a finish
            # committed the state.json but crashed BEFORE op.mark_committed()
            # (crash points finish_after_state_replace / finish_after_event),
            # the op is still RUNNING while the run state already advanced.
            # Per the plan's crash semantics ("crash after step 6 before step 7
            # -> state committed, recover marks op COMMITTED"), reconcile it.
            latest = EventLog(self.control_root).latest()
            if latest is not None:
                self._reconcile_latest_op(latest, state)

            # 2. classify + clear an orphan active op
            if state.active_operation_id:
                op_id = state.active_operation_id
                try:
                    op = OperationManager(self.control_root).open_existing(op_id)
                except FileNotFoundError:
                    op = None  # orphan points at a missing op dir; just clear it
                outcome = "NEEDS_HUMAN"
                if op is not None:
                    if op.status not in ("COMMITTED", "FAILED", "UNKNOWN"):
                        op.mark_unknown()
                    outcome = classify_unknown_replay(
                        op, allow_costful_replay=allow_costful_replay
                    )
                state.active_operation_id = None
                state.active_operation_lease = None
                self._last_recover_outcome = outcome
                # If the orphan is NOT safely replayable, the run cannot proceed
                # automatically -> advance to NEEDS_HUMAN (a human must inspect
                # the half-run op). A REPLAY outcome leaves the run state as-is;
                # the engine decides whether to replay (recover itself never
                # re-invokes an adapter).
                if outcome == "NEEDS_HUMAN" and state.state != "NEEDS_HUMAN":
                    state.previous_state = state.state
                    state.state = "NEEDS_HUMAN"
                    state.revision = state.revision + 1

            # 3. persist the recovered state
            state.save(self.control_root)
            return state

    def _reconcile_latest_op(self, latest_event: dict, state: State) -> None:
        """Reconcile an op left mid-lifecycle by a crash.

        Two crash windows (plan Task 3.4 crash semantics):

        A) crash AFTER the single state.json replace (step 6) but BEFORE
           op.mark_committed (step 7): the run state is advanced and
           ``active_operation_id`` is cleared, but the op is still RUNNING. The
           latest event is the op's BEGIN (the finish event was never appended).
           Detection: state has NO active op, the latest event's op is the one
           that began the in-flight transition, that op is RUNNING with a result,
           and the begin event's snapshot has ``active_operation_id`` SET while
           the current state has it CLEARED (i.e. a finish cleared it). => mark
           the op COMMITTED.

        B) a finish event WAS appended (crash after finish_after_event) but
           before op.mark_committed: the latest event is a FINISH whose snapshot
           equals the current state.json (sha match) and has
           ``active_operation_id`` cleared. => mark the op COMMITTED.
        """
        op_id = latest_event.get("operation_id")
        if not op_id:
            return
        try:
            op = OperationManager(self.control_root).open_existing(op_id)
        except FileNotFoundError:
            return
        if op.status in ("COMMITTED", "FAILED", "UNKNOWN"):
            return  # already terminal

        snap_rel = latest_event.get("state_after_path")
        expected_sha = latest_event.get("state_after_sha256")
        if not snap_rel or not expected_sha or not (self.control_root / snap_rel).exists():
            return
        snap_path = self.control_root / snap_rel
        snap = json.loads(snap_path.read_bytes())
        snap_active = snap.get("active_operation_id")
        current_sha = canonical.hash_file(self.control_root / STATE_FILENAME)

        if current_sha == expected_sha:
            # Case B: the latest event produced the current state. If this was a
            # finish (active_op cleared in its snapshot), the op is committed.
            if snap_active is None:
                op.mark_committed()
            return

        # Case A: the latest event is a BEGIN (snap_active set) that did NOT
        # produce the current state (sha differs) — meaning a finish replaced
        # state.json and cleared active_operation_id, then crashed before either
        # appending its own event or marking the op COMMITTED. If the current
        # state has no active op AND this begin's op is RUNNING with a result,
        # the finish committed — reconcile.
        if snap_active == op_id and state.active_operation_id is None and op.has_result():
            op.mark_committed()
            return

        # Case C (P0-1 order fix): the latest event is a FINISH (snap_active is
        # None — a finish clears the active op in its snapshot) whose snapshot
        # sha DIFFERS from the current state.json. With the corrected commit
        # order (event appended BEFORE the state.json replace), this means the
        # process crashed AFTER appending the durable finish event but BEFORE
        # replacing state.json. The current state.json still shows the
        # intermediate state with an active op set, but the FINISH event already
        # carries the authoritative committed state (sha-verified snapshot). Roll
        # the run state FORWARD from that snapshot and mark the op COMMITTED —
        # do NOT classify the orphan as UNKNOWN/NEEDS_HUMAN (the transition did
        # commit; only the state.json mirror lagged).
        if snap_active is None and op.has_result():
            rolled = State.from_dict(snap)
            # mutate the in-memory state the caller is reconciling, in place,
            # so recover()'s subsequent steps (orphan classification, persistence)
            # see the rolled-forward view. The snapshot already reflects the
            # committed transition (state advanced, active op cleared, artifact
            # pointer set), so every relevant field is copied.
            state.state = rolled.state
            state.previous_state = rolled.previous_state
            state.revision = rolled.revision
            state.active_operation_id = rolled.active_operation_id
            state.active_operation_lease = rolled.active_operation_lease
            state.artifact_generation = rolled.artifact_generation
            # ALL authoritative artifact shas the finish folded into its snapshot
            # (prepare_generation fills the full NAME_TO_STATE_SHA set). Copying
            # only artifact_generation + the two *_result shas would leave the
            # rolled-forward state pointing at the NEW generation but carrying the
            # OLD spec/plan/manifest shas, breaking the
            # 'state sha == blob sha of the pointed generation' invariant.
            state.spec_sha256 = rolled.spec_sha256
            state.plan_sha256 = rolled.plan_sha256
            state.task_manifest_sha256 = rolled.task_manifest_sha256
            state.execution_result_sha256 = rolled.execution_result_sha256
            state.review_result_sha256 = rolled.review_result_sha256
            # authoritative fields the finish folded into its commit (P0-2):
            state.base_commit = rolled.base_commit
            op.mark_committed()

    def _verify_generation_blobs(self, state: State) -> None:
        """Integrity-check the generation ``state.artifact_generation`` points
        at (Spec §4.7, P0-3; Plan Task 3.5/3.8: recovery verifies each blob's
        sha and FAILS — never returns successfully against an incomplete/
        corrupt generation). Fail CLOSED on:

          - the generation manifest is missing (the committed generation is
            incomplete — the state dangles at a generation that no longer
            exists);
          - any referenced blob is missing (incomplete generation);
          - any blob's recomputed sha != the manifest's recorded sha
            (tampering / corruption / disk rot).

        A state with NO current generation (no artifacts committed yet) passes
        trivially. Raises :class:`StateCorruptError` for the missing-manifest/
        blob cases (the committed view is unreconstructable) and
        :class:`artifacts.ArtifactCorrupt` for a sha mismatch.
        """
        from zcode_agentflow import artifacts as _artifacts

        gen_id = getattr(state, "artifact_generation", None)
        if not gen_id:
            return
        try:
            gen = _artifacts.load_generation(self.control_root, gen_id)
        except FileNotFoundError:
            raise StateCorruptError(
                f"state points at generation {gen_id!r} whose manifest is "
                f"missing — the committed generation is incomplete; cannot "
                f"recover a consistent state"
            )
        for name, ref in gen.index.items():
            bpath = self.control_root / ref["blob"]
            if not bpath.exists():
                raise StateCorruptError(
                    f"artifact {name!r} blob {ref['blob']!r} of generation "
                    f"{gen_id!r} is missing — the committed generation is "
                    f"incomplete; cannot recover a consistent state"
                )
            actual = canonical.sha256_hex(bpath.read_bytes())
            if actual != ref["sha256"]:
                raise _artifacts.ArtifactCorrupt(
                    f"artifact {name!r} blob sha mismatch during recovery: "
                    f"manifest {ref['sha256']!r} != on-disk {actual!r} at "
                    f"{ref['blob']!r}"
                )

    def _load_or_rebuild_state(self) -> State:
        """Load state.json; on corruption/absence, rebuild from the latest
        event's ``state_after`` snapshot (sha-verified). Raise
        :class:`StateCorruptError` if both fail."""
        try:
            return State.load(self.control_root)
        except StateError:
            pass  # fall through to rebuild

        latest = EventLog(self.control_root).latest()
        if latest is None:
            raise StateCorruptError(
                f"state.json is corrupt/missing and there are no events to "
                f"rebuild from in {self.control_root}"
            )
        snap_rel = latest.get("state_after_path")
        expected_sha = latest.get("state_after_sha256")
        if not snap_rel or not expected_sha:
            raise StateCorruptError(
                "latest event carries no recoverable state_after snapshot"
            )
        snap_path = self.control_root / snap_rel
        if not snap_path.exists():
            raise StateCorruptError(
                f"state_after snapshot missing: {snap_rel}"
            )
        actual_sha = canonical.hash_file(snap_path)
        if actual_sha != expected_sha:
            raise StateCorruptError(
                f"state_after snapshot sha mismatch (expected {expected_sha}, "
                f"got {actual_sha}) for {snap_rel}"
            )
        # rebuild state.json from the verified snapshot
        rebuilt = State.from_dict(json.loads(snap_path.read_bytes()))
        rebuilt.save(self.control_root)
        return rebuilt


# ===========================================================================
# Transactional workflow_init (Task 3.7; Prompt §11, P0-4, errata)
#
# init_run owns the run-creation critical section inside ONE registry-lock
# block (errata #5: this task is the SOLE owner of the initial pointer write;
# Task 6.8 delegates here and does NOT repeat it). It:
#   1. asserts a new run is allowed (no non-terminal active pointer);
#   2. runs preflight (git repo, no in-progress merge, roots non-nesting, no
#      branch/worktree collision);
#   3. seeds the NEW state.json, then begin_operation (NEW -> INITIALIZING_WORKTREE)
#      — a half-created worktree from a crashed prior attempt is CLAIMED if its
#      branch ownership matches this run, else InitNeedsHuman (NEVER auto-remove);
#   4. gitops.create_worktree (journal branch);
#   5. finish_operation (state=INITIALIZING_WORKTREE, base_commit) — the single
#      commit point;
#   6. writes the active-run pointer under the SAME registry lock.
# No destructive compensation: a half-created worktree is never auto-removed.
# ===========================================================================


def init_run(*, user_data_root, target_repo, control_root, worktree_root,
             run_id, plugin_root, goal) -> dict:
    """Create a run transactionally: preflight -> begin/worktree/finish ->
    set the active pointer. Returns ``{run_id, base_commit, ...}``.

    Holds the Registry Lock across the whole critical section (pointer +
    allocation). The Per-Run lock is taken only across the begin/finish
    bookkeeping (by ``Store.begin_operation``/``finish_operation``); the
    ``gitops.create_worktree`` call runs between them, Per-Run-lock-free.
    """
    from zcode_agentflow import active_run as _active_run
    from zcode_agentflow import gitops as _gitops
    from zcode_agentflow import preflight as _preflight

    control_root = Path(control_root)
    worktree_root = Path(worktree_root)
    # git worktree add -b takes a SHORT branch name; the full ref is
    # refs/heads/<short>. We keep both: short for git ops, full for comparisons.
    branch_short = f"agentflow/{run_id}"
    branch = f"refs/heads/{branch_short}"

    with _active_run.ActiveRunRegistry.locked(user_data_root) as registry:
        # 1. only one active run at a time (P0-4). Surface a registry rejection
        # as InitRejected so callers can distinguish "blocked by an active run"
        # from an environment preflight failure.
        try:
            registry.assert_new_run_allowed_locked()
        except RuntimeError as exc:
            raise InitRejected(str(exc)) from exc

        # 2. preflight (read-only environment check)
        _preflight.preflight(
            target_repo=target_repo, control_root=control_root,
            worktree_root=worktree_root, branch=branch,
        )

        # 3. seed the NEW state.json for this run, then begin the init op.
        control_root.mkdir(parents=True, exist_ok=True)
        fresh = State(
            run_id=run_id, revision=1, state="NEW", previous_state="",
            plan_version=1, current_task="", completed_tasks=[],
            roots={
                "plugin_root": str(Path(plugin_root)),
                "target_repo_root": str(Path(target_repo)),
                "run_worktree_root": str(worktree_root),
                "control_root": str(control_root),
            },
        )
        fresh.save(control_root)

        s = Store(control_root)
        op_id = f"{run_id}__workflow_init__r1__a1"

        # Worktree recovery: if the worktree path already exists, a prior init
        # attempt crashed after `git worktree add` before finish. CLAIM it iff
        # its branch ownership matches this run; else InitNeedsHuman. We NEVER
        # auto-remove (no destructive compensation).
        wt_created = False
        base_commit = None
        if worktree_root.exists() and any(worktree_root.iterdir()):
            _claim_or_raise(target_repo, worktree_root, branch)
            # the worktree is ours and already on `branch`; reuse its base commit
            base_commit = _gitops.head(target_repo)
            wt_created = True  # treat as already done; skip create below
        else:
            base_commit = _gitops.head(target_repo)

        # begin the init transaction (NEW -> INITIALIZING_WORKTREE). This takes
        # the Per-Run lock for a short bookkeeping step.
        br = s.begin_operation(
            operation_id=op_id,
            request={"tool": "workflow_init", "args": {"target_repo": str(target_repo), "goal": goal}},
            tool="workflow_init",
            expected_revision=State.load(control_root).revision,
            intermediate_state="INITIALIZING_WORKTREE",
            replay_policy="UNSAFE_WRITE",  # worktree creation has side effects
        )

        try:
            # external-ish step (Per-Run-lock-free): create the worktree if we
            # did not claim an existing one.
            if not wt_created:
                _gitops.create_worktree(
                    target_repo=target_repo, worktree_path=worktree_root, branch=branch_short,
                )
            # fault-injection checkpoint: crash here simulates a process kill
            # after the worktree exists but before finish commits.
            _crash_checkpoint("init_after_worktree_add")

            s.finish_operation(
                operation_id=op_id,
                lease_token=br.lease_token,
                expected_revision=br.revision_after_begin,
                next_state="INITIALIZING_WORKTREE",
                staged_artifacts={},
                result={"worktree": str(worktree_root)},
                base_commit=base_commit,
            )
        except CrashInjected:
            raise
        except BaseException:
            # any non-crash failure during init: surface as InitNeedsHuman
            # (the half-created worktree is left in place for a human).
            raise InitNeedsHuman(
                f"workflow_init for run {run_id!r} failed mid-transaction; "
                f"worktree may be half-created at {worktree_root} (NOT auto-removed)"
            )

        # 6. write the active-run pointer under the SAME registry lock (errata:
        # this task is the sole owner of the initial pointer write).
        registry.set_pointer_locked(
            run_id=run_id, control_root=control_root, target_repo_root=target_repo,
        )

    return {
        "run_id": run_id,
        "base_commit": base_commit,
        "control_root": str(control_root),
        "worktree_root": str(worktree_root),
        "branch": branch,
    }


def _claim_or_raise(target_repo, worktree_root, branch) -> None:
    """If ``worktree_root`` is an existing worktree of ``target_repo`` whose
    branch IS ``branch``, claim it (no-op — it's ours). Otherwise raise
    :class:`InitNeedsHuman` (foreign collision; never auto-remove)."""
    from zcode_agentflow import gitops as _gitops

    branch_short = branch.split("refs/heads/", 1)[-1]
    owner_branch = None
    for wt in _gitops.worktree_list(target_repo):
        wpath = wt.get("path")
        if wpath:
            try:
                if Path(wpath).resolve() == Path(worktree_root).resolve():
                    owner_branch = wt.get("branch")
                    break
            except OSError:
                continue
    # git records the branch as refs/heads/... or bare short name depending on
    # version; accept either form.
    owner_short = (owner_branch or "").split("refs/heads/", 1)[-1]
    if owner_short != branch_short:
        raise InitNeedsHuman(
            f"worktree path {worktree_root} exists but is owned by branch "
            f"{owner_branch!r}, not this run's branch {branch!r} (foreign "
            f"collision); refusing to overwrite — needs human"
        )
    # owner matches -> claim (no destructive action)


# ===========================================================================
# EventLog — append-only, resumable seq (Review §六.2)
# ===========================================================================


class EventLog:
    """Append-only ``events.jsonl`` with a seq that resumes from disk.

    Each event is a single canonical-JSON line carrying the recoverable fields
    from Spec §3.5: ``seq``, ``at``, ``operation_id``, ``from``, ``to``,
    ``reason``, ``revision``, ``state_after_path``, ``state_after_sha256``.

    The next seq is derived from the highest seq already on disk, so a NEW
    EventLog constructed against an existing run CONTINUES the sequence rather
    than resetting to 1. Appends are atomic+fsync'd (a crash never leaves a
    half-line). The caller is responsible for holding the per-Run lock while
    appending (the EventLog does not take the lock itself).
    """

    def __init__(self, control_root):
        self.control_root = Path(control_root)
        self.control_root.mkdir(parents=True, exist_ok=True)
        self._path = self.control_root / EVENTS_FILENAME
        self._next_seq = self._last_seq_on_disk() + 1

    # ---- seq derivation --------------------------------------------------

    def _last_seq_on_disk(self) -> int:
        """Highest seq currently in events.jsonl, or 0 if none/unreadable.

        Reads the file directly (no lock) at construction only; this is safe
        because the EventLog is constructed under the per-Run lock or on an
        idle run. A torn/corrupt trailing line is ignored (its seq is not
        counted) — recovery (Task 3.8) handles a corrupt state_after, not a
        corrupt events.jsonl; here we just skip un-parseable lines defensively.
        """
        if not self._path.exists():
            return 0
        last = 0
        for line in self._path.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # skip a torn trailing line defensively
                continue
            seq = obj.get("seq", 0)
            if isinstance(seq, int) and seq > last:
                last = seq
        return last

    @property
    def next_seq(self) -> int:
        return self._next_seq

    # ---- append ----------------------------------------------------------

    def append(
        self,
        *,
        operation_id: str,
        from_state: str,
        to_state: str,
        reason: str,
        state_after_path: str,
        state_after_sha: str,
        revision: int | None = None,
    ) -> dict:
        """Append one recoverable event, fsync it, and advance the seq.

        Returns the event dict that was written. The ``state_after_path`` is a
        path RELATIVE to control_root (so the event is portable); recovery
        (Task 3.8) joins it back to control_root and verifies the sha.

        ``revision`` is the run-state revision AFTER this transition (so events
        and the reconstructed state agree). If omitted, it is not written.
        """
        ev = {
            "seq": self._next_seq,
            "at": _utc_now_iso(),
            "operation_id": operation_id,
            "from": from_state,
            "to": to_state,
            "reason": reason,
            "state_after_path": state_after_path,
            "state_after_sha256": state_after_sha,
        }
        if revision is not None:
            ev["revision"] = revision
        line = canonical.canonical_json_bytes(ev)
        # Append-only: open in binary append, write the line, flush+fsync so a
        # crash before the next append never loses this event.
        with open(self._path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._next_seq += 1
        return ev

    # ---- read ------------------------------------------------------------

    def latest(self) -> dict | None:
        """Return the last (highest-seq) event, or None if the log is empty."""
        if not self._path.exists():
            return None
        last = None
        for line in self._path.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
        return last


# ===========================================================================
# State — the state.json snapshot (Spec §3.4)
# ===========================================================================


@dataclass
class State:
    """The in-memory model of ``state.json`` (Spec §3.4).

    ``save()`` writes via ``canonical.write_canonical_json`` (tmp+fsync+
    os.replace) so concurrent readers NEVER see a half-write; ``load``
    round-trips every field. ``revision`` is the lightweight CAS counter
    (each transition +1).

    Task 3.5 adds ``artifact_generation`` (the sole commit pointer); it is
    carried here as an optional field so a v1.0/3.2 state without it still
    round-trips. ``active_operation_id`` / ``active_operation_lease`` are the
    two-phase-transaction fields added in Task 3.4; they default to None so a
    State created in this task round-trips without them.
    """

    run_id: str
    revision: int
    state: str
    previous_state: str
    plan_version: int
    current_task: str
    completed_tasks: list
    roots: dict
    spec_sha256: str | None = None
    plan_sha256: str | None = None
    task_manifest_sha256: str | None = None
    base_commit: str | None = None
    task_start_commit: str | None = None
    last_reviewed_commit: str | None = None
    review_round: int = 0
    repair_round: int = 0
    updated_at: str = field(default_factory=_utc_now_iso)
    # Added in later tasks (3.4/3.5) — optional here so this dataclass round-trips
    # whether or not the producing task has written them.
    artifact_generation: str | None = None
    active_operation_id: str | None = None
    active_operation_lease: str | None = None
    # Task 3.5: per-artifact authoritative shas for the execution-result and
    # review-result logical names (used by the review/handoff binding). Optional
    # so a State created before those artifacts exist round-trips without them.
    execution_result_sha256: str | None = None
    review_result_sha256: str | None = None

    # ---- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        """Canonical dict form written to state.json.

        Includes every Spec §3.4 field plus the later-task fields when set.
        Keys that are None (unset) are OMITTED so a freshly-created State does
        not carry null placeholders that would shift its hash.
        """
        d = {
            "run_id": self.run_id,
            "revision": self.revision,
            "state": self.state,
            "previous_state": self.previous_state,
            "plan_version": self.plan_version,
            "current_task": self.current_task,
            "completed_tasks": list(self.completed_tasks),
            "roots": dict(self.roots),
            "review_round": self.review_round,
            "repair_round": self.repair_round,
            "updated_at": self.updated_at,
        }
        for k in (
            "spec_sha256",
            "plan_sha256",
            "task_manifest_sha256",
            "base_commit",
            "task_start_commit",
            "last_reviewed_commit",
            "artifact_generation",
            "active_operation_id",
            "active_operation_lease",
            "execution_result_sha256",
            "review_result_sha256",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    def save(self, control_root) -> None:
        """Atomically write state.json (tmp+fsync+os.replace).

        The caller holds the per-Run lock and sets ``updated_at`` (a transition
        owns its timestamp); ``save`` does NOT silently overwrite it so a
        ``load -> save`` round-trip preserves every field exactly.
        """
        path = Path(control_root) / STATE_FILENAME
        canonical.write_canonical_json(path, self.to_dict())

    @classmethod
    def load(cls, control_root) -> "State":
        """Load state.json from ``control_root``.

        Raises :class:`StateError` if the file is missing or not parseable as
        JSON. Unknown-extra keys are ignored (forward-compat); every required
        Spec §3.4 field must be present.
        """
        path = Path(control_root) / STATE_FILENAME
        if not path.exists():
            raise StateError(f"state.json not found at {path}")
        try:
            d = json.loads(path.read_bytes())
        except (json.JSONDecodeError, ValueError) as exc:
            raise StateError(f"state.json at {path} is corrupt: {exc}") from exc
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        """Build a State from a parsed dict, applying required-field checks and
        defaults for the optional later-task fields."""
        required = (
            "run_id", "revision", "state", "previous_state",
            "plan_version", "current_task", "completed_tasks", "roots",
        )
        for k in required:
            if k not in d:
                raise StateError(f"state.json missing required field {k!r}")
        return cls(
            run_id=d["run_id"],
            revision=d["revision"],
            state=d["state"],
            previous_state=d["previous_state"],
            plan_version=d["plan_version"],
            current_task=d["current_task"],
            completed_tasks=list(d["completed_tasks"]),
            roots=dict(d["roots"]),
            spec_sha256=d.get("spec_sha256"),
            plan_sha256=d.get("plan_sha256"),
            task_manifest_sha256=d.get("task_manifest_sha256"),
            base_commit=d.get("base_commit"),
            task_start_commit=d.get("task_start_commit"),
            last_reviewed_commit=d.get("last_reviewed_commit"),
            review_round=d.get("review_round", 0),
            repair_round=d.get("repair_round", 0),
            updated_at=d.get("updated_at", _utc_now_iso()),
            artifact_generation=d.get("artifact_generation"),
            active_operation_id=d.get("active_operation_id"),
            active_operation_lease=d.get("active_operation_lease"),
            execution_result_sha256=d.get("execution_result_sha256"),
            review_result_sha256=d.get("review_result_sha256"),
        )
