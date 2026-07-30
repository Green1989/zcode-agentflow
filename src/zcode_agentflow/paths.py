"""Four-roots model + path containment (Task 2.4; Spec §2.2).

The four roots are typed, absolute, mutually non-containing paths:

  - ``plugin_root``        plugin code (user-level, read-only, cross-project)
  - ``target_repo_root``   the business repo under change (Git-managed)
  - ``run_worktree_root``  this run's isolated worktree (read-write code; OUTSIDE the repo)
  - ``control_root``       state + artifacts (OUTSIDE the repo)

Containment (``is_within``) is decided on NORMALIZED absolute paths: the path is
``resolve()``-d (which collapses ``..``) BEFORE comparison, so a crafted ``..``
cannot escape its root. MCP file operations use ``assert_within`` to root-bound
business-code ops to run_worktree_root and control-data ops to control_root.

This module owns path geometry ONLY. It does not create directories, read or
write files, or touch state — those are the Store's job in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["RootsError", "Roots", "make_roots", "is_within", "assert_within"]


class RootsError(ValueError):
    """Raised when roots are nested (a root contains another) or a path escapes
    its declared root."""


@dataclass(frozen=True)
class Roots:
    """The four canonical roots as normalized absolute paths.

    Frozen so a Roots instance can be shared freely (the active-run pointer,
    the Store, and the Engine all hold the same immutable reference).
    """
    plugin_root: Path
    target_repo_root: Path
    run_worktree_root: Path
    control_root: Path


def make_roots(
    *,
    plugin_root,
    target_repo_root,
    run_worktree_root,
    control_root,
) -> Roots:
    """Build a :class:`Roots` from the four inputs, normalizing each to an
    absolute path and asserting they are mutually non-containing.

    Per Spec §2.2, ``run_worktree_root`` and ``control_root`` must live OUTSIDE
    ``target_repo_root``; and no root may contain any other root (they are
    distinct, independently-managed trees).
    """
    roots = Roots(
        plugin_root=Path(plugin_root).resolve(),
        target_repo_root=Path(target_repo_root).resolve(),
        run_worktree_root=Path(run_worktree_root).resolve(),
        control_root=Path(control_root).resolve(),
    )
    members = [
        roots.plugin_root,
        roots.target_repo_root,
        roots.run_worktree_root,
        roots.control_root,
    ]
    # mutual non-containment: no root is a subpath of another (incl. equality)
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            if _raw_contains(a, b) or _raw_contains(b, a):
                raise RootsError(
                    f"roots must be mutually non-containing, but one contains "
                    f"another: {a} vs {b}"
                )
    return roots


def is_within(path, base) -> bool:
    """Return True iff ``path`` is equal to or nested under ``base``.

    Both are resolved (``..`` collapsed) before comparison, so a path that
    textually starts with ``base`` but escapes via ``..`` is correctly reported
    as OUTSIDE. ``base`` is considered to contain itself.
    """
    p = Path(path).resolve()
    b = Path(base).resolve()
    if p == b:
        return True
    try:
        # relative_to raises if p is not under b — that is exactly containment
        p.relative_to(b)
        return True
    except ValueError:
        return False


def assert_within(path, base) -> None:
    """Raise :class:`RootsError` if ``path`` is not within ``base``.

    The escape attempt (a ``..`` that leaves the root) is surfaced as a hard
    error rather than silently allowed.
    """
    if not is_within(path, base):
        raise RootsError(
            f"path {path!s} is outside its root {base!s} "
            f"(resolved: {Path(path).resolve()} not under {Path(base).resolve()})"
        )


# ---------------------------------------------------------------------------
# internal
# ---------------------------------------------------------------------------


def _raw_contains(parent, child) -> bool:
    """True iff ``child`` is equal to or nested under ``parent`` (both already
    resolved). Used only by make_roots for the mutual-non-containment check."""
    parent = Path(parent)
    child = Path(child)
    if parent == child:
        return True
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
