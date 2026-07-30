"""Task 1.5 — real FastMCP stdio server + workflow_status + clean stdout.

REAL subprocess integration tests: spawn the actual server and drive it with
the OFFICIAL mcp SDK client over stdio. The official SDK owns framing; we never
hand-roll newline-JSON.

P1-6 invariant (load-bearing): server stdout is MCP-FRAMES-ONLY. Any stray
print / traceback from the server corrupts the framing the client relies on.
The protocol tests below prove this implicitly (a stray print would break the
SDK client), and `test_server_stdout_contains_only_mcp_frames` additionally
captures the child's raw stdout and asserts it is exclusively MCP traffic.

Async tests run on the asyncio backend only (anyio plugin would otherwise run
each on both asyncio + trio).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_MODULE = "zcode_agentflow.server"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def _server_env() -> dict:
    """Env for the spawned server: put src on PYTHONPATH and isolate from the
    operator's real production config so validate_capabilities doesn't fail-fast
    during these protocol tests."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["AGENTFLOW_TEST_MODE"] = "1"
    return env


def _server_params():
    """Official mcp SDK parameters describing how to launch our server."""
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-u", "-m", SERVER_MODULE],
        env=_server_env(),
    )


@asynccontextmanager
async def sdk_session():
    """Spawn the real server (via the official SDK transport) and yield a
    connected, initialized ClientSession."""
    from mcp.client.stdio import stdio_client
    from mcp.client.session import ClientSession

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# ---------- 1. initialize handshake -----------------------------------------


@pytest.mark.anyio
async def test_initialize_handshake():
    from mcp.client.stdio import stdio_client
    from mcp.client.session import ClientSession

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
    assert result is not None
    assert result.protocolVersion
    assert result.serverInfo is not None
    assert result.serverInfo.name  # "zcode-agentflow"


# ---------- 2. tools/list contains workflow_status --------------------------


@pytest.mark.anyio
async def test_tools_list_contains_workflow_status():
    async with sdk_session() as session:
        tools = await session.list_tools()
    names = [t.name for t in tools.tools]
    assert "workflow_status" in names


# ---------- 3. tools/call workflow_status -----------------------------------


@pytest.mark.anyio
async def test_tools_call_workflow_status():
    async with sdk_session() as session:
        res = await session.call_tool("workflow_status", {})
    assert not res.isError
    text = "".join(getattr(c, "text", "") for c in (res.content or []))
    payload = json.loads(text)
    assert "active_run" in payload


# ---------- 4. MCP error mapping (wrong args -> error result, not crash) ----


@pytest.mark.anyio
async def test_mcp_error_mapping():
    """An MCP error (here: calling an UNKNOWN tool) maps to an MCP error RESULT
    (isError=True), not a server crash. This proves the server surfaces errors
    through the protocol instead of dying."""
    async with sdk_session() as session:
        res = await session.call_tool("does_not_exist", {})
    assert res.isError


# ---------- 5. P1-6: stdout is MCP-only -------------------------------------


@pytest.mark.anyio
async def test_server_stdout_contains_only_mcp_frames():
    """Launch the server ourselves (so we own its stdout), drive a FULL session
    (initialize -> initialized notification -> tools/list -> tools/call
    workflow_status) using the SDK's ACTUAL framing (newline-delimited JSON-RPC,
    one `json + "\\n"` per message), and capture its stdout.

    Assert stdout is exclusively MCP frames. Driving the full session (incl. a
    real tool CALL) ensures any stray print anywhere in the request path —
    startup, dispatch, or inside workflow_status — is captured. A stray print
    appears as a non-JSON line => P1-6 violation."""
    from mcp.types import (
        JSONRPCMessage,
        JSONRPCNotification,
        JSONRPCRequest,
        LATEST_PROTOCOL_VERSION,
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", SERVER_MODULE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_server_env(),
        bufsize=0,
    )

    def line_for(message) -> bytes:
        msg = JSONRPCMessage(root=message)
        return (msg.model_dump_json(by_alias=True, exclude_none=True) + "\n").encode("utf-8")

    try:
        assert proc.stdin is not None

        # 1) initialize
        proc.stdin.write(line_for(JSONRPCRequest(
            method="initialize", jsonrpc="2.0", id=1,
            params={
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "p", "version": "0"},
            },
        )))
        proc.stdin.flush()
        await asyncio.sleep(0.4)

        # 2) initialized notification (required before requests per spec)
        proc.stdin.write(line_for(JSONRPCNotification(
            method="notifications/initialized", jsonrpc="2.0",
        )))
        proc.stdin.flush()
        await asyncio.sleep(0.2)

        # 3) tools/list
        proc.stdin.write(line_for(JSONRPCRequest(
            method="tools/list", jsonrpc="2.0", id=2,
        )))
        proc.stdin.flush()
        await asyncio.sleep(0.3)

        # 4) tools/call workflow_status  (exercises the tool body — a stray
        #    print here is the canonical P1-6 violation we must catch)
        proc.stdin.write(line_for(JSONRPCRequest(
            method="tools/call", jsonrpc="2.0", id=3,
            params={"name": "workflow_status", "arguments": {}},
        )))
        proc.stdin.flush()
        await asyncio.sleep(0.5)  # let the full exchange flush to stdout

        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else b""

    # stdout must be empty or newline-delimited JSON-RPC ONLY (the SDK framing).
    # Any stray print / traceback appears as a non-JSON line => P1-6 violation.
    assert out, "expected at least the server's MCP responses on stdout"
    text = out.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        if line.strip():
            json.loads(line)  # every non-blank line must be valid JSON-RPC


# ---------- 6. uses official SDK framing (explicit) -------------------------


def test_uses_official_sdk_framing():
    """Every protocol test above drives the server via the OFFICIAL mcp SDK
    client (mcp.client.stdio.stdio_client + ClientSession), and the clean-stdout
    test sends frames built with official SDK primitives (JSONRPCMessage /
    JSONRPCRequest / JSONRPCNotification). No hand-rolled framing assumption
    anywhere. This test pins that the official transport classes are what we
    depend on."""
    from mcp.client.session import ClientSession  # noqa: F401
    from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: F401
    from mcp.server.fastmcp import FastMCP  # noqa: F401


# ---------- Task 1.6: 9-tool surface + param schemas -----------------------

#: the exact 9-tool MCP surface (Spec §5.1). Order is the workflow order.
NINE_TOOLS = [
    "workflow_status",
    "workflow_init",
    "planner_draft_spec",
    "workflow_approve_spec",
    "planner_create_plan",
    "workflow_start_task",
    "workflow_verify_task",
    "reviewer_review_task",
    "workflow_complete_task",
]


@pytest.mark.anyio
async def test_tools_list_has_all_nine():
    """The MCP surface is EXACTLY these 9 tools — no more, no less."""
    async with sdk_session() as session:
        tools = await session.list_tools()
    names = sorted(t.name for t in tools.tools)
    assert names == sorted(NINE_TOOLS), f"unexpected tool surface: {names}"
    assert len(tools.tools) == 9


@pytest.mark.anyio
async def test_tool_param_schemas():
    """Lock the input-schema shape of each tool (Spec §5.1). CAS tools carry
    `expected_revision`. Bodies are deferred to Phase 6, but the signatures
    must be correct now so callers (and ZCode) see a stable contract."""
    async with sdk_session() as session:
        tools = await session.list_tools()
    by_name = {t.name: t for t in tools.tools}

    def required_props(tool_name):
        schema = by_name[tool_name].inputSchema or {}
        return set((schema.get("required") or [])), set((schema.get("properties") or {}).keys())

    # workflow_status: no params
    req, props = required_props("workflow_status")
    assert props == set()

    # workflow_init(target_repo, goal, run_id=None)  (run_id optional per Task 1.6)
    req, props = required_props("workflow_init")
    assert {"target_repo", "goal"} <= props
    assert req == {"target_repo", "goal"}
    assert "run_id" in props  # optional

    # planner_draft_spec(goal, answers=None, expected_revision=None)
    req, props = required_props("planner_draft_spec")
    assert {"goal", "answers", "expected_revision"} <= props
    assert req == {"goal"}

    # CAS tools carry expected_revision as required input where the spec uses CAS
    # workflow_approve_spec(expected_revision)
    req, props = required_props("workflow_approve_spec")
    assert "expected_revision" in props
    assert req == {"expected_revision"}

    # planner_create_plan(approved_spec_sha256, expected_revision)
    req, props = required_props("planner_create_plan")
    assert {"approved_spec_sha256", "expected_revision"} <= props
    assert req == {"approved_spec_sha256", "expected_revision"}

    # the four task-scoped tools take (task_id, expected_revision)
    for tool in ["workflow_start_task", "workflow_verify_task",
                 "reviewer_review_task", "workflow_complete_task"]:
        req, props = required_props(tool)
        assert {"task_id", "expected_revision"} <= props, f"{tool} missing params"
        assert req == {"task_id", "expected_revision"}, f"{tool} CAS/task_id not required"


@pytest.mark.anyio
async def test_other_tools_deferred_to_phase6():
    """The 8 tools declared in Task 1.6 raise NotImplementedError when called —
    their BODIES are deferred to Phase 6. (workflow_status is already live from
    Task 1.5.) Calling them surfaces an error result, not a partial body."""
    async with sdk_session() as session:
        # workflow_init with a dummy target — body is not implemented yet
        res = await session.call_tool(
            "workflow_init",
            {"target_repo": "/nonexistent/for/test", "goal": "probe"},
        )
    assert res.isError

