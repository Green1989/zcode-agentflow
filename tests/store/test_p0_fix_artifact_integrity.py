"""P0-3 RED: artifact integrity is NOT verified on read / recovery, and the
Review Bundle swallows corruption as a generic exception.

Three sub-defects the reviewer named:

  (a) ``ArtifactResolver.read`` returns blob bytes WITHOUT recomputing and
      comparing the manifest/state sha. ``hash_of`` only trusts the manifest.
  (b) The "recovery verifies each blob" test only checks freshly-written normal
      blobs; it never tampers a blob and then runs recovery. ``recover()`` never
      rehashes a blob.
  (c) ``review_bundle`` catches ``Exception`` for ``execution-result.json`` and
      silently falls back to the caller's object — masking generation/blob
      corruption instead of failing closed.

These tests reproduce each by TAMPERING a blob (or pointing at a corrupt
generation) and asserting the system fails closed (raises), rather than
returning/propagating corrupt content. Expected to FAIL (RED) on the current
code and PASS once:
  - ``ArtifactResolver.read`` rehashes the blob against the manifest and raises
    on mismatch;
  - recovery refuses a corrupt blob/manifest;
  - ``review_bundle`` narrows its ``except`` to ``ArtifactNotFound`` only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import artifacts, canonical, store


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _seed_state(control: Path) -> store.State:
    st = store.State(
        run_id="20260730120000-abc123",
        revision=1,
        state="INITIALIZING_WORKTREE",
        previous_state="NEW",
        plan_version=1,
        current_task="",
        completed_tasks=[],
        roots={
            "plugin_root": str(control / "plugin"),
            "target_repo_root": str(control / "repo"),
            "run_worktree_root": str(control / "wt"),
            "control_root": str(control),
        },
    )
    st.save(control)
    return st


def _commit_spec(control: Path, *, body: bytes = b"# Spec\n") -> store.State:
    """Write + commit a SPEC generation via prepare_generation + a direct state
    pointer set, returning the committed State."""
    prev = _seed_state(control)
    nxt = store.State.from_dict(prev.to_dict())
    artifacts.prepare_generation(
        control_root=control,
        operation_id="gen1",
        changes={"SPEC.md": body},
        prev_state=prev,
        next_state=nxt,
    )
    nxt.artifact_generation = "gen1"
    nxt.save(control)
    return nxt


# --------------------------------------------------------------------------- #
# (a) read() must rehash the blob and fail closed on tamper
# --------------------------------------------------------------------------- #


def test_resolver_read_detects_tampered_blob(tmp_path: Path):
    """RED: ArtifactResolver.read must recompute the blob's sha and compare to
    the manifest. A tampered blob must raise, not return corrupt content.
    """
    control = tmp_path / "control"
    control.mkdir()
    st = _commit_spec(control, body=b"# Spec\n")

    resolver = artifacts.ArtifactResolver(control, st)
    # sanity: reads correctly before tamper
    assert resolver.read_text("SPEC.md").strip() == "# Spec"

    # tamper the blob in place (change content but keep the manifest sha)
    ref = resolver._ref("SPEC.md")
    bpath = control / ref["blob"]
    bpath.write_bytes(b"# TAMPERED CONTENT\n")

    with pytest.raises(Exception):
        # must FAIL CLOSED — recompute sha, mismatch -> raise. Today it returns
        # the tampered bytes silently.
        resolver.read("SPEC.md")


# --------------------------------------------------------------------------- #
# (b) recovery must refuse a corrupt blob/manifest
# --------------------------------------------------------------------------- #


def test_recover_refuses_tampered_blob_in_pointed_generation(tmp_path: Path):
    """RED: when the committed generation's blob has been tampered, recovery
    must detect the mismatch rather than silently rehydrating corrupt content.
    Today recover() never rehashes, so it returns a State whose resolver would
    serve tampered bytes.
    """
    control = tmp_path / "control"
    control.mkdir()
    st = _commit_spec(control, body=b"# Spec\n")

    # tamper the SPEC blob
    gen = artifacts.load_generation(control, st.artifact_generation)
    spec_ref = gen.index["SPEC.md"]
    (control / spec_ref["blob"]).write_bytes(b"# TAMPERED\n")

    # a fresh Store recovering must surface the corruption (fail closed).
    fresh = store.Store(control)
    with pytest.raises(Exception):
        # Either recover() itself raises, OR reading through the recovered
        # state raises. The invariant: tampering must NOT pass silently.
        recovered = fresh.recover()
        artifacts.ArtifactResolver(control, recovered).read("SPEC.md")


# --------------------------------------------------------------------------- #
# (c) review_bundle must NOT swallow corruption as a generic Exception
# --------------------------------------------------------------------------- #


def test_review_bundle_does_not_mask_blob_corruption(tmp_path: Path, monkeypatch):
    """RED: review_bundle's except clause for execution-result.json must be
    narrow (ArtifactNotFound only). A blob-integrity error must propagate, not
    be silently replaced by the caller's exec_result object.
    """
    from zcode_agentflow import review_bundle

    control = tmp_path / "control"
    control.mkdir()
    # commit a generation that contains ALL resolved names, so the bundle loop
    # reaches execution-result.json (SPEC/PLAN/manifest read cleanly first).
    prev = _seed_state(control)
    nxt = store.State.from_dict(prev.to_dict())
    artifacts.prepare_generation(
        control_root=control,
        operation_id="gen1",
        changes={
            "SPEC.md": b"# Spec\n",
            "PLAN.md": b"# Plan\n",
            "task-manifest.json": b'{"tasks": []}\n',
            "execution-result.json": b'{"ok": true}\n',
        },
        prev_state=prev,
        next_state=nxt,
    )
    nxt.artifact_generation = "gen1"
    nxt.save(control)

    resolver = artifacts.ArtifactResolver(control, nxt)
    op_dir = control / "operations" / "op1"
    op_dir.mkdir(parents=True)

    # tamper ONLY the execution-result blob so its read() raises an integrity
    # error (after P0-3(a) makes read() rehash). Today review_bundle catches
    # that as a bare Exception and silently substitutes the caller's object.
    ref = resolver._ref("execution-result.json")
    (control / ref["blob"]).write_bytes(b'{"ok": "TAMPERED"}\n')

    # assemble must propagate the integrity failure (raise), NOT silently fall
    # back to the caller-supplied exec_result.
    with pytest.raises(artifacts.ArtifactCorrupt):
        review_bundle.assemble(
            operation_dir=op_dir,
            resolver=resolver,
            run_worktree_root=control / "wt",
            base_commit="deadbeef",
            head_commit="feedface",
            task={"id": "T001", "title": "t"},
            exec_result={"ok": "caller-supplied fallback"},
            resolved_agent_context="ctx",
            review_prompt="review this",
        )
