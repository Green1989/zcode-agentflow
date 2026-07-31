"""Task 2.7 — model-output context invariants (P1-3).

Beyond field TYPES (Task 2.1's structural schemas), validate that planner/review
output is CONSISTENT WITH THE CURRENT RUN. Untrusted fields (especially
``base_commit``) are injected/checked, never trusted from the model.

Two-layer consistency (parallel P0-4 / consistency #1):
  1. The RAW schema forbids authoritative fields entirely (run_id, base_commit,
     hashes) — so the model physically cannot emit them. There is NO "compare
     Codex's base_commit" step anywhere.
  2. The engine INJECTS run_id/goal/base_commit/plan_version from state into the
     FINAL envelope, then validates the FINAL schema (Task 2.1) AND runs these
     context-invariant checks: the FINAL fields must equal the state values.

This module tests the context-invariant layer (check_spec_context /
check_plan_context). The structural schemas are already locked in Task 2.1; here
we add the run-context rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import schemas

RUN_ID = "20260730120000-abc123"
BASE_COMMIT = "abc1234"
HASH64 = "a" * 64


# ---------------------------------------------------------------------------
# FINAL spec envelope: run_id / goal / status-vs-questions consistency
# ---------------------------------------------------------------------------


def _final_spec(*, run_id=RUN_ID, status="READY_FOR_SPEC_APPROVAL",
                goal="Refactor X", questions=None):
    return {
        "run_id": run_id,
        "status": status,
        "goal": goal,
        "spec_markdown": "# SPEC\n\nbody",
        "questions": [] if questions is None else questions,
        "assumptions": [],
    }


def test_spec_context_matches_run():
    """FINAL spec.run_id == state.run_id and spec.goal == state.goal pass."""
    spec = _final_spec(run_id=RUN_ID, goal="Refactor X")
    schemas.check_spec_context(spec, run_id=RUN_ID, goal="Refactor X")


def test_spec_context_rejects_run_id_mismatch():
    """The engine injects run_id from state; a FINAL spec whose run_id differs
    from state.run_id is rejected (it could only happen via a bug or tampering)."""
    spec = _final_spec(run_id="20260730120000-zzz999")
    with pytest.raises(schemas.SchemaError):
        schemas.check_spec_context(spec, run_id=RUN_ID, goal="Refactor X")


def test_spec_context_rejects_goal_mismatch():
    spec = _final_spec(goal="DIFFERENT goal")
    with pytest.raises(schemas.SchemaError):
        schemas.check_spec_context(spec, run_id=RUN_ID, goal="Refactor X")


def test_spec_context_ready_implies_no_questions():
    """READY_FOR_SPEC_APPROVAL => questions must be empty (clean handoff)."""
    spec = _final_spec(status="READY_FOR_SPEC_APPROVAL",
                       questions=["an open question"])
    # first the structural schema would reject this (READY+questions), but the
    # context check also enforces it independently
    with pytest.raises(schemas.SchemaError):
        schemas.check_spec_context(spec, run_id=RUN_ID, goal="Refactor X")


def test_spec_context_needs_user_input_requires_questions():
    """NEEDS_USER_INPUT <=> questions non-empty."""
    spec = _final_spec(status="NEEDS_USER_INPUT", questions=[])
    with pytest.raises(schemas.SchemaError):
        schemas.check_spec_context(spec, run_id=RUN_ID, goal="Refactor X")
    # and the valid direction: NEEDS_USER_INPUT with questions passes
    ok = _final_spec(status="NEEDS_USER_INPUT", questions=["q1"])
    schemas.check_spec_context(ok, run_id=RUN_ID, goal="Refactor X")


# ---------------------------------------------------------------------------
# consistency #1 / parallel P0-4: RAW plan rejects authoritative fields
# ---------------------------------------------------------------------------


def test_raw_plan_rejects_authoritative_fields():
    """consistency #1: the RAW plan-proposal schema REJECTS any envelope
    containing base_commit / run_id / hashes — the model cannot emit them, so
    there is no 'compare Codex's base_commit' step. The engine injects them."""
    def _ready(plan):
        # R2-1: strict-mode envelope — all top-level keys always present.
        return {"status": "READY_FOR_EXECUTION", "plan": plan,
                "questions": [], "block_reason": ""}

    base_task = {
        "id": "T001",
        "title": "t",
        "dependencies": [],
        "declared_files": ["src/a.cpp"],
        "verification_commands": [
            {"kind": "executable", "program": "pytest",
             "args": [], "cwd": ".", "timeout_sec": 60},
        ],
        "acceptance_criteria": ["ok"],
        "planned_changes": {
            "may_add_dependencies": False,
            "may_run_migrations": False,
            "may_delete_files": [],
        },
    }

    # base valid RAW plan
    valid = _ready({"plan_markdown": "# PLAN", "tasks": [dict(base_task)]})
    schemas.validate_raw_plan(valid)  # baseline passes

    # adding run_id at the wrapper level is rejected
    bad = _ready({"plan_markdown": "# PLAN", "tasks": [dict(base_task)]})
    bad["run_id"] = RUN_ID
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)

    # adding base_commit at the plan level is rejected
    bad = _ready({"plan_markdown": "# PLAN", "base_commit": BASE_COMMIT,
                  "tasks": [dict(base_task)]})
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)

    # adding base_commit at the TASK level is rejected (engine injects it)
    bad_task = dict(base_task)
    bad_task["base_commit"] = BASE_COMMIT
    bad = _ready({"plan_markdown": "# PLAN", "tasks": [bad_task]})
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)

    # adding a hash anywhere in the task is rejected
    bad_task = dict(base_task)
    bad_task["plan_sha256"] = HASH64
    bad = _ready({"plan_markdown": "# PLAN", "tasks": [bad_task]})
    with pytest.raises(schemas.SchemaError):
        schemas.validate_raw_plan(bad)


# ---------------------------------------------------------------------------
# FINAL plan envelope: state base_commit / plan_version / single T001 task
# ---------------------------------------------------------------------------


def _final_plan(*, run_id=RUN_ID, base_commit=BASE_COMMIT, plan_version=1,
                task_id="T001", dependencies=None, declared_files=("src/a.cpp",),
                verification_commands=None):
    return {
        "run_id": run_id,
        "status": "READY_FOR_EXECUTION",
        "plan_markdown": "# PLAN\nbody",
        "plan_version": plan_version,
        "base_commit": base_commit,
        "tasks": [
            {
                "id": task_id,
                "title": "t",
                "dependencies": [] if dependencies is None else dependencies,
                "declared_files": list(declared_files),
                "verification_commands": verification_commands or [
                    {"kind": "executable", "program": "pytest",
                     "args": [], "cwd": ".", "timeout_sec": 60},
                ],
                "acceptance_criteria": ["ok"],
                "planned_changes": {
                    "may_add_dependencies": False,
                    "may_run_migrations": False,
                    "may_delete_files": [],
                },
            }
        ],
    }


def test_final_plan_uses_state_base_commit():
    """consistency #1: the FINAL plan-envelope (engine-assembled) must have
    base_commit == state.base_commit (injected), plan_version == 1, and the
    single task must be T001 with empty dependencies."""
    plan = _final_plan(base_commit=BASE_COMMIT, plan_version=1)
    schemas.check_plan_context(
        plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
        program_allowlist=["pytest"], interpreter_allowlist=[],
    )


def test_final_plan_rejects_base_commit_mismatch():
    plan = _final_plan(base_commit="deadbeef000")
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


def test_final_plan_rejects_run_id_mismatch():
    plan = _final_plan(run_id="20260730120000-zzz999")
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


def test_final_plan_rejects_noninitial_plan_version():
    """v1 starts at plan_version == 1 (replans are v1.2). A first plan with a
    different version is a context error."""
    plan = _final_plan(plan_version=2)
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


def test_final_plan_requires_single_task_t001():
    """v1 is a single-task run; the task id must be T001."""
    plan = _final_plan(task_id="T002")
    # T002 is structurally a valid task_id, but contextually v1 only has T001
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


def test_final_plan_requires_empty_dependencies():
    """The single task has no dependencies (nothing precedes it in v1)."""
    plan = _final_plan(dependencies=["T000"])
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


# ---------------------------------------------------------------------------
# declared_files + verification cwd: relative, no traversal
# ---------------------------------------------------------------------------


def test_declared_files_relative_no_traversal():
    """All declared_files are relative and none contain '..' (no worktree escape
    via a crafted declared path)."""
    plan = _final_plan(declared_files=("src/a.cpp", "tests/a_test.cpp"))
    schemas.check_plan_context(
        plan, run_id=RUN_ID, base_commit=BASE_COMMIT,
        program_allowlist=["pytest"], interpreter_allowlist=[],
    )

    # an absolute path is rejected
    bad = _final_plan(declared_files=("C:/secret/x.cpp",))
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )
    # a '..' traversal is rejected
    bad = _final_plan(declared_files=("../escape.cpp",))
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


def test_verification_cwd_relative():
    """verification.cwd must be relative (resolved under the worktree at runtime;
    an absolute cwd would escape the run worktree root)."""
    bad = _final_plan(verification_commands=[
        {"kind": "executable", "program": "pytest",
         "args": [], "cwd": "C:/elsewhere", "timeout_sec": 60},
    ])
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )
    # relative cwd is fine
    ok = _final_plan(verification_commands=[
        {"kind": "executable", "program": "pytest",
         "args": [], "cwd": ".", "timeout_sec": 60},
    ])
    schemas.check_plan_context(
        ok, run_id=RUN_ID, base_commit=BASE_COMMIT,
        program_allowlist=["pytest"], interpreter_allowlist=[],
    )


def test_verification_cwd_rejects_dotdot():
    """A cwd with '..' would escape the worktree."""
    bad = _final_plan(verification_commands=[
        {"kind": "executable", "program": "pytest",
         "args": [], "cwd": "../sibling", "timeout_sec": 60},
    ])
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


def test_path_safety_is_host_platform_independent():
    """The relative/absolute check must reject a path if EITHER a POSIX or a
    Windows flavor treats it as absolute/anchored — regardless of the host OS.
    This pins the portability fix (PurePath is platform-flavored)."""
    # a Windows-style absolute must be rejected even when the host is POSIX,
    # and a POSIX absolute must be rejected on a Windows host.
    for bad_cwd in ("C:/elsewhere", "C:\\elsewhere", "/abs/path"):
        bad = _final_plan(verification_commands=[
            {"kind": "executable", "program": "pytest",
             "args": [], "cwd": bad_cwd, "timeout_sec": 60},
        ])
        with pytest.raises(schemas.SchemaError):
            schemas.check_plan_context(
                bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
                program_allowlist=["pytest"], interpreter_allowlist=[],
            )
    # and a backslash '..' traversal is caught too (Windows separator form)
    bad = _final_plan(verification_commands=[
        {"kind": "executable", "program": "pytest",
         "args": [], "cwd": "sub\\..\\escape", "timeout_sec": 60},
    ])
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )


# ---------------------------------------------------------------------------
# program / interpreter allowlist (P1-2 structured config, not AGENTS.md text)
# ---------------------------------------------------------------------------


def test_verification_program_in_allowlist():
    """The executable `program` must be in the structured program_allowlist
    (P1-2 moved the allowlist from Markdown to config; AGENTS.md is context-only).
    """
    # program NOT in allowlist => rejected
    bad = _final_plan(verification_commands=[
        {"kind": "executable", "program": "rm",
         "args": ["-rf", "/"], "cwd": ".", "timeout_sec": 60},
    ])
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )
    # program IN allowlist => accepted
    ok = _final_plan(verification_commands=[
        {"kind": "executable", "program": "cmake",
         "args": ["--build"], "cwd": ".", "timeout_sec": 60},
    ])
    schemas.check_plan_context(
        ok, run_id=RUN_ID, base_commit=BASE_COMMIT,
        program_allowlist=["pytest", "cmake"], interpreter_allowlist=[],
    )


def test_verification_script_interpreter_in_allowlist():
    """For script kinds (cmd_script/bash_script), the `program` is the script
    FILE but it runs under an INTERPRETER. The interpreter must be in
    interpreter_allowlist (consistency with P1-3). We model this by requiring the
    script kind's program to be a recognized script that an allowed interpreter
    can run; here we assert the kind maps to an interpreter and that interpreter
    is allowlisted."""
    # bash_script with bash not allowlisted => rejected
    bad = _final_plan(verification_commands=[
        {"kind": "bash_script", "program": "run-tests.sh",
         "args": [], "cwd": ".", "timeout_sec": 60},
    ])
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=[], interpreter_allowlist=["cmd.exe"],
        )
    # bash_script with bash allowlisted => accepted
    ok = _final_plan(verification_commands=[
        {"kind": "bash_script", "program": "run-tests.sh",
         "args": [], "cwd": ".", "timeout_sec": 60},
    ])
    schemas.check_plan_context(
        ok, run_id=RUN_ID, base_commit=BASE_COMMIT,
        program_allowlist=[], interpreter_allowlist=["bash", "sh"],
    )
    # cmd_script with cmd allowlisted => accepted
    ok2 = _final_plan(verification_commands=[
        {"kind": "cmd_script", "program": "build.bat",
         "args": [], "cwd": ".", "timeout_sec": 60},
    ])
    schemas.check_plan_context(
        ok2, run_id=RUN_ID, base_commit=BASE_COMMIT,
        program_allowlist=[], interpreter_allowlist=["cmd.exe", "powershell"],
    )


# ---------------------------------------------------------------------------
# cross-check: a structurally-invalid envelope is rejected before context checks
# ---------------------------------------------------------------------------


def test_context_checks_run_on_structurally_valid_envelopes():
    """The context checkers call the structural validator first; a structurally
    invalid envelope (e.g. missing a required FINAL field) is rejected by the
    structural schema, not silently passed to the context layer."""
    bad = _final_plan()
    del bad["base_commit"]  # structurally invalid FINAL plan
    with pytest.raises(schemas.SchemaError):
        schemas.check_plan_context(
            bad, run_id=RUN_ID, base_commit=BASE_COMMIT,
            program_allowlist=["pytest"], interpreter_allowlist=[],
        )
