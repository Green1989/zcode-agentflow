"""Open-source release hardening — Task 4: CI + security automation config.

These tests validate the GitHub Actions / Dependabot / secret-scanning config
that ships with the repo. They assert the config EXISTS and matches the release
contract (Windows + the validated Python versions, lock-driven install, no real
model-CLI smoke, Dependabot covering pip + actions, secret scanning with
SHA-pinned third-party actions, no floating tags).

They do NOT run the workflows (that is GitHub's job); they parse the YAML to
catch config drift before it reaches a PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"


# ---------------------------------------------------------------------------
# CI workflow exists and matches the release contract
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    assert path.is_file(), f"missing: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_ci_workflow_exists():
    """A GitHub Actions workflow file ships under .github/workflows/."""
    assert WORKFLOWS.is_dir(), ".github/workflows/ directory missing"
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    assert ymls, "no workflow files under .github/workflows/"


def test_ci_runs_on_windows():
    """The primary platform is Windows (AGENTS.md: 'Windows / Git Bash'). The
    workflow must include a windows-latest runner."""
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    found_windows = False
    for wf in ymls:
        text = wf.read_text(encoding="utf-8")
        if "windows-latest" in text:
            found_windows = True
            break
    assert found_windows, "no windows-latest runner in any workflow"


def test_ci_tests_python_311_and_313():
    """The validated versions are 3.11 (minimum) and 3.13 (the spike env). CI
    must cover both so a regression on either is caught."""
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    text = "\n".join(w.read_text(encoding="utf-8") for w in ymls)
    assert '"3.11"' in text or "'3.11'" in text or "3.11" in text, (
        "CI must test Python 3.11 (the declared minimum)"
    )
    assert '"3.13"' in text or "'3.13'" in text or "3.13" in text, (
        "CI must test Python 3.13 (the validated spike version)"
    )


def test_ci_uses_lock_driven_install():
    """The install must follow the lock-driven contract: requirements.lock
    first, then the package with --no-deps. This is the install procedure the
    validated versions were tested under."""
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    text = "\n".join(w.read_text(encoding="utf-8") for w in ymls)
    assert "requirements.lock" in text, (
        "CI must install from requirements.lock (the validated version freeze)"
    )
    assert "--no-deps" in text, (
        "CI must install the package with --no-deps (lock-driven contract)"
    )


def test_ci_runs_pytest_default_suite():
    """CI runs the default pytest suite (the same `python -m pytest -q` that is
    green locally). The smoke tests are excluded by pyproject addopts and must
    NOT be invoked in CI."""
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    text = "\n".join(w.read_text(encoding="utf-8") for w in ymls)
    assert "pytest" in text, "CI must run pytest"
    # the smoke harness (real model CLIs) must NOT run in CI
    assert "AGENTFLOW_SMOKE=1" not in text and "run_local_smoke" not in text, (
        "CI must NOT run the real-CLI smoke (it spends tokens + needs login); "
        "smoke stays manual opt-in only"
    )


# ---------------------------------------------------------------------------
# third-party actions are SHA-pinned (no floating tags)
# ---------------------------------------------------------------------------


def test_third_party_actions_are_sha_pinned():
    """Any third-party GitHub Action used (e.g. gitleaks) must be pinned to a
    full commit SHA, not a floating tag like @v2. Floating tags can be
    repointed by a compromised maintainer (supply-chain risk). Uses of
    actions/checkout, actions/setup-python etc. are also expected to be
    SHA-pinned for a release-hardened repo."""
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    for wf in ymls:
        text = wf.read_text(encoding="utf-8")
        # find every `uses:` directive
        for m in re.finditer(r'uses:\s*(\S+)', text):
            ref = m.group(1)
            # skip local actions (./...)
            if ref.startswith("./"):
                continue
            # the version part after @ must be a 40-char hex SHA, not a tag
            at = ref.rfind("@")
            version = ref[at + 1:] if at != -1 else ""
            assert re.fullmatch(r"[0-9a-f]{40}", version), (
                f"action {ref!r} in {wf.name} is not SHA-pinned (expected a "
                f"40-char commit SHA after @, got {version!r}). Pin third-party "
                f"actions to a SHA, not a floating tag."
            )


# ---------------------------------------------------------------------------
# Dependabot covers pip + GitHub Actions
# ---------------------------------------------------------------------------


def test_dependabot_config_exists():
    """Dependabot config ships so dependency + action updates are tracked."""
    assert DEPENDABOT.is_file(), ".github/dependabot.yml missing"


def test_dependabot_covers_pip_and_actions():
    """Dependabot must watch both pip (requirements.lock / pyproject) and
    github-actions (the workflow files)."""
    cfg = _load_yaml(DEPENDABOT)
    ecosystems = set()
    for entry in cfg.get("updates", []):
        # the dependabot config key is `package-ecosystem`
        ecosystems.add(entry.get("package-ecosystem"))
    assert "pip" in ecosystems, "Dependabot must cover pip"
    assert "github-actions" in ecosystems, "Dependabot must cover github-actions"


# ---------------------------------------------------------------------------
# secret scanning config
# ---------------------------------------------------------------------------


def test_secret_scanning_workflow_exists():
    """A secret-scanning workflow (gitleaks or equivalent) ships so leaked
    credentials are caught in CI, not after a public release."""
    ymls = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    text = "\n".join(w.read_text(encoding="utf-8") for w in ymls)
    # gitleaks is the chosen scanner; its action reference must appear
    assert "gitleaks" in text.lower(), (
        "a gitleaks (or equivalent) secret-scan step must be present in CI"
    )
