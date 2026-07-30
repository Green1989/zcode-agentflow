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

__all__ = ["CONFIG_SCHEMA", "DEFAULTS", "ConfigError", "load", "validate_capabilities"]


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
                    "required": ["shell", "program_allowlist_from_agents_md",
                                 "cwd_must_be_within", "network_policy",
                                 "secret_redaction"],
                    "properties": {
                        "shell": {"type": "boolean"},
                        "program_allowlist_from_agents_md": {"type": "boolean"},
                        "cwd_must_be_within": {"type": "string"},
                        # honest advisory marker — NOT an enforced `network: deny`
                        "network_policy": {"type": "string",
                                           "enum": ["advisory_only"]},
                        "secret_redaction": {"type": "boolean"},
                    },
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


def validate_capabilities(config: dict, capabilities_findings: dict | None = None) -> dict:
    """Validate classified capability gates at startup (Task 1.4).

    Body implemented in Task 1.4. Signature declared here per the Task 1.2
    [CONTRACT]. Returns an effective_config (resolved approval mode / reasoning
    effort). Raises CapabilityError when a required gate is false or an unknown
    gate is present.
    """
    raise NotImplementedError("validate_capabilities is implemented in Task 1.4")
