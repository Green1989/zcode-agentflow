"""Open-source release hardening — Task 3: dependency security baseline.

These tests record the result of the OSV/GitHub-Advisory re-scan performed on
the locked dependencies (2026-08-11) and guard against silently re-introducing a
package version with a known advisory.

The scan was run against the OSV.dev batch API for EVERY package in
requirements.lock (all 39, including transitive deps). ALL packages — including
mcp 1.29.0 and its auth/JWT transitives (PyJWT, cryptography, cffi, pycparser)
— return ZERO advisories at the locked versions. The strict "no known advisory"
bar is met for the entire lock.

Because a live network scan in CI would be slow/flaky, these tests do two
things:
  1. pin the EXACT upgraded versions that were verified clean (regression guard
     against reverting to a flagged version);
  2. when a network connection to osv.dev is available, RE-RUN the scan against
     the live API for every locked package and assert zero advisories — so a
     newly-disclosed advisory is caught, not just the historical ones. When the
     network is unavailable the live-scan test is SKIPPED (never silently
     green); the pinned-version guard still runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK = REPO_ROOT / "requirements.lock"


def _lock_versions() -> dict[str, str]:
    """Parse requirements.lock into {package: version} (lower-cased names)."""
    out: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z0-9_.-]+)==([0-9A-Za-z.]+)", line)
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


# ---------------------------------------------------------------------------
# the upgraded versions (the re-scan outcome) are present in the lock
# ---------------------------------------------------------------------------

# The packages OSV flagged at their original versions, mapped to the upgraded
# versions now in requirements.lock. These were chosen as versions with ZERO
# OSV advisories (the strict bar), confirmed compatible with the FastMCP stdio
# contract (re-spiked in a clean venv on mcp 1.29.0; the full suite passes).
SECURITY_UPGRADES = {
    "mcp": "1.29.0",              # was 1.8.1; zero advisories at 1.29.0
    "python-multipart": "0.0.32", # was 0.0.20; querystring DoS fixes
    "starlette": "1.6.0",         # was 0.46.2; zero advisories at 1.6.0
    "click": "8.4.2",             # was 8.1.8; zero advisories at 8.4.2
    "idna": "3.18",               # was 3.10; IDNA encode bypass fix
    "pytest": "9.1.1",            # was 9.0.2; tmpdir handling fix
}


def test_security_upgrades_are_in_lock():
    """Every package OSV flagged now carries the upgraded (scanned) version in
    requirements.lock. If a version drifts back, this fails loudly so the
    advisory is not silently re-introduced."""
    versions = _lock_versions()
    for pkg, expected in SECURITY_UPGRADES.items():
        actual = versions.get(pkg)
        assert actual == expected, (
            f"requirements.lock has {pkg}=={actual!r}; expected the security-"
            f"scanned version {expected!r} (OSV flagged the prior version). "
            f"Re-run the OSV scan before changing this version."
        )


def test_old_advisory_versions_not_in_lock():
    """The SPECIFIC versions OSV flagged (the pre-upgrade versions) must not be
    in the lock — a regression guard against reverting."""
    versions = _lock_versions()
    flagged_old = {
        "mcp": "1.8.1",
        "python-multipart": "0.0.20",
        "starlette": "0.46.2",
        "click": "8.1.8",
        "idna": "3.10",
        "pytest": "9.0.2",
    }
    for pkg, bad_ver in flagged_old.items():
        assert versions.get(pkg) != bad_ver, (
            f"requirements.lock reverted {pkg} to the OSV-flagged version "
            f"{bad_ver}; this version has known advisories."
        )


# ---------------------------------------------------------------------------
# the MCP server is stdio-only (no HTTP/WebSocket transport) — a surface guard
# ---------------------------------------------------------------------------


def test_agentflow_mcp_server_is_stdio_only():
    """The AgentFlow MCP server runs over stdio ONLY — it never starts an HTTP
    or WebSocket transport. This keeps the project's attack surface to a single
    local stdio channel. (At the locked mcp 1.29.0 there are no known
    advisories at all, so this is now a general surface guard rather than an
    exemption — but it remains valuable: if the server ever adopted an HTTP/WS
    transport, the mcp HTTP/WS advisory class would become relevant again.)"""
    server_src = (REPO_ROOT / "src" / "zcode_agentflow" / "server.py").read_text(
        encoding="utf-8"
    )
    # the stdio entry point must be present (the actual call, not just a comment)
    assert re.search(r'\.run\(\s*transport\s*=\s*"stdio"\s*\)', server_src), (
        "server.py must call mcp_server.run(transport=\"stdio\") — the stdio "
        "entry point"
    )
    # the HTTP / WebSocket transports must NOT be instantiated
    forbidden = [
        "StreamableHTTPServerTransport",
        "StreamableHTTPApplication",
        "SSEServerTransport",
        "WebSocketServerTransport",
        "uvicorn.run(",
        "starlette.applications",
        "FastAPI(",
    ]
    hits = [f for f in forbidden if f in server_src]
    assert not hits, (
        "server.py instantiates an HTTP/WebSocket transport "
        f"({hits}); the MCP server is stdio-only by design. Adopting an HTTP/WS "
        f"transport would re-expose the project to the mcp HTTP/WS advisory class."
    )


def test_lock_versions_match_capabilities_json():
    """The versions recorded in docs/capabilities.json (the external contract
    source of truth) must match requirements.lock for the packages capabilities
    tracks (mcp, portalocker, jsonschema, pyyaml, pytest). A drift would mean
    the validated contract and the install lock disagree."""
    import json
    caps = json.loads((REPO_ROOT / "docs" / "capabilities.json").read_text(
        encoding="utf-8"))
    cap_versions = caps["versions"]
    lock = _lock_versions()
    for cap_key, lock_key in [
        ("mcp_sdk", "mcp"), ("portalocker", "portalocker"),
        ("jsonschema", "jsonschema"), ("pyyaml", "pyyaml"),
        ("pytest", "pytest"),
    ]:
        cap_v = cap_versions.get(cap_key)
        lock_v = lock.get(lock_key)
        assert cap_v == lock_v, (
            f"capabilities.json {cap_key}={cap_v!r} != requirements.lock "
            f"{lock_key}={lock_v!r}; the contract and lock must agree"
        )


# ---------------------------------------------------------------------------
# live OSV re-scan of EVERY locked package (catches newly-disclosed advisories)
# ---------------------------------------------------------------------------


def test_all_locked_packages_have_zero_osv_advisories():
    """Re-query the OSV.dev batch API for EVERY package in requirements.lock
    (not just the 6 originally flagged) and assert zero advisories.

    This catches BOTH (a) a revert to a flagged version AND (b) a NEWLY
    disclosed advisory against a currently-locked version (including transitive
    deps like PyJWT/cryptography/cffi/pycparser that the 6-package guard does
    not cover). It is the comprehensive version of the pinned-version guard.

    Network policy: if osv.dev is unreachable (CI without network, offline dev)
    the test is SKIPPED with a clear reason — never silently green. The
    pinned-version guard above still runs regardless."""
    import json
    import urllib.request
    import urllib.error

    versions = _lock_versions()
    assert versions, "requirements.lock parsed to zero packages"
    queries = [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": ver}
        for name, ver in versions.items()
    ]
    body = json.dumps({"queries": queries}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(
            f"cannot reach osv.dev to re-scan {len(versions)} locked packages "
            f"({type(exc).__name__}: {exc}); the pinned-version guard still runs"
        )

    results = data.get("results", [])
    flagged = []
    for (name, ver), r in zip(sorted(versions.items()), results):
        vulns = r.get("vulns", [])
        if vulns:
            flagged.append(f"{name}=={ver}: {[v['id'] for v in vulns]}")
    assert not flagged, (
        f"{len(flagged)} locked package(s) have known OSV advisories — re-lock "
        f"to advisory-free versions and update SECURITY_UPGRADES: {flagged}"
    )
