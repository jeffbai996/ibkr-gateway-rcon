"""Tests for smart_restart_async — the non-blocking restart path.

Background: the original gc.smart_restart wrapped subprocess.run with a 240s
timeout. When the WSL→cmd.exe bridge held the subprocess alive past that
timeout, the Discord interaction's deferred token went stale and
followup.send() returned 404 — user saw "thinking..." forever. The async
path fires the command in a detached process group via Popen, returns
immediately, and polls the port for ~10s to detect cold-start success.
For hot restarts (port was already up), port_up stays False because IBKey
auth + JVM warmup is 2-3min — the watchdog confirms via heartbeat instead.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import gateway_ctl as gc


def _make_gw(name: str = "primary", port: int = 4001) -> gc.GatewayConfig:
    return gc.GatewayConfig(
        name=name,
        port=port,
        restart_cmd=f"echo restart {name}",
        start_cmd=f"echo start {name}",
        skip_file=Path("/tmp/x.skip"),
    )


def test_hot_restart_was_already_up_returns_fast():
    """Port up before fire → port_up=False after wait (gateway is bouncing,
    won't come back up within 10s — watchdog handles it)."""
    gw = _make_gw()
    port_listening = MagicMock(return_value=True)  # always up

    with patch.object(gc, "_fire_async") as mock_fire:
        mock_fire.return_value = MagicMock(pid=12345)
        # Tight timeout so the test finishes quickly. Real callers use 10s.
        result = gc.smart_restart_async(
            gw, port_listening, success_wait_s=0.3, poll_interval_s=0.1
        )

    assert result["fired"] is True
    assert result["was_already_up"] is True
    assert result["port_up"] is False  # we don't detect down→up flips
    assert result["pid"] == 12345
    assert result["elapsed_ms"] >= 250  # waited the full ~300ms
    mock_fire.assert_called_once_with(gw.restart_cmd)


def test_cold_start_port_comes_up_short_circuits():
    """Port down before fire, comes up mid-poll → port_up=True, returns early."""
    gw = _make_gw()
    # First call (was_up_before) → False; subsequent → True
    port_listening = MagicMock(side_effect=[False, True])

    with patch.object(gc, "_fire_async") as mock_fire:
        mock_fire.return_value = MagicMock(pid=99)
        result = gc.smart_restart_async(
            gw, port_listening, success_wait_s=2.0, poll_interval_s=0.1
        )

    assert result["fired"] is True
    assert result["was_already_up"] is False
    assert result["port_up"] is True
    assert result["pid"] == 99
    # Should return well before the 2.0s deadline
    assert result["elapsed_ms"] < 1000
    # Port-down path prefers start_cmd over restart_cmd
    mock_fire.assert_called_once_with(gw.start_cmd)
