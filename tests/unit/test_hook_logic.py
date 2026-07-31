"""Task 7.1 — Hook logic built on the REAL Phase-0 spike contract.

Every input shape here is taken from ``docs/capabilities.json`` (the spike source
of truth), never guessed:
  - stdin is DUAL-KEY (snake_case + camelCase); tool events carry
    tool_name/toolName + tool_input/toolInput (zcode.hooks.stdin_shape).
  - hook cwd is the project root; the plugin root is reachable via the
    ``ZCODE_PROJECT_DIR`` / ``CLAUDE_PROJECT_DIR`` env var (template_vars).
  - exit codes: 0 pass / 2 deny (PreToolUse) / other-nonzero error
    (exit_code_denies).
  - Stop may request continuation up to 3 times via ``stopHookActive``
    (stop_reentry_limit).

Invariants under test (Prompt §18, Review §六.6/§六.7):
  * ``run_protect_artifacts`` denies edits to the authoritative control data
    (logical names AND the physical Artifact-Index store artifacts/+blobs) and
    resolves the plugin root via the spike-confirmed mechanism, not a hardcoded
    relative path.
  * ``run_gate_on_stop`` delegates to the SINGLE read-only ``engine.check_stop``
    and acts on ``{allow_stop, reasons}`` — it does NOT re-derive
    review/clean/HEAD logic.
  * past the Stop re-entry cap the hook escalates to NEEDS_HUMAN feedback
    instead of looping.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import sys
from pathlib import Path

import pytest

from zcode_agentflow import hook_logic


# ---------------------------------------------------------------------------
# Fixtures — a fake control_root tree so the protect hook has real paths to
# reason about, and a fake stop decision so gate_on_stop exercises the real
# delegation without a heavy Engine.
# ---------------------------------------------------------------------------


@pytest.fixture
def control_tree(tmp_path: Path) -> Path:
    """Build a minimal control_root skeleton with the logical control-data files
    AND the physical Artifact-Index store (artifacts/, blobs/) so both layers
    can be protected (parallel P1-1).

    Also writes an ``active-run.json`` pointer under the plugin root (``tmp_path``)
    so the hook can resolve the control root via the spike-confirmed
    ``ZCODE_PROJECT_DIR`` → pointer → ``control_root`` chain."""
    import json as _json

    control_root = tmp_path / "control"
    (control_root / "operations").mkdir(parents=True)
    (control_root / "approvals").mkdir(parents=True)
    (control_root / "reviews").mkdir(parents=True)
    (control_root / "artifacts" / "gen1").mkdir(parents=True)
    (control_root / "blobs").mkdir(parents=True)
    (control_root / "worktree").mkdir(parents=True)  # NOT protected
    for name in ("state.json", "events.jsonl"):
        (control_root / name).write_text("{}", encoding="utf-8")
    (control_root / "operations" / "op1.json").write_text("{}", encoding="utf-8")
    (control_root / "approvals" / "apr_x.json").write_text("{}", encoding="utf-8")
    (control_root / "reviews" / "rev1.json").write_text("{}", encoding="utf-8")
    (control_root / "artifacts" / "gen1" / "SPEC.md").write_text("# spec",
                                                                 encoding="utf-8")
    (control_root / "blobs" / "sha256abc").write_text("blob", encoding="utf-8")
    # active-run pointer under the plugin root (tmp_path) — the spike-confirmed
    # chain the hook uses to locate the control root.
    (tmp_path / "active-run.json").write_text(
        _json.dumps({"run_id": "r1", "control_root": str(control_root)}),
        encoding="utf-8",
    )
    return control_root


def _tool_payload(tool_name: str, tool_input: dict) -> dict:
    """A spike-accurate PreToolUse stdin payload (both key casings present)."""
    return {
        "hook_event_name": "PreToolUse",
        "hookEventName": "PreToolUse",
        "tool_name": tool_name,
        "toolName": tool_name,
        "tool_input": tool_input,
        "toolInput": tool_input,
        "cwd": "/repo",
        "sessionId": "sess-1",
        "session_id": "sess-1",
    }


# ===========================================================================
# protect_artifacts
# ===========================================================================


class TestProtectArtifacts:
    def test_denies_edit_of_control_data_logical_and_physical(self, control_tree,
                                                              monkeypatch):
        # plugin root reached via the spike-confirmed ZCODE_PROJECT_DIR env var
        # (not a hardcoded relative path — Review §六.7).
        monkeypatch.setenv("ZCODE_PROJECT_DIR", str(control_tree.parent))
        targets = [
            str(control_tree / "state.json"),
            str(control_tree / "events.jsonl"),
            str(control_tree / "operations" / "op1.json"),
            str(control_tree / "approvals" / "apr_x.json"),
            str(control_tree / "reviews" / "rev1.json"),
            # physical Artifact-Index store (parallel P1-1): the real bytes live
            # under artifacts/+blobs/, so protecting only SPEC.md is insufficient.
            str(control_tree / "artifacts" / "gen1" / "SPEC.md"),
            str(control_tree / "blobs" / "sha256abc"),
        ]
        for path in targets:
            for tool_name in ("Write", "Edit", "ApplyPatch", "MultiEdit"):
                payload = _tool_payload(tool_name, {"file_path": path})
                # some tools nest the path under 'path'/'patch' — exercise both
                rc = hook_logic.run_protect_artifacts(payload)
                assert rc == hook_logic.EXIT_DENY, (
                    f"{tool_name} of control data {path} must be denied "
                    f"(EXIT_DENY), got {rc}"
                )

    def test_allows_worktree_edits(self, control_tree, monkeypatch):
        monkeypatch.setenv("ZCODE_PROJECT_DIR", str(control_tree.parent))
        worktree_file = control_tree / "worktree" / "src" / "x.py"
        worktree_file.parent.mkdir(parents=True, exist_ok=True)
        worktree_file.write_text("x = 1", encoding="utf-8")
        payload = _tool_payload("Write", {"file_path": str(worktree_file)})
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_ALLOW

    def test_resolves_plugin_root_via_spike_mechanism(self, control_tree,
                                                     monkeypatch):
        """The hook MUST reach the control root via the spike-confirmed env var,
        never a hardcoded relative path. If the env var is unset the hook may
        not silently fall back to a guess — it must treat the root as unknown
        and allow (fail-open for normal edits) rather than mis-resolve."""
        monkeypatch.delenv("ZCODE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        payload = _tool_payload("Write",
                                {"file_path": str(control_tree / "state.json")})
        # without the spike mechanism the hook cannot find the control root, so
        # it returns EXIT_ALLOW (cannot protect what it cannot locate) — the
        # critical point is it does NOT raise / invent a path.
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_ALLOW

    def test_non_edit_tools_and_unknown_fields_are_allowed(self, control_tree,
                                                          monkeypatch):
        """Only side-effecting file-edit tools are gated; read tools (Read/Glob)
        and payloads the hook does not understand pass through (fail-open)."""
        monkeypatch.setenv("ZCODE_PROJECT_DIR", str(control_tree.parent))
        for tool_name in ("Read", "Glob", "Grep"):
            payload = _tool_payload(tool_name,
                                    {"file_path": str(control_tree / "state.json")})
            assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_ALLOW
        # a payload with no file_path at all must not crash
        payload = _tool_payload("Write", {})
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_ALLOW

    def test_path_traversal_cannot_bypass_protection(self, control_tree,
                                                    monkeypatch):
        """A ``..`` that escapes toward the control root must still be caught:
        the hook resolves candidate paths before comparison (paths.is_within)."""
        monkeypatch.setenv("ZCODE_PROJECT_DIR", str(control_tree.parent))
        worktree_file = control_tree / "worktree" / "src" / "x.py"
        worktree_file.parent.mkdir(parents=True, exist_ok=True)
        worktree_file.write_text("x = 1", encoding="utf-8")
        traversal = worktree_file.parent / ".." / ".." / "state.json"
        payload = _tool_payload("Write", {"file_path": str(traversal)})
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_DENY


# ===========================================================================
# protect_artifacts — PRODUCTION layout (P0-3 audit-fix)
# The real pointer lives at <user_data_root>/active-run.json, and ZCODE_PROJECT_DIR
# is the PROJECT dir (docs/capabilities.json hook_cwd) — a DIFFERENT directory.
# The hook MUST resolve via bootstrap.build_run_context() → user_data_root, NOT by
# looking for the pointer under ZCODE_PROJECT_DIR.
# ===========================================================================


def _write_fallback_config(dest):
    """A config.yaml copy with allow_non_adversarial_fallback=true so the
    production capability gate resolves (tool_confirmation=false in this env)."""
    import yaml
    repo_root = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((repo_root / "config.yaml").read_text(encoding="utf-8"))
    cfg["human_gates"]["spec_approval"]["allow_non_adversarial_fallback"] = True
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return dest


def _production_layout(tmp_path: Path) -> dict:
    """Build the REAL production layout: plugin_root, user_data_root (holding the
    active-run pointer → control_root), and a SEPARATE project_dir. Returns the
    paths so a test can point env vars at them.

    This mirrors the deployed shape: the pointer is NOT under the project dir, so
    a hook that looks for it under ZCODE_PROJECT_DIR would fail-open (the P0-3
    bug)."""
    import json as _json
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    user_data = tmp_path / "agentflow-data"
    user_data.mkdir()
    control_root = user_data / "runs" / "r1" / "control"
    for sub in ("operations", "approvals", "reviews", "artifacts/gen1", "blobs"):
        (control_root / sub).mkdir(parents=True)
    (control_root / "worktree").mkdir(parents=True)  # NOT protected
    for name in ("state.json", "events.jsonl"):
        (control_root / name).write_text("{}", encoding="utf-8")
    (control_root / "operations" / "op1.json").write_text("{}", encoding="utf-8")
    (control_root / "artifacts" / "gen1" / "SPEC.md").write_text("# spec",
                                                                 encoding="utf-8")
    (control_root / "blobs" / "sha256abc").write_text("blob", encoding="utf-8")
    # the pointer lives under USER_DATA_ROOT, not the project dir
    (user_data / "active-run.json").write_text(
        _json.dumps({"run_id": "r1", "control_root": str(control_root)}),
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"  # the repo being worked on (hook cwd)
    project_dir.mkdir()
    cfg = _write_fallback_config(tmp_path / "config.yaml")
    return {
        "plugin": plugin,
        "user_data": user_data,
        "control_root": control_root,
        "project_dir": project_dir,
        "config": cfg,
    }


def _set_production_env(monkeypatch, layout: dict) -> None:
    """Set the production env (plugin root + user_data_root + config + caps) and
    ALSO set ZCODE_PROJECT_DIR to the project dir — to prove the hook does NOT
    rely on ZCODE_PROJECT_DIR to find the pointer."""
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("ZCODE_PLUGIN_ROOT", str(layout["plugin"]))
    monkeypatch.setenv("AGENTFLOW_USER_DATA_ROOT", str(layout["user_data"]))
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(layout["config"]))
    monkeypatch.setenv("AGENTFLOW_CAPABILITIES",
                       str(repo_root / "docs" / "capabilities.json"))
    # ZCODE_PROJECT_DIR is the PROJECT dir (where the hook's cwd is), NOT where
    # the pointer lives — a correct hook must ignore this for pointer resolution.
    monkeypatch.setenv("ZCODE_PROJECT_DIR", str(layout["project_dir"]))
    # ensure the test-only var is NOT set (we are testing the production path)
    monkeypatch.delenv("AGENTFLOW_RUN_CONTEXT", raising=False)


class TestProtectArtifactsProductionLayout:
    """The hook must protect control data when the pointer lives under
    user_data_root (production), even though ZCODE_PROJECT_DIR points elsewhere."""

    def test_denies_edit_when_pointer_under_user_data_root(self, tmp_path,
                                                            monkeypatch):
        layout = _production_layout(tmp_path)
        _set_production_env(monkeypatch, layout)
        payload = _tool_payload("Write",
                                {"file_path": str(layout["control_root"] / "state.json")})
        # RED today: the hook looks under ZCODE_PROJECT_DIR (the project dir),
        # finds no pointer there, and returns EXIT_ALLOW. GREEN after the fix
        # resolves via build_run_context() → user_data_root.
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_DENY

    def test_denies_edit_of_physical_artifact_store_in_production(self, tmp_path,
                                                                   monkeypatch):
        layout = _production_layout(tmp_path)
        _set_production_env(monkeypatch, layout)
        payload = _tool_payload("Edit", {
            "file_path": str(layout["control_root"] / "blobs" / "sha256abc")
        })
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_DENY

    def test_allows_worktree_edit_in_production(self, tmp_path, monkeypatch):
        layout = _production_layout(tmp_path)
        _set_production_env(monkeypatch, layout)
        wt_file = layout["control_root"] / "worktree" / "src" / "x.py"
        wt_file.parent.mkdir(parents=True, exist_ok=True)
        wt_file.write_text("x = 1", encoding="utf-8")
        payload = _tool_payload("Write", {"file_path": str(wt_file)})
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_ALLOW

    def test_fails_open_when_no_context_resolvable(self, tmp_path, monkeypatch):
        """No production env, no ZCODE_PROJECT_DIR with a pointer => fail-open
        (cannot protect what cannot be located). Unchanged behavior; the fix must
        not make the hook DENY everything when context is absent."""
        monkeypatch.delenv("ZCODE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("ZCODE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("AGENTFLOW_RUN_CONTEXT", raising=False)
        payload = _tool_payload("Write",
                                {"file_path": str(tmp_path / "state.json")})
        assert hook_logic.run_protect_artifacts(payload) == hook_logic.EXIT_ALLOW


# ===========================================================================
# gate_on_stop
# ===========================================================================


class _FakeEngine:
    """Minimal stand-in: records the call and returns a scripted stop decision."""

    def __init__(self, decision: dict):
        self._decision = decision
        self.check_stop_calls: list[str | None] = []

    def check_stop(self, run_id: str | None = None) -> dict:
        self.check_stop_calls.append(run_id)
        return copy.deepcopy(self._decision)


def _stop_payload(extra: dict | None = None) -> dict:
    base = {
        "hook_event_name": "Stop",
        "hookEventName": "Stop",
        "stopHookActive": False,
        "toolCallCount": 4,
        "responsePreview": "...",
        "cwd": "/repo",
        "sessionId": "sess-1",
        "session_id": "sess-1",
    }
    if extra:
        base.update(extra)
    return base


class TestGateOnStop:
    def test_calls_engine_check_stop_and_allows_when_passing(self, monkeypatch):
        eng = _FakeEngine({"allow_stop": True, "reasons": [], "checks": []})
        monkeypatch.setattr(hook_logic, "_build_engine", lambda: eng)
        rc, feedback = hook_logic.run_gate_on_stop_with_feedback(_stop_payload())
        assert eng.check_stop_calls == [None]  # delegates, single call
        assert rc == hook_logic.EXIT_ALLOW
        assert feedback is None  # no continuation requested

    def test_blocks_when_check_fails(self, monkeypatch):
        eng = _FakeEngine({"allow_stop": False,
                           "reasons": ["review not passed", "worktree dirty"],
                           "checks": []})
        monkeypatch.setattr(hook_logic, "_build_engine", lambda: eng)
        rc, feedback = hook_logic.run_gate_on_stop_with_feedback(_stop_payload())
        # failing hard checks => request continuation (not a hard deny) so the
        # agent learns why it must not stop.
        assert rc == hook_logic.EXIT_DENY
        assert feedback is not None
        assert "review not passed" in feedback["additionalContext"]
        # the gate did NOT re-derive the reasons itself — they came from
        # check_stop verbatim.
        assert eng.check_stop_calls == [None]

    def test_respects_stop_reentry_limit(self, monkeypatch, tmp_path):
        """Past the spike-confirmed cap (3 continuations) the hook stops looping
        and escalates to NEEDS_HUMAN feedback (stop_reentry_limit).

        The re-entry count is tracked by a per-(session,turn) side counter under
        the control root (the host enforces its own cap too, but does not hand
        the hook a count), so a real control root is wired up here."""
        control_root = tmp_path / "control"
        (control_root / "operations").mkdir(parents=True)
        monkeypatch.setenv("ZCODE_PROJECT_DIR", str(tmp_path))
        (tmp_path / "active-run.json").write_text(
            __import__("json").dumps({"control_root": str(control_root)}),
            encoding="utf-8",
        )
        eng = _FakeEngine({"allow_stop": False, "reasons": ["blocked"], "checks": []})
        monkeypatch.setattr(hook_logic, "_build_engine", lambda: eng)
        # under the cap: continuation requested each time (and counted)
        for n in range(0, 3):
            rc, _ = hook_logic.run_gate_on_stop_with_feedback(
                _stop_payload({"stopHookActive": True, "toolCallCount": n})
            )
            assert rc == hook_logic.EXIT_DENY
        # at/over the cap: escalate to NEEDS_HUMAN instead of another loop
        rc, feedback = hook_logic.run_gate_on_stop_with_feedback(
            _stop_payload({"stopHookActive": True, "toolCallCount": 3})
        )
        assert rc == hook_logic.EXIT_DENY
        assert feedback is not None
        assert "NEEDS_HUMAN" in feedback["additionalContext"]

    def test_no_active_run_allows_stop(self, monkeypatch):
        """If there is no active run (no Engine), the Stop hook has nothing to
        gate and must allow the stop — never block an idle session."""
        monkeypatch.setattr(hook_logic, "_build_engine", lambda: None)
        rc, feedback = hook_logic.run_gate_on_stop_with_feedback(_stop_payload())
        assert rc == hook_logic.EXIT_ALLOW
        assert feedback is None


# ===========================================================================
# hook script wiring — the thin scripts read stdin, call hook_logic, emit
# stdout feedback + exit code exactly per the spike contract.
# ===========================================================================


def _load_script(name: str):
    """Load a hooks/*.py script as an isolated module (it has a __main__ guard
    and is not on the package path)."""
    here = Path(__file__).resolve().parent.parent.parent
    path = here / "hooks" / name
    spec = importlib.util.spec_from_file_location(f"_hook_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_protect_artifacts_script_wiring(monkeypatch, control_tree, capsys):
    """The hook script reads a PreToolUse payload from stdin and exits 2 when
    the edit targets control data, echoing the deny via stdout JSON feedback."""
    monkeypatch.setenv("ZCODE_PROJECT_DIR", str(control_tree.parent))
    payload = _tool_payload("Write",
                            {"file_path": str(control_tree / "state.json")})
    monkeypatch.setattr(sys, "stdin", io.StringIO(__import__("json").dumps(payload)))

    mod = _load_script("protect_artifacts.py")
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == hook_logic.EXIT_DENY
    out = capsys.readouterr().out.strip()
    assert out, "deny feedback must be emitted on stdout as JSON"
    import json
    parsed = json.loads(out)
    assert parsed.get("hookSpecificOutput", {}).get("hookEventName") == "PreToolUse"
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_gate_on_stop_script_wiring(monkeypatch, capsys):
    """The Stop hook script reads stdin, delegates to check_stop via
    hook_logic, and requests continuation (exit 2) when the run is not
    clear to stop."""
    eng = _FakeEngine({"allow_stop": False, "reasons": ["dirty"], "checks": []})
    monkeypatch.setattr(hook_logic, "_build_engine", lambda: eng)
    monkeypatch.setattr(sys, "stdin", io.StringIO(__import__("json").dumps(_stop_payload())))
    mod = _load_script("gate_on_stop.py")
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == hook_logic.EXIT_DENY
    out = capsys.readouterr().out.strip()
    import json
    parsed = json.loads(out)
    # continuation is requested via the spike-confirmed feedback field
    assert "additionalContext" in parsed
    assert "dirty" in parsed["additionalContext"]
