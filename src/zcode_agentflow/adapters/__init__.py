"""Adapters — external CLI subprocess wrappers (Phase 4).

Three adapters (Codex, Grok, Verification) share the :class:`Adapter` protocol
and the :func:`run_subprocess` helper from :mod:`zcode_agentflow.adapters.base`.

Invariants (Phase 4):
  - an adapter NEVER acquires the Run Lock (it runs between begin/finish);
  - an adapter NEVER writes state/events/authoritative artifacts — only
    ``operation/raw`` (the sole persistable output);
  - subprocess output is captured IN MEMORY, REDACTED, then written (P1-4).
"""

from zcode_agentflow.adapters.base import Adapter, AdapterResult, AdapterTimeout

__all__ = ["Adapter", "AdapterResult", "AdapterTimeout"]
