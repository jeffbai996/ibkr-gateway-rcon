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


def _fmt_status(cfg: gc.Config) -> str:
    probe = gc.make_port_probe(cfg.port_probe)
    now = _now()
    lines = ["```", f"{'name':<12} {'port':<6} {'state':<6} {'paused until':<22} last restart"]
    for gw in cfg.gateways:
        st = gc.status_for(gw, port_listening=probe, log_path=cfg.log_file, now=now)
        state = "UP" if st.up else "DOWN"
        if st.skipped:
            paused = "indefinite" if st.skipped_until is None else st.skipped_until.isoformat(timespec="minutes")
        else:
            paused = "—"
        last = st.last_restart_at.isoformat(timespec="seconds") if st.last_restart_at else "—"
        lines.append(f"{gw.name:<12} {gw.port:<6} {state:<6} {paused:<22} {last}")
    lines.append("```")
    return "\n".join(lines)


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

    @group.command(name="status", description="Show state of every gateway.")
    async def status(interaction: discord.Interaction):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        await interaction.response.send_message(_fmt_status(cfg))

    @group.command(name="pause", description="Suppress restarts for a gateway (or all).")
    @app_commands.describe(
        name="Which gateway to pause, or 'all' for every gateway.",
        duration="How long — e.g. 30m, 2h, 1d. Leave blank for indefinite.",
    )
    @gateway_choice
    async def pause(
        interaction: discord.Interaction,
        name: app_commands.Choice[str],
        duration: Optional[str] = None,
    ):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _resolve_targets(cfg, name.value)
        if not targets:
            return await interaction.response.send_message(f"Unknown gateway `{name.value}`.", ephemeral=True)
        try:
            until = gc.parse_duration(duration, now=_now())
        except gc.DurationError as e:
            return await interaction.response.send_message(f"Bad duration: {e}", ephemeral=True)
        for gw in targets:
            gc.pause(gw, until=until)
        label = "indefinitely" if until is None else f"until `{until.isoformat(timespec='minutes')}`"
        names = ", ".join(f"`{g.name}`" for g in targets)
        await interaction.response.send_message(f"⏸️ {names} paused {label}.")

    @group.command(name="resume", description="Clear the pause on a gateway (or all).")
    @app_commands.describe(name="Which gateway to resume, or 'all' for every gateway.")
    @gateway_choice
    async def resume(interaction: discord.Interaction, name: app_commands.Choice[str]):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _resolve_targets(cfg, name.value)
        if not targets:
            return await interaction.response.send_message(f"Unknown gateway `{name.value}`.", ephemeral=True)
        for gw in targets:
            gc.resume(gw)
        names = ", ".join(f"`{g.name}`" for g in targets)
        await interaction.response.send_message(f"▶️ {names} resumed.")

    @group.command(name="restart", description="Kick a gateway now (or all), regardless of pause state.")
    @app_commands.describe(name="Which gateway to restart, or 'all' for every gateway.")
    @gateway_choice
    async def restart(interaction: discord.Interaction, name: app_commands.Choice[str]):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        targets = _resolve_targets(cfg, name.value)
        if not targets:
            return await interaction.response.send_message(f"Unknown gateway `{name.value}`.", ephemeral=True)
        await interaction.response.defer(thinking=True)

        # Run all restarts in parallel — 240s timeout per, but two gateways
        # serially would block the UI for 8 minutes worst-case. Parallel keeps it
        # bounded to the single longest restart.
        results = await asyncio.gather(
            *[asyncio.to_thread(gc.restart, gw) for gw in targets],
            return_exceptions=False,
        )

        parts: list[str] = []
        overall_ok = True
        for gw, res in zip(targets, results):
            if res.returncode == 0:
                parts.append(f"✅ `{gw.name}`: exit 0")
            else:
                overall_ok = False
                parts.append(f"⚠️ `{gw.name}`: exit {res.returncode}")
                tail = (res.stderr or res.stdout or "").strip()
                if tail:
                    parts.append(f"```{tail[-500:]}```")

        header = "restart issued for " + ", ".join(f"`{g.name}`" for g in targets)
        summary = header + "\n" + "\n".join(parts)
        # Discord's 2000-char cap.
        if len(summary) > 1900:
            summary = summary[:1900] + "…"
        await interaction.followup.send(summary)

    @group.command(name="tail", description="Show the tail of the watchdog log.")
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

    @group.command(name="health", description="Gateway process health — uptime, restarts, heartbeat.")
    async def health(interaction: discord.Interaction):
        if not _channel_ok(interaction):
            return await _reject_channel(interaction)
        now = _now()
        data = await asyncio.to_thread(
            bf.fetch_health_data,
            cfg,
            probe,
            heartbeat,
            _watchdog_interval(),
            now,
        )
        await interaction.response.send_message(bf.build_health(data, now))

    @group.command(name="brief", description="Portfolio brief: NLV, P&L, top positions, today's trades.")
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
