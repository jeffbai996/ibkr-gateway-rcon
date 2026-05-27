"""Edge-case tests for _wrap_wsl_cmd — quoting, idempotency, case, non-matches.

The base happy-path is covered by test_wsl_cmd_wrapper.py. This file probes the
regex boundaries: single-quoted bodies, the `start ` idempotency guard, cmd.exe
without /c, and embedded-quote re-quoting. No subprocess is run — _wrap_wsl_cmd
is a pure string transform.
"""
from gateway_ctl import _wrap_wsl_cmd


def test_single_quoted_body_is_rewrapped():
    cmd = "cmd.exe /c 'StartGateway.bat'"
    wrapped = _wrap_wsl_cmd(cmd)
    # Surrounding single quotes are stripped and re-quoted with double quotes.
    assert 'start "" /MIN cmd /c "StartGateway.bat"' in wrapped


def test_idempotent_when_start_already_present_lowercase():
    cmd = 'cmd.exe /c "start "" /MIN cmd /c RestartGateway.bat"'
    assert _wrap_wsl_cmd(cmd) == cmd


def test_idempotent_when_body_starts_with_start_keyword():
    # Body itself begins with `start ` (after quote strip) → left alone.
    cmd = 'cmd.exe /c "start notepad.exe"'
    assert _wrap_wsl_cmd(cmd) == cmd


def test_cmd_exe_without_slash_c_is_passthrough():
    # The regex anchors on `cmd.exe /c`; bare `cmd.exe foo` doesn't match.
    cmd = "cmd.exe /k somecommand"
    assert _wrap_wsl_cmd(cmd) == cmd


def test_empty_string_passthrough():
    assert _wrap_wsl_cmd("") == ""


def test_only_first_cmd_exe_anchors_the_wrap():
    # A path containing the literal text cmd.exe later in the body shouldn't
    # produce a second wrap. Exactly one start marker.
    cmd = "cmd.exe /c run.bat"
    wrapped = _wrap_wsl_cmd(cmd)
    assert wrapped.count('start "" /MIN cmd /c') == 1


def test_body_with_arguments_preserved():
    cmd = "cmd.exe /c RestartGateway.bat --flag value"
    wrapped = _wrap_wsl_cmd(cmd)
    assert "RestartGateway.bat --flag value" in wrapped
    assert 'start "" /MIN cmd /c' in wrapped


def test_leading_whitespace_before_cd_preserved():
    cmd = "cd /mnt/c/IBC && cmd.exe /c RestartGateway_primary.bat"
    wrapped = _wrap_wsl_cmd(cmd)
    assert wrapped.startswith("cd /mnt/c/IBC && ")


def test_multiline_body_handled_via_dotall():
    # The regex uses re.DOTALL so a body spanning newlines still matches.
    cmd = "cmd.exe /c first\nsecond"
    wrapped = _wrap_wsl_cmd(cmd)
    assert 'start "" /MIN cmd /c' in wrapped
    assert "second" in wrapped
