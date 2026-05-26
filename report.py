"""Detailed portfolio report — the granular cousin of /gateway brief.

Backs `/gateway report`. Where brief abbreviates ($1.28M) and shows the top
handful, the report shows full grouped numbers ($1,284,300), a rich margin
block (cushion, max-drawdown-before-forced-liquidation, excess liquidity),
every position with its NLV weight, local concentration (top-3 + HHI), and a
fixed −10% stress line.

Data comes ONLY from the direct IBKR MCP REST endpoints — no Gemini /api/query
LLM path (slow, costs spend, prose-parse brittle). Sources:
- /api/summary    structured account dict (nlv, margin fields, leverage)
- /api/positions  per-lot rows with weight_pct already computed by the MCP
- /api/margin     fixed markdown — max-drawdown distances brief doesn't carry
- /api/stress     fixed markdown — preflight buffer at a given drawdown
- /api/account-pnl markdown — daily P&L (reused via brief._extract_daily_pnl)

Kept separate from discord_bot.py so it tests without spinning up discord.py.
The bot imports `fetch_report_data` + `build_report`.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

import brief as bf

# The −10% scenario used for the stress line. A proxy for "semis drop 10%"
# since the whole book is concentrated semis; a portfolio-wide 10% drawdown
# is the closest deterministic preflight the MCP exposes.
STRESS_DRAWDOWN_PCT = 10.0

_PACIFIC = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class ReportData:
    summary: Optional[dict]
    positions: Optional[dict]
    margin_md: Optional[str]        # /api/margin markdown (may be None on fail)
    stress_md: Optional[str]        # /api/stress markdown (may be None on fail)
    fx_rates: dict[str, float]
    healthy: bool
    fetch_errors: list[str] = field(default_factory=list)
    pnl_by_account: dict[str, str] = field(default_factory=dict)


async def fetch_report_data(mcp_url: str = bf.MCP_DEFAULT_URL) -> ReportData:
    """Pull everything the report needs from the direct REST endpoints.

    All fetches run concurrently. Any single failure degrades that section to
    'n/a' rather than failing the whole report — except a failed health check,
    which means no live data at all.
    """
    errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        health = await bf._fetch_json(session, f"{mcp_url}/api/health")
        if not health or health.get("status") not in ("ok", "degraded"):
            errors.append("mcp health check failed")
            return ReportData(None, None, None, None, {}, False, errors)

        accounts = health.get("accounts", [])
        primary = accounts[0] if accounts else None

        summary_task = bf._fetch_json(session, f"{mcp_url}/api/summary")
        positions_task = bf._fetch_json(session, f"{mcp_url}/api/positions")
        fx_task = bf._fetch_json(session, f"{mcp_url}/api/prices?symbols=USDCAD=X")
        margin_task = bf._fetch_json(
            session,
            f"{mcp_url}/api/margin" + (f"?account={primary}" if primary else ""),
        )
        stress_task = bf._fetch_json(
            session, f"{mcp_url}/api/stress?drawdown_pct={STRESS_DRAWDOWN_PCT}"
        )
        pnl_tasks = {
            acct: bf._fetch_json(session, f"{mcp_url}/api/account-pnl?account={acct}")
            for acct in accounts
        }

        (summary, positions, fx_json, margin_json, stress_json) = await asyncio.gather(
            summary_task, positions_task, fx_task, margin_task, stress_task
        )
        pnl_results = dict(zip(pnl_tasks.keys(), await asyncio.gather(*pnl_tasks.values())))
        pnl_md = {a: (r or {}).get("markdown", "") for a, r in pnl_results.items()}

        fx_rates: dict[str, float] = {}
        if fx_json and "prices" in fx_json:
            usdcad = fx_json["prices"].get("USDCAD=X", {})
            if "price" in usdcad:
                fx_rates["USDCAD"] = float(usdcad["price"])

        if summary is None:
            errors.append("summary fetch failed")
        if positions is None:
            errors.append("positions fetch failed")

        return ReportData(
            summary=summary,
            positions=positions,
            margin_md=(margin_json or {}).get("markdown") if margin_json else None,
            stress_md=(stress_json or {}).get("markdown") if stress_json else None,
            fx_rates=fx_rates,
            healthy=True,
            fetch_errors=errors,
            pnl_by_account=pnl_md,
        )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _money_full(n: float) -> str:
    """Full grouped dollars, no M/k abbreviation: 1284300 -> '$1,284,300'.

    This is the defining behaviour of the report vs the brief — Jeff wants the
    whole number, not a rounded headline.
    """
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.0f}"


def _hhi(weights_pct: list[float]) -> float:
    """Herfindahl-Hirschman index over position weights (given as percents).

    Returns the 0..1 form (Σ of squared weight fractions). 1.0 = one position;
    lower = more diversified. ~0.18 here = heavy concentration.
    """
    return sum((w / 100.0) ** 2 for w in weights_pct)


def _top_n_weight(weights_pct: list[float], n: int) -> float:
    """Sum of the n largest weights (percent)."""
    return sum(sorted(weights_pct, reverse=True)[:n])


def _grab(md: str, label: str) -> Optional[str]:
    """Pull the raw value after a '**Label**: <value>' line in MCP markdown."""
    if not md:
        return None
    m = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+)", md)
    return m.group(1).strip() if m else None


def _money_from(s: Optional[str]) -> Optional[float]:
    """Parse '$-908,603.44 CAD' / '+8.04%' leading number to float."""
    if not s:
        return None
    m = re.search(r"-?\$?-?([\d,]+(?:\.\d+)?)", s)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    return -val if s.lstrip().startswith(("-", "$-")) or "$-" in s else val


def _pct_from(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"([+-]?[\d.]+)%", s)
    return float(m.group(1)) if m else None


def _parse_margin(md: Optional[str]) -> dict:
    """Extract the granular margin distances the summary dict doesn't carry."""
    if not md:
        return {}
    return {
        "cushion_pct": _pct_from(_grab(md, "Cushion")),
        "maint_drawdown_pct": _pct_from(_grab(md, "Before Forced Liquidation (maint)")),
        "init_drawdown_pct": _pct_from(_grab(md, "Before Buying Power Restricted (initial)")),
        "excess_init": _money_from(_grab(md, "Excess (Initial)")),
        "excess_maint": _money_from(_grab(md, "Excess (Maintenance)")),
    }


def _parse_stress(md: Optional[str]) -> dict:
    """Extract verdict + buffer from the preflight stress markdown."""
    if not md:
        return {}
    verdict = ""
    vm = re.search(r"Verdict:\s*(.+)", md)
    if vm:
        verdict = vm.group(1).strip()
    return {
        "verdict": verdict,
        "buffer": _money_from(_grab(md, "Buffer")),
        "stressed_equity": _money_from(_grab(md, "Stressed Equity")),
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(data: ReportData) -> str:
    """Mobile-friendly detailed report, CAD, wrapped in a code block.

    Every line ≤ 32 chars (narrow mobile Discord code block). Sections:
    §1 header · §2 margin · §3 positions · §4 concentration · §5 stress.
    """
    if not data.healthy:
        return "⚠️ report unavailable — ibkr mcp not responding."

    now = datetime.now(_PACIFIC).strftime("%a %d %b · %H:%M PT")
    lines: list[str] = ["```", "📊 IBKR REPORT", now, ""]

    usdcad = data.fx_rates.get("USDCAD")
    if usdcad:
        lines.append(f"CAD · USDCAD {usdcad:.4f}")
        lines.append("")

    # §1 header — full numbers
    summary = data.summary or {}
    combined = summary.get("combined_nlv")
    if combined is not None:
        lines.append(f"NLV  {_money_full(combined)}")
        lines.append("")

    for acct in summary.get("accounts", []):
        acct_id = acct.get("account", "?")
        if acct.get("error") or acct.get("nlv") is None:
            lines.append(acct_id)
            lines.append(f"  ⚠️ {acct.get('error', 'no data')}")
            lines.append("")
            continue

        nlv = acct.get("nlv", 0)
        cushion = acct.get("cushion_pct", 0)
        pnl_tuple = bf._extract_daily_pnl(data.pnl_by_account.get(acct_id, ""))

        lines.append(acct_id)
        lines.append(f"  nlv   {_money_full(nlv)}")
        if pnl_tuple:
            d, p = pnl_tuple
            lines.append(f"  day   {_money_full(d)} ({bf._pct(p)})")
        lines.append(f"  cash  {_money_full(acct.get('cash', 0))}")
        lines.append(f"  bp    {_money_full(acct.get('buying_power', 0))}")
        lines.append(f"  gpv   {_money_full(acct.get('gpv', 0))}")
        lines.append(f"  lev   {acct.get('leverage', 0):.2f}x")
        lines.append(f"  util  {acct.get('margin_util_pct', 0):.0f}%")
        tag = "ok" if cushion >= 10 else "tight" if cushion >= 5 else "CRIT"
        lines.append(f"  cush  {cushion:.1f}% ({tag})")
        lines.append("")

    # §2 margin — granular distances from /api/margin
    m = _parse_margin(data.margin_md)
    if m and any(v is not None for v in m.values()):
        lines.append("⚖️ MARGIN")
        if m.get("excess_maint") is not None:
            lines.append(f"  excess liq")
            lines.append(f"  {_money_full(m['excess_maint'])}")
        if m.get("maint_drawdown_pct") is not None:
            lines.append(f"  dd to liq {m['maint_drawdown_pct']:.2f}%")
        if m.get("init_drawdown_pct") is not None:
            lines.append(f"  dd to bp  {m['init_drawdown_pct']:.2f}%")
        lines.append("")

    # §3 positions — full value + weight + unrealized
    if data.positions:
        rows = bf._combine_positions(data.positions, data.fx_rates)
        weights = _position_weights(data.positions)
        if rows:
            lines.append("📈 POSITIONS")
            for r in rows:
                label = r["label"][:6]
                w = weights.get(r["symbol"])
                wtxt = f"{w:.1f}%" if w is not None else "  -"
                lines.append(f"  {label:<6} {wtxt:>6}")
                mv = _money_full(r["market_value_cad"])
                pl = bf._pct(r["unrealized_pct"])
                lines.append(f"    {mv}  {pl}")
            lines.append("")

            # §4 concentration — local, no endpoint
            wl = [w for w in weights.values() if w is not None]
            if wl:
                lines.append("🎯 CONCENTRATION")
                lines.append(f"  top-3  {_top_n_weight(wl, 3):.1f}%")
                lines.append(f"  HHI    {_hhi(wl):.3f}")
                lines.append("")

    # §5 stress — fixed −10% preflight
    s = _parse_stress(data.stress_md)
    if s and (s.get("buffer") is not None or s.get("verdict")):
        lines.append(f"⚠️ STRESS −{STRESS_DRAWDOWN_PCT:.0f}%")
        if s.get("verdict"):
            lines.append(f"  {s['verdict'][:26]}")
        if s.get("buffer") is not None:
            lines.append(f"  buffer {_money_full(s['buffer'])}")
        lines.append("")

    lines.append("```")
    return "\n".join(lines)


def _position_weights(positions: dict) -> dict[str, Optional[float]]:
    """Map symbol -> weight_pct as provided by the MCP (already % of NLV)."""
    out: dict[str, Optional[float]] = {}
    for p in positions.get("positions", []):
        out[p["symbol"]] = p.get("weight_pct")
    return out
