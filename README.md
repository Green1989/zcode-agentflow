# ZCode-AgentFlow

ZCode-supervised AgentFlow: a single-task run driven from `NEW` to
`READY_TO_MERGE` by a real MCP stdio server, with **Codex (GPT)** planning,
**Grok (Grok-4.5)** reviewing, and two human gates. The supervisor **never**
pushes, merges, or opens a PR — that is the user's decision, made **outside**
AgentFlow.

## Status

v1 is implemented and tested. The 9-tool MCP server, the state machine
(`NEW` → `READY_TO_MERGE`), the two human gates, and the Codex/Grok adapters
are in place. The default test suite is green. See `docs/phase8-acceptance.md`
for the Layer 1/2 E2E results and the honest Layer 3 (real-CLI smoke) status.

This project ships as a **ZCode plugin** installed from a Git clone + local
marketplace (see §Install) — it is not published on PyPI.

## What it does

AgentFlow is a **process-integrity** workflow for a single task. A ZCode
supervisor drives one task through a fixed sequence of 9 MCP tools, handing
structured artifacts (SPEC, PLAN, execution-result, review-bundle) to an
external **Codex** planner and **Grok** reviewer through versioned files + JSON
schemas (no chat relay). Two human gates (SPEC approval + final merge) gate the
run, and the terminal state `READY_TO_MERGE` stops short of any push/merge/PR.

It detects direct edits, stale reviews, hash mismatches, and unauthorized
changes, routing failures to `NEEDS_HUMAN`. It does **not** defend against a
same-privilege malicious process that edits bytes directly (see the threat model
in `docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md`); v1 network
isolation is advisory-only.

## Prerequisites

- **Python 3.11+** (validated on 3.11 and 3.13).
- **Git** on `PATH`.
- **ZCode** (the host that loads the plugin + runs the MCP server).
- **codex** CLI installed and logged in via ChatGPT (`~/.codex`) — the Planner.
- **grok** CLI installed and logged in via xAI — the Reviewer. Grok has **no**
  automatic proxy injection (unlike codex), so if your network needs a proxy to
  reach xAI you must configure it (see §Configure).
- Windows / Git Bash is the primary platform (Linux is best-effort in CI).

## Install

### Development install

Per the lock-driven contract — pinned versions first, then the package with
`--no-deps` so dependency versions cannot drift past the validated freeze:

```bash
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
```

Verify the installed versions match the freeze:

```bash
python -c "import mcp, portalocker, jsonschema, yaml; print('mcp', mcp.__version__)"
```

### Install as a ZCode plugin (the distribution form)

This repo ships a `marketplace.json` at the root so ZCode's plugin Discover
flow can add it as a **local marketplace** and install the plugin. The
marketplace root *is* the plugin root (`plugins[].source == "."`), so no
`pluginRoot` or absolute path is needed.

1. **Settings → Plugin Management → Discover → `+`** → choose **Local
   directory/file** → select **this repo's root directory** (or the
   `marketplace.json` file directly).
2. The `zcode-agentflow` card appears → **Get / Install** → moves to
   **Installed**.
3. On the **Installed** tab, confirm the plugin is **Enabled**. Open its detail
   view to verify the components: **MCP** (the `zcode-agentflow` stdio server),
   **Skill** (`agentflow`), **Commands** (`agentflow-start`, `agentflow-status`),
   **Hooks**.
4. **Start a new session.** The 9 MCP tools become available.

See `docs/README.md` for the full operator guide (configure, run, recover).

## Configure

`config.yaml` is the production config, loaded + validated by
`src/zcode_agentflow/config.py` (unknown keys / wrong types are rejected — it is
a validator, not a silent deep-merge).

### Configuring the Grok proxy (if your network needs one)

Grok has **no** automatic proxy injection (unlike codex's `~/.codex/.env`). If
your network needs a proxy to reach xAI, set `reviewer.proxy` in `config.yaml`
with **your own** proxy address:

```yaml
reviewer:
  proxy:
    HTTPS_PROXY: http://127.0.0.1:YOUR-PORT
    HTTP_PROXY:  http://127.0.0.1:YOUR-PORT
    ALL_PROXY:   http://127.0.0.1:YOUR-PORT
```

If your network reaches xAI directly, leave it unset. Without a configured proxy
on a proxy-required network, the Grok review hangs and times out → `NEEDS_HUMAN`
(the engine behaves exactly as designed). See AGENTS.md G7/G8 for the full
contract.

No proxy address is shipped by default — you provide your own.

## The 9-tool order

```
workflow_status          (read-only; safe any time)
workflow_init            NEW → INITIALIZING_WORKTREE  (creates the run worktree)
planner_draft_spec       → DRAFTING_SPEC → READY_FOR_SPEC_APPROVAL
                         (or NEEDS_USER_INPUT — re-enter with answers)
workflow_approve_spec    HUMAN GATE 1: → PLANNING  (locks spec_sha256)
planner_create_plan      → READY_FOR_EXECUTION  (v1: exactly one task)
workflow_start_task      → EXECUTING_TASK  (records task_start_commit)
  *executor edits code, git add, git commit in the worktree*  ← real work
workflow_verify_task     → VERIFYING_TASK  (verification + change audit)
reviewer_review_task     → REVIEWING_TASK  (Grok read-only review; PASS keeps state)
workflow_complete_task   HUMAN GATE 2: → READY_TO_MERGE  (full rehash hard checks)
```

`expected_revision` is a lightweight CAS: it must equal `state.revision`. A
mismatch means another call advanced the state; re-read `workflow_status`.

See `skills/agentflow/SKILL.md` for the full `ALLOWED_FROM` table.

## Two human gates

- **Gate 1 — SPEC approval** (`workflow_approve_spec`): the SPEC must be
  approved by a human before planning begins.
- **Gate 2 — final merge** (`workflow_complete_task` → `READY_TO_MERGE`): the
  run stops at `READY_TO_MERGE`; the user decides push/PR/merge externally.

Gate 1's confirmation strength is **environment-dependent**: ZCode supports
tool-level mandatory confirmation, but whether it is enforced depends on your
`~/.claude/settings.json` (`permissions.defaultMode`). When it is not enforced,
the approval uses the non-adversarial convention fallback **with a surfaced
security warning** (never hidden). See `docs/capabilities.json` and the threat
model for details.

## READY_TO_MERGE — no auto push/merge/PR

When `workflow_complete_task` returns `READY_TO_MERGE`:

- **STOP.** Present the handoff (branch, HEAD commit, base commit, changed
  files, test results, review result, SPEC/PLAN sha256) to the user.
- **Do NOT push. Do NOT merge. Do NOT open a PR.** v1 performs none of these.
- The user merges externally, outside AgentFlow.

## Security model and limitations

- **Process-integrity, not tamper-proof.** AgentFlow detects direct edits,
  stale reviews, and hash mismatches, but does not defend against a
  same-privilege malicious process that edits bytes directly (Spec §1.2).
- **v1 network isolation is advisory-only.** The MCP layer provides
  process-integrity, not OS-level network isolation.
- **Single task, one review, two gates.** v1 supports exactly one task per run,
  one Grok review, and two human gates. Multi-task plans route to
  `NEEDS_HUMAN` (BLOCKED), never silent `tasks[0]`.
- **FAIL → `NEEDS_HUMAN`.** Any integrity failure routes to `NEEDS_HUMAN`;
  there is no automatic repair, replan, or final review in v1.

See `SECURITY.md` for vulnerability reporting, and the full threat model in
`docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md`.

## Testing

```bash
python -m pytest -q          # the default suite (excludes the real-CLI smoke)
```

The **Layer 3 smoke** (`tests/smoke/run_local_smoke.py`) exercises the real
Codex/Grok CLIs end-to-end. It is **off by default** (never collected by
`pytest`) and runs only when manually invoked with `AGENTFLOW_SMOKE=1` on a
machine with the real CLIs installed and logged in. It is not part of CI (it
spends real model tokens and needs login state).

See `docs/phase8-acceptance.md` for the three-layer E2E results.

## License

MIT — see [LICENSE](LICENSE).
