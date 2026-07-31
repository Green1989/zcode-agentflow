"""In-process fake Codex/Grok adapters for the Task 6.7 stdio happy-path test.

Loaded by the server subprocess via ``AGENTFLOW_RUN_CONTEXT.adapters_module``.
Exposes ``build_adapters() -> {"codex": ..., "grok": ...}`` so Engine uses these
instead of real CLI subprocesses.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field

from zcode_agentflow.adapters.base import AdapterResult

__all__ = ["build_adapters", "FakeCodex", "FakeGrok"]


@dataclass
class FakeCodex:
    """Scripted RAW outputs keyed by request kind. Each kind pops from its queue."""

    draft_outputs: list = field(default_factory=list)
    plan_outputs: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        kind = request.get("kind")
        self.calls.append({"kind": kind, "repo_cwd": str(repo_cwd)})
        if kind == "draft_spec":
            raw = self.draft_outputs.pop(0) if self.draft_outputs else _default_draft()
        elif kind == "create_plan":
            raw = self.plan_outputs.pop(0) if self.plan_outputs else _default_plan()
        else:
            raise RuntimeError(f"unknown codex kind {kind!r}")
        return AdapterResult(
            exit_code=0, stdout="", stderr="", artifacts={},
            parsed=copy.deepcopy(raw),
        )


@dataclass
class FakeGrok:
    decision: dict = field(default_factory=lambda: {
        "decision": "PASS",
        "spec_compliance": True,
        "plan_compliance": True,
        "tests_sufficient": True,
        "issues": [],
        "replan_required": False,
    })
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append({"bundle_dir": request.get("bundle_dir")})
        return AdapterResult(
            exit_code=0, stdout="", stderr="", artifacts={},
            parsed=copy.deepcopy(self.decision),
        )


def _default_draft() -> dict:
    return {
        "goal": "stdio happy path",
        "spec_markdown": "# Spec\n\nStdio happy-path change: add src/x.py.\n",
        "questions": [],
        "assumptions": [
            {"id": "A1", "assumption": "single file change", "risk": "low",
             "default": "proceed"},
        ],
    }


def _default_plan() -> dict:
    # verification uses the real python interpreter (allowlisted in test config)
    return {
        "status": "READY_FOR_EXECUTION",
        "plan": {
            "plan_markdown": "# Plan\n\nSingle task T001: add src/x.py.\n",
            "tasks": [
                {
                    "id": "T001",
                    "title": "add src/x.py",
                    "dependencies": [],
                    "declared_files": ["src/x.py"],
                    "verification_commands": [
                        {
                            "kind": "executable",
                            "program": sys.executable,
                            "args": ["-c", "print('ok')"],
                            "cwd": ".",
                            "timeout_sec": 30,
                        }
                    ],
                    "acceptance_criteria": ["src/x.py exists", "verify exits 0"],
                    "planned_changes": {
                        "may_add_dependencies": False,
                        "may_run_migrations": False,
                        "may_delete_files": [],
                    },
                }
            ],
        },
    }


# Module-level singletons so a single server process reuses the same fakes
# across successive tool calls (one Engine rebuild per call still sees them).
_CODEX = FakeCodex(draft_outputs=[_default_draft()], plan_outputs=[_default_plan()])
_GROK = FakeGrok()


def build_adapters() -> dict:
    """Entry point loaded by bootstrap.load_run_context()."""
    return {"codex": _CODEX, "grok": _GROK}
