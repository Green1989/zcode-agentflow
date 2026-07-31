"""Codex adapter — Planner (Task 4.2; Review P0-3, P0-4; parallel P0-5).

Drives ``codex exec`` to draft a SPEC (``kind=draft_spec``) or create a PLAN
(``kind=create_plan``). The Phase-0 contract (docs/capabilities.json codex.*)
is the authority for the flags:

  - prompt via **stdin** (no positional PROMPT arg) — Review P0-3: the prompt
    is NEVER concatenated into argv;
  - ``-C <repo>`` = ``repo_cwd`` (the run worktree) — P0-4: cwd is the repo,
    NOT control_root;
  - ``-s read-only`` (sandbox policy; from capabilities.read_only_held);
  - ``--output-schema <file>`` constrains the model's final answer to the RAW
    schema (spec-draft-output for draft_spec; plan-proposal for create_plan);
  - ``-o <file>`` writes the result JSON to ``operation_dir/out.json`` — a THIRD
    concept distinct from stdin and cwd;
  - ``-c model_reasoning_effort=\"high\"`` (capabilities default; config may
    downgrade to medium);
  - ``--json`` emits JSONL events to stdout (captured, redacted, ignored by the
    adapter — the result is read from the ``-o`` file).

RAW-only validation (parallel P0-4): the adapter validates the model's JSON
against the RAW schema ONLY (``validate_raw_spec`` / ``validate_raw_plan``) and
returns the raw object on ``AdapterResult.parsed``. The RAW schema forbids
authoritative fields (run_id/base_commit/hashes/plan_version); the engine
injects those and validates the FINAL envelope (Tasks 6.2/6.3). For
``create_plan``, the RAW ``plan-proposal`` wrapper makes a multi-task goal fail
cleanly at validation (P0-5).

The adapter NEVER acquires the Run Lock, NEVER writes state/events/authoritative
artifacts — only ``operation/raw`` (via the shared helper) and the ``-o`` file
(which the Store promotes if/when finish commits).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from zcode_agentflow import schemas
from zcode_agentflow.adapters.base import AdapterResult, run_subprocess

__all__ = ["CodexAdapter", "CodexAdapterError"]

_log = logging.getLogger(__name__)

# kind -> (RAW validator, schema filename stem)
_KIND_TO_SCHEMA = {
    "draft_spec": (schemas.validate_raw_spec, "spec-draft-output"),
    "create_plan": (schemas.validate_raw_plan, "plan-proposal"),
}


class CodexAdapterError(RuntimeError):
    """Raised when the codex invocation cannot be assembled or its RAW output
    fails validation. A subprocess timeout is surfaced as
    :class:`AdapterTimeout` (from the base helper) instead."""


class CodexAdapter:
    """Adapter for ``codex exec`` (Planner). See module docstring for the
    Phase-0 contract. ``command`` defaults to ``codex``; tests pass
    ``sys.executable`` and a fake script path in the request (``executable``),
    so the adapter runs ``<python> <fake>`` instead of the real CLI."""

    def __init__(self, command: str = "codex"):
        self.command = command

    def invoke(
        self, *, request: dict, repo_cwd, operation_dir, timeout: int
    ) -> AdapterResult:
        kind = request.get("kind")
        if kind not in _KIND_TO_SCHEMA:
            raise CodexAdapterError(
                f"unknown codex request kind {kind!r}; expected one of "
                f"{sorted(_KIND_TO_SCHEMA)}"
            )
        validator, schema_stem = _KIND_TO_SCHEMA[kind]

        prompt = request.get("prompt")
        if not prompt:
            raise CodexAdapterError("codex request missing 'prompt'")

        # The schema path: prefer an explicit request value (tests), else the
        # shipped schema under the repo schemas/ dir.
        schema_path = request.get("schema_path") or _shipped_schema(schema_stem)
        if not schema_path or not Path(schema_path).is_file():
            raise CodexAdapterError(
                f"schema file for {kind!r} not found: {schema_path!r}"
            )

        # ---- assemble argv (token list, shell=False) ----
        # The codex binary may be the real CLI (command='codex') or, for tests,
        # a fake script run under a python interpreter. When ``executable`` is
        # supplied in the request, command=<interpreter> and argv[1]=<script>.
        argv: list[str] = [self.command]
        executable = request.get("executable")
        if executable:
            argv.append(str(executable))

        argv += [
            "exec",
            # prompt via stdin (no positional PROMPT) — Review P0-3
            "-s", str(request.get("sandbox", "read-only")),   # read-only sandbox
            "-C", str(repo_cwd),                              # cwd = run worktree (P0-4)
            "--output-schema", str(schema_path),              # RAW schema constraint
            "-o", str(Path(operation_dir) / "out.json"),      # output -> operation staging
        ]
        effort = request.get("reasoning_effort", "high")
        argv += ["-c", f'model_reasoning_effort="{effort}"']
        argv += ["--json"]

        out_path = Path(operation_dir) / "out.json"

        exit_code, stdout, stderr = run_subprocess(
            argv,
            stdin_text=prompt,            # prompt via stdin (NOT argv)
            cwd=repo_cwd,                 # cwd = run worktree (P0-4)
            timeout=timeout,
            operation_dir=operation_dir,
        )

        # read the result JSON from the -o path (the model's structured output)
        parsed = None
        if out_path.is_file():
            try:
                parsed = json.loads(out_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError) as exc:
                raise CodexAdapterError(
                    f"codex wrote a non-JSON result to {out_path}: {exc}"
                ) from exc

        # A non-zero exit means the model run failed; do NOT return an
        # unvalidated parsed object (the engine would have to re-check exit_code
        # before trusting it). Drop parsed on failure; the caller (engine)
        # inspects exit_code/stderr to route to fail/unknown.
        if exit_code != 0:
            parsed = None

        # validate ONLY the RAW schema (parallel P0-4). The RAW schema forbids
        # authoritative fields; the engine injects them and validates FINAL.
        if parsed is not None:
            try:
                validator(parsed)
            except schemas.SchemaError as exc:
                raise CodexAdapterError(
                    f"codex RAW output failed {kind!r} schema validation: {exc}"
                ) from exc

        return AdapterResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            artifacts={"out.json": out_path} if out_path.is_file() else {},
            parsed=parsed,
        )


def _shipped_schema(stem: str) -> str | None:
    """Return the path to the shipped schema ``schemas/<stem>.schema.json`` if
    it exists, else None."""
    repo_root = Path(__file__).resolve().parents[3]
    p = repo_root / "schemas" / f"{stem}.schema.json"
    return str(p) if p.is_file() else None
