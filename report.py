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
    dividends_md: Optional[str] = None  # /api/dividends markdown
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
        dividends_task = bf._fetch_json(session, f"{mcp_url}/api/dividends")
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

        (summary, fx_json, margin_json, stress_json, div_json) = await asyncio.gather(
            summary_task, fx_task, margin_task, stress_task, dividends_task
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

        if summary is None:
            errors.append("summary fetch failed")

        return ReportData(
            summary=summary,
            positions=positions,
            margin_md=(margin_json or {}).get("markdown") if margin_json else None,
            stress_md=(stress_json or {}).get("markdown") if stress_json else None,
            dividends_md=(div_json or {}).get("markdown") if div_json else None,
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


def _unrealized_cad(rows: list[dict], fx: dict[str, float]) -> float:
    """Sum unrealized P&L across position rows, converting each to CAD.

    The positions feed denominates unrealized_pnl in each position's OWN
    currency (USD rows in USD, CAD rows in CAD). Summing raw conflates
    currencies — the bug that made the total wander. Convert per-row.

    This is the AUTHORITATIVE unrealized figure: the positions feed is stable,
    unlike /api/account-pnl which returns a partial value on the first cold
    call after the gateway idles.
    """
    total = 0.0
    for r in rows:
        pnl = float(r.get("unrealized_pnl", 0) or 0)
        ccy = r.get("currency", "USD")
        total += bf._fx_to_cad(pnl, ccy, fx)
    return total


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


def _parse_account_pnl(md: Optional[str]) -> dict:
    """Pull unrealized + realized P&L from /api/account-pnl markdown.

    The markdown carries '**Unrealized P&L**: +$X CAD' and '**Realized P&L**'
    beyond the daily figure brief already parses.
    """
    if not md:
        return {}
    return {
        "unrealized": _money_from(_grab(md, "Unrealized P&L")),
        "realized": _money_from(_grab(md, "Realized P&L")),
    }


def _account_stale(md: Optional[str]) -> bool:
    """True if the account-pnl markdown carries the MCP's cached-data warning.

    When a gateway is offline the MCP serves last-known values and stamps
    '⚠️ CACHED DATA — IB Gateway is offline.' Surfacing this stops a stale
    snapshot being read as live.
    """
    if not md:
        return False
    return "CACHED DATA" in md or "Gateway is offline" in md


def _next_dividend(md: Optional[str]) -> Optional[str]:
    """First upcoming dividend line from the dividends table: 'SYM ex MM-DD'."""
    if not md:
        return None
    best: Optional[tuple[str, str, str]] = None  # (date, sym, amount)
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|") or "Symbol" in s or "---" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        sym, ex_date, amt = cells[0], cells[1], cells[2]
        if not re.match(r"\d{4}-\d{2}-\d{2}", ex_date):
            continue
        if best is None or ex_date < best[0]:
            best = (ex_date, sym, amt)
    if not best:
        return None
    date, sym, amt = best
    return f"{sym} {date[5:]} {amt}"  # strip year for width


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

def _account_rows(positions: Optional[dict], acct_id: str) -> list[dict]:
    """Position rows belonging to one account, sorted by market value desc.

    Values stay in each row's native currency; weight_pct is already % of that
    account's NLV (so it sums to ~100 within the account — no cross-account
    merge, which is what produced >100% concentration).
    """
    if not positions:
        return []
    rows = [r for r in positions.get("positions", []) if r.get("account") == acct_id]
    rows.sort(key=lambda r: abs(float(r.get("market_value", 0) or 0)), reverse=True)
    return rows


def build_report(data: ReportData) -> str:
    """Mobile-friendly detailed report, CAD, wrapped in a code block.

    Per-account sections so every number is attributable: combined NLV +
    combined unrealized up top, then each account gets its own header, margin
    snapshot, positions (its own weights/prices), and concentration. Columns
    align to a shared 30-col grid; every line ≤ 32 chars.

    Unrealized is summed from the STABLE positions feed (per-currency → CAD),
    not the /api/account-pnl markdown which flickers on cold calls.
    """
    if not data.healthy:
        return "⚠️ report unavailable — ibkr mcp not responding."

    now = datetime.now(_PACIFIC).strftime("%a %d %b · %H:%M PT")
    lines: list[str] = ["```", "📊 IBKR REPORT", now, ""]

    usdcad = data.fx_rates.get("USDCAD")
    if usdcad:
        lines.append(f"CAD · USDCAD {usdcad:.4f}")

    summary = data.summary or {}
    accounts = summary.get("accounts", [])

    # ---- Combined totals (the only cross-account aggregates) ----
    combined = summary.get("combined_nlv")
    all_rows = (data.positions or {}).get("positions", [])
    combined_unreal = _unrealized_cad(all_rows, data.fx_rates) if all_rows else None
    if combined is not None:
        lines.append(_kv("NLV", _money_full(combined)))
    if combined_unreal is not None:
        lines.append(_kv("uPnl", _money_full(combined_unreal)))
    lines.append("")

    # ---- Per-account sections ----
    for acct in accounts:
        acct_id = acct.get("account", "?")
        # short tag for the header (last 4 of the IB account id)
        short = acct_id[-4:] if len(acct_id) >= 4 else acct_id
        stale = _account_stale(data.pnl_by_account.get(acct_id, ""))
        head = f"━ {acct_id}" + (" ⚠️STALE" if stale else "")
        lines.append(head[:CONTENT_W])

        if acct.get("error") or acct.get("nlv") is None:
            lines.append(f"  ⚠️ {acct.get('error', 'no data')}"[:CONTENT_W])
            lines.append("")
            continue

        cushion = acct.get("cushion_pct", 0)
        pnl_tuple = bf._extract_daily_pnl(data.pnl_by_account.get(acct_id, ""))
        acct_rows = _account_rows(data.positions, acct_id)
        acct_unreal = _unrealized_cad(acct_rows, data.fx_rates) if acct_rows else None

        lines.append(_kv("  nlv", _money_full(acct.get("nlv", 0))))
        if pnl_tuple:
            d, p = pnl_tuple
            lines.append(_kv("  day", f"{_money_full(d)} {bf._pct(p)}"))
        if acct_unreal is not None:
            lines.append(_kv("  uPnl", _money_full(acct_unreal)))
        lines.append(_kv("  cash", _money_full(acct.get("cash", 0))))
        lines.append(_kv("  bp", _money_full(acct.get("buying_power", 0))))
        lines.append(_kv("  gpv", _money_full(acct.get("gpv", 0))))
        lines.append(_kv("  lev", f"{acct.get('leverage', 0):.2f}x"))
        lines.append(_kv("  util", f"{acct.get('margin_util_pct', 0):.0f}%"))
        excess = acct.get("excess_liquidity")
        if excess is not None:
            lines.append(_kv("  exliq", _money_full(excess)))
        tag = "ok" if cushion >= 10 else "tight" if cushion >= 5 else "CRIT"
        lines.append(_kv("  cush", f"{cushion:.1f}% ({tag})"))
        lines.append("")

        # positions for THIS account — price/value from the row itself
        # (the row carries the correct price for its OWN listing, incl. CDRs
        # like AVGO(C) which trade at a fraction of the US parent — never join
        # the US ticker quote by bare symbol, that was the price bug).
        if acct_rows:
            lines.append("  📈 positions")
            for r in acct_rows:
                ccy = r.get("currency", "USD")
                sym = r["symbol"] + ("(C)" if ccy != "USD" else "")
                label = sym[:8]   # 'AVGO(C)' = 7, NVDA(C) = 7; cap at 8
                w = r.get("weight_pct")
                wtxt = f"{w:.1f}%" if w is not None else "-"
                price = r.get("market_price")
                price_txt = f"${price:,.2f}" if price is not None else "—"
                right = f"{wtxt}  {price_txt}"
                # 2-space indent + label, value right-aligned so the line is
                # exactly CONTENT_W regardless of label/CDR length.
                inner = f"{label:<8}{right:>{CONTENT_W - 2 - 8}}"
                lines.append(f"  {inner}")
                mv_cad = bf._fx_to_cad(float(r.get("market_value", 0) or 0), ccy, data.fx_rates)
                up_cad = bf._fx_to_cad(float(r.get("unrealized_pnl", 0) or 0), ccy, data.fx_rates)
                lines.append(_kv("    mv", _money_full(mv_cad)))
                lines.append(_kv("    uP", _money_full(up_cad)))
            lines.append("")

            # concentration — within this account, weights sum to ~100
            wl = [r["weight_pct"] for r in acct_rows if r.get("weight_pct") is not None]
            if wl:
                lines.append(_kv("  top-3", f"{_top_n_weight(wl, 3):.1f}%"))
                lines.append(_kv("  HHI", f"{_hhi(wl):.3f}"))
                lines.append("")

    # ---- Stress (whole-book preflight) ----
    s = _parse_stress(data.stress_md)
    if s and (s.get("buffer") is not None or s.get("verdict")):
        lines.append(f"⚠️ STRESS −{STRESS_DRAWDOWN_PCT:.0f}% (primary)")
        if s.get("verdict"):
            lines.append(f"  {s['verdict'][:CONTENT_W - 2]}")
        if s.get("buffer") is not None:
            lines.append(_kv("  buffer", _money_full(s["buffer"])))
        lines.append("")

    # ---- Next dividend ----
    nxt = _next_dividend(data.dividends_md)
    if nxt:
        lines.append(_kv("next div", nxt))
        lines.append("")

    lines.append("```")
    return "\n".join(lines)
