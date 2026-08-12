# Security Policy

## Supported versions

ZCode-AgentFlow is pre-1.0 (`0.1.x`). Security fixes are applied to the latest
`main` branch only; there are no backported release lines yet. When a fixed
release is cut, this section will list the supported version(s).

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.** Please
report it privately via **GitHub Security Advisories**:

1. Go to the **Security** tab of this repository.
2. Click **Advisories → Report a vulnerability**.
3. Describe the issue, the affected component, and a reproduction if possible.

This uses GitHub's private vulnerability reporting, which notifies the
maintainers confidentially. We will acknowledge receipt and work with you on a
fix and coordinated disclosure.

> If GitHub Security Advisories is unavailable for any reason, open a regular
> issue asking the maintainers for an alternate private contact channel —
> **without** including the vulnerability details — and we will reply with a
> secure contact.

## What to include in a report

- A description of the issue and its security impact.
- The component or file affected (e.g. `src/zcode_agentflow/engine.py`).
- A minimal reproduction (steps, input, observed vs. expected).
- Your assessment of severity and any suggested fix.

## What NOT to include in a public issue

**Never paste real secrets, API keys, tokens, authentication files, raw model
output, or personal absolute paths into a public GitHub issue or PR.** The
redaction layer (`src/zcode_agentflow/redact.py`) scrubs credential-bearing
strings from persisted subprocess output, but it cannot protect data you paste
directly into a public report. If a reproduction requires credentials, use the
private advisory channel and redact/replace them.

## Scope

This policy covers the ZCode-AgentFlow code in this repository — the MCP server,
the engine, the adapters, the hooks, and the packaging.

**Out of scope:**

- Vulnerabilities in dependencies — report those upstream. This project pins
  dependency versions in `requirements.lock` and re-scans them against OSV /
  the GitHub Advisory Database on every lock refresh (see
  `tests/unit/test_dependency_security.py`).
- Vulnerabilities in the external **Codex** or **Grok** CLIs themselves —
  report those to OpenAI / xAI respectively.
- Attacks that require an attacker with the **same privileges** as the
  AgentFlow process editing files directly — the threat model (Spec §1.2)
  explicitly states the MCP layer provides process-integrity, not defense
  against a same-privilege malicious process.

## Security-relevant design notes

- **No auto push / merge / PR.** The terminal state is `READY_TO_MERGE`; the
  user performs any push/merge externally. AgentFlow never runs `git push`,
  `git merge`, or remote operations.
- **Two human gates.** SPEC approval and the final merge are both human-gated.
  Gate 1 confirmation strength is environment-dependent (see `README.md`); when
  strong confirmation is not configured, a `security_warning` is surfaced and
  the non-adversarial convention fallback runs (never a silent degrade).
- **Lock-driven install.** `requirements.lock` pins every version; the package
  installs with `--no-deps` so versions cannot drift past the validated,
  OSV-scanned freeze.
