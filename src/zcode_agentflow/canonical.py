"""Canonical-bytes infrastructure (Task 1.1b) — the SINGLE canonicalization rule
for the whole project.

Everything that is hashed, compared, or atomically persisted goes through these
functions:

- ``canonical_text_bytes``: UTF-8 strict decode, normalize CRLF/CR -> LF, ensure
  exactly one trailing newline.
- ``canonical_json_bytes``: ``json.dumps(ensure_ascii=False, sort_keys=True,
  separators=(",",":"))`` then ``canonical_text_bytes``.
- ``write_canonical_text`` / ``write_canonical_json``: atomic via
  temp-file + fsync + ``os.replace`` (concurrent readers never see a partial).
- ``sha256_hex`` / ``hash_file``: hashing always over canonical bytes.

Task 2.2 later adds the *exhaustive* edge-case suite against these same
functions — there is no second canonicalization implementation anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

__all__ = [
    "canonical_text_bytes",
    "canonical_json_bytes",
    "write_canonical_text",
    "write_canonical_json",
    "sha256_hex",
    "hash_file",
]


def canonical_text_bytes(raw: bytes) -> bytes:
    """Normalize arbitrary bytes to canonical text bytes.

    - decode strictly as UTF-8 (invalid UTF-8 raises UnicodeDecodeError);
    - normalize CRLF and lone CR to LF;
    - collapse any run of trailing newlines to exactly one trailing newline;
    - never strip interior blank lines; re-encode as UTF-8.
    """
    text = raw.decode("utf-8")          # strict; raises on invalid UTF-8
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.rstrip("\n") + "\n"     # exactly one trailing newline
    return text.encode("utf-8")


def canonical_json_bytes(obj) -> bytes:
    """Canonical JSON bytes: sorted keys, compact separators, UTF-8, one
    trailing newline. Deterministic for equal objects regardless of insertion
    order."""
    dumped = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical_text_bytes(dumped.encode("utf-8"))


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Write bytes to ``path`` atomically: temp file in the same dir, fsync the
    data (and the dir on POSIX) before ``os.replace`` so a crash never leaves a
    partial file visible to readers."""
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the same directory so os.replace is atomic on the
    # same filesystem (rename across devices is not atomic).
    fd, tmp_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # clean up the temp file on any failure; never leave a stale .tmp
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_canonical_text(path, data: bytes) -> None:
    """Atomically write canonical text bytes to ``path``."""
    _atomic_replace_bytes(Path(path), canonical_text_bytes(data))


def write_canonical_json(path, obj) -> None:
    """Atomically write canonical JSON bytes of ``obj`` to ``path``."""
    _atomic_replace_bytes(Path(path), canonical_json_bytes(obj))


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path) -> str:
    """SHA-256 (lowercase hex) over the canonical bytes of the file on disk."""
    raw = Path(path).read_bytes()
    return sha256_hex(canonical_text_bytes(raw))
