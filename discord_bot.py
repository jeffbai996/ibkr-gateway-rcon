"""Discord slash-command front end for gateway control + in-process watchdog.

Exposes:
    /gateway status
    /gateway pause    <name>  [duration]
    /gateway resume   <name>
    /gateway restart  <name>
    /gateway tail     [n]

In addition to the slash-command UI, this bot runs the watchdog logic
internally via a background task that ticks every WATCHDOG_INTERVAL_SEC
seconds (default 180). Each tick:

  1. Probes every gateway port
  2. Checks skip-files
  3. Fires restart commands for any gateway that's down and not paused
  4. Touches a heartbeat file

The heartbeat is consumed by the deadman's switch (see deadman.py) to detect a
stuck or crashed bot and restart the service.

Commands are guild-scoped to keep them out of DMs and other servers. An
optional CHANNEL_ID env var restricts where the bot will respond — if set, the
bot politely refuses in any other channel.

Config path is read from the GATEWAY_RCON_CONFIG env var (defaults to
./config.yaml relative to the bot script).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import gateway_ctl as gc
import brief as bf


log = logging.getLogger("gateway_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _env_required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"{key} is required", file=sys.stderr)
        sys.exit(2)
    return val


def _load_cfg() -> gc.Config:
    path = Path(os.environ.get("GATEWAY_RCON_CONFIG", "config.yaml"))
    return gc.load_config(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_age_relative(dt: datetime, now: datetime) -> str:
    """Human-friendly relative age: '3m ago', '2h ago', '1d ago'."""
    delta = (now - dt).total_seconds()
    if delta < 0:
        return "in the future (clock skew?)"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _fmt_until_relative(dt: datetime, now: datetime) -> str:
    """'until X' phrasing for future deadlines: 'for 25m more', 'for 1.5h more'."""
    delta = (dt - now).total_seconds()
    if delta <= 0:
        return "expiring now"
    if delta < 60:
        return f"for {int(delta)}s more"
    if delta < 3600:
        return f"for {int(delta / 60)}m more"
    if delta < 86400:
        return f"for {delta / 3600:.1f}h more"
    return f"for {delta / 86400:.1f}d more"


def _fmt_status(cfg: gc.Config, mcp_per_gw: Optional[dict] = None) -> str:
    """One block per gateway, stacked, <40 chars. Relative times for clarity.

    `state:` reflects TCP port listening (gateway process state). The new
    `mcp:` line reflects whether the MCP server can actually reach that
    gateway — port-listening alone misses the "zombie process" case where
    the gateway holds the socket but can't serve API calls.
    """
    probe = gc.make_port_probe(cfg.port_probe)
    now = _now()
    lines = ["```"]
    for i, gw in enumerate(cfg.gateways):
        st = gc.status_for(gw, port_listening=probe, log_path=cfg.log_file, now=now)
        state = "running" if st.up else "stopped"

        if st.skipped:
            if st.skipped_until is None:
                pause_line = "watchdog paused indefinitely"
            else:
                pause_line = f"watchdog paused {_fmt_until_relative(st.skipped_until, now)}"
        else:
            pause_line = "watchdog active"

        if st.last_restart_at is None:
            last_line = "no restarts on record"
        else:
            last_line = f"last restart {_fmt_age_relative(st.last_restart_at, now)}"

        # MCP reachability: the truth-teller. Port listening != service healthy.
        mcp_line = None
        if mcp_per_gw is not None:
            mcp_info = mcp_per_gw.get(gw.name)
            if mcp_info is None:
                mcp_line = "mcp: unknown"
            elif not mcp_info.get("connected"):
                mcp_line = "mcp: disconnected"
            else:
                age = mcp_info.get("last_data_age_s")
                if age is None:
                    mcp_line = "mcp: connected"
                elif age < 60:
                    mcp_line = f"mcp: connected ({int(age)}s data)"
                elif age < 600:
                    mcp_line = f"mcp: connected ({int(age/60)}m data)"
                else:
                    mcp_line = f"mcp: STALE ({_fmt_age_relative_short(age)} data)"

        if i > 0:
            lines.append("")
        lines.append(f"{gw.name} (port {gw.port})")
        lines.append(f"  state:   {state}")
        if mcp_line is not None:
            lines.append(f"  {mcp_line}")
        lines.append(f"  {pause_line}")
        lines.append(f"  {last_line}")
    lines.append("```")
    return "\n".join(lines)


def _fmt_age_relative_short(seconds: float) -> str:
    """Compact age for the STALE-data case: '5m', '2h', '1.3d'."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


ALL_SENTINEL = "__all__"


def _choices(cfg: gc.Config) -> list[app_commands.Choice[str]]:
    """Build the per-command gateway picker, including an 'all' option that
    applies the action to every configured gateway."""
    per_gateway = [app_commands.Choice(name=g.name, value=g.name) for g in cfg.gateways]
    return per_gateway + [app_commands.Choice(name="all (every gateway)", value=ALL_SENTINEL)]


def _resolve_targets(cfg: gc.Config, choice_value: str) -> list[gc.GatewayConfig]:
    """Translate a slash-command choice value into the list of gateways it
    refers to. ALL_SENTINEL means every gateway; anything else is a single
    gateway lookup."""
    if choice_value == ALL_SENTINEL:
        return list(cfg.gateways)
    gw = cfg.get(choice_value)
    return [gw] if gw else []


def _watchdog_interval() -> int:
    try:
        return int(os.environ.get("WATCHDOG_INTERVAL_SEC", "180"))
    except ValueError:
        return 180


def _heartbeat_path(cfg: gc.Config) -> Path:
    return cfg.state_dir / "bot.heartbeat"


def _log_append(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(line.rstrip() + "\n")


def _watchdog_log(cfg: gc.Config, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _log_append(cfg.log_file, f"{ts} — {message}")


def _parse_channel_ids(raw: str | None) -> set[str]:
    """DISCORD_CONTROL_CHANNEL_ID accepts a single ID or a comma-separated list."""
    if not raw:
        return set()
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def build_bot() -> discord.Client:
    cfg = _load_cfg()
    allowed_channels = _parse_channel_ids(os.environ.get("DISCORD_CONTROL_CHANNEL_ID"))
    guild_id_raw = os.environ.get("DISCORD_GUILD_ID")
    guild_obj = discord.Object(id=int(guild_id_raw)) if guild_id_raw else None

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # Port probe + heartbeat file location are computed once at boot.
    probe = gc.make_port_probe(cfg.port_probe)
    heartbeat = _heartbeat_path(cfg)

    def _channel_ok(interaction: discord.Interaction) -> bool:
        if not allowed_channels:
            return True
        return str(interaction.channel_id) in allowed_channels

    def _reject_channel(interaction: discord.Interaction):
        return interaction.response.send_message(
            "Gateway controls are restricted to the configured channel(s).",
            ephemeral=True,
        )

    group = app_commands.Group(name="gateway", description="Control IBKR gateways")

    # Build choices fresh from config so deploys pick up new gateways.
    gateway_choice = app_commands.choices(name=_choices(cfg))

    @group.command(name="status", description="Show state of every gateway")
    async def status(interaction: discord.Interaction):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        # Defer so the MCP probe doesn't race the 3s interaction timeout.
        await interaction.response.defer(thinking=True)
        # MCP probe is best-effort — if it fails, we still render the
        # process-level state, just without the mcp: line.
        try:
            mcp_per_gw, _ = await bf.fetch_mcp_status(bf.mcp_url_from_env())
        except Exception:
            mcp_per_gw = None
        await interaction.followup.send(_fmt_status(cfg, mcp_per_gw))

    def _targets_from_choice(choice: Optional[app_commands.Choice[str]]) -> list[gc.GatewayConfig]:
        """If no choice was made, default to every gateway."""
        if choice is None:
            return list(cfg.gateways)
        return _resolve_targets(cfg, choice.value)

    @group.command(name="pause", description="Suppress restarts for a gateway (defaults to all)")
    @app_commands.describe(
        name="Gateway to pause. Omit to pause every gateway.",
        duration="How long — e.g. 30m, 2h, 1d. Leave blank for indefinite.",
    )
    @gateway_choice
    async def pause(
        interaction: discord.Interaction,
        name: Optional[app_commands.Choice[str]] = None,
        duration: Optional[str] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _targets_from_choice(name)
        if not targets:
            return await interaction.response.send_message("No gateways configured.", ephemeral=True)
        try:
            until = gc.parse_duration(duration, now=_now())
        except gc.DurationError as e:
            return await interaction.response.send_message(f"Bad duration: {e}", ephemeral=True)
        for gw in targets:
            gc.pause(gw, until=until)
        label = "indefinitely" if until is None else f"until `{until.isoformat(timespec='minutes')}`"
        names = ", ".join(f"`{g.name}`" for g in targets)
        await interaction.response.send_message(f"⏸️ {names} paused {label}.")

    @group.command(name="resume", description="Clear the pause on a gateway (defaults to all)")
    @app_commands.describe(name="Gateway to resume. Omit to resume every gateway.")
    @gateway_choice
    async def resume(
        interaction: discord.Interaction,
        name: Optional[app_commands.Choice[str]] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _targets_from_choice(name)
        if not targets:
            return await interaction.response.send_message("No gateways configured.", ephemeral=True)
        for gw in targets:
            gc.resume(gw)
        names = ", ".join(f"`{g.name}`" for g in targets)
        await interaction.response.send_message(f"▶️ {names} resumed.")

    @group.command(name="restart", description="Restart gateway now, regardless of pause state")
    @app_commands.describe(name="Gateway to restart. Omit to restart every gateway.")
    @gateway_choice
    async def restart(
        interaction: discord.Interaction,
        name: Optional[app_commands.Choice[str]] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _targets_from_choice(name)
        if not targets:
            return await interaction.response.send_message("No gateways configured.", ephemeral=True)
        target_names = [g.name for g in targets]
        log.info("/gateway restart fired by %s in %s — targets=%s",
                 interaction.user, interaction.channel_id, target_names)
        await interaction.response.defer(thinking=True)

        # Clear any skip-files first — a restart is an explicit "bring this
        # back up" action, it shouldn't get swatted by a lingering pause.
        for gw in targets:
            gc.resume(gw)

        # smart_restart_async: fire the restart/start command in a detached
        # session, then poll the port for ~10s. Returns immediately after
        # success-or-timeout instead of blocking 240s on cmd.exe like the old
        # smart_restart did. For hot restarts the port doesn't come back up
        # within 10s (IBKey + JVM warmup is 2-3min), so port_up=False is
        # expected — the watchdog confirms via heartbeat. Critical: keeps the
        # Discord interaction token alive so followup.send() always lands.
        t0 = time.monotonic()
        results = await asyncio.gather(
            *[asyncio.to_thread(gc.smart_restart_async, gw, probe) for gw in targets],
            return_exceptions=False,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        for gw, res in zip(targets, results):
            log.info("smart_restart_async(%s) fired pid=%s port_up=%s "
                     "was_already_up=%s in %dms",
                     gw.name, res["pid"], res["port_up"],
                     res["was_already_up"], res["elapsed_ms"])

        parts: list[str] = []
        for gw, res in zip(targets, results):
            if res["port_up"]:
                # Cold start case — port came up within the 10s wait.
                parts.append(f"✅ `{gw.name}`: up (cold start)")
            elif res["was_already_up"]:
                # Hot restart — port was up, fired restart, port hasn't come
                # back yet (expected; takes 2-3min).
                parts.append(f"🔄 `{gw.name}`: restart fired (was running) "
                            f"— watchdog will confirm in ~2-3min")
            else:
                # Cold start, port hasn't come up in 10s. May still be
                # warming up — watchdog will catch it.
                parts.append(f"⏳ `{gw.name}`: start fired (port still down) "
                            f"— watchdog will retry if it doesn't come up")

        header = "restart issued for " + ", ".join(f"`{g.name}`" for g in targets)
        summary = header + "\n" + "\n".join(parts)
        if len(summary) > 1900:
            summary = summary[:1900] + "…"
        try:
            await interaction.followup.send(summary)
            log.info("/gateway restart followup delivered (%d chars)", len(summary))
        except discord.NotFound as e:
            # Interaction token went stale before subprocess returned — typical
            # when smart_restart blocks past Discord's 15min webhook window or
            # the user dismisses the loading bubble. The work succeeded; user
            # just won't see the result inline.
            log.error("/gateway restart followup 404 (interaction expired) "
                     "after %dms — work completed but user got no reply: %s",
                     elapsed_ms, e)
        except Exception:
            log.exception("/gateway restart followup.send failed")
            raise

    @group.command(name="stop", description="Stop gateway now")
    @app_commands.describe(name="Gateway to stop. Omit to stop every gateway.")
    @gateway_choice
    async def stop(
        interaction: discord.Interaction,
        name: Optional[app_commands.Choice[str]] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _targets_from_choice(name)
        if not targets:
            return await interaction.response.send_message("No gateways configured.", ephemeral=True)

        # Refuse if any target lacks a stop_cmd — no-op without it.
        missing = [g.name for g in targets if not g.stop_cmd]
        if missing:
            return await interaction.response.send_message(
                f"⚠️ no stop_cmd configured for: {', '.join(missing)}. "
                f"edit config.yaml and restart the bot.",
                ephemeral=True,
            )

        await interaction.response.defer(thinking=True)

        # Run stop commands FIRST, then decide whether to pause based on
        # whether stop actually succeeded. Auto-pausing before the stop runs
        # leaves phantom pauses in place when stop fails (e.g. telnet missing
        # on Windows and Stop.bat errors out).
        results = await asyncio.gather(
            *[asyncio.to_thread(gc.stop, gw) for gw in targets],
            return_exceptions=False,
        )

        parts: list[str] = []
        paused_gateways: list[str] = []
        for gw, res in zip(targets, results):
            if res is None:
                parts.append(f"⚠️ `{gw.name}`: no stop_cmd configured")
                continue
            if res.returncode == 0:
                # Successful stop → auto-pause so watchdog doesn't revive it.
                gc.pause(gw, until=None)
                paused_gateways.append(gw.name)
                parts.append(f"✅ `{gw.name}`: stopped, watchdog paused")
            else:
                err = (res.stderr or res.stdout or "").strip()
                parts.append(f"⚠️ `{gw.name}`: stop failed (exit {res.returncode}). gateway NOT paused.")
                if err:
                    parts.append(f"```{err[-400:]}```")

        header = "stop issued for " + ", ".join(f"`{g.name}`" for g in targets)
        if paused_gateways:
            header += (
                f"\nauto-paused: {', '.join(paused_gateways)} — "
                f"use /gateway restart to bring them back"
            )
        summary = header + "\n" + "\n".join(parts)
        if len(summary) > 1900:
            summary = summary[:1900] + "…"
        await interaction.followup.send(summary)

    @group.command(name="tail", description="Show the tail of the watchdog log")
    @app_commands.describe(n="Number of lines (default 20, max 100).")
    async def tail(interaction: discord.Interaction, n: Optional[int] = 20):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        n = max(1, min(n or 20, 100))
        lines = gc.tail_log(cfg.log_file, n=n)
        if not lines:
            return await interaction.response.send_message("Log is empty (or missing).")
        # Discord's 2000-char cap — truncate from the top if we overflow.
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[-1900:]
        await interaction.response.send_message(f"```\n{text}\n```")

    @group.command(name="health", description="Gateway process health — uptime, restarts, heartbeat")
    async def health(interaction: discord.Interaction):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.defer(thinking=True)
        now = _now()
        # MCP probe is best-effort — if it fails we still render the
        # gateway-process section, just without the mcp: line.
        try:
            mcp_per_gw, acct_errs = await bf.fetch_mcp_status(bf.mcp_url_from_env())
        except Exception:
            mcp_per_gw, acct_errs = {}, []
        data = await asyncio.to_thread(
            bf.fetch_health_data,
            cfg,
            probe,
            heartbeat,
            _watchdog_interval(),
            now,
            mcp_per_gw,
            acct_errs,
        )
        await interaction.followup.send(bf.build_health(data, now))

    @group.command(name="brief", description="Portfolio brief: NLV, P&L, top positions, today's trades")
    async def brief_cmd(interaction: discord.Interaction):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.defer(thinking=True)
        data = await bf.fetch_brief_data(bf.mcp_url_from_env())
        out = bf.build_brief(data)
        # Discord 2000-char cap.
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    @group.command(name="pnl", description="Per-account P&L breakdown — daily, unrealized, realized")
    @app_commands.describe(account="Account ID (e.g. U12345678). Omit for all.")
    async def pnl_cmd(
        interaction: discord.Interaction,
        account: Optional[str] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.defer(thinking=True)
        view = await bf.fetch_account_view(
            bf.mcp_url_from_env(),
            want_positions=False,
            want_pnl=True,
            want_trades=False,
        )
        out = bf.build_pnl(view, account=account)
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    @group.command(name="positions", description="Top positions with cost basis, mv, unrealized P&L")
    @app_commands.describe(
        account="Account ID to filter. Omit for combined.",
        top="How many rows (default 10, max 25).",
    )
    async def positions_cmd(
        interaction: discord.Interaction,
        account: Optional[str] = None,
        top: Optional[int] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.defer(thinking=True)
        n = max(1, min(int(top or 10), 25))
        view = await bf.fetch_account_view(
            bf.mcp_url_from_env(),
            want_positions=True,
            want_pnl=False,
            want_trades=False,
        )
        out = bf.build_positions(view, account=account, top_n=n)
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    @group.command(name="trades", description="Today's executions by account")
    @app_commands.describe(account="Account ID to filter. Omit for all.")
    async def trades_cmd(
        interaction: discord.Interaction,
        account: Optional[str] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.defer(thinking=True)
        view = await bf.fetch_account_view(
            bf.mcp_url_from_env(),
            want_positions=False,
            want_pnl=False,
            want_trades=True,
        )
        out = bf.build_trades(view, account=account)
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    @group.command(name="margin", description="Margin close-up: cushion, excess liq, bp, leverage, util")
    @app_commands.describe(account="Account ID to filter. Omit for all.")
    async def margin_cmd(
        interaction: discord.Interaction,
        account: Optional[str] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.defer(thinking=True)
        view = await bf.fetch_account_view(
            bf.mcp_url_from_env(),
            want_positions=False,
            want_pnl=False,
            want_trades=False,
        )
        out = bf.build_margin(view, account=account)
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    # Register globally; on_ready will copy to the guild for instant availability.
    tree.add_command(group)

    # --- background watchdog loop -----------------------------------------

    @tasks.loop(seconds=_watchdog_interval())
    async def watchdog():
        try:
            now = _now()
            # Port probes can block on subprocess — push to a thread so the
            # event loop keeps spinning.
            actions = await asyncio.to_thread(
                gc.watchdog_tick,
                cfg.gateways,
                probe,
                now,
            )
            for action in actions:
                gw = cfg.get(action.gateway_name)
                if gw is None:
                    continue
                _watchdog_log(cfg, f"port probe failed for {gw.name}, restarting {gw.name} gateway")
                res = await asyncio.to_thread(gc.restart, gw)
                _watchdog_log(
                    cfg,
                    f"{gw.name} restart command issued (exit {res.returncode})",
                )
            # Heartbeat AFTER the work is done — a stuck tick won't refresh it.
            await asyncio.to_thread(gc.write_heartbeat, heartbeat, now)
        except Exception as e:  # don't let a bad tick kill the loop
            log.exception("watchdog tick raised: %s", e)

    @watchdog.before_loop
    async def watchdog_ready():
        await client.wait_until_ready()
        log.info("watchdog loop starting (interval=%ss)", _watchdog_interval())

    @client.event
    async def on_ready():
        log.info("logged in as %s (id=%s)", client.user, client.user.id)

        # If guild wasn't provided via env, auto-discover from one of the allowed
        # control channels. Guild-scoped sync is instant; global sync takes ~1h
        # on first push, so discovery is the faster path.
        nonlocal_guild = guild_obj
        if nonlocal_guild is None and allowed_channels:
            for cid in allowed_channels:
                ch = client.get_channel(int(cid))
                if ch is not None and getattr(ch, "guild", None) is not None:
                    nonlocal_guild = discord.Object(id=ch.guild.id)
                    log.info("auto-discovered guild id %s from channel %s", ch.guild.id, cid)
                    break

        if nonlocal_guild is not None:
            # Copy the global command tree into this guild for instant availability,
            # then sync. Without this, global commands take ~1h to propagate.
            tree.copy_global_to(guild=nonlocal_guild)
            await tree.sync(guild=nonlocal_guild)
            log.info("slash commands synced to guild %s", nonlocal_guild.id)

            # Clear the global command list so users don't see duplicates
            # (one guild-scoped entry + one global entry). Safe because every
            # command we register is also copied into the guild above.
            tree.clear_commands(guild=None)
            await tree.sync()
            log.info("cleared global command list (using guild-scoped only)")
        else:
            await tree.sync()
            log.info("slash commands synced globally (may take up to 1h)")

        if not watchdog.is_running():
            watchdog.start()

    return client


def main() -> int:
    load_dotenv()
    token = _env_required("DISCORD_BOT_TOKEN")
    client = build_bot()
    client.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
