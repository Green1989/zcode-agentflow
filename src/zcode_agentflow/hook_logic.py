"""Hook logic built on the REAL Phase-0 spike contract (Task 7.1).

Two hooks, both delegating to existing authoritative code rather than
re-deriving workflow logic (Review §六.6/§六.7):

  * :func:`run_protect_artifacts` — a ``PreToolUse`` guard that denies file-edit
    tools from mutating the run's authoritative control data. It protects BOTH
    the logical control files (``state.json`` / ``events.jsonl`` /
    ``operations/*`` / ``approvals/*`` / ``reviews/*``) AND the physical
    Artifact-Index store (``artifacts/*`` / ``blobs/*``) — because with the
    complete-snapshot design the real bytes live under those trees, and guarding
    only the logical names leaves the authoritative bytes editable (parallel
    P1-1). The plugin/control root is located via the spike-confirmed mechanism
    (``ZCODE_PROJECT_DIR`` / ``CLAUDE_PROJECT_DIR``), never a hardcoded relative
    path. It conforms to the v1 threat model: process-integrity only, NOT a
    defense against a same-privilege malicious process (Spec §1.2).

  * :func:`run_gate_on_stop` / :func:`run_gate_on_stop_with_feedback` — the
    ``Stop`` hook. It delegates to the SINGLE read-only ``engine.check_stop`` and
    acts on ``{allow_stop, reasons}``. It does NOT re-derive review / clean /
    HEAD logic. Past the spike-confirmed Stop re-entry cap (3 continuations) it
    escalates to NEEDS_HUMAN feedback instead of looping.

Spike contract facts (``docs/capabilities.json`` → zcode.hooks):
  - stdin is DUAL-KEY (snake_case + camelCase); tool events carry
    ``tool_name``/``toolName`` + ``tool_input``/``toolInput``.
  - hook cwd is the project root; the plugin root is reachable via
    ``ZCODE_PROJECT_DIR`` / ``CLAUDE_PROJECT_DIR`` env vars.
  - exit codes: ``0`` pass / ``2`` deny (PreToolUse) / other-nonzero error.
  - feedback: stdout parsed as strict JSON (extra keys fail validation);
    ``additionalContext`` injects into the conversation; PreToolUse may return
    ``allow``/``ask``/``deny``; Stop may request continuation.
  - Stop may request continuation up to 3 times (``stopHookActive``).

All logging is on stderr and sanitized; hook stdout is protocol-only.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from zcode_agentflow import paths

__all__ = [
    "EXIT_ALLOW",
    "EXIT_DENY",
    "EXIT_ERROR",
    "STOP_REENTRY_CAP",
    "EDIT_TOOLS",
    "PROTECTED_SUBPATHS",
    "run_protect_artifacts",
    "run_gate_on_stop",
    "run_gate_on_stop_with_feedback",
    "build_protect_feedback",
    "build_stop_feedback",
    "_build_engine",
]

_log = logging.getLogger(__name__)

# exit codes per the spike (exit_code_denies): 0 pass / 2 deny / other error.
EXIT_ALLOW = 0
EXIT_DENY = 2
EXIT_ERROR = 1

# spike stop_reentry_limit: Stop may request continuation up to 3 times.
STOP_REENTRY_CAP = 3

# File-mutating tool names the guard applies to (spike tool_aliases: Write/Edit
# arrive as ApplyPatch; MultiEdit is the batch edit). Read/Glob/Grep are NOT
# gated — protecting control data from READS is not a v1 requirement and would
# block legitimate status/debugging.
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "ApplyPatch", "NotebookEdit"})

# Logical control-data files + the physical Artifact-Index store. The control
# root contains both layers; both are authoritative (parallel P1-1: guarding
# only SPEC.md leaves artifacts/+blobs/ editable).
PROTECTED_SUBPATHS = (
    "state.json",
    "active-run.json",
    "events.jsonl",
    "operations",
    "approvals",
    "reviews",
    "artifacts",
    "blobs",
)

# The keys a file-edit tool may carry its target path under (different tools use
# different field names). All are inspected; the first resolvable one wins.
_PATH_KEYS = ("file_path", "filePath", "notebook_path", "notebookPath", "path")


# ---------------------------------------------------------------------------
# plugin / control root resolution (spike-confirmed mechanism)
# ---------------------------------------------------------------------------


def _plugin_root_env_var() -> str | None:
    """Return the plugin root from the spike-confirmed env vars
    (``ZCODE_PROJECT_DIR`` / ``CLAUDE_PROJECT_DIR``), or None when unset.

    This is the spike-confirmed mechanism (template_vars). The hook NEVER uses a
    hardcoded relative path to find the control root (Review §六.7) — if neither
    var is set the hook treats the root as unknown."""
    return os.environ.get("ZCODE_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")


def _resolve_control_root() -> Path | None:
    """Resolve the active run's control_root from the active-run pointer.

    The plugin root (env var) locates the user_data_root that owns the
    ``active-run.json`` pointer; the pointer records the run's control_root.
    Returns None when no env var is set OR no active run exists — the hook then
    fails OPEN (allows) rather than inventing a path."""
    plugin_root = _plugin_root_env_var()
    if plugin_root is None:
        return None
    pointer = Path(plugin_root) / "active-run.json"
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cr = data.get("control_root")
    if not isinstance(cr, str) or not cr:
        return None
    return Path(cr)


# ---------------------------------------------------------------------------
# payload parsing (DUAL-KEY per the spike; pick whichever is present)
# ---------------------------------------------------------------------------


def _get_dual(payload: dict, key_snake: str, key_camel: str, default=None):
    """Read a dual-keyed field (spike: every field provided in BOTH casings)."""
    if key_snake in payload:
        return payload[key_snake]
    if key_camel in payload:
        return payload[key_camel]
    return default


def _extract_target_path(tool_input: Any) -> Path | None:
    """Extract the file path a file-edit tool targets, from any of the known
    field names. Returns None if no path can be resolved (the hook then fails
    open for that call)."""
    if not isinstance(tool_input, dict):
        return None
    for key in _PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return Path(val)
    # ApplyPatch carries a 'patch' string with diff headers; it does not name a
    # single path, so we cannot cheaply decide — fail open (the worktree change
    # audit in verify_task is the authoritative guard for edits).
    return None


def _is_protected(target: Path, control_root: Path) -> bool:
    """True iff ``target`` resolves to a protected control-data path.

    ``..`` traversal is collapsed by ``paths.is_within`` (resolve-before-
    compare), so an escape attempt toward the control root is still caught."""
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return False
    if not paths.is_within(resolved, control_root):
        return False
    # path relative to the control root (POSIX-joined for the prefix checks)
    try:
        rel = resolved.relative_to(Path(control_root).resolve())
    except ValueError:
        return False
    rel_str = rel.as_posix()
    for sub in PROTECTED_SUBPATHS:
        if rel_str == sub or rel_str.startswith(sub + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# protect_artifacts (PreToolUse)
# ---------------------------------------------------------------------------


def run_protect_artifacts(payload: dict) -> int:
    """Decide whether a PreToolUse file-edit call may proceed.

    Returns ``EXIT_ALLOW`` (0) when the edit is outside the protected control
    data (or no control root can be located / the tool is not a file edit), and
    ``EXIT_DENY`` (2) when it targets the authoritative control data. Conforms
    to the v1 threat model: process-integrity only, not a defense against a
    same-privilege malicious process that could edit the bytes directly
    (Spec §1.2)."""
    try:
        tool_name = _get_dual(payload, "tool_name", "toolName", "") or ""
        if tool_name not in EDIT_TOOLS:
            return EXIT_ALLOW
        control_root = _resolve_control_root()
        if control_root is None:
            # unknown root: fail open for ordinary edits rather than invent a
            # path. The authoritative guard remains verify_task's change audit.
            return EXIT_ALLOW
        target = _extract_target_path(_get_dual(payload, "tool_input", "toolInput", {}))
        if target is None:
            return EXIT_ALLOW
        if _is_protected(target, control_root):
            _log.warning(
                "protect_artifacts: denied edit of control data %s by %s",
                _sanitize(target), tool_name,
            )
            return EXIT_DENY
        return EXIT_ALLOW
    except Exception:  # never crash the host; an error must not block work.
        _log.exception("protect_artifacts: error evaluating payload; failing open")
        return EXIT_ALLOW


def build_protect_feedback(payload: dict, target: Path) -> dict:
    """Build the stdout-JSON feedback for a denied PreToolUse edit.

    Only spike-confirmed fields are emitted (extra keys fail validation):
    ``hookSpecificOutput.{hookEventName, permissionDecision,
    permissionDecisionReason}``."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "AgentFlow protects the run's authoritative control data "
                "(state.json, events.jsonl, operations/, approvals/, reviews/, "
                "and the artifact store artifacts/+blobs/). Edit the worktree "
                "instead; never edit control data directly. (Process-integrity "
                "only; not tamper-proof against a same-privilege process.)"
            ),
        }
    }


# ---------------------------------------------------------------------------
# gate_on_stop (Stop)
# ---------------------------------------------------------------------------


def _build_engine():
    """Build the workflow Engine for the active run, or None if there is none.

    The hook delegates the stop decision to ``engine.check_stop`` (the single
    read-only source — Review §六.6). Returns None when no run context is
    configured or there is no active run; in that case the Stop hook allows the
    stop (nothing to gate)."""
    try:
        from zcode_agentflow import bootstrap
        ctx = bootstrap.load_run_context()
        if ctx is None:
            return None
        try:
            return bootstrap.build_engine_for_active_run(ctx)
        except Exception:
            # no active run / corrupt pointer — nothing to gate.
            return None
    except Exception:
        _log.exception("gate_on_stop: could not build engine; allowing stop")
        return None


def _stop_counter_path(payload: dict) -> Path | None:
    """Location of the per-(session,turn) Stop continuation counter file.

    The host does not hand the hook a re-entry count (the spike's
    stop_reentry_limit is enforced host-side via ``stopHookActive``). To still
    escalate to NEEDS_HUMAN instead of looping, the hook keeps a tiny side
    counter under the control root, keyed by ``(sessionId, turnId)``. Returns
    None when no control root can be located — then the cap cannot be enforced
    and the hook simply requests continuation each time (the host's own cap
    remains the backstop)."""
    cr = _resolve_control_root()
    if cr is None:
        return None
    session = _get_dual(payload, "session_id", "sessionId", "") or "unknown"
    turn = _get_dual(payload, "turn_id", "turnId", "") or _get_dual(
        payload, "turnId", "turn_id", "") or "t0"
    # filesystem-safe key (ids are already restricted, but be defensive)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in f"{session}__{turn}")
    return Path(cr) / "operations" / f".stop_counter__{safe}.json"


def _continuation_count(payload: dict) -> int:
    """Read how many continuations THIS (session,turn) has already consumed.

    Returns 0 when no counter exists yet (first Stop of the turn) or when no
    control root is locatable. The counter is bumped by
    :func:`_bump_continuation` only after a continuation is actually requested,
    so the count reflects prior denials, not this call."""
    p = _stop_counter_path(payload)
    if p is None or not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("count", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _bump_continuation(payload: dict) -> None:
    """Increment the per-(session,turn) continuation counter (best-effort)."""
    p = _stop_counter_path(payload)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        count = _continuation_count(payload) + 1
        p.write_text(json.dumps({"count": count}), encoding="utf-8")
    except OSError:
        _log.warning("stop counter could not be persisted; host cap remains")


def run_gate_on_stop_with_feedback(payload: dict) -> tuple[int, dict | None]:
    """Run the Stop gate and return ``(exit_code, feedback_json_or_None)``.

    Delegates to ``engine.check_stop`` (read-only, the single source) and acts
    on ``{allow_stop, reasons}``. When the run is not clear to stop, it requests
    continuation up to :data:`STOP_REENTRY_CAP` times; past the cap it escalates
    to NEEDS_HUMAN feedback instead of looping (stop_reentry_limit)."""
    eng = _build_engine()
    if eng is None:
        # no active run / no context: nothing to gate, allow the stop.
        return EXIT_ALLOW, None
    try:
        decision = eng.check_stop()
    except Exception:
        _log.exception("gate_on_stop: check_stop raised; escalating to NEEDS_HUMAN")
        return EXIT_DENY, build_stop_feedback(payload, reasons=None,
                                              needs_human=True)
    if decision.get("allow_stop"):
        return EXIT_ALLOW, None
    reasons = list(decision.get("reasons") or [])
    # how many continuations have already been requested for this turn?
    continuations = _continuation_count(payload)
    over_cap = continuations >= STOP_REENTRY_CAP
    if over_cap:
        # past the spike-confirmed cap: stop looping, escalate to a human.
        feedback = build_stop_feedback(payload, reasons=reasons, needs_human=True)
    else:
        # under the cap: request continuation and record it.
        _bump_continuation(payload)
        feedback = build_stop_feedback(payload, reasons=reasons, needs_human=False)
    return EXIT_DENY, feedback


def run_gate_on_stop(payload: dict) -> int:
    """Stop gate returning only the exit code (matches the Task 7.1 contract)."""
    rc, _ = run_gate_on_stop_with_feedback(payload)
    return rc


def build_stop_feedback(payload: dict, *, reasons: list[str] | None,
                        needs_human: bool) -> dict:
    """Build the stdout-JSON feedback for a blocking Stop.

    Only spike-confirmed fields are emitted: top-level ``additionalContext``
    (injected into the conversation). When the re-entry cap is reached the
    context escalates to NEEDS_HUMAN instead of requesting another continuation
    (stop_reentry_limit)."""
    if needs_human:
        context = (
            "NEEDS_HUMAN: the run is not clear to stop and the Stop-hook "
            "continuation cap has been reached. Reasons: "
            + "; ".join(reasons or ["unknown"])
            + ". A human must decide: complete_task, replan, or abandon."
        )
    else:
        context = (
            "Do not stop yet — the AgentFlow run is not clear to complete. "
            "Reasons from check_stop: "
            + "; ".join(reasons or ["unknown"])
            + ". Resolve these (e.g. run the next workflow tool) before stopping."
        )
    return {"additionalContext": context}


# ---------------------------------------------------------------------------
# sanitization (stderr / feedback never leak absolute control paths verbatim)
# ---------------------------------------------------------------------------


def _sanitize(path: Path) -> str:
    """Reduce a path to its final two components for log messages (no absolute
    control_root path in stderr, per the reporting discipline)."""
    parts = path.parts
    return "/".join(parts[-2:]) if len(parts) >= 2 else str(path)
