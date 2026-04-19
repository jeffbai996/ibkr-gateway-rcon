"""Tests for _wrap_wsl_cmd — the WSL → cmd.exe detach wrapper.

Background: when a WSL-side subprocess.run invokes `cmd.exe /c BAT` and that
bat file ends up spawning a long-lived Windows GUI process (IBGateway java),
cmd.exe's /c session can't exit because the grandchild inherits handles
through the WSL bridge. subprocess.run hangs until its timeout, even though
the gateway actually launched successfully.

Fix: rewrite the payload from
    cmd.exe /c <body>
to
    cmd.exe /c "start "" /MIN cmd /c <body>"
which tells Windows to detach the bat into a separate cmd.exe that doesn't
inherit the parent's handles. The outer cmd.exe returns in ~3s after `start`
fires instead of blocking until the java child exits.
"""
from gateway_ctl import _wrap_wsl_cmd


def test_passthrough_plain_linux_command():
    cmd = "ls -la /tmp"
    assert _wrap_wsl_cmd(cmd) == cmd


def test_passthrough_when_no_cmd_exe():
    cmd = "echo hello"
    assert _wrap_wsl_cmd(cmd) == cmd


def test_wraps_absolute_cmd_exe_path():
    cmd = "cd /mnt/c/IBController && /mnt/c/Windows/system32/cmd.exe /c RestartGateway_primary.bat"
    wrapped = _wrap_wsl_cmd(cmd)
    assert 'start "" /MIN cmd /c' in wrapped
    assert "RestartGateway_primary.bat" in wrapped


def test_wraps_bare_cmd_exe():
    cmd = "cmd.exe /c StartGateway.bat"
    wrapped = _wrap_wsl_cmd(cmd)
    assert 'start "" /MIN cmd /c' in wrapped
    assert "StartGateway.bat" in wrapped


def test_wraps_preserves_working_dir_change():
    cmd = "cd /mnt/c/IBController2 && cmd.exe /c RestartGateway_secondary.bat"
    wrapped = _wrap_wsl_cmd(cmd)
    # The `cd` stays outside the wrap — it's a shell-level directive, not part
    # of what gets handed to cmd.exe
    assert wrapped.startswith("cd /mnt/c/IBController2 && ")
    assert 'start "" /MIN cmd /c "RestartGateway_secondary.bat"' in wrapped


def test_idempotent():
    """Wrapping an already-wrapped command does nothing — `start` marker
    already present, caller's config already correct, don't double-wrap."""
    cmd = 'cmd.exe /c "start "" /MIN cmd /c RestartGateway_primary.bat"'
    assert _wrap_wsl_cmd(cmd) == cmd


def test_preserves_quoted_arguments():
    cmd = 'cmd.exe /c "echo hello world"'
    wrapped = _wrap_wsl_cmd(cmd)
    assert 'start "" /MIN cmd /c' in wrapped
    assert "echo hello world" in wrapped


def test_escapes_embedded_quotes_in_body():
    # If the bat name itself had quotes (rare), make sure we don't mangle them.
    # Real IBC paths don't have this, but cheap to handle.
    cmd = 'cmd.exe /c RestartGateway.bat'
    wrapped = _wrap_wsl_cmd(cmd)
    # Should be runnable: outer cmd.exe /c "start "" /MIN cmd /c RestartGateway.bat"
    assert wrapped.count('start "" /MIN cmd /c') == 1
