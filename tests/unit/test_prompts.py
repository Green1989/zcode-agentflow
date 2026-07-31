"""Task 4.0 — Prompt templates (P0-2; parallel P1-2, P1-5).

Three prompt files the adapters/engine render: planner-draft, planner-plan,
reviewer. They use an explicit ``{{ var }}`` placeholder syntax with a
per-template allowed variable set.

parallel P1-5: a generic "no ``{...}`` after render" rule false-positives on
JSON examples / code braces legitimately present in a prompt body. Only
``{{ ... }}`` is a placeholder; single ``{`` / ``}`` are left untouched.

parallel P1-2: Codex's cwd is the worktree under a read-only sandbox, so an
external ``control_root/resolved-agent-context.md`` path may be unreadable.
The planner templates therefore INLINE ``{{ agent_context }}`` content into the
stdin prompt; they never instruct Codex to read an external control_root path.
(Grok's reviewer prompt may still reference files inside its Review Bundle,
which is assembled under its accessible dir.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import prompts as prompts_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

# a sample agent context (parallel P1-2 / errata #4: prompt template tests do
# NOT depend on Task 5.3's resolved-agent-context.md; the engine reads it).
SAMPLE_AGENT_CONTEXT = (
    "# Agent context\n"
    "verification: python -m pytest tests/unit/test_prompts.py -v\n"
)


# ---------------------------------------------------------------------------
# Required placeholders present per template
# ---------------------------------------------------------------------------


def test_all_required_placeholders_present():
    """Each template file contains its required ``{{ var }}`` placeholders."""
    draft = (PROMPTS_DIR / "planner-draft.md").read_text(encoding="utf-8")
    plan = (PROMPTS_DIR / "planner-plan.md").read_text(encoding="utf-8")
    reviewer = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")

    for var in ("{{ goal }}", "{{ answers }}", "{{ agent_context }}"):
        assert var in draft, f"planner-draft.md missing placeholder {var!r}"
    for var in ("{{ approved_spec_sha256 }}", "{{ spec_markdown }}",
                "{{ agent_context }}"):
        assert var in plan, f"planner-plan.md missing placeholder {var!r}"
    # reviewer references the bundle files explicitly (see test below for names)
    assert "{{ agent_context }}" in reviewer


# ---------------------------------------------------------------------------
# Render leaves no unreplaced placeholders; literal { } untouched (parallel P1-5)
# ---------------------------------------------------------------------------


def test_render_leaves_no_unreplaced_placeholders():
    """After rendering, no residual ``{{ ... }}`` remains.

    parallel P1-5: literal ``{`` / ``}`` (e.g. a JSON example in the prompt
    body) is NOT a placeholder and must NOT be flagged. Only ``{{ }}`` is.
    """
    for name, fields in (
        ("planner-draft.md", {
            "goal": "add a+b", "answers": "none", "agent_context": "ctx",
        }),
        ("planner-plan.md", {
            "approved_spec_sha256": "abc123",
            "spec_markdown": "# Spec", "agent_context": "ctx",
        }),
    ):
        rendered = prompts_mod.render(name, **fields)
        assert "{{" not in rendered, (
            f"{name}: residual placeholder after render"
        )
        assert "}}" not in rendered, (
            f"{name}: residual placeholder after render"
        )


def test_literal_braces_in_body_are_not_placeholders():
    """parallel P1-5: a single ``{`` or ``}``` (e.g. JSON example) in the body
    survives render untouched and is not treated as an unrendered placeholder.

    We exercise this through ``render``'s post-condition: even with a JSON
    brace in a supplied field, the renderer only substitutes ``{{ }}`` and
    leaves single braces alone."""
    rendered = prompts_mod.render(
        "planner-plan.md",
        approved_spec_sha256="abc",
        spec_markdown='example JSON: {"k": 1}',
        agent_context="ctx",
    )
    # the JSON braces round-trip unchanged
    assert '{"k": 1}' in rendered
    assert "{{" not in rendered and "}}" not in rendered


# ---------------------------------------------------------------------------
# Unknown / missing variables rejected (parallel P1-5 per-template variable set)
# ---------------------------------------------------------------------------


def test_unknown_variable_rejected():
    """parallel P1-5: passing a variable NOT in the template's allowed set is an
    error (per-template variable sets prevent accidental/typo'd fields)."""
    with pytest.raises(prompts_mod.RenderError):
        prompts_mod.render(
            "planner-draft.md",
            goal="g", answers="a", agent_context="c",
            not_a_real_field="oops",
        )


def test_missing_variable_rejected():
    """A required field that is omitted is also an error (no silent ``{{ }}``
    left in the rendered prompt)."""
    with pytest.raises(prompts_mod.RenderError):
        prompts_mod.render(
            "planner-draft.md",
            goal="g", answers="a",  # agent_context missing
        )


def test_unknown_template_rejected():
    with pytest.raises(prompts_mod.RenderError):
        prompts_mod.render("does-not-exist.md", foo="bar")


# ---------------------------------------------------------------------------
# Content invariants of each template body
# ---------------------------------------------------------------------------


def test_draft_template_emits_spec_only():
    """The draft body instructs SPEC-only output (no plan)."""
    body = (PROMPTS_DIR / "planner-draft.md").read_text(encoding="utf-8").lower()
    assert "spec" in body
    # must NOT ask for a plan / task list
    assert "do not produce a plan" in body or "no plan" in body \
        or "spec only" in body or "spec-only" in body


def test_plan_template_requires_exactly_one_task():
    """The plan body states: produce exactly ONE task; if the goal can't be one
    task, return BLOCKED (ties to P0-5 wrapper)."""
    body = (PROMPTS_DIR / "planner-plan.md").read_text(encoding="utf-8")
    low = body.lower()
    assert "exactly one task" in low or "exactly 1 task" in low \
        or "one task" in low
    assert "blocked" in low


def test_reviewer_references_bundle_files():
    """The reviewer body names the bundle files explicitly
    (SPEC.md/PLAN.md/task-manifest.json/execution-result.json/diff.patch)."""
    body = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
    for name in ("SPEC.md", "PLAN.md", "task-manifest.json",
                 "execution-result.json", "diff.patch"):
        assert name in body, f"reviewer.md does not reference {name!r}"


def test_agent_context_is_inlined_not_external_path():
    """parallel P1-2: the planner prompts inline ``{{ agent_context }}`` content
    into the stdin prompt; they do NOT instruct Codex to read an external
    control_root/... path (which may be outside its read-only sandbox).

    Grok's reviewer prompt may still reference files inside its Review Bundle
    (assembled under its accessible dir), so the control_root prohibition
    applies to the PLANNER templates only."""
    for name in ("planner-draft.md", "planner-plan.md"):
        body = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        low = body.lower()
        # must inline the agent context as content, not as an external read
        assert "{{ agent_context }}" in body
        assert "control_root/resolved-agent-context" not in low
        assert "read the file resolved-agent-context" not in low
        assert "resolved-agent-context.md" not in low


# ---------------------------------------------------------------------------
# Render returns the inlined content (engine-facing contract)
# ---------------------------------------------------------------------------


def test_render_inlines_agent_context_content():
    """The supplied ``agent_context`` string appears verbatim in the rendered
    prompt (the engine reads resolved-agent-context.md and passes its content
    here — Task 6.2/6.3 responsibility, parallel P1-2)."""
    rendered = prompts_mod.render(
        "planner-draft.md",
        goal="g", answers="a", agent_context=SAMPLE_AGENT_CONTEXT,
    )
    assert "verification: python -m pytest tests/unit/test_prompts.py -v" in rendered


def test_reviewer_md_renders_end_to_end():
    """reviewer.md renders with its required ``agent_context`` field and
    references all five bundle files in the rendered output."""
    rendered = prompts_mod.render("reviewer.md", agent_context=SAMPLE_AGENT_CONTEXT)
    for name in ("SPEC.md", "PLAN.md", "task-manifest.json",
                 "execution-result.json", "diff.patch"):
        assert name in rendered
    assert "{{" not in rendered and "}}" not in rendered
    # the inlined reviewer guidance round-trips
    assert "verification: python -m pytest" in rendered


def test_field_value_containing_braces_is_not_flagged():
    """parallel P1-5 regression guard: a legitimate field value that contains
    ``{{`` or ``}}`` (a C++ closer, a Go/Jinja template, an f-string) must NOT
    be treated as an unrendered placeholder. The residual check uses the
    placeholder regex, not a raw substring scan."""
    # a spec body that legitimately contains double braces (e.g. C++/Go/Jinja)
    spec_with_braces = (
        "## change\n"
        "the template uses {{ .Name }} and ends with `std::cout << x;}}`\n"
    )
    rendered = prompts_mod.render(
        "planner-plan.md",
        approved_spec_sha256="abc",
        spec_markdown=spec_with_braces,
        agent_context="ctx",
    )
    # the placeholder {{ approved_spec_sha256 }} etc. were substituted, but the
    # field's OWN double braces survive untouched and do not raise.
    assert "{{ .Name }}" in rendered
    assert "}}`" in rendered
    # the real placeholders are gone (regex did not leave a {{ name }} behind)
    from zcode_agentflow.prompts import _PLACEHOLDER_RE
    assert _PLACEHOLDER_RE.search(rendered) is None
