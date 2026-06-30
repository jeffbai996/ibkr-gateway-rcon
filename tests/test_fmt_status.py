"""Tests for /gateway status health output — specifically the mcp:
reachability line added to address the port-listening != service-healthy bug
(2026-04-20).
"""
from pathlib import Path

from datetime import datetime, timezone

import brief as bf
import gateway_ctl as gc


def _make_cfg(tmp_path: Path) -> gc.Config:
    """Minimal two-gateway config for formatter tests."""
    return gc.Config(
        state_dir=tmp_path,
        log_file=tmp_path / "watchdog.log",
        port_probe="ss",
        gateways=[
            gc.GatewayConfig(
                name="primary",
                port=4001,
                restart_cmd="echo noop",
                skip_file=tmp_path / "primary.skip",
            ),
            gc.GatewayConfig(
                name="secondary",
                port=4002,
                restart_cmd="echo noop",
                skip_file=tmp_path / "secondary.skip",
            ),
        ],
    )


def _build_status(
    tmp_path: Path,
    monkeypatch,
    mcp_per_gw: dict[str, dict] | None,
    port_up: bool = False,
) -> str:
    cfg = _make_cfg(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    probe = lambda _p: port_up
    monkeypatch.setattr(gc, "make_port_probe", lambda _: probe)
    data = bf.fetch_health_data(
        cfg,
        probe,
        tmp_path / "bot.heartbeat",
        180,
        now,
        mcp_per_gw,
        [],
    )
    return bf.build_health(data, now)


def test_fmt_status_without_mcp_data(tmp_path, monkeypatch):
    """mcp_per_gw=None (probe failed) → no mcp: line, preserves fallback."""
    out = _build_status(tmp_path, monkeypatch, None)
    assert "mcp:" not in out
    assert "primary" in out
    assert "state:   stopped" in out


def test_fmt_status_with_connected_mcp(tmp_path, monkeypatch):
    """Both gateways connected with fresh data → connected (Xs data)."""
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 12.0},
        "secondary": {"connected": True, "last_data_age_s": 45.0},
    }
    out = _build_status(tmp_path, monkeypatch, mcp_per_gw)
    assert "mcp:     connected (12s data)" in out
    assert "mcp:     connected (45s data)" in out


def test_fmt_status_zombie_process(tmp_path, monkeypatch):
    """THE KEY TEST: port listens but gateway unreachable via MCP.

    This is the exact '/gateway status says running but it's not working'
    case. Without the fix, user sees state:running and assumes healthy.
    With the fix, the mcp: disconnected line exposes the zombie process.
    """
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 5.0},
        "secondary": {"connected": False},
    }
    out = _build_status(tmp_path, monkeypatch, mcp_per_gw, port_up=True)
    assert "state:   running" in out
    assert "mcp:     disconnected" in out
    assert "mcp:     connected (5s data)" in out


def test_fmt_status_with_stale_data(tmp_path, monkeypatch):
    """Connected but data is >10min old → STALE flag."""
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 1500.0},  # 25m
        "secondary": {"connected": True, "last_data_age_s": 3.0},
    }
    out = _build_status(tmp_path, monkeypatch, mcp_per_gw)
    assert "mcp:     STALE (25m data)" in out
    assert "mcp:     connected (3s data)" in out


def test_fmt_status_data_age_buckets(tmp_path, monkeypatch):
    """Age < 60s → Xs, 60-600s → Ym, >=600s → STALE."""
    # 59s → seconds bucket
    out = _build_status(tmp_path, monkeypatch, {
        "primary": {"connected": True, "last_data_age_s": 59.0},
        "secondary": {"connected": True, "last_data_age_s": 59.0},
    })
    assert "mcp:     connected (59s data)" in out

    # 60s → minutes bucket (still healthy)
    out = _build_status(tmp_path, monkeypatch, {
        "primary": {"connected": True, "last_data_age_s": 60.0},
        "secondary": {"connected": True, "last_data_age_s": 60.0},
    })
    assert "mcp:     connected (1m data)" in out

    # 599s → still minutes bucket
    out = _build_status(tmp_path, monkeypatch, {
        "primary": {"connected": True, "last_data_age_s": 599.0},
        "secondary": {"connected": True, "last_data_age_s": 599.0},
    })
    assert "mcp:     connected (9m data)" in out

    # 600s → STALE
    out = _build_status(tmp_path, monkeypatch, {
        "primary": {"connected": True, "last_data_age_s": 600.0},
        "secondary": {"connected": True, "last_data_age_s": 600.0},
    })
    assert "mcp:     STALE (10m data)" in out


def test_fmt_status_unknown_gateway(tmp_path, monkeypatch):
    """mcp_per_gw missing this gateway → mcp: unknown, no crash."""
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 1.0},
        # secondary missing entirely
    }
    out = _build_status(tmp_path, monkeypatch, mcp_per_gw)
    assert "mcp: unknown" not in out
    assert "mcp:     connected (1s data)" in out


def test_fmt_status_connected_but_no_age(tmp_path, monkeypatch):
    """Connected with last_data_age_s missing → 'connected' without age."""
    mcp_per_gw = {
        "primary": {"connected": True},  # no last_data_age_s
        "secondary": {"connected": True, "last_data_age_s": 5.0},
    }
    out = _build_status(tmp_path, monkeypatch, mcp_per_gw)
    # Primary: plain "mcp: connected" with no parenthetical age
    lines = out.split("\n")
    assert any(l.strip() == "mcp:     connected" for l in lines)
    assert "mcp:     connected (5s data)" in out
