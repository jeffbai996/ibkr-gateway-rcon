"""Discord slash-command front end for gateway control + in-process watchdog.

Exposes:
    /gateway status
    /gateway pause    <name>  [duration]
    /gateway resume   <name>
    /gateway restart  <name>
    /gateway tail     [n]
    /gateway quote    <symbols>

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
import report as rp


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

    async def _guard_channel(interaction: discord.Interaction) -> bool:
        """Channel allowlist check for every command. True if allowed; sends
        the rejection message and returns False otherwise. Call site:
        `if not await _guard_channel(interaction): return`"""
        if _channel_ok(interaction):
            return True
        await _reject_channel(interaction)
        return False

    group = app_commands.Group(name="gateway", description="Control IBKR gateways")

    # Build choices fresh from config so deploys pick up new gateways.
    gateway_choice = app_commands.choices(name=_choices(cfg))

    @group.command(name="status", description="Gateway state — process, uptime, restarts, heartbeat, MCP")
    async def status(interaction: discord.Interaction):
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
        targets = _targets_from_choice(name)
        if not targets:
            return await interaction.response.send_message("No gateways configured.", ephemeral=True)
        target_names = [g.name for g in targets]
        log.info("/gateway restart fired by %s in %s — targets=%s",
                 interaction.user, interaction.channel_id, target_names)
        await interaction.response.defer(thinking=True)

        # Clear any skip-files first — a restart is an explicit "bring this
        # back up" action, it shouldn't get swatted by a lingering pause.
        # Also clear watchdog backoff so a stood-down watchdog resumes duty.
        for gw in targets:
            gc.resume(gw)
            gc.reset_backoff(watchdog_backoff, gw.name)

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
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
        n = max(1, min(n or 20, 100))
        lines = gc.tail_log(cfg.log_file, n=n)
        if not lines:
            return await interaction.response.send_message("Log is empty (or missing).")
        # Discord's 2000-char cap — truncate from the top if we overflow.
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[-1900:]
        await interaction.response.send_message(f"```\n{text}\n```")

    @group.command(name="brief", description="Portfolio brief: NLV, P&L, top positions, today's trades")
    async def brief_cmd(interaction: discord.Interaction):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bf.fetch_brief_data(bf.mcp_url_from_env())
        out = bf.build_brief(data)
        # Discord 2000-char cap.
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    @group.command(name="report", description="Detailed portfolio report: full numbers, margin, positions, concentration, stress")
    @app_commands.describe(account="Which account — primary, secondary, or both (default both)")
    @app_commands.choices(account=[
        app_commands.Choice(name="both (default)", value="both"),
        app_commands.Choice(name="primary", value="primary"),
        app_commands.Choice(name="secondary", value="secondary"),
    ])
    async def report_cmd(
        interaction: discord.Interaction,
        account: Optional[app_commands.Choice[str]] = None,
    ):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        which = account.value if account else "both"
        data = await rp.fetch_report_data(bf.mcp_url_from_env())
        # One message per account card (each fits under Discord's 2000-char
        # cap); first via the interaction followup, the rest to the channel.
        messages = rp.build_report_messages(data, which=which)
        await interaction.followup.send(messages[0])
        for extra in messages[1:]:
            await interaction.channel.send(extra)

    @group.command(name="pnl", description="Per-account P&L breakdown — daily, unrealized, realized")
    @app_commands.describe(account="Account ID (e.g. U12345678). Omit for all.")
    async def pnl_cmd(
        interaction: discord.Interaction,
        account: Optional[str] = None,
    ):
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
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
        if not await _guard_channel(interaction):
            return
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

    @group.command(name="quote", description="Live quotes for symbols (e.g. mu avgo nvda goog)")
    @app_commands.describe(symbols="Space- or comma-separated tickers, max 10")
    async def quote_cmd(
        interaction: discord.Interaction,
        symbols: str,
    ):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)

        syms = [s.strip().upper() for s in symbols.replace(",", " ").split() if s.strip()]
        if not syms:
            return await interaction.followup.send(
                "usage: `/gateway quote mu avgo nvda goog`"
            )
        if len(syms) > 10:
            return await interaction.followup.send("max 10 symbols.")

        prices, errors = await bf.fetch_quotes(syms, bf.mcp_url_from_env())
        out = bf.build_quotes(syms, prices, errors)
        if len(out) > 1950:
            out = out[:1950] + "…"
        await interaction.followup.send(out)

    @group.command(name="ta", description="Technicals for one symbol (SMA/RSI/52w/vol)")
    @app_commands.describe(symbol="Ticker, e.g. nvda")
    async def ta_cmd(interaction: discord.Interaction, symbol: str):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        sym = symbol.strip().upper()
        if not sym:
            return await interaction.followup.send("usage: `/gateway ta nvda`")
        md = await rp.fetch_technicals(sym, bf.mcp_url_from_env())
        out = rp.build_technicals(sym, md)
        await interaction.followup.send(out)

    @group.command(name="whatif", description="Margin/equity impact of a hypothetical trade")
    @app_commands.describe(
        action="buy or sell",
        symbol="Ticker, e.g. nvda",
        quantity="Number of shares",
        account="Account to simulate against",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="buy", value="buy"),
        app_commands.Choice(name="sell", value="sell"),
    ])
    @app_commands.choices(account=[
        app_commands.Choice(name="primary (default)", value="primary"),
        app_commands.Choice(name="secondary", value="secondary"),
    ])
    async def whatif_cmd(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        symbol: str,
        quantity: int,
        account: Optional[app_commands.Choice[str]] = None,
    ):
        if not await _guard_channel(interaction):
            return
        if quantity <= 0:
            return await interaction.response.send_message(
                "⚠️ quantity must be positive", ephemeral=True,
            )
        await interaction.response.defer(thinking=True)
        sym = symbol.strip().upper()
        if not sym:
            return await interaction.followup.send(
                "usage: `/gateway whatif action:buy symbol:nvda quantity:100`"
            )
        # Resolve account: "primary"/"secondary" labels go to MCP which has its
        # own resolve_account mapping. None or "primary" = default (omit param).
        account_value = account.value if account is not None else None
        try:
            md = await bf.fetch_what_if(
                action.value,
                sym,
                quantity,
                account_value,
                mcp_url=bf.mcp_url_from_env(),
            )
        except Exception as e:
            log.exception("whatif fetch failed: %s", e)
            return await interaction.followup.send(f"⚠️ what-if failed: {e}")
        out = bf.format_whatif_for_discord(md)
        await interaction.followup.send(out)

    # --- Tier-1 risk / intelligence commands (ported to richer MCP tools) ---

    @group.command(name="drawdown", description="NAV vs peak: drawdown %, recovery needed")
    @app_commands.describe(account="Account ID. Omit for primary.")
    async def drawdown_cmd(interaction: discord.Interaction, account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        q = f"?account={account}" if account else ""
        md = await bf.fetch_markdown_endpoint(f"/api/drawdown{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "drawdown"))

    @group.command(name="var", description="1-day Value-at-Risk (95%/99%) + component breakdown")
    @app_commands.describe(account="Account ID. Omit for primary.")
    async def var_cmd(interaction: discord.Interaction, account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        q = f"?account={account}" if account else ""
        md = await bf.fetch_markdown_endpoint(f"/api/var{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "VaR"))

    @group.command(name="correlation", description="Pairwise correlation matrix — hidden concentration")
    @app_commands.describe(account="Account ID. Omit for primary.")
    async def correlation_cmd(interaction: discord.Interaction, account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        q = f"?account={account}" if account else ""
        md = await bf.fetch_markdown_endpoint(f"/api/correlation{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "correlation"))

    @group.command(name="sector", description="Sector exposure: weights + HHI concentration")
    @app_commands.describe(account="Account ID. Omit for primary.")
    async def sector_cmd(interaction: discord.Interaction, account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        q = f"?account={account}" if account else ""
        md = await bf.fetch_markdown_endpoint(f"/api/sector{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "sector"))

    @group.command(name="beta", description="Portfolio beta vs benchmark (default SPY)")
    @app_commands.describe(benchmark="Benchmark symbol (default SPY)", account="Account ID.")
    async def beta_cmd(interaction: discord.Interaction, benchmark: Optional[str] = None,
                       account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        parts = []
        if benchmark:
            parts.append(f"benchmark={benchmark.strip().upper()}")
        if account:
            parts.append(f"account={account}")
        q = ("?" + "&".join(parts)) if parts else ""
        md = await bf.fetch_markdown_endpoint(f"/api/beta{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "beta"))

    @group.command(name="geopolitical", description="Geopolitical risk mapped to held positions")
    @app_commands.describe(account="Account ID. Omit for primary.")
    async def geopolitical_cmd(interaction: discord.Interaction, account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        q = f"?account={account}" if account else ""
        md = await bf.fetch_markdown_endpoint(f"/api/geopolitical{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "geopolitical"))

    @group.command(name="thesis", description="Check a news item against your thesis pillars")
    @app_commands.describe(news="Headline / development to validate")
    async def thesis_cmd(interaction: discord.Interaction, news: str):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        from urllib.parse import quote_plus
        md = await bf.fetch_markdown_endpoint(
            f"/api/thesis?news_item={quote_plus(news)}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "thesis check"))

    @group.command(name="rebalance", description="Rebalance plan: current vs target, trades to hit it")
    @app_commands.describe(targets="Targets as SYM:PCT pairs, e.g. MU:25,NVDA:30,SGOV:20",
                           account="Account ID. Omit for primary.")
    async def rebalance_cmd(interaction: discord.Interaction, targets: str,
                            account: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        from urllib.parse import quote_plus
        q = f"?targets={quote_plus(targets)}"
        if account:
            q += f"&account={account}"
        md = await bf.fetch_markdown_endpoint(f"/api/rebalance{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "rebalance"))

    @group.command(name="compare", description="Relative performance across symbols")
    @app_commands.describe(symbols="Comma/space-separated tickers, max 8",
                           duration="Lookback: 5 D | 1 M | 3 M | 6 M | 1 Y")
    async def compare_cmd(interaction: discord.Interaction, symbols: str,
                          duration: Optional[str] = None):
        if not await _guard_channel(interaction):
            return
        await interaction.response.defer(thinking=True)
        from urllib.parse import quote_plus
        syms = ",".join(s.strip().upper() for s in symbols.replace(",", " ").split() if s.strip())
        if not syms:
            return await interaction.followup.send("usage: `/gateway compare nvda smh spy`")
        q = f"?symbols={quote_plus(syms)}"
        if duration:
            q += f"&duration={quote_plus(duration)}"
        md = await bf.fetch_markdown_endpoint(f"/api/compare{q}", bf.mcp_url_from_env())
        await interaction.followup.send(bf.clean_markdown_for_discord(md, "compare"))

    # Register globally; on_ready will copy to the guild for instant availability.
    tree.add_command(group)

    # --- background watchdog loop -----------------------------------------

    # Outage backoff state, shared with /gateway restart (which resets it).
    watchdog_backoff: dict[str, gc.BackoffState] = {}

    async def _alert_channels(text: str) -> None:
        """Best-effort alert to the allowed control channels."""
        for cid in allowed_channels:
            try:
                ch = client.get_channel(int(cid))
                if ch is not None:
                    await ch.send(text)
            except Exception:
                log.exception("watchdog alert send failed for channel %s", cid)

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
                watchdog_backoff,
            )
            for action in actions:
                gw = cfg.get(action.gateway_name)
                if gw is None:
                    continue
                if action.reason == "gave_up":
                    msg = (f"{gw.name} still down after final backoff attempt — "
                           f"watchdog standing down until /gateway restart")
                    _watchdog_log(cfg, msg)
                    await _alert_channels(
                        f"🚨 `{gw.name}` gateway is still down after 4 restart "
                        f"attempts (immediate, +5m, +10m, +15m). Watchdog is "
                        f"standing down — if it's stuck on 2FA, approve the "
                        f"prompt, then run `/gateway restart`."
                    )
                    continue
                attempts = watchdog_backoff.get(gw.name)
                nth = attempts.attempts if attempts else "?"
                _watchdog_log(cfg, f"port probe failed for {gw.name}, restarting {gw.name} gateway (attempt {nth}/4)")
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
