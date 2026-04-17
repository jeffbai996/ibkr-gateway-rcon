"""Tests for watchdog_tick — the pure-function core of the in-process watchdog.

Given a config + a port probe + "now", it returns an ACTION LIST: which
gateways need to be restarted (because they're down and not skipped), and
optionally side-effects on skip-files (garbage collection).

Keeping this pure means the bot loop and the deadman-mode CLI can both call
the same function with zero divergence."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway_ctl import (
    GatewayConfig,
    WatchdogAction,
    watchdog_tick,
    write_skip,
)


NOW = datetime(2026, 4, 17, 20, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path: Path, name: str, port: int) -> GatewayConfig:
    return GatewayConfig(
        name=name,
        port=port,
        restart_cmd="echo noop",
        skip_file=tmp_path / f"{name}.skip",
    )


def test_all_up_no_actions(tmp_path):
    gws = [_cfg(tmp_path, "primary", 4001), _cfg(tmp_path, "secondary", 4002)]
    actions = watchdog_tick(
        gws,
        port_listening=lambda port: True,
        now=NOW,
    )

    assert actions == []


def test_one_down_returns_restart_action(tmp_path):
    gws = [_cfg(tmp_path, "primary", 4001), _cfg(tmp_path, "secondary", 4002)]

    actions = watchdog_tick(
        gws,
        port_listening=lambda port: port != 4002,  # primary up, secondary down
        now=NOW,
    )

    assert actions == [WatchdogAction(gateway_name="secondary", reason="port_down")]


def test_down_but_skipped_indefinite_no_action(tmp_path):
    gws = [_cfg(tmp_path, "secondary", 4002)]
    write_skip(gws[0].skip_file, until=None)

    actions = watchdog_tick(
        gws, port_listening=lambda port: False, now=NOW,
    )

    assert actions == []


def test_down_but_skipped_future_no_action(tmp_path):
    gws = [_cfg(tmp_path, "secondary", 4002)]
    write_skip(gws[0].skip_file, until=NOW + timedelta(minutes=5))

    actions = watchdog_tick(
        gws, port_listening=lambda port: False, now=NOW,
    )

    assert actions == []


def test_down_with_expired_skip_still_restarts(tmp_path):
    # Skip-file past its deadline should be GC'd and restart should fire.
    gws = [_cfg(tmp_path, "secondary", 4002)]
    write_skip(gws[0].skip_file, until=NOW - timedelta(minutes=5))

    actions = watchdog_tick(
        gws, port_listening=lambda port: False, now=NOW,
    )

    assert actions == [WatchdogAction(gateway_name="secondary", reason="port_down")]
    assert not gws[0].skip_file.exists(), "expired skip should be GC'd"


def test_mixed_state(tmp_path):
    primary = _cfg(tmp_path, "primary", 4001)
    secondary = _cfg(tmp_path, "secondary", 4002)
    tertiary = _cfg(tmp_path, "tertiary", 4003)
    write_skip(secondary.skip_file, until=NOW + timedelta(minutes=30))  # paused

    actions = watchdog_tick(
        [primary, secondary, tertiary],
        port_listening=lambda port: port == 4001,  # only primary up
        now=NOW,
    )

    # primary is up (no action), secondary is down but paused (no action),
    # tertiary is down and active — restart.
    assert actions == [WatchdogAction(gateway_name="tertiary", reason="port_down")]
