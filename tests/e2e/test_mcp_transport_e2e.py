"""Task 8.2 — Layer 2 MCP transport E2E over real stdio (Prompt §21).

Layer 2 proves the SAME happy path + rejection path as Layer 1, but driven
through the actual MCP protocol (a real server subprocess + the official SDK
client), never calling the Engine directly. Fake adapters are injected via the
test factory hook (``AGENTFLOW_RUN_CONTEXT.adapters_module``); Layer 3 swaps
them for real Codex/Grok CLIs.

The distinctive Layer-2 assertion is **process-restart recovery**: the run is
driven part-way in server process A, A is torn down, a fresh server process B
is spawned against the same user_data_root, and the active-run pointer + Store
recovery reconnect B to the SAME run. B then drives the run to READY_TO_MERGE,
and a final ``workflow_status`` (from yet another fresh process) observes the
terminal READY_TO_MERGE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from zcode_agentflow import gitops, store

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_MODULE = "zcode_agentflow.server"
# A dedicated fake-adapters module under tests/e2e so the server subprocess can
# import it (it must be importable from the spawned process via PYTHONPATH).
FAKE_ADAPTERS_MODULE = "tests.e2e._fake_adapters"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ============================================================================
# temp fixture helpers (real repo, plugin root, run context)
# ============================================================================


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin_root(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir(exist_ok=True)
    (pr / "AGENTS.md.template").write_bytes(
        (REPO_ROOT / "AGENTS.md.template").read_bytes()
    )
    return pr


def _run_context_json(*, plugin_root, user_data_root) -> str:
    return json.dumps({
        "plugin_root": str(plugin_root),
        "user_data_root": str(user_data_root),
        "config": {
            "verification": {
                "program_allowlist": [sys.executable, "python"],
                "interpreter_allowlist": ["cmd.exe", "bash"],
            },
        },
        "effective_config": {
            "effective_approval_mode": "zcode_tool_confirmation",
            "confirmation_configured": True,
        },
        "adapters_module": FAKE_ADAPTERS_MODULE,
    })


def _server_env(run_context_json: str) -> dict:
    env = dict(os.environ)
    # src + repo root on PYTHONPATH so zcode_agentflow AND tests.e2e import
    env["PYTHONPATH"] = (
        str(REPO_ROOT / "src") + os.pathsep
        + str(REPO_ROOT) + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    env["AGENTFLOW_TEST_MODE"] = "1"
    env["AGENTFLOW_RUN_CONTEXT"] = run_context_json
    return env


def _server_params(run_context_json: str):
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-u", "-m", SERVER_MODULE],
        env=_server_env(run_context_json),
    )


@asynccontextmanager
async def sdk_session(run_context_json: str):
    """Spawn a REAL server subprocess and wrap an SDK ClientSession over stdio."""
    from mcp.client.stdio import stdio_client
    from mcp.client.session import ClientSession

    async with stdio_client(_server_params(run_context_json)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _payload(res) -> dict:
    """Parse a tools/call result into a dict. Raises on MCP error."""
    if res.isError:
        text = "".join(getattr(c, "text", "") or "" for c in (res.content or []))
        raise AssertionError(f"MCP tool error: {text}")
    text = "".join(getattr(c, "text", "") or "" for c in (res.content or []))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        sc = (getattr(res, "structuredContent", None)
              or getattr(res, "structured_content", None))
        if isinstance(sc, dict):
            return sc
        raise


def _commit_in_worktree(worktree, content, msg="task change"):
    """The executor's real work: edit + git add + git commit (NOT via MCP)."""
    env = {"PATH": os.environ.get("PATH", "")}
    p = Path(worktree) / "a.txt"
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=worktree, check=True, env=env)
    return gitops.head(worktree)


# ============================================================================
# 1. Happy path over stdio — process A starts the run, B restarts + completes,
#    then a fresh process observes READY_TO_MERGE via workflow_status.
# ============================================================================


@pytest.mark.anyio
async def test_stdio_run_reaches_ready_to_merge(tmp_path):
    repo = _make_repo(tmp_path)
    plugin = _plugin_root(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    ctx_json = _run_context_json(plugin_root=plugin, user_data_root=user_data)
    run_id = "20260730120000-abc2aa"

    # ---- server process A: init -> verify (then crash/restart) -------------
    async with sdk_session(ctx_json) as session:
        init = _payload(await session.call_tool(
            "workflow_init",
            {"target_repo": str(repo), "goal": "layer2 e2e", "run_id": run_id},
        ))
        assert init["run_id"] == run_id
        control = Path(init["control_root"])
        worktree = Path(init["worktree_root"])

        st = store.State.load(control)
        draft = _payload(await session.call_tool(
            "planner_draft_spec", {"goal": "layer2 e2e", "expected_revision": st.revision},
        ))
        assert draft["status"] == "READY_FOR_SPEC_APPROVAL"

        st = store.State.load(control)
        approve = _payload(await session.call_tool(
            "workflow_approve_spec", {"expected_revision": st.revision},
        ))
        assert approve["state"] == "PLANNING"

        st = store.State.load(control)
        plan = _payload(await session.call_tool(
            "planner_create_plan",
            {"approved_spec_sha256": st.spec_sha256, "expected_revision": st.revision},
        ))
        assert plan["status"] == "READY_FOR_EXECUTION"

        st = store.State.load(control)
        start = _payload(await session.call_tool(
            "workflow_start_task", {"task_id": "T001", "expected_revision": st.revision},
        ))
        task_start = start["task_start_commit"]
        # REAL executor work between start and verify (NOT via MCP)
        new_head = _commit_in_worktree(worktree, "edited by executor\n", msg="task: edit a.txt")
        assert new_head != task_start
        assert gitops.is_clean(worktree)

        st = store.State.load(control)
        verify = _payload(await session.call_tool(
            "workflow_verify_task", {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert verify["status"] == "READY_FOR_REVIEW"
    # ---- process A torn down (simulates a crash / restart) -----------------

    # ---- server process B: fresh subprocess resumes the active run via the
    #      pointer + Store.recover, then review + complete to READY_TO_MERGE.
    async with sdk_session(ctx_json) as session:
        st = store.State.load(control)
        review = _payload(await session.call_tool(
            "reviewer_review_task", {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert review["decision"] == "PASS"

        st = store.State.load(control)
        complete = _payload(await session.call_tool(
            "workflow_complete_task", {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert complete["state"] == "READY_TO_MERGE"

        st = store.State.load(control)
        assert st.state == "READY_TO_MERGE"
        assert st.last_reviewed_commit == new_head

        # workflow_status (same process B) sees the terminal run
        status = _payload(await session.call_tool("workflow_status", {}))
        assert status["active_run"] is not None
        assert status["active_run"]["state"] == "READY_TO_MERGE"
        assert status["active_run"]["run_id"] == run_id

    # ---- a THIRD fresh process observes READY_TO_MERGE via workflow_status
    #      alone (read-only pointer path, no Engine built) — the durable
    #      handoff survives independent of any single server process.
    async with sdk_session(ctx_json) as session:
        status = _payload(await session.call_tool("workflow_status", {}))
        assert status["active_run"] is not None
        assert status["active_run"]["state"] == "READY_TO_MERGE"
        assert status["active_run"]["run_id"] == run_id


# ============================================================================
# 2. Rejection over stdio — an MCP error result is returned (not a crash) and
#    the server stays alive for a subsequent legal call.
# ============================================================================


@pytest.mark.anyio
async def test_stdio_rejection_returns_mcp_error_and_keeps_server_alive(tmp_path):
    repo = _make_repo(tmp_path)
    plugin = _plugin_root(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    ctx_json = _run_context_json(plugin_root=plugin, user_data_root=user_data)

    async with sdk_session(ctx_json) as session:
        # no active run yet — start_task must fail (MCP error, not a crash)
        res = await session.call_tool(
            "workflow_start_task", {"task_id": "T001", "expected_revision": 1},
        )
        assert res.isError

        # server still alive: workflow_status works
        status_res = await session.call_tool("workflow_status", {})
        assert not status_res.isError
        assert "active_run" in _payload(status_res)

        # a legal init still works after the error
        init = _payload(await session.call_tool(
            "workflow_init",
            {"target_repo": str(repo), "goal": "rejection e2e",
             "run_id": "20260730120000-abc2de"},
        ))
        assert init["run_id"] == "20260730120000-abc2de"
        control = Path(init["control_root"])

        # drive to READY_FOR_SPEC_APPROVAL so approve is the legal next step,
        # then exercise a rejection: complete_task from an illegal state.
        st = store.State.load(control)
        _payload(await session.call_tool(
            "planner_draft_spec", {"goal": "rejection e2e", "expected_revision": st.revision},
        ))
        st = store.State.load(control)
        # complete_task is illegal from READY_FOR_SPEC_APPROVAL => MCP error
        bad = await session.call_tool(
            "workflow_complete_task", {"task_id": "T001", "expected_revision": st.revision},
        )
        assert bad.isError

        # server still alive: status still resolves the run, still pre-complete
        alive = await session.call_tool("workflow_status", {})
        assert not alive.isError
        status = _payload(alive)
        assert status["active_run"]["run_id"] == "20260730120000-abc2de"
        assert status["active_run"]["state"] == "READY_FOR_SPEC_APPROVAL"
