#!/usr/bin/env python3
"""AgentFlow PreToolUse hook — protect authoritative control data.

Thin script (Task 7.1): reads the spike-shape PreToolUse payload from stdin,
delegates to ``zcode_agentflow.hook_logic.run_protect_artifacts``, and answers
on the spike-confirmed channels:
  - exit code 0 (allow) or 2 (deny)  (zcode.hooks.exit_code_denies)
  - on deny, a strict-JSON object on stdout with ``hookSpecificOutput``
    (``permissionDecision: deny``)  (zcode.hooks.feedback_channel)

stdout is PROTOCOL-ONLY (no prints, no logging) — extra keys fail validation.
All diagnostics go to stderr and are sanitized. Plugin/control root resolution
uses the spike-confirmed ``ZCODE_PROJECT_DIR`` env var, never a hardcoded path.

Threat model (Spec §1.2): this hook provides process-integrity — it stops the
model from editing control data via the ordinary tool surface. It is NOT a
defense against a same-privilege malicious process that edits the bytes
directly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# logs to STDERR only — stdout is the protocol channel (G4 / P1-6).
logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="agentflow-protect: %(message)s")

# allow running as a bare script (ZCode invokes `python hooks/protect_artifacts.py`):
# make the package importable without relying on an installed editable package.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from zcode_agentflow import hook_logic  # noqa: E402


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        # unparseable stdin: cannot evaluate safely; fail open (allow) per the
        # hook's fail-open discipline. A bare exit 0 is the allow signal.
        sys.exit(hook_logic.EXIT_ALLOW)

    rc = hook_logic.run_protect_artifacts(payload)
    if rc == hook_logic.EXIT_DENY:
        # echo the deny via the spike-confirmed stdout JSON fields.
        target = hook_logic._extract_target_path(  # noqa: SLF001
            payload.get("tool_input", payload.get("toolInput", {}))
        )
        feedback = hook_logic.build_protect_feedback(payload, target or Path("?"))
        sys.stdout.write(json.dumps(feedback))
    sys.exit(rc)


if __name__ == "__main__":
    main()
