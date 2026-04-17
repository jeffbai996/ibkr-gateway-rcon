"""Small CLI surface over gateway_ctl, used by watchdog.sh and humans.

Subcommands:
  list-names <config.yaml>                 → whitespace-separated names
  status-one <config.yaml> <gateway>       → "up|down active|skipped"
  status-all <config.yaml>                 → human-readable table
  pause <config.yaml> <gateway> [duration] → touch skip-file
  resume <config.yaml> <gateway>           → remove skip-file
  restart-one <config.yaml> <gateway>      → fire restart_cmd synchronously
  tail-log <config.yaml> [n]               → dump last N log lines
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import gateway_ctl as gc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(cfg_path: str) -> gc.Config:
    return gc.load_config(cfg_path)


def cmd_list_names(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    print(" ".join(g.name for g in cfg.gateways))
    return 0


def cmd_status_one(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    gw = cfg.get(args.gateway)
    if gw is None:
        print(f"unknown gateway: {args.gateway}", file=sys.stderr)
        return 2
    probe = gc.make_port_probe(cfg.port_probe)
    st = gc.status_for(gw, port_listening=probe, log_path=cfg.log_file, now=_now())
    up_token = "up" if st.up else "down"
    skip_token = "skipped" if st.skipped else "active"
    print(f"{up_token} {skip_token}")
    return 0


def cmd_status_all(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    probe = gc.make_port_probe(cfg.port_probe)
    now = _now()

    rows = []
    for gw in cfg.gateways:
        st = gc.status_for(gw, port_listening=probe, log_path=cfg.log_file, now=now)
        skip = "—"
        if st.skipped:
            skip = "indefinite" if st.skipped_until is None else st.skipped_until.isoformat(timespec="minutes")
        last = st.last_restart_at.isoformat(timespec="seconds") if st.last_restart_at else "—"
        rows.append((gw.name, str(gw.port), "UP" if st.up else "DOWN", skip, last))

    header = ("name", "port", "state", "paused until", "last restart")
    widths = [max(len(header[i]), max(len(r[i]) for r in rows)) for i in range(5)]

    def fmt(r):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(r))

    print(fmt(header))
    print(fmt(tuple("-" * w for w in widths)))
    for r in rows:
        print(fmt(r))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    gw = cfg.get(args.gateway)
    if gw is None:
        print(f"unknown gateway: {args.gateway}", file=sys.stderr)
        return 2
    try:
        until = gc.parse_duration(args.duration, now=_now())
    except gc.DurationError as e:
        print(f"bad duration: {e}", file=sys.stderr)
        return 2
    gc.pause(gw, until=until)
    label = "indefinite" if until is None else f"until {until.isoformat(timespec='minutes')}"
    print(f"paused {gw.name} ({label})")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    gw = cfg.get(args.gateway)
    if gw is None:
        print(f"unknown gateway: {args.gateway}", file=sys.stderr)
        return 2
    gc.resume(gw)
    print(f"resumed {gw.name}")
    return 0


def cmd_restart_one(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    gw = cfg.get(args.gateway)
    if gw is None:
        print(f"unknown gateway: {args.gateway}", file=sys.stderr)
        return 2
    res = gc.restart(gw)
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    return res.returncode


def cmd_tail(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    for line in gc.tail_log(cfg.log_file, n=args.n):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gwctl", description="gateway control")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-names")
    s.add_argument("config")
    s.set_defaults(func=cmd_list_names)

    s = sub.add_parser("status-one")
    s.add_argument("config")
    s.add_argument("gateway")
    s.set_defaults(func=cmd_status_one)

    s = sub.add_parser("status-all")
    s.add_argument("config")
    s.set_defaults(func=cmd_status_all)

    s = sub.add_parser("pause")
    s.add_argument("config")
    s.add_argument("gateway")
    s.add_argument("duration", nargs="?", default="")
    s.set_defaults(func=cmd_pause)

    s = sub.add_parser("resume")
    s.add_argument("config")
    s.add_argument("gateway")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("restart-one")
    s.add_argument("config")
    s.add_argument("gateway")
    s.set_defaults(func=cmd_restart_one)

    s = sub.add_parser("tail-log")
    s.add_argument("config")
    s.add_argument("n", nargs="?", type=int, default=20)
    s.set_defaults(func=cmd_tail)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
