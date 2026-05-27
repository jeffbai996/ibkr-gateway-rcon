"""Tests for _parse_channel_ids — the DISCORD_CONTROL_CHANNEL_ID parser.

This is the authorization gate for the bot: only IDs in the parsed set may
run gateway commands. The function accepts a single ID or a comma-separated
list, trims whitespace, and drops blanks. Generic fake channel IDs only.
"""
from discord_bot import _parse_channel_ids


def test_none_returns_empty_set():
    assert _parse_channel_ids(None) == set()


def test_empty_string_returns_empty_set():
    assert _parse_channel_ids("") == set()


def test_single_id():
    assert _parse_channel_ids("123456789") == {"123456789"}


def test_comma_separated_list():
    assert _parse_channel_ids("111,222,333") == {"111", "222", "333"}


def test_trims_whitespace_around_ids():
    assert _parse_channel_ids(" 111 , 222 ,333 ") == {"111", "222", "333"}


def test_drops_empty_tokens():
    # Trailing comma / double comma shouldn't introduce empty entries.
    assert _parse_channel_ids("111,,222,") == {"111", "222"}


def test_only_commas_returns_empty():
    assert _parse_channel_ids(",,,") == set()


def test_duplicate_ids_deduped():
    assert _parse_channel_ids("111,111,222") == {"111", "222"}
