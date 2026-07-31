"""Fake ``codex`` executable for CodexAdapter tests (Task 4.2).

Run as a standalone subprocess (NOT imported). It emulates the codex contract
that matters for the adapter:

  1. the prompt arrives via **stdin** (codex reads the prompt from stdin when no
     positional PROMPT arg is given — Phase 0 capability);
  2. its cwd is ``repo_cwd`` (the run worktree, NOT control_root);
  3. it writes its result JSON to the ``-o`` path (the operation staging dir,
     distinct from both cwd and stdin).

It records what it observed (stdin text, ``os.getcwd()``, full argv, the
``-o`` / ``-C`` / ``--output-schema`` / ``-s`` values) to a diagnostics file
next to the ``-o`` output so the test can assert the adapter wired argv/stdin/
cwd/output correctly.

It decides WHICH raw output to emit from the ``--output-schema`` FILENAME:
``spec-draft-output.schema.json`` => a valid RAW spec-draft-output;
``plan-proposal.schema.json`` => a valid RAW plan-proposal. This also exercises
that the adapter passes the correct schema file path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _arg_value(argv: list[str], flag: str) -> str | None:
    """Return the token after ``flag`` in argv, or None."""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _emit_spec_draft(goal: str) -> dict:
    """A valid RAW spec-draft-output (NO authoritative fields)."""
    return {
        "goal": goal or "fake goal",
        "spec_markdown": "# Spec\n\nDrafted by fake codex.\n",
        "questions": [],
        "assumptions": [
            {
                "id": "A1",
                "assumption": "the fake repo is sufficient",
                "risk": "low",
                "default": "proceed",
            }
        ],
    }


def _emit_plan_proposal() -> dict:
    """A valid RAW plan-proposal wrapper (single task, NO authoritative fields)."""
    return {
        "status": "READY_FOR_EXECUTION",
        "plan": {
            "plan_markdown": "# Plan\n\nSingle task T001.\n",
            "tasks": [
                {
                    "id": "T001",
                    "title": "implement the change",
                    "dependencies": [],
                    "declared_files": ["src/example.py"],
                    "verification_commands": [
                        {
                            "kind": "executable",
                            "program": "python",
                            "args": ["-m", "pytest"],
                            "cwd": ".",
                            "timeout_sec": 60,
                        }
                    ],
                    "acceptance_criteria": ["tests pass"],
                    "planned_changes": {
                        "may_add_dependencies": False,
                        "may_run_migrations": False,
                        "may_delete_files": [],
                    },
                }
            ],
        },
    }


def main() -> int:
    argv = list(sys.argv)
    o_path = _arg_value(argv, "-o")
    if not o_path:
        sys.stderr.write("fake_codex: missing -o <out_file>\n")
        return 2

    # 1. read stdin (the prompt). codex reads the prompt from stdin when no
    # positional PROMPT arg is given.
    stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not stdin_text.strip():
        sys.stderr.write("fake_codex: ASSERT FAILED — received empty stdin\n")
        return 3

    # If asked to simulate a hung codex (timeout test), sleep forever after
    # reading stdin. The adapter's process-tree kill must terminate us.
    if os.environ.get("FAKE_CODEX_HANG"):
        import time
        time.sleep(60)
        return 0  # unreachable if the kill works

    schema_file = _arg_value(argv, "--output-schema") or ""
    schema_name = Path(schema_file).name if schema_file else ""

    # 2. emit the matching RAW output (chosen by schema filename, so the test
    #    also exercises that the adapter passed the right schema path).
    #    A non-empty FAKE_CODEX_EMIT_INVALID env var forces an INVALID object
    #    (missing required fields) so a test can prove the adapter's validation
    #    path actually rejects bad model output.
    if os.environ.get("FAKE_CODEX_EMIT_INVALID"):
        # deliberately invalid RAW output (missing spec_markdown/questions/assumptions)
        out_obj = {"goal": "incomplete"}
    elif "spec-draft-output" in schema_name:
        out_obj = _emit_spec_draft(stdin_text.splitlines()[0][:80] if stdin_text else "")
    elif "plan-proposal" in schema_name:
        out_obj = _emit_plan_proposal()
    else:
        # unknown schema => emit a spec draft by default (tests pin the schema)
        out_obj = _emit_spec_draft("")

    # 3. write the result JSON to the -o path
    Path(o_path).write_text(json.dumps(out_obj), encoding="utf-8")

    # If asked to simulate a failing codex, exit non-zero AFTER writing (so we
    # can assert the adapter drops parsed on non-zero exit).
    if os.environ.get("FAKE_CODEX_FAIL_EXIT"):
        sys.stderr.write("fake_codex: simulated failure\n")
        return 4

    # 4. record diagnostics next to -o so the test can assert argv/stdin/cwd
    diag = {
        "stdin_len": len(stdin_text),
        "stdin_first_line": stdin_text.splitlines()[0] if stdin_text else "",
        "cwd": os.getcwd(),
        "argv": argv,
        "o_path": o_path,
        "schema_file": schema_file,
        "s_flag": _arg_value(argv, "-s"),
        "C_flag": _arg_value(argv, "-C"),
    }
    diag_path = os.path.join(os.path.dirname(o_path) or ".", "fake_diag.json")
    Path(diag_path).write_text(json.dumps(diag), encoding="utf-8")

    # codex prints a tiny JSONL event line to stdout (--json); harmless.
    sys.stdout.write('{"type":"turn.completed","ok":true}\n')
    return 0


if __name__ == "__main__":
    sys.exit(main())
