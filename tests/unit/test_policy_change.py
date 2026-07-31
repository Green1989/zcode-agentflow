"""Task 5.2 — Git change policy via ``git diff --name-status -z`` (Prompt §16).

These tests use REAL temporary git repos (not mocks) to exercise the exact
``--name-status -z`` plumbing that ``gitops.name_status_z`` centralizes, across
all the cases that historically broke naive whitespace-split parsers:

  - A/M/D status codes (one path field) and Rxx/Cxx (TWO path fields — old+new);
  - renames via ``git mv`` reported as Rxx (old removed, new added) — neither
    missed nor falsely flagged as unauthorized;
  - filenames with spaces and double quotes (``weird "name".txt``) survive the
    NUL-separated parse;
  - the configurability of dependency / migration / irreversible-migration
    patterns, and the four audit verdicts of Spec §5.3:
      * declared+authorized change          → allowed
      * unauthorized delete / dependency /
        migration / undeclared file         → NEEDS_HUMAN
      * irreversible migration              → ALWAYS NEEDS_HUMAN (even if
                                              "authorized" — Spec §5.3)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from zcode_agentflow import policy_change

# A minimal env for git calls in tests (stable identity, no global config leaks).
_GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(*args, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=_GIT_ENV)
    assert result.returncode == 0, (
        f"git {args} failed: {result.stderr.decode('utf-8', 'replace')}"
    )
    return result.stdout.decode("utf-8")


def _init_repo(repo: Path) -> None:
    """Initialize a fresh git repo in ``repo`` with a stable test identity."""
    _git("init", "-q", cwd=repo)


def _commit(repo: Path, msg: str = "change") -> str:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", msg, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).strip()


def _base_config(**overrides) -> dict:
    """A change_policy config matching the committed config.yaml shape, plus the
    optional dependency/migration/irreversible pattern lists the audit reads
    (defaulted inside policy_change when absent)."""
    cp = {
        "on_unauthorized_delete": "NEEDS_HUMAN",
        "on_unauthorized_dependency": "NEEDS_HUMAN",
        "on_unauthorized_migration": "NEEDS_HUMAN",
        "on_undeclared_file": "NEEDS_HUMAN",
        "irreversible_migration": "always_need_human",
    }
    cp.update(overrides.pop("change_policy", {}))
    cfg = {"change_policy": cp}
    cfg.update(overrides)
    return cfg


def _planned(*, declared_files=None, may_add_dependencies=False,
             may_run_migrations=False, may_delete_files=None) -> dict:
    return {
        "may_add_dependencies": may_add_dependencies,
        "may_run_migrations": may_run_migrations,
        "may_delete_files": list(may_delete_files or []),
    } | ({"declared_files": list(declared_files or [])} if declared_files else {})


# ===========================================================================
# authorized / unauthorized deletes
# ===========================================================================


def test_authorized_delete_allowed(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "stale.txt").write_text("x\n", encoding="utf-8")
    base = _commit(repo, "init")
    (repo / "stale.txt").unlink()
    head = _commit(repo, "remove stale")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=[], planned_changes=_planned(may_delete_files=["stale.txt"]),
        config=_base_config(),
    )
    assert res["needs_human"] is False
    assert res["deleted_files"] == ["stale.txt"]


def test_unauthorized_delete_needs_human(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "important.txt").write_text("x\n", encoding="utf-8")
    base = _commit(repo, "init")
    (repo / "important.txt").unlink()
    head = _commit(repo, "remove important")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=[], planned_changes=_planned(may_delete_files=[]),
        config=_base_config(),
    )
    assert res["needs_human"] is True
    assert "important.txt" in res["deleted_files"]
    assert any("delete" in r.lower() for r in res["reasons"])


# ===========================================================================
# dependency change
# ===========================================================================


def test_dependency_change_unauthorized_needs_human(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    base = _commit(repo, "init")
    # modify a dependency file (package.json) without authorization
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    head = _commit(repo, "add dep")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["app.py", "package.json"],
        planned_changes=_planned(may_add_dependencies=False),
        config=_base_config(change_policy={"dependency_file_patterns": ["package.json"]}),
    )
    # dependency_files_changed is a BOOLEAN (Review §六.9 — never a list)
    assert res["dependency_files_changed"] is True
    assert isinstance(res["dependency_files_changed"], bool)
    assert res["needs_human"] is True
    assert any("depend" in r.lower() for r in res["reasons"])


def test_dependency_change_authorized_allowed(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    base = _commit(repo, "init")
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    head = _commit(repo, "add dep")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["app.py", "package.json"],
        planned_changes=_planned(may_add_dependencies=True),
        config=_base_config(change_policy={"dependency_file_patterns": ["package.json"]}),
    )
    assert res["dependency_files_changed"] is True
    assert res["needs_human"] is False


def test_dependency_rename_checks_old_and_new_paths(tmp_path: Path):
    """A rename's old path is still relevant for dependency classification."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    base = _commit(repo, "init")
    _git("mv", "requirements.txt", "requirements-prod.txt", cwd=repo)
    head = _commit(repo, "rename dependency manifest")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["requirements-prod.txt"],
        planned_changes=_planned(may_add_dependencies=True),
        config=_base_config(),
    )

    assert res["dependency_files_changed"] is True
    assert res["needs_human"] is False


def test_authorized_dependency_file_still_cannot_bypass_undeclared_check(tmp_path: Path):
    """Dependency classification and scope classification are independent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    base = _commit(repo, "init")
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    head = _commit(repo, "add dependency manifest")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=[],
        planned_changes=_planned(may_add_dependencies=True),
        config=_base_config(change_policy={"dependency_file_patterns": ["package.json"]}),
    )

    assert res["dependency_files_changed"] is True
    assert res["undeclared_files_changed"] == ["package.json"]
    assert res["needs_human"] is True


# ===========================================================================
# undeclared file
# ===========================================================================


def test_undeclared_file_needs_human(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    base = _commit(repo, "init")
    # modify a file that was NOT declared in the plan
    (repo / "secret_other.py").write_text("print('oops')\n", encoding="utf-8")
    head = _commit(repo, "touch undeclared")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["app.py"],  # secret_other.py is NOT declared
        planned_changes=_planned(),
        config=_base_config(),
    )
    assert res["needs_human"] is True
    assert "secret_other.py" in res["undeclared_files_changed"]
    assert any("undeclared" in r.lower() for r in res["reasons"])


# ===========================================================================
# rename handling (Rxx)
# ===========================================================================


def test_rename_handled_correctly(tmp_path: Path):
    """A ``git mv`` shows up as Rxx with old+new paths. The audit must see the
    rename as a single authorized change when the NEW path is declared — neither
    missing the old path nor falsely flagging it as an unauthorized delete."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "old_name.py").write_text("print('x')\n", encoding="utf-8")
    base = _commit(repo, "init")
    _git("mv", "old_name.py", "new_name.py", cwd=repo)
    head = _commit(repo, "rename")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["new_name.py"],  # the plan declared the new path
        planned_changes=_planned(),
        config=_base_config(),
    )
    # rename is not a delete, not a dependency, not undeclared => allowed
    assert res["needs_human"] is False
    # old_name.py must NOT be reported as a real deletion
    assert res["deleted_files"] == []


# ===========================================================================
# special filenames (spaces + quotes)
# ===========================================================================


def test_special_filename_with_spaces_and_quotes(tmp_path: Path):
    """A filename containing spaces (and, on POSIX, double quotes) must be parsed
    intact via the NUL-separated ``-z`` output — a whitespace split would corrupt
    it so it would not match the declared entry and be falsely flagged.

    A literal double-quote is a Windows-reserved filename char (AGENTS.md G3) and
    cannot exist on disk on Windows; we use chars that are special for SHELL /
    whitespace parsing but legal on every platform (spaces) and additionally
    exercise a double-quote name on non-Windows (matches the gitops test pattern).
    """
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("x\n", encoding="utf-8")
    base = _commit(repo, "init")
    special = "weird name with spaces.txt"   # spaces — breaks a whitespace split
    if not sys.platform.startswith("win"):
        special = 'weird "name".txt'         # double-quote is legal on POSIX
    (repo / special).write_text("data\n", encoding="utf-8")
    head = _commit(repo, "add special")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=[special],  # declared => not undeclared
        planned_changes=_planned(),
        config=_base_config(),
    )
    assert res["needs_human"] is False
    # if the parser had split on whitespace, the special name would be corrupt
    # and not match the declared entry => it would appear undeclared.
    assert res["undeclared_files_changed"] == []


# ===========================================================================
# irreversible migration always NEEDS_HUMAN
# ===========================================================================


def test_irreversible_migration_always_needs_human(tmp_path: Path):
    """An irreversible-migration pattern is ALWAYS NEEDS_HUMAN, even when the
    plan authorized migrations (Spec §5.3: '高风险或不可逆迁移 → 始终 NEEDS_HUMAN')."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("x\n", encoding="utf-8")
    base = _commit(repo, "init")
    (repo / "migrations").mkdir(parents=True, exist_ok=True)
    (repo / "migrations" / "drop_users.sql").write_text("DROP TABLE users;\n", encoding="utf-8")
    head = _commit(repo, "irreversible migration")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["app.py", "migrations/drop_users.sql"],
        planned_changes=_planned(may_run_migrations=True),  # "authorized"!
        config=_base_config(change_policy={
            "migration_file_patterns": ["migrations/**"],
            "irreversible_migration_patterns": ["migrations/drop_users.sql"],
        }),
    )
    assert res["irreversible_migration"] is True
    assert res["needs_human"] is True
    assert any("irreversible" in r.lower() for r in res["reasons"])


def test_rename_checks_old_path_for_irreversible_pattern(tmp_path: Path):
    """Renaming an irreversible migration cannot hide its old path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "drop.sql").write_text("DROP TABLE users;\n", encoding="utf-8")
    base = _commit(repo, "init")
    _git("mv", "drop.sql", "drop-users.sql", cwd=repo)
    head = _commit(repo, "rename irreversible migration")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["drop-users.sql"],
        planned_changes=_planned(),
        config=_base_config(change_policy={
            "irreversible_migration_patterns": ["drop.sql"],
        }),
    )

    assert res["irreversible_migration"] is True
    assert res["needs_human"] is True


# ===========================================================================
# migration A-vs-M distinction (migrations_added = NEW files only)
# ===========================================================================


def test_modified_migration_not_counted_as_added(tmp_path: Path):
    """``migrations_added`` records only NEW migration files (A/R/C), not a
    modification (M) of an existing one. A modified-but-existing migration is
    still a dependency-style change to flag, but it is not an 'added' migration
    — the key name and Spec §5.3 ('是否新增 migration') mean additions only.

    Here an existing migration file is MODIFIED in the task diff: it must NOT
    appear in ``migrations_added`` (it's not new), though it remains
    undeclared/changed if not in declared_files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "migrations").mkdir(parents=True, exist_ok=True)
    (repo / "migrations" / "0001_init.sql").write_text("-- init\n", encoding="utf-8")
    (repo / "app.py").write_text("x\n", encoding="utf-8")
    base = _commit(repo, "init with existing migration")
    # MODIFY the existing migration (M status, not A)
    (repo / "migrations" / "0001_init.sql").write_text("-- init + tweak\n", encoding="utf-8")
    head = _commit(repo, "tweak existing migration")

    res = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=["migrations/0001_init.sql", "app.py"],  # declared => not undeclared
        planned_changes=_planned(may_run_migrations=False),
        config=_base_config(change_policy={"migration_file_patterns": ["migrations/**"]}),
    )
    # the existing migration was MODIFIED, not added => not in migrations_added
    assert res["migrations_added"] == []
    # and since it was declared + no new migration, no human needed for migration
    assert res["needs_human"] is False


# ===========================================================================
# patterns are configurable
# ===========================================================================


def test_patterns_configurable(tmp_path: Path):
    """The dependency / migration / irreversible patterns come from config, so a
    project using a non-standard layout can configure its own. Same file content,
    different config => different verdict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("x\n", encoding="utf-8")
    base = _commit(repo, "init")
    (repo / "gems.lock").write_text("x\n", encoding="utf-8")  # ruby-ish lockfile
    head = _commit(repo, "add gems.lock")

    # Default config: gems.lock is NOT a known dependency pattern => treated as a
    # plain undeclared file (it is undeclared), needs_human for undeclared.
    res_default = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=[], planned_changes=_planned(may_add_dependencies=False),
        config=_base_config(),
    )
    # With a custom dependency pattern, the SAME file is classified as a dependency
    # change (not a generic undeclared file) — proving the pattern is configurable.
    res_custom = policy_change.audit(
        worktree=repo, base_commit=base, head_commit=head,
        declared_files=[], planned_changes=_planned(may_add_dependencies=False),
        config=_base_config(change_policy={"dependency_file_patterns": ["gems.lock"]}),
    )
    assert res_default["dependency_files_changed"] is False
    assert res_custom["dependency_files_changed"] is True
    # both still need a human (unauthorized), but for different classified reasons
    assert res_default["needs_human"] is True
    assert res_custom["needs_human"] is True
    assert any("depend" in r.lower() for r in res_custom["reasons"])
