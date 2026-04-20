"""Tests for _fmt_status output — specifically the mcp: reachability line
added to address the port-listening != service-healthy bug (2026-04-20).
"""
from pathlib import Path

import discord_bot as db
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


def test_fmt_age_relative_short_boundaries():
    """Each boundary flips to the next unit cleanly."""
    assert db._fmt_age_relative_short(5) == "5s"
    assert db._fmt_age_relative_short(59) == "59s"
    assert db._fmt_age_relative_short(60) == "1m"
    assert db._fmt_age_relative_short(3599) == "59m"
    assert db._fmt_age_relative_short(3600) == "1.0h"
    assert db._fmt_age_relative_short(86399) == "24.0h"
    assert db._fmt_age_relative_short(86400) == "1.0d"
    assert db._fmt_age_relative_short(172800) == "2.0d"


def test_fmt_status_without_mcp_data(tmp_path, monkeypatch):
    """mcp_per_gw=None (probe failed) → no mcp: line, preserves fallback."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: False)
    out = db._fmt_status(cfg, mcp_per_gw=None)
    assert "mcp:" not in out
    assert "primary (port 4001)" in out
    assert "state:   stopped" in out


def test_fmt_status_with_connected_mcp(tmp_path, monkeypatch):
    """Both gateways connected with fresh data → connected (Xs data)."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: True)
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 12.0},
        "secondary": {"connected": True, "last_data_age_s": 45.0},
    }
    out = db._fmt_status(cfg, mcp_per_gw=mcp_per_gw)
    assert "mcp: connected (12s data)" in out
    assert "mcp: connected (45s data)" in out


def test_fmt_status_zombie_process(tmp_path, monkeypatch):
    """THE KEY TEST: port listens but gateway unreachable via MCP.

    This is the exact '/gateway status says running but it's not working'
    case. Without the fix, user sees state:running and assumes healthy.
    With the fix, the mcp: disconnected line exposes the zombie process.
    """
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: True)
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 5.0},
        "secondary": {"connected": False},
    }
    out = db._fmt_status(cfg, mcp_per_gw=mcp_per_gw)
    assert "state:   running" in out  # port still listening
    assert "mcp: disconnected" in out  # but mcp can't reach it
    assert "mcp: connected (5s data)" in out  # primary healthy


def test_fmt_status_with_stale_data(tmp_path, monkeypatch):
    """Connected but data is >10min old → STALE flag."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: True)
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 1500.0},  # 25m
        "secondary": {"connected": True, "last_data_age_s": 3.0},
    }
    out = db._fmt_status(cfg, mcp_per_gw=mcp_per_gw)
    assert "mcp: STALE (25m data)" in out
    assert "mcp: connected (3s data)" in out


def test_fmt_status_data_age_buckets(tmp_path, monkeypatch):
    """Age < 60s → Xs, 60-600s → Ym, >=600s → STALE."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: True)

    # 59s → seconds bucket
    out = db._fmt_status(cfg, mcp_per_gw={
        "primary": {"connected": True, "last_data_age_s": 59.0},
        "secondary": {"connected": True, "last_data_age_s": 59.0},
    })
    assert "mcp: connected (59s data)" in out

    # 60s → minutes bucket (still healthy)
    out = db._fmt_status(cfg, mcp_per_gw={
        "primary": {"connected": True, "last_data_age_s": 60.0},
        "secondary": {"connected": True, "last_data_age_s": 60.0},
    })
    assert "mcp: connected (1m data)" in out

    # 599s → still minutes bucket
    out = db._fmt_status(cfg, mcp_per_gw={
        "primary": {"connected": True, "last_data_age_s": 599.0},
        "secondary": {"connected": True, "last_data_age_s": 599.0},
    })
    assert "mcp: connected (9m data)" in out

    # 600s → STALE
    out = db._fmt_status(cfg, mcp_per_gw={
        "primary": {"connected": True, "last_data_age_s": 600.0},
        "secondary": {"connected": True, "last_data_age_s": 600.0},
    })
    assert "mcp: STALE (10m data)" in out


def test_fmt_status_unknown_gateway(tmp_path, monkeypatch):
    """mcp_per_gw missing this gateway → mcp: unknown, no crash."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: True)
    mcp_per_gw = {
        "primary": {"connected": True, "last_data_age_s": 1.0},
        # secondary missing entirely
    }
    out = db._fmt_status(cfg, mcp_per_gw=mcp_per_gw)
    assert "mcp: unknown" in out
    assert "mcp: connected (1s data)" in out


def test_fmt_status_connected_but_no_age(tmp_path, monkeypatch):
    """Connected with last_data_age_s missing → 'connected' without age."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(gc, "make_port_probe", lambda _: lambda _p: True)
    mcp_per_gw = {
        "primary": {"connected": True},  # no last_data_age_s
        "secondary": {"connected": True, "last_data_age_s": 5.0},
    }
    out = db._fmt_status(cfg, mcp_per_gw=mcp_per_gw)
    # Primary: plain "mcp: connected" with no parenthetical age
    lines = out.split("\n")
    assert any(l.strip() == "mcp: connected" for l in lines)
    assert "mcp: connected (5s data)" in out
