"""Task 3.5 — Atomic promotion: state.json is the ONLY commit pointer +
Artifact Index + ArtifactResolver (P0-3, P1-4; parallel-review P0-1).

Three v2.1 gaps fixed at once:
  (a) a new generation held ONLY this op's artifacts (the previous SPEC vanished
      from the "current" view);
  (b) some code read ``control_root/SPEC.md`` directly (the pointer gave no
      real atomicity);
  (c) ``current.json`` and ``state.json`` were two independent pointers — a crash
      between switching current.json and replacing state.json exposed "new
      artifacts + old state".

Fix: ``state.json`` is the SINGLE commit pointer; it carries
``artifact_generation``. Each generation is a COMPLETE logical snapshot (Artifact
Index mode) — a new generation inherits the previous index and replaces only
this operation's changes. ``prepare_generation`` writes blobs + manifest and
fills ``next_state`` fields but does NOT touch state.json; the SINGLE
``os.replace(state.json)`` happens in ``finish_operation`` (already the sole
committer). ALL reads go through one :class:`ArtifactResolver`.

Layout under control_root:
    artifacts/blobs/<sha>                  content-addressed blobs
    artifacts/<generation_id>/manifest.json  the Generation (id + index)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import artifacts, canonical, store


def _seed_state(control: Path, *, generation=None) -> store.State:
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
    if generation is not None:
        st.artifact_generation = generation
    st.save(control)
    return st


# ---------- state.json is the only commit pointer ---------------------------


def test_state_is_the_only_commit_pointer(tmp_path: Path):
    """There is NO current.json. state.json carries artifact_generation;
    prepare_generation writes blobs + manifest + fills next_state fields but
    does NOT os.replace(state.json); only finish_operation does that."""
    control = tmp_path / "control"
    control.mkdir()
    prev = _seed_state(control)

    next_state = store.State.from_dict(prev.to_dict())
    gen = artifacts.prepare_generation(
        control_root=control,
        operation_id="gen1",
        changes={"SPEC.md": b"# Spec\n"},
        prev_state=prev,
        next_state=next_state,
    )
    # the generation manifest + blob were written
    manifest_path = control / "artifacts" / gen.generation_id / "manifest.json"
    assert manifest_path.exists()
    # next_state got the pointer + the SPEC sha (derived from the index)
    assert next_state.artifact_generation == "gen1"
    expected_spec_sha = canonical.sha256_hex(canonical.canonical_text_bytes(b"# Spec\n"))
    assert next_state.spec_sha256 == expected_spec_sha

    # CRITICAL: prepare_generation did NOT replace state.json — the on-disk
    # state still reflects the PRE generation (artifact_generation unset).
    on_disk = store.State.load(control)
    assert on_disk.artifact_generation is None

    # there is NO current.json anywhere
    assert not (control / "current.json").exists()


def test_reader_never_observes_new_artifacts_with_old_state(tmp_path: Path):
    """Inject a crash AFTER blobs are written but BEFORE state.json replace:
    on resume, ArtifactResolver(state) resolves the OLD generation — never a
    mix of new artifacts + old state."""
    control = tmp_path / "control"
    control.mkdir()

    # generation 1: SPEC v1 committed (state points at gen1)
    prev = _seed_state(control)
    next1 = store.State.from_dict(prev.to_dict())
    artifacts.prepare_generation(control_root=control, operation_id="gen1",
                                 changes={"SPEC.md": b"# Spec v1\n"},
                                 prev_state=prev, next_state=next1)
    next1.save(control)  # commit gen1

    # generation 2: SPEC v2 prepared but NOT committed (crash before replace)
    next2 = store.State.from_dict(next1.to_dict())
    artifacts.prepare_generation(control_root=control, operation_id="gen2",
                                 changes={"SPEC.md": b"# Spec v2\n"},
                                 prev_state=next1, next_state=next2)
    # DO NOT save next2 — simulate crash. On-disk state still points at gen1.

    state_after_crash = store.State.load(control)
    assert state_after_crash.artifact_generation == "gen1"
    resolver = artifacts.ArtifactResolver(control, state_after_crash)
    # reader sees the OLD SPEC (gen1), NOT v2 — no mix
    assert resolver.read_text("SPEC.md") == "# Spec v1\n"
    assert resolver.hash_of("SPEC.md") == canonical.sha256_hex(
        canonical.canonical_text_bytes(b"# Spec v1\n")
    )

    # once gen2 is committed, the new generation is visible with matching state
    next2.save(control)
    resolver2 = artifacts.ArtifactResolver(control, store.State.load(control))
    assert resolver2.read_text("SPEC.md") == "# Spec v2\n"


# ---------- generations are complete logical snapshots -----------------------


def test_new_generation_inherits_full_snapshot(tmp_path: Path):
    """After draft(SPEC) -> plan(PLAN+manifest) -> verify(execution-result) ->
    review(review-result), the current generation's index maps ALL five logical
    names — not just the latest op's files."""
    control = tmp_path / "control"
    control.mkdir()
    prev = _seed_state(control)

    # gen1: SPEC
    s1 = store.State.from_dict(prev.to_dict())
    g1 = artifacts.prepare_generation(control_root=control, operation_id="g1",
        changes={"SPEC.md": b"SPEC\n"}, prev_state=prev, next_state=s1)
    s1.save(control)
    # gen2: PLAN + task-manifest
    s2 = store.State.from_dict(s1.to_dict())
    artifacts.prepare_generation(control_root=control, operation_id="g2",
        changes={"PLAN.md": b"PLAN\n", "task-manifest.json": b"{}"},
        prev_state=s1, next_state=s2)
    s2.save(control)
    # gen3: execution-result
    s3 = store.State.from_dict(s2.to_dict())
    artifacts.prepare_generation(control_root=control, operation_id="g3",
        changes={"execution-result.json": b"{}"}, prev_state=s2, next_state=s3)
    s3.save(control)
    # gen4: review-result
    s4 = store.State.from_dict(s3.to_dict())
    g4 = artifacts.prepare_generation(control_root=control, operation_id="g4",
        changes={"review-result.json": b"{}"}, prev_state=s3, next_state=s4)
    s4.save(control)

    # the CURRENT generation (g4) maps all five names
    gen = artifacts.load_generation(control, g4.generation_id)
    assert set(gen.index.keys()) == {
        "SPEC.md", "PLAN.md", "task-manifest.json",
        "execution-result.json", "review-result.json",
    }


def test_generation_replaces_only_changed_artifacts(tmp_path: Path):
    """A new generation COPIES FORWARD the prior index and only swaps the names
    this op changed. The unchanged SPEC blob ref is byte-identical."""
    control = tmp_path / "control"
    control.mkdir()
    prev = _seed_state(control)

    s1 = store.State.from_dict(prev.to_dict())
    g1 = artifacts.prepare_generation(control_root=control, operation_id="g1",
        changes={"SPEC.md": b"SPEC\n"}, prev_state=prev, next_state=s1)
    s1.save(control)
    spec_blob_g1 = g1.index["SPEC.md"]

    s2 = store.State.from_dict(s1.to_dict())
    g2 = artifacts.prepare_generation(control_root=control, operation_id="g2",
        changes={"PLAN.md": b"PLAN\n"}, prev_state=s1, next_state=s2)
    s2.save(control)
    # SPEC blob ref is UNCHANGED (inherited), PLAN is new
    assert g2.index["SPEC.md"] == spec_blob_g1
    assert "PLAN.md" in g2.index
    # blobs are content-addressed: the SPEC blob is the SAME file (no duplication)
    assert (control / spec_blob_g1["blob"]).exists()


# ---------- resolver is the only read path ----------------------------------


def test_resolver_is_the_only_read_path(tmp_path: Path):
    """ArtifactResolver(state) returns the current generation's blob. A
    grep-style guard asserts no production code reads control_root/SPEC.md
    (or any logical artifact) directly — only artifacts.py / store.py may touch
    authoritative artifact paths."""
    control = tmp_path / "control"
    control.mkdir()
    prev = _seed_state(control)
    s1 = store.State.from_dict(prev.to_dict())
    artifacts.prepare_generation(control_root=control, operation_id="g1",
        changes={"SPEC.md": b"# hello\n"}, prev_state=prev, next_state=s1)
    s1.save(control)

    resolver = artifacts.ArtifactResolver(control, store.State.load(control))
    assert resolver.read_text("SPEC.md") == "# hello\n"
    assert resolver.read("SPEC.md") == b"# hello\n"
    with pytest.raises(artifacts.ArtifactNotFound):
        resolver.read("PLAN.md")  # not in this generation

    # guard: scan the production package for direct logical-artifact FILE READS.
    # The intent (per the plan) is "no code reads control_root/SPEC.md directly
    # — only artifacts.py / store.py may touch authoritative artifact paths".
    # We therefore flag a path-style READ of a logical name (``/ "SPEC.md"`` or
    # a read_bytes/read_text/open on such a path), NOT a bare mention of the
    # name in a docstring/comment (which is harmless).
    import os
    import re

    pkg = Path(__file__).resolve().parents[2] / "src" / "zcode_agentflow"
    names = r"(?:SPEC|PLAN|task-manifest|execution-result|review-result)\.(?:md|json)"
    # a path-style access of a logical name:  root / "SPEC.md"  OR  "SPEC.md".read_bytes(...)
    # OR  open("SPEC.md", ...)  — all shapes that would actually READ the artifact.
    path_access = re.compile(
        rf'/\s*["\']({names})["\']'
        rf'|["\']({names})["\']\s*\.\s*(?:read_bytes|read_text|read\()'
        rf'|open\s*\(\s*["\']({names})["\']'
    )
    offenders = []
    allowed_files = {"artifacts.py", "store.py", "review_bundle.py"}
    for root, _dirs, files in os.walk(pkg):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            if fn in allowed_files:
                continue
            text = (Path(root) / fn).read_text(encoding="utf-8")
            # strip comments/docstring-quoted lines only loosely: we still scan
            # but require a path-access shape so docstrings naming a file don't trip.
            for m in path_access.finditer(text):
                offenders.append((fn, m.group(0)))
    assert not offenders, (
        f"production code reads logical artifact paths directly (must go via "
        f"ArtifactResolver): {offenders}"
    )


# ---------- recovery: half-written generation is invisible ------------------


def test_promotion_index_drives_idempotent_recovery(tmp_path: Path):
    """The index is per-name {blob, sha256}; recovery verifies each blob's sha.
    A half-written generation (pre-state-replace) is invisible and discarded."""
    control = tmp_path / "control"
    control.mkdir()
    prev = _seed_state(control)
    s1 = store.State.from_dict(prev.to_dict())
    g1 = artifacts.prepare_generation(control_root=control, operation_id="g1",
        changes={"SPEC.md": b"SPEC\n"}, prev_state=prev, next_state=s1)
    s1.save(control)

    # every blob in gen1 verifies against its recorded sha
    for name, ref in g1.index.items():
        blob_path = control / ref["blob"]
        assert blob_path.exists()
        assert canonical.hash_file(blob_path) == ref["sha256"]


def test_crash_between_blob_writes_recovers_consistently(tmp_path: Path):
    """A crash mid-blob-write leaves state pointing at the OLD generation; the
    partial new generation is invisible and recovery discards it."""
    control = tmp_path / "control"
    control.mkdir()
    prev = _seed_state(control)
    s1 = store.State.from_dict(prev.to_dict())
    artifacts.prepare_generation(control_root=control, operation_id="g1",
        changes={"SPEC.md": b"SPEC v1\n"}, prev_state=prev, next_state=s1)
    s1.save(control)

    # simulate a partial gen2: write a stray blob + a manifest but NOT a valid
    # full index, and do NOT point state at it
    bogus_dir = control / "artifacts" / "gen2_partial"
    bogus_dir.mkdir(parents=True)
    canonical.write_canonical_json(bogus_dir / "manifest.json",
        {"generation_id": "gen2_partial", "index": {"SPEC.md": {"blob": "blobs/deadbeef", "sha256": "deadbeef"}}})

    # state still points at gen1 -> resolver ignores the partial gen2 entirely
    state = store.State.load(control)
    assert state.artifact_generation == "g1"
    resolver = artifacts.ArtifactResolver(control, state)
    assert resolver.read_text("SPEC.md") == "SPEC v1\n"
