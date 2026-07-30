"""FastMCP stdio server — the 9-tool MCP surface (Task 1.5 wires workflow_status;
Task 1.6 declares the other 8 signatures; Phase 6 fills the bodies).

Critical invariants:
- stdout is MCP-FRAMES-ONLY (P1-6): every log handler is on stderr; NO print;
  the official FastMCP SDK owns stdout framing. Subprocess adapters (Phase 4)
  capture child stdout and never inherit it here.
- validate_capabilities runs at startup (P1-1) and fails fast — except in test
  mode, where there is no real production config to gate on.
"""

from __future__ import annotations

import logging
import os
import sys

# Configure logging to STDERR ONLY — stdout is the MCP protocol channel (P1-6).
# Do this at import time so any later logging.* call lands on stderr, never stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("AGENTFLOW_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("zcode_agentflow.server")

# Official mcp SDK import path, recorded by the Phase 0 spike
# (docs/capabilities.json versions.mcp_sdk_import_path).
from mcp.server.fastmcp import FastMCP  # noqa: E402

from zcode_agentflow.config import (  # noqa: E402
    CapabilityError,
    validate_capabilities,
)

__all__ = ["mcp_server", "workflow_status", "get_engine", "main"]

mcp_server = FastMCP("zcode-agentflow")

# How the server discovers the Phase 0 findings + production config at startup.
# In a real plugin run these are resolved from plugin_root; in tests the
# capability gate is bypassed via AGENTFLOW_TEST_MODE.
_CAPABILITIES_PATH = os.environ.get(
    "AGENTFLOW_CAPABILITIES", os.path.join("docs", "capabilities.json")
)
_CONFIG_PATH = os.environ.get("AGENTFLOW_CONFIG", "config.yaml")


def _startup_capability_check() -> None:
    """Run validate_capabilities at server startup (P1-1). Skipped in test mode,
    where no real production config/findings are present."""
    if os.environ.get("AGENTFLOW_TEST_MODE") == "1":
        logger.warning("AGENTFLOW_TEST_MODE=1 — skipping startup capability gate")
        return
    import json
    from pathlib import Path

    from zcode_agentflow import config as config_mod

    cap_path = Path(_CAPABILITIES_PATH)
    cfg_path = Path(_CONFIG_PATH)
    if not cap_path.exists() or not cfg_path.exists():
        # No config to gate on yet — cannot validate; do not silently start.
        raise CapabilityError(
            f"cannot validate capabilities: missing {_CAPABILITIES_PATH!r} or {_CONFIG_PATH!r}"
        )
    findings = json.loads(cap_path.read_text(encoding="utf-8"))["gates"]
    cfg = config_mod.load(cfg_path)
    validate_capabilities(cfg, findings)


@mcp_server.tool()
def workflow_status() -> dict:
    """Return a snapshot of the active run, or {"active_run": None}.

    Phase 1: returns the placeholder. The real snapshot (read the active-run
    pointer, surface state/task/revision) is wired in Phase 6 once the Engine
    exists.
    """
    return {"active_run": None}


def get_engine() -> "Engine":  # type: ignore[name-defined]  # noqa: F821
    """Return the workflow Engine (Phase 6). Not available in Phase 1."""
    raise NotImplementedError("Engine is implemented in Phase 6")


def main() -> None:
    """Server entry point: validate capabilities, then run FastMCP on stdio."""
    _startup_capability_check()
    # FastMCP owns stdout (MCP framing). All our logging is on stderr (P1-6).
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
