"""Strict schema loading + two-stage validation (Task 2.1; parallel P0-4).

Two stages (the whole point of parallel P0-4):
- RAW schemas describe what the model (Codex/Grok) actually emits. They forbid
  authoritative fields (run_id, base_commit, hashes, plan_version) via
  ``additionalProperties:false``. The adapter validates the model's JSON against
  the RAW schema. There is no "overwrite then verify" — the model never had the
  authoritative field, so there is nothing to compare against.
- FINAL schemas describe what the engine assembles: the RAW payload PLUS
  authoritative fields injected from state. The engine validates the FINAL
  envelope after injection, THEN runs the context-invariant layer (Task 2.7).

This module is a *structural* validator only. The cross-field rules that depend
on the current run (run_id==state.run_id, base_commit==state.base_commit, the
single task being T001, relative paths, program allowlist) live in Task 2.7's
context-invariant checkers, which run on top of a structurally-valid FINAL
envelope. Here we enforce only the structural cross-field rules that are
intrinsic to a review/plan regardless of the run (decision/issues/replan
consistency, wrapper status/questions consistency).

Schema files live in the repo's ``schemas/`` directory and use cross-file
``$ref`` (e.g. ``spec-envelope.schema.json#/definitions/hash``). We build a
single :class:`jsonschema.RefResolver` that resolves any shipped schema by its
``$id`` so the references work regardless of the caller's cwd.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import referencing
from referencing.jsonschema import DRAFT7

__all__ = [
    "SCHEMAS_DIR",
    "SchemaError",
    "load_schema",
    "validate_against",
    "validate_raw_spec",
    "validate_raw_plan",
    "validate_raw_review",
    "validate_spec",
    "validate_plan",
    "validate_review",
]

# schemas/ lives at the repo root (the future plugin_root); it is a committed
# source directory, NOT a runtime artifact.
_REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = Path(
    os.environ.get("AGENTFLOW_SCHEMAS_DIR", _REPO_ROOT / "schemas")
)


class SchemaError(ValueError):
    """Raised when an object fails schema validation or a cross-field invariant.

    This is a ValueError subclass so it interoperates with the rest of the
    codebase's input-validation conventions.
    """


# ---------------------------------------------------------------------------
# Schema registry: load every shipped schema once, indexed by name and $id.
# ---------------------------------------------------------------------------


def _load_all_schemas(schemas_dir: Path) -> tuple[dict, dict]:
    """Load every ``*.schema.json`` in ``schemas_dir``.

    Returns ``(by_name, by_id)``: by_name maps the stem (e.g.
    ``spec-envelope``) to the schema dict; by_id maps the schema's ``$id``
    (e.g. ``spec-envelope.schema.json``) to the same dict for $ref resolution.
    """
    by_name: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    if not schemas_dir.is_dir():
        raise SchemaError(f"schemas directory not found: {schemas_dir}")
    for path in sorted(schemas_dir.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        name = path.name[: -len(".schema.json")]
        by_name[name] = schema
        sid = schema.get("$id", path.name)
        by_id[sid] = schema
    return by_name, by_id


_BY_NAME, _BY_ID = _load_all_schemas(SCHEMAS_DIR)


def _registry() -> "referencing.Registry":
    """A referencing.Registry that resolves any shipped schema by its $id.

    Uses the modern ``referencing`` API (jsonschema>=4.18 deprecates
    ``RefResolver`` in its favor). Each schema is registered under both its
    ``$id`` and its bare filename so cross-file ``$ref`` like
    ``spec-envelope.schema.json#/definitions/hash`` resolves.
    """
    resources = []
    for sid, schema in _BY_ID.items():
        # Register under the $id as-is. ``referencing`` keys by the retrieval
        # URI; the $id values here are plain filenames, which match the $ref
        # targets used across these schemas.
        resources.append((sid, DRAFT7.create_resource(schema)))
    return referencing.Registry().with_resources(resources)


def load_schema(name: str) -> dict:
    """Return the schema dict for ``name`` (e.g. ``spec-envelope``)."""
    try:
        return _BY_NAME[name]
    except KeyError:
        known = ", ".join(sorted(_BY_NAME))
        raise SchemaError(
            f"unknown schema {name!r}; known schemas: {known}"
        ) from None


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def validate_against(obj, schema_name: str) -> None:
    """Validate ``obj`` against the named schema; raise SchemaError on failure."""
    schema = load_schema(schema_name)
    registry = _registry()
    try:
        jsonschema.validate(instance=obj, schema=schema, registry=registry)
    except jsonschema.ValidationError as exc:
        raise SchemaError(
            f"{schema_name} validation failed: {exc.message} "
            f"(at path {list(exc.absolute_path)})"
        ) from exc


# ---------------------------------------------------------------------------
# RAW validation (model output) + structural cross-field invariants
# ---------------------------------------------------------------------------


def _check_decision_invariants(obj: dict) -> None:
    """Cross-field rules intrinsic to a review decision, regardless of run.

      decision == PASS   => issues empty AND spec/plan/tests_sufficient all true
      decision == FAIL   => at least one issue
      decision == REPLAN => replan_required == true
    """
    decision = obj["decision"]
    issues = obj.get("issues", [])
    if decision == "PASS":
        if issues:
            raise SchemaError("decision=PASS requires empty issues")
        if not (obj.get("spec_compliance") and obj.get("plan_compliance")
                and obj.get("tests_sufficient")):
            raise SchemaError(
                "decision=PASS requires spec_compliance, plan_compliance, "
                "and tests_sufficient all true"
            )
    elif decision == "FAIL":
        if not issues:
            raise SchemaError("decision=FAIL requires at least one issue")
    elif decision == "REPLAN":
        if not obj.get("replan_required"):
            raise SchemaError("decision=REPLAN requires replan_required==true")


def validate_raw_spec(obj: dict) -> None:
    """Validate the RAW spec-draft-output (Codex output). No authoritative fields."""
    validate_against(obj, "spec-draft-output")


def validate_raw_plan(obj: dict) -> None:
    """Validate the RAW plan-proposal wrapper (Codex output) + wrapper-status rules.

    Wrapper status semantics (P0-5):
      READY_FOR_EXECUTION => a plan is present and questions is empty/absent
      NEEDS_USER_INPUT    => questions non-empty (no plan required)
      BLOCKED             => block_reason present
    """
    validate_against(obj, "plan-proposal")
    status = obj["status"]
    if status == "READY_FOR_EXECUTION":
        if "plan" not in obj:
            raise SchemaError("status=READY_FOR_EXECUTION requires a plan")
        questions = obj.get("questions", [])
        if questions:
            raise SchemaError(
                "status=READY_FOR_EXECUTION must not carry open questions"
            )
    elif status == "NEEDS_USER_INPUT":
        if not obj.get("questions"):
            raise SchemaError(
                "status=NEEDS_USER_INPUT requires non-empty questions"
            )
    elif status == "BLOCKED":
        if not obj.get("block_reason"):
            raise SchemaError("status=BLOCKED requires block_reason")


def validate_raw_review(obj: dict) -> None:
    """Validate the RAW review-decision (Grok output) + structural cross-field rules."""
    validate_against(obj, "review-decision")
    _check_decision_invariants(obj)


# ---------------------------------------------------------------------------
# FINAL validation (engine-assembled, post-injection)
# ---------------------------------------------------------------------------


def validate_spec(obj: dict) -> None:
    """Validate the FINAL spec-envelope (RAW spec + injected run_id/status).

    The context invariants that depend on the current run (run_id==state.run_id,
    goal==state.goal) are checked by Task 2.7's check_spec_context; this is the
    structural validator.
    """
    validate_against(obj, "spec-envelope")


def validate_plan(obj: dict) -> None:
    """Validate the FINAL plan-envelope (single-task plan + injected run_id/
    base_commit/plan_version). Context invariants land in Task 2.7."""
    validate_against(obj, "plan-envelope")


def validate_review(obj: dict) -> None:
    """Validate the FINAL review-result (RAW decision + injected context) and
    re-check the structural decision invariants on the assembled envelope."""
    validate_against(obj, "review-result")
    _check_decision_invariants(obj)
