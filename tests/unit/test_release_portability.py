"""Open-source release hardening — Task 2: machine-specific config removal.

A public, cross-machine release must NOT bake in any single operator's local
environment. These tests enforce the portability rules:

  1. No tracked file hard-codes the developer's local proxy address as a
     *default* the product ships with.
  2. No tracked file hard-codes the developer's local repo absolute path.
  3. The production ``config.yaml`` loads cleanly with NO proxy configured.
  4. When a user DOES configure a proxy in ``config.yaml``, the Grok adapter
     injects it into the subprocess env (G7/G8 contract preserved), AND when
     the user does NOT, nothing is injected.

Detector construction (no literal values in this file):
  The detector constants below are assembled from FRAGMENTS at runtime so that
  this test file does not itself contain the exact literal strings it scans
  for. This means the scanner can scan ALL tracked files — **including this
  very file** — without a tautological self-match, and without an exclusion
  that would hide a real leak introduced into this file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# The developer's local environment specifics that must NOT ship as defaults.
# Assembled from fragments so THIS file does not contain the exact literal
# (otherwise the scanner would have to exclude itself, hiding any leak added
# here later). The proxy is built from numeric fragments; the repo-path regex
# is built so no single literal is the full absolute checkout path.
_LOCAL_PROXY = "127" + "." + "0" + "." + "0" + "." + "1" + ":" + "7897"
# match the developer's local repo absolute path (drive-letter + repo dir,
# any slash/case). Fragmented so the full path is not a single literal here.
_LOCAL_PATH_RE = re.compile("D" + ":[\\\\/]" + "[Zz]Code[\\\\/]" + "zcode-agentflow")


# ---------------------------------------------------------------------------
# 1. no hard-coded local proxy / local repo path in ANY tracked file
#    (the scanner scans every tracked text file, including this one — there is
#    no self-exclusion, because the detector literals are fragmented above)
# ---------------------------------------------------------------------------


def _tracked_text_files() -> list[Path]:
    """All text files under version control whose content could carry a
    hard-coded local address. This includes BOTH committed (tracked) files AND
    untracked-but-not-ignored files (i.e. new files staged or about to be
    committed), via ``git ls-files -z --cached --others --exclude-standard``.

    The ``-z`` flag makes git emit paths separated by NUL bytes with NO quoting
    and NO octal escaping. Plain (non-z) git output escapes non-ASCII path
    components (e.g. the Chinese-named ``00-使用说明.md`` becomes
    ``"00-\\344\\275\\277..."`` with octal escapes and surrounding quotes), so a
    line-based parser would build a Path that does not exist and silently skip
    the file — a real blind spot. NUL-split parsing handles every path.

    This is deliberate: a scanner that only sees committed files misses a leak
    in a new file until AFTER it is committed, at which point the green suite
    was a lie. Properly gitignored artifacts (spike outputs, control_root,
    runtime state) remain excluded by ``--exclude-standard``."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    )
    files = []
    # split on NUL; git -z emits raw bytes (utf-8 on this repo) with no quoting.
    for raw in out.stdout.split(b"\x00"):
        if not raw:
            continue
        line = raw.decode("utf-8", errors="surrogateescape")
        p = REPO_ROOT / line
        if p.suffix.lower() in {".py", ".md", ".yaml", ".yml", ".json", ".toml",
                                ".txt", ".template", ".cfg", ".ini"} or line in {
                                "config.yaml", ".gitignore"}:
            files.append(p)
    return files


def test_scanner_enumerates_non_ascii_paths():
    """Regression guard: ``_tracked_text_files`` must enumerate files with
    non-ASCII path components. Plain (non -z) ``git ls-files`` output escapes
    such paths (e.g. ``00-使用说明.md`` becomes ``"00-\\344\\275\\277..."`` with
    octal escapes and quotes), so a line-based parser would build a non-existent
    Path and silently skip the file — a real blind spot that previously let a
    leak in a Chinese-named file evade the scanner. The repo ships exactly one
    such tracked markdown (the execution-prompt usage note); it MUST appear in
    the scan list by its real (unescaped) name."""
    scanned = {p.name for p in _tracked_text_files()}
    # the real Chinese-named doc that ships in this repo
    assert "00-使用说明.md" in scanned, (
        "scanner is not enumerating non-ASCII paths; the Chinese-named "
        "00-使用说明.md is missing — git output is likely being parsed as "
        "escaped text instead of via NUL-split (-z). This blind spot would let "
        "a leak in any non-ASCII-named file evade the scanner."
    )


def test_no_hardcoded_local_proxy_in_tracked_files():
    """The developer's local proxy must not appear as a shipped default in ANY
    tracked text file — including this test file (the detector is fragmented
    so this file is clean too). There is NO per-file exception: capabilities.json
    no longer carries the literal address either (it was generalized in this
    hardening pass), so no file may contain it.

    Decode/read failures are NOT silently skipped: they are collected and
    reported, so a binary-disguised-as-text file cannot evade the scan by
    raising UnicodeDecodeError. (test_all_scanned_text_files_are_readable
    independently asserts zero unread files.)"""
    hits = []
    unread = []
    for p in _tracked_text_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            unread.append(f"{p.relative_to(REPO_ROOT)} ({type(exc).__name__})")
            continue
        if _LOCAL_PROXY in text:
            hits.append(str(p.relative_to(REPO_ROOT)))
    # unread files are a scanning failure, not a pass — surface them loudly
    assert not unread, (
        "scanner could not read these text files (a decode/read failure must "
        "not silently skip a file): " + ", ".join(unread)
    )
    assert not hits, (
        "hard-coded local proxy appears as a shipped value in: "
        + ", ".join(hits)
    )


def test_config_yaml_has_no_default_proxy():
    """config.yaml must NOT ship with a live proxy value under reviewer.proxy."""
    cfg_path = REPO_ROOT / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    assert _LOCAL_PROXY not in text, (
        "config.yaml ships a live local proxy address; it must be removed or "
        "commented as an example"
    )


def test_plugin_manifest_has_no_hardcoded_proxy():
    """The plugin manifest env must NOT inject a hard-coded local proxy."""
    plugin = (REPO_ROOT / ".zcode-plugin" / "plugin.json").read_text(encoding="utf-8")
    assert _LOCAL_PROXY not in plugin, (
        "plugin.json env ships a hard-coded local proxy"
    )


def test_no_local_repo_absolute_path_in_tracked_files():
    """The developer's checkout path must not appear in any tracked file.
    Decode/read failures are surfaced (not silently skipped) — see the proxy
    scan test for the rationale."""
    hits = []
    unread = []
    for p in _tracked_text_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            unread.append(f"{p.relative_to(REPO_ROOT)} ({type(exc).__name__})")
            continue
        if _LOCAL_PATH_RE.search(text):
            hits.append(str(p.relative_to(REPO_ROOT)))
    assert not unread, (
        "scanner could not read these text files: " + ", ".join(unread)
    )
    assert not hits, (
        "local repo absolute path appears in: " + ", ".join(hits)
    )


# ---------------------------------------------------------------------------
# 2. the production config loads with NO proxy, and WITH a user proxy
# ---------------------------------------------------------------------------


def test_production_config_loads_without_proxy(tmp_path):
    """Removing reviewer.proxy entirely must still produce a valid config."""
    from zcode_agentflow import config as config_mod
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.get("reviewer", {}).pop("proxy", None)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    loaded = config_mod.load(p)
    assert "proxy" not in loaded.get("reviewer", {})


def test_production_config_loads_with_user_proxy(tmp_path):
    """When a user sets a proxy, the config loads with it (G7/G8 path)."""
    from zcode_agentflow import config as config_mod
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("reviewer", {})["proxy"] = {
        "HTTPS_PROXY": "http://user-proxy.example:8080",
        "HTTP_PROXY": "http://user-proxy.example:8080",
        "ALL_PROXY": "http://user-proxy.example:8080",
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    loaded = config_mod.load(p)
    assert loaded["reviewer"]["proxy"]["HTTPS_PROXY"] == \
        "http://user-proxy.example:8080"


# ---------------------------------------------------------------------------
# 3. Grok adapter injects a USER-provided proxy AND skips it when absent
#    (observes the actual subprocess env via a fake grok that echoes it back)
# ---------------------------------------------------------------------------


_VALID_DECISION = (
    '{"decision":"PASS","spec_compliance":true,"plan_compliance":true,'
    '"tests_sufficient":true,"issues":[],"replan_required":false}'
)


def _make_fake_grok(tmp_path: Path) -> str:
    """A fake grok script that emits a valid envelope AND echoes the proxy env
    vars it received into a sidecar file, so the test can OBSERVE what the
    adapter actually injected into the child env."""
    toolbin = tmp_path / "toolbin"
    toolbin.mkdir(exist_ok=True)
    marker = tmp_path / "child_env.txt"
    # the script writes HTTPS_PROXY/HTTP_PROXY/ALL_PROXY (the 3 the adapter may
    # set) to the marker, then prints the envelope.
    write_line = f'echo HTTPS_PROXY=%HTTPS_PROXY% HTTP_PROXY=%HTTP_PROXY% ALL_PROXY=%ALL_PROXY% > "{marker}"'
    if sys.platform == "win32":
        fake = toolbin / "fake_grok.cmd"
        fake.write_text(
            f"@echo off\r\n{write_line}\r\n"
            f'echo {{"structuredOutput": {_VALID_DECISION}}}\r\n',
            encoding="utf-8",
        )
        return str(fake)
    else:
        fake = toolbin / "fake_grok.sh"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "HTTPS_PROXY=$HTTPS_PROXY HTTP_PROXY=$HTTP_PROXY ALL_PROXY=$ALL_PROXY" > "{marker}"\n'
            f"echo '{{\"structuredOutput\": {_VALID_DECISION}}}'\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return str(fake)


def _make_clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "")}
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    return repo


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("SPEC.md", "PLAN.md", "task-manifest.json", "execution-result.json",
                 "diff.patch", "review-request.json", "prompt.md"):
        (bundle / name).write_text("x\n", encoding="utf-8")
    return bundle


def test_grok_adapter_injects_user_configured_proxy(tmp_path):
    """The GrokAdapter must inject the user-configured proxy into the child
    subprocess env. This test OBSERVES the actual child env by running a fake
    grok that writes the received proxy vars to a sidecar file — it does not
    merely assert the call succeeded."""
    from zcode_agentflow.adapters.grok import GrokAdapter
    marker = tmp_path / "child_env.txt"
    argv0 = _make_fake_grok(tmp_path)
    repo = _make_clean_repo(tmp_path)
    bundle = _make_bundle(tmp_path)
    schema = REPO_ROOT / "schemas" / "review-decision.schema.json"
    adapter = GrokAdapter(command=argv0)
    request = {
        "bundle_dir": str(bundle), "repo_root": str(repo),
        "schema_path": str(schema),
        "proxy": {"HTTPS_PROXY": "http://user-proxy.example:8080",
                  "HTTP_PROXY": "http://user-proxy.example:8080",
                  "ALL_PROXY": "http://user-proxy.example:8080"},
    }
    result = adapter.invoke(request=request, repo_cwd=bundle,
                            operation_dir=tmp_path / "op", timeout=30)
    assert result.exit_code == 0
    # OBSERVE the child env: the marker must contain the user proxy
    child_env = marker.read_text(encoding="utf-8")
    assert "http://user-proxy.example:8080" in child_env, (
        f"adapter did not inject the user proxy into the child env; child saw: "
        f"{child_env!r}"
    )


def test_grok_adapter_no_proxy_when_user_does_not_configure(tmp_path, monkeypatch):
    """When the user does NOT configure a proxy, the adapter must NOT inject
    one. This test OBSERVES the child env via the same marker mechanism — the
    child must see NO proxy value from the adapter.

    Ambient proxy vars are scrubbed via ``monkeypatch.delenv`` so the test
    process env is restored after the test (never leaks a side effect into the
    rest of the suite)."""
    from zcode_agentflow.adapters.grok import GrokAdapter
    marker = tmp_path / "child_env.txt"
    argv0 = _make_fake_grok(tmp_path)
    repo = _make_clean_repo(tmp_path)
    bundle = _make_bundle(tmp_path)
    schema = REPO_ROOT / "schemas" / "review-decision.schema.json"
    adapter = GrokAdapter(command=argv0)
    # scrub any ambient proxy from the test process env so the only source is
    # the adapter's injection (os.environ copy + request proxy). monkeypatch
    # restores the originals after the test.
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    request = {
        "bundle_dir": str(bundle), "repo_root": str(repo),
        "schema_path": str(schema),
        "proxy": {},  # no proxy configured
    }
    result = adapter.invoke(request=request, repo_cwd=bundle,
                            operation_dir=tmp_path / "op", timeout=30)
    assert result.exit_code == 0
    child_env = marker.read_text(encoding="utf-8")
    # the three proxy vars must be EMPTY in the child (adapter injected nothing)
    assert "user-proxy" not in child_env
    # parse the marker: each var should be empty after the '='
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        # marker shape: VAR=value ...  — value must be empty
        m = re.search(rf"{var}=(\S*)", child_env)
        if m:
            assert m.group(1) == "", (
                f"adapter injected {var}={m.group(1)!r} when the user configured "
                f"no proxy; child env: {child_env!r}"
            )


# ---------------------------------------------------------------------------
# 4. redaction fixtures do not trip secret scanners (segmented construction)
# ---------------------------------------------------------------------------


def test_redact_fixtures_are_segmented_for_scanner_safety():
    """The redaction test fixtures must be constructed so no single literal
    looks like a real, high-entropy provider secret that GitHub Push Protection
    / gitleaks would block. This test asserts the fixture SOURCE does not
    contain any bare literal matching a real-credential regex shape.

    The fixtures in test_redact.py are SEGMENTED: each secret-shaped value is
    built by concatenation so no single string literal in the source is a
    plausible real credential. This test verifies that segmentation."""
    fixtures_src = (REPO_ROOT / "tests" / "unit" / "test_redact.py").read_text(
        encoding="utf-8")
    # A real OpenAI key shape in a SINGLE literal: sk-proj- + 40+ base62 chars.
    # If the fixtures were segmented (built from parts), no single literal
    # matches this. If a fixture stored the whole key as one string literal,
    # this would catch it.
    high_entropy_openai = re.compile(r'"sk-proj-[A-Za-z0-9_-]{40,}"')
    matches = high_entropy_openai.findall(fixtures_src)
    assert matches == [], (
        f"test_redact.py contains full-literal OpenAI key shapes {matches!r}; "
        f"segment the fixtures so no single string literal is a real key"
    )
    # same for xai, ghp, github_pat in single literals
    for pat in [r'"xai-[A-Za-z0-9]{20,}"', r'"ghp_[A-Za-z0-9]{30,}"',
                r'"github_pat_[A-Za-z0-9]{40,}"', r'"AKIA[A-Z0-9]{16}"']:
        hits = re.findall(pat, fixtures_src)
        assert hits == [], (
            f"test_redact.py contains full-literal secret shape {hits!r} for "
            f"pattern {pat}; segment the fixtures"
        )
    # PEM private-key armor markers are themselves scanner triggers (gitleaks
    # keys on "-----BEGIN ... PRIVATE KEY-----"). A complete marker must not
    # appear as a single string literal — it must be built from fragments.
    for pem_pat in [r'"-----BEGIN [A-Z ]*PRIVATE KEY-----"',
                    r'"-----END [A-Z ]*PRIVATE KEY-----"']:
        hits = re.findall(pem_pat, fixtures_src)
        assert hits == [], (
            f"test_redact.py contains a full-literal PEM marker {hits!r}; "
            f"build the armor markers from fragments so no single literal is a "
            f"complete BEGIN/END PRIVATE KEY marker"
        )
