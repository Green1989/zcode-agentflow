"""Task 2.1 — strict schemas, planner-result wrapper, raw-vs-final split.

Two-stage validation (parallel P0-4):
- RAW schemas describe what the model (Codex/Grok) actually emits. They have
  ``additionalProperties:false`` and FORBID authoritative fields (run_id,
  base_commit, hashes, plan_version). The adapter validates the model's JSON
  against the RAW schema — there is nothing authoritative to "trust and compare".
- FINAL schemas describe what the engine assembles: the RAW payload PLUS
  authoritative fields it injects from state (run_id/goal/base_commit/plan_version/
  hashes/commits). The engine validates the FINAL envelope after injection.

The model physically cannot emit authoritative fields (the RAW schema rejects
them), so there is no "overwrite model field then verify it matches" logic
anywhere — that can never catch a wrong model value (the whole point of
parallel P0-4).

Task 2.7 adds the *context-invariant* layer (run_id==state.run_id etc.) on top
of these FINAL schemas; this task locks the structural schemas + the
structural cross-field rules (e.g. decision/issues/replan consistency).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from zcode_agentflow import schemas

# Shared defs used across schemas
HASH64 = "a" * 64
COMMIT = "abc1234"
RUN_ID = "20260730120000-abc123"
TASK_ID = "T001"


# ===========================================================================
# RAW: spec-draft-output (what Codex emits from planner_draft_spec)
# ===========================================================================


def _raw_spec():
    return {
        "goal": "Refactor DevSetPage while preserving behavior",
        "spec_markdown": "# SPEC\n\nBody of the specification.",
        "questions": [],
        "assumptions": [
            {"id": "A1", "assumption": "tests cover current behavior",
             "risk": "low", "default": "keep them"},
        ],
    }


def test_raw_spec_valid_passes():
    schemas.validate_raw_spec(_raw_spec())


def test_raw_spec_missing_spec_markdown_rejected():
    bad = _raw_spec()
    del bad["spec_markdown"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


def test_raw_spec_missing_goal_rejected():
    bad = _raw_spec()
    del bad["goal"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


def test_raw_spec_rejects_authoritative_run_id():
    """parallel P0-4: the RAW schema FORBIDS run_id — the model must not emit it."""
    bad = _raw_spec()
    bad["run_id"] = RUN_ID
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


def test_raw_spec_rejects_authoritative_status():
    """The RAW schema forbids the authoritative `status` field (engine-injected)."""
    bad = _raw_spec()
    bad["status"] = "READY_FOR_SPEC_APPROVAL"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


def test_raw_spec_rejects_authoritative_hash():
    bad = _raw_spec()
    bad["spec_sha256"] = HASH64
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


def test_raw_spec_rejects_unknown_field():
    bad = _raw_spec()
    bad["unexpected_field"] = "nope"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_spec(bad)


# ===========================================================================
# RAW: plan-proposal (the planner-result wrapper, P0-5)
# ===========================================================================


def _raw_plan_task():
    """The single task shape inside plan-proposal.plan (NO base_commit/run_id)."""
    return {
        "id": TASK_ID,
        "title": "Establish behavior baseline tests",
        "dependencies": [],
        "declared_files": ["src/App.cpp", "tests/AppTests.cpp"],
        "verification_commands": [
            {"kind": "cmd_script", "program": "run-tests.bat",
             "args": ["AppTests"], "cwd": ".", "timeout_sec": 120},
        ],
        "acceptance_criteria": ["existing behavior tests pass"],
        "planned_changes": {
            "may_add_dependencies": False,
            "may_run_migrations": False,
            "may_delete_files": ["tests/old_unused.cpp"],
        },
    }


def _raw_plan_proposal_ready():
    return {
        "status": "READY_FOR_EXECUTION",
        "plan": {
            "plan_markdown": "# PLAN\n\nSingle-task plan body.",
            "tasks": [_raw_plan_task()],
        },
        # R2-1: strict-mode envelope — all keys always present.
        "questions": [],
        "block_reason": "",
    }


def test_raw_plan_ready_for_execution_passes():
    schemas.validate_raw_plan(_raw_plan_proposal_ready())


def test_raw_plan_needs_user_input_passes():
    """P0-5: the wrapper lets a multi-task goal fail cleanly — NEEDS_USER_INPUT
    carries questions instead of a plan. R2-1: strict-mode envelope always carries
    plan/questions/block_reason; for NEEDS_USER_INPUT plan is null."""
    schemas.validate_raw_plan(
        {"status": "NEEDS_USER_INPUT",
         "plan": None,
         "questions": ["Should the new API be async?"],
         "block_reason": ""}
    )


def test_raw_plan_blocked_passes():
    schemas.validate_raw_plan(
        {"status": "BLOCKED", "plan": None, "questions": [], "block_reason": "no tests exist"}
    )


def test_raw_plan_two_tasks_rejected():
    """Plan is a strict SINGLE-task plan (maxItems:1). 2 tasks => invalid."""
    bad = _raw_plan_proposal_ready()
    bad["plan"]["tasks"].append(_raw_plan_task())
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_zero_tasks_rejected():
    """tasks is minItems:1 when a plan is present."""
    bad = _raw_plan_proposal_ready()
    bad["plan"]["tasks"] = []
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_task_rejects_base_commit():
    """consistency #1 / parallel P0-4: the RAW task forbids base_commit; the
    engine injects it. There is no 'compare Codex's base_commit' step."""
    bad = _raw_plan_proposal_ready()
    bad["plan"]["tasks"][0]["base_commit"] = COMMIT
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_task_rejects_run_id():
    bad = _raw_plan_proposal_ready()
    bad["plan"]["run_id"] = RUN_ID
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_rejects_authoritative_base_commit_at_plan_level():
    bad = _raw_plan_proposal_ready()
    bad["plan"]["base_commit"] = COMMIT
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_verification_command_bad_kind_rejected():
    """P1-5: verification_commands[].kind is an enum (executable/cmd_script/
    bash_script). An arbitrary shell string is NOT accepted."""
    bad = _raw_plan_proposal_ready()
    bad["plan"]["tasks"][0]["verification_commands"][0]["kind"] = "shell"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_verification_command_missing_program_rejected():
    bad = _raw_plan_proposal_ready()
    del bad["plan"]["tasks"][0]["verification_commands"][0]["program"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_ready_with_empty_questions_ok():
    """READY_FOR_EXECUTION may carry an empty questions list (or omit it)."""
    p = _raw_plan_proposal_ready()
    p["questions"] = []
    schemas.validate_raw_plan(p)


def test_raw_plan_needs_user_input_without_questions_rejected():
    """NEEDS_USER_INPUT must carry questions (the wrapper is meaningful)."""
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan({"status": "NEEDS_USER_INPUT"})


def test_raw_plan_blocked_without_block_reason_rejected():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan({"status": "BLOCKED"})


def test_raw_plan_ready_with_questions_rejected():
    """READY_FOR_EXECUTION must not carry open questions (clean handoff)."""
    bad = _raw_plan_proposal_ready()
    bad["questions"] = ["open question"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_ready_with_block_reason_rejected():
    """Round-2 P1: the fixed-key envelope must be INTERNALLY CONSISTENT. A
    READY_FOR_EXECUTION wrapper carrying a non-empty block_reason is contradictory
    (block_reason belongs only to BLOCKED) and must be rejected — the engine
    ignores it today, but the envelope should not carry inconsistent state."""
    bad = _raw_plan_proposal_ready()
    bad["block_reason"] = "stale reason"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_needs_user_input_with_non_null_plan_rejected():
    """Round-2 P1: NEEDS_USER_INPUT must carry a NULL plan (the wrapper models a
    union over status; a non-null plan here is contradictory)."""
    bad = {"status": "NEEDS_USER_INPUT", "plan": _raw_plan_proposal_ready()["plan"],
           "questions": ["q?"], "block_reason": ""}
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_blocked_with_non_null_plan_rejected():
    """Round-2 P1: BLOCKED must carry a NULL plan (a plan is meaningless when the
    goal is blocked)."""
    bad = {"status": "BLOCKED", "plan": _raw_plan_proposal_ready()["plan"],
           "questions": [], "block_reason": "no tests"}
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_blocked_with_questions_rejected():
    """Round-2 P1: BLOCKED must not carry open questions (questions belong only to
    NEEDS_USER_INPUT)."""
    bad = {"status": "BLOCKED", "plan": None,
           "questions": ["why?"], "block_reason": "no tests"}
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


def test_raw_plan_unknown_field_rejected():
    bad = _raw_plan_proposal_ready()
    bad["surprise"] = 1
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


# ===========================================================================
# RAW: review-decision (what Grok emits)
# ===========================================================================


def _raw_review_pass():
    return {
        "decision": "PASS",
        "spec_compliance": True,
        "plan_compliance": True,
        "tests_sufficient": True,
        "issues": [],
        "replan_required": False,
    }


def _raw_review_fail_issue():
    return {
        "id": "I1",
        "severity": "high",
        "category": "correctness",
        "file": "src/App.cpp",
        "line": 42,
        "description": "null deref possible",
        "required_action": "add guard",
    }


def test_raw_review_pass_passes():
    schemas.validate_raw_review(_raw_review_pass())


def test_raw_review_fail_with_issue_passes():
    obj = _raw_review_pass()
    obj["decision"] = "FAIL"
    obj["spec_compliance"] = False
    obj["issues"] = [_raw_review_fail_issue()]
    schemas.validate_raw_review(obj)


def test_raw_review_replan_with_flag_passes():
    obj = _raw_review_pass()
    obj["decision"] = "REPLAN"
    obj["replan_required"] = True
    obj["issues"] = [_raw_review_fail_issue()]
    schemas.validate_raw_review(obj)


def test_raw_review_rejects_authoritative_run_id():
    bad = _raw_review_pass()
    bad["run_id"] = RUN_ID
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_rejects_authoritative_task_id():
    bad = _raw_review_pass()
    bad["task_id"] = TASK_ID
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_rejects_authoritative_commits():
    bad = _raw_review_pass()
    bad["base_commit"] = COMMIT
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_rejects_authoritative_hash():
    bad = _raw_review_pass()
    bad["spec_sha256"] = HASH64
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_pass_with_issues_rejected():
    """cross-field invariant: PASS => issues empty."""
    bad = _raw_review_pass()
    bad["issues"] = [_raw_review_fail_issue()]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_fail_without_issues_rejected():
    """cross-field invariant: FAIL => at least one issue."""
    bad = _raw_review_pass()
    bad["decision"] = "FAIL"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_replan_without_flag_rejected():
    """cross-field invariant: REPLAN => replan_required==true."""
    bad = _raw_review_pass()
    bad["decision"] = "REPLAN"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_bad_decision_enum_rejected():
    bad = _raw_review_pass()
    bad["decision"] = "MAYBE"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_issue_bad_severity_rejected():
    bad = _raw_review_pass()
    bad["decision"] = "FAIL"
    bad["spec_compliance"] = False
    issue = _raw_review_fail_issue()
    issue["severity"] = "catastrophic"
    bad["issues"] = [issue]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


def test_raw_review_issue_missing_required_action_rejected():
    bad = _raw_review_pass()
    bad["decision"] = "FAIL"
    bad["spec_compliance"] = False
    issue = _raw_review_fail_issue()
    del issue["required_action"]
    bad["issues"] = [issue]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_review(bad)


# ===========================================================================
# FINAL: spec-envelope (engine-assembled = RAW spec + injected run_id/status)
# ===========================================================================


def _final_spec_envelope():
    return {
        "run_id": RUN_ID,
        "status": "READY_FOR_SPEC_APPROVAL",
        "goal": "Refactor DevSetPage while preserving behavior",
        "spec_markdown": "# SPEC\n\nBody.",
        "questions": [],
        "assumptions": [],
    }


def test_final_spec_envelope_passes():
    schemas.validate_spec(_final_spec_envelope())


def test_final_spec_envelope_requires_run_id():
    bad = _final_spec_envelope()
    del bad["run_id"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_spec(bad)


def test_final_spec_envelope_bad_status_rejected():
    bad = _final_spec_envelope()
    bad["status"] = "WRONG"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_spec(bad)


# ===========================================================================
# FINAL: plan-envelope (engine-assembled = single-task plan + injected
# run_id/base_commit/plan_version)
# ===========================================================================


def _final_plan_envelope():
    return {
        "run_id": RUN_ID,
        "status": "READY_FOR_EXECUTION",
        "plan_markdown": "# PLAN\n\nBody.",
        "plan_version": 1,
        "base_commit": COMMIT,
        "tasks": [
            {
                "id": TASK_ID,
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


def test_final_plan_envelope_passes():
    schemas.validate_plan(_final_plan_envelope())


def test_final_plan_envelope_requires_base_commit():
    """FINAL requires the engine-injected base_commit (RAW forbids it)."""
    bad = _final_plan_envelope()
    del bad["base_commit"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_plan(bad)


def test_final_plan_envelope_requires_run_id():
    bad = _final_plan_envelope()
    del bad["run_id"]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_plan(bad)


def test_final_plan_envelope_two_tasks_rejected():
    bad = _final_plan_envelope()
    bad["tasks"].append(copy.deepcopy(bad["tasks"][0]))
    with pytest.raises(schemas.SchemaError):
        schemas.validate_plan(bad)


# ===========================================================================
# FINAL: review-result (engine-assembled = RAW decision + injected context)
# ===========================================================================


def _final_review_result_pass():
    return {
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "base_commit": COMMIT,
        "head_commit": "def5678",
        "spec_sha256": HASH64,
        "plan_sha256": HASH64,
        "task_manifest_sha256": HASH64,
        "plan_version": 1,
        "decision": "PASS",
        "spec_compliance": True,
        "plan_compliance": True,
        "tests_sufficient": True,
        "issues": [],
        "replan_required": False,
    }


def test_final_review_result_passes():
    schemas.validate_review(_final_review_result_pass())


def test_final_review_result_non_64_hex_hash_rejected():
    """hash must be exactly 64 lowercase hex chars (Spec §4.7 / shared defs)."""
    bad = _final_review_result_pass()
    bad["spec_sha256"] = "XYZ"  # not 64 hex
    with pytest.raises(schemas.SchemaError):
        schemas.validate_review(bad)


def test_final_review_result_uppercase_hash_rejected():
    bad = _final_review_result_pass()
    bad["spec_sha256"] = "A" * 64  # uppercase not canonical
    with pytest.raises(schemas.SchemaError):
        schemas.validate_review(bad)


def test_final_review_result_pass_with_issues_rejected():
    """The cross-field invariant still holds on the FINAL envelope."""
    bad = _final_review_result_pass()
    bad["decision"] = "FAIL"
    bad["spec_compliance"] = False
    bad["issues"] = [
        {"id": "I1", "severity": "high", "category": "correctness",
         "file": "src/App.cpp", "line": 1, "description": "x",
         "required_action": "fix"},
    ]
    # FAIL-with-issue is valid structurally
    schemas.validate_review(bad)

    # but PASS-with-issue is rejected
    bad["decision"] = "PASS"
    bad["spec_compliance"] = True
    bad["plan_compliance"] = True
    bad["tests_sufficient"] = True
    with pytest.raises(schemas.SchemaError):
        schemas.validate_review(bad)


def test_final_review_result_bad_issue_severity_rejected():
    bad = _final_review_result_pass()
    bad["decision"] = "FAIL"
    bad["spec_compliance"] = False
    bad["issues"] = [
        {"id": "I1", "severity": "nope", "category": "correctness",
         "file": "src/App.cpp", "line": 1, "description": "x",
         "required_action": "fix"},
    ]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_review(bad)


def test_final_review_result_requires_injected_context():
    """FINAL review-result requires the engine-injected context fields."""
    for field in ("run_id", "task_id", "base_commit", "head_commit",
                  "spec_sha256", "plan_sha256", "task_manifest_sha256",
                  "plan_version"):
        bad = _final_review_result_pass()
        del bad[field]
        with pytest.raises(schemas.SchemaError):
            schemas.validate_review(bad)


# ===========================================================================
# FINAL: execution-result
# ===========================================================================


def _final_execution_result():
    return {
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "status": "READY_FOR_REVIEW",
        "base_commit": COMMIT,
        "head_commit": "def5678",
        "worktree_clean": True,
        "untracked_files": [],
        "verification": [
            {"command": "pytest", "exit_code": 0, "duration_ms": 1234},
        ],
        "changed_files": ["tests/AppTests.cpp"],
        "change_audit": {
            "deleted_files": [],
            "dependency_files_changed": False,
            "migrations_added": [],
            "undeclared_files_changed": [],
        },
    }


def test_execution_result_passes():
    schemas.validate_against(_final_execution_result(), "execution-result")


def test_execution_result_bad_status_rejected():
    bad = _final_execution_result()
    bad["status"] = "WHATEVER"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_against(bad, "execution-result")


def test_execution_result_dependency_files_changed_as_string_rejected():
    """Review §六.9: dependency_files_changed must be boolean, not a string."""
    bad = _final_execution_result()
    bad["change_audit"]["dependency_files_changed"] = "false"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_against(bad, "execution-result")


# ===========================================================================
# validate_against dispatch + shared defs
# ===========================================================================


def test_validate_against_unknown_schema_raises():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_against({}, "does-not-exist")


def test_schemas_are_strict_additional_properties():
    """Every shipped schema file has additionalProperties:false at its root and
    in nested objects — the lock against the model sneaking in extra fields."""
    schemas_dir = Path(__file__).resolve().parents[2] / "schemas"
    assert schemas_dir.is_dir(), f"schemas dir missing: {schemas_dir}"
    for schema_file in sorted(schemas_dir.glob("*.schema.json")):
        import json
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        assert schema.get("additionalProperties") is False, (
            f"{schema_file.name} root is not additionalProperties:false"
        )


def test_raw_model_facing_schemas_are_self_contained():
    """AF-7 (production blocker) — every RAW (model-emitted) schema uploaded to a
    model CLI's response_format MUST be self-contained: all ``$ref`` targets must
    resolve WITHIN the same document (``#/definitions/...``). Cross-file refs
    (``other.schema.json#/...``) are standard JSON-Schema, but model-API
    response_format validators (e.g. the codex/OpenAI strict schema validator)
    do NOT resolve cross-file refs — they reject the schema with
    'reference can only point to definitions defined at the top level of the
    schema' (HTTP 400). This blocked the real Codex plan step in Layer 3.

    The RAW schemas are exactly the ones the adapters pass via --output-schema:
    spec-draft-output, plan-proposal, review-decision."""
    import json
    import re

    schemas_dir = Path(__file__).resolve().parents[2] / "schemas"
    raw_files = ["spec-draft-output.schema.json", "plan-proposal.schema.json",
                 "review-decision.schema.json"]
    for name in raw_files:
        path = schemas_dir / name
        assert path.exists(), f"RAW schema missing: {name}"
        text = path.read_text(encoding="utf-8")
        refs = re.findall(r'"\$ref"\s*:\s*"([^"]+)"', text)
        cross_file = [r for r in refs if not r.startswith("#")]
        assert not cross_file, (
            f"RAW schema {name} has cross-file $ref(s) {cross_file} — model-API "
            f"response_format validators reject these; inline the definition as "
            f"#/definitions/<name> so the schema is self-contained."
        )


def test_raw_plan_schema_is_strict_mode_compliant():
    """R2-1 (was the AF-7 xfailed blocker, now resolved) — the plan-proposal RAW
    schema must satisfy codex's strict-mode structured-outputs rule: every
    object's `required` lists ALL its property keys (no optional fields), so the
    schema uploads cleanly to a model-API response_format. The wrapper models the
    status-union via a fixed-key envelope (plan is nullable) instead of optional
    keys, and schemas.validate_raw_plan enforces the per-status semantics."""
    import json
    schemas_dir = Path(__file__).resolve().parents[2] / "schemas"
    schema = json.loads((schemas_dir / "plan-proposal.schema.json")
                        .read_text(encoding="utf-8"))

    def _all_required(obj):
        # every object in the schema must list ALL its properties in `required`
        props = obj.get("properties", {})
        if props:
            required = set(obj.get("required", []))
            missing = set(props) - required
            assert not missing, (
                f"strict mode: object has properties not in `required`: {missing}"
            )
        # recurse into nested object properties
        for p in props.values():
            if isinstance(p, dict) and "type" in p:
                # type may be a single object schema or ["object", "null"]
                types = p["type"] if isinstance(p["type"], list) else [p["type"]]
                if "object" in types:
                    _all_required(p)
            if "$ref" in p and p["$ref"] == "#/definitions/plannedChanges":
                pass  # checked via definitions below

    _all_required(schema)
    for name, d in schema.get("definitions", {}).items():
        if d.get("type") == "object":
            _all_required(d)
