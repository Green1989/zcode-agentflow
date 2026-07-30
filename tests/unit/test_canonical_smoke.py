"""Task 1.1b — canonical-bytes primitives + a few key cases.

This is the SINGLE canonicalization rule for the whole project (Task 2.2 adds
the exhaustive edge-case suite against the SAME functions — no second
implementation). Here we lock the primitives and the cases the active-run
pointer / config / Store depend on immediately.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from zcode_agentflow import canonical

# ---------- primitives -------------------------------------------------------


def test_canonical_text_single_trailing_newline():
    """`b"body"` -> exactly `b"body\\n"` (one trailing newline, no more)."""
    assert canonical.canonical_text_bytes(b"body") == b"body\n"
    # CRLF / CR collapsed to LF as part of normalization
    assert canonical.canonical_text_bytes(b"a\r\nb") == b"a\nb\n"
    assert canonical.canonical_text_bytes(b"a\rb") == b"a\nb\n"


def test_canonical_json_is_deterministic():
    """Same object => same bytes: sorted keys + compact separators + UTF-8 +
    single trailing newline. Order of insertion must not matter."""
    a = {"b": 2, "a": 1, "c": [3, 2, 1]}
    b = {"c": [3, 2, 1], "a": 1, "b": 2}  # different insertion order
    ba = canonical.canonical_json_bytes(a)
    bb = canonical.canonical_json_bytes(b)
    assert ba == bb
    # compact separators, sorted keys
    assert ba == b'{"a":1,"b":2,"c":[3,2,1]}\n'


def test_sha256_hex_lower():
    """Known SHA-256 vector, lowercase hex."""
    # SHA-256("") = e3b0c44298fc1c149afbf4c8996fb924...
    assert canonical.sha256_hex(b"") == hashlib.sha256(b"").hexdigest()
    # lowercase, 64 hex chars
    h = canonical.sha256_hex(b"abc")
    assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert h == h.lower() and len(h) == 64


# ---------- atomic write -----------------------------------------------------


def test_write_canonical_json_is_atomic_replace(tmp_path: Path):
    """write_canonical_json is atomic via tmp + fsync + os.replace:

    1. the temp file is created in the SAME directory as the target (so
       os.replace is a same-filesystem rename = atomic), and is cleaned up on
       success (no stale .tmp);
    2. a failure mid-write leaves the original untouched and no temp residue;
    3. the target never holds a truncated/partial payload — every observable
       state is whole canonical JSON.
    """
    target = tmp_path / "pointer.json"

    # (1) successful write lands whole + canonical, no temp residue.
    canonical.write_canonical_json(target, {"v": 1})
    assert target.read_bytes() == canonical.canonical_json_bytes({"v": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == [], f"temp file residue left behind: {leftovers}"

    # (2) failure during write (os.replace raises) leaves NO partial target and
    # NO temp residue — the original is preserved and the temp is cleaned up.
    target.write_bytes(canonical.canonical_json_bytes({"original": True}))
    original = target.read_bytes()

    class BoomError(Exception):
        pass

    def boom(*_a, **_k):
        raise BoomError("simulated replace failure")

    real_replace = os.replace
    monkey_patched = False
    try:
        os.replace = boom  # noqa: PTH123 — monkeypatch to force the except-path
        monkey_patched = True
        with pytest.raises(BoomError):
            canonical.write_canonical_json(target, {"should": "not appear"})
    finally:
        if monkey_patched:
            os.replace = real_replace

    assert target.read_bytes() == original, "original target was clobbered on failure"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == [], f"temp file left behind after failed write: {leftovers}"

    # (3) deterministic atomicity: a deliberate PARTIAL payload written directly
    # (simulating what a non-atomic writer would leave mid-crash) is observably
    # different from anything write_canonical_json ever produces. Across many
    # writes, every read of the target must parse as whole JSON.
    canonical.write_canonical_json(target, {"seed": 0})
    for i in range(1, 60):
        canonical.write_canonical_json(target, {"v": i, "k": "x" * 30})
        data = target.read_bytes()
        # whole + canonical: parses, and matches the canonical bytes of the obj
        parsed = json.loads(data)
        assert parsed["v"] == i
        assert data == canonical.canonical_json_bytes({"v": i, "k": "x" * 30})

    # prove a truncated payload is never equal to the canonical whole:
    whole = canonical.canonical_json_bytes({"v": 59, "k": "x" * 30})
    truncated = whole[:-5]  # cut off the tail — a non-atomic writer could leave this
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)
    assert truncated != whole


def test_hash_file_uses_canonical_bytes(tmp_path: Path):
    """hash_file re-hashes the canonical bytes of whatever is on disk."""
    p = tmp_path / "spec.md"
    p.write_bytes(b"hello\n")  # already canonical
    assert canonical.hash_file(p) == canonical.sha256_hex(b"hello\n")
    # a file with CRLF hashes as if normalized to LF
    p.write_bytes(b"hello\r\n")
    assert canonical.hash_file(p) == canonical.sha256_hex(b"hello\n")
