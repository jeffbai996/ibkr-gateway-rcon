"""Portfolio brief + gateway health composition.

Reads from:
- The IBKR MCP dashboard HTTP API (http://<host>:<port>/api/*) for portfolio data.
- The bot's own watchdog log + heartbeat file for health data.

Output is a mobile-friendly Discord message — short rows, stacked layout, no
wide tables that wrap on narrow screens.

Kept separate from discord_bot.py so it can be tested without spinning up
discord.py. The bot imports `build_brief` and `build_health` and sends the
returned string.
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

import gateway_ctl as gc


MCP_DEFAULT_URL = "http://localhost:8001"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _money(n: float, ccy: str = "CAD") -> str:
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.2f}M {ccy}"
    if a >= 1_000:
        return f"{sign}${a/1_000:.1f}k {ccy}"
    return f"{sign}${a:.0f} {ccy}"


def _pct(n: float) -> str:
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.2f}%"


def _emoji_pnl(n: float) -> str:
    if n > 0:
        return "🟢"
    if n < 0:
        return "🔴"
    return "⚪"


# ---------------------------------------------------------------------------
# Brief — portfolio at a glance
# ---------------------------------------------------------------------------


@dataclass
class BriefData:
    """Everything the brief template needs, pre-fetched and merged."""

    summary: Optional[dict]
    positions: Optional[dict]
    pnl_by_account: dict[str, str]  # account -> markdown blob
    trades_by_account: dict[str, str]  # account -> markdown blob
    fx_rates: dict[str, float]  # e.g. {"USDCAD": 1.3687}
    healthy: bool
    fetch_errors: list[str]


async def fetch_brief_data(mcp_url: str = MCP_DEFAULT_URL) -> BriefData:
    errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        health = await _fetch_json(session, f"{mcp_url}/api/health")
        if not health or health.get("status") != "ok":
            errors.append("mcp health check failed")
            return BriefData(None, None, {}, {}, {}, False, errors)

        accounts = health.get("accounts", [])

        summary_task = _fetch_json(session, f"{mcp_url}/api/summary")
        positions_task = _fetch_json(session, f"{mcp_url}/api/positions")
        fx_task = _fetch_json(session, f"{mcp_url}/api/prices?symbols=USDCAD=X")
        pnl_tasks = {
            acct: _fetch_json(session, f"{mcp_url}/api/account-pnl?account={acct}")
            for acct in accounts
        }
        trades_tasks = {
            acct: _fetch_json(session, f"{mcp_url}/api/trades?account={acct}")
            for acct in accounts
        }

        summary, positions, fx_json = await asyncio.gather(summary_task, positions_task, fx_task)
        pnl_results = dict(zip(pnl_tasks.keys(), await asyncio.gather(*pnl_tasks.values())))
        trades_results = dict(zip(trades_tasks.keys(), await asyncio.gather(*trades_tasks.values())))

        pnl_md = {a: (r or {}).get("markdown", "") for a, r in pnl_results.items()}
        trades_md = {a: (r or {}).get("markdown", "") for a, r in trades_results.items()}

        fx_rates: dict[str, float] = {}
        if fx_json and "prices" in fx_json:
            usdcad_data = fx_json["prices"].get("USDCAD=X", {})
            if "price" in usdcad_data:
                fx_rates["USDCAD"] = float(usdcad_data["price"])

        if summary is None:
            errors.append("summary fetch failed")
        if positions is None:
            errors.append("positions fetch failed")
        if not fx_rates:
            errors.append("fx fetch failed")

        return BriefData(summary, positions, pnl_md, trades_md, fx_rates, True, errors)


def _fx_to_cad(value: float, ccy: str, fx: dict[str, float]) -> float:
    """Convert a value in some currency to CAD using live rates. CAD passes
    through; USD uses USDCAD; other currencies return value unchanged with
    no failure (tiny positions won't skew much)."""
    if ccy == "CAD":
        return value
    if ccy == "USD" and "USDCAD" in fx:
        return value * fx["USDCAD"]
    # Unknown currency / missing rate: treat as zero-impact pass-through.
    return value


def _combine_positions(positions: dict, fx: dict[str, float]) -> list[dict]:
    """Group positions by (symbol, currency), summing across accounts.

    Keeps CDR listings (CAD) as separate rows from their US parents — trying
    to merge them is meaningless because they have different shares-per-unit.
    Label with '(C)' for non-USD listings so Jeff can spot them.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for p in positions.get("positions", []):
        sym = p["symbol"]
        ccy = p.get("currency", "USD")
        key = (sym, ccy)
        if key not in by_key:
            by_key[key] = {
                "symbol": sym,
                "currency": ccy,
                "shares": 0.0,
                "cost_total_native": 0.0,
                "market_value_native": 0.0,
                "unrealized_pnl_native": 0.0,
            }
        row = by_key[key]
        shares = float(p.get("shares", 0))
        avg_cost = float(p.get("avg_cost", 0))
        row["shares"] += shares
        row["cost_total_native"] += shares * avg_cost
        row["market_value_native"] += float(p.get("market_value", 0))
        row["unrealized_pnl_native"] += float(p.get("unrealized_pnl", 0))

    out = []
    for (sym, ccy), r in by_key.items():
        avg_native = (r["cost_total_native"] / r["shares"]) if r["shares"] else 0.0
        unreal_pct = (
            (r["unrealized_pnl_native"] / r["cost_total_native"] * 100)
            if r["cost_total_native"] else 0.0
        )
        # Display label: 'GENERIC' for USD, 'GENERIC(C)' for CAD (CDR-style).
        label = sym if ccy == "USD" else f"{sym}(C)"
        out.append({
            "label": label,
            "symbol": sym,
            "currency": ccy,
            "shares": r["shares"],
            "avg_cost_cad": _fx_to_cad(avg_native, ccy, fx),
            "market_value_cad": _fx_to_cad(r["market_value_native"], ccy, fx),
            "unrealized_pnl_cad": _fx_to_cad(r["unrealized_pnl_native"], ccy, fx),
            "unrealized_pct": unreal_pct,
        })

    out.sort(key=lambda r: r["market_value_cad"], reverse=True)
    return out


def _extract_daily_pnl(pnl_md: str) -> Optional[tuple[float, float]]:
    """Parse '**Daily P&L**: +$199,838.47 CAD (+2.99% of NLV)' → (dollars, percent)."""
    if not pnl_md:
        return None
    for line in pnl_md.splitlines():
        if "Daily P&L" in line:
            try:
                # Extract dollar amount between $ and ' CAD'
                dollar_part = line.split("$", 1)[1].split(" ", 1)[0].replace(",", "")
                dollars = float(dollar_part.lstrip("+"))
                if "-" in line.split("$")[0][-3:]:  # negative sign before $
                    dollars = -dollars
                # Extract percent
                pct_part = line.split("(")[1].split("%")[0]
                percent = float(pct_part.lstrip("+"))
                return dollars, percent
            except (IndexError, ValueError):
                return None
    return None


def _today_trades_brief(trades_md: str, max_lines: int = 6) -> list[str]:
    """Pull readable trade lines out of the markdown trades response."""
    if not trades_md or "No executions" in trades_md:
        return []
    lines = []
    for raw in trades_md.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        if s.startswith("|") and "-|-" in s:  # table separator
            continue
        # Filter out the markdown table header bar
        if s.startswith("|") and s.count("|") >= 4:
            lines.append(s)
    return lines[:max_lines]


def _money_cad(n: float) -> str:
    """Money formatter for values known to be in CAD. Strips the ccy suffix
    since the whole brief is single-currency."""
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a/1_000:.1f}k"
    return f"{sign}${a:.0f}"


def build_brief(data: BriefData, top_n: int = 5) -> str:
    """Mobile-friendly brief, CAD-only, wrapped in a code block. <40 char lines."""
    if not data.healthy:
        return "⚠️ brief unavailable — ibkr mcp not responding."

    lines: list[str] = ["```"]

    # FX note at the top
    usdcad = data.fx_rates.get("USDCAD")
    if usdcad:
        lines.append(f"all values CAD · USDCAD {usdcad:.4f}")
        lines.append("")

    if data.summary:
        combined_nlv = data.summary.get("combined_nlv")
        if combined_nlv is not None:
            lines.append(f"NLV  {_money_cad(combined_nlv)}")
            lines.append("")

        for acct in data.summary.get("accounts", []):
            acct_id = acct["account"]
            nlv = acct.get("nlv", 0)
            cushion = acct.get("cushion_pct", 0)
            lev = acct.get("leverage", 0)
            liq = acct.get("excess_liquidity", 0)
            bp = acct.get("buying_power", 0)
            cash = acct.get("cash", 0)
            pnl_tuple = _extract_daily_pnl(data.pnl_by_account.get(acct_id, ""))

            lines.append(f"{acct_id}")
            lines.append(f"  nlv     {_money_cad(nlv)}")
            if pnl_tuple:
                dollars, pct = pnl_tuple
                lines.append(f"  day     {_money_cad(dollars)} ({_pct(pct)})")
            lines.append(f"  liq     {_money_cad(liq)}")
            lines.append(f"  bp      {_money_cad(bp)}")
            lines.append(f"  cash    {_money_cad(cash)}")
            cushion_tag = "ok" if cushion >= 10 else "tight" if cushion >= 5 else "CRIT"
            lines.append(f"  cushion {cushion:.1f}% ({cushion_tag})")
            lines.append(f"  lev     {lev:.2f}x")
            lines.append("")

    if data.positions:
        rows = _combine_positions(data.positions, data.fx_rates)[:top_n]
        if rows:
            # --- Holdings section ---
            lines.append("top positions (CAD)")
            lines.append(f"  {'sym':<7} {'shares':>7} {'avg':>7} {'mv':>7}")
            for r in rows:
                label = r["label"][:7]
                shares = f"{int(r['shares']):,}"
                avg = f"${r['avg_cost_cad']:,.0f}"
                mv = _money_cad(r["market_value_cad"])
                lines.append(f"  {label:<7} {shares:>7} {avg:>7} {mv:>7}")
            lines.append("")

            # --- Unrealized P&L section ---
            lines.append("unrealized p&l")
            lines.append(f"  {'sym':<7} {'pnl':>8} {'%':>8}")
            for r in rows:
                label = r["label"][:7]
                pnl = _money_cad(r["unrealized_pnl_cad"])
                pct = _pct(r["unrealized_pct"])
                lines.append(f"  {label:<7} {pnl:>8} {pct:>8}")
            lines.append("")

    # Today's trades — compact
    trades_any = False
    for acct, md in data.trades_by_account.items():
        tlines = _today_trades_brief(md, max_lines=5)
        if tlines:
            if not trades_any:
                lines.append("today's trades")
                trades_any = True
            lines.append(f"  {acct}")
            for t in tlines:
                cells = [c.strip() for c in t.strip("|").split("|")]
                compact = " ".join(c for c in cells if c)[:34]
                lines.append(f"    {compact}")
    if trades_any:
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    lines.append("```")

    if data.fetch_errors:
        lines.append(f"⚠️ partial: {', '.join(data.fetch_errors)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Health — gateway process state
# ---------------------------------------------------------------------------


@dataclass
class HealthData:
    gateways: list[gc.GatewayStatus]
    heartbeat_age_s: Optional[float]
    restarts_last_24h: dict[str, int]
    last_restart_per_gateway: dict[str, Optional[datetime]]
    watchdog_interval_s: int


def fetch_health_data(
    cfg: gc.Config,
    port_listening: Callable[[int], bool],
    heartbeat_path: Path,
    watchdog_interval_s: int,
    now: datetime,
) -> HealthData:
    statuses = [
        gc.status_for(gw, port_listening=port_listening, log_path=cfg.log_file, now=now)
        for gw in cfg.gateways
    ]

    hb = gc.read_heartbeat(heartbeat_path)
    hb_age = (now - hb).total_seconds() if hb else None

    # Count restarts per gateway in the last 24h
    restarts_24h: dict[str, int] = defaultdict(int)
    last_restart: dict[str, Optional[datetime]] = {gw.name: None for gw in cfg.gateways}
    cutoff = now - timedelta(hours=24)
    import re
    line_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — (\w+) restart command issued")
    if cfg.log_file.exists():
        for raw in cfg.log_file.read_text().splitlines():
            m = line_re.match(raw)
            if not m:
                continue
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            name = m.group(2)
            if name not in last_restart:
                continue
            if dt >= cutoff:
                restarts_24h[name] += 1
            if last_restart[name] is None or dt > last_restart[name]:
                last_restart[name] = dt

    return HealthData(
        gateways=statuses,
        heartbeat_age_s=hb_age,
        restarts_last_24h=dict(restarts_24h),
        last_restart_per_gateway=last_restart,
        watchdog_interval_s=watchdog_interval_s,
    )


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def build_health(data: HealthData, now: datetime) -> str:
    """Mobile-friendly health wrapped in a code block."""
    lines: list[str] = ["```"]

    # Heartbeat
    if data.heartbeat_age_s is None:
        lines.append("heartbeat  MISSING")
    else:
        max_healthy = data.watchdog_interval_s * 2
        tag = "ok" if data.heartbeat_age_s < max_healthy else "STALE"
        lines.append(f"heartbeat  {_fmt_age(data.heartbeat_age_s)} ago ({tag})")
    lines.append(f"interval   {data.watchdog_interval_s}s")
    lines.append("")

    for i, st in enumerate(data.gateways):
        if i > 0:
            lines.append("")
        state = "UP" if st.up else "DOWN"
        lines.append(f"{st.name} (port {st.port})")
        lines.append(f"  state     {state}")
        if st.skipped:
            if st.skipped_until is None:
                lines.append(f"  paused    indefinite")
            else:
                delta = (st.skipped_until - now).total_seconds()
                lines.append(f"  paused    {_fmt_age(max(delta, 0))} left")
        else:
            lines.append(f"  paused    no")
        restarts = data.restarts_last_24h.get(st.name, 0)
        last = data.last_restart_per_gateway.get(st.name)
        last_str = "never" if last is None else f"{_fmt_age((now - last).total_seconds())} ago"
        lines.append(f"  24h rest  {restarts}")
        lines.append(f"  last      {last_str}")

    lines.append("```")
    return "\n".join(lines)


def mcp_url_from_env() -> str:
    return os.environ.get("IBKR_MCP_URL", MCP_DEFAULT_URL)
