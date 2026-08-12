# Contributing to ZCode-AgentFlow

Thanks for your interest in contributing. This project is a ZCode-supervised
AgentFlow with strict process-integrity and honest-reporting requirements, so a
few conventions matter more than usual.

## Development prerequisites

- **Windows / Git Bash** is the primary development platform (the project is
  validated on it). Linux is best-effort via CI.
- **Python 3.11+** (validated on 3.11 and 3.13).
- **Git** on `PATH`.

## Setup

Install the lock-driven way (pinned versions first, then the package with
`--no-deps`):

```bash
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
```

Run the default test suite to confirm a green baseline before you start:

```bash
python -m pytest -q
```

The **Layer 3 smoke** (`tests/smoke/run_local_smoke.py`) is off by default and
is **not** part of CI — it spends real model tokens and needs codex/grok login.
Do not enable it in CI or in the default suite.

## Test-driven development

This project enforces TDD for behavior changes:

1. **Write or update the test first**, run it, and confirm it **fails for the
   right reason** (RED) — i.e. the target behavior is genuinely missing, not a
   typo.
2. **Write the minimal implementation** that makes the test pass.
3. **Run the test** and confirm it passes (GREEN).

Do not write the implementation first and then back-fill a test shaped to fit
it. Do not delete or weaken a test to make it pass — if a test is genuinely
wrong, explain why and replace it with an equivalent-strength test.

## Commit and branch conventions

- One logical change per commit. Use a clear prefix: `feat:`, `fix:`, `test:`,
  `docs:`, `build:`, `ci:`, `security:`, `chore:`.
- Branch from `main` for your work; do not commit directly to `main` for
  non-trivial changes.
- Local commits only — **do not push** unless asked. No auto-merge, no auto-PR.

## What never goes into a commit

**Never commit:**

- Real API keys, tokens, passwords, or authentication files (`.env`, `*.pem`,
  `*.key`, `~/.codex`, `~/.grok`, `~/.claude`).
- Your local proxy address (e.g. a specific `127.0.0.1:PORT`). The shipped
  config carries **no** proxy default; configure yours locally or via env, and
  use a placeholder (`YOUR-PORT`) in any documented example.
- Your local absolute paths (e.g. `D:\...\zcode-agentflow`). Use a
  `<repo-root>` placeholder in docs.
- Raw CLI output, model responses, or hook logs from a real run.
- Runtime state — `state.json`, `events.jsonl`, `operations/`, `approvals/`,
  `reviews/`, `blobs/`, or the `.agentflow/` control root. These are generated
  at runtime and gitignored; do not force-add them.
- Spike / probe scripts or their outputs.

The test fixtures in `tests/unit/test_redact.py` use **low-entropy sentinel
values** (e.g. `sk-proj-ABCDEFGHIJ1234567890...`) so they exercise the
redaction patterns without looking like real credentials to secret scanners. If
you add fixtures, follow the same convention — do not paste a real-looking
secret.

## Dependency changes

`requirements.lock` is the single source of truth for validated versions. A
dependency bump is a **contract change**:

1. Update the version in `requirements.lock` (and the compatible-release bound
   in `pyproject.toml`).
2. **Re-run the OSV scan** (see `tests/unit/test_dependency_security.py`) to
   confirm the new version has no known advisory affecting this project's
   surface.
3. If the bump is the **MCP SDK**, re-verify the FastMCP stdio contract
   (import path, `@mcp.tool()` registration, `mcp_server.run(transport="stdio")`)
   in a clean venv, and update `docs/capabilities.json` with the real evidence.
4. Run the full default suite and confirm it is green.

Do not bump a version and claim it is validated without the above.

## Capability claims

External interface facts (SDK paths, CLI flags, hook contracts) live in
`docs/capabilities.json` and take precedence over generic docs and model
memory. If you change an adapter or a hook, update the evidence there. Do not
assert an interface supports something without a real run backing it.

## CI

GitHub Actions runs the default pytest suite on Windows + Ubuntu for Python
3.11 and 3.13, plus a gitleaks secret scan. See `.github/workflows/`. All
third-party actions are SHA-pinned — if you add one, pin it to a full commit
SHA, not a floating tag.
