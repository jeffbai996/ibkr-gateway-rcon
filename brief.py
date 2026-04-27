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
    since the whole brief is single-currency. Default 2 decimal places on M,
    1 on k."""
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a/1_000:.1f}k"
    return f"{sign}${a:.0f}"


def _money_cad_hi(n: float) -> str:
    """Higher-precision variant — one extra decimal place. Used for the
    account-level headline numbers (NLV, day P&L, liq, bp) where Jeff wants
    finer resolution."""
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.3f}M"
    if a >= 1_000:
        return f"{sign}${a/1_000:.2f}k"
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
            # Headline NLV gets the extra decimal.
            lines.append(f"NLV  {_money_cad_hi(combined_nlv)}")
            lines.append("")

        for acct in data.summary.get("accounts", []):
            acct_id = acct["account"]
            # If the MCP couldn't get this account's summary (e.g. subscription
            # stale), surface that honestly instead of showing $0 / CRIT.
            if acct.get("error") or acct.get("nlv") is None:
                lines.append(f"{acct_id}")
                err = acct.get("error", "data unavailable")
                lines.append(f"  ⚠️ {err}")
                lines.append("")
                continue

            nlv = acct.get("nlv", 0)
            cushion = acct.get("cushion_pct", 0)
            lev = acct.get("leverage", 0)
            liq = acct.get("excess_liquidity", 0)
            bp = acct.get("buying_power", 0)
            cash = acct.get("cash", 0)
            pnl_tuple = _extract_daily_pnl(data.pnl_by_account.get(acct_id, ""))

            lines.append(f"{acct_id}")
            # Account-level headlines use the higher-precision formatter.
            lines.append(f"  nlv     {_money_cad_hi(nlv)}")
            if pnl_tuple:
                dollars, pct = pnl_tuple
                lines.append(f"  day     {_money_cad_hi(dollars)} ({_pct(pct)})")
            lines.append(f"  liq     {_money_cad_hi(liq)}")
            lines.append(f"  bp      {_money_cad_hi(bp)}")
            # Cash is often negative and large — default precision is fine.
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
# Per-account deep views — pnl, positions, trades, margin
# ---------------------------------------------------------------------------


def _parse_pnl_markdown(md: str) -> dict[str, Optional[float]]:
    """Extract daily/unrealized/realized dollar amounts + daily % from the
    markdown body the MCP returns. Missing fields come back as None."""
    out: dict[str, Optional[float]] = {
        "daily": None,
        "daily_pct": None,
        "unrealized": None,
        "realized": None,
    }
    if not md:
        return out

    def _dollars(line: str) -> Optional[float]:
        if "$" not in line:
            return None
        try:
            after = line.split("$", 1)[1]
            token = after.split()[0].replace(",", "")
            val = float(token.lstrip("+"))
            # sign lives just before the $ (e.g. "-$1,234" or "+$1,234")
            before = line.split("$", 1)[0]
            if before.rstrip().endswith("-"):
                val = -val
            return val
        except (IndexError, ValueError):
            return None

    for raw in md.splitlines():
        line = raw.strip()
        if "Daily P&L" in line:
            out["daily"] = _dollars(line)
            if "(" in line and "%" in line:
                try:
                    pct_token = line.split("(", 1)[1].split("%", 1)[0]
                    out["daily_pct"] = float(pct_token.lstrip("+"))
                except (IndexError, ValueError):
                    pass
        elif "Unrealized P&L" in line:
            out["unrealized"] = _dollars(line)
        elif "Realized P&L" in line:
            out["realized"] = _dollars(line)
    return out


@dataclass
class AccountView:
    """Shared per-account fetch bundle used by /pnl, /positions, /trades,
    /margin. All four commands need the summary + some subset of positions /
    pnl / trades, so we fetch in parallel once and format separately."""

    summary: Optional[dict]
    positions: Optional[dict]
    pnl_by_account: dict[str, str]
    trades_by_account: dict[str, str]
    fx_rates: dict[str, float]
    healthy: bool
    fetch_errors: list[str]
    accounts: list[str]


async def fetch_account_view(
    mcp_url: str = MCP_DEFAULT_URL,
    want_positions: bool = True,
    want_pnl: bool = True,
    want_trades: bool = True,
) -> AccountView:
    """Fetch whatever subset the caller needs — skips endpoints that aren't
    required so /margin doesn't pay for positions, etc."""
    errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        health = await _fetch_json(session, f"{mcp_url}/api/health")
        if not health or health.get("status") != "ok":
            errors.append("mcp health check failed")
            return AccountView(None, None, {}, {}, {}, False, errors, [])

        accounts: list[str] = list(health.get("accounts", []))

        summary_task = _fetch_json(session, f"{mcp_url}/api/summary")
        fx_task = _fetch_json(session, f"{mcp_url}/api/prices?symbols=USDCAD=X")
        tasks_map: dict[str, Any] = {"summary": summary_task, "fx": fx_task}

        if want_positions:
            tasks_map["positions"] = _fetch_json(session, f"{mcp_url}/api/positions")
        if want_pnl:
            for acct in accounts:
                tasks_map[f"pnl:{acct}"] = _fetch_json(
                    session, f"{mcp_url}/api/account-pnl?account={acct}"
                )
        if want_trades:
            for acct in accounts:
                tasks_map[f"trades:{acct}"] = _fetch_json(
                    session, f"{mcp_url}/api/trades?account={acct}"
                )

        keys = list(tasks_map.keys())
        results = await asyncio.gather(*tasks_map.values())
        by_key = dict(zip(keys, results))

        summary = by_key.get("summary")
        positions = by_key.get("positions")
        fx_json = by_key.get("fx")

        pnl_md: dict[str, str] = {}
        trades_md: dict[str, str] = {}
        for acct in accounts:
            if want_pnl:
                pnl_md[acct] = (by_key.get(f"pnl:{acct}") or {}).get("markdown", "")
            if want_trades:
                trades_md[acct] = (by_key.get(f"trades:{acct}") or {}).get("markdown", "")

        fx_rates: dict[str, float] = {}
        if fx_json and "prices" in fx_json:
            usdcad_data = fx_json["prices"].get("USDCAD=X", {})
            if "price" in usdcad_data:
                fx_rates["USDCAD"] = float(usdcad_data["price"])

        if summary is None:
            errors.append("summary fetch failed")
        if want_positions and positions is None:
            errors.append("positions fetch failed")
        if not fx_rates:
            errors.append("fx fetch failed")

        return AccountView(
            summary=summary,
            positions=positions,
            pnl_by_account=pnl_md,
            trades_by_account=trades_md,
            fx_rates=fx_rates,
            healthy=True,
            fetch_errors=errors,
            accounts=accounts,
        )


def _accounts_for(view: AccountView, requested: Optional[str]) -> tuple[list[str], Optional[str]]:
    """Resolve an optional account filter against the discovered list.
    Returns (accounts_to_render, error_message_or_None)."""
    if requested is None:
        return view.accounts, None
    if requested in view.accounts:
        return [requested], None
    return [], f"unknown account `{requested}` — known: {', '.join(view.accounts) or '(none)'}"


def build_pnl(view: AccountView, account: Optional[str] = None) -> str:
    """Per-account daily/unrealized/realized P&L. CAD everywhere since that's
    the reporting currency the MCP returns."""
    if not view.healthy:
        return "⚠️ pnl unavailable — ibkr mcp not responding."

    accounts, err = _accounts_for(view, account)
    if err:
        return err
    if not accounts:
        return "no accounts available"

    lines: list[str] = ["```"]
    usdcad = view.fx_rates.get("USDCAD")
    if usdcad:
        lines.append(f"all values CAD · USDCAD {usdcad:.4f}")
        lines.append("")

    summary_by_acct = {
        a.get("account"): a for a in (view.summary or {}).get("accounts", [])
    }

    combined_daily = 0.0
    combined_unreal = 0.0
    combined_realized = 0.0
    any_daily = False
    any_unreal = False
    any_realized = False

    for i, acct in enumerate(accounts):
        if i > 0:
            lines.append("")
        lines.append(acct)
        summary = summary_by_acct.get(acct) or {}
        # MCP subscription stale / reconnect race — surface honestly instead of
        # rendering $0s that look like real numbers.
        if summary.get("error") or summary.get("nlv") is None:
            err = summary.get("error", "data unavailable")
            lines.append(f"  ⚠️ {err}")
            continue

        parsed = _parse_pnl_markdown(view.pnl_by_account.get(acct, ""))
        # Treat a trivially-zero P&L markdown block (all three fields zero or
        # N/A) the same way — it means the account's subscription hasn't
        # populated, not that P&L is actually zero.
        looks_empty = (
            parsed["daily"] is None
            and (parsed["unrealized"] or 0) == 0
            and (parsed["realized"] or 0) == 0
        )
        if looks_empty:
            lines.append(f"  ⚠️ pnl data unavailable")
            continue

        nlv = summary.get("nlv")

        if nlv is not None:
            lines.append(f"  nlv        {_money_cad_hi(nlv)}")
        if parsed["daily"] is not None:
            any_daily = True
            combined_daily += parsed["daily"]
            if parsed["daily_pct"] is not None:
                lines.append(
                    f"  day        {_money_cad_hi(parsed['daily'])} ({_pct(parsed['daily_pct'])})"
                )
            else:
                lines.append(f"  day        {_money_cad_hi(parsed['daily'])}")
        if parsed["unrealized"] is not None:
            any_unreal = True
            combined_unreal += parsed["unrealized"]
            lines.append(f"  unrealized {_money_cad_hi(parsed['unrealized'])}")
        if parsed["realized"] is not None:
            any_realized = True
            combined_realized += parsed["realized"]
            lines.append(f"  realized   {_money_cad_hi(parsed['realized'])}")

    # Combined totals only when rendering all accounts.
    if account is None and len(accounts) > 1:
        combined_nlv = (view.summary or {}).get("combined_nlv")
        lines.append("")
        lines.append("combined")
        if combined_nlv is not None:
            lines.append(f"  nlv        {_money_cad_hi(combined_nlv)}")
        if any_daily:
            pct = (combined_daily / combined_nlv * 100) if combined_nlv else 0.0
            lines.append(f"  day        {_money_cad_hi(combined_daily)} ({_pct(pct)})")
        if any_unreal:
            lines.append(f"  unrealized {_money_cad_hi(combined_unreal)}")
        if any_realized:
            lines.append(f"  realized   {_money_cad_hi(combined_realized)}")

    lines.append("```")
    if view.fetch_errors:
        lines.append(f"⚠️ partial: {', '.join(view.fetch_errors)}")
    return "\n".join(lines)


def _positions_for_account(
    positions_json: dict,
    account: Optional[str],
    fx: dict[str, float],
) -> list[dict]:
    """Positions combined across accounts (or filtered to one) and grouped by
    (symbol, currency) so CDR listings stay distinct from US parents."""
    by_key: dict[tuple[str, str], dict] = {}
    for p in positions_json.get("positions", []):
        if account is not None and p.get("account") != account:
            continue
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


def build_positions(
    view: AccountView,
    account: Optional[str] = None,
    top_n: int = 10,
) -> str:
    """Positions view with cost basis, mv, unrealized P&L + %. Defaults to top
    10 across all accounts; narrower than /brief which caps at 5."""
    if not view.healthy:
        return "⚠️ positions unavailable — ibkr mcp not responding."
    if view.positions is None:
        return "⚠️ positions fetch failed"

    accounts, err = _accounts_for(view, account)
    if err:
        return err

    # If a specific account was asked for, check whether the summary for that
    # account reports an error — in that case positions will be empty not
    # because there are none, but because the subscription is stale. Surface
    # that instead of lying with "no positions".
    if account is not None and view.summary is not None:
        summary_by_acct = {a.get("account"): a for a in view.summary.get("accounts", [])}
        a = summary_by_acct.get(account) or {}
        if a.get("error") or a.get("nlv") is None:
            err_msg = a.get("error", "data unavailable")
            return f"⚠️ {account}: {err_msg}"

    filter_acct = account if account is not None else None
    rows = _positions_for_account(view.positions, filter_acct, view.fx_rates)[:top_n]
    if not rows:
        return f"no positions {'for ' + account if account else 'found'}"

    lines: list[str] = ["```"]
    usdcad = view.fx_rates.get("USDCAD")
    scope = account if account else "all accounts"
    header = f"positions · {scope}"
    if usdcad:
        header += f" · USDCAD {usdcad:.4f}"
    lines.append(header)
    lines.append("")
    lines.append(f"{'sym':<7} {'shares':>7} {'avg':>7} {'mv':>7} {'pnl':>8} {'%':>7}")
    for r in rows:
        label = r["label"][:7]
        shares = f"{int(r['shares']):,}"
        avg = f"${r['avg_cost_cad']:,.0f}"
        mv = _money_cad(r["market_value_cad"])
        pnl = _money_cad(r["unrealized_pnl_cad"])
        pct = _pct(r["unrealized_pct"])
        lines.append(f"{label:<7} {shares:>7} {avg:>7} {mv:>7} {pnl:>8} {pct:>7}")
    lines.append("```")
    if view.fetch_errors:
        lines.append(f"⚠️ partial: {', '.join(view.fetch_errors)}")
    return "\n".join(lines)


def build_trades(view: AccountView, account: Optional[str] = None) -> str:
    """Dump today's executions per account. The MCP already returns a neatly
    formatted markdown table — we just concatenate with a header per account."""
    if not view.healthy:
        return "⚠️ trades unavailable — ibkr mcp not responding."

    accounts, err = _accounts_for(view, account)
    if err:
        return err
    if not accounts:
        return "no accounts available"

    # Any account whose summary reports an error gets flagged — "No executions"
    # from a disconnected account is misleading (we don't actually know).
    summary_by_acct: dict[str, dict] = {}
    if view.summary is not None:
        summary_by_acct = {a.get("account"): a for a in view.summary.get("accounts", [])}

    chunks: list[str] = []
    any_trades = False
    all_flagged = True
    for acct in accounts:
        a = summary_by_acct.get(acct) or {}
        if a.get("error") or (a and a.get("nlv") is None):
            err_msg = a.get("error", "data unavailable")
            chunks.append(f"**{acct}**")
            chunks.append(f"⚠️ {err_msg} (trade data not reliable)")
            chunks.append("")
            continue

        md = (view.trades_by_account.get(acct) or "").strip()
        if not md:
            continue
        chunks.append(f"**{acct}**")
        chunks.append(md)
        chunks.append("")
        all_flagged = False
        if "No executions" not in md:
            any_trades = True

    if not chunks:
        return "no trade data available"
    if not any_trades and not all_flagged:
        return "no executions this session"

    out = "\n".join(chunks).rstrip()
    if view.fetch_errors:
        out += f"\n⚠️ partial: {', '.join(view.fetch_errors)}"
    return out


def build_margin(view: AccountView, account: Optional[str] = None) -> str:
    """Margin close-up: init/maint margin, cushion, excess liq, bp, leverage,
    utilization %. Pulls everything from the summary payload."""
    if not view.healthy or view.summary is None:
        return "⚠️ margin unavailable — ibkr mcp not responding."

    accounts, err = _accounts_for(view, account)
    if err:
        return err

    by_acct = {a.get("account"): a for a in view.summary.get("accounts", [])}
    if account is not None and account not in by_acct:
        return f"no summary for `{account}`"

    render_accts = [account] if account else list(by_acct.keys())
    if not render_accts:
        return "no accounts available"

    lines: list[str] = ["```"]
    lines.append("all values CAD")
    lines.append("")

    for i, acct_id in enumerate(render_accts):
        if i > 0:
            lines.append("")
        a = by_acct.get(acct_id) or {}
        lines.append(f"{acct_id}")
        # MCP returns {"account": X, "error": "No summary available"} when the
        # ib_insync subscription hasn't populated for this account. Surface
        # the real reason instead of rendering zeros that look like collapse.
        if not a or a.get("error") or a.get("nlv") is None:
            err = a.get("error", "data unavailable")
            lines.append(f"  ⚠️ {err}")
            continue
        cushion = a.get("cushion_pct", 0)
        cushion_tag = "ok" if cushion >= 10 else "tight" if cushion >= 5 else "CRIT"
        lines.append(f"  nlv         {_money_cad_hi(a.get('nlv', 0))}")
        lines.append(f"  gpv         {_money_cad_hi(a.get('gpv', 0))}")
        lines.append(f"  init margin {_money_cad_hi(a.get('init_margin', 0))}")
        lines.append(f"  maint       {_money_cad_hi(a.get('maint_margin', 0))}")
        lines.append(f"  excess liq  {_money_cad_hi(a.get('excess_liquidity', 0))}")
        lines.append(f"  buying pwr  {_money_cad_hi(a.get('buying_power', 0))}")
        lines.append(f"  cushion     {cushion:.1f}% ({cushion_tag})")
        lines.append(f"  leverage    {a.get('leverage', 0):.2f}x")
        lines.append(f"  margin util {a.get('margin_util_pct', 0):.1f}%")

    lines.append("```")
    if view.fetch_errors:
        lines.append(f"⚠️ partial: {', '.join(view.fetch_errors)}")
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
    mcp_per_gateway: dict[str, dict] = None  # type: ignore[assignment]
    account_errors: list[str] = None  # type: ignore[assignment]


def fetch_health_data(
    cfg: gc.Config,
    port_listening: Callable[[int], bool],
    heartbeat_path: Path,
    watchdog_interval_s: int,
    now: datetime,
    mcp_per_gateway: Optional[dict[str, dict]] = None,
    account_errors: Optional[list[str]] = None,
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
        mcp_per_gateway=mcp_per_gateway or {},
        account_errors=account_errors or [],
    )


async def fetch_mcp_status(mcp_url: str = MCP_DEFAULT_URL) -> tuple[dict[str, dict], list[str]]:
    """Pull per-gateway MCP connection state + any account-level subscription
    errors. Returns ({gateway_name: {connected, last_data_age_s}}, [error_msg]).
    Both empty on any failure — caller treats that as 'mcp didn't answer'."""
    async with aiohttp.ClientSession() as session:
        health = await _fetch_json(session, f"{mcp_url}/api/health")
        summary = await _fetch_json(session, f"{mcp_url}/api/summary")

    per_gw: dict[str, dict] = {}
    if health:
        for key in ("primary", "secondary"):
            if isinstance(health.get(key), dict):
                per_gw[key] = health[key]

    errs: list[str] = []
    if summary:
        for a in summary.get("accounts", []):
            if a.get("error"):
                errs.append(f"{a.get('account', '?')}: {a['error']}")
            elif a.get("nlv") is None:
                errs.append(f"{a.get('account', '?')}: data unavailable")
    return per_gw, errs


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def build_health(data: HealthData, now: datetime) -> str:
    """Mobile-friendly health with readable labels."""
    lines: list[str] = ["```"]

    # Watchdog heartbeat
    if data.heartbeat_age_s is None:
        lines.append("watchdog: heartbeat MISSING")
    else:
        max_healthy = data.watchdog_interval_s * 2
        if data.heartbeat_age_s < max_healthy:
            lines.append(
                f"watchdog: healthy ({_fmt_age(data.heartbeat_age_s)} ago, "
                f"ticks every {data.watchdog_interval_s}s)"
            )
        else:
            lines.append(
                f"watchdog: STALE ({_fmt_age(data.heartbeat_age_s)} ago — "
                f"expected every {data.watchdog_interval_s}s)"
            )
    lines.append("")

    for i, st in enumerate(data.gateways):
        if i > 0:
            lines.append("")
        state = "running" if st.up else "stopped"
        lines.append(f"{st.name} (port {st.port})")
        lines.append(f"  state:   {state}")

        # MCP-layer view of this gateway: TCP connected + how fresh the last
        # data packet was. This is the piece that catches "port open but
        # subscription dead" — the wifey's-gateway failure mode.
        mcp_info = (data.mcp_per_gateway or {}).get(st.name)
        if mcp_info is not None:
            if not mcp_info.get("connected"):
                lines.append(f"  mcp:     disconnected")
            else:
                age = mcp_info.get("last_data_age_s")
                if age is None:
                    lines.append(f"  mcp:     connected")
                elif age < 60:
                    lines.append(f"  mcp:     connected ({int(age)}s data)")
                elif age < 600:
                    lines.append(f"  mcp:     connected ({int(age/60)}m data)")
                else:
                    lines.append(f"  mcp:     STALE ({_fmt_age(age)} data)")

        if st.skipped:
            if st.skipped_until is None:
                lines.append(f"  watchdog paused indefinitely")
            else:
                delta = (st.skipped_until - now).total_seconds()
                if delta <= 0:
                    lines.append(f"  watchdog paused (expiring)")
                elif delta < 60:
                    lines.append(f"  watchdog paused for {int(delta)}s more")
                elif delta < 3600:
                    lines.append(f"  watchdog paused for {int(delta/60)}m more")
                elif delta < 86400:
                    lines.append(f"  watchdog paused for {delta/3600:.1f}h more")
                else:
                    lines.append(f"  watchdog paused for {delta/86400:.1f}d more")
        else:
            lines.append(f"  watchdog active")

        restarts = data.restarts_last_24h.get(st.name, 0)
        last = data.last_restart_per_gateway.get(st.name)
        if last is None:
            lines.append(f"  no restarts on record")
        else:
            delta = (now - last).total_seconds()
            lines.append(f"  last restart {_fmt_age(delta)} ago")
        lines.append(f"  restarts in last 24h: {restarts}")

    # Account-level subscription health — a gateway can be "connected" at the
    # TCP level but still have a dead accountSummary subscription for one or
    # both accounts. Flag it clearly so it doesn't look like the gateway is
    # fine when /pnl and /margin know it isn't.
    if data.account_errors:
        lines.append("")
        lines.append("account subscriptions:")
        for e in data.account_errors:
            lines.append(f"  ⚠️ {e}")

    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Quotes — live prices for arbitrary symbols
# ---------------------------------------------------------------------------


def _fmt_quote_price(n: float) -> str:
    if n >= 1000:
        return f"${n:,.2f}"
    if n >= 1:
        return f"${n:.2f}"
    return f"${n:.4f}"


async def fetch_quotes(
    symbols: list[str],
    mcp_url: str = MCP_DEFAULT_URL,
) -> tuple[dict[str, dict], list[str]]:
    """Fetch live quotes for the given symbols. Returns (prices_by_symbol,
    errors). prices_by_symbol mirrors the MCP /api/prices response shape;
    callers should defensively read fields since IBKR snapshots vary."""
    if not symbols:
        return {}, ["no symbols requested"]
    syms_param = ",".join(symbols)
    async with aiohttp.ClientSession() as session:
        data = await _fetch_json(session, f"{mcp_url}/api/prices?symbols={syms_param}")
    if not data or "prices" not in data:
        return {}, ["mcp prices fetch failed"]
    return data["prices"], []


def build_quotes(
    symbols: list[str],
    prices: dict[str, dict],
    errors: list[str],
) -> str:
    """Render a narrow 3-column quote table (sym / last / chg %). Preserves
    the order the user typed. Symbols missing from the response render as '—'
    and are listed in a footer."""
    if errors and not prices:
        return f"⚠️ quotes unavailable — {'; '.join(errors)}"

    lines: list[str] = ["```"]
    lines.append(f"{'sym':<5} {'last':>10} {'chg':>7}")

    missing: list[str] = []
    for sym in symbols:
        info = prices.get(sym) or prices.get(sym.upper()) or {}
        raw_price = info.get("price")
        try:
            price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None:
            missing.append(sym)
            lines.append(f"{sym[:5]:<5} {'—':>10} {'—':>7}")
            continue
        chg_pct: Optional[float]
        try:
            raw_chg = info.get("change_pct")
            chg_pct = float(raw_chg) if raw_chg is not None and raw_chg != "" else None
        except (TypeError, ValueError):
            chg_pct = None
        if chg_pct is None:
            prev = info.get("prev_close")
            if prev not in (None, ""):
                try:
                    chg_pct = (price - float(prev)) / float(prev) * 100
                except (TypeError, ValueError, ZeroDivisionError):
                    chg_pct = None
        chg_str = _pct(chg_pct) if chg_pct is not None else "—"
        lines.append(f"{sym[:5]:<5} {_fmt_quote_price(price):>10} {chg_str:>7}")

    lines.append("```")
    if missing:
        lines.append(f"⚠️ no data: {', '.join(missing)}")
    return "\n".join(lines)


def mcp_url_from_env() -> str:
    return os.environ.get("IBKR_MCP_URL", MCP_DEFAULT_URL)
