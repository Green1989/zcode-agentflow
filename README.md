# ZCode-AgentFlow

ZCode-supervised AgentFlow: a single-task run driven from `NEW` to
`READY_TO_MERGE` by a real MCP stdio server, with **Codex (GPT)** planning,
**Grok (Grok-4.5)** reviewing, and two human gates.

## Status

Design/plan finalized (see `docs/superpowers/plans/`) — implementation is
in progress, executed phase-by-phase per
`docs/prompts/zcode-agentflow-execution-prompts-v1/`.

## Install (development)

Per the Phase 0 contract (P1-3) — pinned versions first, then the package
with `--no-deps` so dependency versions cannot drift past the spike-validated
freeze:

```bash
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
```

External interface facts (SDK paths, CLI flags, hook contract) live in
`docs/capabilities.json` and take precedence over generic docs.
