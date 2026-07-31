"""Git change policy — the second audit layer (Task 5.2; Prompt §16, Spec §5.3).

``workflow_verify_task`` runs TWO independent policy layers (Spec §5.2/§5.3):

  1. the *verification command* policy (run_verification, Phase 4/6), and
  2. THIS *git change* policy, which inspects ``git diff <base>..<head>`` to
     catch the dangerous changes that happen during code implementation:
       - file deletes,
       - dependency manifest / lockfile edits,
       - new migrations,
       - edits to files the PLAN never declared (out-of-scope / "越界").

Verdict rules (Spec §5.3):

    already authorized in the approved SPEC/PLAN
        (declared_files / planned_changes.may_delete_files /
         may_add_dependencies / may_run_migrations)        → allowed
    unplanned dependency / migration / delete / 越界 file  → NEEDS_HUMAN
    irreversible / high-risk migration                      → ALWAYS NEEDS_HUMAN
                                                              (even if "authorized")

The audit uses :func:`gitops.name_status_z` (the centralized ``-z`` parser) so
special filenames (spaces, quotes, unicode) and renames (Rxx/Cxx with TWO path
fields) survive intact. ``dependency_files_changed`` is a **boolean** to match
the strict execution-result schema (Review §六.9 — never a list).

Configurability: the dependency / migration / irreversible-migration filename
patterns come from ``config["change_policy"]`` (``dependency_file_patterns``,
``migration_file_patterns``, ``irreversible_migration_patterns``); when absent,
sensible cross-stack defaults apply. The four verdict actions
(``on_unauthorized_*`` / ``irreversible_migration``) are also config-driven so a
deployment can choose different escalation per category.
"""

from __future__ import annotations

import fnmatch

from zcode_agentflow import gitops

__all__ = ["audit", "DEFAULT_DEPENDENCY_PATTERNS", "DEFAULT_MIGRATION_PATTERNS"]


# Default dependency-manifest / lockfile patterns (cross-stack). Used when the
# config does not override them. These are the files whose modification implies a
# new/changed dependency — the audit classifies such a change as a dependency
# change regardless of declared_files.
DEFAULT_DEPENDENCY_PATTERNS = [
    # Python
    "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock",
    # Node
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # JVM
    "pom.xml", "build.gradle", "build.gradle.kts", "build.sbt",
    # Rust / Go / Ruby / .NET
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Gemfile", "Gemfile.lock", "packages.config",
]

# Default migration-file patterns (cross-stack). A NEW file matching these is a
# migration being ADDED. Defaults are intentionally broad directory globs.
DEFAULT_MIGRATION_PATTERNS = [
    "migrations/**", "db/migrations/**", "migrate/**",
    "alembic/versions/**", "flyway/**",
]


def _patterns(config: dict, key: str, defaults: list[str]) -> list[str]:
    """Read a pattern list from ``config["change_policy"][key]`` with a fallback
    to ``defaults``. Returns a non-empty list."""
    cp = (config or {}).get("change_policy", {}) or {}
    pats = cp.get(key)
    if not pats:
        return list(defaults)
    return list(pats)


def _matches_any(path: str, patterns: list[str]) -> bool:
    """True iff ``path`` (posix, forward-slash) matches any glob in ``patterns``.

    Uses :func:`fnmatch.fnmatchcase` (case-sensitive — git paths are). The ``**``
    sequence is collapsed to ``*`` so directory globs like ``migrations/**``
    match ``migrations/drop_users.sql``; fnmatch's ``*`` already spans ``/``.
    """
    for pat in patterns:
        norm = pat.replace("\\", "/")  # normalize config patterns to posix
        if fnmatch.fnmatchcase(path, norm.replace("**", "*")):
            return True
    return False


def audit(
    *,
    worktree,
    base_commit: str,
    head_commit: str,
    declared_files,
    planned_changes,
    config: dict,
) -> dict:
    """Audit ``base_commit..head_commit`` in ``worktree`` against the plan.

    Returns a dict with the keys consumed by the execution-result
    ``change_audit`` block (Spec §4.4 / Review §六.9) plus the escalation flags
    the engine needs to decide NEEDS_HUMAN:

      - ``needs_human``                (bool)
      - ``reasons``                    (list[str] — human-readable cause(s))
      - ``deleted_files``              (list[str])  — files removed (D status)
      - ``dependency_files_changed``   (bool)       — any dep manifest/lock edited
      - ``migrations_added``           (list[str])  — new migration files (A)
      - ``undeclared_files_changed``   (list[str])  — edits outside declared_files
      - ``irreversible_migration``     (bool)       — an irreversible migration
                                                      (ALWAYS NEEDS_HUMAN)

    Authorization comes from ``planned_changes`` (Spec §4.2):
      - ``may_delete_files``      — explicitly deletable paths
      - ``may_add_dependencies``  — bool
      - ``may_run_migrations``    — bool
    and from ``declared_files`` (the paths the PLAN said it would touch).
    """
    cp = (config or {}).get("change_policy", {}) or {}
    declared = {p.replace("\\", "/") for p in (declared_files or [])}
    pc = planned_changes or {}
    may_delete = {p.replace("\\", "/") for p in (pc.get("may_delete_files") or [])}
    may_add_deps = bool(pc.get("may_add_dependencies", False))
    may_run_migrations = bool(pc.get("may_run_migrations", False))

    dep_pats = _patterns(config, "dependency_file_patterns", DEFAULT_DEPENDENCY_PATTERNS)
    mig_pats = _patterns(config, "migration_file_patterns", DEFAULT_MIGRATION_PATTERNS)
    irr_pats = _patterns(config, "irreversible_migration_patterns", [])

    entries = gitops.name_status_z(worktree, base_commit, head_commit)

    deleted_files: list[str] = []
    dependency_files: list[str] = []
    migrations_added: list[str] = []
    undeclared_files: list[str] = []
    reasons: list[str] = []
    irreversible = False

    for status, old_path, new_path in entries:
        kind = status[0]
        old_posix = old_path.replace("\\", "/") if old_path else ""
        new_posix = new_path.replace("\\", "/") if new_path else ""

        # ---- DELETE (one path; the deleted path is old_path/new_path since
        # gitops reports D as (status, path, "")). For a real delete the path
        # existed in base and is gone in head.
        if kind == "D":
            deleted = new_posix or old_posix  # new_path is "" for D per gitops
            deleted_files.append(deleted)
            # the per-file detail is in `deleted_files`; the summary reason is
            # added post-loop only if any delete is unauthorized (avoids
            # duplicating N+1 reasons for N unauthorized deletes).
            continue

        # ---- RENAME / COPY: both old and new path. Treat as a single authorized
        # change when the NEW path is declared (refactor/rename). Do NOT count
        # the old path as a delete (it moved, not removed) — Spec §5.3 correction.
        if kind in ("R", "C"):
            # the new path is the effective touched file; check it against
            # declared/dependency/migration rules like an add/modify. A rename/
            # copy's NEW path counts as an ADDED migration (kind passed).
            _classify_path(
                new_posix, kind, declared, dep_pats, mig_pats, irr_pats,
                dependency_files, migrations_added, undeclared_files,
            )
            if _matches_any(new_posix, irr_pats):
                irreversible = True
            continue

        # ---- ADD / MODIFY / etc (one path: new_path for A/M/T/U/B).
        path = new_posix or old_posix
        _classify_path(
            path, kind, declared, dep_pats, mig_pats, irr_pats,
            dependency_files, migrations_added, undeclared_files,
        )
        if _matches_any(path, irr_pats):
            irreversible = True

    # ---- assemble verdicts --------------------------------------------------
    if deleted_files and any(d not in may_delete for d in deleted_files):
        reasons.append(
            f"{cp.get('on_unauthorized_delete', 'NEEDS_HUMAN')}: "
            f"unauthorized delete(s)"
        )
    if dependency_files and not may_add_deps:
        reasons.append(
            f"{cp.get('on_unauthorized_dependency', 'NEEDS_HUMAN')}: "
            f"dependency file(s) changed without authorization"
        )
    if migrations_added and not may_run_migrations:
        reasons.append(
            f"{cp.get('on_unauthorized_migration', 'NEEDS_HUMAN')}: "
            f"migration(s) added without authorization"
        )
    if undeclared_files:
        reasons.append(
            f"{cp.get('on_undeclared_file', 'NEEDS_HUMAN')}: "
            f"{len(undeclared_files)} undeclared file(s) changed"
        )
    if irreversible:
        reasons.append(
            f"{cp.get('irreversible_migration', 'always_need_human')}: "
            f"irreversible migration ALWAYS needs a human (Spec §5.3)"
        )

    needs_human = bool(reasons)

    return {
        "needs_human": needs_human,
        "reasons": reasons,
        "deleted_files": sorted(deleted_files),
        "dependency_files_changed": bool(dependency_files),
        "migrations_added": sorted(migrations_added),
        "undeclared_files_changed": sorted(undeclared_files),
        "irreversible_migration": irreversible,
    }


def _classify_path(
    path: str,
    kind: str,
    declared: set,
    dep_pats: list[str],
    mig_pats: list[str],
    irr_pats: list[str],
    dependency_files: list,
    migrations_added: list,
    undeclared_files: list,
) -> None:
    """Classify an added/modified/renamed-new path into dependency / migration /
    undeclared buckets (mutates the passed lists).

    ``kind`` is the status code's first char (A / M / R / C / T / U / B). A path
    is recorded in ``migrations_added`` ONLY when it is a NEW file (A) or a
    rename/copy's NEW path (R/C) — a *modification* (M) of an existing migration
    file is NOT an added migration (Spec §5.3 / key name). Dependency-file edits
    (A or M) and undeclared-file edits (A or M) qualify regardless of kind: a
    dependency manifest modification is still a dependency change, and an edit
    to a non-declared file is still out-of-scope.
    """
    is_dep = _matches_any(path, dep_pats)
    is_mig = _matches_any(path, mig_pats)
    if is_dep:
        dependency_files.append(path)
        return
    if is_mig and kind in ("A", "R", "C"):
        # only a genuinely NEW migration file counts as "added"
        migrations_added.append(path)
        return
    if path not in declared:
        undeclared_files.append(path)
