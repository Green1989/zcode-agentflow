"""Shared pytest fixtures for the zcode_agentflow test suite.

Per Task 1.1 these provide the four-roots-style temp dirs and the canonical
Phase 0 findings loader. Later tasks extend conftest with engine/store fixtures.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# Repo root (the directory containing pyproject.toml). pytest is launched from
# the repo root, so cwd is a stable anchor; resolve() keeps it absolute.
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def control_root(tmp_path: Path) -> Path:
    """A repo-external directory holding AgentFlow state/artifacts (read-write)."""
    d = tmp_path / "control_root"
    d.mkdir()
    return d


@pytest.fixture()
def worktree_root(tmp_path: Path) -> Path:
    """A repo-external directory holding the run's writable code worktree."""
    d = tmp_path / "worktree_root"
    d.mkdir()
    return d


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """A real minimal git repo with one committed file (for git-backed tests)."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    (repo / "README.md").write_text("sample repo\n", encoding="utf-8")
    env_min = {"PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env_min)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, env=env_min
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, env=env_min)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env_min)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env_min
    )
    return repo


@pytest.fixture()
def capabilities() -> dict:
    """Load docs/capabilities.json — the Phase 0 factual source of truth (§5)."""
    path = REPO_ROOT / "docs" / "capabilities.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
