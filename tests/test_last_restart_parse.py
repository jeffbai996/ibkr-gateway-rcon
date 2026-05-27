"""Tests for _parse_last_restart — extracting the newest restart timestamp
for a given gateway from the watchdog log.

The log-line regex is the load-bearing bit: it must match the exact
"<ts> — <name> restart command issued" shape, filter by gateway name, and
pick the latest timestamp across multiple entries.
"""
from datetime import datetime, timezone

import gateway_ctl as gc


def _write(tmp_path, *lines):
    p = tmp_path / "watchdog.log"
    p.write_text("\n".join(lines) + ("\n" if lines else ""))
    return p


def test_missing_log_returns_none(tmp_path):
    assert gc._parse_last_restart(tmp_path / "nope.log", "primary") is None


def test_single_matching_line(tmp_path):
    p = _write(tmp_path, "2026-05-20 10:30:00 — primary restart command issued")
    got = gc._parse_last_restart(p, "primary")
    assert got == datetime(2026, 5, 20, 10, 30, 0, tzinfo=timezone.utc)


def test_picks_latest_of_multiple(tmp_path):
    p = _write(
        tmp_path,
        "2026-05-20 10:00:00 — primary restart command issued",
        "2026-05-20 14:45:00 — primary restart command issued",
        "2026-05-20 12:00:00 — primary restart command issued",
    )
    got = gc._parse_last_restart(p, "primary")
    # Latest by timestamp, not by file order.
    assert got == datetime(2026, 5, 20, 14, 45, 0, tzinfo=timezone.utc)


def test_filters_by_gateway_name(tmp_path):
    p = _write(
        tmp_path,
        "2026-05-20 10:00:00 — secondary restart command issued",
        "2026-05-20 11:00:00 — primary restart command issued",
    )
    got = gc._parse_last_restart(p, "primary")
    assert got == datetime(2026, 5, 20, 11, 0, 0, tzinfo=timezone.utc)


def test_no_matching_gateway_returns_none(tmp_path):
    p = _write(tmp_path, "2026-05-20 10:00:00 — secondary restart command issued")
    assert gc._parse_last_restart(p, "primary") is None


def test_ignores_unrelated_lines(tmp_path):
    p = _write(
        tmp_path,
        "2026-05-20 09:00:00 — primary heartbeat ok",       # not a restart line
        "garbage line with no structure",
        "2026-05-20 10:00:00 — primary restart command issued",
    )
    got = gc._parse_last_restart(p, "primary")
    assert got == datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)


def test_empty_log_returns_none(tmp_path):
    p = tmp_path / "watchdog.log"
    p.write_text("")
    assert gc._parse_last_restart(p, "primary") is None
