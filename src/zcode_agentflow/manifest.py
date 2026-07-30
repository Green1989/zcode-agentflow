"""Self-reference-free task manifest (Task 2.5; Prompt §8).

The task-manifest is hashed for review binding, so it MUST NOT contain its own
hash: a self-referential sha would change whenever the file is written, making
the binding circular and unverifiable. The manifest's sha256 therefore lives in
``state.json`` / a review-bundle sidecar, never inside the manifest file itself.

This module is a PURE BUILDER (consistency #2). It:
  - builds the manifest dict from a FINAL plan-envelope,
  - produces the canonical bytes of that dict (for the Store to stage), and
  - computes the manifest sha over those canonical bytes.

It does NOT write any authoritative control_root file. The single artifact-
commit path belongs to the Store's ``prepare_generation`` + ``finish_operation``
(Task 3.5): the Engine passes ``manifest_bytes(...)`` as a staged artifact
(``changes={"task-manifest.json": manifest_bytes(...)}``), and the Store commits
it. Keeping manifest.py write-free means there is exactly one writer of
control_root artifacts.
"""

from __future__ import annotations

import re

from zcode_agentflow import canonical

__all__ = [
    "ManifestError",
    "build_manifest",
    "manifest_bytes",
    "manifest_sha_of_bytes",
    "inject_for_review",
]

# canonical sha form (matches the shared schema defs): 64 lowercase hex
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

# the review-relevant fields carried per task in the manifest (Spec §4.3).
# Kept lean: only what Grok binds against — no title/dependencies duplication.
_TASK_FIELDS = (
    "id",
    "verification_commands",
    "acceptance_criteria",
    "declared_files",
    "planned_changes",
)


class ManifestError(ValueError):
    """Raised when a manifest cannot be built (multi-task plan, bad plan sha,
    malformed plan-envelope)."""


def build_manifest(plan_envelope: dict, *, plan_sha256: str) -> dict:
    """Build a self-reference-free task manifest from a FINAL plan-envelope.

    The manifest carries ``plan_sha256`` (the binding anchor to the approved
    plan), ``current_task``, and the single task's review-relevant fields. It
    does NOT carry its own sha (that would be self-referential) nor the plan's
    authoritative envelope fields (run_id/base_commit/plan_version/plan_markdown
    — those live in the plan-envelope and state.json).

    v1 is a single-task run: a plan with zero or more-than-one task is rejected
    here (the manifest cannot represent it).
    """
    if not isinstance(plan_envelope, dict):
        raise ManifestError(f"plan_envelope must be a dict, got {type(plan_envelope).__name__}")
    if not isinstance(plan_sha256, str) or not _HASH_RE.match(plan_sha256):
        raise ManifestError(
            f"plan_sha256 must be 64 lowercase hex, got {plan_sha256!r}"
        )
    tasks = plan_envelope.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise ManifestError(
            f"v1 manifest requires exactly one task; got {len(tasks) if isinstance(tasks, list) else 'non-list'}"
        )
    plan_task = tasks[0]
    task_id = plan_task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ManifestError("plan task is missing an id")

    # Carry the review-relevant fields. A FINAL plan-envelope has been validated
    # upstream, but be defensive: a missing binding field is a ManifestError, not
    # a raw KeyError, so callers get a consistent error type.
    manifest_task = {}
    for field in _TASK_FIELDS:
        if field not in plan_task:
            raise ManifestError(
                f"plan task {task_id!r} is missing required field {field!r}"
            )
        manifest_task[field] = plan_task[field]

    return {
        "plan_sha256": plan_sha256,
        "current_task": task_id,
        "tasks": [manifest_task],
    }


def manifest_bytes(manifest: dict) -> bytes:
    """Return the canonical JSON bytes of the manifest (the single stable
    serialization the Store stages and hashes)."""
    return canonical.canonical_json_bytes(manifest)


def manifest_sha_of_bytes(data: bytes) -> str:
    """Return the sha256 over the manifest's canonical bytes. This is the value
    stored in state.json / the review bundle — never inside the manifest file."""
    return canonical.sha256_hex(data)


def inject_for_review(bundle: dict, *, manifest: dict) -> dict:
    """Return a NEW bundle dict with the manifest and its sha attached, so Grok
    can review against the manifest and the recorded sha is carried for binding.

    Does NOT mutate ``bundle`` (pure function; callers keep a stable view). The
    injected ``task_manifest_sha256`` equals ``manifest_sha_of_bytes(manifest_bytes(manifest))``.
    """
    data = manifest_bytes(manifest)
    sha = manifest_sha_of_bytes(data)
    out = dict(bundle)
    out["task_manifest"] = manifest
    out["task_manifest_sha256"] = sha
    return out
