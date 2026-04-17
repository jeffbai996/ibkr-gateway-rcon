"""Backend for the gateway control system.

Pure functions where possible. Filesystem touches are isolated so the Discord
and Flask layers can share one source of truth: skip-files, the watchdog log,
and a port-probe callback.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Config file is missing, malformed, or semantically invalid."""


class DurationError(Exception):
    """Unparseable or meaningless duration string."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayConfig:
    name: str
    port: int
    restart_cmd: str
    skip_file: Path


@dataclass(frozen=True)
class Config:
    state_dir: Path
    log_file: Path
    port_probe: str
    gateways: list[GatewayConfig]

    def get(self, name: str) -> Optional[GatewayConfig]:
        for gw in self.gateways:
            if gw.name == name:
                return gw
        return None


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    base = path.parent
    state_dir = (base / raw.get("state_dir", "state")).resolve()
    log_file = (base / raw.get("log_file", "watchdog.log")).resolve()
    port_probe = raw.get("port_probe", "netstat")

    gws_raw = raw.get("gateways")
    if not gws_raw:
        raise ConfigError("config must define at least one gateway")

    seen: set[str] = set()
    gateways: list[GatewayConfig] = []
    for entry in gws_raw:
        name = entry["name"]
        if name in seen:
            raise ConfigError(f"duplicate gateway name: {name}")
        seen.add(name)

        skip_file = (state_dir / entry["skip_file"]).resolve()
        gateways.append(
            GatewayConfig(
                name=name,
                port=int(entry["port"]),
                restart_cmd=entry["restart_cmd"],
                skip_file=skip_file,
            )
        )

    return Config(
        state_dir=state_dir,
        log_file=log_file,
        port_probe=port_probe,
        gateways=gateways,
    )


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def parse_duration(raw: Optional[str], now: datetime) -> Optional[datetime]:
    """Turn a duration string into an absolute datetime.

    Accepts "30m" / "2h" / "45s" / "1d" or an ISO-8601 timestamp. Empty/None
    means "indefinite" (no deadline — caller stores None).
    """
    if raw is None or raw == "":
        return None

    text = raw.strip()

    m = _DURATION_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            raise DurationError(f"duration must be positive: {raw}")
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return now + delta

    # Try ISO-8601. Python 3.11+ accepts 'Z'; fall back for older versions.
    try:
        if text.endswith("Z"):
            text_iso = text[:-1] + "+00:00"
        else:
            text_iso = text
        dt = datetime.fromisoformat(text_iso)
    except ValueError as e:
        raise DurationError(f"cannot parse duration: {raw!r}") from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    if dt <= now:
        raise DurationError(f"duration resolves to the past: {raw}")

    return dt


# ---------------------------------------------------------------------------
# Skip files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkipState:
    """Contents of a skip-file.

    `until=None` means indefinite (paused until a human removes it).
    `until=<datetime>` means auto-resume at that instant.
    """

    until: Optional[datetime]


def read_skip(path: Path) -> Optional[SkipState]:
    path = Path(path)
    if not path.exists():
        return None

    text = path.read_text().strip()
    if not text:
        return SkipState(until=None)

    try:
        if text.endswith("Z"):
            text_iso = text[:-1] + "+00:00"
        else:
            text_iso = text
        dt = datetime.fromisoformat(text_iso)
    except ValueError:
        log.warning("skip file %s has unparseable contents, treating as indefinite", path)
        return SkipState(until=None)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return SkipState(until=dt)


def write_skip(path: Path, until: Optional[datetime]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if until is None:
        path.write_text("")
    else:
        # Always normalize to UTC ISO-8601 for consistency with bash readers.
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        path.write_text(until.astimezone(timezone.utc).isoformat())


def clear_skip(path: Path) -> None:
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_skipped(path: Path, now: datetime) -> bool:
    """True iff the skip-file says we should suppress a restart right now.

    Side effect: if the file exists with an expired deadline, this removes it.
    That way stale skip-files don't linger and watchdog runs stay fast.
    """
    state = read_skip(path)
    if state is None:
        return False
    if state.until is None:
        return True
    if state.until > now:
        return True
    # Expired — garbage collect.
    clear_skip(path)
    return False


# ---------------------------------------------------------------------------
# Status aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayStatus:
    name: str
    port: int
    up: bool
    skipped: bool
    skipped_until: Optional[datetime]
    last_restart_at: Optional[datetime]


_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — (?P<name>\w+) restart command issued"
)


def _parse_last_restart(log_path: Path, gateway_name: str) -> Optional[datetime]:
    if not log_path.exists():
        return None

    latest: Optional[datetime] = None
    for line in log_path.read_text().splitlines():
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        if m.group("name") != gateway_name:
            continue
        try:
            dt = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if latest is None or dt > latest:
            latest = dt

    return latest


def status_for(
    cfg: GatewayConfig,
    port_listening: Callable[[int], bool],
    log_path: Path,
    now: datetime,
) -> GatewayStatus:
    up = bool(port_listening(cfg.port))
    skip_state = read_skip(cfg.skip_file)
    skipped = False
    skipped_until: Optional[datetime] = None
    if skip_state is not None:
        if skip_state.until is None:
            skipped = True
        elif skip_state.until > now:
            skipped = True
            skipped_until = skip_state.until
        else:
            # Expired skip — clean up so status reflects reality.
            clear_skip(cfg.skip_file)

    return GatewayStatus(
        name=cfg.name,
        port=cfg.port,
        up=up,
        skipped=skipped,
        skipped_until=skipped_until,
        last_restart_at=_parse_last_restart(log_path, cfg.name),
    )


# ---------------------------------------------------------------------------
# Port probes
# ---------------------------------------------------------------------------


def make_port_probe(kind: str) -> Callable[[int], bool]:
    """Return a callable that returns True if `port` is LISTENING locally.

    Kinds:
      - netstat: Linux `ss -tln` (preferred) or `netstat -tln`.
      - wsl-cmd-netstat: Windows netstat via cmd.exe — for WSL installs where
        gateways run on the Windows side.
    """
    if kind == "ss":
        return _ss_probe
    if kind == "netstat":
        return _linux_netstat_probe
    if kind == "wsl-cmd-netstat":
        return _wsl_cmd_netstat_probe
    raise ConfigError(f"unknown port_probe: {kind}")


def _ss_probe(port: int) -> bool:
    out = _run_cmd(["ss", "-tln"])
    return _contains_listen(out, port)


def _linux_netstat_probe(port: int) -> bool:
    # Some distros ship `ss` but not `netstat`. Fall back silently.
    out = _run_cmd(["netstat", "-tln"])
    if not out:
        out = _run_cmd(["ss", "-tln"])
    return _contains_listen(out, port)


def _wsl_cmd_netstat_probe(port: int) -> bool:
    out = _run_cmd(["/mnt/c/Windows/system32/cmd.exe", "/c", "netstat", "-ano"])
    return _contains_listen(out, port, windows_style=True)


def _run_cmd(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("port probe %s failed: %s", cmd, e)
        return ""
    return res.stdout


def _contains_listen(netstat_output: str, port: int, windows_style: bool = False) -> bool:
    token_listen = ":" + str(port)
    for line in netstat_output.splitlines():
        if token_listen not in line:
            continue
        if windows_style:
            if "LISTENING" in line:
                return True
        else:
            if "LISTEN" in line:
                return True
    return False


# ---------------------------------------------------------------------------
# Operations (pause / resume / restart / tail)
# ---------------------------------------------------------------------------


def pause(gw: GatewayConfig, until: Optional[datetime]) -> None:
    """Write a skip-file so the watchdog stops restarting this gateway."""
    write_skip(gw.skip_file, until=until)


def resume(gw: GatewayConfig) -> None:
    """Remove the skip-file — watchdog resumes on next tick."""
    clear_skip(gw.skip_file)


def restart(gw: GatewayConfig) -> subprocess.CompletedProcess:
    """Fire the configured restart command. Returns CompletedProcess for callers
    that want exit code / stdout. Does NOT raise on non-zero exit."""
    return subprocess.run(
        gw.restart_cmd, shell=True, capture_output=True, text=True, timeout=60
    )


def tail_log(log_path: Path, n: int = 20) -> list[str]:
    if not log_path.exists():
        return []
    lines = log_path.read_text().splitlines()
    return lines[-n:]
