"""Prompt template rendering (Task 4.0; P0-2; parallel P1-2, P1-5).

Three prompt files live in the repo's ``prompts/`` directory (committed
source — they are NOT runtime artifacts):

  - ``prompts/planner-draft.md`` — sent to Codex via stdin for
    ``planner_draft_spec`` (SPEC-only output).
  - ``prompts/planner-plan.md`` — sent to Codex via stdin for
    ``planner_create_plan`` (exactly-one-task-or-BLOCKED, P0-5).
  - ``prompts/reviewer.md`` — sent to Grok for ``reviewer_review_task``
    (references the Review Bundle files).

Placeholder syntax: ``{{ var }}`` (exactly two opening / two closing braces).
Each template declares its ALLOWED variable set (``TEMPLATE_VARS``); this is
the per-template guard against typo'd or stray fields (parallel P1-5).

parallel P1-5: a generic "no ``{...}`` after render" rule would false-positive
on JSON examples / code braces legitimately present in a prompt body. Only
``{{ ... }}`` is a placeholder; single ``{`` / ``}`` are left untouched.

parallel P1-2: the planner prompts INLINE ``{{ agent_context }}`` content into
the stdin prompt; they never instruct Codex to read an external
``control_root/resolved-agent-context.md`` path (its cwd is the worktree under
a read-only sandbox, which may be outside that path). The engine reads the
resolved agent-context file (Task 5.3) and passes its CONTENT as the
``agent_context`` field. Grok's reviewer prompt may reference files inside its
Review Bundle (assembled under its accessible dir).
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["PROMPTS_DIR", "TEMPLATE_VARS", "RenderError", "render"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
# fall back to the env override / repo-root layout used elsewhere
if not PROMPTS_DIR.is_dir():
    PROMPTS_DIR = Path(_REPO_ROOT / "prompts")


# Per-template allowed variable sets (parallel P1-5). Each key is the template
# filename (relative to prompts/). render() rejects any field NOT listed here
# for the requested template, and requires every listed field to be supplied.
TEMPLATE_VARS: dict[str, set[str]] = {
    "planner-draft.md": {"goal", "answers", "agent_context"},
    "planner-plan.md": {
        "approved_spec_sha256", "spec_markdown", "agent_context",
    },
    # reviewer.md references the bundle files by relative name and carries one
    # render-time placeholder: agent_context (inlined reviewer guidance). It is
    # REQUIRED when rendering reviewer.md.
    "reviewer.md": {"agent_context"},
}


class RenderError(ValueError):
    """Raised when a template cannot be rendered (unknown template, unknown or
    missing variable, or a residual ``{{ }}`` placeholder after substitution)."""


# Matches a placeholder: two opening braces, optional whitespace, a name,
# optional whitespace, two closing braces. The name must be a plain identifier
# (so a JSON ``{{k:1}}``-style blob is not falsely consumed — and JSON uses
# single braces anyway).
_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


def _load_template(template_name: str) -> str:
    """Return the raw text of ``template_name`` from ``prompts/``."""
    path = PROMPTS_DIR / template_name
    if not path.is_file():
        known = ", ".join(sorted(TEMPLATE_VARS))
        raise RenderError(
            f"unknown prompt template {template_name!r}; known: {known}"
        )
    return path.read_text(encoding="utf-8")


def render(template_name: str, **fields: str) -> str:
    """Render ``template_name`` by substituting its ``{{ var }}`` placeholders.

    Guarantees (Task 4.0):
      1. ``template_name`` must be a known template (else :class:`RenderError`).
      2. Every variable in the template's allowed set must be supplied; an
         unknown field (not in the allowed set) is rejected (parallel P1-5).
      3. Only ``{{ var }}`` is substituted; single ``{`` / ``}`` (JSON / code
         in the body) are left untouched (parallel P1-5).
      4. After substitution, no ``{{ ... }}`` may remain — a leftover means a
         placeholder was declared in the body but not in the allowed set (a
         template bug), which we surface as :class:`RenderError`.
    """
    if template_name not in TEMPLATE_VARS:
        known = ", ".join(sorted(TEMPLATE_VARS))
        raise RenderError(
            f"unknown prompt template {template_name!r}; known: {known}"
        )
    allowed = set(TEMPLATE_VARS[template_name])

    supplied = set(fields)
    unknown = supplied - allowed
    if unknown:
        raise RenderError(
            f"template {template_name!r} does not accept variable(s) "
            f"{sorted(unknown)} (allowed: {sorted(allowed)})"
        )

    body = _load_template(template_name)

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in allowed:
            raise RenderError(
                f"template {template_name!r} uses placeholder "
                f"{{{{ {name} }}}} but its allowed set is {sorted(allowed)} "
                f"(add it to TEMPLATE_VARS or remove it from the template)"
            )
        if name not in supplied:
            raise RenderError(
                f"template {template_name!r} requires variable {name!r} "
                f"which was not supplied"
            )
        return str(fields[name])

    rendered = _PLACEHOLDER_RE.sub(_sub, body)

    # Post-condition: no WELL-FORMED placeholder may remain. We search with the
    # placeholder regex, NOT a raw "{{"/"}}" substring scan, so a legitimate
    # field value containing braces (a C++ `}}`, a Go/Jinja template, an
    # f-string) is not falsely flagged (parallel P1-5: only {{ var }} is a
    # placeholder). A leftover {{ name }} here means the template body used a
    # placeholder whose name is not in the allowed set (a template bug) — the
    # per-name guard in _sub already raised for known-but-unsupplied names, so
    # reaching here implies a name that was supplied-and-allowed yet still did
    # not match (e.g. whitespace-only), which should never happen.
    if _PLACEHOLDER_RE.search(rendered) is not None:
        raise RenderError(
            f"template {template_name!r} has a residual placeholder after "
            f"rendering (a {{{{ name }}}} not in its allowed set)"
        )
    return rendered
