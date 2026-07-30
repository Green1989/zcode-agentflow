"""Task 2.6 — git primitives, a dedicated fully-tested module (P0-2).

All git operations live here (consumed by store/adapters/engine/policy). These
tests use REAL temporary git repos (not string mocks) and cover the tricky
cases: --name-status -z parsing of A/D/M/Rxx with old+new paths, filenames with
spaces / quotes / unicode, worktree creation outside the repo, etc.

The conftest `fake_repo` fixture gives a clean repo with one committed file;
these tests build on top of it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from zcode_agentflow import gitops

# A minimal env for git calls in tests (stable identity, no global config leaks).
_GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(*args, cwd: Path) -> str:
    """Run a git command in cwd, return stdout (utf-8). Raises on failure."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, env=_GIT_ENV,
    )
    assert result.returncode == 0, (
        f"git {args} failed: {result.stderr.decode('utf-8', 'replace')}"
    )
    return result.stdout.decode("utf-8")


def _commit(repo: Path, msg: str = "change") -> str:
    """Stage all changes and commit; return the new HEAD commit hash."""
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", msg, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).strip()


# ===========================================================================
# basic repo queries
# ===========================================================================


def test_is_git_repo(fake_repo: Path, tmp_path: Path):
    assert gitops.is_git_repo(fake_repo) is True
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert gitops.is_git_repo(not_a_repo) is False


def test_head(fake_repo: Path):
    h = gitops.head(fake_repo)
    assert isinstance(h, str) and len(h) == 40  # full 40-hex commit
    # matches git rev-parse HEAD
    assert h == _git("rev-parse", "HEAD", cwd=fake_repo).strip()


def test_is_clean(fake_repo: Path):
    """A fresh repo with everything committed is clean."""
    assert gitops.is_clean(fake_repo) is True
    # dirty after a new untracked file
    (fake_repo / "new.txt").write_text("x", encoding="utf-8")
    assert gitops.is_clean(fake_repo) is False


def test_untracked_files(fake_repo: Path):
    """untracked_files lists files not tracked by git (uses -z so special names
    survive)."""
    assert gitops.untracked_files(fake_repo) == []
    (fake_repo / "untracked.txt").write_text("x", encoding="utf-8")
    files = gitops.untracked_files(fake_repo)
    assert "untracked.txt" in files


# ===========================================================================
# worktree creation + listing
# ===========================================================================


def test_create_worktree_outside_repo(fake_repo: Path, tmp_path: Path):
    """create_worktree makes a linked worktree OUTSIDE the repo and returns the
    base commit (the commit the worktree is created from)."""
    worktree_path = tmp_path / "wt_outside"
    base = gitops.create_worktree(
        target_repo=fake_repo,
        worktree_path=worktree_path,
        branch="agentflow/test-run",
    )
    # worktree was actually created and lives outside the main repo
    assert worktree_path.is_dir()
    assert gitops.is_git_repo(worktree_path)
    # the returned base commit is the repo's HEAD at creation time
    assert base == gitops.head(fake_repo)
    # the worktree shares the repo's content (README.md visible)
    assert (worktree_path / "README.md").exists()


def test_worktree_list(fake_repo: Path, tmp_path: Path):
    """worktree_list enumerates the main + linked worktrees (used by init
    recovery, Task 3.7)."""
    wt1 = tmp_path / "wt1"
    gitops.create_worktree(target_repo=fake_repo, worktree_path=wt1,
                           branch="agentflow/r1")
    wts = gitops.worktree_list(fake_repo)
    # at least the main worktree + the one we just created
    paths = {str(Path(w["path"]).resolve()) if isinstance(w.get("path"), str) else None
             for w in wts}
    assert any(str(fake_repo.resolve()) in (p or "") for p in paths)
    assert any(str(wt1.resolve()) in (p or "") for p in paths)


# ===========================================================================
# branch_exists
# ===========================================================================


def test_branch_exists(fake_repo: Path, tmp_path: Path):
    gitops.create_worktree(target_repo=fake_repo, worktree_path=tmp_path / "wt",
                           branch="agentflow/exists")
    assert gitops.branch_exists(fake_repo, "agentflow/exists") is True
    assert gitops.branch_exists(fake_repo, "agentflow/does-not-exist") is False


# ===========================================================================
# diff_patch
# ===========================================================================


def test_diff_patch(fake_repo: Path):
    """diff_patch returns a unified-diff string for base..head, citing changed
    content (used for the Review Bundle)."""
    base = gitops.head(fake_repo)
    (fake_repo / "README.md").write_text("changed content\n", encoding="utf-8")
    head = _commit(fake_repo, "edit README")

    patch = gitops.diff_patch(fake_repo, base, head)
    assert isinstance(patch, str)
    assert "changed content" in patch
    # it's a real diff: contains the file path
    assert "README.md" in patch


# ===========================================================================
# changed_files_z
# ===========================================================================


def test_changed_files_z(fake_repo: Path):
    """changed_files_z returns the NUL-separated file list (special chars safe)."""
    base = gitops.head(fake_repo)
    (fake_repo / "src.txt").write_text("a", encoding="utf-8")
    head = _commit(fake_repo, "add src")
    changed = gitops.changed_files_z(fake_repo, base, head)
    assert "src.txt" in changed


# ===========================================================================
# name_status_z — the tricky parser (A/D/M/Rxx with old+new paths)
# ===========================================================================


def test_name_status_z_added_modified_deleted(fake_repo: Path):
    """name_status_z parses A/M/D entries from --name-status -z output as
    ``(status, old_path, new_path)`` tuples.

    Slot semantics (the contract gitops consumers rely on):
      - Add    (A): the path is in head => (A, "", path)
      - Modify (M): the path is in both   => (M, "", path)
      - Delete (D): the path existed in base, gone in head => (D, path, "")
    A delete's path lives in the OLD slot (it is the base-side path that
    disappeared), mirroring how a rename carries its base-side path in `old`.
    """
    base = gitops.head(fake_repo)
    # add a file, modify README
    (fake_repo / "added.txt").write_text("new", encoding="utf-8")
    (fake_repo / "README.md").write_text("modified\n", encoding="utf-8")
    head = _commit(fake_repo, "add+modify")

    entries = gitops.name_status_z(fake_repo, base, head)
    by_new = {e[2]: e for e in entries}  # index adds/modifies by new path
    assert by_new["added.txt"][0] == "A"
    assert by_new["added.txt"][1] == ""  # no old path for an add
    assert by_new["README.md"][0] == "M"

    # now DELETE added.txt and confirm the D entry has the path in the OLD slot
    base2 = head
    (fake_repo / "added.txt").unlink()
    head2 = _commit(fake_repo, "delete added")
    entries2 = gitops.name_status_z(fake_repo, base2, head2)
    dels = [e for e in entries2 if e[0].startswith("D")]
    assert dels, f"expected a delete entry, got {entries2}"
    status, old, new = dels[0]
    assert status == "D"
    assert old == "added.txt", (
        f"delete path must be in the OLD slot, got old={old!r} new={new!r}"
    )
    assert new == "", "a delete has no new path"


def test_name_status_z_rename(fake_repo: Path):
    """name_status_z parses a RENAME (Rxx) entry: status Rxxx, OLD path, NEW path.
    Git emits R<score> with the old path and the new path as two separate -z
    fields. The parser must capture both."""
    base = gitops.head(fake_repo)
    # configure git to detect this small rename reliably
    _git("config", "diff.renames", "true", cwd=fake_repo)
    _git("mv", "README.md", "RENAMED.md", cwd=fake_repo)
    head = _commit(fake_repo, "rename README -> RENAMED")

    entries = gitops.name_status_z(fake_repo, base, head)
    renames = [e for e in entries if e[0].startswith("R")]
    assert renames, f"expected a rename entry, got {entries}"
    status, old, new = renames[0]
    assert status.startswith("R")
    assert old == "README.md"
    assert new == "RENAMED.md"


def test_name_status_z_empty_when_no_changes(fake_repo: Path):
    base = gitops.head(fake_repo)
    head = base  # same commit
    assert gitops.name_status_z(fake_repo, base, head) == []


def test_name_status_z_raises_on_truncated_rename(fake_repo: Path):
    """If git -z output were truncated mid-record (a status requiring two paths
    with fewer than two path tokens left), the parser raises GitError rather
    than throwing a raw IndexError or silently producing a malformed entry."""
    from unittest import mock

    base = gitops.head(fake_repo)
    (fake_repo / "x.txt").write_text("x", encoding="utf-8")
    head = _commit(fake_repo, "add x")

    # simulate truncated output: a rename status with only ONE following path
    truncated = "R100\0old_only".encode("utf-8")
    with mock.patch.object(gitops, "_run", return_value=truncated):
        with pytest.raises(gitops.GitError):
            gitops.name_status_z(fake_repo, base, head)


def test_name_status_z_raises_on_truncated_single_path(fake_repo: Path):
    """A one-path status (A/M/D) with NO following path token raises GitError."""
    from unittest import mock

    base = gitops.head(fake_repo)
    truncated = "M".encode("utf-8")  # status with no path after it
    with mock.patch.object(gitops, "_run", return_value=truncated):
        with pytest.raises(gitops.GitError):
            gitops.name_status_z(fake_repo, base, gitops.head(fake_repo))


# ===========================================================================
# special filenames: spaces, quotes, unicode
# ===========================================================================


def test_filename_with_spaces_and_unicode(fake_repo: Path):
    """Files with spaces, parens, and unicode in their names must parse correctly
    via -z (NOT split on spaces or mangled by quoting). This is why we use -z
    everywhere instead of line/whitespace splitting.

    Note: a literal double-quote (``"``) is a Windows-reserved filename char
    (AGENTS.md G3) and CANNOT exist on disk on Windows, so we test the chars
    that are special for SHELL/whitespace parsing but legal on every platform
    (spaces, parentheses, leading dots) plus a CJK unicode name. On non-Windows
    we additionally exercise a double-quote name to cover that case too.
    """
    import sys

    base = gitops.head(fake_repo)
    names = [
        "weird name with spaces.txt",   # spaces — would break whitespace split
        "(parens) file.txt",             # parens — shell-special
        "中文.txt",                       # CJK unicode (multibyte)
    ]
    if not sys.platform.startswith("win"):
        # double-quote is legal on POSIX; include it there (Windows can't create it)
        names.append('weird "name".txt')
    for n in names:
        (fake_repo / n).write_text("content", encoding="utf-8")
    head = _commit(fake_repo, "special names")

    changed = gitops.changed_files_z(fake_repo, base, head)
    for n in names:
        assert n in changed, f"special name lost in changed_files_z: {n!r} (got {changed})"

    entries = gitops.name_status_z(fake_repo, base, head)
    by_new = {e[2]: e for e in entries}
    for n in names:
        assert n in by_new, f"special name lost in name_status_z: {n!r}"
        assert by_new[n][0] == "A", f"expected A for {n!r}, got {by_new[n]}"


def test_changed_files_z_and_name_status_z_handle_nested_dirs(fake_repo: Path):
    """A file in a nested dir path parses correctly (no path component dropped)."""
    base = gitops.head(fake_repo)
    nested = fake_repo / "src" / "deep" / "module.cpp"
    nested.parent.mkdir(parents=True)
    nested.write_text("code", encoding="utf-8")
    head = _commit(fake_repo, "nested file")
    changed = gitops.changed_files_z(fake_repo, base, head)
    assert "src/deep/module.cpp" in changed
    entries = gitops.name_status_z(fake_repo, base, head)
    assert any(e[2] == "src/deep/module.cpp" and e[0] == "A" for e in entries)


# ===========================================================================
# git_path (resolves the .git path; for MERGE_HEAD/rebase detection)
# ===========================================================================


def test_git_path(fake_repo: Path):
    """git_path resolves the repository's .git location (directory in a normal
    repo). Used by preflight to detect MERGE_HEAD / rebase in progress."""
    gp = gitops.git_path(fake_repo)
    assert isinstance(gp, Path)
    # in a plain repo it's <repo>/.git
    assert gp.name == ".git" or (gp / "HEAD").exists()


def test_git_path_on_a_subdirectory(fake_repo: Path):
    """git_path resolves correctly from a subdirectory of the repo."""
    sub = fake_repo / "subdir"
    sub.mkdir()
    gp = gitops.git_path(sub)
    # resolves back to the repo's .git
    assert (gp / "HEAD").exists()
