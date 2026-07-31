"""Config loading + validation (Task 1.2) and the capability-validation hook
(Task 1.4; signature declared here, body filled in 1.4).

The loader is a genuine *validator*: yaml.safe_load → validate against a strict
JSON Schema (`additionalProperties:false` everywhere, so unknown keys and wrong
types are rejected) → apply defaults for optional fields. It is NOT a silent
deep-merge.

External interface facts (gates) come from docs/capabilities.json and are
mirrored into config.yaml's `capabilities:` block (single source of truth).
"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import yaml

__all__ = [
    "CONFIG_SCHEMA",
    "DEFAULTS",
    "ConfigError",
    "CapabilityError",
    "load",
    "validate_capabilities",
]


class CapabilityError(RuntimeError):
    """Raised at startup when a required capability is missing, an unknown gate
    is present, or a fallback-capable gate is false but its fallback is not
    allowed by config (Spec §5.4 / G6: fail loud, never silently degrade)."""


class ConfigError(ValueError):
    """Raised when config.yaml fails schema validation or a capability gate."""


# --- strict schema -----------------------------------------------------------
# `additionalProperties: false` at every object level => unknown keys rejected.
# Required-vs-optional is encoded with `required` + a `default` applied after
# validation for optional fields (DEFAULTS below).

CONFIG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "auth", "workflow", "human_gates", "planner",
                 "reviewer", "verification", "change_policy", "review",
                 "capabilities"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "auth": {
            "type": "object",
            "additionalProperties": False,
            "required": ["codex", "grok"],
            "properties": {
                "codex": {"type": "string"},
                "grok": {"type": "string"},
            },
        },
        "workflow": {
            "type": "object",
            "additionalProperties": False,
            "required": ["worktree", "branch_prefix"],
            "properties": {
                "worktree": {"type": "boolean"},
                "branch_prefix": {"type": "string", "minLength": 1},
                "max_task_repair_rounds": {"type": "integer", "minimum": 0},
                "max_replan_rounds": {"type": "integer", "minimum": 0},
            },
        },
        "human_gates": {
            "type": "object",
            "additionalProperties": False,
            "required": ["spec_approval", "final_merge"],
            "properties": {
                "spec_approval": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["required", "method", "allow_non_adversarial_fallback"],
                    "properties": {
                        "required": {"type": "boolean"},
                        "method": {"type": "string"},
                        "allow_non_adversarial_fallback": {"type": "boolean"},
                    },
                },
                "final_merge": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["required", "v1_terminal_state"],
                    "properties": {
                        "required": {"type": "boolean"},
                        "v1_terminal_state": {"type": "string"},
                    },
                },
            },
        },
        "planner": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "command", "mode", "model", "sandbox", "timeout_sec"],
            "properties": {
                "type": {"type": "string"},
                "command": {"type": "string"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "model_reasoning_effort": {"type": "string"},
                "sandbox": {"type": "string"},
                "output_schema": {"type": "string"},
                "timeout_sec": {"type": "integer", "minimum": 1},
            },
        },
        "reviewer": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "command", "model", "output_format", "sandbox", "timeout_sec"],
            "properties": {
                "type": {"type": "string"},
                "command": {"type": "string"},
                "model": {"type": "string"},
                "output_format": {"type": "string"},
                "sandbox": {"type": "string"},
                "timeout_sec": {"type": "integer", "minimum": 1},
            },
        },
        "verification": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command_policy"],
            "properties": {
                "command_policy": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["shell", "cwd_must_be_within", "network_policy",
                                 "secret_redaction"],
                    "properties": {
                        "shell": {"type": "boolean"},
                        "cwd_must_be_within": {"type": "string"},
                        # honest advisory marker — NOT an enforced `network: deny`
                        "network_policy": {"type": "string",
                                           "enum": ["advisory_only"]},
                        "secret_redaction": {"type": "boolean"},
                    },
                },
                # Task 4.4 (P1-2, parallel P1-3): TWO distinct structured
                # allowlists. These are OPTIONAL (default []) so a config
                # without them still validates; the engine supplies runtime
                # allowlists when invoking run_verification. AGENTS.md is
                # context-only and is NEVER a security source.
                "program_allowlist": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "interpreter_allowlist": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
        },
        "change_policy": {
            "type": "object",
            "additionalProperties": False,
            "required": ["on_unauthorized_delete", "on_unauthorized_dependency",
                         "on_unauthorized_migration", "on_undeclared_file",
                         "irreversible_migration"],
            "properties": {
                "on_unauthorized_delete": {"type": "string"},
                "on_unauthorized_dependency": {"type": "string"},
                "on_unauthorized_migration": {"type": "string"},
                "on_undeclared_file": {"type": "string"},
                "irreversible_migration": {"type": "string"},
            },
        },
        "review": {
            "type": "object",
            "additionalProperties": False,
            "required": ["require", "cross_field_invariants"],
            "properties": {
                "require": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "cross_field_invariants": {"type": "boolean"},
            },
        },
        "capabilities": {
            "type": "object",
            # gate names are externally defined (docs/capabilities.json);
            # validated structurally: each gate is an object with value+class.
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "class"],
                "properties": {
                    "value": {},
                    "class": {"type": "string",
                              "enum": ["required", "optional", "fallback-capable"]},
                    "fallback": {"type": "string"},
                },
            },
        },
    },
}


# Optional fields that get a default if absent (keeps production config small
# without KeyError surprises; `test_defaults_applied_for_missing_optional`).
DEFAULTS = {
    ("workflow", "max_task_repair_rounds"): 2,
    ("workflow", "max_replan_rounds"): 2,
}


def _apply_defaults(cfg: dict) -> dict:
    for path, value in DEFAULTS.items():
        node = cfg
        for key in path[:-1]:
            node = node[key]
        node.setdefault(path[-1], value)
    return cfg


def load(path) -> dict:
    """Load YAML, validate against CONFIG_SCHEMA (strict), apply defaults.

    Raises ConfigError on any schema violation (unknown key, wrong type,
    missing required field, dishonest network policy).
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    try:
        jsonschema.validate(instance=cfg, schema=CONFIG_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"config.yaml validation failed: {exc.message}") from exc
    return _apply_defaults(cfg)


def validate_capabilities(config: dict, capabilities_findings: dict) -> dict:
    """Validate classified capability gates at startup (Task 1.4; P1-1, P1-2).

    Gate class semantics (Task 0.5 / docs/capabilities.json gates._summary):
      - ``required``        : value is false  => startup fail
      - ``optional``        : never blocks (value irrelevant)
      - ``fallback-capable``: value is false  => allowed ONLY if its fallback is
                                acceptable per config; otherwise fail
      - ``unknown``         (or any unrecognized class): block

    Runs in server startup (Task 1.5) and before workflow_init. Returns an
    ``effective_config`` with the resolved approval mode and reasoning effort.

    Special gates (resolved into the effective_config):
      - ``tool_confirmation``:
          * value true  => effective_approval_mode = "prefer_zcode_tool_confirmation"
          * value false + allow_non_adversarial_fallback=true
                        => effective_approval_mode = "non_adversarial_convention"
          * value false + allow_non_adversarial_fallback=false => CapabilityError
      - ``reasoning_effort_high``:
          * value true  => "high"; value false => fallback "medium"
    """
    findings = capabilities_findings or {}
    allow_fallback = bool(
        config.get("human_gates", {}).get("spec_approval", {}).get(
            "allow_non_adversarial_fallback", False
        )
    )

    # default effective values (absent special gates)
    effective = {
        "effective_approval_mode": "prefer_zcode_tool_confirmation",
        "effective_reasoning_effort": "high",
    }

    KNOWN_CLASSES = {"required", "optional", "fallback-capable"}

    for name, gate in findings.items():
        if name.startswith("_"):
            continue  # doc-comment metadata (e.g. gates._summary), not a gate
        if not isinstance(gate, dict):
            raise CapabilityError(f"gate {name!r} is not an object: {gate!r}")
        cls = gate.get("class")
        value = gate.get("value")

        # unknown class OR explicit unknown value => block (resolve before startup)
        if cls not in KNOWN_CLASSES or value == "unknown" or cls == "unknown":
            raise CapabilityError(
                f"capability gate {name!r} is unresolved (class={cls!r}, value={value!r})"
            )

        if cls == "optional":
            continue  # never blocks

        if cls == "required":
            if not value:
                raise CapabilityError(
                    f"required capability {name!r} is not available (value=false)"
                )
            continue

        # fallback-capable
        if value:
            continue  # capability present; no fallback needed

        # value is false for a fallback-capable gate — gate-specific resolution
        if name == "tool_confirmation":
            if allow_fallback:
                effective["effective_approval_mode"] = "non_adversarial_convention"
            else:
                raise CapabilityError(
                    "fallback-capable capability 'tool_confirmation' is false and "
                    "config human_gates.spec_approval.allow_non_adversarial_fallback "
                    "is false — refusing to silently degrade (Spec §5.4 / G6)"
                )
        elif name == "reasoning_effort_high":
            fallback = gate.get("fallback", "medium")
            effective["effective_reasoning_effort"] = fallback
        else:
            # a generic fallback-capable gate that is false: allowed only if the
            # operator opted into degradation. We map that opt-in to the single
            # global fallback switch; tool_confirmation is the canonical case.
            if not allow_fallback:
                raise CapabilityError(
                    f"fallback-capable capability {name!r} is false and no fallback "
                    "is allowed by config"
                )

    return effective
