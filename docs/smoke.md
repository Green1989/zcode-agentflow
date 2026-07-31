# Layer 3 — Local Smoke Harness (real Codex / Grok CLIs)

This is the **only** layer that exercises the real model CLIs end to end. Layers
1 and 2 (under `tests/e2e/`) use fake adapters and prove the *engine + MCP
transport*; Layer 3 proves the **Phase-0 CLI contracts** actually work against a
real AgentFlow run — `codex exec` (stdin prompt, `-s read-only`, `-C repo`,
`--output-schema`) and `grok -p` (`--cwd` absolute path, explicit proxy
injection per G7/G8, `structuredOutput` authoritative).

The harness is a **standalone script, not a pytest test**, and it is **off by
default**. The default `python -m pytest -q` suite never collects it
(`pyproject.toml` sets `--ignore=tests/smoke`). It runs only when invoked
manually with `AGENTFLOW_SMOKE=1`, and only after a capability gate confirms the
real CLIs are on `PATH` (Phase 0 marks `planner_ok` / `reviewer_ok` as
**required**).

## When to run it

Run this manually, on a machine where the real CLIs are installed and logged in,
to validate the external contracts after any adapter or prompt change. It is
**not** part of CI by design: it spends real model tokens and needs network +
login state.

## Prerequisites

- **codex** installed and logged in (`~/.codex`, ChatGPT account) — see
  `docs/capabilities.json` `codex.auth`.
- **grok** installed and logged in (xAI account) — see `docs/capabilities.json`
  `grok.auth`.
- **Local proxy reachable** for grok. Grok has **no** automatic proxy injection
  (unlike codex's `~/.codex/.env`), so the harness passes
  `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` explicitly in the subprocess env
  (capability gates G7/G8). Default: `http://127.0.0.1:7897` (Clash Verge);
  override with `AGENTFLOW_PROXY`.
- Python deps installed:
  `pip install -r requirements.lock && pip install -e . --no-deps`.

## Run it

From the repo root:

```bash
AGENTFLOW_SMOKE=1 python tests/smoke/run_local_smoke.py
```

Optional overrides:

```bash
AGENTFLOW_SMOKE=1 AGENTFLOW_PROXY=http://127.0.0.1:7897 \
  python tests/smoke/run_local_smoke.py
```

## What it does

1. **Gate** — refuses to run unless `AGENTFLOW_SMOKE=1` is set and both `codex`
   and `grok` are on `PATH` (exits `2` if the env var is unset, `3` if a CLI is
   missing).
2. Builds a **tiny real git repo** (one file, `a.txt`) in a temp dir — lowest
   cost, fully verifiable.
3. Drives the **full v1 workflow** with the REAL adapters:
   `init → draft (codex) → approve (GATE 1) → create_plan (codex) → start_task
   → [executor edits a.txt + real git commit] → verify → review (grok) →
   complete (GATE 2)`.
4. **Observes both human gates**: SPEC approval is recorded with
   configured-confirmation; the run **stops at READY_TO_MERGE** — no merge, no
   push, no PR.
5. Prints a **READY_TO_MERGE handoff** (`run_id`, branch, base/head commits,
   changed files, review decision) and explicitly states it did not push/merge.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | PASS — reached `READY_TO_MERGE` with real Codex/Grok. |
| `1` | A real CLI / adapter error (surfaced honestly, not swapped for a fake). |
| `2` | `AGENTFLOW_SMOKE` is not `1` (harness is off by default). |
| `3` | Capability gate failed (`codex`/`grok` not on `PATH`). |
| `4` | Stopped safely at a non-terminal state (`NEEDS_HUMAN` / `NEEDS_USER_INPUT`). |

## Honesty rule

Any failure (CLI error, verify failure, Grok non-PASS) is reported honestly with
its exit code and reason. The harness **never** swaps the real CLIs for fakes to
manufacture a pass — that would defeat the entire purpose of Layer 3.
