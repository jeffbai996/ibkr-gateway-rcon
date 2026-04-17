"""Tests for Gateway status aggregation — ties port probe + skip-file state
+ last-restart-log parsing into a single shape the UI layers render."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway_ctl import (
    GatewayConfig,
    GatewayStatus,
    status_for,
    write_skip,
)


NOW = datetime(2026, 4, 17, 20, 0, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path, name: str, port: int) -> GatewayConfig:
    return GatewayConfig(
        name=name,
        port=port,
        restart_cmd="echo noop",
        skip_file=tmp_path / f"{name}.skip",
    )


def test_status_up_and_not_skipped(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    got = status_for(
        cfg,
        port_listening=lambda port: True,
        log_path=tmp_path / "watchdog.log",
        now=NOW,
    )

    assert isinstance(got, GatewayStatus)
    assert got.name == "primary"
    assert got.up is True
    assert got.skipped is False
    assert got.skipped_until is None


def test_status_down_and_not_skipped(tmp_path):
    cfg = make_config(tmp_path, "secondary", 4002)
    got = status_for(
        cfg,
        port_listening=lambda port: False,
        log_path=tmp_path / "watchdog.log",
        now=NOW,
    )

    assert got.up is False
    assert got.skipped is False


def test_status_skipped_indefinite(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    write_skip(cfg.skip_file, until=None)

    got = status_for(
        cfg,
        port_listening=lambda port: False,
        log_path=tmp_path / "watchdog.log",
        now=NOW,
    )

    assert got.skipped is True
    assert got.skipped_until is None


def test_status_skipped_until_future(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    deadline = NOW + timedelta(minutes=30)
    write_skip(cfg.skip_file, until=deadline)

    got = status_for(
        cfg,
        port_listening=lambda port: False,
        log_path=tmp_path / "watchdog.log",
        now=NOW,
    )

    assert got.skipped is True
    assert got.skipped_until == deadline


def test_status_last_restart_parses_log(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    log = tmp_path / "watchdog.log"
    log.write_text(
        "2026-04-17 10:00:00 — port 4001 not listening, restarting primary gateway\n"
        "2026-04-17 10:00:05 — primary restart command issued\n"
        "2026-04-17 11:00:00 — port 4002 not listening, restarting secondary gateway\n"
        "2026-04-17 11:00:05 — secondary restart command issued\n"
    )

    got = status_for(
        cfg,
        port_listening=lambda port: True,
        log_path=log,
        now=NOW,
    )

    assert got.last_restart_at == datetime(2026, 4, 17, 10, 0, 5, tzinfo=timezone.utc)


def test_status_last_restart_none_when_no_log(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    got = status_for(
        cfg,
        port_listening=lambda port: True,
        log_path=tmp_path / "nope.log",
        now=NOW,
    )

    assert got.last_restart_at is None


def test_status_last_restart_none_for_other_gateway(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    log = tmp_path / "watchdog.log"
    log.write_text(
        "2026-04-17 11:00:00 — port 4002 not listening, restarting secondary gateway\n"
        "2026-04-17 11:00:05 — secondary restart command issued\n"
    )

    got = status_for(
        cfg,
        port_listening=lambda port: True,
        log_path=log,
        now=NOW,
    )

    # Only secondary restarts in the log, not primary.
    assert got.last_restart_at is None


def test_status_picks_most_recent_restart(tmp_path):
    cfg = make_config(tmp_path, "primary", 4001)
    log = tmp_path / "watchdog.log"
    log.write_text(
        "2026-04-17 10:00:05 — primary restart command issued\n"
        "2026-04-17 10:30:10 — primary restart command issued\n"
        "2026-04-17 10:15:07 — primary restart command issued\n"
    )

    got = status_for(
        cfg,
        port_listening=lambda port: True,
        log_path=log,
        now=NOW,
    )

    # Takes the latest by timestamp, not last line.
    assert got.last_restart_at == datetime(2026, 4, 17, 10, 30, 10, tzinfo=timezone.utc)
