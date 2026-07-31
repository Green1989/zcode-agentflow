"""Local admin commands — abandon / archive (Task 6.8; consistency #4).

These are NOT MCP tools. The MCP surface stays exactly 9. Operators invoke them
via the local CLI::

    zcode-agentflow admin abandon --run-id <id> --user-data-root <path>
    zcode-agentflow admin archive --run-id <id> --user-data-root <path>

Both mutate the active-run pointer ONLY inside ``ActiveRunRegistry.locked`` via
the ``*_locked`` methods (errata single-owner pattern; never re-acquire).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from zcode_agentflow import active_run

__all__ = ["abandon_run", "archive_run", "main"]


class AdminError(RuntimeError):
    """Raised when an admin command cannot proceed (no pointer, wrong run_id)."""


def abandon_run(*, user_data_root, run_id: str) -> None:
    """Abandon the active run: clear the pointer so a new workflow_init is allowed.

    Holds the Registry Lock for the whole critical section. Refuses to clear a
    pointer that names a DIFFERENT run_id (defense against a stale CLI call).
    """
    with active_run.ActiveRunRegistry.locked(user_data_root) as registry:
        ptr = registry.read_pointer_locked()
        if ptr is None:
            raise AdminError(f"no active-run pointer to abandon (run_id={run_id!r})")
        if ptr.get("run_id") != run_id:
            raise AdminError(
                f"active pointer names {ptr.get('run_id')!r}, not {run_id!r}; "
                f"refusing to abandon the wrong run"
            )
        registry.clear_pointer_locked()


def archive_run(*, user_data_root, run_id: str) -> None:
    """Archive a terminal (or any) active run: clear the pointer.

    v1 archive is pointer-clear only (no blob relocation). Same lock discipline
    as abandon. A future phase may move control_root under an archives/ tree.
    """
    with active_run.ActiveRunRegistry.locked(user_data_root) as registry:
        ptr = registry.read_pointer_locked()
        if ptr is None:
            raise AdminError(f"no active-run pointer to archive (run_id={run_id!r})")
        if ptr.get("run_id") != run_id:
            raise AdminError(
                f"active pointer names {ptr.get('run_id')!r}, not {run_id!r}; "
                f"refusing to archive the wrong run"
            )
        registry.clear_pointer_locked()


def main(argv: list[str] | None = None) -> int:
    """CLI entry for ``zcode-agentflow admin ...``."""
    parser = argparse.ArgumentParser(
        prog="zcode-agentflow admin",
        description="Local admin commands (NOT MCP tools): abandon / archive.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p):
        p.add_argument("--run-id", required=True, help="run id to abandon/archive")
        p.add_argument(
            "--user-data-root",
            required=True,
            help="directory holding active-run.json (the pointer home)",
        )

    p_abandon = sub.add_parser("abandon", help="clear the active-run pointer")
    _add_common(p_abandon)
    p_archive = sub.add_parser("archive", help="archive (clear) the active-run pointer")
    _add_common(p_archive)

    args = parser.parse_args(argv)
    try:
        if args.command == "abandon":
            abandon_run(user_data_root=args.user_data_root, run_id=args.run_id)
        elif args.command == "archive":
            archive_run(user_data_root=args.user_data_root, run_id=args.run_id)
        else:
            parser.error(f"unknown command {args.command!r}")
    except AdminError as exc:
        print(f"admin error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
