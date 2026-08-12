"""Task 1.4 — validate_capabilities at startup (P1-1, P1-2).

Gate class semantics (from Task 0.5 / docs/capabilities.json gates._summary):
- required && value==false  => startup fail (CapabilityError)
- optional                   => never blocks (value irrelevant)
- fallback-capable && false  => allowed ONLY if its fallback is acceptable per config
- unknown                    => block

The loader runs at server startup (before workflow_init) and produces an
effective_config: the resolved approval mode and resolved reasoning effort.
"""

from __future__ import annotations

import copy

import pytest

from zcode_agentflow import config as config_mod
from zcode_agentflow.config import (
    CapabilityError,
    validate_capabilities,
)

REPO_CONFIG_PATH = None  # tests build synthetic configs


# ---- helpers ----------------------------------------------------------------


def _base_findings() -> dict:
    """All-green findings (no gate fails) — mutate per test."""
    return {
        "planner_ok": {"value": True, "class": "required"},
        "reviewer_ok": {"value": True, "class": "required"},
        "tool_confirmation": {
            "value": True, "class": "fallback-capable", "fallback": "non_adversarial_convention",
        },
        "reasoning_effort_high": {
            "value": True, "class": "fallback-capable", "fallback": "medium",
        },
        "network_isolation": {"value": False, "class": "optional"},
        "mcp_stdio_server": {"value": True, "class": "required"},
        "hook_contract_known": {"value": True, "class": "required"},
        "proxy_required_for_external_llm": {"value": True, "class": "required"},
    }


def _base_config() -> dict:
    """Minimal config shape carrying only what validate_capabilities reads."""
    return {
        "human_gates": {
            "spec_approval": {
                "required": True,
                "method": "prefer_zcode_tool_confirmation",
                "allow_non_adversarial_fallback": False,
            },
        },
        "planner": {"model_reasoning_effort": "high"},
    }


# ---- required / optional ----------------------------------------------------


def test_required_missing_fails():
    findings = _base_findings()
    findings["planner_ok"] = {"value": False, "class": "required"}
    with pytest.raises(CapabilityError):
        validate_capabilities(_base_config(), findings)


def test_optional_false_allowed():
    findings = _base_findings()
    findings["network_isolation"] = {"value": False, "class": "optional"}
    # optional false must NOT raise
    eff = validate_capabilities(_base_config(), findings)
    assert eff is not None


# ---- fallback-capable (tool_confirmation) -----------------------------------


def test_fallback_capable_false_with_fallback_allowed():
    findings = _base_findings()
    findings["tool_confirmation"] = {
        "value": False, "class": "fallback-capable", "fallback": "non_adversarial_convention",
    }
    cfg = _base_config()
    cfg["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] = True
    eff = validate_capabilities(cfg, findings)
    assert eff["effective_approval_mode"] == "non_adversarial_convention"


def test_fallback_capable_false_without_fallback_fails():
    findings = _base_findings()
    findings["tool_confirmation"] = {
        "value": False, "class": "fallback-capable", "fallback": "non_adversarial_convention",
    }
    cfg = _base_config()
    cfg["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] = False
    with pytest.raises(CapabilityError):
        validate_capabilities(cfg, findings)


def test_tool_confirmation_true_uses_prefer_zcode():
    findings = _base_findings()
    # tool_confirmation true => effective mode is prefer_zcode_tool_confirmation
    eff = validate_capabilities(_base_config(), findings)
    assert eff["effective_approval_mode"] == "prefer_zcode_tool_confirmation"


# ---- fallback-capable (reasoning_effort_high) -------------------------------


def test_reasoning_effort_high_false_degrades_to_medium():
    findings = _base_findings()
    findings["reasoning_effort_high"] = {
        "value": False, "class": "fallback-capable", "fallback": "medium",
    }
    eff = validate_capabilities(_base_config(), findings)
    assert eff["effective_reasoning_effort"] == "medium"


def test_reasoning_effort_high_true_stays_high():
    findings = _base_findings()
    eff = validate_capabilities(_base_config(), findings)
    assert eff["effective_reasoning_effort"] == "high"


# ---- unknown blocks ---------------------------------------------------------


def test_unknown_blocks():
    findings = _base_findings()
    findings["planner_ok"] = {"value": "unknown", "class": "unknown"}
    with pytest.raises(CapabilityError):
        validate_capabilities(_base_config(), findings)


def test_gate_with_unknown_class_blocks():
    """A gate whose class is not one of the known classes is treated as blocking
    (defensive — anything we don't recognize must not pass silently)."""
    findings = _base_findings()
    findings["planner_ok"] = {"value": True, "class": "mystery"}
    with pytest.raises(CapabilityError):
        validate_capabilities(_base_config(), findings)


# ---- integration with the real production config + findings -----------------


def test_validate_against_real_config_and_findings():
    """The REAL production config.yaml + the REAL docs/capabilities.json findings.

    Real environment (capabilities.json): tool_confirmation is value=false
    (the Phase-0 spike env ran defaultMode=yolo/bypass — MCP-level mandatory
    confirmation was NOT enforced there; other envs may differ),
    class=fallback-capable, fallback=non_adversarial_convention. R2-3 (P0-3):
    the committed config.yaml ships allow_non_adversarial_fallback=true
    (capabilities tool_confirmation is value:false), so validate_capabilities
    RESOLVES to the non_adversarial_convention fallback (not raise) — the server
    can start, with the canonical security_warning surfaced (approval.py /
    plugin.json). This is the documented reduced-assurance state for an env where
    tool-level confirmation is not configured.

    If the committed config flips back to false, this assertion must flip to
    expecting CapabilityError (G6 fail-loud) — update it deliberately.
    """
    from pathlib import Path
    import json

    repo_root = Path(__file__).resolve().parents[2]
    cfg = config_mod.load(repo_root / "config.yaml")
    findings = json.loads(
        (repo_root / "docs" / "capabilities.json").read_text(encoding="utf-8")
    )
    effective = validate_capabilities(cfg, findings["gates"])
    assert effective["effective_approval_mode"] == "non_adversarial_convention"
