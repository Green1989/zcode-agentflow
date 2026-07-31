"""Top-level CLI dispatcher for ``zcode-agentflow``.

Subcommands:
  (default / no subcommand)  — run the FastMCP stdio server
  admin abandon|archive      — local admin pointer lifecycle (NOT MCP tools)

This keeps the MCP surface at exactly 9 tools (consistency #4) while exposing
abandon/archive as a local command the operator runs outside the model loop.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "admin":
        from zcode_agentflow.admin import main as admin_main
        raise SystemExit(admin_main(argv[1:]))
    # default: FastMCP stdio server
    from zcode_agentflow.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
