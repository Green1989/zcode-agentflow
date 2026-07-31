#!/usr/bin/env python3
"""Task 8.3 — Layer 3 local smoke harness (real Codex / Grok CLIs).

A STANDALONE script (NOT a pytest test) that drives a full v1 run against a
tiny sample repo using the REAL ``codex exec`` (Planner) and ``grok -p``
(Reviewer) adapters. It is OFF BY DEFAULT and never collected by the default
``pytest`` suite: it runs only when invoked manually with the ``AGENTFLOW_SMOKE=1``
env var set, and only after a capability gate confirms the real CLIs are
available per Phase 0 (``docs/capabilities.json``).

Layer 3 is the only layer that exercises the real model CLIs end to end. It
proves the Phase-0 contracts (codex stdin/--output-schema, grok --cwd absolute
path / proxy injection / structuredOutput) actually work against a real AgentFlow
run — something fakes can never prove.

Usage (manual, from the repo root):

    AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py

Exit code 0 == the run reached READY_TO_MERGE; non-zero == it stopped at a safe
state (NEEDS_HUMAN / a real CLI error / a missing capability). Failures are
reported honestly — the harness never swaps real CLIs for fakes to "pass".

Requires: codex logged in (``~/.codex``), grok logged in, and the local proxy
(Clash Verge ``127.0.0.1:7897``) reachable for grok (G7/G8). The two human gates
are observed: SPEC approval is recorded (configured-confirmation) and the run
stops at READY_TO_MERGE (no merge/push).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make src importable when run directly (the package is installed in
# production, but this harness also runs from a source checkout).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from zcode_agentflow import (  # noqa: E402  (sys.path set above on purpose)
    active_run,
    artifacts,
    engine,
    gitops,
    ids,
    store,
)
from zcode_agentflow.adapters.codex import CodexAdapter  # noqa: E402
from zcode_agentflow.adapters.grok import GrokAdapter  # noqa: E402

# The local proxy grok MUST reach to get off the machine (G7). Override via env.
DEFAULT_PROXY = "http://127.0.0.1:7897"
RUN_ID = "20260731120000-5e0ea1"  # 14 digits + 6 hex (ids.validate_run_id)


def _log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


# ---------------------------------------------------------------------------
# gate: AGENTFLOW_SMOKE=1 + capability check
# ---------------------------------------------------------------------------


def _check_gate() -> dict:
    """Confirm the harness may run and the real CLIs exist.

    Returns the loaded capabilities.json. Exits with a clear message (not an
    exception) when the gate is not satisfied, so a manual operator knows what
    to enable instead of debugging a traceback."""
    if os.environ.get("AGENTFLOW_SMOKE") != "1":
        _log("AGENTFLOW_SMOKE is not '1' — this harness is OFF BY DEFAULT.")
        _log("Re-run with:  AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py")
        sys.exit(2)

    caps_path = REPO_ROOT / "docs" / "capabilities.json"
    caps = json.loads(caps_path.read_text(encoding="utf-8"))

    for cli in ("codex", "grok"):
        if shutil.which(cli) is None:
            _log(f"CAPABILITY GATE FAILED: '{cli}' is not on PATH.")
            _log("Phase 0 marked planner_ok/reviewer_ok as required; this harness "
                 "needs the real CLIs installed and logged in.")
            sys.exit(3)
    _log("capability gate passed: codex + grok on PATH (Phase 0 required gates).")
    return caps


# ---------------------------------------------------------------------------
# tiny sample repo (lowest cost, result-verifiable)
# ---------------------------------------------------------------------------


def _make_sample_repo(parent: Path) -> Path:
    """A tiny real git repo with one file the executor will edit."""
    repo = parent / "sample_repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "smoke@example.com"],
                   cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "Smoke"],
                   cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin_root(parent: Path) -> Path:
    pr = parent / "plugin"
    pr.mkdir()
    (pr / "AGENTS.md.template").write_bytes(
        (REPO_ROOT / "AGENTS.md.template").read_bytes()
    )
    return pr


# ---------------------------------------------------------------------------
# run the full workflow with REAL adapters
# ---------------------------------------------------------------------------


def _build_engine(control: Path, worktree: Path, repo: Path, plugin: Path,
                  user_data: Path) -> "engine.Engine":
    """Build an Engine wired to the REAL codex/grok adapters.

    The reviewer config carries the proxy (G7: grok has no auto proxy injection).
    Verification allowlists the running interpreter so a trivial verify passes.
    """
    proxy = os.environ.get("AGENTFLOW_PROXY", DEFAULT_PROXY)
    cfg = {
        "verification": {
            "program_allowlist": [sys.executable, "python"],
            "interpreter_allowlist": ["cmd.exe", "bash"],
        },
        "planner": {"timeout_sec": 600},
        "reviewer": {
            "timeout_sec": 300,
            # grok MUST receive these explicitly (G7/G8) or it hangs.
            "proxy": {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy},
        },
    }
    return engine.Engine(
        store.Store(control),
        roots={
            "plugin_root": str(plugin),
            "target_repo_root": str(repo),
            "run_worktree_root": str(worktree),
            "control_root": str(control),
        },
        config=cfg,
        effective_config={
            # configured-confirmation: treat the MCP tool confirmation as the
            # strong method (the operator is driving via the script here; in
            # production this is the real ZCode tool-confirmation gate).
            "effective_approval_mode": "zcode_tool_confirmation",
            "confirmation_configured": True,
        },
        user_data_root=user_data,
        adapters={"codex": CodexAdapter(command="codex"),
                  "grok": GrokAdapter(command="grok")},
    )


def run_smoke() -> dict:
    caps = _check_gate()
    ids.validate_run_id(RUN_ID)  # defensive: fail loud on a bad id, not mid-run

    with tempfile.TemporaryDirectory(prefix="agentflow_smoke_") as td:
        parent = Path(td)
        repo = _make_sample_repo(parent)
        plugin = _plugin_root(parent)
        user_data = parent / "user_data"
        user_data.mkdir()
        control = user_data / "runs" / RUN_ID / "control"
        worktree = user_data / "runs" / RUN_ID / "worktree"
        control.mkdir(parents=True, exist_ok=True)
        worktree.parent.mkdir(parents=True, exist_ok=True)

        goal = ("Edit a.txt to append a single documented line, then verify "
                "with a trivial python one-liner. Single task, single file.")

        # 1. init
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        init = eng.workflow_init(target_repo=str(repo), goal=goal, run_id=RUN_ID)
        _log(f"init ok: run_id={init['run_id']} base={init['base_commit'][:8]}")

        # 2. draft (real Codex) — HUMAN GATE 1 follows
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        draft = eng.planner_draft_spec(goal=goal, expected_revision=st.revision)
        _log(f"draft ok: status={draft['status']} (real codex)")
        if draft["status"] == "NEEDS_USER_INPUT":
            _log("Codex asked questions; re-run with answers — stopping safely.")
            return {"outcome": "NEEDS_USER_INPUT", "questions": draft["questions"]}

        # 3. approve (HUMAN GATE 1: recorded with configured confirmation)
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        approve = eng.workflow_approve_spec(expected_revision=st.revision)
        _log(f"approve ok (GATE 1): method={approve['approval_method']}")

        # 4. create_plan (real Codex, single task)
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        plan = eng.planner_create_plan(
            approved_spec_sha256=st.spec_sha256, expected_revision=st.revision,
        )
        if plan["status"] != "READY_FOR_EXECUTION":
            _log(f"plan NOT ready: {plan['status']} — stopping safely (NEEDS_HUMAN).")
            return {"outcome": "NEEDS_HUMAN", "reason": "plan " + plan["status"]}
        _log(f"plan ok: task={plan['current_task']} (real codex, single task)")

        # 5. start_task
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        start = eng.workflow_start_task(task_id="T001", expected_revision=st.revision)
        task_start = start["task_start_commit"]
        _log(f"start ok: task_start={task_start[:8]}")

        # --- the executor's REAL work: edit a.txt, git add, git commit ---
        # (the operator is the "executor" here; AgentFlow never writes code)
        env = {"PATH": os.environ.get("PATH", "")}
        a_txt = worktree / "a.txt"
        a_txt.write_text(a_txt.read_text(encoding="utf-8") + "smoke edit\n",
                         encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "task: append smoke edit line"],
                       cwd=worktree, check=True, env=env)
        new_head = gitops.head(worktree)
        _log(f"executor committed: {new_head[:8]} (real git)")

        # 6. verify (runs the declared verification command)
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        verify = eng.workflow_verify_task(task_id="T001", expected_revision=st.revision)
        if verify["status"] != "READY_FOR_REVIEW":
            _log(f"verify FAILED: {verify['status']} — NEEDS_HUMAN.")
            return {"outcome": "NEEDS_HUMAN", "reason": "verify " + verify["status"]}
        _log(f"verify ok: status={verify['status']}")

        # 7. review (real Grok, read-only bundle) — must observe a PASS for v1
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        review = eng.reviewer_review_task(task_id="T001", expected_revision=st.revision)
        if review["decision"] != "PASS":
            _log(f"review NOT PASS: {review['decision']} — NEEDS_HUMAN (real grok).")
            return {"outcome": "NEEDS_HUMAN", "reason": "review " + review["decision"]}
        _log(f"review ok: decision=PASS (real grok)")

        # 8. complete — HUMAN GATE 2 implied; reaches READY_TO_MERGE, no merge/push
        eng = _build_engine(control, worktree, repo, plugin, user_data)
        st = store.State.load(control)
        complete = eng.workflow_complete_task(task_id="T001", expected_revision=st.revision)
        _log(f"complete ok: state={complete['state']} (GATE 2, NO merge/push)")

        # final handoff snapshot (side-effect-free read of state + artifacts)
        st = store.State.load(control)
        resolver = artifacts.ArtifactResolver(control, st)
        ptr = active_run.read_pointer(user_data)
        handoff = {
            "outcome": "READY_TO_MERGE",
            "run_id": st.run_id,
            "branch": "agentflow/" + RUN_ID,
            "base_commit": st.base_commit,
            "head_commit": new_head,
            "changed_files": json.loads(resolver.read("execution-result.json"))["changed_files"],
            "spec_sha256": st.spec_sha256,
            "plan_sha256": st.plan_sha256,
            "review_decision": json.loads(resolver.read("review-result.json"))["decision"],
            "terminal_pointer": ptr.get("terminal") if ptr else None,
            "capabilities_versions": {
                "codex_cli": caps.get("codex", {}).get("cli_version"),
                "grok_cli": caps.get("grok", {}).get("cli_version"),
            },
            "pushed": False, "merged": False, "pr_created": False,
        }
        _log("READY_TO_MERGE handoff (NO push/merge/PR performed):")
        print(json.dumps({k: handoff[k] for k in
                          ("outcome", "run_id", "branch", "base_commit",
                           "head_commit", "changed_files", "review_decision",
                           "pushed", "merged", "pr_created")},
                         indent=2))
        return handoff


if __name__ == "__main__":
    try:
        result = run_smoke()
    except Exception as exc:  # surface a real CLI failure honestly
        _log(f"SMOKE FAILED (real CLI / adapter error): {type(exc).__name__}: {exc}")
        sys.exit(1)
    if result.get("outcome") != "READY_TO_MERGE":
        _log(f"SMOKE stopped safely at {result.get('outcome')} (not a pass).")
        sys.exit(4)
    _log("SMOKE PASS: reached READY_TO_MERGE with real Codex/Grok.")
    sys.exit(0)
