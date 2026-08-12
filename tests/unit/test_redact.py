"""Task 4.1 — secret redaction patterns (Prompt §20).

The redaction layer scrubs credential-bearing strings from subprocess output
BEFORE that output is persisted to ``operation/raw``. The patterns cover the
common secret shapes a model or CLI may echo: Bearer tokens, OpenAI/xAI keys,
GitHub PATs, generic api[_-]?key/secret/password/token assignments, and
Set-Cookie headers. Non-secret content (including non-ASCII) is preserved
verbatim so legitimate model output / file paths survive.
"""

from __future__ import annotations

from zcode_agentflow import redact


# ---------------------------------------------------------------------------
# secret shapes that MUST be redacted
#
# Provider-key-shaped values (OpenAI sk-, xAI xai-, GitHub ghp_/github_pat_,
# AWS AKIA, PEM blocks) are SEGMENTED: built by concatenating fragments at
# module load so that NO single string literal in this source file is a
# complete, plausible real credential. This keeps the fixtures scanner-safe
# (GitHub Push Protection / gitleaks do not flag fragmented literals) while
# still exercising every redaction pattern at runtime, where the concatenated
# value is what redact.redact() sees.
# ---------------------------------------------------------------------------

# --- segmented provider-key builders (fragments, not whole-key literals) ---
_O = "sk" + "-" + "proj-" + "ABCDEFGHIJ1234567890abcdefghijklmnopqrstuvwxyz"
_O2 = "sk" + "-" + "1234567890abcdef1234567890abcdef1234567890"
_O3 = "sk" + "-" + "ant-api03-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"
_BEARER = "sk" + "-" + "live-SECRET_TOKEN_VALUE_HERE"
_XAI = "xai" + "-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
_GHP = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789AB"
_GPAT = "github" + "_pat_" + "1234567890abcdefghijklmnopqrstuvwxyz0123456789"
_AKID = "AKIA" + "IOSFODNN7EXAMPLE"
_AWSKEY = ("aws_secret_access_key=" + "wJalr"
           + "XUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
# PEM private-key blocks. The BEGIN/END armor markers are themselves scanner
# triggers (gitleaks keys on them), so the markers are built from fragments too:
# no single literal in this source is a complete "-----BEGIN ... PRIVATE KEY-----".
_PEM_DASH = "-" * 5
_PEM_BEGIN_RSA = _PEM_DASH + "BEGIN RSA PRIVATE KEY" + _PEM_DASH
_PEM_END_RSA = _PEM_DASH + "END RSA PRIVATE KEY" + _PEM_DASH
_PEM_BEGIN = _PEM_DASH + "BEGIN PRIVATE KEY" + _PEM_DASH
_PEM_END = _PEM_DASH + "END PRIVATE KEY" + _PEM_DASH
_PEM_RSA_BODY = "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/yOn4..."
_PEM_GEN_BODY = "MIIBVwIBADANBgkqhkiG9w0BAQEFAASCAUE..."
_PEM_RSA = _PEM_BEGIN_RSA + "\n" + _PEM_RSA_BODY + "\n" + _PEM_END_RSA
_PEM_GEN = _PEM_BEGIN + "\n" + _PEM_GEN_BODY + "\n" + _PEM_END

SECRET_CASES = [
    # Authorization: Bearer ...
    "Authorization: Bearer " + _BEARER,
    "authorization: bearer abc123def456",
    # OpenAI-style keys (segmented)
    _O, _O2, _O3,
    # xAI / grok keys (segmented)
    _XAI,
    # GitHub PATs (classic + fine-grained; segmented)
    _GHP, _GPAT,
    # Slack tokens
    "xoxb-1234567890-abcdefghij",
    "xoxp-9876543210-zxywvutsrq",
    # AWS access-key IDs (segmented)
    _AKID,
    # generic api[_-]?key = ... (plain sentinel values, not key-shaped)
    "api_key=supersecret_value",
    "api-key: supersecret_value",
    "apikey=supersecret_value",
    # JSON-quoted forms (model output often emits credentials as JSON)
    '"api_key": "supersecret_value"',
    '"password": "hunter2"',
    '"access_token": "ya29.exampletoken"',
    # cookie / password / secret / token assignments (sentinel values)
    "Cookie: session=abcdef0123456789; path=/",
    "Set-Cookie: auth=secretcookievalue; HttpOnly",
    "password=hunter2",
    "secret=topsecretdata",
    "token=bearer xyz789",
    "access_token=ya29.exampletoken",
    # compound identifier names (no word boundary before the secret keyword)
    "client_secret=oauth_client_secret_VALUE",
    "client_secret: oauth_client_secret_VALUE",
    _AWSKEY,
    "secret_key=topsecretkeyvalue",
    "jwt_secret=supersecretjwtvalue",
    "db_secret=databasepasswordvalue",
    # PEM private-key block (segmented)
    _PEM_RSA, _PEM_GEN,
    # URL-embedded credentials
    "https://alice:hunter2-password@git.example.com/org/repo",
    "postgres://user:secretpass@db.host:5432/prod",
]


def test_secret_shapes_are_redacted():
    for case in SECRET_CASES:
        out = redact.redact(case)
        # the secret value must not survive
        assert "supersecret_value" not in out
        assert "hunter2" not in out
        assert "topsecretdata" not in out
        assert "sk-live-SECRET_TOKEN_VALUE_HERE" not in out
        # and a REDACTED marker is present (so the scrub is observable)
        assert redact.REDACTED_MARKER in out, (
            f"case {case!r} was not visibly redacted (got {out!r})"
        )


def test_bearer_token_full_value_gone():
    out = redact.redact("Authorization: Bearer sk-live-abcDEF123")
    assert "sk-live-abcDEF123" not in out
    assert "Bearer" not in out or redact.REDACTED_MARKER in out


# ---------------------------------------------------------------------------
# non-secret content preserved (including non-ASCII)
# ---------------------------------------------------------------------------

PRESERVE_CASES = [
    "the quick brown fox jumps over the lazy dog",
    "no secrets here, just a normal log line",
    "file: src/zcode_agentflow/adapters/codex.py:42",
    "exit code 0, 12 tests passed",
    "café résumé naïve — unicode survives",
    "汉字 日本語 한국어",
    "JSON example: {\"status\": \"READY_FOR_EXECUTION\"}",
    "a path with spaces and (parens) and [brackets]",
]


def test_non_secret_content_preserved():
    for case in PRESERVE_CASES:
        assert redact.redact(case) == case, (
            f"non-secret content was altered: {case!r} -> {redact.redact(case)!r}"
        )


def test_non_ascii_preserved():
    s = "模型输出：测试通过 ✓ (café)"
    assert redact.redact(s) == s


def test_empty_and_plain_strings_untouched():
    assert redact.redact("") == ""
    assert redact.redact("   ") == "   "
    assert redact.redact("no newline here") == "no newline here"


def test_secret_amid_normal_content():
    """A secret embedded in otherwise-normal output is scrubbed but the
    surrounding text survives."""
    line = (
        "starting codex...\n"
        "Authorization: Bearer sk-live-LEAKME\n"
        "done, wrote out.json\n"
    )
    out = redact.redact(line)
    assert "sk-live-LEAKME" not in out
    assert "LEAKME" not in out
    assert "starting codex..." in out
    assert "done, wrote out.json" in out
    assert redact.REDACTED_MARKER in out


def test_each_secret_case_value_is_actually_gone():
    """For every SECRET_CASES entry, the distinctive secret VALUE (not just the
    four shared sentinel values) must not survive redaction. This catches a
    pattern that redacts the keyword but leaves the value, or vice versa.

    The provider-key needles reuse the SEGMENTED builders so this source file
    never holds a whole-key literal either."""
    # the distinctive value is the token after the last = : or @, or the whole
    # opaque token for the bare-key shapes.
    for case in SECRET_CASES:
        out = redact.redact(case)
        assert redact.REDACTED_MARKER in out, (
            f"case not visibly redacted: {case!r} -> {out!r}"
        )
        # none of these distinctive substrings may survive. Provider-key needles
        # are built from the same segmented fragments as SECRET_CASES.
        needles = [
            "supersecret_value", "hunter2", "topsecretdata", _BEARER,
            "oauth_client_secret_VALUE", "wJalr" + "XUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "topsecretkeyvalue", "supersecretjwtvalue", "databasepasswordvalue",
            "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn", "MIIBVwIBADANBgkqhkiG9w0BAQEFAASCAUE",
            "alice:hunter2-password", "user:secretpass",
            _O, _O3, _XAI, _GHP, _GPAT,
            "xoxb-1234567890-abcdefghij", _AKID,
            "secretcookievalue", "abcdef0123456789", "ya29.exampletoken",
            "bearer xyz789",
        ]
        for needle in needles:
            if needle.lower() in case.lower():
                assert needle not in out, (
                    f"secret value {needle!r} survived redaction in case {case!r} "
                    f"-> {out!r}"
                )
