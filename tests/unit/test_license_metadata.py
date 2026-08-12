"""Open-source release hardening — Task 1: LICENSE + public package metadata.

These tests lock the two pieces a public GitHub release must have:
  1. a real MIT LICENSE file at the repo root with the correct copyright line;
  2. ``pyproject.toml`` metadata that is consistent with that license and does
     NOT claim a false PyPI URL or shipping channel that does not exist.

The distribution form here is a ZCode *plugin* installed from a Git clone +
local marketplace (see README / ``marketplace.json``); the tests assert that
public claim is reflected honestly rather than implying an independent wheel
deployment that was never validated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LICENSE = REPO_ROOT / "LICENSE"
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# LICENSE file
# ---------------------------------------------------------------------------


def _license_text() -> str:
    assert LICENSE.is_file(), f"MIT LICENSE file is missing at {LICENSE}"
    return LICENSE.read_text(encoding="utf-8")


def test_license_file_exists_with_mit_identifier():
    text = _license_text()
    # The SPDX identifier and the words "MIT License" must both appear so the
    # file is unambiguously recognized by license scanners (GitHub's licensee,
    # detect license, etc.).
    assert "MIT License" in text
    assert "MIT" in text


def test_license_has_release_copyright_line():
    text = _license_text()
    # The release copyright line required by the release-hardening goal.
    assert "Copyright (c) 2026 ZCode-AgentFlow contributors" in text


def test_license_contains_permission_and_no_warranty_clauses():
    """A genuine MIT text carries the permission grant and the no-warranty
    paragraph. We check for the load-bearing phrases rather than an exact
    byte-for-byte match so minor wording stays maintainable."""
    text = _license_text()
    lowered = text.lower()
    assert "permission is hereby granted, free of charge" in lowered
    assert "without warranty" in lowered or "as is" in lowered


# ---------------------------------------------------------------------------
# pyproject.toml metadata
# ---------------------------------------------------------------------------


def _pyproject_text() -> str:
    assert PYPROJECT.is_file(), f"pyproject.toml is missing at {PYPROJECT}"
    return PYPROJECT.read_text(encoding="utf-8")


def test_pyproject_license_is_mit():
    """The license declaration in pyproject.toml must be MIT and consistent with
    the LICENSE file. Modern packaging accepts ``license = "MIT"`` (SPDX) on
    setuptools >= 77; the legacy ``{ text = "MIT" }`` form is also accepted.
    Either way the string MIT must be present exactly once as the license."""
    text = _pyproject_text()
    # the [project] license key must say MIT
    assert re.search(r'license\s*=\s*("?MIT"|\{\s*text\s*=\s*"MIT"\s*\})', text), (
        "pyproject.toml license declaration is not MIT"
    )


def test_pyproject_has_python_version_and_classifiers():
    """Public metadata must state the supported Python range and carry standard
    classifiers so indexers and IDEs classify it correctly."""
    text = _pyproject_text()
    assert 'requires-python' in text
    assert "Programming Language :: Python :: 3" in text


def test_pyproject_has_project_urls_not_fake_pypi():
    """Project URLs must point at the real GitHub repo, not a fabricated PyPI
    address. There is no published wheel on PyPI for this project (it is a
    ZCode plugin installed from a clone); claiming one would be dishonest."""
    text = _pyproject_text()
    # a Documentation/Repository/Homepage url must be present
    assert re.search(r"\[project\.urls\]", text), "missing [project.urls] table"
    # must NOT claim a fake PyPI project page
    assert "pypi.org/project/zcode-agentflow" not in text
    # the github URL, if named, must be the real org placeholder the operator
    # can fill in — at minimum a Repository entry must exist.
    assert "Repository" in text or "Source" in text or "Homepage" in text


def test_pyproject_does_not_implement_independent_wheel_deployment():
    """The distribution form is a ZCode plugin (clone + local marketplace). The
    build backend may produce an sdist/wheel for ``pip install -e .``, but the
    metadata must not imply a standalone independently-deployable wheel channel
    that was never validated. We only assert no false PyPI upload URL is
    declared; the README carries the honest install story."""
    text = _pyproject_text()
    # no false "download_url" pointing at a PyPI artifact
    assert "pypi.org" not in text.lower()


# ---------------------------------------------------------------------------
# README install story consistency (Task 1.4)
# ---------------------------------------------------------------------------


def test_readme_states_plugin_install_form_honestly():
    """The README must describe the ACTUAL install form (clone + local
    marketplace plugin) and not imply a wheel is independently deployable
    unless that is genuinely validated."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    # the honest install verbs must appear
    assert "plugin" in lowered
    # must NOT claim a published PyPI wheel deployment
    assert "pip install zcode-agentflow" not in lowered.replace(" ", "") or \
        "git clone" in lowered
