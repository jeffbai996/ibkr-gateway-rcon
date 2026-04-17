"""Tests for skip-file read/write — the filesystem state the watchdog reads
to decide whether to skip a gateway on its next tick."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway_ctl import (
    SkipState,
    read_skip,
    write_skip,
    clear_skip,
    is_skipped,
)


NOW = datetime(2026, 4, 17, 20, 0, 0, tzinfo=timezone.utc)


def test_read_skip_missing_file_returns_none(tmp_path):
    assert read_skip(tmp_path / "missing.skip") is None


def test_write_then_read_indefinite(tmp_path):
    path = tmp_path / "gw.skip"
    write_skip(path, until=None)

    got = read_skip(path)

    assert got == SkipState(until=None)


def test_write_then_read_with_deadline(tmp_path):
    path = tmp_path / "gw.skip"
    deadline = NOW + timedelta(minutes=30)
    write_skip(path, until=deadline)

    got = read_skip(path)

    assert got == SkipState(until=deadline)


def test_clear_skip_removes_file(tmp_path):
    path = tmp_path / "gw.skip"
    write_skip(path, until=None)
    assert path.exists()

    clear_skip(path)

    assert not path.exists()


def test_clear_skip_idempotent_on_missing(tmp_path):
    # Clearing a non-existent skip should not raise.
    clear_skip(tmp_path / "gw.skip")


def test_is_skipped_missing_file_is_false(tmp_path):
    assert is_skipped(tmp_path / "gw.skip", now=NOW) is False


def test_is_skipped_indefinite_is_true(tmp_path):
    path = tmp_path / "gw.skip"
    write_skip(path, until=None)

    assert is_skipped(path, now=NOW) is True


def test_is_skipped_future_deadline_is_true(tmp_path):
    path = tmp_path / "gw.skip"
    write_skip(path, until=NOW + timedelta(minutes=5))

    assert is_skipped(path, now=NOW) is True


def test_is_skipped_past_deadline_is_false(tmp_path):
    path = tmp_path / "gw.skip"
    write_skip(path, until=NOW - timedelta(minutes=5))

    assert is_skipped(path, now=NOW) is False


def test_is_skipped_past_deadline_auto_cleans(tmp_path):
    # Once a deadline has passed, reading should GC the file so the
    # watchdog doesn't have to re-parse it every 3 minutes.
    path = tmp_path / "gw.skip"
    write_skip(path, until=NOW - timedelta(minutes=5))
    assert path.exists()

    is_skipped(path, now=NOW)

    assert not path.exists()


def test_write_skip_creates_parent_dir(tmp_path):
    # If state/ doesn't exist yet, we should make it.
    path = tmp_path / "state" / "gw.skip"
    write_skip(path, until=None)

    assert path.exists()
