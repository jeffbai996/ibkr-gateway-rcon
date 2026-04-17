"""Tests for parse_duration — the helper that turns '30m', '2h', ISO timestamps
into a concrete datetime the skip-file can store."""
from datetime import datetime, timedelta, timezone

import pytest

from gateway_ctl import parse_duration, DurationError


NOW = datetime(2026, 4, 17, 20, 0, 0, tzinfo=timezone.utc)


def test_parse_duration_minutes_returns_now_plus_delta():
    got = parse_duration("30m", now=NOW)
    assert got == NOW + timedelta(minutes=30)


def test_parse_duration_hours():
    got = parse_duration("2h", now=NOW)
    assert got == NOW + timedelta(hours=2)


def test_parse_duration_days():
    got = parse_duration("1d", now=NOW)
    assert got == NOW + timedelta(days=1)


def test_parse_duration_seconds():
    got = parse_duration("45s", now=NOW)
    assert got == NOW + timedelta(seconds=45)


def test_parse_duration_accepts_iso_timestamp():
    got = parse_duration("2026-04-18T00:00:00+00:00", now=NOW)
    assert got == datetime(2026, 4, 18, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_duration_accepts_iso_z_suffix():
    got = parse_duration("2026-04-18T00:00:00Z", now=NOW)
    assert got == datetime(2026, 4, 18, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_duration_empty_returns_none_for_indefinite():
    assert parse_duration("", now=NOW) is None
    assert parse_duration(None, now=NOW) is None


def test_parse_duration_rejects_garbage():
    with pytest.raises(DurationError):
        parse_duration("forever", now=NOW)


def test_parse_duration_rejects_negative():
    with pytest.raises(DurationError):
        parse_duration("-30m", now=NOW)


def test_parse_duration_rejects_zero():
    with pytest.raises(DurationError):
        parse_duration("0m", now=NOW)


def test_parse_duration_rejects_past_timestamp():
    # Pausing "until yesterday" is meaningless.
    with pytest.raises(DurationError):
        parse_duration("2026-04-16T00:00:00Z", now=NOW)
