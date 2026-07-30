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
    "ArtifactNotFound",
    "Generation",
    "ArtifactResolver",
    "prepare_generation",
    "load_generation",
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
        """Return the canonical bytes of ``logical_name`` from the current gen."""
        ref = self._ref(logical_name)
        # ref["blob"] is relative to control_root (e.g. artifacts/blobs/<sha>)
        bpath = self.control_root / ref["blob"]
        return bpath.read_bytes()

    def read_text(self, logical_name: str) -> str:
        """Return ``logical_name`` decoded as canonical UTF-8 text (exactly one
        trailing newline, matching the persisted canonical bytes)."""
        return self.read(logical_name).decode("utf-8")

    def hash_of(self, logical_name: str) -> str:
        """Return the authoritative sha256 of ``logical_name`` (from the index,
        which equals the canonical sha of its blob)."""
        return self._ref(logical_name)["sha256"]
