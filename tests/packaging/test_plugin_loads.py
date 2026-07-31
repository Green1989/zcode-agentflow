"""Task 7.2 — plugin manifest + confirmation honesty + skill/commands.

These tests pin the packaging to the REAL Phase-0 findings. The decisive fact
(docs/capabilities.json → gates.tool_confirmation): tool_confirmation is
``value:false, class:fallback-capable`` — i.e. ZCode *supports* tool-level
confirmation but it is NOT *configured* in this environment (defaultMode=yolo/
bypassPermissions), and the mechanism is ``permissions.defaultMode`` + allow/deny
rules in ``~/.claude/settings.json`` (NOT a per-tool field in a plugin
manifest). Therefore the manifest MUST NOT fabricate a per-tool confirmation
switch; per the plan's own [DATA] note, the fallback path is documented instead
and a ``security_warning`` is surfaced (ties to Task 5.1/1.4 / approval.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / ".zcode-plugin" / "plugin.json"
SKILL = REPO / "skills" / "agentflow" / "SKILL.md"
CMD_START = REPO / "commands" / "agentflow-start.md"
CMD_STATUS = REPO / "commands" / "agentflow-status.md"

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


def _load_plugin() -> dict:
    assert PLUGIN.exists(), f"plugin manifest missing at {PLUGIN}"
    return json.loads(PLUGIN.read_text(encoding="utf-8"))


# ===========================================================================
# test 1 — manifest matches the spike shape
# ===========================================================================


def test_manifest_matches_spike_shape():
    """plugin.json uses the real manifest fields recorded in capabilities.json.

    capabilities.json zcode.manifest_fields.plugin_manifest:
      path .zcode-plugin/plugin.json; component_fields =
      [commands, skills, hooks, mcpServers, agents]; mcpServers shape
      <name>: {command, args[], cwd, env}; plugins expand ${ZCODE_*} templates.
    """
    m = _load_plugin()
    # component_fields is the authoritative list of component keys a plugin may
    # declare. The manifest must carry the components AgentFlow ships.
    for key in ("mcpServers", "skills", "commands", "hooks"):
        assert key in m, f"manifest missing component field {key!r} (spike component_fields)"
    # mcpServers entry must use the spike shape (command/args/env; templates
    # expanded for plugins). cwd optional.
    servers = m["mcpServers"]
    assert isinstance(servers, dict) and servers, "mcpServers must be a non-empty object"
    name, entry = next(iter(servers.items()))
    assert "command" in entry and isinstance(entry["command"], str)
    assert "args" in entry and isinstance(entry["args"], list)
    # the MCP server is launched via the package's server entry point
    joined = entry["command"] + " " + " ".join(entry["args"])
    assert "server" in joined or "zcode_agentflow" in joined, (
        "mcpServer must launch the AgentFlow FastMCP server"
    )


# ===========================================================================
# test 2 — confirmation honesty (P1-1: supported ≠ configured)
# ===========================================================================


def test_manifest_configures_confirmation_for_approve_spec_only():
    """P1-1 honesty gate.

    capabilities.json proves tool_confirmation is ``value:false``,
    ``class:fallback-capable`` and the mechanism is ``permissions.defaultMode``
    + allow/deny rules in ``~/.claude/settings.json`` — NOT a per-tool manifest
    field. So the manifest MUST NOT claim a per-tool confirmation switch that
    does not exist (that would be "伪装"/faking the gate). Instead it documents
    the confirmed fallback and surfaces a ``security_warning`` (plan [DATA]
    note; ties to approval.py / Task 5.1).

    This test asserts the honest outcome:
      - no fabricated per-tool confirmation key on workflow_approve_spec;
      - a toolConfirmation block records supported=false, configured=false,
        fallback in effect, and the canonical security_warning text.
    """
    m = _load_plugin()
    # 1. there is NO per-tool confirmation switch on workflow_approve_spec
    #    (such a field does not exist in the ZCode plugin schema; declaring it
    #    would be fabrication).
    tools_block = m.get("tools", {})
    if isinstance(tools_block, dict):
        approve = tools_block.get("workflow_approve_spec", {})
        for forbidden in ("requireConfirmation", "confirm", "mandatoryConfirmation"):
            assert forbidden not in approve, (
                f"manifest must not fabricate per-tool confirmation field "
                f"{forbidden!r} (ZCode has no such plugin field)"
            )
    # 2. the confirmed fallback + warning is surfaced honestly.
    tc = m.get("toolConfirmation")
    assert isinstance(tc, dict), "manifest must declare a toolConfirmation block"
    assert tc.get("supported") is False, (
        "supported must be False — capabilities.json gates.tool_confirmation.value=false"
    )
    assert tc.get("configured") is False, (
        "configured must be False — defaultMode=yolo; supported ≠ configured (P1-1)"
    )
    assert tc.get("fallback") == "non_adversarial_convention", (
        "fallback must be the spike-confirmed non_adversarial_convention"
    )
    warning = tc.get("security_warning", "")
    assert isinstance(warning, str) and warning.strip(), (
        "a security_warning MUST be surfaced on a fallback (Spec §5.4 / G6)"
    )
    # the warning must be the CANONICAL text from approval.py (single source)
    from zcode_agentflow import approval
    assert warning == approval.SECURITY_WARNING


# ===========================================================================
# test 3 — SKILL describes the workflow + the no-push/terminal rules
# ===========================================================================


def test_skill_describes_workflow_and_no_push():
    """SKILL.md covers: 9-tool order, two human gates, ALLOWED_FROM, 'never edit
    SPEC/PLAN/state directly', 'READY_TO_MERGE terminal; user merges externally;
    do not push'."""
    assert SKILL.exists(), f"SKILL.md missing at {SKILL}"
    text = SKILL.read_text(encoding="utf-8")
    # 9-tool order is referenced (each tool name present)
    for tool in NINE_TOOLS:
        assert tool in text, f"SKILL.md must reference tool {tool}"
    # two human gates
    assert re.search(r"two\s+human\s+gates|2\s+human\s+gates|human\s+gates?\b", text, re.I)
    assert "workflow_approve_spec" in text and (
        "final" in text.lower() or "READY_TO_MERGE" in text
    )
    # ALLOWED_FROM concept
    assert "ALLOWED_FROM" in text
    # never edit authoritative artifacts directly
    assert re.search(r"never\s+edit.*(SPEC|PLAN|state).*(directly|yourself)", text, re.I) \
        or "do not edit" in text.lower() and "SPEC" in text
    # READY_TO_MERGE terminal + no push/merge
    assert "READY_TO_MERGE" in text
    assert re.search(r"do\s+not\s+push|never\s+push|no\s+push", text, re.I)
    assert re.search(r"do\s+not\s+merge|never\s+merge|no\s+(auto-?)?merge", text, re.I)


# ===========================================================================
# test 4 — commands exist and reference the tools
# ===========================================================================


def test_commands_reference_tools():
    """start/status commands exist and reference the workflow tools."""
    assert CMD_START.exists(), f"start command missing at {CMD_START}"
    assert CMD_STATUS.exists(), f"status command missing at {CMD_STATUS}"
    start = CMD_START.read_text(encoding="utf-8")
    status = CMD_STATUS.read_text(encoding="utf-8")
    # the start command bootstraps a run -> references workflow_init / the tools
    assert "workflow_init" in start, "start command must reference workflow_init"
    # the status command reads state -> references workflow_status
    assert "workflow_status" in status, "status command must reference workflow_status"
    # abandon/archive are local admin commands, NOT MCP tools — they must not be
    # advertised as tools in the commands
    for cmd_text, label in ((start, "start"), (status, "status")):
        assert not re.search(r"^\s*[-*]\s+`(workflow_abandon|workflow_archive|mcp__.*abandon)",
                             cmd_text, re.M), \
            f"{label} command must not list abandon/archive as MCP tools"


# ===========================================================================
# bonus — hooks component wires the Task 7.1 hooks; abandon/archive are NOT
# registered as MCP tools in the manifest (the 9-tool surface is fixed).
# ===========================================================================


def test_manifest_advertises_exactly_nine_mcp_tools():
    """The MCP surface is FIXED at 9 tools (Spec §5.1 / errata #8). The manifest
    must not register abandon/archive as MCP tools (they are local admin
    commands). The package's tool surface is asserted in the mcp suite; here we
    assert the manifest does not contradict it."""
    m = _load_plugin()
    # The AgentFlow MCP server is a single FastMCP server; the manifest exposes
    # it as ONE mcpServers entry. Tool count is enforced by the server itself
    # (tested in tests/mcp); the manifest simply must not invent extra servers
    # called abandon/archive.
    server_names = list(m.get("mcpServers", {}).keys())
    assert all("abandon" not in n and "archive" not in n for n in server_names), \
        "abandon/archive must not be registered as MCP servers/tools"
