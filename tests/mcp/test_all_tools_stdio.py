"""Task 6.7 — wire Engine into the 9 FastMCP tools; full stdio happy path.

Real subprocess MCP client (official SDK), fake Codex/Grok adapters injected via
``AGENTFLOW_RUN_CONTEXT.adapters_module``. The test itself performs the real
git commit between start_task and verify_task (the executor's work), then drives
verify → review → complete to READY_TO_MERGE.
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
FAKE_ADAPTERS_MODULE = "tests.mcp._fake_adapters_for_stdio"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def _plugin_root(tmp_path: Path) -> Path:
    pr = tmp_path / "plugin"
    pr.mkdir()
    template = REPO_ROOT / "AGENTS.md.template"
    (pr / "AGENTS.md.template").write_bytes(template.read_bytes())
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
            "change_policy": {
                "on_unauthorized_delete": "NEEDS_HUMAN",
                "on_unauthorized_dependency": "NEEDS_HUMAN",
                "on_unauthorized_migration": "NEEDS_HUMAN",
                "on_undeclared_file": "NEEDS_HUMAN",
                "irreversible_migration": "always_need_human",
            },
            "planner": {"timeout_sec": 30},
            "reviewer": {"timeout_sec": 30},
        },
        "effective_config": {
            "effective_approval_mode": "zcode_tool_confirmation",
            "effective_reasoning_effort": "high",
        },
        "adapters_module": FAKE_ADAPTERS_MODULE,
    })


def _server_env(run_context_json: str) -> dict:
    env = dict(os.environ)
    # src + repo root on PYTHONPATH so zcode_agentflow AND tests.mcp import
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
    # FastMCP may wrap the dict as JSON text or as structured content
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # some FastMCP versions put the dict under structuredContent
        sc = getattr(res, "structuredContent", None) or getattr(res, "structured_content", None)
        if isinstance(sc, dict):
            return sc
        raise


def _revision_of(control_root) -> int:
    return store.State.load(control_root).revision


# ---------- full happy path -------------------------------------------------


@pytest.mark.anyio
async def test_stdio_full_happy_path_reaches_ready_to_merge(tmp_path):
    repo = _make_repo(tmp_path)
    plugin = _plugin_root(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    ctx_json = _run_context_json(plugin_root=plugin, user_data_root=user_data)

    async with sdk_session(ctx_json) as session:
        # 1. init
        init = _payload(await session.call_tool(
            "workflow_init",
            {"target_repo": str(repo), "goal": "stdio happy path",
             "run_id": "20260730120000-abcdf1"},
        ))
        assert init["run_id"] == "20260730120000-abcdf1"
        assert init["base_commit"]
        control = Path(init["control_root"])
        worktree = Path(init["worktree_root"])
        assert gitops.is_git_repo(worktree)

        # 2. draft (no questions => READY_FOR_SPEC_APPROVAL)
        draft = _payload(await session.call_tool(
            "planner_draft_spec",
            {"goal": "stdio happy path"},
        ))
        assert draft["status"] == "READY_FOR_SPEC_APPROVAL"
        st = store.State.load(control)
        assert st.state == "READY_FOR_SPEC_APPROVAL"
        assert st.spec_sha256

        # 3. approve
        approve = _payload(await session.call_tool(
            "workflow_approve_spec",
            {"expected_revision": st.revision},
        ))
        assert approve["state"] == "PLANNING"
        st = store.State.load(control)

        # 4. create_plan
        plan = _payload(await session.call_tool(
            "planner_create_plan",
            {"approved_spec_sha256": st.spec_sha256,
             "expected_revision": st.revision},
        ))
        assert plan["status"] == "READY_FOR_EXECUTION"
        st = store.State.load(control)
        assert st.current_task == "T001"

        # 5. start_task
        start = _payload(await session.call_tool(
            "workflow_start_task",
            {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert start["task_start_commit"]
        st = store.State.load(control)
        assert st.state == "EXECUTING_TASK"

        # --- the "executor" does real work outside AgentFlow ---
        env = {"PATH": os.environ.get("PATH", "")}
        (worktree / "src").mkdir(exist_ok=True)
        (worktree / "src" / "x.py").write_text("print('x')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "task: add src/x.py"],
            cwd=worktree, check=True, env=env,
        )
        assert gitops.is_clean(worktree)

        # 6. verify
        st = store.State.load(control)
        verify = _payload(await session.call_tool(
            "workflow_verify_task",
            {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert verify["status"] == "READY_FOR_REVIEW"
        st = store.State.load(control)
        assert st.state == "VERIFYING_TASK"
        assert st.verified_head_commit == gitops.head(worktree)

        # 7. review
        review = _payload(await session.call_tool(
            "reviewer_review_task",
            {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert review["decision"] == "PASS"
        st = store.State.load(control)
        assert st.state == "REVIEWING_TASK"
        assert st.last_reviewed_commit == gitops.head(worktree)

        # 8. complete
        complete = _payload(await session.call_tool(
            "workflow_complete_task",
            {"task_id": "T001", "expected_revision": st.revision},
        ))
        assert complete["state"] == "READY_TO_MERGE"
        st = store.State.load(control)
        assert st.state == "READY_TO_MERGE"

        # 9. status still resolves the terminal run
        status = _payload(await session.call_tool("workflow_status", {}))
        assert status["active_run"] is not None
        assert status["active_run"]["state"] == "READY_TO_MERGE"
        assert status["active_run"]["run_id"] == "20260730120000-abcdf1"


# ---------- error path: illegal state does not crash the server --------------


@pytest.mark.anyio
async def test_stdio_error_returns_mcp_error_not_crash(tmp_path):
    """An illegal-state call surfaces as an MCP error result; the server stays
    alive for a subsequent legal call (workflow_status)."""
    repo = _make_repo(tmp_path)
    plugin = _plugin_root(tmp_path)
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    ctx_json = _run_context_json(plugin_root=plugin, user_data_root=user_data)

    async with sdk_session(ctx_json) as session:
        # no active run yet — start_task must fail (not crash)
        res = await session.call_tool(
            "workflow_start_task",
            {"task_id": "T001", "expected_revision": 1},
        )
        assert res.isError

        # server still alive: workflow_status works
        status_res = await session.call_tool("workflow_status", {})
        assert not status_res.isError
        status = _payload(status_res)
        assert "active_run" in status

        # a legal init still works after the error
        init = _payload(await session.call_tool(
            "workflow_init",
            {"target_repo": str(repo), "goal": "recover after error",
             "run_id": "20260730120000-err0a1"},
        ))
        assert init["run_id"] == "20260730120000-err0a1"

        # illegal transition from INITIALIZING_WORKTREE: complete_task rejected
        st = store.State.load(init["control_root"])
        bad = await session.call_tool(
            "workflow_complete_task",
            {"task_id": "T001", "expected_revision": st.revision},
        )
        assert bad.isError

        # still alive
        alive = await session.call_tool("workflow_status", {})
        assert not alive.isError
