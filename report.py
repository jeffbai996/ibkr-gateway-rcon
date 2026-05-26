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
    prices: dict[str, dict] = field(default_factory=dict)  # symbol -> day-change
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
        fx_task = bf._fetch_json(session, f"{mcp_url}/api/prices?symbols=USDCAD=X")
        margin_task = bf._fetch_json(
            session,
            f"{mcp_url}/api/margin" + (f"?account={primary}" if primary else ""),
        )
        stress_task = bf._fetch_json(
            session, f"{mcp_url}/api/stress?drawdown_pct={STRESS_DRAWDOWN_PCT}"
        )
        # /api/positions with no param only returns the PRIMARY account. Fetch
        # each account explicitly and concatenate, so a multi-account book shows
        # every holding (the primary-only default silently dropped the rest).
        pos_tasks = {
            acct: bf._fetch_json(session, f"{mcp_url}/api/positions?account={acct}")
            for acct in accounts
        }
        pnl_tasks = {
            acct: bf._fetch_json(session, f"{mcp_url}/api/account-pnl?account={acct}")
            for acct in accounts
        }

        (summary, fx_json, margin_json, stress_json) = await asyncio.gather(
            summary_task, fx_task, margin_task, stress_task
        )
        pos_results = dict(zip(pos_tasks.keys(), await asyncio.gather(*pos_tasks.values())))
        pnl_results = dict(zip(pnl_tasks.keys(), await asyncio.gather(*pnl_tasks.values())))
        pnl_md = {a: (r or {}).get("markdown", "") for a, r in pnl_results.items()}

        # Concatenate every account's positions into one dict shaped like the
        # single-account response, so downstream code (and brief helpers) are
        # account-agnostic.
        all_rows: list[dict] = []
        for acct, res in pos_results.items():
            if res and isinstance(res.get("positions"), list):
                all_rows.extend(res["positions"])
        positions = {"positions": all_rows, "merged": []} if all_rows else None
        if not all_rows:
            errors.append("positions fetch failed")

        fx_rates: dict[str, float] = {}
        if fx_json and "prices" in fx_json:
            usdcad = fx_json["prices"].get("USDCAD=X", {})
            if "price" in usdcad:
                fx_rates["USDCAD"] = float(usdcad["price"])

        # Pull live day-change for every held symbol in one batch. Positions
        # rows carry market_price but no day move; /api/prices has
        # change/change_pct/previous_close per symbol.
        prices: dict[str, dict] = {}
        symbols = sorted({r["symbol"] for r in all_rows})
        if symbols:
            pr_json = await bf._fetch_json(
                session, f"{mcp_url}/api/prices?symbols={','.join(symbols)}"
            )
            if pr_json and isinstance(pr_json.get("prices"), dict):
                prices = pr_json["prices"]

        if summary is None:
            errors.append("summary fetch failed")

        return ReportData(
            summary=summary,
            positions=positions,
            prices=prices,
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
# Column grid
# ---------------------------------------------------------------------------
#
# Everything aligns to ONE grid so columns line up across every section. The
# content width is 30 chars (fits a 32-char mobile code block with margin).
# `_kv` renders a left label padded to LABEL_W, then a value right-aligned to
# the remaining width, so all values share a right edge.

CONTENT_W = 30
LABEL_W = 8


def _kv(label: str, value: str) -> str:
    """label left-padded to LABEL_W, value right-aligned to fill CONTENT_W."""
    value_w = CONTENT_W - LABEL_W
    return f"{label:<{LABEL_W}}{value:>{value_w}}"


def _arrow(n: float) -> str:
    return "▲" if n > 0 else "▼" if n < 0 else "―"


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(data: ReportData) -> str:
    """Mobile-friendly detailed report, CAD, wrapped in a code block.

    Every line ≤ 32 chars (narrow mobile Discord code block), columns aligned
    to a shared grid throughout. Sections: §1 header · §2 margin · §3 positions
    (current price, day $/%, mkt value, unrealized) · §4 concentration ·
    §5 stress. Positions merge across accounts by symbol.
    """
    if not data.healthy:
        return "⚠️ report unavailable — ibkr mcp not responding."

    now = datetime.now(_PACIFIC).strftime("%a %d %b · %H:%M PT")
    lines: list[str] = ["```", "📊 IBKR REPORT", now, ""]

    usdcad = data.fx_rates.get("USDCAD")
    if usdcad:
        lines.append(f"CAD · USDCAD {usdcad:.4f}")
        lines.append("")

    # §1 header — combined + per-account, full numbers, aligned grid
    summary = data.summary or {}
    combined = summary.get("combined_nlv")
    if combined is not None:
        lines.append(_kv("NLV", _money_full(combined)))
        lines.append("")

    for acct in summary.get("accounts", []):
        acct_id = acct.get("account", "?")
        if acct.get("error") or acct.get("nlv") is None:
            lines.append(acct_id)
            lines.append(f"  ⚠️ {acct.get('error', 'no data')}"[:CONTENT_W])
            lines.append("")
            continue

        cushion = acct.get("cushion_pct", 0)
        pnl_tuple = bf._extract_daily_pnl(data.pnl_by_account.get(acct_id, ""))

        lines.append(acct_id)
        lines.append(_kv("  nlv", _money_full(acct.get("nlv", 0))))
        if pnl_tuple:
            d, p = pnl_tuple
            lines.append(_kv("  day", f"{_money_full(d)} {bf._pct(p)}"))
        lines.append(_kv("  cash", _money_full(acct.get("cash", 0))))
        lines.append(_kv("  bp", _money_full(acct.get("buying_power", 0))))
        lines.append(_kv("  gpv", _money_full(acct.get("gpv", 0))))
        lines.append(_kv("  lev", f"{acct.get('leverage', 0):.2f}x"))
        lines.append(_kv("  util", f"{acct.get('margin_util_pct', 0):.0f}%"))
        tag = "ok" if cushion >= 10 else "tight" if cushion >= 5 else "CRIT"
        lines.append(_kv("  cush", f"{cushion:.1f}% ({tag})"))
        lines.append("")

    # §2 margin — granular distances from /api/margin, aligned
    m = _parse_margin(data.margin_md)
    if m and any(v is not None for v in m.values()):
        lines.append("⚖️ MARGIN")
        if m.get("excess_maint") is not None:
            lines.append(_kv("  exliq", _money_full(m["excess_maint"])))
        if m.get("maint_drawdown_pct") is not None:
            lines.append(_kv("  dd liq", f"{m['maint_drawdown_pct']:.2f}%"))
        if m.get("init_drawdown_pct") is not None:
            lines.append(_kv("  dd bp", f"{m['init_drawdown_pct']:.2f}%"))
        lines.append("")

    # §3 positions — merged across accounts, with live day-change.
    # 3-line card per holding, columns aligned to the grid:
    #   SYM            weight%   price
    #    day  +$x.xx (+x.xx%)
    #    mv   $x,xxx   uP +x.x%
    if data.positions:
        rows = bf._combine_positions(data.positions, data.fx_rates)
        weights = _position_weights(data.positions)
        if rows:
            lines.append("📈 POSITIONS")
            for r in rows:
                sym = r["symbol"]
                label = r["label"][:7]
                w = weights.get(sym)
                wtxt = f"{w:.1f}%" if w is not None else "-"
                px = data.prices.get(sym, {})
                price = px.get("price")
                price_txt = f"${price:,.2f}" if price is not None else "—"
                # line 1: SYM (label) ... weight ... current price.
                # label left, the rest right-aligned to the shared 30-col edge.
                right = f"{wtxt}  {price_txt}"
                lines.append(f"{label:<8}{right:>{CONTENT_W - 8}}")
                # line 2: day move from /api/prices (quote ccy)
                chg = px.get("change")
                chg_pct = px.get("change_pct")
                if chg is not None and chg_pct is not None:
                    day = f"{_arrow(chg)}${abs(chg):,.2f} {chg_pct:+.2f}%"
                else:
                    day = "n/a"
                lines.append(_kv("  day", day))
                # line 3: market value (CAD)
                lines.append(_kv("  mv", _money_full(r["market_value_cad"])))
                # line 4: unrealized %
                lines.append(_kv("  uPnl", bf._pct(r["unrealized_pct"])))
            lines.append("")

            # §4 concentration — local, no endpoint
            wl = [w for w in weights.values() if w is not None]
            if wl:
                lines.append("🎯 CONCENTRATION")
                lines.append(_kv("  top-3", f"{_top_n_weight(wl, 3):.1f}%"))
                lines.append(_kv("  HHI", f"{_hhi(wl):.3f}"))
                lines.append("")

    # §5 stress — fixed −10% preflight
    s = _parse_stress(data.stress_md)
    if s and (s.get("buffer") is not None or s.get("verdict")):
        lines.append(f"⚠️ STRESS −{STRESS_DRAWDOWN_PCT:.0f}%")
        if s.get("verdict"):
            lines.append(f"  {s['verdict'][:CONTENT_W - 2]}")
        if s.get("buffer") is not None:
            lines.append(_kv("  buffer", _money_full(s["buffer"])))
        lines.append("")

    lines.append("```")
    return "\n".join(lines)


def _position_weights(positions: dict) -> dict[str, Optional[float]]:
    """Map symbol -> summed weight_pct across accounts (already % of NLV).

    With multi-account merge, the same symbol can appear in two accounts; sum
    their weights so the merged view reflects total portfolio exposure.
    """
    out: dict[str, Optional[float]] = {}
    for p in positions.get("positions", []):
        sym = p["symbol"]
        w = p.get("weight_pct")
        if w is None:
            out.setdefault(sym, None)
            continue
        out[sym] = (out.get(sym) or 0.0) + w
    return out
