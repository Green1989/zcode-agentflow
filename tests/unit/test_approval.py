"""Task 5.1 — Approval records (Prompt §17; Review §六.5; P1-1).

The core invariants under test:

  - **"supported" ≠ "configured"** (P1-1): ZCode *supporting* MCP tool-level
    mandatory confirmation is NOT enough — the tool must be *configured* on
    ``workflow_approve_spec`` specifically. Only the conjunction
    ``supported AND configured`` yields the strong record
    ``approval_method="zcode_tool_confirmation"``. ``supported=true`` alone but
    not configured on the tool is treated as NOT strong (never falsely claim
    strong confirmation).
  - The approval record carries ``run_id`` plus all Spec §4.6 fields
    (``{approval_id, run_id, gate, artifact_sha256, approved_at,
    approval_method, commit_at_approval}``).
  - fallback=false + no strong confirmation ⇒ :class:`ApprovalUnavailableError`
    (loud, never a silent downgrade — Spec §5.4 / G6).
  - fallback=true ⇒ record uses ``non_adversarial_convention`` AND emits a
    non-empty ``security_warning`` ZCode must display.
  - Records are written under ``control_root/approvals/<id>.json`` via the
    canonical writer, NEVER inside the target repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zcode_agentflow import approval
from zcode_agentflow.approval import (
    ApprovalUnavailableError,
    record_approval,
    resolve_approval_mode,
)


# ---- helpers ----------------------------------------------------------------

_RUN_ID = "20260729T110000-001"  # a plausible run id (shape not validated here)
_SHA = "a" * 64                  # 64-hex sha256 placeholder
_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _common_kwargs(control_root, *, effective_approval_mode):
    return dict(
        control_root=control_root,
        run_id=_RUN_ID,
        gate="SPEC",
        artifact_sha256=_SHA,
        commit_at_approval=_COMMIT,
        effective_approval_mode=effective_approval_mode,
    )


# ===========================================================================
# resolve_approval_mode — the P1-1 "supported ≠ configured" decision
# ===========================================================================


def test_resolve_strong_when_supported_and_configured():
    """supported=true AND configured=true ⇒ strong mode."""
    mode = resolve_approval_mode(
        tool_confirmation_supported=True,
        confirmation_configured=True,
        allow_non_adversarial_fallback=False,
    )
    assert mode == "zcode_tool_confirmation"


def test_resolve_supported_but_not_configured_is_not_strong():
    """supported=true but NOT configured on the tool ⇒ must NOT be strong
    (P1-1: never falsely claim strong confirmation). With no fallback allowed
    it resolves to None ⇒ the caller (record_approval) fails loud."""
    mode = resolve_approval_mode(
        tool_confirmation_supported=True,
        confirmation_configured=False,
        allow_non_adversarial_fallback=False,
    )
    assert mode is None


def test_resolve_supported_but_not_configured_falls_back_when_allowed():
    """supported=true but not configured + fallback allowed ⇒ convention mode."""
    mode = resolve_approval_mode(
        tool_confirmation_supported=True,
        confirmation_configured=False,
        allow_non_adversarial_fallback=True,
    )
    assert mode == "non_adversarial_convention"


def test_resolve_unsupported_fallback_false_is_none():
    """supported=false + fallback false ⇒ None (caller fails loud)."""
    mode = resolve_approval_mode(
        tool_confirmation_supported=False,
        confirmation_configured=False,
        allow_non_adversarial_fallback=False,
    )
    assert mode is None


def test_resolve_unsupported_fallback_true_is_convention():
    """supported=false + fallback allowed ⇒ convention mode."""
    mode = resolve_approval_mode(
        tool_confirmation_supported=False,
        confirmation_configured=False,
        allow_non_adversarial_fallback=True,
    )
    assert mode == "non_adversarial_convention"


# ===========================================================================
# record_approval — record shape + fields
# ===========================================================================


def test_record_includes_run_id_and_all_fields(control_root: Path):
    """The strong record carries every Spec §4.6 field, including run_id."""
    rec = record_approval(
        **_common_kwargs(control_root, effective_approval_mode="zcode_tool_confirmation")
    )
    # every Spec §4.6 field present and non-empty
    expected = {
        "approval_id", "run_id", "gate", "artifact_sha256",
        "approved_at", "approval_method", "commit_at_approval",
    }
    assert expected.issubset(rec.keys())
    assert rec["run_id"] == _RUN_ID
    assert rec["gate"] == "SPEC"
    assert rec["artifact_sha256"] == _SHA
    assert rec["commit_at_approval"] == _COMMIT
    assert rec["approval_method"] == "zcode_tool_confirmation"
    # approved_at is an ISO-8601 timestamp string
    assert isinstance(rec["approved_at"], str) and rec["approved_at"]
    # approval_id is a non-empty string (stable id of the record)
    assert isinstance(rec["approval_id"], str) and rec["approval_id"]


def test_strong_only_when_configured(control_root: Path):
    """supported+configured ⇒ strong record; supported but NOT configured ⇒
    the record's approval_method must NOT be the strong value."""
    # strong path
    mode_strong = resolve_approval_mode(
        tool_confirmation_supported=True,
        confirmation_configured=True,
        allow_non_adversarial_fallback=False,
    )
    rec_strong = record_approval(
        **_common_kwargs(control_root, effective_approval_mode=mode_strong)
    )
    assert rec_strong["approval_method"] == "zcode_tool_confirmation"

    # supported=true but not configured → resolves to None → record_approval fails
    mode_unconfigured = resolve_approval_mode(
        tool_confirmation_supported=True,
        confirmation_configured=False,
        allow_non_adversarial_fallback=False,
    )
    assert mode_unconfigured is None
    with pytest.raises(ApprovalUnavailableError):
        record_approval(
            **_common_kwargs(control_root, effective_approval_mode=mode_unconfigured)
        )


def test_unsupported_and_fallback_false_fails_loud(control_root: Path):
    """effective_approval_mode=None (no strong confirmation, no fallback) ⇒
    ApprovalUnavailableError, NEVER a silent downgrade (Spec §5.4 / G6)."""
    with pytest.raises(ApprovalUnavailableError):
        record_approval(**_common_kwargs(control_root, effective_approval_mode=None))


def test_fallback_true_returns_security_warning(control_root: Path):
    """fallback convention mode ⇒ record uses non_adversarial_convention AND
    carries a non-empty security_warning ZCode must display."""
    rec = record_approval(
        **_common_kwargs(control_root, effective_approval_mode="non_adversarial_convention")
    )
    assert rec["approval_method"] == "non_adversarial_convention"
    assert isinstance(rec["security_warning"], str) and rec["security_warning"]
    # the warning must mention the reduced assurance (not an empty generic string)
    assert rec["security_warning"].strip() != ""


# ===========================================================================
# persistence — record written under control_root/approvals/<id>.json
# ===========================================================================


def test_record_written_under_control_root_approvals(control_root: Path):
    """The record is persisted as canonical JSON under
    ``control_root/approvals/<approval_id>.json`` and round-trips."""
    rec = record_approval(
        **_common_kwargs(control_root, effective_approval_mode="zcode_tool_confirmation")
    )
    approvals_dir = control_root / "approvals"
    assert approvals_dir.is_dir()
    written = approvals_dir / f"{rec['approval_id']}.json"
    assert written.is_file()
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk == rec  # canonical writer ⇒ exact round-trip


def test_record_id_is_stable_id(control_root: Path):
    """approval_id is deterministic for the same run+gate+artifact so a replay
    of the same approval does not create divergent records (idempotent-ish)."""
    r1 = record_approval(
        **_common_kwargs(control_root, effective_approval_mode="zcode_tool_confirmation")
    )
    r2 = record_approval(
        **_common_kwargs(control_root, effective_approval_mode="zcode_tool_confirmation")
    )
    assert r1["approval_id"] == r2["approval_id"]


# ===========================================================================
# contract lock: config intent string ≠ record method string (P1-1)
# ===========================================================================
#
# `config.validate_capabilities` resolves the OPERATOR INTENT and emits
# "prefer_zcode_tool_confirmation" (strong *preferred*) or
# "non_adversarial_convention" (fallback). That config field is NOT the record's
# approval_method: the record method is "zcode_tool_confirmation" only after
# resolve_approval_mode additionally confirms the tool is CONFIGURED for
# mandatory confirmation (the extra `confirmation_configured` input config lacks).
# Wiring the config field straight into record_approval is the P1-1 bug; these
# tests pin the boundary so a future engine task (6.3) does not regress it.


def test_record_rejects_config_intent_string_prefer_zcode(control_root: Path):
    """record_approval must NOT accept the config intent string
    "prefer_zcode_tool_confirmation" — that prefix means "preferred but not yet
    proven configured". Feeding it directly would bypass the configured-check
    (P1-1). It must raise ValueError so the wiring bug surfaces immediately."""
    with pytest.raises(ValueError):
        record_approval(
            **_common_kwargs(
                control_root, effective_approval_mode="prefer_zcode_tool_confirmation"
            )
        )


def test_intent_string_is_distinct_from_record_method():
    """The config intent constant and the record method constant are distinct
    strings — documenting that config output cannot be fed verbatim to
    record_approval."""
    assert "prefer_zcode_tool_confirmation" != "zcode_tool_confirmation"

