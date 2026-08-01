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


# ===========================================================================
# AF-5 (audit-fix) — the manifest wires the PRODUCTION context env, and that
# env actually resolves a RunContext (closes the "test only parses JSON" gap;
# P0-2 root cause was that the manifest set AGENTFLOW_CONFIG/_CAPABILITIES but
# nothing read them, and ZCODE_PLUGIN_ROOT was absent).
# ===========================================================================


def test_manifest_env_wires_production_context():
    """The mcpServers env must set ZCODE_PLUGIN_ROOT (so bootstrap.process_config
    can resolve the plugin root) AND point AGENTFLOW_CONFIG/AGENTFLOW_CAPABILITIES
    at files inside the PLUGIN root (config.yaml + docs/capabilities.json ship
    WITH the plugin — they are not per-project). Pointing them at
    ${ZCODE_PROJECT_DIR} would require every target repo to carry AgentFlow's
    config, breaking the cross-project user-level plugin model (Spec §6: config
    lives at plugin_root)."""
    m = _load_plugin()
    entry = next(iter(m["mcpServers"].values()))
    env = entry.get("env", {})
    assert isinstance(env, dict)
    # ZCODE_PLUGIN_ROOT is expanded by the plugin host (capabilities.json
    # manifest_fields.plugin_manifest); the server reads it via process_config.
    assert env.get("ZCODE_PLUGIN_ROOT") == "${ZCODE_PLUGIN_ROOT}", (
        "manifest must set ZCODE_PLUGIN_ROOT so process_config() can resolve "
        "the production run context (P0-2 fix)"
    )
    # config + capabilities ship with the plugin (PLUGIN root), not per-project.
    assert env["AGENTFLOW_CONFIG"] == "${ZCODE_PLUGIN_ROOT}/config.yaml", (
        "config.yaml ships with the plugin (Spec §6); must point at the plugin "
        "root, not ${ZCODE_PROJECT_DIR}"
    )
    assert env["AGENTFLOW_CAPABILITIES"] == "${ZCODE_PLUGIN_ROOT}/docs/capabilities.json", (
        "capabilities.json ships with the plugin; must point at the plugin root"
    )


def test_manifest_env_actually_resolves_run_context(tmp_path, monkeypatch):
    """The env the manifest resolves, when expanded by the host and set on a real
    process, makes bootstrap.process_config() build a non-None RunContext. This
    is more than JSON parsing: it proves the manifest's env wiring is sufficient
    for the production context path (the gap behind P0-2).

    The host expands ${ZCODE_PLUGIN_ROOT} -> an absolute dir; we simulate that by
    using the repo itself as the plugin root (it contains config.yaml +
    docs/capabilities.json, exactly as an installed plugin would)."""
    from zcode_agentflow import bootstrap

    # a config that opts into the fallback so the capability gate resolves
    # (tool_confirmation=false in this env; the shipped config.yaml has the
    # switch false -> G6 fail-loud. Resolution-path coverage, not gate coverage.)
    import yaml
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    cfg["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] = True
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    # simulate the host-expanded env: plugin root = repo (ships config + caps),
    # user_data_root = a temp dir.
    for var in ("ZCODE_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "AGENTFLOW_RUN_CONTEXT",
                "AGENTFLOW_TEST_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(REPO))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(tmp_path / "ud"))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(cfg_path))
    monkeypatch.setenv("AGENTFLOW_CAPABILITIES",
                       str(REPO / "docs" / "capabilities.json"))

    ctx = bootstrap.process_config()
    assert isinstance(ctx, bootstrap.RunContext)
    assert ctx.plugin_root == REPO.resolve()
    assert "effective_approval_mode" in ctx.effective_config


# ===========================================================================
# Phase 7 packaging gap — local ZCode marketplace
#
# ZCode's Discover → "+" adds a *marketplace* (it expects a marketplace.json at
# the root), not a bare plugin directory. A plugin dir carrying only
# .zcode-plugin/plugin.json is therefore not installable through the UI until a
# minimal marketplace.json lists it. This pins the contract the shipped
# marketplace.json must satisfy so the plugin is discoverable as a local dev
# marketplace (no absolute paths, no directory/filesystem source objects).
# ===========================================================================


MARKETPLACE = REPO / "marketplace.json"


def _load_marketplace() -> dict:
    assert MARKETPLACE.exists(), f"marketplace.json missing at {MARKETPLACE}"
    return json.loads(MARKETPLACE.read_text(encoding="utf-8"))


def test_marketplace_exists_and_parses():
    """A local marketplace.json ships at the repo root so ZCode's Discover "+"
    can add this repo as a local marketplace (a bare plugin dir is not enough)."""
    assert MARKETPLACE.exists(), (
        "marketplace.json must ship at the repo root — ZCode Discover '+' adds a "
        "marketplace, not a bare plugin directory"
    )
    m = _load_marketplace()
    assert isinstance(m, dict), "marketplace.json must be a JSON object"


def test_marketplace_top_level_name_and_version():
    """name pins the marketplace identity; version is the documented integer 1."""
    m = _load_marketplace()
    assert m.get("name") == "zcode-agentflow-local", (
        "marketplace name must be 'zcode-agentflow-local'"
    )
    assert m.get("version") == 1, "marketplace version must be the integer 1"


def test_marketplace_lists_exactly_the_agentflow_plugin():
    """plugins must contain exactly zcode-agentflow and nothing else."""
    m = _load_marketplace()
    plugins = m.get("plugins")
    assert isinstance(plugins, list), "plugins must be a list"
    names = [p.get("name") for p in plugins]
    assert names == ["zcode-agentflow"], (
        f"plugins must be exactly ['zcode-agentflow']; got {names}"
    )


def test_marketplace_plugin_version_matches_manifest():
    """The marketplace plugin version must agree with .zcode-plugin/plugin.json
    (a drift would make the marketplace advertise a non-existent version)."""
    m = _load_marketplace()
    plugin = m["plugins"][0]
    manifest = _load_plugin()
    assert plugin.get("version") == manifest.get("version"), (
        f"marketplace plugin version {plugin.get('version')!r} must equal the "
        f"manifest version {manifest.get('version')!r}"
    )


def test_marketplace_source_is_relative_dot():
    """source is the relative path '.' — the marketplace root itself is the
    plugin root (no directory/filesystem source object, no pluginRoot)."""
    m = _load_marketplace()
    plugin = m["plugins"][0]
    source = plugin.get("source")
    assert source == ".", (
        f"plugin source must be the relative path '.' (marketplace root == plugin "
        f"root); got {source!r}"
    )
    # must NOT be a directory/filesystem source OBJECT (per packaging decision)
    assert not isinstance(source, dict), (
        "source must be a plain relative-path string, not a source object"
    )
    # pluginRoot is intentionally absent — '.' already points at the repo root
    assert "pluginRoot" not in m, (
        "pluginRoot must not be set — '.' already resolves to the marketplace root"
    )


def test_marketplace_source_resolves_to_plugin_manifest():
    """The resolved source '.' must contain .zcode-plugin/plugin.json, i.e. the
    marketplace root genuinely is a valid plugin root."""
    m = _load_marketplace()
    plugin_root = REPO / plugin_source_path(m["plugins"][0])
    assert (plugin_root / ".zcode-plugin" / "plugin.json").exists(), (
        f"resolved plugin root {plugin_root} must contain "
        f".zcode-plugin/plugin.json"
    )


def test_marketplace_contains_no_absolute_paths():
    """No host-specific absolute paths may appear anywhere in marketplace.json —
    it must stay portable (source is the relative '.')."""
    text = MARKETPLACE.read_text(encoding="utf-8")
    # re-parse and walk values so assertions are on actual data, not formatting
    m = _load_marketplace()

    def _walk(obj):
        if isinstance(obj, dict):
            yield from obj.keys()
            for v in obj.values():
                yield from _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _walk(v)
        else:
            yield obj

    for value in _walk(m):
        assert not (isinstance(value, str) and (
            # Windows drive letter (e.g. C:\) or Posix absolute (/...)
            re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("/")
        )), f"marketplace.json must not contain an absolute path: {value!r} (in {text!r})"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def plugin_source_path(plugin_entry: dict) -> str:
    """The plugin source as a path string (relative-path source form only)."""
    source = plugin_entry["source"]
    assert isinstance(source, str), (
        "this marketplace contract only permits a relative-path source string"
    )
    return source
