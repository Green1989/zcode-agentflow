# ZCode-AgentFlow v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v1 vertical slice of ZCode-AgentFlow — a single-task run that goes `NEW → ... → READY_TO_MERGE` with one Codex-driven plan, one Grok review, and two human gates, fully driven by a ZCode-loadable MCP server.

**Architecture:** Python MCP server exposes 9 workflow tools backed by a recoverable state machine (`state.json` + append-only `events.jsonl` + idempotent `operations/`). Three runners (Codex planner, Grok reviewer, verification+git-change policy) are called via subprocess with raw output capture. All hashes computed by MCP over canonical bytes. Four-roots model isolates plugin code, target repo, worktree, and control data.

**Tech Stack:** Python 3.11+, `mcp` SDK (stdio), `jsonschema`, `pyyaml`, `pytest`, `pytest-mock`. Subprocess calls to `codex exec` and `grok -p`. Runs on Windows/Git Bash (so all file I/O uses `pathlib`, forward-slash normalization, explicit UTF-8/LF).

**Reference spec:** `docs/superpowers/specs/2026-07-29-zcode-agentflow-design.md` (Approved for planning, v3.1). Section references below point into it.

---

## File Structure (what each file owns)

Decomposition is by responsibility. Each file has one job; later tasks build on earlier ones.

```
mcp/                                    ← plugin_root/mcp (the package)
├── __init__.py                         package marker (empty)
├── paths.py          four-roots model, path normalization & escape detection (spec §2.2)
├── config.py         load config.yaml, capability flags (spec §6)
├── io_atomic.py      atomic write (tmp+flush+os.replace), file lock, LF/UTF-8 canonicalization
├── hashes.py         canonical-byte SHA-256 (spec §4.7)
├── state.py          state.json snapshot + revision CAS + load/save (spec §3.4)
├── events.py         append-only events.jsonl, recoverable (spec §3.5)
├── operations.py     operation lifecycle + idempotency + commit protocol (spec §3.6)
├── persistence.py    Store facade tying state+events+operations together
├── gitops.py         git wrappers (worktree, commit, diff, HEAD, clean check)
├── schemas.py        load JSON Schemas + cross-field invariants (spec §4.5)
├── policy_verify.py  verification command policy runner (spec §5.2)
├── policy_change.py  git change policy runner (spec §5.3)
├── approval.py       trusted approval records + fallback handling (spec §5.4, §4.6)
├── runner_codex.py   codex exec wrapper, read-only, raw capture (spec §5.1 planner)
├── runner_grok.py    grok -p wrapper, read-only, raw capture (spec §5.1 reviewer)
└── server.py         MCP tool layer: 9 tools, hard checks, state transitions (spec §5.1)

schemas/                               ← plugin_root/schemas
├── spec-envelope.schema.json
├── plan-envelope.schema.json
├── execution-result.schema.json
└── review-result.schema.json

prompts/                               ← plugin_root/prompts
├── planner-draft.md
├── planner-plan.md
└── reviewer.md

tests/                                 ← test mirror of mcp/
├── conftest.py       fixtures: tmp control_root, fake git repo, monkeypatched runners
├── test_hashes.py
├── test_io_atomic.py
├── test_paths.py
├── test_state.py
├── test_events.py
├── test_operations.py
├── test_persistence.py
├── test_gitops.py
├── test_schemas.py
├── test_policy_verify.py
├── test_policy_change.py
├── test_approval.py
├── test_runner_codex.py
├── test_runner_grok.py
├── test_server_tools.py     the 9 tools, state transitions, hard checks
└── test_e2e_v1.py           spec §8.2 demo, with fake runners

config.yaml                            plugin config (spec §6)
requirements.txt
pytest.ini
.gitignore                             (already exists; extend later tasks if needed)
```

**Build order rationale:** Foundation first (no deps) → persistence (depends on io/hashes) → git/schemas/policy (depend on persistence primitives) → runners (depend on io/schemas) → server (depends on everything) → e2e. Each task is independently testable and committable.

---

## Conventions for every task

- **Run tests from repo root** with `python -m pytest <path> -v`.
- **Commit per task** with the shown message; never batch tasks.
- **Windows note:** use forward slashes in code paths; `pathlib.Path` handles the rest. Tests must not assume `os.sep`.
- **TDD:** red → green → commit, every task. The test file is created in step 1; the test runs and **fails** in step 2.

---

## Phase 0 — Project bootstrap

### Task 0.1: Python project skeleton

**Files:**
- Create: `requirements.txt`
- Create: `pytest.ini`
- Create: `mcp/__init__.py`
- Create: `mcp/__about__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write requirements**

Create `requirements.txt`:

```text
mcp>=1.2.0
jsonschema>=4.21.0
pyyaml>=6.0.1
```

Create `requirements-dev.txt`:

```text
-r requirements.txt
pytest>=8.0.0
pytest-mock>=3.12.0
```

- [ ] **Step 2: Write pytest config**

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = -ra
```

- [ ] **Step 3: Create package markers**

Create empty `mcp/__init__.py` and `tests/__init__.py`.

Create `mcp/__about__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Write the conftest with the canonical control_root fixture**

Create `tests/conftest.py`:

```python
import sys
import pathlib
import pytest

# Make the mcp package importable regardless of cwd.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def control_root(tmp_path):
    """Isolated control_root for a run; tests create runs/ underneath."""
    root = tmp_path / "control"
    root.mkdir()
    return root


@pytest.fixture
def fake_repo(tmp_path):
    """A real git repo on disk, for gitops tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo
```

- [ ] **Step 5: Verify the environment**

Run: `python -m pip install -r requirements-dev.txt && python -m pytest -v`
Expected: pytest collects 0 tests and exits 0 ("no tests ran" is fine).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini mcp/__init__.py mcp/__about__.py tests/__init__.py tests/conftest.py
git commit -m "chore: bootstrap python package, requirements, pytest config"
```

---


## Phase 1 — Foundation primitives

### Task 1.1: Canonical hashes (spec §4.7)

**Files:**
- Create: `mcp/hashes.py`
- Test: `tests/test_hashes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hashes.py`:

```python
import pathlib
from mcp import hashes


def test_canonical_hash_normalizes_crlf_to_lf():
    b_win = "line1\r\nline2\r\n".encode("utf-8")
    b_unix = "line1\nline2\n".encode("utf-8")
    assert hashes.sha256_canonical(b_win) == hashes.sha256_canonical(b_unix)


def test_canonical_hash_is_utf8_sha256_hex_lower():
    digest = hashes.sha256_canonical("hello".encode("utf-8"))
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_hash_file_uses_canonical_bytes(tmp_path):
    f = tmp_path / "x.md"
    f.write_bytes("a\r\nb\r\n".encode("utf-8"))
    assert hashes.hash_file(f) == hashes.sha256_canonical("a\nb\n".encode("utf-8"))


def test_hash_file_preserves_trailing_single_newline(tmp_path):
    f = tmp_path / "x.md"
    f.write_bytes("body\n".encode("utf-8"))
    assert hashes.hash_file(f) == hashes.sha256_canonical("body\n".encode("utf-8"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hashes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp.hashes'` (or import error).

- [ ] **Step 3: Write minimal implementation**

Create `mcp/hashes.py`:

```python
"""Canonical-byte hashing (spec §4.7).

All hashes in AgentFlow are computed by MCP over canonical bytes:
UTF-8, LF line endings, single trailing newline. Model-provided hashes
are never trusted (diagnostic only).
"""
import hashlib
import pathlib
import re

_TRAILING_WS = re.compile(rb"\r\n|\r")


def canonical_bytes(raw: bytes) -> bytes:
    """Normalize line endings to LF. Caller decides on trailing newline."""
    return _TRAILING_WS.sub(b"\n", raw)


def sha256_canonical(raw: bytes) -> str:
    return hashlib.sha256(canonical_bytes(raw)).hexdigest()


def hash_file(path: pathlib.Path) -> str:
    """Hash a file's bytes canonically. Does NOT alter the file on disk."""
    return sha256_canonical(path.read_bytes())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hashes.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/hashes.py tests/test_hashes.py
git commit -m "feat: canonical-byte sha256 hashing (spec §4.7)"
```

### Task 1.2: Atomic I/O + canonicalization (spec §3.4, §4.7)

**Files:**
- Create: `mcp/io_atomic.py`
- Test: `tests/test_io_atomic.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_io_atomic.py`:

```python
import pathlib
from mcp import io_atomic


def test_write_atomic_replaces_existing(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("OLD")
    io_atomic.write_atomic(f, "NEW")
    assert f.read_text() == "NEW"


def test_write_atomic_lf_canonicalizes_crlf(tmp_path):
    f = tmp_path / "s.md"
    io_atomic.write_atomic(f, "a\r\nb\r\n")
    assert f.read_bytes() == b"a\nb\n"


def test_write_atomic_json_roundtrip(tmp_path):
    f = tmp_path / "x.json"
    io_atomic.write_atomic_json(f, {"k": 1, "list": [3, 2, 1]})
    import json
    assert json.loads(f.read_text()) == {"k": 1, "list": [3, 2, 1]}


def test_read_json_missing_raises_filenotfound(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        io_atomic.read_json(tmp_path / "nope.json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_io_atomic.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/io_atomic.py`:

```python
"""Atomic file writes + canonicalization (spec §3.4, §4.7).

Writes go to a temp file in the same dir, fsync, then os.replace.
Text writes are canonicalized to UTF-8 / LF.
"""
import json
import os
import pathlib
import tempfile

from . import hashes


def _to_lf_bytes(text_or_bytes) -> bytes:
    if isinstance(text_or_bytes, str):
        text_or_bytes = text_or_bytes.encode("utf-8")
    return hashes.canonical_bytes(text_or_bytes)


def write_atomic(path: pathlib.Path, content) -> None:
    """Atomically write content (str or bytes), LF-canonical."""
    data = _to_lf_bytes(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except Exception:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def write_atomic_json(path: pathlib.Path, obj) -> None:
    write_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def read_json(path: pathlib.Path):
    import json as _json
    with open(path, "r", encoding="utf-8") as fh:
        return _json.load(fh)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_io_atomic.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/io_atomic.py tests/test_io_atomic.py
git commit -m "feat: atomic LF-canonical file writes (spec §3.4, §4.7)"
```

### Task 1.3: Four-roots model + path escape detection (spec §2.2)

**Files:**
- Create: `mcp/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_paths.py`:

```python
import pathlib
import pytest
from mcp import paths


def test_roots_normalizes_to_absolute():
    r = paths.Roots(
        plugin_root="plugin",
        target_repo_root="repo",
        run_worktree_root="wt",
        control_root="ctrl",
    )
    assert r.plugin_root.is_absolute()
    assert r.run_worktree_root.is_absolute()


def test_is_within_true_for_subpath(tmp_path):
    base = tmp_path / "wt"
    base.mkdir()
    f = base / "src" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert paths.is_within(f, base) is True


def test_is_within_false_for_escape(tmp_path):
    base = tmp_path / "wt"
    base.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert paths.is_within(outside / "x", base) is False


def test_is_within_false_for_dotdot(tmp_path):
    base = tmp_path / "wt"
    base.mkdir()
    attacker = base / ".." / "secret"
    assert paths.is_within(attacker, base) is False


def test_roots_to_dict_roundtrip_absolute_strings(tmp_path):
    r = paths.Roots(str(tmp_path / "p"), str(tmp_path / "r"), str(tmp_path / "w"), str(tmp_path / "c"))
    d = r.to_dict()
    assert all(isinstance(v, str) for v in d.values())
    r2 = paths.Roots(**d)
    assert r2 == r
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paths.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/paths.py`:

```python
"""Four-roots model + path escape detection (spec §2.2).

plugin_root        read-only plugin code
target_repo_root   business repo, git operations only
run_worktree_root  ZCode edits business code here
control_root       state/events/operations/reviews, outside the repo
"""
import dataclasses
import pathlib


@dataclasses.dataclass(frozen=True)
class Roots:
    plugin_root: pathlib.Path
    target_repo_root: pathlib.Path
    run_worktree_root: pathlib.Path
    control_root: pathlib.Path

    def __post_init__(self):
        object.__setattr__(self, "plugin_root", _norm(self.plugin_root))
        object.__setattr__(self, "target_repo_root", _norm(self.target_repo_root))
        object.__setattr__(self, "run_worktree_root", _norm(self.run_worktree_root))
        object.__setattr__(self, "control_root", _norm(self.control_root))

    def to_dict(self) -> dict:
        return {
            "plugin_root": str(self.plugin_root),
            "target_repo_root": str(self.target_repo_root),
            "run_worktree_root": str(self.run_worktree_root),
            "control_root": str(self.control_root),
        }


def _norm(p) -> pathlib.Path:
    p = pathlib.Path(p)
    if not p.is_absolute():
        p = p.resolve()
    # Collapse .. and . lexically without touching the filesystem.
    return pathlib.Path(*p.parts).resolve(strict=False)


def is_within(path, base) -> bool:
    """True if `path` resolves to a location inside `base` (escape-proof)."""
    path = pathlib.Path(path).resolve(strict=False)
    base = pathlib.Path(base).resolve(strict=False)
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paths.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/paths.py tests/test_paths.py
git commit -m "feat: four-roots model + path escape detection (spec §2.2)"
```


## Phase 2 — Recoverable persistence

### Task 2.1: state.json snapshot + revision CAS (spec §3.4)

**Files:**
- Create: `mcp/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_state.py`:

```python
import pytest
from mcp import state as st


def test_new_state_has_revision_1(control_root):
    s = st.State.new(
        run_id="r1",
        roots=st.Roots("p", "tr", "wt", str(control_root)),
    )
    assert s.revision == 1
    assert s.state == "NEW"


def test_save_and_load_roundtrip(control_root):
    s = st.State.new(run_id="r1", roots=st.Roots("p", "tr", "wt", str(control_root)))
    s.save()
    loaded = st.State.load(control_root)
    assert loaded == s


def test_transition_bumps_revision_and_records_previous(control_root):
    s = st.State.new(run_id="r1", roots=st.Roots("p", "tr", "wt", str(control_root)))
    s2 = s.transition(to="PLANNING", expected_revision=1)
    assert s2.revision == 2
    assert s2.previous_state == "NEW"


def test_transition_rejects_stale_expected_revision(control_root):
    s = st.State.new(run_id="r1", roots=st.Roots("p", "tr", "wt", str(control_root)))
    s.save()
    with pytest.raises(st.CasError):
        s.transition(to="PLANNING", expected_revision=99)


def test_load_missing_raises(control_root):
    with pytest.raises(FileNotFoundError):
        st.State.load(control_root)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/state.py`:

```python
"""state.json snapshot + revision CAS (spec §3.4)."""
import dataclasses
import datetime as _dt
import pathlib
import copy

from . import io_atomic, paths as _paths


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


class CasError(Exception):
    """expected_revision does not match current revision."""


@dataclasses.dataclass
class State:
    run_id: str
    revision: int
    state: str
    previous_state: str
    roots: _paths.Roots
    spec_sha256: str = ""
    plan_sha256: str = ""
    task_manifest_sha256: str = ""
    plan_version: int = 0
    current_task: str = ""
    completed_tasks: list = dataclasses.field(default_factory=list)
    base_commit: str = ""
    task_start_commit: str = ""
    last_reviewed_commit: str = ""
    review_round: int = 0
    repair_round: int = 0
    updated_at: str = ""

    @classmethod
    def new(cls, run_id, roots):
        return cls(
            run_id=run_id,
            revision=1,
            state="NEW",
            previous_state="",
            roots=roots if isinstance(roots, _paths.Roots) else _paths.Roots(**roots),
            updated_at=_now_iso(),
        )

    @classmethod
    def load(cls, control_root: pathlib.Path):
        data = io_atomic.read_json(pathlib.Path(control_root) / "state.json")
        d = dict(data)
        d["roots"] = _paths.Roots(**d["roots"])
        return cls(**d)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "revision": self.revision,
            "state": self.state,
            "previous_state": self.previous_state,
            "roots": self.roots.to_dict(),
            "spec_sha256": self.spec_sha256,
            "plan_sha256": self.plan_sha256,
            "task_manifest_sha256": self.task_manifest_sha256,
            "plan_version": self.plan_version,
            "current_task": self.current_task,
            "completed_tasks": list(self.completed_tasks),
            "base_commit": self.base_commit,
            "task_start_commit": self.task_start_commit,
            "last_reviewed_commit": self.last_reviewed_commit,
            "review_round": self.review_round,
            "repair_round": self.repair_round,
            "updated_at": self.updated_at,
        }

    def save(self) -> None:
        self.updated_at = _now_iso()
        io_atomic.write_atomic_json(
            pathlib.Path(self.roots.control_root) / "state.json", self.to_dict()
        )

    def transition(self, to: str, expected_revision: int) -> "State":
        if expected_revision != self.revision:
            raise CasError(
                f"expected revision {expected_revision}, current {self.revision}"
            )
        nxt = copy.deepcopy(self)
        nxt.previous_state = nxt.state
        nxt.state = to
        nxt.revision = self.revision + 1
        return nxt

    def __eq__(self, other):
        return isinstance(other, State) and self.to_dict() == other.to_dict()


# Re-export so tests can import Roots from mcp.state
Roots = _paths.Roots
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_state.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/state.py tests/test_state.py
git commit -m "feat: state.json snapshot with revision CAS (spec §3.4)"
```

### Task 2.2: events.jsonl append + recovery (spec §3.5)

**Files:**
- Create: `mcp/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_events.py`:

```python
import json
from mcp import events


def test_append_writes_one_jsonl_line(control_root):
    log = events.EventLog(control_root)
    log.append(seq=1, at="t1", operation_id="op1", frm="NEW", to="PLANNING",
               reason="draft", revision=2, state_after_path="operations/op1/state_after.json",
               state_after_sha="abc")
    lines = (control_root / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["to"] == "PLANNING"
    assert rec["state_after_sha256"] == "abc"


def test_latest_returns_last_state_after_pointer(control_root):
    log = events.EventLog(control_root)
    log.append(seq=1, at="t1", operation_id="op1", frm="NEW", to="A",
               reason="x", revision=1, state_after_path="p1", state_after_sha="h1")
    log.append(seq=2, at="t2", operation_id="op2", frm="A", to="B",
               reason="y", revision=2, state_after_path="p2", state_after_sha="h2")
    latest = log.latest()
    assert latest["state_after_path"] == "p2"


def test_latest_empty_returns_none(control_root):
    assert events.EventLog(control_root).latest() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_events.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/events.py`:

```python
"""Append-only events.jsonl (spec §3.5)."""
import json
import os
import pathlib


class EventLog:
    def __init__(self, control_root: pathlib.Path):
        self.path = pathlib.Path(control_root) / "events.jsonl"

    def append(self, *, seq, at, operation_id, frm, to, reason, revision,
               state_after_path, state_after_sha) -> None:
        record = {
            "seq": seq,
            "at": at,
            "operation_id": operation_id,
            "from": frm,
            "to": to,
            "reason": reason,
            "revision": revision,
            "state_after_path": state_after_path,
            "state_after_sha256": state_after_sha,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def latest(self):
        if not self.path.exists():
            return None
        last = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
        return last
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_events.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/events.py tests/test_events.py
git commit -m "feat: append-only events.jsonl with recovery pointer (spec §3.5)"
```

### Task 2.3: operations lifecycle + idempotency + commit protocol (spec §3.6)

**Files:**
- Create: `mcp/operations.py`
- Test: `tests/test_operations.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_operations.py`:

```python
import pytest
from mcp import operations as ops


def test_prepare_creates_dir_with_prepared(control_root):
    m = ops.Manager(control_root)
    o = m.prepare("op1", request={"goal": "x"})
    assert o.status == "PREPARED"
    assert (control_root / "operations" / "op1" / "request.json").exists()
    assert (control_root / "operations" / "op1" / "request_sha256").exists()


def test_idempotent_commit_returns_same_result(control_root):
    m = ops.Manager(control_root)
    o = m.prepare("op1", request={"goal": "x"})
    o.mark_running()
    o.record_result({"ok": True})
    o.write_state_after({"state": "A"})
    r1 = o.commit(from_state="A", to_state="B", reason="t", state_after={"state": "B"})
    o2 = m.prepare("op1", request={"goal": "x"})
    r2 = o2.commit(from_state="A", to_state="B", reason="t", state_after={"state": "B"})
    assert r1 == r2  # idempotent replay


def test_conflicting_request_sha_rejected(control_root):
    m = ops.Manager(control_root)
    m.prepare("op1", request={"goal": "x"}).mark_running()
    with pytest.raises(ops.OperationConflict):
        m.prepare("op1", request={"goal": "DIFFERENT"})


def test_running_with_result_can_continue_commit(control_root):
    m = ops.Manager(control_root)
    o = m.prepare("op1", request={"k": 1})
    o.mark_running()
    o.record_result({"ok": True})          # simulate crash AFTER result written
    o2 = m.open_existing("op1")            # recovery reopen
    assert o2.status == "RUNNING"
    o2.write_state_after({"state": "A"})
    o2.commit(from_state="A", to_state="B", reason="r", state_after={"state": "B"})
    assert m.open_existing("op1").status == "COMMITTED"


def test_running_without_result_is_unknown(control_root):
    m = ops.Manager(control_root)
    m.prepare("op1", request={"k": 1}).mark_running()   # crash mid-execution
    o2 = m.open_existing("op1")
    assert o2.status == "UNKNOWN"  # cannot tell if external op completed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_operations.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/operations.py`:

```python
"""Operation lifecycle, idempotency, commit protocol (spec §3.6).

statuses: PREPARED -> RUNNING -> COMMITTED | UNKNOWN
Recovery: see README-style rules in open_existing().
"""
import json
import pathlib

from . import hashes, io_atomic


class OperationConflict(Exception):
    pass


class Operation:
    def __init__(self, manager: "Manager", op_id: str):
        self.manager = manager
        self.op_id = op_id
        self.dir = manager.root / "operations" / op_id

    @property
    def status_path(self) -> pathlib.Path:
        return self.dir / "status"

    @property
    def status(self) -> str:
        if not self.status_path.exists():
            return "PREPARED"
        return self.status_path.read_text(encoding="utf-8").strip()

    def _set_status(self, s: str) -> None:
        io_atomic.write_atomic(self.status_path, s)

    def mark_running(self) -> None:
        if self.status == "PREPARED":
            self._set_status("RUNNING")

    def record_result(self, result: dict) -> None:
        io_atomic.write_atomic_json(self.dir / "result.json", result)
        io_atomic.write_atomic(self.dir / "raw" / "stdout.txt", result.get("stdout", ""))
        io_atomic.write_atomic(self.dir / "raw" / "stderr.txt", result.get("stderr", ""))

    def has_result(self) -> bool:
        return (self.dir / "result.json").exists()

    def write_state_after(self, state: dict) -> str:
        io_atomic.write_atomic_json(self.dir / "state_after.json", state)
        sha = hashes.sha256_canonical(
            json.dumps(state, sort_keys=True).encode("utf-8")
        )
        io_atomic.write_atomic(self.dir / "state_after_sha256", sha)
        return sha

    def result(self) -> dict:
        return io_atomic.read_json(self.dir / "result.json")

    def commit(self, *, from_state, to_state, reason, state_after) -> dict:
        """Finalize: write state_after, append event, mark COMMITTED.

        Idempotent: if already COMMITTED with same request, return stored result.
        """
        if self.status == "COMMITTED":
            return self.result()
        sha = self.write_state_after(state_after)
        # Event append delegated to Manager (which owns EventLog + revision).
        self.manager._emit_event(self, from_state, to_state, reason, sha, state_after)
        self._set_status("COMMITTED")
        return self.result()


class Manager:
    def __init__(self, control_root: pathlib.Path, event_log=None):
        self.root = pathlib.Path(control_root)
        self.event_log = event_log  # injected in persistence.py facade
        self._seq = 0

    def prepare(self, op_id: str, request: dict) -> Operation:
        o = Operation(self, op_id)
        req_sha = hashes.sha256_canonical(
            json.dumps(request, sort_keys=True).encode("utf-8")
        )
        if o.dir.exists():
            existing_sha = (o.dir / "request_sha256").read_text(encoding="utf-8").strip()
            if existing_sha != req_sha:
                raise OperationConflict(
                    f"operation {op_id} exists with a different request"
                )
            return self.open_existing(op_id)
        o.dir.mkdir(parents=True)
        (o.dir / "raw").mkdir()
        io_atomic.write_atomic_json(o.dir / "request.json", request)
        io_atomic.write_atomic(o.dir / "request_sha256", req_sha)
        o._set_status("PREPARED")
        return o

    def open_existing(self, op_id: str) -> Operation:
        o = Operation(self, op_id)
        if not o.dir.exists():
            raise FileNotFoundError(op_id)
        # Recovery classification:
        current = o.status
        if current == "RUNNING":
            o._set_status("UNKNOWN" if not o.has_result() else "RUNNING")
        return o

    def _emit_event(self, op, from_state, to_state, reason, sha, state_after):
        if self.event_log is None:
            return
        self._seq += 1
        import datetime as _dt
        self.event_log.append(
            seq=self._seq,
            at=_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
            operation_id=op.op_id,
            frm=from_state,
            to=to_state,
            reason=reason,
            revision=state_after.get("revision", 0),
            state_after_path=f"operations/{op.op_id}/state_after.json",
            state_after_sha=sha,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_operations.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/operations.py tests/test_operations.py
git commit -m "feat: operation lifecycle + idempotency + commit protocol (spec §3.6)"
```

### Task 2.4: Persistence facade (ties state + events + operations)

**Files:**
- Create: `mcp/persistence.py`
- Test: `tests/test_persistence.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_persistence.py`:

```python
from mcp import persistence as p
from mcp import state as st


def _make_store(control_root):
    s = st.State.new(run_id="r1", roots=st.Roots("p", "tr", "wt", str(control_root)))
    s.save()
    return p.Store(control_root)


def test_commit_transition_advances_state_events_and_revision(control_root):
    store = _make_store(control_root)
    cur = store.current()
    nxt = cur.transition(to="PLANNING", expected_revision=cur.revision)
    store.commit_transition(
        operation_id="op1",
        request={"goal": "x"},
        next_state=nxt,
        reason="draft",
    )
    reloaded = store.current()
    assert reloaded.state == "PLANNING"
    assert reloaded.revision == 2
    assert (control_root / "events.jsonl").exists()
    assert (control_root / "operations" / "op1" / "status").read_text().strip() == "COMMITTED"


def test_recover_from_events_when_state_corrupt(control_root):
    store = _make_store(control_root)
    cur = store.current()
    nxt = cur.transition(to="PLANNING", expected_revision=cur.revision)
    store.commit_transition("op1", {"g": 1}, nxt, "draft")
    # corrupt state.json
    (control_root / "state.json").write_text("{ broken")
    recovered = store.recover()
    assert recovered.state == "PLANNING"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/persistence.py`:

```python
"""Store facade: state snapshot + events + operations, with recovery (spec §3.5, §3.6)."""
import json
import pathlib

from . import events, io_atomic, operations as ops, state as st


class StateCorruptError(Exception):
    pass


class Store:
    def __init__(self, control_root: pathlib.Path):
        self.control_root = pathlib.Path(control_root)
        self.event_log = events.EventLog(self.control_root)
        self.operations = ops.Manager(self.control_root, event_log=self.event_log)

    def current(self) -> st.State:
        return st.State.load(self.control_root)

    def commit_transition(self, operation_id, request, next_state: st.State, reason: str) -> dict:
        """Atomically: prepare/run/result/event/state.replace/commit."""
        op = self.operations.prepare(operation_id, request)
        if op.status == "COMMITTED":
            return op.result()
        op.mark_running()
        op.record_result({"ok": True, "transition": reason})
        op.commit(
            from_state=next_state.previous_state,
            to_state=next_state.state,
            reason=reason,
            state_after=next_state.to_dict(),
        )
        next_state.save()
        return op.result()

    def recover(self) -> st.State:
        """Rebuild state.json from events.jsonl if it is corrupt."""
        latest = self.event_log.latest()
        if latest is None:
            raise StateCorruptError("no events to recover from")
        snapshot_path = self.control_root / latest["state_after_path"]
        snapshot = io_atomic.read_json(snapshot_path)
        actual_sha = self._sha_of_file(snapshot_path)
        if actual_sha != latest["state_after_sha256"]:
            raise StateCorruptError("state_after snapshot hash mismatch")
        d = dict(snapshot)
        from . import paths as _paths
        d["roots"] = _paths.Roots(**d["roots"])
        rebuilt = st.State(**d)
        rebuilt.save()
        return rebuilt

    @staticmethod
    def _sha_of_file(path: pathlib.Path) -> str:
        from . import hashes
        # state_after.json was written by write_atomic_json (canonical), so hash canonically.
        return hashes.sha256_canonical(path.read_bytes())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/persistence.py tests/test_persistence.py
git commit -m "feat: persistence facade with event-based recovery (spec §3.5-§3.6)"
```

---


## Phase 3 — Git, schemas, policies, approval

### Task 3.1: Git operations wrapper (spec §2.2, §5.3)

**Files:**
- Create: `mcp/gitops.py`
- Test: `tests/test_gitops.py`

Uses real git on a temp repo (the `fake_repo` fixture). No network.

- [ ] **Step 1: Write the failing test**

Create `tests/test_gitops.py`:

```python
import subprocess
from mcp import gitops


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def test_is_git_repo_false_for_plain_dir(tmp_path):
    assert gitops.is_git_repo(tmp_path) is False


def test_create_worktree_outside_repo(fake_repo):
    _git(fake_repo, "init")
    _git(fake_repo, "config", "user.email", "t@t")
    _git(fake_repo, "config", "user.name", "t")
    (fake_repo / "f.txt").write_text("hi")
    _git(fake_repo, "add", "-A")
    _git(fake_repo, "commit", "-m", "init")
    wt = fake_repo.parent / "wt"
    base = gitops.create_worktree(target_repo=fake_repo, worktree_path=wt, branch="agentflow/r1")
    assert wt.exists()
    assert base  # a commit sha


def test_head_returns_commit(fake_repo):
    _git(fake_repo, "init")
    _git(fake_repo, "config", "user.email", "t@t")
    _git(fake_repo, "config", "user.name", "t")
    (fake_repo / "a.txt").write_text("x")
    _git(fake_repo, "add", "-A")
    _git(fake_repo, "commit", "-m", "i")
    h = gitops.head(fake_repo)
    assert len(h) == 40


def test_is_clean_true_then_false(fake_repo):
    _git(fake_repo, "init")
    _git(fake_repo, "config", "user.email", "t@t")
    _git(fake_repo, "config", "user.name", "t")
    (fake_repo / "a.txt").write_text("x")
    _git(fake_repo, "add", "-A")
    _git(fake_repo, "commit", "-m", "i")
    assert gitops.is_clean(fake_repo) is True
    (fake_repo / "a.txt").write_text("dirty")
    assert gitops.is_clean(fake_repo) is False


def test_untracked_files_listed(fake_repo):
    _git(fake_repo, "init")
    _git(fake_repo, "config", "user.email", "t@t")
    _git(fake_repo, "config", "user.name", "t")
    (fake_repo / "a.txt").write_text("x")
    _git(fake_repo, "add", "-A")
    _git(fake_repo, "commit", "-m", "i")
    (fake_repo / "new.txt").write_text("u")
    assert gitops.untracked_files(fake_repo) == ["new.txt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gitops.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/gitops.py`:

```python
"""Git operations (spec §2.2, §5.3). Thin subprocess wrappers."""
import pathlib
import subprocess


def _run(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def is_git_repo(path) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def create_worktree(*, target_repo, worktree_path, branch: str) -> str:
    """Create a worktree at worktree_path (outside the repo) on a new branch.
    Returns the base commit sha."""
    target_repo = pathlib.Path(target_repo)
    worktree_path = pathlib.Path(worktree_path)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    base = head(target_repo)
    subprocess.run(
        ["git", "-C", str(target_repo), "worktree", "add", "-b", branch, str(worktree_path)],
        check=True, capture_output=True,
    )
    return base


def head(repo) -> str:
    return _run(repo, "rev-parse", "HEAD")


def is_clean(repo) -> bool:
    out = _run(repo, "status", "--porcelain")
    return out == ""


def untracked_files(repo) -> list:
    out = _run(repo, "status", "--porcelain")
    files = []
    for line in out.splitlines():
        if line.startswith("?? "):
            files.append(line[3:].strip().strip('"'))
    return files


def changed_files_between(repo, base_commit, head_commit) -> list:
    out = _run(repo, "diff", "--name-only", base_commit, head_commit)
    return [l for l in out.splitlines() if l]


def diff_stat(repo, base_commit, head_commit) -> str:
    return _run(repo, "diff", "--stat", base_commit, head_commit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gitops.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/gitops.py tests/test_gitops.py
git commit -m "feat: git operations wrapper (spec §2.2, §5.3)"
```

### Task 3.2: JSON Schema loading + cross-field invariants (spec §4.5)

**Files:**
- Create: `schemas/review-result.schema.json`
- Create: `mcp/schemas.py`
- Test: `tests/test_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_schemas.py`:

```python
import pytest
from mcp import schemas


def _base_review(**over):
    d = {"decision": "PASS", "spec_compliance": True, "plan_compliance": True,
         "tests_sufficient": True, "issues": [], "replan_required": False,
         "base_commit": "a", "head_commit": "b", "task_id": "T1", "run_id": "r1",
         "spec_sha256": "s", "plan_sha256": "p", "task_manifest_sha256": "m", "plan_version": 1}
    d.update(over)
    return d


def test_review_pass_with_issues_is_invalid():
    with pytest.raises(schemas.InvariantError):
        schemas.validate_review(_base_review(decision="PASS", issues=[{"id": "R1"}]))


def test_review_pass_clean_is_valid():
    schemas.validate_review(_base_review())  # no raise


def test_review_fail_without_issues_is_invalid():
    with pytest.raises(schemas.InvariantError):
        schemas.validate_review(_base_review(decision="FAIL", issues=[]))


def test_review_replan_requires_flag():
    with pytest.raises(schemas.InvariantError):
        schemas.validate_review(_base_review(decision="REPLAN", replan_required=False))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write the schema file**

Create `schemas/review-result.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "review-result",
  "type": "object",
  "required": ["run_id", "task_id", "base_commit", "head_commit",
               "spec_sha256", "plan_sha256", "task_manifest_sha256", "plan_version",
               "decision", "spec_compliance", "plan_compliance", "tests_sufficient",
               "issues", "replan_required"],
  "properties": {
    "run_id": {"type": "string"},
    "task_id": {"type": "string"},
    "base_commit": {"type": "string"},
    "head_commit": {"type": "string"},
    "spec_sha256": {"type": "string"},
    "plan_sha256": {"type": "string"},
    "task_manifest_sha256": {"type": "string"},
    "plan_version": {"type": "integer"},
    "decision": {"enum": ["PASS", "FAIL", "REPLAN", "BLOCKED"]},
    "spec_compliance": {"type": "boolean"},
    "plan_compliance": {"type": "boolean"},
    "tests_sufficient": {"type": "boolean"},
    "issues": {"type": "array"},
    "replan_required": {"type": "boolean"}
  }
}
```

- [ ] **Step 4: Write minimal implementation**

Create `mcp/schemas.py`:

```python
"""JSON Schema loading + cross-field invariants (spec §4.5)."""
import json
import pathlib

import jsonschema

_SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[1] / "schemas"


class SchemaError(Exception):
    pass


class InvariantError(Exception):
    pass


def _load(name: str):
    with open(_SCHEMA_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_against(obj, schema_name: str) -> None:
    schema = _load(schema_name)
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError as e:
        raise SchemaError(f"{schema_name} validation failed: {e.message}") from e


def _check_review_invariants(r: dict) -> None:
    d = r["decision"]
    if d == "PASS":
        if r["issues"]:
            raise InvariantError("PASS with non-empty issues")
        if not (r["spec_compliance"] and r["plan_compliance"] and r["tests_sufficient"]):
            raise InvariantError("PASS requires all compliance flags true")
    elif d == "FAIL":
        if not r["issues"]:
            raise InvariantError("FAIL requires at least one issue")
    elif d == "REPLAN":
        if not r["replan_required"]:
            raise InvariantError("REPLAN requires replan_required=true")


def validate_review(obj: dict) -> None:
    validate_against(obj, "review-result.schema.json")
    _check_review_invariants(obj)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add schemas/review-result.schema.json mcp/schemas.py tests/test_schemas.py
git commit -m "feat: review schema + cross-field invariants (spec §4.5)"
```

### Task 3.3: Verification command policy runner (spec §5.2)

**Files:**
- Create: `mcp/policy_verify.py`
- Test: `tests/test_policy_verify.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy_verify.py`:

```python
import pytest
from mcp import policy_verify as pv


def test_runs_allowlisted_program_in_worktree(tmp_path):
    wt = tmp_path / "wt"; wt.mkdir()
    res = pv.run(verification={"program": "python", "args": ["-c", "print(1)"],
                               "cwd": ".", "timeout_sec": 10},
                 worktree_root=wt, program_allowlist=["python"])
    assert res["exit_code"] == 0


def test_rejects_program_not_in_allowlist(tmp_path):
    with pytest.raises(pv.PolicyViolation):
        pv.run(verification={"program": "rm", "args": ["-rf", "/"], "cwd": "."},
               worktree_root=tmp_path, program_allowlist=["python"])


def test_rejects_cwd_escape(tmp_path):
    wt = tmp_path / "wt"; wt.mkdir()
    with pytest.raises(pv.PolicyViolation):
        pv.run(verification={"program": "python", "args": [], "cwd": ".."},
               worktree_root=wt, program_allowlist=["python"])


def test_secret_redaction_in_stdout(tmp_path):
    wt = tmp_path / "wt"; wt.mkdir()
    res = pv.run(verification={"program": "python",
                               "args": ["-c", "print('token=sk-live-12345')"],
                               "cwd": ".", "timeout_sec": 10},
                 worktree_root=wt, program_allowlist=["python"])
    assert "sk-live-12345" not in res["stdout"]
    assert "REDACTED" in res["stdout"]


def test_nonzero_exit_recorded_not_raised(tmp_path):
    wt = tmp_path / "wt"; wt.mkdir()
    res = pv.run(verification={"program": "python",
                               "args": ["-c", "import sys; sys.exit(3)"],
                               "cwd": ".", "timeout_sec": 10},
                 worktree_root=wt, program_allowlist=["python"])
    assert res["exit_code"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_policy_verify.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/policy_verify.py`:

```python
"""Verification command policy runner (spec §5.2).

Schema: program + args + cwd + timeout_sec. shell=False. cwd must stay
inside worktree_root. Program must be in allowlist. Secret redaction applied.
Non-zero exit is recorded, not raised (caller decides NEEDS_HUMAN).
"""
import pathlib
import re
import subprocess

from . import paths

_SECRET = re.compile(r"(sk-|token|secret|password|api[_-]?key)[=:][\w\-]+", re.IGNORECASE)


class PolicyViolation(Exception):
    pass


def _redact(text: str) -> str:
    return _SECRET.sub("REDACTED", text)


def run(*, verification: dict, worktree_root, program_allowlist: list) -> dict:
    program = verification["program"]
    args = verification.get("args", [])
    cwd_rel = verification.get("cwd", ".")
    timeout = verification.get("timeout_sec", 120)

    if program not in program_allowlist:
        raise PolicyViolation(f"program {program!r} not in allowlist")

    cwd = (pathlib.Path(worktree_root) / cwd_rel).resolve(strict=False)
    if not paths.is_within(cwd, worktree_root):
        raise PolicyViolation("cwd escapes worktree_root")

    try:
        proc = subprocess.run(
            [program, *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout, shell=False,
        )
        return {
            "command": f"{program} {' '.join(args)}",
            "exit_code": proc.returncode,
            "stdout": _redact(proc.stdout),
            "stderr": _redact(proc.stderr),
        }
    except subprocess.TimeoutExpired as e:
        return {"command": program, "exit_code": -1, "stdout": "", "stderr": f"timeout: {e}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_policy_verify.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/policy_verify.py tests/test_policy_verify.py
git commit -m "feat: verification command policy runner (spec §5.2)"
```

### Task 3.4: Git change policy runner (spec §5.3)

**Files:**
- Create: `mcp/policy_change.py`
- Test: `tests/test_policy_change.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy_change.py`:

```python
import subprocess
from mcp import policy_change as pc


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _seed(repo):
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "keep.txt").write_text("1"); (repo / "requirements.txt").write_text("pkg==1")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "i")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_authorized_delete_allowed(fake_repo):
    base = _seed(fake_repo)
    (fake_repo / "keep.txt").unlink()
    _git(fake_repo, "add", "-A"); _git(fake_repo, "commit", "-m", "del")
    head = _git(fake_repo, "rev-parse", "HEAD").strip()
    audit = pc.audit(worktree=fake_repo, base_commit=base, head_commit=head,
                     declared_files=["keep.txt"],
                     planned_changes={"may_delete_files": ["keep.txt"]})
    assert audit["needs_human"] is False


def test_unauthorized_delete_needs_human(fake_repo):
    base = _seed(fake_repo)
    (fake_repo / "keep.txt").unlink()
    _git(fake_repo, "add", "-A"); _git(fake_repo, "commit", "-m", "del")
    head = _git(fake_repo, "rev-parse", "HEAD").strip()
    audit = pc.audit(worktree=fake_repo, base_commit=base, head_commit=head,
                     declared_files=[], planned_changes={"may_delete_files": []})
    assert audit["needs_human"] is True
    assert "unauthorized_delete" in audit["reasons"]


def test_dependency_change_unauthorized_needs_human(fake_repo):
    base = _seed(fake_repo)
    (fake_repo / "requirements.txt").write_text("pkg==2")
    _git(fake_repo, "add", "-A"); _git(fake_repo, "commit", "-m", "dep")
    head = _git(fake_repo, "rev-parse", "HEAD").strip()
    audit = pc.audit(worktree=fake_repo, base_commit=base, head_commit=head,
                     declared_files=[], planned_changes={"may_add_dependencies": False})
    assert audit["needs_human"] is True
    assert "dependency_files_changed" in audit["reasons"]


def test_undeclared_file_needs_human(fake_repo):
    base = _seed(fake_repo)
    (fake_repo / "surprise.txt").write_text("x")
    _git(fake_repo, "add", "-A"); _git(fake_repo, "commit", "-m", "s")
    head = _git(fake_repo, "rev-parse", "HEAD").strip()
    audit = pc.audit(worktree=fake_repo, base_commit=base, head_commit=head,
                     declared_files=["keep.txt"], planned_changes={})
    assert audit["needs_human"] is True
    assert "undeclared_files_changed" in audit["reasons"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_policy_change.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/policy_change.py`:

```python
"""Git change policy runner (spec §5.3).

Inspects git diff between task_start_commit and HEAD. Flags:
unauthorized delete, dependency files changed (unless authorized),
migrations, undeclared files. Returns whether NEEDS_HUMAN is required.
"""
from . import gitops

_DEPENDENCY_FILES = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py",
    "package.json", "package-lock.json", "yarn.lock", "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle",
}


def audit(*, worktree, base_commit, head_commit, declared_files, planned_changes) -> dict:
    changed = gitops.changed_files_between(worktree, base_commit, head_commit)
    deleted = [f for f in changed if not (worktree / f).exists()]
    dep_changed = [f for f in changed if f in _DEPENDENCY_FILES]
    migrations = [f for f in changed if "migration" in f.lower()]
    declared = set(declared_files or [])
    undeclared = [f for f in changed if f not in declared and f not in deleted]

    reasons = []
    may_delete = set(planned_changes.get("may_delete_files", []) or [])
    for d in deleted:
        if d not in may_delete:
            reasons.append("unauthorized_delete")
            break
    if dep_changed and not planned_changes.get("may_add_dependencies", False):
        reasons.append("dependency_files_changed")
    if migrations and not planned_changes.get("may_run_migrations", False):
        reasons.append("migration_added")
    if undeclared:
        reasons.append("undeclared_files_changed")

    return {
        "needs_human": bool(reasons),
        "reasons": reasons,
        "deleted_files": deleted,
        "dependency_files_changed": dep_changed,
        "migrations_added": migrations,
        "undeclared_files_changed": undeclared,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_policy_change.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/policy_change.py tests/test_policy_change.py
git commit -m "feat: git change policy runner (spec §5.3)"
```

### Task 3.5: Trusted approval records + fallback handling (spec §5.4, §4.6)

**Files:**
- Create: `mcp/approval.py`
- Test: `tests/test_approval.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_approval.py`:

```python
import pytest
from mcp import approval as ap


def test_strong_method_accepted_when_supported(control_root):
    am = ap.Manager(control_root, tool_confirmation_supported=True,
                    allow_non_adversarial_fallback=False)
    rec = am.record(gate="SPEC", artifact_sha256="abc", commit_at_approval="c1")
    assert rec["approval_method"] == "zcode_tool_confirmation"


def test_strong_unsupported_and_no_fallback_raises(control_root):
    am = ap.Manager(control_root, tool_confirmation_supported=False,
                    allow_non_adversarial_fallback=False)
    with pytest.raises(ap.ApprovalUnavailableError):
        am.record(gate="SPEC", artifact_sha256="abc", commit_at_approval="c1")


def test_fallback_records_method_and_is_allowed_when_enabled(control_root):
    am = ap.Manager(control_root, tool_confirmation_supported=False,
                    allow_non_adversarial_fallback=True)
    rec = am.record(gate="SPEC", artifact_sha256="abc", commit_at_approval="c1")
    assert rec["approval_method"] == "non_adversarial_convention"


def test_records_persisted_to_approvals_dir(control_root):
    am = ap.Manager(control_root, tool_confirmation_supported=True,
                    allow_non_adversarial_fallback=False)
    am.record(gate="SPEC", artifact_sha256="abc", commit_at_approval="c1")
    files = list((control_root / "approvals").glob("*.json"))
    assert len(files) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_approval.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/approval.py`:

```python
"""Trusted approval records + fallback handling (spec §5.4, §4.6)."""
import datetime as _dt
import pathlib
import uuid

from . import io_atomic


class ApprovalUnavailableError(Exception):
    """Raised when strong confirmation unavailable and fallback disabled."""


class Manager:
    def __init__(self, control_root, *, tool_confirmation_supported: bool,
                 allow_non_adversarial_fallback: bool):
        self.control_root = pathlib.Path(control_root)
        self.strong = tool_confirmation_supported
        self.allow_fallback = allow_non_adversarial_fallback

    def record(self, *, gate: str, artifact_sha256: str, commit_at_approval: str) -> dict:
        if self.strong:
            method = "zcode_tool_confirmation"
        elif self.allow_fallback:
            method = "non_adversarial_convention"
        else:
            raise ApprovalUnavailableError(
                "ZCode does not support tool-level confirmation and "
                "allow_non_adversarial_fallback is false"
            )
        rec = {
            "approval_id": "apr_" + uuid.uuid4().hex[:12],
            "gate": gate,
            "artifact_sha256": artifact_sha256,
            "approved_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
            "approval_method": method,
            "commit_at_approval": commit_at_approval,
        }
        d = self.control_root / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        io_atomic.write_atomic_json(d / f"{rec['approval_id']}.json", rec)
        return rec
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_approval.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/approval.py tests/test_approval.py
git commit -m "feat: trusted approval records with explicit fallback (spec §5.4, §4.6)"
```

---

## Phase 4 — Planner & Reviewer runners

### Task 4.1: Codex planner runner (read-only, raw capture) (spec §5.1 planner)

**Files:**
- Create: `mcp/runner_codex.py`
- Test: `tests/test_runner_codex.py`

The runner builds the `codex exec` command, runs it via subprocess, captures raw
stdout/stderr, and parses the structured JSON result. CLI invocation is wrapped in
a single function so tests monkeypatch it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runner_codex.py`:

```python
import json
import pathlib
from mcp import runner_codex as rc


def test_build_command_uses_exec_readonly_and_schema(monkeypatch, tmp_path):
    schema = tmp_path / "s.json"; schema.write_text("{}")
    cmd = rc.build_command(
        model="gpt-5.6", reasoning_effort="high", sandbox="read-only",
        output_schema=schema, output_path=tmp_path / "out.json",
    )
    joined = " ".join(cmd)
    assert "exec" in cmd
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "--config" in cmd and "model_reasoning_effort=high" in cmd
    assert "--model" in cmd and "gpt-5.6" in cmd


def test_run_parses_envelope_and_records_raw(monkeypatch, tmp_path):
    envelope = {"status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
                "spec_markdown": "# S", "questions": [], "assumptions": []}
    captured = {}

    def fake_invoke(cmd, *, cwd, timeout):
        captured["cmd"] = cmd
        out = pathlib.Path(cwd) / "out.json"
        out.write_text(json.dumps(envelope), encoding="utf-8")
        return 0, "STDOUT_RAW", "STDERR_RAW"

    monkeypatch.setattr(rc, "_invoke", fake_invoke)
    res = rc.run(
        prompt="do it",
        model="gpt-5.6", reasoning_effort="high", sandbox="read-only",
        schema_name="spec-envelope.schema.json",
        workdir=tmp_path,
    )
    assert res["envelope"]["status"] == "READY_FOR_SPEC_APPROVAL"
    assert res["raw"]["stdout"] == "STDOUT_RAW"
    assert res["raw"]["exit_code"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runner_codex.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/runner_codex.py`:

```python
"""Codex planner runner (spec §5.1 planner).

Runs `codex exec` read-only, captures raw output, parses the structured JSON
envelope written by Codex via --output-schema / -o. CLI flag names are isolated
in build_command() so they can be adjusted per installed version (capability check).
"""
import json
import pathlib
import subprocess

_SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[1] / "schemas"


def build_command(*, model, reasoning_effort, sandbox, output_schema, output_path):
    return [
        "codex", "exec",
        "--model", model,
        "--config", f"model_reasoning_effort={reasoning_effort}",
        "--sandbox", sandbox,
        "--output-schema", str(output_schema),
        "-o", str(output_path),
        "-",  # read prompt from stdin
    ]


def _invoke(cmd, *, cwd, timeout):
    """Run the command with prompt on stdin. Returns (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        cmd, cwd=str(cwd), input="", capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run(*, prompt, model, reasoning_effort, sandbox, schema_name, workdir,
        timeout=600, output_filename="out.json"):
    schema_path = _SCHEMA_DIR / schema_name
    out_path = pathlib.Path(workdir) / output_filename
    cmd = build_command(model=model, reasoning_effort=reasoning_effort,
                        sandbox=sandbox, output_schema=schema_path, output_path=out_path)
    exit_code, stdout, stderr = _invoke(cmd, cwd=workdir, timeout=timeout)
    envelope = None
    if out_path.exists():
        envelope = json.loads(out_path.read_text(encoding="utf-8"))
    if exit_code != 0:
        raise RuntimeError(f"codex exec failed (exit {exit_code}): {stderr}")
    return {"envelope": envelope, "raw": {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runner_codex.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/runner_codex.py tests/test_runner_codex.py
git commit -m "feat: codex planner runner, read-only + raw capture (spec §5.1)"
```

### Task 4.2: Grok reviewer runner (read-only, raw capture) (spec §5.1 reviewer)

**Files:**
- Create: `mcp/runner_grok.py`
- Test: `tests/test_runner_grok.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_runner_grok.py`:

```python
import json
import pathlib
from mcp import runner_grok as rg


def test_build_command_uses_prompt_and_readonly_flags():
    cmd = rg.build_command(prompt="review T1", model="grok-4.5", cwd=pathlib.Path("/r"))
    joined = " ".join(cmd)
    assert "-p" in cmd
    assert "review T1" in cmd
    assert "--model" in cmd and "grok-4.5" in cmd
    assert "--cwd" in cmd


def test_run_parses_review_json(monkeypatch, tmp_path):
    review = {"decision": "PASS", "spec_compliance": True, "plan_compliance": True,
              "tests_sufficient": True, "issues": [], "replan_required": False,
              "base_commit": "a", "head_commit": "b", "task_id": "T1", "run_id": "r1",
              "spec_sha256": "s", "plan_sha256": "p", "task_manifest_sha256": "m", "plan_version": 1}

    def fake_invoke(cmd, *, cwd, timeout):
        return 0, json.dumps(review), ""

    monkeypatch.setattr(rg, "_invoke", fake_invoke)
    res = rg.run(prompt="review T1", model="grok-4.5", cwd=tmp_path)
    assert res["review"]["decision"] == "PASS"
    assert res["raw"]["exit_code"] == 0


def test_run_nonzero_raises(monkeypatch, tmp_path):
    def fake_invoke(cmd, *, cwd, timeout):
        return 2, "", "boom"
    monkeypatch.setattr(rg, "_invoke", fake_invoke)
    import pytest
    with pytest.raises(RuntimeError):
        rg.run(prompt="x", model="grok-4.5", cwd=tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runner_grok.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/runner_grok.py`:

```python
"""Grok reviewer runner (spec §5.1 reviewer).

Runs `grok -p` read-only, captures raw output, parses JSON review. Flags are
isolated in build_command(); real sandbox/allow-deny flags are confirmed against
the installed CLI version at capability-check time (v1 starts with the documented
--output-format json shape).
"""
import json
import pathlib
import subprocess


def build_command(*, prompt, model, cwd):
    return [
        "grok", "--no-auto-update",
        "-p", prompt,
        "--model", model,
        "--cwd", str(cwd),
        "--output-format", "json",
    ]


def _invoke(cmd, *, cwd, timeout):
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run(*, prompt, model, cwd, timeout=300):
    cmd = build_command(prompt=prompt, model=model, cwd=pathlib.Path(cwd))
    exit_code, stdout, stderr = _invoke(cmd, cwd=cwd, timeout=timeout)
    if exit_code != 0:
        raise RuntimeError(f"grok -p failed (exit {exit_code}): {stderr}")
    return {"review": json.loads(stdout), "raw": {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runner_grok.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/runner_grok.py tests/test_runner_grok.py
git commit -m "feat: grok reviewer runner, read-only + raw capture (spec §5.1)"
```

### Task 4.3: Prompt templates + spec/plan/execution schemas (spec §4.1-§4.4)

**Files:**
- Create: `schemas/spec-envelope.schema.json`
- Create: `schemas/plan-envelope.schema.json`
- Create: `schemas/execution-result.schema.json`
- Create: `prompts/planner-draft.md`
- Create: `prompts/planner-plan.md`
- Create: `prompts/reviewer.md`
- Test: `tests/test_schemas_shapes.py`

- [ ] **Step 1: Write the failing test (shape validation)**

Create `tests/test_schemas_shapes.py`:

```python
import json
import pathlib
from mcp import schemas


SCHEMAS = pathlib.Path(__file__).resolve().parents[1] / "schemas"


def test_spec_envelope_schema_exists_and_validates_sample():
    schema = json.loads((SCHEMAS / "spec-envelope.schema.json").read_text(encoding="utf-8"))
    sample = {"run_id": "r1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
              "spec_markdown": "# S", "questions": [], "assumptions": []}
    schemas.validate_against(sample, "spec-envelope.schema.json")


def test_plan_envelope_schema_validates_sample():
    sample = {"run_id": "r1", "status": "READY_FOR_EXECUTION", "plan_markdown": "# P",
              "plan_version": 1, "base_commit": "abc",
              "tasks": [{"id": "T001", "title": "t", "dependencies": [],
                         "declared_files": [], "verification_commands": [],
                         "acceptance_criteria": [], "planned_changes": {}}]}
    schemas.validate_against(sample, "plan-envelope.schema.json")


def test_execution_result_schema_validates_sample():
    sample = {"run_id": "r1", "task_id": "T001", "status": "READY_FOR_REVIEW",
              "base_commit": "a", "head_commit": "b", "worktree_clean": True,
              "untracked_files": [], "verification": [], "changed_files": [],
              "change_audit": {"deleted_files": [], "dependency_files_changed": False,
                               "migrations_added": [], "undeclared_files_changed": []}}
    schemas.validate_against(sample, "execution-result.schema.json")


def test_prompt_files_exist():
    prompts = pathlib.Path(__file__).resolve().parents[1] / "prompts"
    for name in ("planner-draft.md", "planner-plan.md", "reviewer.md"):
        assert (prompts / name).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schemas_shapes.py -v`
Expected: FAIL (missing schema files).

- [ ] **Step 3: Write schema files**

Create `schemas/spec-envelope.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "spec-envelope",
  "type": "object",
  "required": ["run_id", "status", "goal", "spec_markdown", "questions", "assumptions"],
  "properties": {
    "run_id": {"type": "string"},
    "status": {"enum": ["NEEDS_USER_INPUT", "READY_FOR_SPEC_APPROVAL"]},
    "goal": {"type": "string"},
    "spec_markdown": {"type": "string"},
    "questions": {"type": "array"},
    "assumptions": {"type": "array"}
  }
}
```

Create `schemas/plan-envelope.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "plan-envelope",
  "type": "object",
  "required": ["run_id", "status", "plan_markdown", "plan_version", "base_commit", "tasks"],
  "properties": {
    "run_id": {"type": "string"},
    "status": {"enum": ["READY_FOR_EXECUTION"]},
    "plan_markdown": {"type": "string"},
    "plan_version": {"type": "integer"},
    "base_commit": {"type": "string"},
    "tasks": {"type": "array", "items": {
      "type": "object",
      "required": ["id", "title", "dependencies", "declared_files",
                   "verification_commands", "acceptance_criteria", "planned_changes"],
      "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "declared_files": {"type": "array", "items": {"type": "string"}},
        "verification_commands": {"type": "array"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "planned_changes": {"type": "object"}
      }
    }}
  }
}
```

Create `schemas/execution-result.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "execution-result",
  "type": "object",
  "required": ["run_id", "task_id", "status", "base_commit", "head_commit",
               "worktree_clean", "untracked_files", "verification",
               "changed_files", "change_audit"],
  "properties": {
    "run_id": {"type": "string"},
    "task_id": {"type": "string"},
    "status": {"enum": ["READY_FOR_REVIEW", "FAILED"]},
    "base_commit": {"type": "string"},
    "head_commit": {"type": "string"},
    "worktree_clean": {"type": "boolean"},
    "untracked_files": {"type": "array", "items": {"type": "string"}},
    "verification": {"type": "array"},
    "changed_files": {"type": "array", "items": {"type": "string"}},
    "change_audit": {"type": "object"}
  }
}
```

- [ ] **Step 4: Write prompt templates**

Create `prompts/planner-draft.md`:

```markdown
You are the PLANNER for an AgentFlow run. Produce ONLY a spec, not a plan.

Goal:
{goal}

Answers to prior questions (if any):
{answers}

Rules:
- Read the repository at the working directory. Do NOT modify any files.
- Produce a SPEC that captures requirements, scope, and acceptance criteria.
- If essential information is missing and would change the architecture,
  ask at most 3 questions (set status=NEEDS_USER_INPUT).
- Otherwise set status=READY_FOR_SPEC_APPROVAL.
- For non-critical unknowns, record assumptions with risk levels; do not block.
- Output strictly the spec-envelope JSON schema provided.
```

Create `prompts/planner-plan.md`:

```markdown
You are the PLANNER for an AgentFlow run. Produce a PLAN based on the APPROVED SPEC only.

Approved SPEC (sha256 {approved_spec_sha256}):
{spec_markdown}

Rules:
- Read the repository. Do NOT modify any files.
- Decompose into tasks. For v1 this run will execute ONE task; still list all,
  but mark which is current.
- Each task MUST declare: declared_files, verification_commands (program+args+cwd+timeout),
  acceptance_criteria, planned_changes (may_add_dependencies, may_run_migrations,
  may_delete_files).
- Output strictly the plan-envelope JSON schema provided.
```

Create `prompts/reviewer.md`:

```markdown
You are the REVIEWER for an AgentFlow run. Review ONE task.

Task: {task_id}
Base commit: {base_commit}  Head commit: {head_commit}
SPEC sha256: {spec_sha256}  PLAN sha256: {plan_sha256}

Inspect the diff (base..head) against the SPEC and PLAN acceptance criteria.
Decide PASS / FAIL / REPLAN / BLOCKED. Honor the cross-field invariants:
PASS implies no issues and all compliance flags true; FAIL implies >=1 issue;
REPLAN implies replan_required=true.

Return strictly the review-result JSON schema. The hashes above are authoritative;
do not invent different values.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_schemas_shapes.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add schemas/ prompts/ tests/test_schemas_shapes.py
git commit -m "feat: spec/plan/execution schemas + prompt templates (spec §4.1-§4.4)"
```

---

## Phase 5 — Config + MCP server (the 9 tools)

### Task 5.1: Config loader + capability flags (spec §6)

**Files:**
- Create: `config.yaml`
- Create: `mcp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
import pathlib
from mcp import config as cfg


def test_load_returns_typed_config(tmp_path):
    yml = tmp_path / "config.yaml"
    yml.write_text(
        "auth:\n  codex: chatgpt_account\n  grok: xai_account\n"
        "workflow:\n  worktree: true\n  branch_prefix: agentflow\n"
        "human_gates:\n  spec_approval:\n    required: true\n"
        "    method: prefer_zcode_tool_confirmation\n"
        "    allow_non_adversarial_fallback: false\n"
        "  final_merge:\n    required: true\n"
        "planner:\n  model: gpt-5.6\n  model_reasoning_effort: high\n  sandbox: read-only\n"
        "  timeout_sec: 600\nreviewer:\n  model: grok-4.5\n  timeout_sec: 300\n"
        "verification:\n  command_policy:\n    shell: false\n    network: deny\n"
        "change_policy:\n  on_unauthorized_delete: NEEDS_HUMAN\n",
        encoding="utf-8",
    )
    c = cfg.load(yml)
    assert c["planner"]["model"] == "gpt-5.6"
    assert c["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] is False
    assert c["verification"]["command_policy"]["network"] == "deny"


def test_defaults_applied_when_keys_missing(tmp_path):
    yml = tmp_path / "config.yaml"
    yml.write_text("auth:\n  codex: chatgpt_account\n  grok: xai_account\n", encoding="utf-8")
    c = cfg.load(yml)
    assert c["planner"]["model_reasoning_effort"] == "high"
    assert c["workflow"]["branch_prefix"] == "agentflow"


def test_capability_flag_default_false():
    # capability flags are discovered at runtime, default to False until set.
    c = cfg._merge_defaults({})
    assert c["capabilities"]["tool_confirmation_supported"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/config.py`:

```python
"""Config loader + capability flags (spec §6)."""
import pathlib

import yaml

_DEFAULTS = {
    "auth": {"codex": "chatgpt_account", "grok": "xai_account"},
    "workflow": {"worktree": True, "branch_prefix": "agentflow"},
    "human_gates": {
        "spec_approval": {
            "required": True,
            "method": "prefer_zcode_tool_confirmation",
            "allow_non_adversarial_fallback": False,
        },
        "final_merge": {"required": True},
    },
    "planner": {
        "type": "codex_cli", "command": "codex", "mode": "exec",
        "model": "gpt-5.6", "model_reasoning_effort": "high",
        "sandbox": "read-only", "timeout_sec": 600,
    },
    "reviewer": {
        "type": "grok_build_cli", "command": "grok", "model": "grok-4.5",
        "output_format": "json", "sandbox": "read-only", "timeout_sec": 300,
    },
    "verification": {
        "command_policy": {"shell": False, "network": "deny",
                           "program_allowlist_from_agents_md": True,
                           "cwd_must_be_within": "run_worktree_root",
                           "secret_redaction": True},
    },
    "change_policy": {
        "on_unauthorized_delete": "NEEDS_HUMAN",
        "on_unauthorized_dependency": "NEEDS_HUMAN",
        "on_unauthorized_migration": "NEEDS_HUMAN",
        "on_undeclared_file": "NEEDS_HUMAN",
        "irreversible_migration": "always_need_human",
    },
    "capabilities": {"tool_confirmation_supported": False},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_defaults(loaded: dict) -> dict:
    return _deep_merge(_DEFAULTS, loaded)


def load(path) -> dict:
    path = pathlib.Path(path)
    with open(path, "r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    return _merge_defaults(loaded)
```

Create `config.yaml` at repo root (production default):

```yaml
version: 1
auth:
  codex: chatgpt_account
  grok: xai_account
workflow:
  worktree: true
  branch_prefix: agentflow
human_gates:
  spec_approval:
    required: true
    method: prefer_zcode_tool_confirmation
    allow_non_adversarial_fallback: false
  final_merge:
    required: true
    v1_terminal_state: READY_TO_MERGE
planner:
  type: codex_cli
  command: codex
  mode: exec
  model: gpt-5.6
  model_reasoning_effort: high
  sandbox: read-only
  timeout_sec: 600
reviewer:
  type: grok_build_cli
  command: grok
  model: grok-4.5
  output_format: json
  sandbox: read-only
  timeout_sec: 300
verification:
  command_policy:
    shell: false
    program_allowlist_from_agents_md: true
    cwd_must_be_within: run_worktree_root
    network: deny
    secret_redaction: true
change_policy:
  on_unauthorized_delete: NEEDS_HUMAN
  on_unauthorized_dependency: NEEDS_HUMAN
  on_unauthorized_migration: NEEDS_HUMAN
  on_undeclared_file: NEEDS_HUMAN
  irreversible_migration: always_need_human
review:
  require:
    - spec_compliance
    - plan_compliance
    - tests_passed
    - no_high_severity_issues
  cross_field_invariants: true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add config.yaml mcp/config.py tests/test_config.py
git commit -m "feat: config loader + capability flags (spec §6)"
```

### Task 5.2: Engine — the workflow logic behind the 9 tools (spec §3.1, §5.1)

This task implements the pure-Python engine (`Engine`) that the MCP `server.py`
wraps. Keeping logic testable without a live MCP transport.

**Files:**
- Create: `mcp/engine.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine.py`:

```python
import json
import subprocess
import pytest
from mcp import engine as en


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def seeded_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x"); _git(repo, "add", "-A"); _git(repo, "commit", "-m", "i")
    return repo


@pytest.fixture
def eng(tmp_path, seeded_repo, monkeypatch):
    control = tmp_path / "control"
    worktree = tmp_path / "wt"
    cfg = {
        "auth": {"codex": "chatgpt_account", "grok": "xai_account"},
        "workflow": {"branch_prefix": "agentflow"},
        "human_gates": {"spec_approval": {"required": True,
                       "allow_non_adversarial_fallback": True}},
        "planner": {"model": "gpt-5.6", "model_reasoning_effort": "high",
                    "sandbox": "read-only", "timeout_sec": 60},
        "reviewer": {"model": "grok-4.5", "timeout_sec": 60},
        "verification": {"command_policy": {"shell": False, "network": "deny",
                         "program_allowlist_from_agents_md": False}},
        "change_policy": {},
        "capabilities": {"tool_confirmation_supported": False},
    }
    e = en.Engine(plugin_root=tmp_path, control_root=control,
                  target_repo=seeded_repo, worktree_root=worktree, config=cfg)
    return e


def test_init_creates_state_and_worktree(eng, tmp_path):
    eng.workflow_init("run-1", goal="g")
    s = eng.status()
    assert s["state"] == "INITIALIZING_WORKTREE"
    assert (tmp_path / "wt").exists()


def test_planner_draft_spec_writes_spec_and_locks_sha(eng, monkeypatch):
    eng.workflow_init("run-1", goal="g")
    envelope = {"run_id": "run-1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
                "spec_markdown": "# SPEC\nbody", "questions": [], "assumptions": []}
    monkeypatch.setattr(eng, "_call_planner", lambda **kw: envelope)
    res = eng.planner_draft_spec(goal="g")
    assert res["status"] == "READY_FOR_SPEC_APPROVAL"
    assert eng.status()["spec_sha256"]  # MCP-computed, locked


def test_approve_spec_records_approval_and_advances(eng, monkeypatch):
    eng.workflow_init("run-1", goal="g")
    monkeypatch.setattr(eng, "_call_planner", lambda **kw: {
        "run_id": "run-1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
        "spec_markdown": "# S", "questions": [], "assumptions": []})
    eng.planner_draft_spec(goal="g")
    rev = eng.status()["revision"]
    rec = eng.workflow_approve_spec(expected_revision=rev)
    assert rec["approval_method"] == "non_adversarial_convention"
    assert eng.status()["state"] == "PLANNING"


def test_create_plan_requires_approved_spec_sha(eng, monkeypatch):
    eng.workflow_init("run-1", goal="g")
    monkeypatch.setattr(eng, "_call_planner", lambda **kw: {
        "run_id": "run-1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
        "spec_markdown": "# S", "questions": [], "assumptions": []})
    eng.planner_draft_spec(goal="g")
    eng.workflow_approve_spec(expected_revision=eng.status()["revision"])
    plan_env = {"run_id": "run-1", "status": "READY_FOR_EXECUTION", "plan_markdown": "# P",
                "plan_version": 1, "base_commit": "x", "tasks": [{
                    "id": "T001", "title": "t", "dependencies": [], "declared_files": [],
                    "verification_commands": [], "acceptance_criteria": [], "planned_changes": {}}]}
    monkeypatch.setattr(eng, "_call_planner", lambda **kw: plan_env)
    eng.planner_create_plan(approved_spec_sha=eng.status()["spec_sha256"],
                            expected_revision=eng.status()["revision"])
    assert eng.status()["state"] == "READY_FOR_EXECUTION"
    assert eng.status()["plan_sha256"]


def test_create_plan_rejects_wrong_spec_sha(eng, monkeypatch):
    eng.workflow_init("run-1", goal="g")
    with pytest.raises(en.WorkflowError):
        eng.planner_create_plan(approved_spec_sha="WRONG",
                                expected_revision=eng.status()["revision"])


def test_verify_task_failure_routes_to_needs_human(eng, monkeypatch):
    # drive to READY_FOR_EXECUTION first
    eng.workflow_init("run-1", goal="g")
    monkeypatch.setattr(eng, "_call_planner", lambda **kw: {
        "run_id": "run-1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
        "spec_markdown": "# S", "questions": [], "assumptions": []})
    eng.planner_draft_spec(goal="g")
    eng.workflow_approve_spec(expected_revision=eng.status()["revision"])
    monkeypatch.setattr(eng, "_call_planner", lambda **kw: {
        "run_id": "run-1", "status": "READY_FOR_EXECUTION", "plan_markdown": "# P",
        "plan_version": 1, "base_commit": "x", "tasks": [{
            "id": "T001", "title": "t", "dependencies": [], "declared_files": [],
            "verification_commands": [], "acceptance_criteria": [], "planned_changes": {}}]})
    eng.planner_create_plan(approved_spec_sha=eng.status()["spec_sha256"],
                            expected_revision=eng.status()["revision"])
    eng.workflow_start_task("T001", expected_revision=eng.status()["revision"])
    # verification that returns nonzero -> NEEDS_HUMAN
    monkeypatch.setattr(eng, "_run_verification", lambda *a, **k: [{"exit_code": 2}])
    eng.workflow_verify_task("T001", expected_revision=eng.status()["revision"])
    assert eng.status()["state"] == "NEEDS_HUMAN"


def test_complete_task_rejects_without_passing_review(eng, monkeypatch):
    eng.workflow_init("run-1", goal="g")
    rev = eng.status()["revision"]
    with pytest.raises(en.WorkflowError):
        eng.workflow_complete_task("T001", expected_revision=rev)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/engine.py`:

```python
"""Workflow engine: pure logic for the 9 tools (spec §3.1, §5.1).

The MCP server (server.py) is a thin transport wrapper over Engine.
Engine holds no transport state; it composes persistence, git, runners,
policies, approval. External calls are isolated as methods so tests can
monkeypatch them (_call_planner, _call_reviewer, _run_verification).
"""
import datetime as _dt
import pathlib

from . import (approval as approval_mod, config as config_mod, gitops,
               hashes, io_atomic, paths, persistence, schemas,
               policy_change, policy_verify, runner_codex, runner_grok, state as st)


class WorkflowError(Exception):
    pass


def _now():
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


class Engine:
    def __init__(self, *, plugin_root, control_root, target_repo, worktree_root, config):
        self.plugin_root = pathlib.Path(plugin_root)
        self.control_root = pathlib.Path(control_root)
        self.target_repo = pathlib.Path(target_repo)
        self.worktree_root = pathlib.Path(worktree_root)
        self.config = config
        self.roots = paths.Roots(
            plugin_root=self.plugin_root,
            target_repo_root=self.target_repo,
            run_worktree_root=self.worktree_root,
            control_root=self.control_root,
        )
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.store = persistence.Store(self.control_root)

    # ---- helpers ----
    def _state(self) -> st.State:
        return self.store.current()

    def _commit(self, operation_id, request, next_state, reason):
        self.store.commit_transition(operation_id, request, next_state, reason)

    # ---- tool: workflow_status ----
    def status(self) -> dict:
        return self._state().to_dict()

    # ---- tool: workflow_init ----
    def workflow_init(self, run_id, goal):
        s = st.State.new(run_id=run_id, roots=self.roots)
        s.save()
        base = gitops.create_worktree(
            target_repo=self.target_repo,
            worktree_path=self.worktree_root,
            branch=f"{self.config['workflow']['branch_prefix']}/{run_id}",
        )
        nxt = s.transition(to="INITIALIZING_WORKTREE", expected_revision=s.revision)
        nxt.base_commit = base
        self._commit(f"{run_id}-init", {"goal": goal}, nxt, "workflow_init")
        return nxt.to_dict()

    # ---- tool: planner_draft_spec ----
    def _call_planner(self, **kw):  # monkeypatched in tests
        return runner_codex.run(**kw)["envelope"]

    def planner_draft_spec(self, goal, answers=None):
        s = self._state()
        envelope = self._call_planner(
            prompt=self._render_prompt("planner-draft.md", goal=goal,
                                       answers=answers or "(none)"),
            model=self.config["planner"]["model"],
            reasoning_effort=self.config["planner"]["model_reasoning_effort"],
            sandbox=self.config["planner"]["sandbox"],
            schema_name="spec-envelope.schema.json",
            workdir=self.control_root,
        )
        schemas.validate_against(envelope, "spec-envelope.schema.json")
        spec_path = self.control_root / "SPEC.md"
        io_atomic.write_atomic(spec_path, envelope["spec_markdown"])
        spec_sha = hashes.hash_file(spec_path)
        nxt = s.transition(to=("NEEDS_USER_INPUT" if envelope.get("questions")
                               else "READY_FOR_SPEC_APPROVAL"),
                           expected_revision=s.revision)
        nxt.spec_sha256 = spec_sha
        self._commit(f"{s.run_id}-draft", {"goal": goal}, nxt, "planner_draft_spec")
        io_atomic.write_atomic_json(self.control_root / "spec-envelope.json", envelope)
        return {"status": nxt.state, "spec_sha256": spec_sha,
                "questions": envelope.get("questions", [])}

    # ---- tool: workflow_approve_spec ----
    def workflow_approve_spec(self, expected_revision):
        s = self._state()
        if s.state != "READY_FOR_SPEC_APPROVAL":
            raise WorkflowError(f"cannot approve from {s.state}")
        am = approval_mod.Manager(
            self.control_root,
            tool_confirmation_supported=self.config["capabilities"]["tool_confirmation_supported"],
            allow_non_adversarial_fallback=self.config["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"],
        )
        rec = am.record(gate="SPEC", artifact_sha256=s.spec_sha256,
                        commit_at_approval=s.base_commit)
        nxt = s.transition(to="PLANNING", expected_revision=expected_revision)
        self._commit(f"{s.run_id}-approve", rec, nxt, "workflow_approve_spec")
        return rec

    # ---- tool: planner_create_plan ----
    def planner_create_plan(self, approved_spec_sha, expected_revision):
        s = self._state()
        if s.state != "PLANNING":
            raise WorkflowError(f"cannot plan from {s.state}")
        if approved_spec_sha != s.spec_sha256:
            raise WorkflowError("approved_spec_sha does not match locked SPEC")
        spec_md = (self.control_root / "SPEC.md").read_text(encoding="utf-8")
        envelope = self._call_planner(
            prompt=self._render_prompt("planner-plan.md",
                                       approved_spec_sha256=s.spec_sha256,
                                       spec_markdown=spec_md),
            model=self.config["planner"]["model"],
            reasoning_effort=self.config["planner"]["model_reasoning_effort"],
            sandbox=self.config["planner"]["sandbox"],
            schema_name="plan-envelope.schema.json",
            workdir=self.control_root,
        )
        schemas.validate_against(envelope, "plan-envelope.schema.json")
        plan_path = self.control_root / "PLAN.md"
        io_atomic.write_atomic(plan_path, envelope["plan_markdown"])
        plan_sha = hashes.hash_file(plan_path)
        manifest = {"plan_sha256": plan_sha,
                    "current_task": envelope["tasks"][0]["id"],
                    "tasks": envelope["tasks"]}
        manifest_sha = hashes.sha256_canonical(
            __import__("json").dumps(manifest, sort_keys=True).encode("utf-8"))
        manifest["task_manifest_sha256"] = manifest_sha
        io_atomic.write_atomic_json(self.control_root / "task-manifest.json", manifest)

        nxt = s.transition(to="READY_FOR_EXECUTION", expected_revision=expected_revision)
        nxt.plan_sha256 = plan_sha
        nxt.task_manifest_sha256 = manifest_sha
        nxt.plan_version = envelope["plan_version"]
        nxt.current_task = envelope["tasks"][0]["id"]
        self._commit(f"{s.run_id}-plan", {"approved_spec_sha": approved_spec_sha},
                     nxt, "planner_create_plan")
        io_atomic.write_atomic_json(self.control_root / "plan-envelope.json", envelope)
        return {"status": nxt.state, "plan_sha256": plan_sha,
                "task_manifest_sha256": manifest_sha}

    # ---- tool: workflow_start_task ----
    def workflow_start_task(self, task_id, expected_revision):
        s = self._state()
        if s.state != "READY_FOR_EXECUTION":
            raise WorkflowError(f"cannot start task from {s.state}")
        nxt = s.transition(to="EXECUTING_TASK", expected_revision=expected_revision)
        nxt.current_task = task_id
        nxt.task_start_commit = gitops.head(self.worktree_root)
        self._commit(f"{s.run_id}-start-{task_id}", {"task_id": task_id},
                     nxt, "workflow_start_task")
        return {"state": nxt.state, "task_start_commit": nxt.task_start_commit}

    # ---- tool: workflow_verify_task ----
    def _run_verification(self, task, manifest):
        results = []
        allowlist = self._program_allowlist()
        for v in task.get("verification_commands", []):
            results.append(policy_verify.run(
                verification=v, worktree_root=self.worktree_root,
                program_allowlist=allowlist))
        return results

    def _program_allowlist(self):
        # v1: explicit minimal allowlist; AGENTS.md-driven allowlist is v1.1.
        return ["python", "pytest", "git"]

    def workflow_verify_task(self, task_id, expected_revision):
        s = self._state()
        if s.state != "EXECUTING_TASK":
            raise WorkflowError(f"cannot verify from {s.state}")
        manifest = io_atomic.read_json(self.control_root / "task-manifest.json")
        task = next(t for t in manifest["tasks"] if t["id"] == task_id)
        results = self._run_verification(task, manifest)
        all_green = all(r.get("exit_code") == 0 for r in results)
        head = gitops.head(self.worktree_root)
        clean = gitops.is_clean(self.worktree_root)
        untracked = gitops.untracked_files(self.worktree_root)
        audit = policy_change.audit(
            worktree=self.worktree_root, base_commit=s.task_start_commit,
            head_commit=head, declared_files=task.get("declared_files", []),
            planned_changes=task.get("planned_changes", {}))
        exec_result = {
            "run_id": s.run_id, "task_id": task_id,
            "status": "READY_FOR_REVIEW" if (all_green and clean and not untracked
                                             and not audit["needs_human"]) else "FAILED",
            "base_commit": s.task_start_commit, "head_commit": head,
            "worktree_clean": clean, "untracked_files": untracked,
            "verification": results,
            "changed_files": gitops.changed_files_between(
                self.worktree_root, s.task_start_commit, head),
            "change_audit": audit,
        }
        io_atomic.write_atomic_json(
            self.control_root / "operations" / f"exec-{task_id}.json", exec_result)
        schemas.validate_against(exec_result, "execution-result.schema.json")
        if exec_result["status"] != "READY_FOR_REVIEW":
            nxt = s.transition(to="NEEDS_HUMAN", expected_revision=expected_revision)
            self._commit(f"{s.run_id}-verify-{task_id}", exec_result, nxt, "verify_failed")
            return {"state": "NEEDS_HUMAN", "execution_result": exec_result}
        nxt = s.transition(to="VERIFYING_TASK", expected_revision=expected_revision)
        nxt.last_reviewed_commit = head
        self._commit(f"{s.run_id}-verify-{task_id}", exec_result, nxt, "workflow_verify_task")
        return {"state": nxt.state, "execution_result": exec_result}

    # ---- tool: reviewer_review_task ----
    def _call_reviewer(self, **kw):  # monkeypatched in tests
        return runner_grok.run(**kw)["review"]

    def reviewer_review_task(self, task_id, expected_revision):
        s = self._state()
        if s.state != "VERIFYING_TASK":
            raise WorkflowError(f"cannot review from {s.state}")
        manifest = io_atomic.read_json(self.control_root / "task-manifest.json")
        prompt = self._render_prompt(
            "reviewer.md", task_id=task_id, base_commit=s.task_start_commit,
            head_commit=s.last_reviewed_commit, spec_sha256=s.spec_sha256,
            plan_sha256=s.plan_sha256)
        review = self._call_reviewer(
            prompt=prompt, model=self.config["reviewer"]["model"],
            cwd=self.worktree_root, timeout=self.config["reviewer"]["timeout_sec"])
        # Inject authoritative hashes (reviewer must not invent them).
        review.update({"spec_sha256": s.spec_sha256, "plan_sha256": s.plan_sha256,
                       "task_manifest_sha256": s.task_manifest_sha256,
                       "plan_version": s.plan_version, "run_id": s.run_id,
                       "task_id": task_id, "base_commit": s.task_start_commit,
                       "head_commit": s.last_reviewed_commit})
        schemas.validate_review(review)
        io_atomic.write_atomic_json(
            self.control_root / "reviews" / f"review-{task_id}.json", review)
        if review["decision"] != "PASS":
            nxt = s.transition(to="NEEDS_HUMAN", expected_revision=expected_revision)
            self._commit(f"{s.run_id}-review-{task_id}", review, nxt, "review_non_pass")
            return {"state": "NEEDS_HUMAN", "decision": review["decision"]}
        nxt = s.transition(to="REVIEWING_TASK", expected_revision=expected_revision)
        self._commit(f"{s.run_id}-review-{task_id}", review, nxt, "reviewer_review_task")
        return {"state": nxt.state, "decision": "PASS"}

    # ---- tool: workflow_complete_task ----
    def workflow_complete_task(self, task_id, expected_revision):
        s = self._state()
        if s.state != "REVIEWING_TASK":
            raise WorkflowError(f"cannot complete from {s.state}")
        review = io_atomic.read_json(
            self.control_root / "reviews" / f"review-{task_id}.json")
        exec_result = io_atomic.read_json(
            self.control_root / "operations" / f"exec-{task_id}.json")
        head = gitops.head(self.worktree_root)
        # 8 hard checks (spec §5.1)
        checks = {
            "exec_ready": exec_result["status"] == "READY_FOR_REVIEW",
            "decision_pass": review["decision"] == "PASS",
            "head_matches": review["head_commit"] == head,
            "worktree_clean": gitops.is_clean(self.worktree_root)
                              and not gitops.untracked_files(self.worktree_root),
            "spec_sha": review["spec_sha256"] == s.spec_sha256,
            "plan_sha": review["plan_sha256"] == s.plan_sha256,
            "manifest_sha": review["task_manifest_sha256"] == s.task_manifest_sha256,
            "cas": expected_revision == s.revision,
        }
        if not all(checks.values()):
            raise WorkflowError(f"complete_task hard checks failed: {checks}")
        nxt = s.transition(to="READY_TO_MERGE", expected_revision=expected_revision)
        nxt.completed_tasks = list(s.completed_tasks) + [task_id]
        self._commit(f"{s.run_id}-complete-{task_id}", checks, nxt, "workflow_complete_task")
        return {"state": "READY_TO_MERGE", "checks": checks}

    # ---- prompt rendering ----
    def _render_prompt(self, name, **fields):
        tpl = (self.plugin_root / "prompts" / name).read_text(encoding="utf-8")
        for k, v in fields.items():
            tpl = tpl.replace("{" + k + "}", str(v))
        return tpl
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/engine.py tests/test_engine.py
git commit -m "feat: workflow engine for the 9 v1 tools (spec §3.1, §5.1)"
```

### Task 5.3: MCP server transport (spec §5.1) — thin wrapper over Engine

**Files:**
- Create: `mcp/server.py`
- Test: `tests/test_server_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_tools.py`:

```python
import inspect
from mcp import server as srv


def test_server_exposes_nine_tools():
    names = srv.tool_names()
    expected = {
        "workflow_status", "workflow_init", "planner_draft_spec",
        "workflow_approve_spec", "planner_create_plan", "workflow_start_task",
        "workflow_verify_task", "reviewer_review_task", "workflow_complete_task",
    }
    assert expected == set(names)
    assert len(names) == 9


def test_dispatch_calls_engine_method(monkeypatch):
    calls = {}

    class FakeEngine:
        def status(self):
            calls["status"] = True
            return {"state": "NEW"}

    monkeypatch.setattr(srv, "_engine", lambda: FakeEngine())
    out = srv.dispatch("workflow_status", {})
    assert calls["status"] is True
    assert out["state"] == "NEW"


def test_dispatch_unknown_tool_raises(monkeypatch):
    import pytest
    monkeypatch.setattr(srv, "_engine", lambda: object())
    with pytest.raises(srv.UnknownToolError):
        srv.dispatch("nope", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_tools.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/server.py`:

```python
"""MCP server transport (spec §5.1). Thin wrapper over Engine.

Exposes 9 tools. Each tool maps to an Engine method and is responsible only for
argument marshalling and transport. All workflow logic lives in Engine.

The stdio entrypoint `main()` is wired in Phase 6 (packaging).
"""
from . import engine as engine_mod

_TOOL_METHODS = {
    "workflow_status": ("status", []),
    "workflow_init": ("workflow_init", ["run_id", "goal"]),
    "planner_draft_spec": ("planner_draft_spec", ["goal", "answers"]),
    "workflow_approve_spec": ("workflow_approve_spec", ["expected_revision"]),
    "planner_create_plan": ("planner_create_plan", ["approved_spec_sha", "expected_revision"]),
    "workflow_start_task": ("workflow_start_task", ["task_id", "expected_revision"]),
    "workflow_verify_task": ("workflow_verify_task", ["task_id", "expected_revision"]),
    "reviewer_review_task": ("reviewer_review_task", ["task_id", "expected_revision"]),
    "workflow_complete_task": ("workflow_complete_task", ["task_id", "expected_revision"]),
}


class UnknownToolError(Exception):
    pass


def tool_names():
    return list(_TOOL_METHODS.keys())


def _engine():
    """Returns the process-wide Engine. Built in Phase 6 from config + env."""
    raise NotImplementedError("engine bootstrap is wired in Phase 6")


def dispatch(tool_name, args):
    if tool_name not in _TOOL_METHODS:
        raise UnknownToolError(tool_name)
    method_name, params = _TOOL_METHODS[tool_name]
    eng = _engine()
    method = getattr(eng, method_name)
    call_args = {p: args.get(p) for p in params}
    return method(**call_args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_tools.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/server.py tests/test_server_tools.py
git commit -m "feat: MCP server transport wrapping the 9 tools (spec §5.1)"
```

---

## Phase 6 — Bootstrap, hooks, packaging, E2E

### Task 6.1: stdio bootstrap + engine factory (spec §5.1)

**Files:**
- Create: `mcp/bootstrap.py`
- Modify: `mcp/server.py` (replace `_engine()`)
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bootstrap.py`:

```python
import os
import pathlib
import subprocess
import pytest
from mcp import bootstrap, server as srv


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x"); _git(repo, "add", "-A"); _git(repo, "commit", "-m", "i")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("auth:\n  codex: chatgpt_account\n  grok: xai_account\n", encoding="utf-8")
    control = tmp_path / "control"; worktree = tmp_path / "wt"
    monkeypatch.setenv("AGENTFLOW_TARGET_REPO", str(repo))
    monkeypatch.setenv("AGENTFLOW_CONTROL_ROOT", str(control))
    monkeypatch.setenv("AGENTFLOW_WORKTREE_ROOT", str(worktree))
    monkeypatch.setenv("AGENTFLOW_RUN_ID", "r1")
    return {"repo": repo, "control": control, "worktree": worktree,
            "plugin_root": tmp_path, "config_path": cfg}


def test_build_engine_returns_engine(env, monkeypatch):
    monkeypatch.setattr(bootstrap, "_plugin_root", lambda: env["plugin_root"])
    eng = bootstrap.build_engine(env["config_path"])
    from mcp import engine
    assert isinstance(eng, engine.Engine)


def test_server_engine_uses_bootstrap(env, monkeypatch):
    monkeypatch.setattr(bootstrap, "_plugin_root", lambda: env["plugin_root"])
    srv.set_engine_factory(lambda: bootstrap.build_engine(env["config_path"]))
    out = srv.dispatch("workflow_init", {"run_id": "r1", "goal": "g"})
    assert out["state"] == "INITIALIZING_WORKTREE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bootstrap.py -v`
Expected: FAIL import error / AttributeError (set_engine_factory missing).

- [ ] **Step 3: Write bootstrap module**

Create `mcp/bootstrap.py`:

```python
"""Build an Engine from env + config (spec §5.1)."""
import os
import pathlib

from . import config, engine as engine_mod, paths


def _plugin_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def build_engine(config_path) -> engine_mod.Engine:
    cfg = config.load(config_path)
    target = pathlib.Path(os.environ["AGENTFLOW_TARGET_REPO"])
    control = pathlib.Path(os.environ["AGENTFLOW_CONTROL_ROOT"])
    worktree = pathlib.Path(os.environ["AGENTFLOW_WORKTREE_ROOT"])
    return engine_mod.Engine(
        plugin_root=_plugin_root(),
        control_root=control,
        target_repo=target,
        worktree_root=worktree,
        config=cfg,
    )
```

Modify `mcp/server.py` — replace the `_engine()` stub and add a factory setter:

```python
# Replace the existing _engine() definition with:
_engine_factory = None


def set_engine_factory(factory):
    global _engine_factory
    _engine_factory = factory


def _engine():
    if _engine_factory is None:
        raise RuntimeError("engine factory not configured; call set_engine_factory()")
    return _engine_factory()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bootstrap.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/bootstrap.py mcp/server.py tests/test_bootstrap.py
git commit -m "feat: stdio engine bootstrap from env+config (spec §5.1)"
```

### Task 6.2: Hooks — protect-artifacts + gate-on-stop (spec §5.6)

**Files:**
- Create: `hooks/hooks.json`
- Create: `hooks/protect_artifacts.py`
- Create: `hooks/gate_on_stop.py`
- Test: `tests/test_hooks.py`

Hooks read the proposed tool path from a JSON payload on stdin and exit non-zero
to deny. (Depth-of-defense only — not a security boundary, per spec §1.2.)

- [ ] **Step 1: Write the failing test**

Create `tests/test_hooks.py`:

```python
import io
import json
import subprocess
import pathlib
from mcp import hooks_common


def test_is_protected_artifact_matches_state_and_specs():
    assert hooks_common.is_protected_artifact("state.json")
    assert hooks_common.is_protected_artifact("SPEC.md")
    assert hooks_common.is_protected_artifact("PLAN.md")
    assert hooks_common.is_protected_artifact("reviews/review-T001.json")
    assert hooks_common.is_protected_artifact("events.jsonl")
    assert not hooks_common.is_protected_artifact("src/main.py")


def test_protect_artifacts_denies_protected(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool": "Edit", "path": "control/state.json"})))
    rc = hooks_common.run_protect_artifacts()
    assert rc == 2  # deny


def test_protect_artifacts_allows_normal(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool": "Edit", "path": "wt/src/main.py"})))
    assert hooks_common.run_protect_artifacts() == 0


def test_gate_on_stop_blocks_when_review_missing(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "tool": "Stop", "control_root": "/tmp/ctrl", "current_task": "T001"})))
    # No review file -> gate blocks (exit 2 + feedback)
    monkeypatch.setattr(hooks_common, "_review_decision", lambda *a: None)
    assert hooks_common.run_gate_on_stop() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: FAIL import error.

- [ ] **Step 3: Write minimal implementation**

Create `mcp/hooks_common.py` (shared logic so hooks are thin and testable):

```python
"""Shared hook logic (spec §5.6). Depth-of-defense only — not a security boundary."""
import json
import os
import pathlib
import sys

_PROTECTED = {"state.json", "events.jsonl", "SPEC.md", "PLAN.md"}


def _read_payload():
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def is_protected_artifact(path_str: str) -> bool:
    name = pathlib.PurePath(path_str).name
    if name in _PROTECTED:
        return True
    parts = pathlib.PurePath(path_str).parts
    if "reviews" in parts and name.endswith(".json"):
        return True
    if "approvals" in parts:
        return True
    return False


def run_protect_artifacts() -> int:
    payload = _read_payload()
    path = payload.get("path", "")
    if is_protected_artifact(path):
        sys.stderr.write(f"[agentflow] editing protected artifact blocked: {path}\n")
        return 2  # deny
    return 0


def _review_decision(control_root, task_id):
    p = pathlib.Path(control_root) / "reviews" / f"review-{task_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("decision")


def run_gate_on_stop() -> int:
    payload = _read_payload()
    control = payload.get("control_root")
    task = payload.get("current_task")
    if not control or not task:
        return 0  # nothing to check
    decision = _review_decision(control, task)
    if decision != "PASS":
        sys.stderr.write(
            f"[agentflow] cannot stop: task {task} has no PASS review (decision={decision})\n")
        return 2  # ask model to continue
    return 0
```

Create `hooks/protect_artifacts.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp.hooks_common import run_protect_artifacts

if __name__ == "__main__":
    sys.exit(run_protect_artifacts())
```

Create `hooks/gate_on_stop.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp.hooks_common import run_gate_on_stop

if __name__ == "__main__":
    sys.exit(run_gate_on_stop())
```

Create `hooks/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Edit|Write", "command": "python hooks/protect_artifacts.py"}
    ],
    "Stop": [
      {"command": "python hooks/gate_on_stop.py"}
    ]
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp/hooks_common.py hooks/ tests/test_hooks.py
git commit -m "feat: protect-artifacts + gate-on-stop hooks (spec §5.6)"
```

### Task 6.3: Skill + commands + plugin manifest (spec §5.5, §5.7)

**Files:**
- Create: `.zcode-plugin/plugin.json`
- Create: `skills/agentflow/SKILL.md`
- Create: `commands/agentflow-start.md`
- Create: `commands/agentflow-status.md`
- Create: `AGENTS.md.template`
- Test: `tests/test_packaging.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_packaging.py`:

```python
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_plugin_manifest_valid():
    m = json.loads((ROOT / ".zcode-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert m["name"] == "zcode-agentflow"
    assert "mcp" in m and "skills" in m and "commands" in m and "hooks" in m


def test_skill_md_describes_workflow():
    txt = (ROOT / "skills" / "agentflow" / "SKILL.md").read_text(encoding="utf-8")
    assert "planner_create_plan" in txt or "workflow" in txt.lower()
    assert "READY_TO_MERGE" in txt


def test_agents_md_template_does_not_overwrite_marker():
    txt = (ROOT / "AGENTS.md.template").read_text(encoding="utf-8")
    assert "DO NOT OVERWRITE" in txt.upper() or "detect" in txt.lower()


def test_commands_reference_tools():
    for name in ("agentflow-start.md", "agentflow-status.md"):
        assert (ROOT / "commands" / name).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_packaging.py -v`
Expected: FAIL (missing files).

- [ ] **Step 3: Write the packaging files**

Create `.zcode-plugin/plugin.json`:

```json
{
  "name": "zcode-agentflow",
  "version": "0.1.0",
  "description": "ZCode-driven AgentFlow: ZCode supervises, Codex plans, Grok reviews.",
  "mcp": {"server": "mcp/server.py", "transport": "stdio"},
  "skills": ["skills/agentflow"],
  "commands": ["commands/agentflow-start.md", "commands/agentflow-status.md"],
  "hooks": "hooks/hooks.json"
}
```

Create `skills/agentflow/SKILL.md`:

```markdown
---
name: agentflow
description: Drive a single-task AgentFlow run (ZCode supervises; Codex plans; Grok reviews). Use when the user starts an agentflow run or asks for run status.
---

# AgentFlow v1 — how the supervisor behaves

You are the Supervisor + Executor of an AgentFlow run. You DO NOT write SPEC/PLAN/state
yourself; you call MCP tools. You DO NOT bypass gates.

## The 9 tools (call in order)
1. `workflow_init(run_id, goal)` — create worktree + state
2. `planner_draft_spec(goal, answers?)` — Codex drafts SPEC
   - if it returns `NEEDS_USER_INPUT`: ask the user (≤3 questions), then call again with `answers`
3. `workflow_approve_spec(expected_revision)` — **user gate 1**: only call after the user explicitly approves the SPEC
4. `planner_create_plan(approved_spec_sha, expected_revision)` — Codex drafts PLAN from the approved SPEC
5. `workflow_start_task(task_id, expected_revision)`
6. Implement the task yourself in the worktree, commit, then:
7. `workflow_verify_task(task_id, expected_revision)` — runs verification + git-change policy
   - if state becomes `NEEDS_HUMAN`: stop and surface the reason to the user
8. `reviewer_review_task(task_id, expected_revision)` — Grok reviews
   - PASS only: proceed. FAIL/REPLAN/BLOCKED → `NEEDS_HUMAN`, stop.
9. `workflow_complete_task(task_id, expected_revision)` — 8 hard checks; on success state = `READY_TO_MERGE`

## Hard rules
- Never edit `SPEC.md`, `PLAN.md`, `state.json`, `events.jsonl`, or `reviews/*` directly.
- Never interpret a non-PASS review as "good enough".
- Never call `workflow_approve_spec` unless the user explicitly approved.
- `READY_TO_MERGE` is the v1 terminal state; the user merges outside AgentFlow.
- On any `NEEDS_HUMAN`: stop, report, wait for the user.
```

Create `commands/agentflow-start.md`:

```markdown
---
description: Start an AgentFlow v1 run for a goal in the current repo
---

Start an AgentFlow v1 run. Load the `agentflow` skill and follow it.

Goal: $ARGUMENTS

Begin at `workflow_init`. Pause for the SPEC approval gate and for any `NEEDS_USER_INPUT`.
```

Create `commands/agentflow-status.md`:

```markdown
---
description: Show current AgentFlow run state
---

Call the `workflow_status` tool and report the current state, revision, current task,
and any pending gate or `NEEDS_HUMAN` reason.
```

Create `AGENTS.md.template`:

```markdown
# AGENTS.md (AgentFlow — DO NOT OVERWRITE an existing AGENTS.md)

When AgentFlow initializes a run, it checks for an existing `AGENTS.md` in the target
repo. If one exists, it appends an "## AgentFlow notes" section and never overwrites
project conventions.

## AgentFlow notes
- Verification commands MUST be declared in PLAN tasks as program+args+cwd+timeout.
- Allowed verification programs (v1 allowlist): python, pytest, git.
- One task per run in v1.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_packaging.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add .zcode-plugin/ skills/ commands/ AGENTS.md.template tests/test_packaging.py
git commit -m "feat: skill, commands, plugin manifest, AGENTS.md template (spec §5.5, §5.7)"
```

### Task 6.4: E2E demo with fake runners (spec §8.2, S4)

Drives the full state machine NEW → READY_TO_MERGE with monkeypatched Codex/Grok
and a real git worktree, exercising the 8 hard checks.

**Files:**
- Create: `tests/test_e2e_v1.py`

- [ ] **Step 1: Write the test**

Create `tests/test_e2e_v1.py`:

```python
import subprocess
import pathlib
import pytest
from mcp import bootstrap, engine as engine_mod, server as srv


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x"); _git(repo, "add", "-A"); _git(repo, "commit", "-m", "i")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("auth:\n  codex: chatgpt_account\n  grok: xai_account\n", encoding="utf-8")
    monkeypatch.setenv("AGENTFLOW_TARGET_REPO", str(repo))
    monkeypatch.setenv("AGENTFLOW_CONTROL_ROOT", str(tmp_path / "control"))
    monkeypatch.setenv("AGENTFLOW_WORKTREE_ROOT", str(tmp_path / "wt"))
    monkeypatch.setattr(bootstrap, "_plugin_root", lambda: tmp_path)
    srv.set_engine_factory(lambda: bootstrap.build_engine(cfg))
    return {"repo": repo, "control": tmp_path / "control", "tmp": tmp_path}


def _passing_plan(repo_head):
    return {"run_id": "r1", "status": "READY_FOR_EXECUTION", "plan_markdown": "# P",
            "plan_version": 1, "base_commit": repo_head, "tasks": [{
                "id": "T001", "title": "t", "dependencies": [],
                "declared_files": ["a.txt"],
                "verification_commands": [{"program": "python", "args": ["-V"],
                                           "cwd": ".", "timeout_sec": 10}],
                "acceptance_criteria": ["a.txt unchanged"],
                "planned_changes": {"may_add_dependencies": False,
                                    "may_run_migrations": False,
                                    "may_delete_files": []}}]}


def _passing_review():
    return {"decision": "PASS", "spec_compliance": True, "plan_compliance": True,
            "tests_sufficient": True, "issues": [], "replan_required": False}


def test_full_run_reaches_ready_to_merge(env, monkeypatch):
    eng = bootstrap.build_engine(env["tmp"] / "config.yaml")
    head = subprocess.run(["git", "-C", str(env["repo"]), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    # Planner returns a SPEC then a PLAN.
    eng._call_planner = lambda **kw: (
        {"run_id": "r1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
         "spec_markdown": "# SPEC", "questions": [], "assumptions": []}
        if "planner-plan" not in kw["prompt"]
        else _passing_plan(head))
    eng._call_reviewer = lambda **kw: _passing_review()
    # Verification: python -V exits 0.
    eng._run_verification = lambda task, manifest: [{"exit_code": 0}]

    srv.dispatch("workflow_init", {"run_id": "r1", "goal": "g"})
    srv.dispatch("planner_draft_spec", {"goal": "g"})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("workflow_approve_spec", {"expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("planner_create_plan", {
        "approved_spec_sha": srv.dispatch("workflow_status", {})["spec_sha256"],
        "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("workflow_start_task", {"task_id": "T001", "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("workflow_verify_task", {"task_id": "T001", "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("reviewer_review_task", {"task_id": "T001", "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    out = srv.dispatch("workflow_complete_task", {"task_id": "T001", "expected_revision": rev})
    assert out["state"] == "READY_TO_MERGE"
    assert all(out["checks"].values())


def test_full_run_blocks_when_review_fails(env, monkeypatch):
    eng = bootstrap.build_engine(env["tmp"] / "config.yaml")
    head = subprocess.run(["git", "-C", str(env["repo"]), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    eng._call_planner = lambda **kw: (
        {"run_id": "r1", "status": "READY_FOR_SPEC_APPROVAL", "goal": "g",
         "spec_markdown": "# SPEC", "questions": [], "assumptions": []}
        if "planner-plan" not in kw["prompt"] else _passing_plan(head))
    eng._call_reviewer = lambda **kw: {"decision": "FAIL", "spec_compliance": False,
        "plan_compliance": False, "tests_sufficient": False,
        "issues": [{"id": "R1", "severity": "HIGH"}], "replan_required": False}
    eng._run_verification = lambda task, manifest: [{"exit_code": 0}]

    srv.dispatch("workflow_init", {"run_id": "r1", "goal": "g"})
    srv.dispatch("planner_draft_spec", {"goal": "g"})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("workflow_approve_spec", {"expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("planner_create_plan", {
        "approved_spec_sha": srv.dispatch("workflow_status", {})["spec_sha256"],
        "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("workflow_start_task", {"task_id": "T001", "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    srv.dispatch("workflow_verify_task", {"task_id": "T001", "expected_revision": rev})
    rev = srv.dispatch("workflow_status", {})["revision"]
    out = srv.dispatch("reviewer_review_task", {"task_id": "T001", "expected_revision": rev})
    assert out["state"] == "NEEDS_HUMAN"
    assert out["decision"] == "FAIL"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_e2e_v1.py -v`
Expected: FAIL (planner prompt gating/`_call_planner` wiring) — drive implementation to make it pass. No new production code expected beyond engine; if it fails, fix the engine wiring (e.g. ensure `kw["prompt"]` is passed through) until green.

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m pytest tests/test_e2e_v1.py -v`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_v1.py
git commit -m "test: e2e v1 demo NEW->READY_TO_MERGE + FAIL->NEEDS_HUMAN (spec §8.2, S4)"
```

### Task 6.5: Full suite + README smoke

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -v`
Expected: all tests pass (count the tasks' expected totals; every module green).

- [ ] **Step 2: Lint/import smoke**

Run: `python -c "import mcp.server, mcp.engine, mcp.bootstrap"`
Expected: no output (clean import).

- [ ] **Step 3: Commit (if any formatting/import fixes were needed)**

```bash
git add -A
git commit -m "chore: full suite green for v1 vertical slice"
```

- [ ] **Step 4: Push**

```bash
git push origin main
```

---

## Definition of Done (v1, maps to spec §0.4 S4)

- [ ] All 9 MCP tools implemented and individually tested.
- [ ] `tests/test_e2e_v1.py` reaches `READY_TO_MERGE` with all 8 hard checks green.
- [ ] FAIL/REPLAN verification/review routes to `NEEDS_HUMAN`.
- [ ] Hashes computed by MCP; CRLF normalized to LF (Task 1.1, 1.2).
- [ ] Operation commit protocol + idempotency + recovery (Task 2.3, 2.4).
- [ ] Verification command policy + git change policy (Task 3.3, 3.4).
- [ ] Approval gate honors `allow_non_adversarial_fallback` (Task 3.5).
- [ ] Hooks protect artifacts + gate-on-stop (Task 6.2).
- [ ] Plugin manifest + skill + commands loadable (Task 6.3).

After v1 is green, the next plans are v1.1 (auto-repair loop), v1.2 (replan), v1.3 (multi-task + final review) per spec §3.3.
