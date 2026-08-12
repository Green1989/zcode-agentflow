# Open-Source Release Hardening — Notes (2026-08-11)

This document records the decisions and reframings made during the open-source
release hardening pass (branch `codex/open-source-release-hardening`). It is a
RELEASE note, intentionally separate from the Phase Reviews under `docs/review/`.

**Note on Review history (honest record):** during this pass the file
`docs/review/2026-07-31-phase7-8-audit-fix-notes.md` was modified in commit
`1dc1b19` and then restored to its original (main) bytes in commit `884d053`.
Its **current content is byte-identical to `main`** (`git diff main` is empty),
but the branch history carries the modification-then-revert. Whether to clean
that history (squash/rebase) is a user decision — it is reported as a blocking
item in the release report, not silently rewritten.

## 1. Machine-specific configuration removed

The Phase-0 spike and the original R2-3 audit-fix documented certain
environment-specific facts as if they were universal production truths. The
release hardening reframes them as **per-operator / environment-probe** facts so
the shipped defaults do not assume every installer shares the original
developer's machine:

- **Proxy address.** `config.yaml` and `.zcode-plugin/plugin.json` no longer
  ship a hard-coded local proxy (the developer's specific `localhost:port`).
  The proxy is an optional USER override set in `config.yaml` `reviewer.proxy`.
  The G7/G8 contract (Grok has no automatic proxy injection; the adapter must
  inject the user-configured proxy into the subprocess env) is **unchanged** —
  only the hard-coded default was removed. See AGENTS.md G7 for the contract.

- **`defaultMode=yolo`.** The Phase-0 spike observed one environment running
  `defaultMode=yolo` where tool-level confirmation was NOT enforced. The
  shipped docs/comments previously stated this as a fixed production fact. It
  is now framed as: the spike env ran yolo; **your environment may differ** —
  whether confirmation is enforced depends on your own
  `~/.claude/settings.json`. The honest security-degrade behaviour (fallback
  with a surfaced `security_warning`; strict mode fails loud) is unchanged.

The Phase-0 evidence file (`docs/capabilities.json`) describes the proxy
**requirement** as a historical probe finding (Grok needs an explicit proxy on
proxy-required networks) but no longer carries the developer's specific address
— the literal was replaced with `<the operator's proxy>` / equivalent generic
phrasing, so no file in the repo ships the concrete value.

## 2. Dependency security re-scan

`requirements.lock` was re-scanned against the OSV / GitHub Advisory Database
(2026-08-11). Six packages were upgraded to versions with **zero** known OSV
advisories (see `tests/unit/test_dependency_security.py` for the exact versions
and the regression guard).

**mcp is clean.** `mcp==1.29.0` returns ZERO advisories from OSV, as do all of
its transitive dependencies (including the auth/JWT support it now pulls in:
PyJWT, cryptography, cffi, pycparser). The strict "no known advisory" bar is met
for the entire lock. (An earlier round of this pass incorrectly concluded that
only mcp 2.0.0 was clean and that it required a server rewrite — that was based
on an incomplete version check. mcp 1.10–1.29 are all stable 1.x releases that
retain `mcp.server.fastmcp.FastMCP`, and 1.28.1/1.29.0 are advisory-free.)

The AgentFlow MCP server remains **stdio-only**
(`server.py`: `mcp_server.run(transport="stdio")`) and instantiates no
HTTP/WebSocket transport. `test_agentflow_mcp_server_is_stdio_only` guards that
surface so the HTTP/WS advisory class cannot become relevant if the server is
ever changed.

## 3. FastMCP stdio contract re-verified (mcp 1.29.0)

Because the mcp SDK upgrade is a validated-contract change, the Phase-0 MCP
probe was re-run in a clean temp venv on mcp 1.29.0:
- `from mcp.server.fastmcp import FastMCP` — imports.
- `@mcp.tool()` registers a tool in the registry.
- `FastMCP.run(transport="stdio")` exists.

`docs/capabilities.json` `versions` block was updated with the real evidence
(mcp_sdk 1.8.1 → 1.29.0) and a dated `_note` describing the re-spike.

## 4. Test count history

The default test suite count grows as tests are added. Historical snapshots for
traceability: 560 @ 2026-07-31 (audit-fix R2), 594 @ 2026-08-11 (release
hardening round 1, clean temp venv), 603 @ 2026-08-11 (local env, mcp 1.9.4),
**605 @ 2026-08-11 (final, mcp 1.29.0 + Unicode-path + live-OSV-all-package
regression tests)**. The authoritative count is whatever `python -m pytest -q`
reports today; the figures here are dated snapshots for traceability.
