"""Artifact Index + ArtifactResolver (Task 3.5; P0-3, P1-4; parallel P0-1).

``state.json.artifact_generation`` is the SOLE commit pointer for artifacts.
There is no ``current.json``. Each generation is a COMPLETE logical snapshot
(Artifact Index mode): a new generation inherits the previous generation's full
index and replaces only this operation's changes. Blobs are content-addressed
(``artifacts/blobs/<sha>``), so inheritance is an index copy with no blob
duplication.

The single commit point is ``state.json``'s atomic replace in
``Store.finish_operation`` (errata #6). :func:`prepare_generation` writes blobs +
the generation manifest and FILLS ``next_state`` fields (``artifact_generation``
+ the per-artifact sha256), but it does NOT touch ``state.json``. A crash after
blob writes but before the state replace leaves the OLD generation visible
(the partial new generation is invisible and discarded).

ALL reads of logical artifacts go through :class:`ArtifactResolver`. No code
reads ``control_root/SPEC.md`` directly — the resolver is the only read path.

Layout under control_root::

    artifacts/blobs/<sha>                       content-addressed blobs
    artifacts/<generation_id>/manifest.json     the Generation {generation_id, index}

Logical names and their authoritative State.sha256 fields:
    SPEC.md               -> spec_sha256
    PLAN.md               -> plan_sha256
    task-manifest.json    -> task_manifest_sha256
    execution-result.json -> execution_result_sha256   (added here as optional)
    review-result.json    -> review_result_sha256      (added here as optional)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from zcode_agentflow import canonical

__all__ = [
    "ARTIFACTS_DIRNAME",
    "BLOBS_DIRNAME",
    "MANIFEST_FILENAME",
    "NAME_TO_STATE_SHA",
    "LOCKED_ARTIFACT_NAMES",
    "ArtifactNotFound",
    "ArtifactCorrupt",
    "ArtifactMismatch",
    "Generation",
    "ArtifactResolver",
    "prepare_generation",
    "load_generation",
    "verify_locked_artifacts",
]

ARTIFACTS_DIRNAME = "artifacts"
BLOBS_DIRNAME = "blobs"
MANIFEST_FILENAME = "manifest.json"

# logical name -> the State field that holds its authoritative sha256.
# prepare_generation fills these on next_state so state and the generation index
# cannot diverge. (execution_result_sha256 / review_result_sha256 are optional
# state fields surfaced for the review/handoff binding; they default to unset
# when no such artifact exists.)
NAME_TO_STATE_SHA = {
    "SPEC.md": "spec_sha256",
    "PLAN.md": "plan_sha256",
    "task-manifest.json": "task_manifest_sha256",
    "execution-result.json": "execution_result_sha256",
    "review-result.json": "review_result_sha256",
}


class ArtifactNotFound(KeyError):
    """Raised when an :class:`ArtifactResolver` is asked for a logical name that
    is not present in the currently-visible generation."""


class ArtifactCorrupt(RuntimeError):
    """Raised when a logical artifact's blob sha does not match the manifest's
    recorded sha (tampering / corruption / disk rot). Reading and recovery fail
    CLOSED on this rather than serving or rehydrating corrupt content (P0-3,
    Spec §4.7 integrity)."""


class ArtifactMismatch(RuntimeError):
    """Raised by :func:`verify_locked_artifacts` when a locked artifact's current
    on-disk sha (recomputed via the resolver) does NOT equal the sha recorded in
    the committed state — i.e. the approved/locked view drifted from disk after
    the gate (Review §五; P0-3). Distinct from :class:`ArtifactCorrupt`, which is
    a blob-vs-its-own-manifest mismatch; this is a state-vs-disk mismatch."""


# The logical artifacts whose sha is LOCKED at a gate and re-checked at every
# later gate (Prompt §14; Spec §4.7). The Engine rehashes these from disk via
# the ArtifactResolver at begin/finish of every operation.
LOCKED_ARTIFACT_NAMES = ("SPEC.md", "PLAN.md", "task-manifest.json")


@dataclass
class Generation:
    """One complete logical snapshot of artifacts.

    ``index`` maps each logical name to ``{"blob": "blobs/<sha>", "sha256": "<sha>"}``.
    The index is COMPLETE — every name the run has ever published is present
    (inherited from the prior generation unless this op replaced it).
    """

    generation_id: str
    index: dict[str, dict]

    @classmethod
    def empty(cls, generation_id: str) -> "Generation":
        return cls(generation_id=generation_id, index={})

    def to_dict(self) -> dict:
        return {"generation_id": self.generation_id, "index": dict(self.index)}


def _artifacts_dir(control_root) -> Path:
    return Path(control_root) / ARTIFACTS_DIRNAME


def _blobs_dir(control_root) -> Path:
    return _artifacts_dir(control_root) / BLOBS_DIRNAME


def _generation_dir(control_root, generation_id) -> Path:
    return _artifacts_dir(control_root) / generation_id


def _blob_path(control_root, sha: str) -> Path:
    return _blobs_dir(control_root) / sha


def _write_blob(control_root, data: bytes) -> str:
    """Write a content-addressed blob (``artifacts/blobs/<sha>``) and return its
    sha. Canonical bytes are hashed/persisted so the same logical content always
    lands at the same blob path (content addressing). Idempotent: a re-write of
    identical bytes is a no-op on disk."""
    canon = canonical.canonical_text_bytes(data)
    sha = canonical.sha256_hex(canon)
    bpath = _blob_path(control_root, sha)
    if not bpath.exists():
        canonical.write_canonical_text(bpath, canon)
    return sha


def _current_generation_id(prev_state) -> str | None:
    return getattr(prev_state, "artifact_generation", None)


def load_generation(control_root, generation_id) -> Generation:
    """Load a :class:`Generation` from its manifest. Raises if the manifest is
    missing or malformed (recovery treats a generation the state does NOT point
    at as invisible; this loader is only called for the pointer's generation)."""
    mpath = _generation_dir(control_root, generation_id) / MANIFEST_FILENAME
    if not mpath.exists():
        raise FileNotFoundError(
            f"generation manifest not found: {generation_id!r}"
        )
    d = json.loads(mpath.read_bytes())
    return Generation(generation_id=d["generation_id"], index=dict(d["index"]))


def prepare_generation(*, control_root, operation_id, changes: dict[str, bytes],
                       prev_state, next_state,
                       crash_after=None, crash_after_manifest=None) -> Generation:
    """Build the next generation WITHOUT committing it.

    Steps (errata #6 — does NOT touch state.json):
      1. start from the CURRENT generation's index (inherit ALL names); if there
         is no current generation, start empty;
      2. for each (name, bytes) in ``changes``: write the content-addressed blob
         and set ``index[name] = {"blob": "blobs/<sha>", "sha256": "<sha>"}``;
      3. write ``artifacts/<operation_id>/manifest.json`` (the new Generation) —
         NOT yet committed (state.json untouched);
      4. fill ``next_state`` fields ONLY: ``artifact_generation = operation_id``
         and each known name's ``*_sha256`` from the index;
      5. return the Generation — the SINGLE ``os.replace(state.json)`` happens in
         ``Store.finish_operation``.

    Because blobs are content-addressed, an unchanged name inherits the prior
    blob ref verbatim (no duplication). The generation is invisible to readers
    until state.json is replaced to point at it.
    """
    control_root = Path(control_root)
    # 1. inherit the current generation's index
    cur_id = _current_generation_id(prev_state)
    if cur_id:
        try:
            index = dict(load_generation(control_root, cur_id).index)
        except FileNotFoundError:
            index = {}
    else:
        index = {}

    # 2. apply this operation's changes (content-addressed blobs)
    for name, data in changes.items():
        sha = _write_blob(control_root, data)
        # blob path is RELATIVE to control_root (self-resolving from control_root)
        index[name] = {"blob": f"{ARTIFACTS_DIRNAME}/{BLOBS_DIRNAME}/{sha}", "sha256": sha}

    # fault-injection checkpoint: crash AFTER blobs are written but BEFORE the
    # manifest (Task 3.6). The partial generation is invisible to readers
    # (state.json still points at the old generation).
    if crash_after:
        from zcode_agentflow.store import _crash_checkpoint
        _crash_checkpoint(crash_after)

    gen = Generation(generation_id=operation_id, index=index)

    # 3. write the generation manifest (not yet committed)
    gdir = _generation_dir(control_root, operation_id)
    gdir.mkdir(parents=True, exist_ok=True)
    canonical.write_canonical_json(gdir / MANIFEST_FILENAME, gen.to_dict())

    # fault-injection checkpoint: crash AFTER the manifest is written (still
    # invisible — state.json is not yet replaced to point at this generation).
    if crash_after_manifest:
        from zcode_agentflow.store import _crash_checkpoint
        _crash_checkpoint(crash_after_manifest)

    # 4. fill next_state fields ONLY (do NOT replace state.json)
    next_state.artifact_generation = operation_id
    for name, sha_field in NAME_TO_STATE_SHA.items():
        if name in index:
            setattr(next_state, sha_field, index[name]["sha256"])

    return gen


class ArtifactResolver:
    """The ONLY read path for logical artifacts.

    Resolves a logical name through the generation that ``state.artifact_generation``
    points at. Because the pointer is committed atomically with the rest of
    state.json (single os.replace), a reader never observes a mix of new
    artifacts and old state: it sees the generation matching the state it read.
    """

    def __init__(self, control_root, state):
        self.control_root = Path(control_root)
        self.state = state
        self._generation = None  # lazy

    def _resolve_generation(self) -> Generation:
        if self._generation is not None:
            return self._generation
        gen_id = getattr(self.state, "artifact_generation", None)
        if not gen_id:
            return Generation.empty("")
        self._generation = load_generation(self.control_root, gen_id)
        return self._generation

    def _ref(self, logical_name: str) -> dict:
        gen = self._resolve_generation()
        if logical_name not in gen.index:
            raise ArtifactNotFound(
                f"logical artifact {logical_name!r} is not in generation "
                f"{gen.generation_id!r}"
            )
        return gen.index[logical_name]

    def read(self, logical_name: str) -> bytes:
        """Return the canonical bytes of ``logical_name`` from the current gen.

        Integrity-checked (Spec §4.7, P0-3): the blob's SHA-256 is recomputed
        over the bytes on disk and compared to the manifest's recorded sha. A
        mismatch (tampering / corruption / disk rot) raises
        :class:`ArtifactCorrupt` rather than returning corrupt content.
        """
        ref = self._ref(logical_name)
        # ref["blob"] is relative to control_root (e.g. artifacts/blobs/<sha>)
        bpath = self.control_root / ref["blob"]
        data = bpath.read_bytes()
        actual = canonical.sha256_hex(data)
        if actual != ref["sha256"]:
            raise ArtifactCorrupt(
                f"artifact {logical_name!r} blob sha mismatch: manifest "
                f"{ref['sha256']!r} != on-disk {actual!r} at {ref['blob']!r}"
            )
        return data

    def read_text(self, logical_name: str) -> str:
        """Return ``logical_name`` decoded as canonical UTF-8 text (exactly one
        trailing newline, matching the persisted canonical bytes)."""
        return self.read(logical_name).decode("utf-8")

    def hash_of(self, logical_name: str) -> str:
        """Return the authoritative sha256 of ``logical_name``.

        Integrity-checked (Spec §4.7, P1-B): the sha is RECOMPUTED over the
        blob's actual on-disk bytes and compared to the manifest's recorded sha.
        A mismatch (tampering / corruption) raises :class:`ArtifactCorrupt`
        rather than handing back the manifest's value — otherwise a downstream
        hard-check binding that only calls ``hash_of`` could accept a tampered
        blob's manifest sha as if it were authoritative.
        """
        ref = self._ref(logical_name)
        bpath = self.control_root / ref["blob"]
        actual = canonical.sha256_hex(bpath.read_bytes())
        if actual != ref["sha256"]:
            raise ArtifactCorrupt(
                f"artifact {logical_name!r} blob sha mismatch: manifest "
                f"{ref['sha256']!r} != on-disk {actual!r} at {ref['blob']!r}"
            )
        return actual


# ===========================================================================
# Gate-time rehash (Task 6.1; Prompt §14; Review §五; P0-3)
#
# Every Engine operation rehashes the LOCKED artifacts (SPEC/PLAN/manifest) from
# disk and compares to the sha recorded in the committed state. A drift (a SPEC
# rewritten post-approval as a new generation, or a blob mutated on disk) is the
# exact post-approval tamper window complete_task must defend against. The read
# goes through the ArtifactResolver (never a control_root root file — P0-3).
# ===========================================================================


def verify_locked_artifacts(state, resolver: "ArtifactResolver") -> None:
    """Rehash the locked artifacts from disk and compare to the committed shas.

    For each of :data:`LOCKED_ARTIFACT_NAMES`, if the state carries a locked sha
    for it, recompute the sha of the current generation's blob (via the resolver
    — the ONLY read path) and raise :class:`ArtifactMismatch` on drift.

    An artifact the state has NOT yet locked (sha is None) is skipped: a fresh
    run has no SPEC/PLAN yet. A None sha paired with a present-on-disk artifact
    is fine for this check — locking happens at the gate that creates it.

    Note: ``resolver.hash_of`` itself raises :class:`ArtifactCorrupt` if a blob's
    bytes disagree with its OWN manifest sha (a tamper of the committed bytes).
    Both failure modes (ArtifactCorrupt: blob-vs-manifest; ArtifactMismatch:
    state-vs-disk) route the run to NEEDS_HUMAN at the engine layer.
    """
    for name in LOCKED_ARTIFACT_NAMES:
        locked = getattr(state, NAME_TO_STATE_SHA[name], None)
        if locked is None:
            continue  # this artifact is not locked yet in this run
        try:
            current = resolver.hash_of(name)
        except ArtifactNotFound:
            # the artifact was locked but the current generation no longer has
            # it — that is a drift too (the locked view is gone).
            raise ArtifactMismatch(
                f"locked artifact {name!r} (state sha {locked!r}) is absent from "
                f"the current generation"
            )
        if current != locked:
            raise ArtifactMismatch(
                f"locked artifact {name!r} sha drifted: state holds {locked!r} "
                f"but the current generation's blob hashes to {current!r}"
            )
