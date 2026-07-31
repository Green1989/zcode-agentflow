"""Secret redaction (Task 4.1; Prompt §20).

Scrubs credential-bearing strings from subprocess output BEFORE that output is
persisted to ``operation/raw``. The patterns cover the common secret shapes a
model or CLI may echo:

  - ``Authorization: Bearer <token>``
  - OpenAI-style keys (``sk-...``), xAI keys (``xai-...``), GitHub PATs
    (``ghp_...``, ``github_pat_...``)
  - generic ``api[_-]?key`` / ``apikey`` / ``password`` / ``secret`` / ``token``
    / ``access_token`` assignments and headers — INCLUDING compound identifier
    names (``client_secret``, ``aws_secret_access_key``, ``secret_key``,
    ``jwt_secret``, ``db_secret``, ``private_key``) and JSON-quoted forms
    (``"api_key": "value"``)
  - ``Set-Cookie:`` / ``Cookie:`` headers
  - PEM private-key blocks (``-----BEGIN ... PRIVATE KEY----- ... -----END-----``)
  - URL-embedded credentials (``https://user:pass@host``)

Non-secret content (including non-ASCII) is preserved verbatim. Redaction errs
on the side of scrubbing ambiguous ``keyword: value`` shapes: a benign line
that happens to read ``secret: the sauce`` would be redacted, which is the safe
failure mode for a layer whose whole purpose is to keep secrets off disk.

This module owns ONLY the substitution patterns. It is dependency-free and
side-effect-free (pure ``str -> str``) so it is trivially testable and safe to
apply to in-memory captures before any disk write (P1-4 ordering).
"""

from __future__ import annotations

import re

__all__ = ["REDACTED_MARKER", "redact"]

REDACTED_MARKER = "REDACTED"


# Each pattern is compiled once at import. We use case-insensitive matching for
# the keyword-bearing shapes; the long opaque token shapes (sk-/xai-/ghp_) are
# matched case-sensitively on their prefix as real CLIs emit them.
#
# ReDoS safety: every quantifier here is linear (``\S+``, ``[A-Za-z0-9_-]{16,}``,
# ``[^\r\n]+``, ``.*?`` over a bounded PEM block). There are no nested
# unbounded quantifiers, so pathological inputs cannot cause catastrophic
# backtracking.
_PATTERNS: list[re.Pattern[str]] = [
    # 1. Authorization: Bearer <token>  (header form, case-insensitive scheme)
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"),
    # 2. OpenAI-style keys: sk- followed by 16+ word chars / dashes
    #    (also catches sk-ant-api03-... / sk-proj-...)
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    # 3. xAI / grok keys: xai- followed by 16+ word chars
    re.compile(r"\bxai-[A-Za-z0-9_-]{16,}"),
    # 4. GitHub classic PAT: ghp_ + 36 chars
    re.compile(r"\bghp_[A-Za-z0-9]{36,}"),
    # 5. GitHub fine-grained PAT: github_pat_ + 22+ chars (token portion)
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    # 6. Slack bot/user tokens: xox[baprs]- + 10+ chars
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # 7. AWS access-key IDs: AKIA + 16 chars
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # 8. generic secret-bearing keyword assignments. Covers BOTH bare
    #    (``api_key=value``) and JSON-quoted (``"api_key": "value"``) forms,
    #    AND compound identifier names (``client_secret``,
    #    ``aws_secret_access_key``, ``secret_key``, ``jwt_secret``,
    #    ``db_secret``, ``private_key``) — these do NOT have a word boundary
    #    before ``secret`` (the preceding char is a letter/underscore), so we
    #    cannot anchor on ``\b``. We do NOT require a leading boundary: the
    #    ``[:=] <value>`` anchor is what marks it an assignment, and we list the
    #    compound forms explicitly so the alternation matches them whole.
    #    An optional closing quote between the keyword and ``[:=]`` lets the
    #    JSON form ``"api_key": ...`` match.
    re.compile(
        r"(?i)(api[_-]?key|apikey|password|passwd|secret|token|access_token|"
        r"client_secret|aws_secret_access_key|secret_key|jwt_secret|db_secret|"
        r"private_key)"
        r"[\"']?\s*[:=]\s*[\"']?\S+"
    ),
    # 9. Set-Cookie / Cookie headers (the whole assignment)
    re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\r\n]+"),
    # 10. PEM private-key blocks (BEGIN ... PRIVATE KEY ----- ... -----END ...)
    re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # 11. URL-embedded credentials: scheme://user:pass@host
    re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s/@]+@[^\s/@]+"),
]


def redact(text: str) -> str:
    """Return ``text`` with secret-bearing substrings replaced by the
    ``REDACTED_MARKER``. Non-secret content (including non-ASCII) is preserved.

    Applied to in-memory captures before any disk write (P1-4): a caller never
    receives the raw secret back, and the bytes persisted to ``operation/raw``
    are already scrubbed (no raw-then-overwrite window).
    """
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        out = pat.sub(REDACTED_MARKER, out)
    return out
