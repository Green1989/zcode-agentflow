"""Grok adapter — Reviewer (Task 4.3; Review P0-5; Prompt §13; parallel P0-4).

Drives ``grok -p`` against the immutable Review Bundle (assembled by
:mod:`zcode_agentflow.review_bundle`). The Phase-0 contract
(docs/capabilities.json grok.*) is the authority, with two HARD environment
rules (G7/G8):

  - G7: Grok has NO automatic proxy injection (unlike codex's ~/.codex/.env).
    The adapter MUST explicitly pass ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``
    in the subprocess ``env``, else the call hangs (network unreachable). The
    proxy values come from the request (config-supplied).
  - G8: ``--cwd`` MUST be an ABSOLUTE path (relative => 'os error 2'); it points
    at the bundle dir. ``--json-schema`` accepts INLINE JSON only (``@file`` is
    unsupported) — the adapter reads the schema file and passes its contents as
    a string.

Read-only + immutability guarantees (Review P0-5):

  - HEAD + clean are checked BEFORE the call (a dirty tree or a moved HEAD at
    call time => raise ``GrokAdapterError``, routing to NEEDS_HUMAN);
  - the bundle file hashes are snapshotted before and re-checked after (the
    bundle must be byte-identical; a modification => raise);
  - HEAD + clean are re-checked AFTER the call.

RAW-only validation (parallel P0-4): the adapter validates the model's
``structuredOutput`` (the authoritative parsed field, Phase 0 G8) against the
RAW ``review-decision`` schema + its cross-field invariants, and returns the
raw decision. The engine injects run_id/task_id/commits/hashes and validates
the FINAL ``review-result`` (Task 6.5).

The adapter NEVER acquires the Run Lock, NEVER writes state/events/authoritative
artifacts — only ``operation/raw`` (via the shared helper).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from zcode_agentflow import gitops, schemas
from zcode_agentflow.adapters.base import AdapterResult, run_subprocess
from zcode_agentflow.review_bundle import REQUIRED_FILES

__all__ = ["GrokAdapter", "GrokAdapterError"]

_log = logging.getLogger(__name__)


class GrokAdapterError(RuntimeError):
    """Raised when the grok invocation cannot be assembled, the repo is dirty /
    HEAD moved at call time, the bundle was modified, the model output is not
    parseable, or its RAW review-decision fails validation."""


class GrokAdapter:
    """Adapter for ``grok -p`` (Reviewer). See module docstring for the
    Phase-0 contract (G7/G8). ``command`` defaults to ``grok``; tests pass
    ``sys.executable`` and a fake script path in the request (``executable``)."""

    def __init__(self, command: str = "grok"):
        self.command = command

    def invoke(
        self, *, request: dict, repo_cwd, operation_dir, timeout: int
    ) -> AdapterResult:
        bundle_dir = Path(request.get("bundle_dir", "")).resolve()
        if not bundle_dir.is_dir():
            raise GrokAdapterError(
                f"review bundle dir not found: {bundle_dir!r}"
            )

        schema_path = request.get("schema_path")
        if not schema_path or not Path(schema_path).is_file():
            raise GrokAdapterError(
                f"review-decision schema file not found: {schema_path!r}"
            )
        proxy = request.get("proxy") or {}

        # ---- pre-call: HEAD + clean + bundle completeness + bundle snapshot ----
        repo_root = request.get("repo_root", repo_cwd)
        pre_head = gitops.head(repo_root)
        if not gitops.is_clean(repo_root):
            raise GrokAdapterError(
                f"repo {repo_root!r} is not clean at review call time; refusing "
                f"to review a dirty tree (route to NEEDS_HUMAN)"
            )
        # bundle completeness (all required files present + non-empty)
        for name in REQUIRED_FILES:
            p = bundle_dir / name
            if not p.is_file() or p.stat().st_size == 0:
                raise GrokAdapterError(
                    f"review bundle missing/empty file: {name}"
                )
        # snapshot bundle hashes (immutability check)
        bundle_before = self._bundle_hashes(bundle_dir)

        # ---- assemble argv (token list, shell=False) ----
        argv: list[str] = [self.command]
        executable = request.get("executable")
        if executable:
            argv.append(str(executable))
        argv += ["-p", "Review the task change against the Review Bundle files."]
        # G8: --cwd is an ABSOLUTE path to the bundle dir.
        argv += ["--cwd", str(bundle_dir)]
        # G8: --json-schema takes INLINE JSON (read the file, pass its contents).
        schema_inline = Path(schema_path).read_text(encoding="utf-8")
        argv += ["--json-schema", schema_inline]
        argv += ["--output-format", "json"]
        # read-only planner mode + auto-approve tool executions (no prompts)
        argv += ["--permission-mode", "plan"]
        argv += ["--always-approve"]

        # G7: explicitly inject proxy env (grok has NO automatic injection).
        child_env = dict(os.environ)
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            if proxy.get(k):
                child_env[k] = proxy[k]

        exit_code, stdout, stderr = run_subprocess(
            argv,
            stdin_text=None,
            cwd=bundle_dir,
            timeout=timeout,
            operation_dir=operation_dir,
            env=child_env,
        )

        # ---- post-call: HEAD + clean + bundle immutability ----
        post_head = gitops.head(repo_root)
        if post_head != pre_head:
            raise GrokAdapterError(
                f"repo HEAD moved during review: was {pre_head!r}, now "
                f"{post_head!r} (route to NEEDS_HUMAN)"
            )
        if not gitops.is_clean(repo_root):
            raise GrokAdapterError(
                f"repo {repo_root!r} became dirty during review (route to "
                f"NEEDS_HUMAN)"
            )
        bundle_after = self._bundle_hashes(bundle_dir)
        if bundle_before != bundle_after:
            changed = sorted(
                set(bundle_before) ^ set(bundle_after)
                | {k for k in bundle_before if bundle_before[k] != bundle_after.get(k)}
            )
            raise GrokAdapterError(
                f"review bundle was modified during the call (files: {changed}); "
                f"the bundle must be immutable (route to NEEDS_HUMAN)"
            )

        # ---- parse the grok JSON envelope; structuredOutput is authoritative ----
        parsed = None
        if exit_code == 0 and stdout.strip():
            try:
                envelope = json.loads(stdout)
            except (json.JSONDecodeError, ValueError) as exc:
                raise GrokAdapterError(
                    f"grok stdout is not a JSON envelope: {exc}"
                ) from exc
            # Phase 0 G8: structuredOutput is the authoritative parsed object.
            parsed = envelope.get("structuredOutput")
            if parsed is None:
                # some envelopes carry the decision only in `text`; try parsing it
                text = envelope.get("text") or ""
                if text.strip().startswith("{"):
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
            if parsed is None:
                raise GrokAdapterError(
                    "grok envelope has no structuredOutput decision"
                )

        if exit_code != 0:
            parsed = None

        # validate ONLY the RAW review-decision schema + cross-field invariants.
        if parsed is not None:
            try:
                schemas.validate_raw_review(parsed)
            except schemas.SchemaError as exc:
                raise GrokAdapterError(
                    f"grok RAW review-decision failed validation: {exc}"
                ) from exc

        return AdapterResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            artifacts={},
            parsed=parsed,
        )

    @staticmethod
    def _bundle_hashes(bundle_dir: Path) -> dict[str, str]:
        """Snapshot the sha256 of every regular file in the bundle dir. Used for
        the before/after immutability check (the bundle must be byte-identical
        before and after the grok call)."""
        out: dict[str, str] = {}
        for p in sorted(bundle_dir.iterdir()):
            if p.is_file():
                out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out
