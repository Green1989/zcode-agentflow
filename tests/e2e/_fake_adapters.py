"""In-process fake Codex/Grok adapters for the Task 8.2 Layer-2 stdio E2E.

Loaded by the server subprocess via ``AGENTFLOW_RUN_CONTEXT.adapters_module``.
Exposes ``build_adapters() -> {"codex": ..., "grok": ...}`` so the Engine uses
these scripted RAW outputs instead of real CLI subprocesses. The single-task
plan declares ``a.txt`` as the change (Layer 2's executor edits a.txt).
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field

from zcode_agentflow.adapters.base import AdapterResult

__all__ = ["build_adapters", "FakeCodex", "FakeGrok"]


@dataclass
class FakeCodex:
    """Scripted RAW outputs keyed by request kind."""

    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        kind = request.get("kind")
        self.calls.append({"kind": kind, "repo_cwd": str(repo_cwd)})
        if kind == "draft_spec":
            raw = _default_draft()
        elif kind == "create_plan":
            raw = _default_plan()
        else:
            raise RuntimeError(f"unknown codex kind {kind!r}")
        return AdapterResult(
            exit_code=0, stdout="", stderr="", artifacts={},
            parsed=copy.deepcopy(raw),
        )


@dataclass
class FakeGrok:
    calls: list = field(default_factory=list)

    def invoke(self, *, request, repo_cwd, operation_dir, timeout) -> AdapterResult:
        self.calls.append({"bundle_dir": request.get("bundle_dir")})
        return AdapterResult(
            exit_code=0, stdout="", stderr="", artifacts={},
            parsed=copy.deepcopy({
                "decision": "PASS", "spec_compliance": True,
                "plan_compliance": True, "tests_sufficient": True,
                "issues": [], "replan_required": False,
            }),
        )


def _default_draft() -> dict:
    return {
        "goal": "layer2 e2e",
        "spec_markdown": "# Spec\n\nLayer-2: edit a.txt.\n",
        "questions": [],
        "assumptions": [
            {"id": "A1", "assumption": "single file change", "risk": "low",
             "default": "proceed"},
        ],
    }


def _default_plan() -> dict:
    return {
        "status": "READY_FOR_EXECUTION",
        "plan": {
            "plan_markdown": "# Plan\n\nSingle task T001: edit a.txt.\n",
            "tasks": [
                {
                    "id": "T001",
                    "title": "edit a.txt",
                    "dependencies": [],
                    "declared_files": ["a.txt"],
                    "verification_commands": [
                        {
                            "kind": "executable",
                            "program": sys.executable,
                            "args": ["-c", "print('ok')"],
                            "cwd": ".",
                            "timeout_sec": 30,
                        }
                    ],
                    "acceptance_criteria": ["a.txt edited", "verify exits 0"],
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
_CODEX = FakeCodex()
_GROK = FakeGrok()


def build_adapters() -> dict:
    """Entry point loaded by bootstrap.load_run_context()."""
    return {"codex": _CODEX, "grok": _GROK}
