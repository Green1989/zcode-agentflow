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

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from zcode_agentflow import canonical

__all__ = [
    "STATE_FILENAME",
    "EVENTS_FILENAME",
    "EventLog",
    "State",
    "StateError",
]

STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateError(RuntimeError):
    """Raised when state.json is missing/corrupt and cannot be loaded."""


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
        )
