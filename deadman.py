"""Deadman's switch for the gateway-rcon Discord bot.

Invoked every N minutes by a systemd timer (or cron). Checks whether the
bot's heartbeat file has been touched recently enough; if not, asks systemd
to restart the bot service.

Exit codes:
  0 — bot is healthy, heartbeat fresh
  1 — bot is stuck; restart was issued
  2 — config or env error

This is intentionally tiny and has no dependency on discord.py — if the bot's
whole Python environment is wedged, the deadman still works.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gateway_ctl as gc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="deadman",
        description="Check gateway-rcon bot heartbeat; restart service if stale.",
    )
    ap.add_argument("config", help="path to config.yaml")
    ap.add_argument(
        "--max-age-sec",
        type=int,
        default=int(os.environ.get("DEADMAN_MAX_AGE_SEC", "600")),
        help="Heartbeat older than this is considered stale (default 600s).",
    )
    ap.add_argument(
        "--service",
        default=os.environ.get("DEADMAN_SERVICE", "ibkr-gateway-rcon-bot"),
        help="systemd --user unit to restart when stale.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report status but don't actually restart.",
    )
    args = ap.parse_args(argv)

    try:
        cfg = gc.load_config(args.config)
    except gc.ConfigError as e:
        print(f"deadman: config error: {e}", file=sys.stderr)
        return 2

    heartbeat = cfg.state_dir / "bot.heartbeat"
    now = _now()

    if not gc.is_heartbeat_stale(heartbeat, max_age=timedelta(seconds=args.max_age_sec), now=now):
        hb = gc.read_heartbeat(heartbeat)
        print(f"ok: heartbeat age {(now - hb).total_seconds():.0f}s")
        return 0

    print(f"STALE: heartbeat age exceeds {args.max_age_sec}s — restarting {args.service}", file=sys.stderr)
    if args.dry_run:
        return 1

    result = subprocess.run(
        ["systemctl", "--user", "restart", args.service],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"deadman: systemctl restart failed (exit {result.returncode}): {result.stderr}", file=sys.stderr)
        return 2
    print(f"deadman: {args.service} restart issued")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
