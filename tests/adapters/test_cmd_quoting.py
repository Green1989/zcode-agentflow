"""Task 4.4 — Windows cmd.exe injection defense (P1-1; parallel P0-5).

``cmd_script`` verification commands run under ``cmd.exe /c``, which RE-PARSES
its command line. The defense is therefore NOT string quoting but REJECTION:
any arg containing a cmd.exe metacharacter (``& | < > ^ % ! ( )``), CR/LF/NUL,
or failing the whitelist regex is rejected BEFORE launch — there is no path by
which a second command can execute.

The launcher NEVER concatenates program+args into a string; it builds a token
list and passes it to ``subprocess.run(..., shell=False)``.
"""

from __future__ import annotations

import pytest

from zcode_agentflow.adapters.verification import (
    _CMD_META, _CMD_ARG_OK, _BASH_META, _build_cmd_argv, _build_bash_argv,
    _check_cmd_arg, _check_bash_arg, VerificationError,
)


# ---------------------------------------------------------------------------
# metacharacter set + whitelist regex (parallel P0-5 full set)
# ---------------------------------------------------------------------------


def test_cmd_metacharacter_set_is_complete():
    """The rejected metacharacter set covers the cmd.exe injection surface
    (parallel P0-5)."""
    for ch in "&|<>^%!()":
        assert ch in _CMD_META, f"missing cmd metachar {ch!r}"


def test_cmd_arg_whitelist_regex():
    """The whitelist allows the safe identifier-ish set and rejects the rest."""
    import re
    ok = ["python", "pytest", "-m", "tests/test_x.py", "src/a.py", "T1",
          "C:/path", r"src\a.py", "v1.2", "--flag=value"]
    for s in ok:
        assert re.match(_CMD_ARG_OK, s), f"whitelist should accept {s!r}"
    bad = ["T1 & del x", "a;b", "a b", "a$b", "a`b", "a(b)", "%PATH%"]
    for s in bad:
        assert not re.match(_CMD_ARG_OK, s), f"whitelist should reject {s!r}"


# ---------------------------------------------------------------------------
# metacharacter rejection in args (the primary defense)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_arg", [
    "T1 & del x",      # command chaining via &
    "T1 | more",       # pipe
    "T1 > out.txt",    # redirect
    "T1 < in.txt",     # input redirect
    "T1 ^ whoami",     # caret escape
    "T1 %PATH%",       # var expansion
    "T1 !VAR!",        # delayed expansion
    "T1 (echo hi)",    # grouping
    "T1\nrm y",        # newline => second command
    "T1\rrm y",        # CR
    "T1\x00rm",        # NUL
])
def test_cmd_script_rejects_metacharacters_in_args(bad_arg):
    """Every cmd.exe metacharacter (and CR/LF/NUL) in an arg is rejected before
    launch — the arg never reaches cmd.exe."""
    with pytest.raises(VerificationError):
        _check_cmd_arg(bad_arg)


def test_cmd_script_cannot_execute_second_command():
    """The canonical injection ``T1 & del x`` is rejected; only ONE command can
    ever run (parallel P0-5 attack test)."""
    with pytest.raises(VerificationError):
        _check_cmd_arg("T1 & del x")
    with pytest.raises(VerificationError):
        _check_cmd_arg("T1\nrm -rf /")


def test_cmd_script_disables_delayed_expansion():
    """``%VAR%`` and ``!VAR!`` expansion are treated as unsafe and rejected (no
    reliance on ``cmd /v``). A test with ``%PATH%`` in args is rejected."""
    with pytest.raises(VerificationError):
        _check_cmd_arg("%PATH%")
    with pytest.raises(VerificationError):
        _check_cmd_arg("!USERPROFILE!")


def test_cmd_script_arg_whitelist_enforced():
    """Every cmd arg must match the whitelist regex (spaces disallowed by
    default)."""
    # a whitelist-conformant arg passes the arg-level check
    _check_cmd_arg("tests/test_x.py")  # no raise
    # a space-containing arg is rejected by the default whitelist
    with pytest.raises(VerificationError):
        _check_cmd_arg("two words")


# ---------------------------------------------------------------------------
# token-list shape (never concatenate program + args)
# ---------------------------------------------------------------------------


def test_cmd_script_never_concatenates_program_and_args(tmp_path):
    """The launcher builds a TOKEN LIST, never a single concatenated string.

    (P0-4: argv references the validated absolute ``script`` path, not the bare
    program string.)"""
    script = tmp_path / "run.bat"
    argv = _build_cmd_argv(script=script, args=["T1", "--opt=val"])
    assert isinstance(argv, list)
    assert argv[0] == "cmd.exe"
    # the script and each arg are SEPARATE tokens (no string built)
    assert str(script) in argv
    assert "T1" in argv
    assert "--opt=val" in argv
    # /d disables AutoRun; /s strips first/last quotes (standard safe form)
    assert "/d" in argv and "/s" in argv and "/c" in argv


def test_bash_script_rejects_shell_metacharacters():
    """``bash_script`` args with shell metacharacters are rejected."""
    for bad in ["a;b", "a|b", "a$b", "a$(whoami)", "a`id`", "a&b"]:
        with pytest.raises(VerificationError):
            _check_bash_arg(bad)


def test_bash_script_argv_shape(tmp_path):
    script = tmp_path / "run.sh"
    cwd = tmp_path
    argv = _build_bash_argv(script=script, cwd=cwd, args=["--flag", "value"])
    assert argv[0] == "bash"
    # the validated script is named (as a posix relpath from cwd)
    assert "run.sh" in argv
    assert "--flag" in argv and "value" in argv
