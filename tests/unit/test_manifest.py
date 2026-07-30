"""Task 2.5 — self-reference-free task manifest (Prompt §8).

The task-manifest is hashed for review binding, so it must NOT contain its own
hash field: a self-referential hash would change its own value whenever written,
making the binding circular and uncheckable. The manifest's sha256 is therefore
stored in state.json / a sidecar, NOT inside the manifest file.

manifest.py is a PURE BUILDER (consistency #2): it builds the manifest dict and
produces its canonical bytes for the Store to stage; it does NOT write any
authoritative control_root file itself. The single artifact-commit path belongs
to the Store's prepare_generation + finish_operation (Task 3.5).

Schema for the manifest (Spec §4.3, minus the self-hash):
  { plan_sha256, current_task, tasks:[{ id, verification_commands,
        acceptance_criteria, declared_files, planned_changes }] }
"""

from __future__ import annotations

import copy

import pytest

from zcode_agentflow import canonical, manifest


# ---------------------------------------------------------------------------
# fixtures: a minimal FINAL plan-envelope to derive a manifest from
# ---------------------------------------------------------------------------


def _plan_envelope():
    return {
        "run_id": "20260730120000-abc123",
        "status": "READY_FOR_EXECUTION",
        "plan_markdown": "# PLAN\n\nBody.",
        "plan_version": 1,
        "base_commit": "abc1234",
        "tasks": [
            {
                "id": "T001",
                "title": "baseline tests",
                "dependencies": [],
                "declared_files": ["src/App.cpp"],
                "verification_commands": [
                    {"kind": "executable", "program": "pytest",
                     "args": [], "cwd": ".", "timeout_sec": 60},
                ],
                "acceptance_criteria": ["tests pass"],
                "planned_changes": {
                    "may_add_dependencies": False,
                    "may_run_migrations": False,
                    "may_delete_files": [],
                },
            }
        ],
    }


def _plan_sha():
    """A real 64-hex sha for the plan, as the Store would compute it."""
    return "a" * 64


# ---------------------------------------------------------------------------
# build_manifest: shape + no self-hash
# ---------------------------------------------------------------------------


def test_manifest_has_no_self_hash_field():
    """The manifest dict MUST NOT contain its own hash (no `task_manifest_sha256`,
    no `sha256`, no `hash`). The self-hash lives in state.json / a sidecar."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    for forbidden in ("task_manifest_sha256", "sha256", "hash", "self_sha"):
        assert forbidden not in m, f"manifest must not carry {forbidden!r}: {m!r}"
    # required fields ARE present
    assert m["plan_sha256"] == _plan_sha()
    assert m["current_task"] == "T001"
    assert isinstance(m["tasks"], list) and len(m["tasks"]) == 1


def test_manifest_task_subset_of_plan_task():
    """The manifest's task carries the review-relevant fields (id,
    verification_commands, acceptance_criteria, declared_files, planned_changes)
    — the stuff Grok reviews against. Title/dependencies are not required for
    review binding (kept lean), but they may be included; the must-haves are the
    five binding fields."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    t = m["tasks"][0]
    assert t["id"] == "T001"
    assert t["declared_files"] == ["src/App.cpp"]
    assert t["verification_commands"][0]["program"] == "pytest"
    assert "acceptance_criteria" in t and "planned_changes" in t


def test_manifest_current_task_matches_single_task():
    """v1 is a single-task run; current_task == the one task's id."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    assert m["current_task"] == m["tasks"][0]["id"] == "T001"


def test_manifest_rejects_multi_task_plan():
    """A plan with more than one task cannot build a v1 manifest (v1 is single
    task). build_manifest raises rather than silently dropping tasks."""
    plan = _plan_envelope()
    plan["tasks"].append(copy.deepcopy(plan["tasks"][0]))
    with pytest.raises(ValueError):
        manifest.build_manifest(plan, plan_sha256=_plan_sha())


def test_manifest_rejects_empty_plan_sha256():
    """plan_sha256 is the binding anchor; an empty/missing sha is a hard error."""
    with pytest.raises(ValueError):
        manifest.build_manifest(_plan_envelope(), plan_sha256="")


def test_manifest_rejects_non_hex_plan_sha256():
    """plan_sha256 must be 64 lowercase hex (canonical sha form)."""
    with pytest.raises(ValueError):
        manifest.build_manifest(_plan_envelope(), plan_sha256="not-a-hash")


def test_manifest_does_not_carry_authoritative_run_or_commit():
    """The manifest is review-binding data, not an authoritative envelope. It
    does not duplicate run_id / base_commit / plan_version from the plan
    (those live in the plan-envelope and state.json)."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    for forbidden in ("run_id", "base_commit", "plan_version", "plan_markdown"):
        assert forbidden not in m, f"manifest must not carry {forbidden!r}"


# ---------------------------------------------------------------------------
# manifest_bytes + manifest_sha_of_bytes: stable hashing roundtrip
# ---------------------------------------------------------------------------


def test_manifest_bytes_is_canonical_json():
    """manifest_bytes returns canonical_json_bytes of the manifest (deterministic,
    sorted keys, single trailing newline)."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    b = manifest.manifest_bytes(m)
    assert b == canonical.canonical_json_bytes(m)
    assert b.endswith(b"\n")


def test_manifest_bytes_deterministic_across_build_order():
    """Two manifests with equal content produce equal canonical bytes — so the
    stored sha is stable regardless of dict insertion order."""
    m1 = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    m2 = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    assert manifest.manifest_bytes(m1) == manifest.manifest_bytes(m2)


def test_hash_roundtrip(tmp_path):
    """hash_file(manifest on disk) == manifest_sha_of_bytes(canonical bytes).

    The sha is stored in state.json / a sidecar (NOT inside the manifest). The
    Store stages the canonical bytes; the on-disk file's hash equals the sha we
    record — this is the review-binding invariant."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    data = manifest.manifest_bytes(m)
    recorded_sha = manifest.manifest_sha_of_bytes(data)

    # simulate the Store writing the canonical bytes to control_root
    manifest_file = tmp_path / "task-manifest.json"
    manifest_file.write_bytes(data)

    # the on-disk file's canonical hash == the recorded sha
    assert canonical.hash_file(manifest_file) == recorded_sha
    # and the sha is 64 lowercase hex
    assert len(recorded_sha) == 64 and recorded_sha == recorded_sha.lower()


def test_manifest_sha_independent_of_disk_formatting(tmp_path):
    """If the Store writes the manifest via canonical bytes, the sha is stable.
    (hash_file is text-level, so a non-canonical write would diverge — which is
    exactly why the Store always stages canonical bytes.)"""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    canonical_data = manifest.manifest_bytes(m)
    sha = manifest.manifest_sha_of_bytes(canonical_data)
    # writing the canonical bytes => on-disk hash matches
    p = tmp_path / "tm.json"
    p.write_bytes(canonical_data)
    assert canonical.hash_file(p) == sha


# ---------------------------------------------------------------------------
# inject_for_review: the review bundle gets the manifest + its sha
# ---------------------------------------------------------------------------


def test_inject_for_review():
    """inject_for_review attaches the manifest and its sha to a review bundle
    dict, so Grok reviews against the manifest AND the recorded sha is carried
    for binding. The sha injected here == manifest_sha_of_bytes(manifest_bytes)."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    data = manifest.manifest_bytes(m)
    expected_sha = manifest.manifest_sha_of_bytes(data)

    bundle = {"plan_sha256": _plan_sha(), "task_id": "T001"}
    out = manifest.inject_for_review(bundle, manifest=m)

    assert out["task_manifest"] == m
    assert out["task_manifest_sha256"] == expected_sha
    # the injected sha equals the canonical-bytes sha (the binding value)
    assert out["task_manifest_sha256"] == canonical.sha256_hex(data)


def test_inject_for_review_does_not_mutate_input():
    """inject_for_review returns a new dict and does not mutate the caller's
    bundle (pure function; no surprises for the caller)."""
    m = manifest.build_manifest(_plan_envelope(), plan_sha256=_plan_sha())
    bundle = {"plan_sha256": _plan_sha()}
    snapshot = dict(bundle)
    out = manifest.inject_for_review(bundle, manifest=m)
    assert bundle == snapshot  # input unchanged
    assert out is not bundle  # new dict returned


# ---------------------------------------------------------------------------
# consistency #2: manifest.py never writes an authoritative control_root file
# ---------------------------------------------------------------------------


def test_manifest_does_not_write_authoritative_control_root(tmp_path):
    """manifest.py exposes ONLY builders + bytes/sha helpers; it has NO function
    that writes task-manifest.json (or anything) to control_root. Authoritative
    on-disk persistence is the Store's job (Task 3.5)."""
    public = [n for n in dir(manifest) if not n.startswith("_")]
    # NO write-to-disk function exists on the public surface
    for name in public:
        assert not name.lower().startswith(("write", "save", "persist", "store",
                                             "commit", "dump")), (
            f"manifest.py must not expose a writer {name!r} (consistency #2)"
        )
    # the canonical write is owned by canonical.py, NOT re-exported here
    assert not hasattr(manifest, "write_canonical_json") or \
        manifest.write_canonical_json is canonical.write_canonical_json
