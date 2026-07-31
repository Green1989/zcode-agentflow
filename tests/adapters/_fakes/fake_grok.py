"""Fake ``grok`` executable for GrokAdapter tests (Task 4.3).

Run as a standalone subprocess (NOT imported). It emulates the grok contract
that matters for the adapter:

  1. its ``--cwd`` is an ABSOLUTE Windows path pointing at the Review Bundle
     (Phase 0 G8); it reads the bundle files relative to that cwd;
  2. it asserts ALL required bundle files are present and non-empty;
  3. it writes a valid RAW review-decision to stdout as a JSON envelope whose
     ``structuredOutput`` field is the parsed decision (Phase 0 G8: the
     authoritative parsed result is ``structuredOutput``, NOT the JSON in
     ``text``);
  4. the RAW decision carries NO authoritative fields (run_id/task_id/commits/
     hashes) — those are injected by the engine.

It records observed argv/cwd/bundle-file-presence to a diagnostics file under
the bundle dir so the test can assert the adapter wired everything correctly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# the files a real Review Bundle must contain (Phase 0 grok.bundle_layout)
REQUIRED_FILES = [
    "SPEC.md",
    "PLAN.md",
    "task-manifest.json",
    "execution-result.json",
    "diff.patch",
    "review-request.json",
    "prompt.md",
]


def _arg_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _emit_review_decision() -> dict:
    """A valid RAW review-decision (NO authoritative fields)."""
    return {
        "decision": "PASS",
        "spec_compliance": True,
        "plan_compliance": True,
        "tests_sufficient": True,
        "issues": [],
        "replan_required": False,
    }


def main() -> int:
    argv = list(sys.argv)
    cwd = _arg_value(argv, "--cwd")
    if not cwd:
        sys.stderr.write("fake_grok: ASSERT FAILED — missing --cwd\n")
        return 3
    if not os.path.isabs(cwd):
        sys.stderr.write(
            f"fake_grok: ASSERT FAILED — --cwd must be absolute, got {cwd!r}\n"
        )
        return 3

    bundle = Path(cwd)
    diag = {
        "cwd": cwd,
        "cwd_absolute": os.path.isabs(cwd),
        "argv": argv,
        "files_present": {},
        "permission_mode": _arg_value(argv, "--permission-mode"),
        "always_approve": "--always-approve" in argv,
        "proxy_injected": any(
            os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
        ),
    }

    # If asked to simulate a hung grok (timeout test), sleep forever after the
    # initial checks. The adapter's process-tree kill must terminate us.
    if os.environ.get("FAKE_GROK_HANG"):
        import time
        time.sleep(60)
        return 0  # unreachable if the kill works

    # 1. assert all required bundle files present + non-empty
    missing_or_empty = []
    for name in REQUIRED_FILES:
        p = bundle / name
        exists = p.exists()
        size = p.stat().st_size if exists else 0
        diag["files_present"][name] = {"exists": exists, "size": size}
        if not exists or size == 0:
            missing_or_empty.append(name)
    if missing_or_empty:
        sys.stderr.write(
            f"fake_grok: ASSERT FAILED — bundle missing/empty: {missing_or_empty}\n"
        )
        # diagnostics go OUTSIDE the bundle (its parent = operation_dir) so the
        # bundle stays byte-pure for the immutability check.
        Path(bundle.parent / "fake_diag.json").write_text(
            json.dumps(diag), encoding="utf-8"
        )
        return 4

    # 2. verify we can actually READ the SPEC/PLAN (proving grok reads the
    #    bundle files relative to --cwd)
    try:
        spec_text = (bundle / "SPEC.md").read_text(encoding="utf-8")
        plan_text = (bundle / "PLAN.md").read_text(encoding="utf-8")
        diag["spec_first_line"] = spec_text.splitlines()[0] if spec_text else ""
        diag["plan_first_line"] = plan_text.splitlines()[0] if plan_text else ""
    except OSError as exc:
        sys.stderr.write(f"fake_grok: could not read bundle: {exc}\n")
        return 5

    # 3. emit a valid RAW review-decision in the grok JSON envelope. The
    #    authoritative parsed result is `structuredOutput` (Phase 0 G8).
    decision = _emit_review_decision()
    # If asked to simulate a FAIL decision (test for issues), flip it.
    if os.environ.get("FAKE_GROK_DECISION") == "FAIL":
        decision = {
            "decision": "FAIL",
            "spec_compliance": False,
            "plan_compliance": True,
            "tests_sufficient": False,
            "issues": [
                {
                    "id": "I1",
                    "severity": "high",
                    "category": "correctness",
                    "file": "src/example.py",
                    "line": 10,
                    "description": "does not satisfy SPEC R1",
                    "required_action": "fix the off-by-one",
                }
            ],
            "replan_required": False,
        }
    # If asked to emit an INVALID RAW decision (schema-failure test), drop a
    # required field (issues) so the adapter's RAW validation rejects it.
    if os.environ.get("FAKE_GROK_INVALID"):
        decision = {"decision": "PASS", "spec_compliance": True,
                    "plan_compliance": True, "tests_sufficient": True,
                    "replan_required": False}  # missing required: issues

    # cite spec+plan content in the `text` field (proving grok consumed them)
    cited = (
        f"Review based on SPEC ({diag['spec_first_line']}) "
        f"and PLAN ({diag['plan_first_line']})."
    )
    envelope = {
        "text": cited,
        "structuredOutput": decision,
        "stopReason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }

    # 4. write diagnostics OUTSIDE the bundle (its parent = operation_dir) so
    #    the bundle stays byte-pure: the adapter hashes every bundle file
    #    before/after and would flag any modification. We never write into the
    #    bundle itself.
    Path(bundle.parent / "fake_diag.json").write_text(json.dumps(diag), encoding="utf-8")

    sys.stdout.write(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
