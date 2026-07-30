"""Task 2.4 — four-roots model + containment (Spec §2.2).

The four roots are typed, absolute, mutually non-containing paths:
  - plugin_root        : plugin code (user-level, read-only)
  - target_repo_root   : the business repo under change (Git)
  - run_worktree_root  : this run's isolated worktree (read-write code; OUTSIDE the repo)
  - control_root       : state + artifacts (OUTSIDE the repo)

MCP file operations are root-bounded: business-code ops stay within
run_worktree_root; control data ops stay within control_root; plugin resources
read from plugin_root. Containment is decided on NORMALIZED absolute paths
(resolve + collapse '..'), so a crafted ``..`` cannot escape its root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zcode_agentflow import paths


# ---------------------------------------------------------------------------
# Roots construction + mutual non-containment
# ---------------------------------------------------------------------------


def test_roots_normalize_absolute_and_distinct(tmp_path: Path):
    """make_roots stores normalized absolute paths and rejects any root that
    contains another (mutual non-containment, Prompt §11)."""
    plugin = tmp_path / "plugin"
    target = tmp_path / "target_repo"
    worktree = tmp_path / "runs" / "worktree"
    control = tmp_path / "runs" / "control"
    for d in (plugin, target, worktree, control):
        d.mkdir(parents=True)

    roots = paths.make_roots(
        plugin_root=plugin,
        target_repo_root=target,
        run_worktree_root=worktree,
        control_root=control,
    )
    # all four are normalized absolute paths
    assert roots.plugin_root == plugin.resolve()
    assert roots.target_repo_root == target.resolve()
    assert roots.run_worktree_root == worktree.resolve()
    assert roots.control_root == control.resolve()
    # mutually non-containing: none is a subpath of another
    members = [
        roots.plugin_root, roots.target_repo_root,
        roots.run_worktree_root, roots.control_root,
    ]
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            assert not paths.is_within(b, a), f"{b} is within {a} (roots must be distinct)"
            assert not paths.is_within(a, b), f"{a} is within {b} (roots must be distinct)"


def test_make_roots_rejects_nested_roots(tmp_path: Path):
    """control_root inside target_repo_root is forbidden (physical isolation
    of control data from business code, Spec §2.2)."""
    target = tmp_path / "target_repo"
    control_inside = target / "agentflow" / "control"
    target.mkdir(parents=True)
    (tmp_path / "plugin").mkdir()
    (tmp_path / "worktree").mkdir()

    with pytest.raises((ValueError, paths.RootsError)):
        paths.make_roots(
            plugin_root=tmp_path / "plugin",
            target_repo_root=target,
            run_worktree_root=tmp_path / "worktree",
            control_root=control_inside,
        )


def test_make_roots_rejects_worktree_inside_repo(tmp_path: Path):
    """run_worktree_root inside target_repo_root is forbidden — worktree lives
    OUTSIDE the repo (avoid .agentflow/worktrees polluting main repo state)."""
    target = tmp_path / "target_repo"
    worktree_inside = target / "worktree"
    target.mkdir(parents=True)
    (tmp_path / "plugin").mkdir()
    (tmp_path / "control").mkdir()

    with pytest.raises((ValueError, paths.RootsError)):
        paths.make_roots(
            plugin_root=tmp_path / "plugin",
            target_repo_root=target,
            run_worktree_root=worktree_inside,
            control_root=tmp_path / "control",
        )


def test_make_roots_rejects_equal_roots(tmp_path: Path):
    """Two roots being the SAME path is also a containment violation."""
    same = tmp_path / "x"
    same.mkdir()
    (tmp_path / "plugin").mkdir()
    (tmp_path / "control").mkdir()
    (tmp_path / "target").mkdir()
    with pytest.raises((ValueError, paths.RootsError)):
        paths.make_roots(
            plugin_root=tmp_path / "plugin",
            target_repo_root=tmp_path / "target",
            run_worktree_root=same,
            control_root=same,
        )


def test_roots_is_frozen_dataclass(tmp_path: Path):
    """Roots is immutable (frozen=True) so it can be safely shared."""
    for d in ("plugin", "target_repo", "worktree", "control"):
        (tmp_path / d).mkdir()
    roots = paths.make_roots(
        plugin_root=tmp_path / "plugin",
        target_repo_root=tmp_path / "target_repo",
        run_worktree_root=tmp_path / "worktree",
        control_root=tmp_path / "control",
    )
    with pytest.raises(Exception):
        roots.control_root = tmp_path / "elsewhere"  # type: ignore[misc]


def test_make_roots_accepts_relative_inputs_by_normalizing(tmp_path: Path,
                                                            monkeypatch):
    """Relative inputs are normalized to absolute (the caller's cwd is the
    anchor). Stored roots are always absolute regardless of input form."""
    for d in ("plugin", "target_repo", "worktree", "control"):
        (tmp_path / d).mkdir()
    monkeypatch.chdir(tmp_path)
    roots = paths.make_roots(
        plugin_root="plugin",
        target_repo_root="target_repo",
        run_worktree_root="worktree",
        control_root="control",
    )
    assert roots.plugin_root.is_absolute()
    assert roots.control_root == (tmp_path / "control").resolve()


# ---------------------------------------------------------------------------
# is_within / assert_within — containment + escape detection
# ---------------------------------------------------------------------------


def test_is_within_true_for_subpath_false_for_escape(tmp_path: Path):
    base = tmp_path / "control_root"
    base.mkdir()
    inside = base / "operations" / "op1"
    inside.mkdir(parents=True)
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()

    assert paths.is_within(inside, base) is True
    assert paths.is_within(base / "state.json", base) is True
    # a sibling outside base is NOT within
    assert paths.is_within(sibling, base) is False
    # base itself: defined as within (a root contains itself)
    assert paths.is_within(base, base) is True


def test_is_within_rejects_dotdot_escape(tmp_path: Path):
    """A path that uses '..' to escape the base must be detected as outside,
    even though its textual form starts with the base."""
    base = tmp_path / "control_root"
    base.mkdir()
    (base / "operations").mkdir()
    # control_root/operations/../../elsewhere escapes to tmp_path/elsewhere
    escaper = base / "operations" / ".." / ".." / "elsewhere"
    assert paths.is_within(escaper, base) is False

    # and a direct ../ escape
    assert paths.is_within(base / "..", base) is False


def test_is_within_handles_deep_nesting(tmp_path: Path):
    """Containment is on the normalized absolute path; a deeper nesting is fine."""
    base = tmp_path / "wt"
    deep = base / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    assert paths.is_within(deep, base) is True


def test_is_within_rejects_cross_drive(tmp_path: Path):
    """A path on a different drive than the base is NOT within it (relative_to
    raises across drives). Skipped where the platform has a single drive root."""
    import sys
    if not sys.platform.startswith("win"):
        pytest.skip("cross-drive containment is a Windows-specific path geometry")
    base = Path("C:/agentflow/control_root")
    other_drive = Path("D:/somewhere/secret")
    assert paths.is_within(other_drive, base) is False
    # and a same-drive path still behaves normally
    assert paths.is_within(Path("C:/agentflow/control_root/ops"), base) is True


def test_assert_within_raises_on_escape(tmp_path: Path):
    base = tmp_path / "control_root"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.assert_within(base / "x.json", base)  # ok, no raise
    with pytest.raises((ValueError, paths.RootsError)):
        paths.assert_within(outside, base)


def test_assert_within_raises_on_dotdot(tmp_path: Path):
    base = tmp_path / "control_root"
    base.mkdir()
    with pytest.raises((ValueError, paths.RootsError)):
        paths.assert_within(base / ".." / "secret", base)


# ---------------------------------------------------------------------------
# worktree + control live OUTSIDE the target repo (Prompt §4 layout)
# ---------------------------------------------------------------------------


def test_worktree_and_control_outside_target_repo(tmp_path: Path):
    """Per Spec §2.2 / Prompt §4, run_worktree_root and control_root must be
    OUTSIDE target_repo_root (physical isolation from business code)."""
    target = tmp_path / "target_repo"
    worktree = tmp_path / "sibling-worktree"
    control = tmp_path / "sibling-control"
    plugin = tmp_path / "plugin"
    for d in (target, worktree, control, plugin):
        d.mkdir()

    roots = paths.make_roots(
        plugin_root=plugin,
        target_repo_root=target,
        run_worktree_root=worktree,
        control_root=control,
    )
    assert not paths.is_within(roots.run_worktree_root, roots.target_repo_root)
    assert not paths.is_within(roots.control_root, roots.target_repo_root)


def test_windows_paths_usable(tmp_path: Path):
    """Windows absolute paths (drive letters, backslashes) work end-to-end. On
    Windows, resolve() yields a drive-anchored absolute path; containment works
    across drive-anchored bases."""
    for d in ("plugin", "target_repo", "worktree", "control"):
        (tmp_path / d).mkdir()
    roots = paths.make_roots(
        plugin_root=tmp_path / "plugin",
        target_repo_root=tmp_path / "target_repo",
        run_worktree_root=tmp_path / "worktree",
        control_root=tmp_path / "control",
    )
    # a path under control_root expressed with backslashes is still within it
    nested = roots.control_root / "operations" / "op1" / "state.json"
    assert paths.is_within(nested, roots.control_root) is True
