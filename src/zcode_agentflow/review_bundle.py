"""Immutable Review Bundle assembly (Task 4.3; Review P0-5; Prompt §13).

The Review Bundle is the COMPLETE, read-only input Grok reviews against. It is
assembled under ``operation_dir`` (staging — promoted by the Store only if/when
finish commits) and contains, per Phase 0 ``grok.bundle_layout``:

  - ``SPEC.md``               — the approved spec (via ArtifactResolver)
  - ``PLAN.md``               — the approved plan (via ArtifactResolver)
  - ``task-manifest.json``    — the task's declared files / verification (via
                                ArtifactResolver)
  - ``execution-result.json`` — the recorded execution outcome (via
                                ArtifactResolver, when present)
  - ``diff.patch``            — the unified diff base..head (via gitops)
  - ``review-request.json``   — this review invocation's metadata (round,
                                commits under review, base/head)
  - ``prompt.md``             — the rendered reviewer prompt + the resolved
                                agent context (so the bundle is self-contained)

ALL logical-artifact reads go through the ArtifactResolver (Task 3.5) — this
module NEVER reads ``control_root/SPEC.md`` directly (the resolver is the only
read path). The bundle is the bridge between the ArtifactResolver's
generation-backed reads and Grok's filesystem-cwd access.

Immutability: ``assemble`` writes the bundle ONCE; the GrokAdapter re-checks
that HEAD is clean and the bundle hashes are unchanged before AND after the
call (a dirty tree or a modified bundle => raise, routing to NEEDS_HUMAN).
"""

from __future__ import annotations

import json
from pathlib import Path

from zcode_agentflow import artifacts as _artifacts
from zcode_agentflow import canonical, gitops

__all__ = ["BUNDLE_DIRNAME", "REQUIRED_FILES", "assemble", "bundle_dir"]


BUNDLE_DIRNAME = "review-bundle"

# The files a Review Bundle must contain (Phase 0 grok.bundle_layout). The
# logical artifacts are read via the resolver; diff.patch/review-request.json/
# prompt.md are assembled here.
REQUIRED_FILES = (
    "SPEC.md",
    "PLAN.md",
    "task-manifest.json",
    "execution-result.json",
    "diff.patch",
    "review-request.json",
    "prompt.md",
)

# logical names that come from the resolver (the rest are assembled here)
_RESOLVED_NAMES = ("SPEC.md", "PLAN.md", "task-manifest.json", "execution-result.json")


def bundle_dir(operation_dir) -> Path:
    """Return the bundle directory path under ``operation_dir``."""
    return Path(operation_dir) / BUNDLE_DIRNAME


def assemble(
    *,
    operation_dir,
    resolver,
    run_worktree_root,
    base_commit,
    head_commit,
    task,
    exec_result,
    resolved_agent_context: str,
    review_prompt: str,
    review_round: int = 1,
) -> Path:
    """Assemble the immutable Review Bundle under ``operation_dir`` and return
    its path.

    Reads SPEC/PLAN/manifest/execution-result through ``resolver`` (the ONLY
    read path for logical artifacts — never control_root root files), computes
    the diff via ``gitops.diff_patch`` (base..head), and writes the
    review-request.json + prompt.md. The bundle is byte-identical for identical
    inputs (canonical writes), so the GrokAdapter's before/after hash check is
    meaningful.

    ``execution-result.json`` is optional: if the resolver does not have it, a
    minimal manifest derived from ``exec_result`` is written so the bundle is
    always complete.
    """
    bdir = bundle_dir(operation_dir)
    bdir.mkdir(parents=True, exist_ok=True)

    # 1. resolved logical artifacts (SPEC/PLAN/manifest/execution-result).
    #    execution-result may be absent in the resolver; fall back to the
    #    caller-supplied exec_result dict ONLY for ArtifactNotFound (the name is
    #    legitimately absent before the first verify). Any other exception — in
    #    particular ArtifactCorrupt (P0-3) — must PROPAGATE, not be masked into
    #    the caller's object, so generation/blob corruption surfaces instead of
    #    silently degrading the review input.
    for name in _RESOLVED_NAMES:
        try:
            data = resolver.read(name)
        except _artifacts.ArtifactNotFound:
            # execution-result.json is the only one that may legitimately be
            # absent; derive it from the caller's exec_result.
            if name == "execution-result.json":
                data = canonical.canonical_json_bytes(exec_result or {})
            else:
                raise
        canonical.write_canonical_text(bdir / name, data)

    # 2. the diff patch (gitops — base..head over the run worktree).
    diff = gitops.diff_patch(run_worktree_root, base_commit, head_commit)
    canonical.write_canonical_text(bdir / "diff.patch", diff.encode("utf-8"))

    # 3. review-request.json — this review invocation's metadata. Carries the
    #    authoritative commits under review (NO model output — pure context).
    review_request = {
        "review_round": review_round,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "task_id": task.get("id") if isinstance(task, dict) else None,
        "task_title": task.get("title") if isinstance(task, dict) else None,
    }
    canonical.write_canonical_json(bdir / "review-request.json", review_request)

    # 4. prompt.md — the reviewer prompt + the inlined resolved agent context,
    #    so the bundle is self-contained (Grok's cwd is the bundle).
    prompt_body = review_prompt.rstrip("\n") + "\n\n"
    if resolved_agent_context:
        prompt_body += (
            "## Resolved agent context (inlined)\n\n"
            + resolved_agent_context.rstrip("\n") + "\n"
        )
    canonical.write_canonical_text(bdir / "prompt.md", prompt_body.encode("utf-8"))

    return bdir
