"""Task 1.1 — packaging / install / pinned-dep guards.

These tests are the P1-7 drift guard: they assert the package installs from a
src layout, the *official* mcp SDK is importable (no local shadow), and the
installed dependency versions match the versions frozen in the Phase 0 spike
(docs/capabilities.json + requirements.lock).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_local_mcp_shadow():
    """G2 trap: there must be NO directory named `mcp` at the repo root that
    would shadow the official SDK when the package is imported."""
    local_mcp = REPO_ROOT / "mcp"
    assert not local_mcp.exists(), f"local mcp/ shadow at {local_mcp} would shadow the SDK (G2)"


def test_official_mcp_sdk_imports():
    """The `mcp` module must resolve to the installed distribution under
    site-packages, NOT to a path inside this repo (G2 trap)."""
    spec = importlib.util.find_spec("mcp")
    assert spec is not None and spec.origin is not None, "mcp SDK not importable"
    origin = Path(spec.origin).resolve()
    repo = REPO_ROOT.resolve()
    # not inside the repo tree, and under site-packages (the installed package)
    assert repo not in origin.parents, (
        f"mcp resolves inside repo ({origin}); official SDK expected under site-packages"
    )
    assert "site-packages" in origin.parts or "dist-packages" in origin.parts, (
        f"mcp origin not under site-packages: {origin}"
    )


def test_package_imports():
    from zcode_agentflow import __version__

    assert __version__ == "0.1.0"


def test_fastmcp_imports():
    """Import path recorded by the Phase 0 spike (capabilities.json
    versions.mcp_sdk_import_path)."""
    from mcp.server.fastmcp import FastMCP  # noqa: F401


def test_deps_match_lock(capabilities: dict):
    """P1-7 drift guard: installed mcp / portalocker versions must equal the
    versions the Phase 0 spike validated (docs/capabilities.json)."""
    expected = capabilities["versions"]
    dists = {d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()}

    mcp_installed = dists.get("mcp")
    assert mcp_installed == expected["mcp_sdk"], (
        f"installed mcp=={mcp_installed} != capabilities.json mcp_sdk=={expected['mcp_sdk']}"
    )

    portalocker_installed = dists.get("portalocker")
    assert portalocker_installed == expected["portalocker"], (
        f"installed portalocker=={portalocker_installed} != "
        f"capabilities.json portalocker=={expected['portalocker']}"
    )
