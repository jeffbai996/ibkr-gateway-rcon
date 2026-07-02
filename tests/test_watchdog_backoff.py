"""Tests for restart backoff in watchdog_tick.

Jeff's spec (2026-07-01): when a gateway goes down, restart immediately;
if still down, retry after 5 min, then 10, then 15. After that, STOP
auto-restarting and wait for a manual /gateway restart (which resets the
state). Without backoff the watchdog restart-stormed every 3 minutes
during a 2FA relogin, killing each pending Second Factor dialog."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway_ctl import (
    GatewayConfig,
    BackoffState,
    reset_backoff,
    watchdog_tick,
)

NOW = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path: Path, name: str = "primary", port: int = 4001) -> GatewayConfig:
    return GatewayConfig(
        name=name,
        port=port,
        restart_cmd="echo noop",
        skip_file=tmp_path / f"{name}.skip",
    )


def _tick(gws, backoff, now, up=False):
    return watchdog_tick(gws, port_listening=lambda p: up, now=now, backoff=backoff)


def test_first_detection_restarts_immediately(tmp_path):
    gws = [_cfg(tmp_path)]
    backoff = {}
    actions = _tick(gws, backoff, NOW)
    assert [a.reason for a in actions] == ["port_down"]
    assert backoff["primary"].attempts == 1


def test_within_first_window_no_action(tmp_path):
    gws = [_cfg(tmp_path)]
    backoff = {}
    _tick(gws, backoff, NOW)
    actions = _tick(gws, backoff, NOW + timedelta(minutes=4, seconds=59))
    assert actions == []
    assert backoff["primary"].attempts == 1


def test_retries_at_5_10_15_minutes(tmp_path):
    gws = [_cfg(tmp_path)]
    backoff = {}
    t = NOW
    _tick(gws, backoff, t)                                # attempt 1 (immediate)
    t += timedelta(minutes=5)
    assert len(_tick(gws, backoff, t)) == 1               # attempt 2 (+5)
    assert len(_tick(gws, backoff, t + timedelta(minutes=9))) == 0
    t += timedelta(minutes=10)
    assert len(_tick(gws, backoff, t)) == 1               # attempt 3 (+10)
    assert len(_tick(gws, backoff, t + timedelta(minutes=14))) == 0
    t += timedelta(minutes=15)
    actions = _tick(gws, backoff, t)                      # attempt 4 (+15, final)
    assert [a.reason for a in actions] == ["port_down"]
    assert backoff["primary"].attempts == 4


def test_exhausted_emits_gave_up_once_then_silence(tmp_path):
    gws = [_cfg(tmp_path)]
    backoff = {}
    t = NOW
    _tick(gws, backoff, t)
    for delay in (5, 10, 15):
        t += timedelta(minutes=delay)
        _tick(gws, backoff, t)
    # Next probe failure after the final attempt: one gave_up action
    t += timedelta(minutes=5)
    actions = _tick(gws, backoff, t)
    assert [a.reason for a in actions] == ["gave_up"]
    # And then nothing, forever, until reset
    for extra in range(1, 20):
        assert _tick(gws, backoff, t + timedelta(minutes=3 * extra)) == []


def test_port_recovery_resets_state(tmp_path):
    gws = [_cfg(tmp_path)]
    backoff = {}
    _tick(gws, backoff, NOW)
    assert backoff["primary"].attempts == 1
    _tick(gws, backoff, NOW + timedelta(minutes=1), up=True)
    assert "primary" not in backoff
    # New outage starts a fresh cycle with an immediate restart
    actions = _tick(gws, backoff, NOW + timedelta(minutes=2))
    assert [a.reason for a in actions] == ["port_down"]
    assert backoff["primary"].attempts == 1


def test_manual_reset_reenables_watchdog(tmp_path):
    gws = [_cfg(tmp_path)]
    backoff = {}
    t = NOW
    _tick(gws, backoff, t)
    for delay in (5, 10, 15, 5):
        t += timedelta(minutes=delay)
        _tick(gws, backoff, t)  # exhausts + gave_up
    reset_backoff(backoff, "primary")
    actions = _tick(gws, backoff, t + timedelta(minutes=1))
    assert [a.reason for a in actions] == ["port_down"]
    assert backoff["primary"].attempts == 1


def test_legacy_call_without_backoff_keeps_old_behavior(tmp_path):
    gws = [_cfg(tmp_path)]
    for i in range(3):
        actions = watchdog_tick(
            gws, port_listening=lambda p: False, now=NOW + timedelta(minutes=3 * i),
        )
        assert [a.reason for a in actions] == ["port_down"]


def test_per_gateway_independence(tmp_path):
    gws = [_cfg(tmp_path, "primary", 4001), _cfg(tmp_path, "secondary", 4002)]
    backoff = {}
    # primary down, secondary up
    actions = watchdog_tick(
        gws, port_listening=lambda p: p == 4002, now=NOW, backoff=backoff,
    )
    assert [a.gateway_name for a in actions] == ["primary"]
    assert "secondary" not in backoff
