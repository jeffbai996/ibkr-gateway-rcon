"""Tests for heartbeat write/read — used by the deadman's switch."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway_ctl import (
    is_heartbeat_stale,
    read_heartbeat,
    write_heartbeat,
)


NOW = datetime(2026, 4, 17, 20, 0, 0, tzinfo=timezone.utc)


def test_read_heartbeat_missing_returns_none(tmp_path):
    assert read_heartbeat(tmp_path / "heartbeat") is None


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path, now=NOW)

    got = read_heartbeat(path)

    assert got == NOW


def test_write_heartbeat_creates_parent(tmp_path):
    path = tmp_path / "nested" / "heartbeat"
    write_heartbeat(path, now=NOW)

    assert path.exists()


def test_heartbeat_fresh_not_stale(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path, now=NOW - timedelta(minutes=2))

    assert is_heartbeat_stale(path, max_age=timedelta(minutes=5), now=NOW) is False


def test_heartbeat_old_is_stale(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path, now=NOW - timedelta(minutes=10))

    assert is_heartbeat_stale(path, max_age=timedelta(minutes=5), now=NOW) is True


def test_missing_heartbeat_is_stale(tmp_path):
    assert is_heartbeat_stale(tmp_path / "missing", max_age=timedelta(minutes=5), now=NOW) is True


def test_unparseable_heartbeat_is_stale(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("garbage")

    assert is_heartbeat_stale(path, max_age=timedelta(minutes=5), now=NOW) is True
