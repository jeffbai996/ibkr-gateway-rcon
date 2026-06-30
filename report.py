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

_PACIFIC = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class ReportData:
    summary: Optional[dict]
    positions: Optional[dict]
    fx_rates: dict[str, float]
    healthy: bool
    margin_by_account: dict[str, str] = field(default_factory=dict)  # acct -> /api/margin md
    dividends_md: Optional[str] = None  # /api/dividends markdown
    day_pct: dict[str, float] = field(default_factory=dict)  # symbol -> day change %
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
            return ReportData(
                summary=None, positions=None, fx_rates={}, healthy=False,
                fetch_errors=errors,
            )

        accounts = health.get("accounts", [])
        primary = accounts[0] if accounts else None

        summary_task = bf._fetch_json(session, f"{mcp_url}/api/summary")
        fx_task = bf._fetch_json(session, f"{mcp_url}/api/prices?symbols=USDCAD=X")
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
        # Per-account margin so each section gets its own buffer/distances
        # (the old single primary-only fetch left the second account blank).
        margin_tasks = {
            acct: bf._fetch_json(session, f"{mcp_url}/api/margin?account={acct}")
            for acct in accounts
        }
        (summary, fx_json, div_json) = await asyncio.gather(
            summary_task, fx_task, dividends_task
        )
        pos_results = dict(zip(pos_tasks.keys(), await asyncio.gather(*pos_tasks.values())))
        pnl_results = dict(zip(pnl_tasks.keys(), await asyncio.gather(*pnl_tasks.values())))
        margin_results = dict(zip(margin_tasks.keys(), await asyncio.gather(*margin_tasks.values())))
        pnl_md = {a: (r or {}).get("markdown", "") for a, r in pnl_results.items()}
        margin_md_by_acct = {a: (r or {}).get("markdown", "") for a, r in margin_results.items()}

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

        # Day-change per held symbol. change_pct is a ratio (listing-agnostic),
        # so joining by bare symbol is safe — the CDR price trap only affected
        # absolute price, which we never take from here.
        day_pct: dict[str, float] = {}
        symbols = sorted({r["symbol"] for r in all_rows})
        if symbols:
            pr = await bf._fetch_json(
                session, f"{mcp_url}/api/prices?symbols={','.join(symbols)}"
            )
            if pr and isinstance(pr.get("prices"), dict):
                for sym, q in pr["prices"].items():
                    if isinstance(q, dict) and q.get("change_pct") is not None:
                        day_pct[sym] = float(q["change_pct"])

        if summary is None:
            errors.append("summary fetch failed")

        return ReportData(
            summary=summary,
            positions=positions,
            margin_by_account=margin_md_by_acct,
            dividends_md=(div_json or {}).get("markdown") if div_json else None,
            fx_rates=fx_rates,
            healthy=True,
            day_pct=day_pct,
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


def _money_compact(n: float) -> str:
    """Abbreviated dollars for the narrow MV column: 2111789 -> '2.1M',
    882349 -> '882k', 285 -> '285'. No $ sign (column header carries units)."""
    a = abs(n)
    sign = "-" if n < 0 else ""
    if a >= 1_000_000:
        return f"{sign}{a / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}{a / 1_000:.0f}k"
    return f"{sign}{a:.0f}"


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


def _dividend_rows(md: Optional[str]) -> list[tuple[str, str, str, str]]:
    """Parse the dividend calendar table into (symbol, ex_date, amount, next12m).

    Table columns: Symbol | Next Ex-Date | Amount/Share | Past 12M | Next 12M.
    Sorted by ex-date ascending (soonest first).
    """
    if not md:
        return []
    out: list[tuple[str, str, str, str]] = []
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|") or "Symbol" in s or "---" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        sym, ex_date, amt = cells[0], cells[1], cells[2]
        nxt = cells[4] if len(cells) >= 5 else ""
        if not re.match(r"\d{4}-\d{2}-\d{2}", ex_date):
            continue
        out.append((sym, ex_date, amt, nxt))
    out.sort(key=lambda r: r[1])
    return out


def _dividend_income_cad(
    divs: list[tuple[str, str, str, str]],
    positions: Optional[dict],
    fx: dict[str, float],
) -> Optional[float]:
    """Expected next-12-month dividend income in CAD.

    Σ over holdings of (next-12M per-share × shares), converted to CAD. The
    calendar's Next-12M column is per share; shares come from the positions
    feed. Returns None if nothing computable.
    """
    if not divs or not positions:
        return None
    shares_by_sym: dict[str, tuple[float, str]] = {}
    for p in positions.get("positions", []):
        shares_by_sym[p["symbol"]] = (
            float(p.get("shares") or 0), p.get("currency", "USD")
        )
    total = 0.0
    seen = False
    for sym, _ex, _amt, nxt in divs:
        m = re.search(r"-?[\d,]+(?:\.\d+)?", nxt or "")
        if not m or sym not in shares_by_sym:
            continue
        per_share = float(m.group(0).replace(",", ""))
        shares, ccy = shares_by_sym[sym]
        total += bf._fx_to_cad(per_share * shares, ccy, fx)
        seen = True
    return total if seen else None


def _dividend_block(data: ReportData) -> list[str]:
    """Shared dividend section: per-holding calendar + next-12M income total."""
    divs = _dividend_rows(data.dividends_md)
    if not divs:
        return []
    out = ["💵 DIVIDENDS (ex · /sh · 12M)"]
    for sym, ex, amt, fwd in divs:
        out.append(_kv(f"  {sym}", f"{ex[5:]} {amt} {fwd}"))
    income = _dividend_income_cad(divs, data.positions, data.fx_rates)
    if income is not None:
        out.append(_kv("  12M tot", _money_full(income)))
    return out


# ---------------------------------------------------------------------------
# Technicals — /gateway ta <symbol>
# ---------------------------------------------------------------------------

async def fetch_technicals(symbol: str, mcp_url: str = bf.MCP_DEFAULT_URL) -> Optional[str]:
    """Fetch /api/technicals markdown for one symbol. None on failure."""
    async with aiohttp.ClientSession() as session:
        j = await bf._fetch_json(session, f"{mcp_url}/api/technicals?symbol={symbol}")
    return (j or {}).get("markdown") if j else None


def parse_technicals(md: Optional[str]) -> dict:
    """Parse /api/technicals markdown into structured fields.

    Source shape (per the live endpoint):
      **Current Price**: $212.60 USD
      | SMA(20) | $214.63 | -0.95% | Below |   (table rows)
      **RSI**: 51.0 — Neutral
      **52W High**: $236.54 USD (date) / **52W Low**: ...
      **Percentile**: 76.9%
      **From 52W High**: -10.12% / **From 52W Low**: +59.95%
      **Vol Ratio (20d/60d)**: 1.14 — Stable
      **20-Day Realized Vol**: 40.8%
    """
    if not md:
        return {}
    out: dict = {"symbol": None, "price": None, "sma": {}, "rsi": None,
                 "rsi_label": None, "hi52": None, "lo52": None,
                 "pctile": None, "from_hi": None, "from_lo": None,
                 "vol_ratio": None, "vol_label": None, "rvol20": None}

    title = re.search(r"#\s*(\S+)\s+Technical", md)
    if title:
        out["symbol"] = title.group(1)
    out["price"] = _money_from(_grab(md, "Current Price"))

    # SMA table rows: | SMA(20) | $214.63 | -0.95% | Below |
    for m in re.finditer(r"SMA\((\d+)\)\s*\|\s*\$?([\d,.]+)\s*\|\s*([+-]?[\d.]+)%", md):
        out["sma"][int(m.group(1))] = (
            float(m.group(2).replace(",", "")), float(m.group(3))
        )

    rsi = re.search(r"\*\*RSI\*\*:\s*([\d.]+)\s*—\s*(\w+)", md)
    if rsi:
        out["rsi"] = float(rsi.group(1))
        out["rsi_label"] = rsi.group(2)

    out["hi52"] = _money_from(_grab(md, "52W High"))
    out["lo52"] = _money_from(_grab(md, "52W Low"))
    out["pctile"] = _pct_from(_grab(md, "Percentile"))
    out["from_hi"] = _pct_from(_grab(md, "From 52W High"))
    out["from_lo"] = _pct_from(_grab(md, "From 52W Low"))

    vr = re.search(r"Vol Ratio \(20d/60d\)\*\*:\s*([\d.]+)\s*—\s*(\w+)", md)
    if vr:
        out["vol_ratio"] = float(vr.group(1))
        out["vol_label"] = vr.group(2)
    out["rvol20"] = _pct_from(_grab(md, "20-Day Realized Vol"))
    return out


def build_technicals(symbol: str, md: Optional[str]) -> str:
    """Render a single-symbol technicals card, fenced, ≤39-col left-aligned."""
    if not md:
        return f"⚠️ no technicals for {symbol.upper()}."
    t = parse_technicals(md)
    sym = t.get("symbol") or symbol.upper()
    lines = [f"📊 {sym} · technicals"]
    if t.get("price") is not None:
        lines.append(_kv("  price", f"${t['price']:,.2f}"))
    if t.get("sma"):
        lines.append("")
        lines.append("  SMA (val · vs price)")
        for window in (20, 50, 200):
            if window in t["sma"]:
                val, pct = t["sma"][window]
                lines.append(_kv(f"    {window}", f"${val:,.2f} {pct:+.1f}%"))
    if t.get("rsi") is not None:
        lines.append("")
        lines.append(_kv("  RSI(14)", f"{t['rsi']:.0f} {t.get('rsi_label') or ''}".strip()))
    if t.get("hi52") is not None or t.get("lo52") is not None:
        lines.append("")
        lines.append("  52-week")
        if t.get("hi52") is not None:
            lines.append(_kv("    high", f"${t['hi52']:,.2f}"))
        if t.get("lo52") is not None:
            lines.append(_kv("    low", f"${t['lo52']:,.2f}"))
        if t.get("pctile") is not None:
            lines.append(_kv("    pctile", f"{t['pctile']:.0f}%"))
        if t.get("from_hi") is not None:
            lines.append(_kv("    from hi", f"{t['from_hi']:+.1f}%"))
        if t.get("from_lo") is not None:
            lines.append(_kv("    from lo", f"{t['from_lo']:+.1f}%"))
    if t.get("vol_ratio") is not None or t.get("rvol20") is not None:
        lines.append("")
        lines.append("  volatility")
        if t.get("vol_ratio") is not None:
            lines.append(_kv("    20/60", f"{t['vol_ratio']:.2f}x {t.get('vol_label') or ''}".strip()))
        if t.get("rvol20") is not None:
            lines.append(_kv("    rVol20", f"{t['rvol20']:.1f}%"))
    return "```\n" + "\n".join(lines) + "\n```"


# ---------------------------------------------------------------------------
# Column grid
# ---------------------------------------------------------------------------
#
# Everything aligns to ONE grid so columns line up across every section. The
# content width is 39 chars — the measured wrap ceiling on Jeff's phone.
# `_kv` left-aligns: label padded to LABEL_W, then the value starts at a fixed
# column and is LEFT-justified (Jeff prefers left-aligned numbers — they form
# a clean left edge under the label column rather than a ragged right edge).

CONTENT_W = 39
LABEL_W = 12


def _kv(label: str, value: str) -> str:
    """Label left-padded to LABEL_W, value left-aligned right after it.

    If the label is longer than LABEL_W (an indented sub-row like
    '    init req') it still gets one trailing space before the value so they
    never run together.
    """
    pad = max(LABEL_W, len(label) + 1)
    return f"{label:<{pad}}{value}"


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


def _account_card(data: ReportData, acct: dict) -> list[str]:
    """One account's full card: header, balances, margin, positions, conc."""
    lines: list[str] = []
    acct_id = acct.get("account", "?")
    stale = _account_stale(data.pnl_by_account.get(acct_id, ""))
    lines.append((f"━ {acct_id}" + (" ⚠️STALE" if stale else ""))[:CONTENT_W])

    if acct.get("error") or acct.get("nlv") is None:
        lines.append(f"  ⚠️ {acct.get('error', 'no data')}"[:CONTENT_W])
        lines.append("")
        return lines

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
    # Realized P&L (today) — parsed from /api/account-pnl markdown.
    realized = _parse_account_pnl(data.pnl_by_account.get(acct_id, "")).get("realized")
    if realized is not None:
        lines.append(_kv("  rPnl", _money_full(realized)))
    # Cost basis = Σ(shares × avg_cost), per-currency → CAD.
    cost_cad = sum(
        bf._fx_to_cad(float(r.get("shares") or 0) * float(r.get("avg_cost") or 0),
                      r.get("currency", "USD"), data.fx_rates)
        for r in acct_rows
    )
    if acct_rows:
        lines.append(_kv("  cost", _money_full(cost_cad)))
    lines.append(_kv("  cash", _money_full(acct.get("cash", 0))))
    lines.append(_kv("  bp", _money_full(acct.get("buying_power", 0))))
    lines.append(_kv("  gpv", _money_full(acct.get("gpv", 0))))
    lines.append(_kv("  lev", f"{acct.get('leverage', 0):.2f}x"))
    lines.append(_kv("  util", f"{acct.get('margin_util_pct', 0):.0f}%"))
    lines.append("")

    # margin block (per-account /api/margin)
    m = _parse_margin(data.margin_by_account.get(acct_id))
    init_m = acct.get("init_margin")
    maint_m = acct.get("maint_margin")
    if any(v is not None for v in (init_m, maint_m, *m.values())):
        lines.append("  ⚖️ margin")
        if init_m is not None:
            lines.append(_kv("    init req", _money_full(init_m)))
        if maint_m is not None:
            lines.append(_kv("    maint req", _money_full(maint_m)))
        if m.get("excess_maint") is not None:
            lines.append(_kv("    excess", _money_full(m["excess_maint"])))
        tag = "ok" if cushion >= 10 else "tight" if cushion >= 5 else "CRIT"
        lines.append(_kv("    cushion", f"{cushion:.1f}% ({tag})"))
        if m.get("maint_drawdown_pct") is not None:
            lines.append(_kv("    dd→liq", f"{m['maint_drawdown_pct']:.2f}%"))
        if m.get("init_drawdown_pct") is not None:
            lines.append(_kv("    dd→bp", f"{m['init_drawdown_pct']:.2f}%"))
        lines.append("")

    # positions for THIS account — price/value from the row itself (the row
    # carries the correct price for its OWN listing, incl. CDRs like AVGO(C)
    # which trade at a fraction of the US parent). Day % from /api/prices (a
    # ratio, listing-agnostic).
    if acct_rows:
        lines.append(f"  📈 positions · {acct_id}")
        # One row per holding, LEFT-aligned columns on a 39-char grid:
        #   SYM(7) WT(6) PRICE(8) DAY(7) MV(6) uP(5)
        # MV (market value, CAD) is abbreviated and DAY is the day-change % so
        # the whole blotter fits the mobile width without wrapping. CDR (CAD)
        # listings are suffixed "-C" and show their OWN price (not the US
        # parent's). Full $ MV + unrealized $ stay available via the account
        # totals (uPnl) up top.
        # Flush-left (no indent) so the full 39-col grid is available; the
        # 📈 header above already scopes the block to this account.
        lines.append(
            f"{'SYM':<7}{'WT':<6}{'PRICE':<8}{'DAY':<7}{'MV':<6}{'uP':<5}"
        )
        for r in acct_rows:
            ccy = r.get("currency", "USD")
            sym = (r["symbol"] + ("-C" if ccy != "USD" else ""))[:7]
            w = r.get("weight_pct")
            wt = f"{w:.1f}%" if w is not None else "-"
            price = r.get("market_price")
            px = f"{price:,.1f}" if price is not None else "—"
            dp = data.day_pct.get(r["symbol"])
            day = f"{dp:+.1f}%" if dp is not None else "—"
            mv_cad = bf._fx_to_cad(float(r.get("market_value", 0) or 0), ccy, data.fx_rates)
            up_native = float(r.get("unrealized_pnl", 0) or 0)
            cost = float(r.get("shares") or 0) * float(r.get("avg_cost") or 0)
            up_pct = (up_native / cost * 100) if cost else 0.0
            up = f"{up_pct:+.0f}%"
            lines.append(
                f"{sym:<7}{wt:<6}{px:<8}{day:<7}{_money_compact(mv_cad):<6}{up:<5}"
            )
        lines.append("")

        wl = [r["weight_pct"] for r in acct_rows if r.get("weight_pct") is not None]
        if wl:
            lines.append(_kv("  top-3", f"{_top_n_weight(wl, 3):.1f}%"))
            lines.append(_kv("  HHI", f"{_hhi(wl):.3f}"))
            lines.append("")

    return lines


def _resolve_accounts(data: ReportData, which: Optional[str]) -> list[dict]:
    """Map a 'primary'/'secondary'/'both'/None selector to account dicts.

    Accounts are ordered as the MCP returns them: index 0 = primary, 1 =
    secondary, matching the gateway naming. None/'both'/'all' → every account.
    """
    accts = (data.summary or {}).get("accounts", [])
    w = (which or "").strip().lower()
    if w in ("", "both", "all"):
        return accts
    if w in ("primary", "p", "1") and accts:
        return accts[:1]
    if w in ("secondary", "s", "2") and len(accts) >= 2:
        return accts[1:2]
    # also accept a literal account id
    by_id = [a for a in accts if a.get("account") == which]
    return by_id or accts


def _report_lines(data: ReportData) -> list[str]:
    """Build the report body as a list of content lines (no code fences).

    Per-account sections so every number is attributable: combined NLV +
    combined unrealized up top, then each account gets its own header, margin
    snapshot, positions (its own weights/prices), and concentration. Columns
    align to a shared 38-col grid.

    Unrealized is summed from the STABLE positions feed (per-currency → CAD),
    not the /api/account-pnl markdown which flickers on cold calls.
    """
    now = datetime.now(_PACIFIC).strftime("%a %d %b · %H:%M PT")
    lines: list[str] = ["📊 IBKR REPORT", now, ""]

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
        lines.extend(_account_card(data, acct))

    # ---- Whole-book liquidation distance (exact, from maint margin) ----
    lines.extend(_liquidation_distance(data, accounts))

    # ---- Dividend calendar + next-12M income total ----
    div_lines = _dividend_block(data)
    if div_lines:
        lines.extend(div_lines)
        lines.append("")

    # trailing blank is noise; drop it
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _liquidation_distance(data: "ReportData", accounts: list[dict]) -> list[str]:
    """Whole-book risk line built from EXACT margin distances, not an estimate.

    Replaces the old preflight "stress" buffer (which mis-modeled the shock:
    it applied a flat drawdown to uncorrelated ballast like GLD and stacked a
    vol-adjustment on the proportional margin scale, inflating the number).

    `dd→liq` (maint_drawdown_pct) comes straight from IBKR's own maintenance
    margin — it's the precise % the book can fall before forced liquidation.
    We surface the TIGHTEST account's distance as the headline whole-book risk,
    since that account liquidates first.
    """
    dists: list[tuple[str, float]] = []
    for acct in accounts:
        acct_id = acct.get("account", "?")
        m = _parse_margin(data.margin_by_account.get(acct_id))
        ddl = m.get("maint_drawdown_pct")
        if ddl is not None:
            dists.append((acct_id, ddl))
    if not dists:
        return []
    acct_id, tightest = min(dists, key=lambda t: t[1])
    out = ["🛡️ LIQUIDATION DISTANCE"]
    # "first forced liquidation at −X%" — the real, exact stress answer.
    tag = "ok" if tightest >= 15 else "tight" if tightest >= 8 else "CRIT"
    label = f"  −{tightest:.2f}% ({tag})"
    if len(dists) > 1:
        label += f"  ← {acct_id}"   # which account hits first
    out.append(label)
    out.append("")
    return out


def build_report(data: ReportData) -> str:
    """Full report as a single fenced code block (used by tests / short books)."""
    if not data.healthy:
        return "⚠️ report unavailable — ibkr mcp not responding."
    return "```\n" + "\n".join(_report_lines(data)) + "\n```"


def _fence(lines: list[str]) -> str:
    """Wrap content lines in a code block, trimming trailing blanks."""
    while lines and lines[-1] == "":
        lines.pop()
    return "```\n" + "\n".join(lines) + "\n```"


def build_report_messages(data: ReportData, which: Optional[str] = None) -> list[str]:
    """One Discord message per account card, selected by `which`.

    `which`: 'primary' / 'secondary' / a U-account id → that one account's
    card. None / 'both' / 'all' → one message per account (so each fits under
    Discord's 2000-char cap and is independently aligned).

    The combined header (NLV + combined unrealized + FX) rides on the first
    message; the whole-book stress + dividend calendar ride on the last.
    """
    if not data.healthy:
        return ["⚠️ report unavailable — ibkr mcp not responding."]

    accts = _resolve_accounts(data, which)
    if not accts:
        return ["⚠️ no matching account."]

    now = datetime.now(_PACIFIC).strftime("%a %d %b · %H:%M PT")
    usdcad = data.fx_rates.get("USDCAD")

    # Header block — only show combined totals when rendering the full book.
    header: list[str] = ["📊 IBKR REPORT", now, ""]
    if usdcad:
        header.append(f"CAD · USDCAD {usdcad:.4f}")
    showing_all = which is None or which.strip().lower() in ("", "both", "all")
    if showing_all:
        summary = data.summary or {}
        combined = summary.get("combined_nlv")
        all_rows = (data.positions or {}).get("positions", [])
        if combined is not None:
            header.append(_kv("NLV", _money_full(combined)))
        if all_rows:
            header.append(_kv("uPnl", _money_full(_unrealized_cad(all_rows, data.fx_rates))))

    # Trailing block — liquidation distance + dividends (whole-book context).
    trailer: list[str] = []
    trailer.extend(_liquidation_distance(data, accts))
    trailer.extend(_dividend_block(data))

    messages: list[str] = []
    for i, acct in enumerate(accts):
        block: list[str] = []
        if i == 0:
            block += header + [""]
        block += _account_card(data, acct)
        if i == len(accts) - 1 and trailer:
            block += [""] + trailer
        messages.append(_fence(block))
    return messages
