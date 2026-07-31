"""AF-1 (audit-fix) — production run-context resolution: `bootstrap.process_config()`.

The v2.2 plan body is silent on where the production MCP server obtains its
`RunContext` (``plugin_root`` / ``user_data_root`` / ``config`` /
``effective_config``). Phase 1–6 therefore shipped with ``AGENTFLOW_RUN_CONTEXT``
as the ONLY working path — whose own docstring admits it is "the test/subprocess
path". The production plugin manifest (``.zcode-plugin/plugin.json``) does NOT set
it, so every state-changing MCP tool raised ``no run context configured`` in a
real ZCode launch (audit P0-2).

These tests assert the production path that Plan erratum 9–10 now specifies:

  * :func:`bootstrap.process_config` resolves a :class:`bootstrap.RunContext`
    from env (``ZCODE_PLUGIN_ROOT`` / ``AGENTFLOW_USER_DATA_ROOT`` /
    ``AGENTFLOW_CONFIG`` / ``AGENTFLOW_CAPABILITIES``) — loading + validating the
    real config and running the capability gate.
  * :func:`bootstrap.build_run_context` is the SOLE dispatcher:
    ``AGENTFLOW_RUN_CONTEXT`` set -> test path (``load_run_context``); else ->
    production path (``process_config``).
  * The real MCP server, spawned with PRODUCTION env (no ``AGENTFLOW_RUN_CONTEXT``,
    no ``AGENTFLOW_TEST_MODE``), answers ``workflow_status`` instead of crashing
    with a missing-context error.

The ``load_run_context`` test-only JSON shape is INTENTIONALLY left untouched
(regression guard for the 7 tests that depend on it).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from zcode_agentflow import bootstrap

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def anyio_backend():
    """anyio backend for the stdio integration test (matches the Layer-2 pattern)."""
    return "asyncio"


# ---------------------------------------------------------------------------
# env helpers — production env vars only (never AGENTFLOW_RUN_CONTEXT)
# ---------------------------------------------------------------------------


_PRODUCTION_ENV_VARS = (
    "ZCODE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_ROOT",
    "AGENTFLOW_USER_DATA_ROOT",
    "AGENTFLOW_CONFIG",
    "AGENTFLOW_CAPABILITIES",
    "AGENTFLOW_RUN_CONTEXT",
    "AGENTFLOW_TEST_MODE",
)


@pytest.fixture(autouse=True)
def _clean_context_env(monkeypatch):
    """Strip every context-resolution env var so each test states its own inputs.

    Without this, the developer's own env (or a prior test) could leak and make a
    RED test appear GREEN (or vice versa)."""
    for var in _PRODUCTION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# process_config
# ---------------------------------------------------------------------------


def test_process_config_resolves_from_env(tmp_path, monkeypatch):
    """process_config() builds a RunContext from production env vars:
    plugin_root (ZCODE_PLUGIN_ROOT), user_data_root, config, effective_config."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    user_data = tmp_path / "agentflow-data"
    cfg = _write_fallback_config(tmp_path / "config.yaml")
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(user_data))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(cfg))
    monkeypatch.setenv(
        "AGENTFLOW_CAPABILITIES", str(REPO_ROOT / "docs" / "capabilities.json")
    )

    ctx = bootstrap.process_config()

    assert isinstance(ctx, bootstrap.RunContext)
    assert ctx.plugin_root == plugin.resolve()
    assert ctx.user_data_root == user_data.resolve()
    # config loaded + validated (strict schema): a known production field is present
    assert ctx.config["human_gates"]["spec_approval"]["required"] is True
    # effective_config came from validate_capabilities (tool_confirmation=false +
    # fallback allowed => the non_adversarial_convention mode)
    assert ctx.effective_config["effective_approval_mode"] == "non_adversarial_convention"


def test_user_data_root_defaults_to_plugin_sibling(tmp_path, monkeypatch):
    """Without AGENTFLOW_USER_DATA_ROOT, user_data_root defaults to
    <plugin_root>/../agentflow-data (a per-user run-data home, OUTSIDE the repo)."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    cfg = _write_fallback_config(tmp_path / "config.yaml")
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(cfg))
    monkeypatch.setenv(
        "AGENTFLOW_CAPABILITIES", str(REPO_ROOT / "docs" / "capabilities.json")
    )

    ctx = bootstrap.process_config()

    expected = (plugin.resolve().parent / "agentflow-data")
    assert ctx.user_data_root == expected
    # the default user_data_root is created (it is the run-data home, not per-run
    # durable state — out of Store scope)
    assert expected.exists()


def test_process_config_reads_and_validates_real_config(tmp_path, monkeypatch):
    """AGENTFLOW_CONFIG must be the REAL config.yaml, loaded through config.load()
    (strict schema: unknown keys / wrong types are rejected). Uses the fallback
    config so the capability gate resolves; the assertion is about LOADING, not
    the gate."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    cfg = _write_fallback_config(tmp_path / "config.yaml")
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(tmp_path / "ud"))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(cfg))
    monkeypatch.setenv(
        "AGENTFLOW_CAPABILITIES", str(REPO_ROOT / "docs" / "capabilities.json")
    )

    ctx = bootstrap.process_config()
    # a field that only survives schema validation (not a free-form deep-merge):
    # verification.command_policy.network_policy must be the honest advisory marker
    assert ctx.config["verification"]["command_policy"]["network_policy"] == "advisory_only"


def test_process_config_rejects_unknown_config_key(tmp_path, monkeypatch):
    """A config.yaml with an unknown top-level key must be rejected by the strict
    loader (the production path must NOT silently deep-merge a malformed config)."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    bad_cfg = tmp_path / "bad.yaml"
    bad_cfg.write_text("version: 1\nthis_key_does_not_exist: true\n", encoding="utf-8")
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(tmp_path / "ud"))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(bad_cfg))
    monkeypatch.setenv(
        "AGENTFLOW_CAPABILITIES", str(REPO_ROOT / "docs" / "capabilities.json")
    )
    with pytest.raises(Exception):
        bootstrap.process_config()


def test_process_config_rejects_when_plugin_root_unset(monkeypatch):
    """No plugin-root env var => a clear error, never a silent None / guess."""
    with pytest.raises(bootstrap.ContextResolutionError):
        bootstrap.process_config()


def test_process_config_accepts_claude_plugin_root_fallback(tmp_path, monkeypatch):
    """CLAUDE_PLUGIN_ROOT is accepted as a secondary plugin-root source
    (ZCODE_PROJECT_DIR/CLAUDE_* parity per capabilities.json template_vars)."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    cfg = _write_fallback_config(tmp_path / "config.yaml")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(tmp_path / "ud"))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(cfg))
    monkeypatch.setenv(
        "AGENTFLOW_CAPABILITIES", str(REPO_ROOT / "docs" / "capabilities.json")
    )
    ctx = bootstrap.process_config()
    assert ctx.plugin_root == plugin.resolve()


def test_process_config_defaults_config_and_caps_to_plugin_root(tmp_path, monkeypatch):
    """R2-2 (P0-1) — the REAL hook environment inherits ``ZCODE_PLUGIN_ROOT`` but
    NOT ``AGENTFLOW_CONFIG`` / ``AGENTFLOW_CAPABILITIES`` (the hook .json files
    have no env block; only mcpServers.env sets those). The hook's cwd is the
    TARGET PROJECT, so a cwd-relative ``config.yaml`` lookup would miss and the
    hook would fail-open.

    config.yaml + docs/capabilities.json ship WITH the plugin (Spec §6), so when
    the env vars are absent process_config() must fall back to
    ``<plugin_root>/config.yaml`` and ``<plugin_root>/docs/capabilities.json``.
    This is the production hook resolution path."""
    # simulate an INSTALLED plugin dir: it contains config.yaml + docs/capabilities.json
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    # the plugin ships a fallback-allowed config (mirrors R2-3 shipped config)
    _write_fallback_config(plugin / "config.yaml")
    (plugin / "docs").mkdir()
    (plugin / "docs" / "capabilities.json").write_bytes(
        (REPO_ROOT / "docs" / "capabilities.json").read_bytes()
    )
    # the hook env: ONLY ZCODE_PLUGIN_ROOT is set (no AGENTFLOW_CONFIG/_CAPABILITIES),
    # and the process cwd is some unrelated target project.
    target_project = tmp_path / "target_project"
    target_project.mkdir()
    monkeypatch.chdir(target_project)
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(tmp_path / "ud"))
    monkeypatch.delenv("AGENTFLOW_CONFIG", raising=False)
    monkeypatch.delenv("AGENTFLOW_CAPABILITIES", raising=False)

    ctx = bootstrap.process_config()
    assert ctx.plugin_root == plugin.resolve()
    # the config loaded is the PLUGIN's config.yaml (not a cwd-relative miss)
    assert ctx.config["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] is True
    assert "effective_approval_mode" in ctx.effective_config


# ---------------------------------------------------------------------------
# build_run_context — the SOLE dispatcher
# ---------------------------------------------------------------------------


def test_build_run_context_uses_test_path_when_run_context_env_set(tmp_path, monkeypatch):
    """AGENTFLOW_RUN_CONTEXT set => the test/subprocess path is used (unchanged).
    process_config() must NOT be called in this branch."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    rc_json = json.dumps({
        "plugin_root": str(plugin),
        "user_data_root": str(tmp_path / "ud"),
        "config": {"human_gates": {"spec_approval": {"required": True}}},
        "effective_config": {"effective_approval_mode": "non_adversarial_convention"},
    })
    monkeypatch.setenv("AGENTFLOW_RUN_CONTEXT", rc_json)

    ctx = bootstrap.build_run_context()
    assert ctx is not None
    assert ctx.plugin_root == plugin.resolve()
    # config here is the test-injected dict (NOT loaded from config.yaml)
    assert ctx.config["human_gates"]["spec_approval"]["required"] is True


def test_build_run_context_uses_production_path_when_no_run_context_env(tmp_path, monkeypatch):
    """No AGENTFLOW_RUN_CONTEXT => production path (process_config) is used."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    cfg = _write_fallback_config(tmp_path / "config.yaml")
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(tmp_path / "ud"))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(cfg))
    monkeypatch.setenv(
        "AGENTFLOW_CAPABILITIES", str(REPO_ROOT / "docs" / "capabilities.json")
    )
    ctx = bootstrap.build_run_context()
    assert ctx is not None
    assert ctx.plugin_root == plugin.resolve()


def test_build_run_context_returns_none_only_when_no_context_anywhere(monkeypatch, tmp_path):
    """No AGENTFLOW_RUN_CONTEXT AND no plugin-root env => the dispatcher raises
    ContextResolutionError (production has NO context to build from). It does not
    silently return None and let downstream tools crash with a confusing error."""
    # no ZCODE_PLUGIN_ROOT, no CLAUDE_PLUGIN_ROOT, no AGENTFLOW_RUN_CONTEXT
    with pytest.raises(bootstrap.ContextResolutionError):
        bootstrap.build_run_context()


# ---------------------------------------------------------------------------
# load_run_context unchanged (test-only regression guard)
# ---------------------------------------------------------------------------


def test_load_run_context_still_works_test_only(tmp_path, monkeypatch):
    """The original test-only loader must still work unchanged (7 tests depend on
    its JSON shape). process_config() is a SEPARATE function and is NOT called by
    load_run_context()."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    rc_json = json.dumps({
        "plugin_root": str(plugin),
        "user_data_root": str(tmp_path / "ud"),
        "config": {},
        "effective_config": {"effective_approval_mode": "zcode_tool_confirmation"},
    })
    monkeypatch.setenv("AGENTFLOW_RUN_CONTEXT", rc_json)
    ctx = bootstrap.load_run_context()
    assert ctx is not None
    assert ctx.effective_config["effective_approval_mode"] == "zcode_tool_confirmation"


# ---------------------------------------------------------------------------
# integration: the REAL server, PRODUCTION env, answers workflow_status
# (closes the "server crashes with no run context" P0-2)
# ---------------------------------------------------------------------------


def _write_fallback_config(dest: Path) -> Path:
    """Write a copy of config.yaml with allow_non_adversarial_fallback=true so the
    production capability gate resolves (tool_confirmation=false in this env).

    Tests exercise the *resolution* path, not the G6 fail-loud decision (that has
    its own coverage in test_config.py). Overriding the switch in the config FILE
    — the gate's real input — is the honest way to let resolution proceed here."""
    import yaml
    src = REPO_ROOT / "config.yaml"
    cfg = yaml.safe_load(src.read_text(encoding="utf-8"))
    cfg["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] = True
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return dest


def _production_env(tmp_path: Path, *, cfg_path: Path | None = None,
                    plugin: Path | None = None) -> dict[str, str]:
    """Production env for a spawned server: plugin root, user_data_root, config,
    capabilities — and NO AGENTFLOW_RUN_CONTEXT / AGENTFLOW_TEST_MODE."""
    plugin = plugin if plugin is not None else (tmp_path / "plugin")
    plugin.mkdir(exist_ok=True)
    env = {
        "ZCODE_PLUGIN_ROOT": str(plugin),
        "AGENTFLOW_USER_DATA_ROOT": str(tmp_path / "agentflow-data"),
        "AGENTFLOW_CONFIG": str(cfg_path or _write_fallback_config(tmp_path / "config.yaml")),
        "AGENTFLOW_CAPABILITIES": str(REPO_ROOT / "docs" / "capabilities.json"),
    }
    return env


@pytest.mark.anyio
async def test_server_uses_production_context_for_workflow_status(tmp_path, monkeypatch):
    """Spawn the REAL server with PRODUCTION env (ZCODE_PLUGIN_ROOT +
    AGENTFLOW_USER_DATA_ROOT + AGENTFLOW_CONFIG + AGENTFLOW_CAPABILITIES; NO
    AGENTFLOW_RUN_CONTEXT, NO AGENTFLOW_TEST_MODE) and call workflow_status over
    stdio. It must return {"active_run": None} — proving the production context
    path works end-to-end and the server does not crash / error on the call."""
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    user_data = tmp_path / "agentflow-data"
    cfg_path = _write_fallback_config(tmp_path / "config.yaml")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # PRODUCTION env — note the deliberate absence of AGENTFLOW_RUN_CONTEXT and
    # AGENTFLOW_TEST_MODE.
    env["ZCODE_PLUGIN_ROOT"] = str(plugin)
    env["AGENTFLOW_USER_DATA_ROOT"] = str(user_data)
    env["AGENTFLOW_CONFIG"] = str(cfg_path)
    env["AGENTFLOW_CAPABILITIES"] = str(REPO_ROOT / "docs" / "capabilities.json")
    env.pop("AGENTFLOW_RUN_CONTEXT", None)
    env.pop("AGENTFLOW_TEST_MODE", None)

    params = StdioServerParameters(
        command=sys.executable, args=["-u", "-m", "zcode_agentflow.server"], env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("workflow_status", {})
            assert not res.isError, (
                "workflow_status must not error in production context; got: "
                + "".join(getattr(c, "text", "") or "" for c in (res.content or []))
            )
            text = "".join(getattr(c, "text", "") or "" for c in (res.content or []))
            data = json.loads(text) if text.strip().startswith("{") else (
                getattr(res, "structuredContent", None)
                or getattr(res, "structured_content", None)
            )
            assert isinstance(data, dict)
            assert data.get("active_run") is None  # no run initialized -> None
