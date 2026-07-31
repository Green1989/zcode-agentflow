"""Task 5.3 — Agent context resolution: never overwrite AGENTS.md (Prompt §19,
Review §六.4; Spec §5.7 / R4).

The repo's existing ``AGENTS.md`` is the user's own context file. AgentFlow
NEVER overwrites, edits, or reformats it (R4 mitigation / Review §六.4). Instead
it composes a *resolved* context = the user's AGENTS.md content followed by
AgentFlow's own notes (from the plugin's ``AGENTS.md.template``), written ONCE
under ``control_root/resolved-agent-context.md`` (a runtime artifact — never
inside the target repo). The engine (6.2/6.3) reads that file and passes its
content as the ``agent_context`` render field to the planner/reviewer prompts.

Invariants under test:
  - no repo AGENTS.md ⇒ resolved = template only.
  - repo AGENTS.md present ⇒ it is preserved byte-for-byte (untouched) and the
    resolved context = user content first, then the AgentFlow notes section once.
  - repeated resolve() calls do NOT duplicate the notes section.
  - non-ASCII (UTF-8) content survives intact (strict UTF-8 round-trip).
  - the resolved file lives under control_root, NEVER inside target_repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import agents_context


# ---- helpers ----------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plugin_root_with_template(tmp_path: Path, template_text: str) -> Path:
    """A plugin_root carrying an AGENTS.md.template (the AgentFlow notes)."""
    plugin = tmp_path / "plugin_root"
    _write(plugin / "AGENTS.md.template", template_text)
    return plugin


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


# ===========================================================================
# no existing AGENTS.md → template only
# ===========================================================================


def test_no_existing_agents_md_uses_template_only(tmp_path: Path):
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    plugin_root = _plugin_root_with_template(tmp_path, "# AgentFlow notes\nsingle task\n")
    control_root = tmp_path / "control_root"
    control_root.mkdir()

    out = agents_context.resolve(
        target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
    )

    resolved = _read(out)
    # the resolved content is the template content (user file absent)
    assert "AgentFlow notes" in resolved
    assert "single task" in resolved
    # and it was written under control_root/resolved-agent-context.md
    assert Path(out) == control_root / "resolved-agent-context.md"


# ===========================================================================
# existing AGENTS.md → preserved and merged (user-first, notes appended once)
# ===========================================================================


def test_existing_agents_md_preserved_and_merged(tmp_path: Path):
    user_md = "# My Project\n\nThis is the user's own AGENTS.md.\n"
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    _write(target_repo / "AGENTS.md", user_md)
    original_bytes = (target_repo / "AGENTS.md").read_bytes()
    plugin_root = _plugin_root_with_template(tmp_path, "# AgentFlow notes\nv1 single task\n")
    control_root = tmp_path / "control_root"
    control_root.mkdir()

    out = agents_context.resolve(
        target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
    )
    resolved = _read(out)

    # 1. the user file is PRESERVED byte-for-byte (never overwritten/edited)
    assert (target_repo / "AGENTS.md").read_bytes() == original_bytes
    # 2. resolved = user content FIRST, then the AgentFlow notes section once
    assert resolved.index("My Project") < resolved.index("AgentFlow notes")
    assert "This is the user's own AGENTS.md." in resolved
    # the AgentFlow notes section header appears exactly once
    assert resolved.count("AgentFlow notes") == 1


# ===========================================================================
# repeated run does not duplicate the section
# ===========================================================================


def test_repeated_run_does_not_duplicate_section(tmp_path: Path):
    """Running resolve() twice must not append the AgentFlow notes twice. The
    notes are derived from the (stable) template + the user file, so the resolved
    output is idempotent across repeated calls."""
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    _write(target_repo / "AGENTS.md", "# My Project\nuser content\n")
    plugin_root = _plugin_root_with_template(tmp_path, "# AgentFlow notes\nnotes body\n")
    control_root = tmp_path / "control_root"
    control_root.mkdir()

    out1 = agents_context.resolve(
        target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
    )
    out2 = agents_context.resolve(
        target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
    )
    # same path, identical content (idempotent — section appears once both times)
    assert Path(out1) == Path(out2)
    r1, r2 = _read(out1), _read(out2)
    assert r1 == r2
    assert r1.count("AgentFlow notes") == 1
    assert r1.count("notes body") == 1


# ===========================================================================
# non-ASCII content handled (UTF-8 strict preserved)
# ===========================================================================


def test_non_ascii_content_handled(tmp_path: Path):
    """UTF-8 content (CJK, accents) in the user AGENTS.md survives intact through
    a strict UTF-8 decode/encode round-trip. Invalid UTF-8 in the user file must
    surface as an error rather than being silently mangled."""
    user_md = "# 项目说明\n\n含中文、émojis 🚀、and accented café。\n"
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    _write(target_repo / "AGENTS.md", user_md)
    plugin_root = _plugin_root_with_template(tmp_path, "# AgentFlow notes\n")
    control_root = tmp_path / "control_root"
    control_root.mkdir()

    out = agents_context.resolve(
        target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
    )
    resolved = _read(out)
    # all non-ASCII content preserved verbatim
    assert "项目说明" in resolved
    assert "émojis 🚀" in resolved
    assert "café" in resolved


def test_invalid_utf8_user_agents_md_raises(tmp_path: Path):
    """A user AGENTS.md that is NOT valid UTF-8 must raise (strict decode),
    never be silently mangled or lossy-converted."""
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    (target_repo / "AGENTS.md").write_bytes(b"# bad utf8 \xff\xfe invalid\n")
    plugin_root = _plugin_root_with_template(tmp_path, "# AgentFlow notes\n")
    control_root = tmp_path / "control_root"
    control_root.mkdir()

    with pytest.raises(UnicodeDecodeError):
        agents_context.resolve(
            target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
        )


# ===========================================================================
# resolved written under control_root, NEVER in target_repo
# ===========================================================================


def test_resolved_written_under_control_root(tmp_path: Path):
    """The resolved file MUST live under control_root (a repo-external runtime
    artifact dir), never inside target_repo. Writing into target_repo would
    create an untracked file the user did not ask for and could be committed by
    mistake."""
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    _write(target_repo / "AGENTS.md", "# user\n")
    plugin_root = _plugin_root_with_template(tmp_path, "# AgentFlow notes\n")
    control_root = tmp_path / "control_root"
    control_root.mkdir()

    out = agents_context.resolve(
        target_repo=target_repo, plugin_root=plugin_root, control_root=control_root,
    )
    out = Path(out)
    # the resolved file is inside control_root ...
    assert control_root in out.parents
    # ... and NOT inside target_repo (no stray file created in the user's repo)
    assert target_repo not in out.parents
    assert not (target_repo / "resolved-agent-context.md").exists()
