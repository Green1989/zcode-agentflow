"""Git primitives — the single, fully-tested git module (Task 2.6; P0-2).

ALL git operations in the project live here, consumed by store / adapters /
engine / policy_change. No other module shells out to git. Everything goes
through ``subprocess.run(["git", ...])`` with ``capture_output`` and UTF-8.

Why a dedicated module (P0-2): the v2-review gap was that git calls were
scattered and the trickiest one — ``--name-status -z`` parsing of A/D/M/Rxx/Cxx
with old+new paths and special filenames — had no task and no tests. This module
centralizes that logic so it is correct ONCE.

Key correctness rules:
  - every list-emitting query uses ``-z`` (NUL-separated) so filenames with
    spaces, quotes, and unicode survive intact (NEVER split on whitespace);
  - ``--name-status -z`` records are variable-length: A/M/D/T/U carry ONE path
    field; Rxx/Cxx carry TWO (old, new). The parser reads the status char to
    decide how many paths follow.
  - diff_patch / changed_files_z / name_status_z all operate on a base..head
    commit range; rename detection (-M) is enabled for name-status so renames
    are reported as Rxx rather than D+A.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = [
    "GitError",
    "is_git_repo",
    "head",
    "is_clean",
    "untracked_files",
    "create_worktree",
    "worktree_list",
    "branch_exists",
    "diff_patch",
    "changed_files_z",
    "name_status_z",
    "git_path",
]

# status codes that carry TWO path fields in --name-status -z output (old, new).
# Everything else (A, M, D, T, U, X, B) carries ONE path field.
_TWO_PATH_STATUSES = {"R", "C"}


class GitError(RuntimeError):
    """Raised when a git command fails or produces unexpected output."""


def _run(args, *, cwd, check: bool = True) -> bytes:
    """Run ``git <args>`` in ``cwd`` and return its raw stdout bytes.

    Uses capture_output (never lets git write to the parent stdout — important
    for the MCP server's clean-stdout invariant, P1-6). stdin is always
    DEVNULL so git never inherits the MCP protocol pipe (a credential helper
    or pager reading stdin would hang the server forever under stdio transport).
    On failure raises GitError with the stderr message (unless check=False).
    """
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if check and result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed (exit {result.returncode}): {err}")
    return result.stdout


def is_git_repo(path) -> bool:
    """True iff ``path`` is inside a git repository (work-tree or .git present)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path), capture_output=True, stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def head(repo) -> str:
    """Return the full 40-hex HEAD commit hash of ``repo``."""
    out = _run(["rev-parse", "HEAD"], cwd=repo)
    return out.decode("utf-8").strip()


def is_clean(repo) -> bool:
    """True iff the working tree has no staged or unstaged changes and no
    untracked files (``git status --porcelain`` is empty)."""
    out = _run(["status", "--porcelain"], cwd=repo)
    return out.strip() == b""


def untracked_files(repo) -> list[str]:
    """List untracked files (NUL-separated via -z so special names survive)."""
    out = _run(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo)
    return [p for p in out.decode("utf-8").split("\0") if p]


def create_worktree(*, target_repo, worktree_path, branch) -> str:
    """Create a linked worktree at ``worktree_path`` (OUTSIDE the repo) on a new
    ``branch`` off the current HEAD. Returns the base commit (the commit the
    worktree is created from = repo HEAD at call time).

    Uses ``git worktree add -b <branch> <path>``. The caller (Task 3.7) owns
    path allocation and recovery; this is the primitive.
    """
    target_repo = Path(target_repo)
    worktree_path = Path(worktree_path)
    base = head(target_repo)
    # -b creates the branch at HEAD; the worktree is checked out there.
    _run(["worktree", "add", "-b", branch, str(worktree_path)], cwd=target_repo)
    return base


def worktree_list(target_repo) -> list[dict]:
    """Enumerate worktrees as ``git worktree list --porcelain`` records.

    Returns a list of dicts (one per worktree) with keys ``worktree`` (path),
    ``HEAD`` (commit), ``branch`` (ref, if set), ``bare`` (if set), plus any
    other porcelain fields present. Used by init recovery (Task 3.7).
    """
    out = _run(["worktree", "list", "--porcelain"], cwd=target_repo)
    records: list[dict] = []
    current: dict = {}
    for line in out.decode("utf-8").splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        parts = line.split(" ", 1)
        key = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        # normalize the path field to a stable key
        if key == "worktree":
            current["path"] = value
        else:
            current[key] = value
    if current:
        records.append(current)
    return records


def branch_exists(target_repo, branch: str) -> bool:
    """True iff ``branch`` exists in ``target_repo`` (local refs)."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(target_repo), capture_output=True, stdin=subprocess.DEVNULL,
    )
    return result.returncode == 0


def diff_patch(repo, base: str, head: str) -> str:
    """Return the unified-diff patch string for ``base..head`` (for the Review
    Bundle). UTF-8 decoded."""
    out = _run(["diff", f"{base}..{head}"], cwd=repo)
    return out.decode("utf-8")


def changed_files_z(repo, base: str, head: str) -> list[str]:
    """Return the list of files changed in ``base..head`` (NUL-separated via -z)."""
    out = _run(["diff", "--name-only", "-z", f"{base}..{head}"], cwd=repo)
    return [p for p in out.decode("utf-8").split("\0") if p]


def name_status_z(repo, base: str, head: str) -> list[tuple[str, str, str]]:
    """Parse ``git diff --name-status -z -M base..head`` into
    ``(status, old_path, new_path)`` tuples.

    -M enables rename detection so a rename is reported as ``Rxx`` with old+new
    paths rather than as a D + A pair.

    ``--name-status -z`` emits NUL-separated records, but records are
    VARIABLE-LENGTH: A/M/D/T/U carry one path field; Rxx/Cxx carry two (old,
    new). The parser reads the status token's first char to decide how many
    paths follow. For an add/modify/delete, ``old_path`` is "" (and ``new_path``
    is "" for a delete). This keeps the (status, old, new) shape uniform.
    """
    out = _run(["diff", "--name-status", "-z", "-M", f"{base}..{head}"], cwd=repo)
    tokens = out.decode("utf-8").split("\0")
    # drop the trailing empty token produced by the final NUL
    if tokens and tokens[-1] == "":
        tokens.pop()

    entries: list[tuple[str, str, str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if not status:
            # a leading/stray empty token (shouldn't happen with -z); skip safely
            continue
        kind = status[0]
        if kind in _TWO_PATH_STATUSES:
            # Rxx/Cxx: TWO path fields (old, new). Need both to be present.
            if i + 2 > len(tokens):
                raise GitError(
                    f"truncated --name-status -z record: status {status!r} "
                    f"expected two paths"
                )
            old_path = tokens[i]
            new_path = tokens[i + 1]
            i += 2
            entries.append((status, old_path, new_path))
        else:
            # ONE path field (A/M/D/T/U/X/B). Need it to be present.
            if i + 1 > len(tokens):
                raise GitError(
                    f"truncated --name-status -z record: status {status!r} "
                    f"expected one path"
                )
            path = tokens[i]
            i += 1
            if kind == "D":
                # Delete: the path existed in base and is gone in head — it is
                # the OLD path; the new path is empty.
                entries.append((status, path, ""))
            else:
                # Add/modify/etc.: the path is in head (new); old path is empty.
                entries.append((status, "", path))
    return entries


def git_path(repo) -> Path:
    """Resolve the repository's ``.git`` location as a directory path.

    In a normal worktree this is ``<repo>/.git``; in a linked worktree or bare
    repo it is wherever ``git rev-parse --git-dir`` points. Used by preflight to
    detect an in-progress merge/rebase (MERGE_HEAD / rebase-merge presence).
    """
    out = _run(["rev-parse", "--git-dir"], cwd=repo)
    git_dir = out.decode("utf-8").strip()
    p = Path(git_dir)
    if not p.is_absolute():
        p = (Path(repo) / p).resolve()
    return p
