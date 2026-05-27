"""Tests for port-probe construction and the netstat LISTEN detector.

make_port_probe dispatches on a config string; _contains_listen parses raw
netstat/ss output. subprocess is fully mocked — no real port probing runs.
"""
import gateway_ctl as gc
import pytest


# ─────────────────────────── make_port_probe dispatch ───────────────────────────


def test_make_port_probe_ss():
    assert gc.make_port_probe("ss") is gc._ss_probe


def test_make_port_probe_netstat():
    assert gc.make_port_probe("netstat") is gc._linux_netstat_probe


def test_make_port_probe_wsl():
    assert gc.make_port_probe("wsl-cmd-netstat") is gc._wsl_cmd_netstat_probe


def test_make_port_probe_unknown_raises():
    with pytest.raises(gc.ConfigError, match="unknown port_probe"):
        gc.make_port_probe("bogus")


# ─────────────────────────── _contains_listen (Linux) ───────────────────────────


_SS_OUTPUT = """\
State      Recv-Q Send-Q Local Address:Port  Peer Address:Port
LISTEN     0      128         0.0.0.0:4001       0.0.0.0:*
LISTEN     0      128       127.0.0.1:7497       0.0.0.0:*
ESTAB      0      0         127.0.0.1:5000     127.0.0.1:54321
"""


def test_contains_listen_finds_listening_port():
    assert gc._contains_listen(_SS_OUTPUT, 4001) is True
    assert gc._contains_listen(_SS_OUTPUT, 7497) is True


def test_contains_listen_missing_port_false():
    assert gc._contains_listen(_SS_OUTPUT, 9999) is False


def test_contains_listen_established_not_listening_false():
    # Port 5000 appears but only in ESTAB state, not LISTEN.
    assert gc._contains_listen(_SS_OUTPUT, 5000) is False


def test_contains_listen_empty_output_false():
    assert gc._contains_listen("", 4001) is False


# ─────────────────────────── _contains_listen (Windows) ───────────────────────────


_WIN_OUTPUT = """\
  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:4001           0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:7497         0.0.0.0:0              ESTABLISHED     5678
"""


def test_contains_listen_windows_style_listening():
    assert gc._contains_listen(_WIN_OUTPUT, 4001, windows_style=True) is True


def test_contains_listen_windows_style_established_false():
    assert gc._contains_listen(_WIN_OUTPUT, 7497, windows_style=True) is False


def test_contains_listen_windows_requires_windows_flag():
    # Windows output uses LISTENING; the Linux path looks for LISTEN as a
    # substring, which DOES match "LISTENING" — documents that quirk.
    assert gc._contains_listen(_WIN_OUTPUT, 4001, windows_style=False) is True


# ─────────────────────────── probe callables with mocked subprocess ──────────────


def test_ss_probe_uses_run_cmd(monkeypatch):
    monkeypatch.setattr(gc, "_run_cmd", lambda cmd: _SS_OUTPUT)
    probe = gc.make_port_probe("ss")
    assert probe(4001) is True
    assert probe(9999) is False


def test_linux_netstat_falls_back_to_ss(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd[0])
        if cmd[0] == "netstat":
            return ""  # netstat absent / empty → triggers ss fallback
        return _SS_OUTPUT

    monkeypatch.setattr(gc, "_run_cmd", fake_run)
    probe = gc.make_port_probe("netstat")
    assert probe(7497) is True
    assert "netstat" in calls and "ss" in calls


def test_run_cmd_swallows_filenotfound(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ss not installed")

    monkeypatch.setattr(gc.subprocess, "run", boom)
    # Returns "" rather than propagating — probe degrades to "port not up".
    assert gc._run_cmd(["ss", "-tln"]) == ""


def test_run_cmd_swallows_timeout(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ss", timeout=10)

    monkeypatch.setattr(gc.subprocess, "run", boom)
    assert gc._run_cmd(["ss", "-tln"]) == ""
