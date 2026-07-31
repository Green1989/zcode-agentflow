"""Task 1.2 — production config.yaml + validated loader.

These tests lock the loader as a real *validator*, not a deep-merge that
silently accepts anything. Unknown keys, wrong types, and a fake
`network: deny` (which would pretend to enforce isolation the threat model
says the MCP layer cannot provide) must be rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zcode_agentflow import config as config_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_CONFIG = REPO_ROOT / "config.yaml"


# ---------- helpers ----------------------------------------------------------


def _prod_yaml() -> dict:
    with PROD_CONFIG.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _write(tmp_path: Path, obj: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")
    return p


# ---------- required tests (Task 1.2 spec) -----------------------------------


def test_load_valid_config():
    """Production config.yaml loads and key fields are present and typed."""
    cfg = config_mod.load(PROD_CONFIG)
    assert cfg["version"] == 1
    # workflow
    assert isinstance(cfg["workflow"]["worktree"], bool)
    assert cfg["workflow"]["branch_prefix"] == "agentflow"
    # human_gates
    hg = cfg["human_gates"]["spec_approval"]
    assert hg["required"] is True
    assert hg["allow_non_adversarial_fallback"] is False
    # planner / reviewer present with a command
    assert cfg["planner"]["command"] == "codex"
    assert cfg["reviewer"]["command"] == "grok"


def test_unknown_top_level_key_rejected(tmp_path):
    obj = _prod_yaml()
    obj["totally_unknown_block"] = {"x": 1}
    p = _write(tmp_path, obj)
    with pytest.raises(Exception):
        config_mod.load(p)


def test_invalid_type_rejected(tmp_path):
    """workflow.worktree must be bool, not a string like 'yes'."""
    obj = _prod_yaml()
    obj["workflow"]["worktree"] = "yes"  # string, not bool
    p = _write(tmp_path, obj)
    with pytest.raises(Exception):
        config_mod.load(p)


def test_network_policy_is_advisory_or_absent(tmp_path):
    """Review §六.3: no fake `network: deny` pretending to enforce isolation.
    If a network policy field is present it must be the honest
    `network_policy: advisory_only`."""
    cfg = config_mod.load(PROD_CONFIG)
    cp = cfg["verification"]["command_policy"]
    # the dangerous name must NOT be present with a "deny"/"allowlist" claim
    assert "network" not in cp, (
        "config uses `network:` which pretends to enforce isolation; v1 is "
        "advisory-only (threat model: MCP provides process-integrity only)"
    )
    # if a network_policy key exists it must be advisory_only
    if "network_policy" in cp:
        assert cp["network_policy"] == "advisory_only"
    # and the production config MUST carry the honest advisory marker
    assert cp.get("network_policy") == "advisory_only"


def test_defaults_applied_for_missing_optional(tmp_path):
    """Optional keys omitted from config get sensible defaults injected by the
    loader — NOT just keys already present in the production file.

    Writes a MINIMAL config that omits the optional v1.1+/v1.2+ workflow knobs,
    then asserts they appear with the documented default values. This exercises
    `_apply_defaults` rather than reading them back from the file."""
    obj = _prod_yaml()
    # remove the optional knobs the loader must inject
    obj["workflow"].pop("max_task_repair_rounds", None)
    obj["workflow"].pop("max_replan_rounds", None)
    p = _write(tmp_path, obj)

    cfg = config_mod.load(p)
    wf = cfg["workflow"]
    assert wf["max_task_repair_rounds"] == config_mod.DEFAULTS[("workflow", "max_task_repair_rounds")]
    assert wf["max_replan_rounds"] == config_mod.DEFAULTS[("workflow", "max_replan_rounds")]
    assert isinstance(wf["max_task_repair_rounds"], int)
    assert isinstance(wf["max_replan_rounds"], int)


def test_verification_allowlists_present_and_typed():
    """Task 4.4 (P1-2, parallel P1-3): production config carries the two
    structured verification allowlists (program_allowlist for kind=executable;
    interpreter_allowlist for kind=cmd_script/bash_script)."""
    cfg = config_mod.load(PROD_CONFIG)
    v = cfg["verification"]
    assert isinstance(v["program_allowlist"], list)
    assert isinstance(v["interpreter_allowlist"], list)
    assert "python" in v["program_allowlist"]
    assert "cmd.exe" in v["interpreter_allowlist"]


def test_verification_allowlists_optional(tmp_path):
    """The allowlists are OPTIONAL: a config without them still validates (the
    engine supplies runtime allowlists when invoking run_verification)."""
    obj = _prod_yaml()
    obj["verification"].pop("program_allowlist", None)
    obj["verification"].pop("interpreter_allowlist", None)
    p = _write(tmp_path, obj)
    cfg = config_mod.load(p)
    # absent => not present (no default injected; caller supplies at call time)
    assert "program_allowlist" not in cfg["verification"]
    assert "interpreter_allowlist" not in cfg["verification"]


def test_verification_allowlist_wrong_type_rejected(tmp_path):
    """The allowlists must be arrays of non-empty strings (strict schema)."""
    obj = _prod_yaml()
    obj["verification"]["program_allowlist"] = "python"  # not an array
    p = _write(tmp_path, obj)
    with pytest.raises(Exception):
        config_mod.load(p)


def test_capabilities_block_sourced_from_findings(capabilities: dict):
    """The `capabilities:` block in config.yaml mirrors the Phase 0 gates in
    docs/capabilities.json (P1-2)."""
    cfg = config_mod.load(PROD_CONFIG)
    cap_cfg = cfg["capabilities"]
    findings_gates = capabilities["gates"]
    # a "gate" is an entry with a class/value; findings also carry a `_summary`
    # doc-comment key (underscore-prefixed) that is NOT a gate.
    gate_names = [n for n in findings_gates if not n.startswith("_")]
    # every gate name in findings must appear in config's capabilities block
    for gate_name in gate_names:
        assert gate_name in cap_cfg, f"config capabilities missing gate {gate_name}"
        # and the class/value must match the findings (single source of truth)
        assert cap_cfg[gate_name]["class"] == findings_gates[gate_name]["class"]
        assert cap_cfg[gate_name]["value"] == findings_gates[gate_name]["value"]
