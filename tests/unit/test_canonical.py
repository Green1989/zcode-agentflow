"""Task 2.2 — the EXHAUSTIVE edge-case suite for the single canonicalization rule.

The canonicalization IMPLEMENTATION already shipped in Task 1.1b
(``zcode_agentflow/canonical.py``) — it is a base module the pointer/config/store
depend on. This task adds the exhaustive edge-case suite that LOCKS the rule
down; NO second canonicalization implementation is written here (only tests
against the existing functions). This guards against any later phase quietly
introducing a divergent normalization.

The single rule (Spec §4.7), enforced everywhere:
  - encoding: UTF-8 (strict decode)
  - newlines: CRLF and lone CR -> LF
  - trailing: collapse any run of trailing newlines to exactly ONE
  - algorithm: SHA-256, lowercase hex
  - JSON: ``ensure_ascii=False, sort_keys=True, separators=(",",":")`` then the
    text rule above (so JSON is also subject to newline normalization)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zcode_agentflow import canonical


# ---------------------------------------------------------------------------
# canonical_text_bytes: trailing-newline normalization
# ---------------------------------------------------------------------------


def test_single_trailing_newline_added():
    """A body with no trailing newline gets exactly one."""
    assert canonical.canonical_text_bytes(b"body") == b"body\n"


def test_multiple_trailing_newlines_collapsed():
    """Any run of trailing newlines collapses to exactly one (never stripped to
    zero, never left as multiple)."""
    assert canonical.canonical_text_bytes(b"body\n\n\n") == b"body\n"
    assert canonical.canonical_text_bytes(b"body\n") == b"body\n"
    assert canonical.canonical_text_bytes(b"body\n\n\n\n\n") == b"body\n"


def test_empty_input_becomes_single_newline():
    """Empty bytes canonicalize to a single newline (one trailing newline)."""
    assert canonical.canonical_text_bytes(b"") == b"\n"


def test_interior_blank_lines_preserved():
    """Only TRAILING newlines collapse; interior blank lines are preserved."""
    assert canonical.canonical_text_bytes(b"a\n\nb") == b"a\n\nb\n"
    assert canonical.canonical_text_bytes(b"a\n\n\nb\n") == b"a\n\n\nb\n"


def test_no_trailing_strip_of_meaningful_whitespace_content():
    """Trailing newlines collapse, but trailing spaces on the last line stay
    (only '\\n' runs are collapsed, not arbitrary whitespace)."""
    assert canonical.canonical_text_bytes(b"line  \n") == b"line  \n"
    assert canonical.canonical_text_bytes(b"line") == b"line\n"


# ---------------------------------------------------------------------------
# canonical_text_bytes: line-ending normalization
# ---------------------------------------------------------------------------


def test_crlf_and_cr_to_lf():
    """Both CRLF and lone CR normalize to LF."""
    assert canonical.canonical_text_bytes(b"a\r\nb") == b"a\nb\n"
    assert canonical.canonical_text_bytes(b"a\rb") == b"a\nb\n"
    assert canonical.canonical_text_bytes(b"a\r\nb\r\n") == b"a\nb\n"
    # mixed line endings
    assert canonical.canonical_text_bytes(b"a\r\nb\rc\nd") == b"a\nb\nc\nd\n"


def test_crlf_not_treated_as_two_newlines():
    """Regression guard: CRLF is ONE line ending, not two (a naive
    \\r->\\n then \\n\\n->\\n would corrupt content)."""
    assert canonical.canonical_text_bytes(b"x\r\ny") == b"x\ny\n"
    # not b"x\n\ny" and not b"x\ny"
    assert canonical.canonical_text_bytes(b"x\r\ny") != b"x\n\ny"
    assert canonical.canonical_text_bytes(b"x\r\ny") != b"x\ny"


# ---------------------------------------------------------------------------
# canonical_text_bytes: UTF-8 handling
# ---------------------------------------------------------------------------


def test_non_ascii_utf8_preserved():
    """Multibyte UTF-8 (incl. CJK, emoji) is preserved through normalization."""
    # 中文.txt content + emoji
    raw = "标题\n正文内容 ✅\n".encode("utf-8")
    out = canonical.canonical_text_bytes(raw)
    assert out == raw  # already canonical (LF, single trailing newline)
    assert "标题" in out.decode("utf-8")
    assert "✅" in out.decode("utf-8")


def test_non_ascii_utf8_crlf_normalized():
    raw = "标题\r\n正文\r".encode("utf-8")
    assert canonical.canonical_text_bytes(raw) == "标题\n正文\n".encode("utf-8")


def test_invalid_utf8_raises():
    """Invalid UTF-8 byte sequences must raise (strict decode), not be silently
    replaced or passed through — hashing corrupted bytes would diverge."""
    with pytest.raises(UnicodeDecodeError):
        canonical.canonical_text_bytes(b"\xff\xfe bad utf8")
    # lone continuation byte without lead
    with pytest.raises(UnicodeDecodeError):
        canonical.canonical_text_bytes(b"ok\x80")
    # truncated 2-byte sequence
    with pytest.raises(UnicodeDecodeError):
        canonical.canonical_text_bytes(b"\xc3")


def test_bom_not_special_cased():
    """A leading UTF-8 BOM is NOT stripped (canonicalization is byte-faithful;
    BOM handling is the caller's concern). This documents the rule precisely."""
    raw = b"\xef\xbb\xbfbody\n"
    assert canonical.canonical_text_bytes(raw) == raw


# ---------------------------------------------------------------------------
# canonical_json_bytes: determinism + determinism NOT assumed from formatting
# ---------------------------------------------------------------------------


def test_canonical_json_is_deterministic_across_insertion_order():
    """Same logical object => same canonical bytes regardless of key order."""
    a = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    b = {"nested": {"x": 1, "y": 2}, "a": 1, "b": 2}
    assert canonical.canonical_json_bytes(a) == canonical.canonical_json_bytes(b)


def test_canonical_json_uses_compact_separators_and_sorted_keys():
    obj = {"b": 2, "a": 1, "c": [3, 2, 1]}
    assert canonical.canonical_json_bytes(obj) == b'{"a":1,"b":2,"c":[3,2,1]}\n'


def test_canonical_json_ensure_ascii_false():
    """Non-ASCII is emitted as real UTF-8 chars (ensure_ascii=False), not
    \\uXXXX escapes — so the bytes are stable and human-readable."""
    obj = {"key": "中文"}
    assert canonical.canonical_json_bytes(obj) == '{"key":"中文"}\n'.encode("utf-8")


def test_canonical_json_has_single_trailing_newline():
    """JSON bytes also obey the single-trailing-newline rule."""
    out = canonical.canonical_json_bytes({"a": 1})
    assert out.endswith(b"\n")
    assert not out.endswith(b"\n\n")


def test_pretty_vs_compact_json_not_assumed_equal():
    """A pretty-printed JSON (indentation, spaces after separators) is NOT
    canonically equal to the compact form — canonical bytes are defined as
    sorted+compact, so two byte-different serializations of the SAME object
    that came from different pretty-printers do NOT match each other but BOTH
    match the one canonical form. This is the point of having a single rule."""
    obj = {"a": 1, "b": [1, 2]}
    compact = canonical.canonical_json_bytes(obj)
    pretty = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                        indent=2).encode("utf-8")
    # pretty != compact (formatting differs)
    assert pretty != compact
    # but canonicalizing the pretty form AGAIN yields the same compact bytes —
    # canonicalization is idempotent at the semantic level
    assert canonical.canonical_json_bytes(
        json.loads(pretty)
    ) == compact


def test_canonical_json_nested_object_keys_sorted():
    """Sorting is recursive: nested object keys are also sorted."""
    obj = {"outer": {"z": 1, "a": 2}}
    out = canonical.canonical_json_bytes(obj).decode("utf-8")
    assert out == '{"outer":{"a":2,"z":1}}\n'


def test_canonical_json_idempotent():
    """Canonicalizing the parsed result of canonical JSON yields identical
    bytes (idempotency)."""
    obj = {"b": 2, "a": 1, "list": [3, 2, 1]}
    once = canonical.canonical_json_bytes(obj)
    twice = canonical.canonical_json_bytes(json.loads(once))
    assert once == twice


# ---------------------------------------------------------------------------
# hash_file: matches canonical of written bytes
# ---------------------------------------------------------------------------


def test_hash_file_matches_canonical_of_written_bytes(tmp_path: Path):
    """hash_file re-hashes the CANONICAL bytes of whatever is on disk — so a
    file written with CRLF hashes identically to the LF version."""
    p = tmp_path / "spec.md"
    p.write_bytes(b"hello\n")  # already canonical
    assert canonical.hash_file(p) == canonical.sha256_hex(b"hello\n")

    p.write_bytes(b"hello\r\n")  # CRLF -> normalized to LF before hashing
    assert canonical.hash_file(p) == canonical.sha256_hex(b"hello\n")

    # multiple trailing newlines collapse before hashing
    p.write_bytes(b"hello\n\n\n")
    assert canonical.hash_file(p) == canonical.sha256_hex(b"hello\n")


def test_hash_file_non_ascii(tmp_path: Path):
    p = tmp_path / "note.md"
    raw = "标题\n正文\n".encode("utf-8")
    p.write_bytes(raw)
    assert canonical.hash_file(p) == canonical.sha256_hex(raw)


def test_hash_file_is_text_level_not_a_json_reserializer(tmp_path: Path):
    """hash_file canonicalizes at the TEXT level (CRLF->LF, single trailing
    newline) — it does NOT parse and re-serialize JSON. So two different
    byte-serializations of the SAME object (compact vs pretty) hash DIFFERENTLY
    when run through hash_file: the hash binds to the on-disk bytes (Spec §4.7:
    "hash over the final landed bytes"), which is the integrity property we want.

    The MCP never writes pretty JSON — it always writes canonical bytes via
    write_canonical_json — so a real artifact file's hash_file always equals the
    canonical-JSON hash. To get a format-INDEPENDENT hash you must go through
    canonical_json_bytes(obj) + sha256_hex, not hash_file on an arbitrary file.
    """
    obj = {"a": 1, "b": [1, 2, 3]}
    p_compact = tmp_path / "compact.json"
    p_pretty = tmp_path / "pretty.json"
    p_compact.write_bytes(canonical.canonical_json_bytes(obj))
    p_pretty.write_bytes(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=4).encode("utf-8")
    )
    # pretty != compact on disk => different hash_file (hash binds to bytes)
    assert canonical.hash_file(p_compact) != canonical.hash_file(p_pretty)

    # but the compact (canonical) file's hash equals the canonical-JSON hash:
    assert canonical.hash_file(p_compact) == canonical.sha256_hex(
        canonical.canonical_json_bytes(obj)
    )
    # and a format-independent hash of the object comes via canonical_json_bytes,
    # which is identical regardless of how the input was pretty-printed:
    assert canonical.sha256_hex(canonical.canonical_json_bytes(obj)) == \
        canonical.sha256_hex(canonical.canonical_json_bytes(json.loads(p_pretty.read_bytes())))


# ---------------------------------------------------------------------------
# sha256_hex: known vectors + lowercase guarantee
# ---------------------------------------------------------------------------


def test_sha256_hex_known_vectors():
    """NIST SHA-256 test vectors."""
    assert canonical.sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    assert canonical.sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_hex_is_lowercase_hex_64():
    h = canonical.sha256_hex(b"some bytes")
    assert len(h) == 64
    assert h == h.lower()
    assert all(c in "0123456789abcdef" for c in h)
    # canonical text bytes + sha256 == hashlib over the same canonical bytes
    assert canonical.sha256_hex(canonical.canonical_text_bytes(b"x\r\ny")) == \
        hashlib.sha256(b"x\ny\n").hexdigest()


# ---------------------------------------------------------------------------
# Atomic write invariants (cross-checked with the canonical rule)
# ---------------------------------------------------------------------------


def test_write_canonical_text_normalizes_on_write(tmp_path: Path):
    """write_canonical_text stores the CANONICAL form, not the raw input."""
    p = tmp_path / "f.txt"
    canonical.write_canonical_text(p, b"a\r\nb\n\n")
    assert p.read_bytes() == b"a\nb\n"


def test_write_canonical_json_is_canonical_and_atomic(tmp_path: Path):
    p = tmp_path / "state.json"
    obj = {"b": 2, "a": 1}
    canonical.write_canonical_json(p, obj)
    assert p.read_bytes() == canonical.canonical_json_bytes(obj)
    # no temp residue
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != p.name]
    assert leftovers == []
