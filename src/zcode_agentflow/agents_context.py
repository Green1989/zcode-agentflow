"""Agent context resolution — never overwrite AGENTS.md (Task 5.3; Prompt §19,
Review §六.4; Spec §5.7 / R4).

The repository's ``AGENTS.md`` is the USER's own context file. AgentFlow treats
it as read-only context (R4 mitigation): it is NEVER overwritten, edited, or
reformatted. Instead this module composes a *resolved* agent context =

    <repo AGENTS.md content>  +  <plugin AGENTS.md.template AgentFlow notes>

and writes it ONCE under ``control_root/resolved-agent-context.md`` (a
repo-external runtime artifact). The Engine (6.2/6.3) reads that file and passes
its content as the ``agent_context`` render field to the planner/reviewer
prompt templates (which inline it into the stdin prompt — never instruct the
model to read an external path under a read-only sandbox).

Why a separate resolved file (and not edit the repo file):
  - editing the user's AGENTS.md would dirty their working tree and could be
    committed by mistake;
  - the notes are AgentFlow's own, project-independent guidance — they do not
    belong in every target repo;
  - keeping the user content first, then notes, gives the model a stable shape.

Idempotency: the resolved file is always recomputed from (user file, template),
never appended to the previous resolved file — so repeated ``resolve()`` calls
produce identical output (no duplicated notes section).

AGENTS.md is CONTEXT ONLY. It is never parsed into an executable allowlist and
is never a security source (Spec §5.7; the verification allowlists come from
structured config — Task 4.4 / P1-2).
"""

from __future__ import annotations

from pathlib import Path

from zcode_agentflow import canonical

__all__ = ["resolve", "RESOLVED_FILENAME"]

RESOLVED_FILENAME = "resolved-agent-context.md"


def resolve(*, target_repo, plugin_root, control_root) -> Path:
    """Compose the resolved agent context and write it under control_root.

    Reads the repo's ``AGENTS.md`` if present (UTF-8 strict — invalid UTF-8
    raises ``UnicodeDecodeError`` rather than being silently mangled), then the
    plugin's ``AGENTS.md.template``, composes (user content first, then the
    template's AgentFlow notes), and writes the result canonically (LF, one
    trailing newline) to ``control_root/resolved-agent-context.md``.

    Returns the resolved file's :class:`pathlib.Path`.

    Never writes into ``target_repo``; never edits the user's ``AGENTS.md``.
    """
    target_repo = Path(target_repo)
    plugin_root = Path(plugin_root)
    control_root = Path(control_root)

    template_path = plugin_root / "AGENTS.md.template"
    # The template is AgentFlow's own shipped file; its presence is a packaging
    # invariant. Missing it is a plugin misconfiguration, surfaced clearly.
    if not template_path.is_file():
        raise FileNotFoundError(
            f"AGENTS.md.template not found in plugin_root {plugin_root}"
        )

    # Read the user's AGENTS.md if present (strict UTF-8). Absent => empty.
    user_agents = target_repo / "AGENTS.md"
    user_bytes = user_agents.read_bytes() if user_agents.is_file() else b""

    template_bytes = template_path.read_bytes()

    # Compose: user content first, then the AgentFlow notes. Both go through the
    # canonical writer which strict-UTF-8-decodes each input — so an invalid
    # user AGENTS.md raises here (never silently mangled).
    composed = _compose(user_bytes, template_bytes)

    resolved_path = control_root / RESOLVED_FILENAME
    canonical.write_canonical_text(resolved_path, composed)
    return resolved_path


def _compose(user_bytes: bytes, template_bytes: bytes) -> bytes:
    """Compose the resolved context bytes: user content first, a separating
    blank line, then the template notes.

    Both inputs are normalized to canonical text (strict UTF-8 decode, CRLF/CR
    → LF, exactly one trailing newline) so the join is clean regardless of the
    source file's line endings, and so a repeated run is byte-identical
    (idempotent). The canonical writer applies a final canonicalization to the
    whole composed blob; the intermediate normalization here is just to make
    the join deterministic.
    """
    # canonical_text_bytes returns bytes; decode back to str to concatenate as
    # text, then re-encode (write_canonical_text canonicalizes once more).
    user_text = canonical.canonical_text_bytes(user_bytes).decode("utf-8")
    template_text = canonical.canonical_text_bytes(template_bytes).decode("utf-8")

    # user content first, then a blank-line separator, then the notes. Each
    # normalized text already ends with exactly one newline, so joining with a
    # "\n" yields the desired single blank line between them.
    return (user_text + "\n" + template_text).encode("utf-8")
