"""Approval records — trusted approval provenance (Task 5.1; Prompt §17,
Review §六.5, P1-1; Spec §4.6 + §5.4).

Why this module exists
----------------------
``workflow_approve_spec`` is an ordinary MCP tool, so a model could call it
itself. Spec §5.4 establishes a *two-layer* scheme so an approval record can
be trusted as genuinely human-issued:

1. **Preferred (strong)**: ZCode's tool-level mandatory human confirmation,
   *configured on* ``workflow_approve_spec`` specifically. The record then
   carries ``approval_method="zcode_tool_confirmation"``.
2. **Fallback**: if strong confirmation is unavailable and the operator opted
   in via ``allow_non_adversarial_fallback``, a convention-based flow gate
   (the model follows the Skill guidance "do not call without user
   confirmation"). This does NOT defend against a malicious model; the record
   carries ``approval_method="non_adversarial_convention"`` plus a
   ``security_warning`` ZCode must display.

The central P1-1 invariant: **"supported" ≠ "configured"**. Knowing ZCode
*supports* tool confirmation is not enough — the tool must actually be
*configured* to require confirmation. Only the conjunction
``supported AND configured`` yields a strong record; ``supported`` alone but
not configured is treated as NOT strong (never falsely claim strong
confirmation). Determining "configured" is a Phase 7 packaging task; this
module takes the resolved decision as input.

Failure mode is loud, never silent (Spec §5.4 / G6): strong unavailable +
fallback not allowed ⇒ :class:`ApprovalUnavailableError`.

Records are persisted as canonical JSON under
``control_root/approvals/<approval_id>.json`` (never inside the target repo).
``record_approval`` is written within a Store ``finish_operation`` transaction
in the full engine; here it owns the canonical file write.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from zcode_agentflow import canonical

__all__ = [
    "ApprovalUnavailableError",
    "resolve_approval_mode",
    "record_approval",
    "SECURITY_WARNING",
]


class ApprovalUnavailableError(RuntimeError):
    """Raised when no strong confirmation is available AND the operator did not
    allow the non-adversarial fallback. The system must NOT silently degrade to
    a weak approval (Spec §5.4 / G6); it fails loud instead."""


# The two record-level approval methods (Spec §4.6).
STRONG = "zcode_tool_confirmation"
FALLBACK = "non_adversarial_convention"

# Security warning surfaced on a fallback (non-strong) approval. ZCode MUST
# display this to the user so the reduced assurance is never hidden (Spec §5.4:
# "同时必须向用户明确提示保障等级已降低").
SECURITY_WARNING = (
    "Security assurance is REDUCED: ZCode tool-level mandatory confirmation is "
    "not in effect for workflow_approve_spec. The approval used the "
    "non-adversarial convention flow, which relies on the model following "
    "guidance and does NOT defend against a malicious or coerced model. "
    "Verify this approval genuinely came from a human before proceeding."
)


def resolve_approval_mode(
    *,
    tool_confirmation_supported: bool,
    confirmation_configured: bool,
    allow_non_adversarial_fallback: bool,
) -> str | None:
    """Resolve the record-level ``approval_method`` (P1-1: supported ≠ configured).

    Returns one of:
      - ``"zcode_tool_confirmation"`` — strong: ZCode supports it AND it is
        *configured* on ``workflow_approve_spec``.
      - ``"non_adversarial_convention"`` — fallback: not strong, but the
        operator allowed the non-adversarial convention flow.
      - ``None`` — not strong and fallback not allowed. The caller
        (:func:`record_approval`) raises :class:`ApprovalUnavailableError`.

    ``supported=True`` alone but ``confirmation_configured=False`` is treated as
    NOT strong (never falsely claim strong confirmation) — this is the exact
    P1-1 gap.
    """
    # Strong requires BOTH supported AND configured on the specific tool.
    if tool_confirmation_supported and confirmation_configured:
        return STRONG
    # Not strong: degrade only if the operator opted into the convention flow.
    if allow_non_adversarial_fallback:
        return FALLBACK
    return None


def _approval_id(*, run_id: str, gate: str, artifact_sha256: str) -> str:
    """Deterministic approval id: ``apr_<16 hex>``.

    Stable for the same (run_id, gate, artifact_sha256) so a replay of the same
    approval does not create divergent records. The 16-hex prefix of a SHA-256
    over the identifying triple is collision-resistant for the scale of one run.
    """
    h = hashlib.sha256()
    h.update(run_id.encode("utf-8"))
    h.update(b"|")
    h.update(gate.encode("utf-8"))
    h.update(b"|")
    h.update(artifact_sha256.encode("utf-8"))
    return "apr_" + h.hexdigest()[:16]


def record_approval(
    *,
    control_root,
    run_id: str,
    gate: str,
    artifact_sha256: str,
    commit_at_approval: str,
    effective_approval_mode: str | None,
) -> dict:
    """Write a trusted approval record and return it (Spec §4.6).

    ``effective_approval_mode`` is the value resolved by
    :func:`resolve_approval_mode` / the Engine startup gate
    (``"zcode_tool_confirmation"`` | ``"non_adversarial_convention"`` | ``None``):

      - ``None`` ⇒ :class:`ApprovalUnavailableError` (loud, never silent).
      - strong  ⇒ record with ``approval_method="zcode_tool_confirmation"``.
      - fallback⇒ record with ``approval_method="non_adversarial_convention"``
                   AND a non-empty ``security_warning`` ZCode must display.

    The record is persisted as canonical JSON under
    ``control_root/approvals/<approval_id>.json`` via the canonical writer
    (atomic, LF, one trailing newline). The record is returned exactly as
    written so callers can round-trip it.
    """
    if effective_approval_mode not in (STRONG, FALLBACK, None):
        raise ValueError(
            f"effective_approval_mode must be one of {STRONG!r}, "
            f"{FALLBACK!r}, None; got {effective_approval_mode!r}"
        )
    if effective_approval_mode is None:
        raise ApprovalUnavailableError(
            "No strong tool-level confirmation is available for "
            "workflow_approve_spec and allow_non_adversarial_fallback is false — "
            "refusing to silently degrade to a weak approval (Spec §5.4 / G6)."
        )

    approval_id = _approval_id(
        run_id=run_id, gate=gate, artifact_sha256=artifact_sha256
    )
    record = {
        "approval_id": approval_id,
        "run_id": run_id,
        "gate": gate,
        "artifact_sha256": artifact_sha256,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approval_method": effective_approval_mode,
        "commit_at_approval": commit_at_approval,
    }
    if effective_approval_mode == FALLBACK:
        record["security_warning"] = SECURITY_WARNING

    approvals_dir = Path(control_root) / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    canonical.write_canonical_json(approvals_dir / f"{approval_id}.json", record)
    return record
