#!/usr/bin/env python3
"""AgentFlow Stop hook — gate stopping on the shared, read-only check_stop.

Thin script (Task 7.1): reads the spike-shape Stop payload from stdin, delegates
to ``zcode_agentflow.hook_logic.run_gate_on_stop_with_feedback`` (which itself
calls the SINGLE read-only ``engine.check_stop`` and never re-derives
review/clean/HEAD logic — Review §六.6), and answers on the spike-confirmed
channels:
  - exit code 0 (allow stop) or 2 (request continuation / NEEDS_HUMAN)
    (zcode.hooks.exit_code_denies; stop_reentry_limit)
  - on block, a strict-JSON object on stdout with ``additionalContext``
    (injected into the conversation)  (zcode.hooks.feedback_channel)

stdout is PROTOCOL-ONLY. Diagnostics → stderr (sanitized).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="agentflow-stop: %(message)s")

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
        # unparseable stdin: cannot evaluate; allow the stop (fail open).
        sys.exit(hook_logic.EXIT_ALLOW)

    rc, feedback = hook_logic.run_gate_on_stop_with_feedback(payload)
    if feedback is not None:
        sys.stdout.write(json.dumps(feedback))
    sys.exit(rc)


if __name__ == "__main__":
    main()
