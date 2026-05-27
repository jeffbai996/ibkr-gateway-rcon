"""Tests for report.py — the detailed /gateway report formatter.

The report is the granular, full-precision cousin of /gateway brief: full
NLV numbers (no M/k abbreviation), rich margin block, per-position weight,
local concentration (top-N + HHI), and a fixed semis-10% stress line.

Fixtures use fake tickers and round numbers per the no-real-data rule.
Field names mirror the live IBKR MCP /api/summary + /api/positions shapes.
"""
import report as rp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _summary():
    """Mirror /api/summary: combined_nlv + per-account rich margin fields."""
    return {
        "combined_nlv": 1284300.0,
        "currency": "CAD",
        "accounts": [
            {
                "account": "U1234567",
                "currency": "CAD",
                "nlv": 1284300.0,
                "gpv": 1875000.0,
                "cash": -402100.0,
                "buying_power": 61200.0,
                "init_margin": 690000.0,
                "maint_margin": 591000.0,
                "excess_liquidity": 48300.0,
                "full_excess_liquidity": 48300.0,
                "cushion_pct": 8.04,
                "leverage": 1.46,
                "margin_util_pct": 46.0,
            }
        ],
    }


def _positions():
    """Mirror /api/positions: per-lot rows with weight_pct already provided."""
    return {
        "positions": [
            {"symbol": "AAPL", "sec_type": "STK", "shares": 1200,
             "avg_cost": 180.0, "market_price": 260.0, "market_value": 312000.0,
             "unrealized_pnl": 96000.0, "currency": "USD",
             "weight_pct": 24.3, "account": "U1234567"},
            {"symbol": "MSFT", "sec_type": "STK", "shares": 800,
             "avg_cost": 300.0, "market_price": 335.0, "market_value": 268000.0,
             "unrealized_pnl": -1100.0, "currency": "USD",
             "weight_pct": 20.9, "account": "U1234567"},
            {"symbol": "GOOGL", "sec_type": "STK", "shares": 1500,
             "avg_cost": 120.0, "market_price": 134.0, "market_value": 201000.0,
             "unrealized_pnl": 21000.0, "currency": "USD",
             "weight_pct": 15.7, "account": "U1234567"},
            {"symbol": "SGOV", "sec_type": "STK", "shares": 2900,
             "avg_cost": 100.0, "market_price": 100.0, "market_value": 290000.0,
             "unrealized_pnl": 0.0, "currency": "USD",
             "weight_pct": 22.6, "account": "U1234567"},
        ],
        "merged": [],
    }


_MARGIN_MD = (
    "# Margin Summary: U1234567\n\n"
    "**Equity**: $1,284,300.00 CAD\n"
    "**Initial Margin Req**: $690,000.00 CAD\n"
    "**Maintenance Margin Req**: $591,000.00 CAD\n\n"
    "## Distances\n"
    "**Above Initial Margin**: $594,300.00 CAD\n"
    "**Above Maintenance Margin**: $693,300.00 CAD\n"
    "**Cushion**: +8.04%\n\n"
    "## Max Drawdown Before Trouble\n"
    "**Before Buying Power Restricted (initial)**: -1.25%\n"
    "**Before Forced Liquidation (maint)**: +7.90%\n\n"
    "## Excess Liquidity\n"
    "**Excess (Initial)**: $48,300.00 CAD\n"
    "**Excess (Maintenance)**: $48,300.00 CAD\n"
)

_STRESS_MD = (
    "# Preflight Check: 10.0% Drawdown\nAccount: U1234567\n\n"
    "## Verdict: \U0001f7e2 OK\n\n"
    "**Current Equity**: $1,284,300.00 CAD\n"
    "**Stressed Equity** (after 10.0% drop): $1,155,870.00 CAD\n"
    "**Buffer**: $74,500.00 CAD\n"
)


_PNL_MD = (
    "# Account P&L: U1234567\n\n"
    "**Daily P&L**: +$12,500.00 CAD (+0.97% of NLV)\n"
    "**Unrealized P&L**: +$116,000.00 CAD\n"
    "**Realized P&L**: +$4,200.00 CAD\n"
)

_DIVIDENDS_MD = (
    "# Dividend Calendar: U1234567\n\n"
    "| Symbol | Next Ex-Date | Amount/Share | Past 12M | Next 12M |\n"
    "|--------|-------------|-------------|----------|----------|\n"
    "| MSFT | 2026-07-13 | $0.75 | $2.90 | $3.10 |\n"
    "| AAPL | 2026-06-04 | $0.25 | $0.96 | $1.04 |\n"
)


def _data(healthy=True, summary=None, positions=None,
          margin_md=_MARGIN_MD, stress_md=_STRESS_MD, fx_rates=None,
          pnl_by_account=None, dividends_md=_DIVIDENDS_MD, day_pct=None):
    return rp.ReportData(
        summary=summary if summary is not None else _summary(),
        positions=positions if positions is not None else _positions(),
        margin_by_account={"U1234567": margin_md} if margin_md else {},
        stress_md=stress_md,
        dividends_md=dividends_md,
        fx_rates=fx_rates if fx_rates is not None else {"USDCAD": 1.37},
        healthy=healthy,
        day_pct=day_pct if day_pct is not None
        else {"AAPL": 1.20, "MSFT": -0.33, "GOOGL": 2.13, "SGOV": 0.0},
        pnl_by_account=pnl_by_account if pnl_by_account is not None
        else {"U1234567": _PNL_MD},
        fetch_errors=[],
    )


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def test_money_full_no_abbreviation():
    # The whole point of this command: full grouped numbers, not $1.28M.
    assert rp._money_full(1284300) == "$1,284,300"
    assert rp._money_full(-402100) == "-$402,100"
    assert rp._money_full(0) == "$0"
    assert "M" not in rp._money_full(1284300)
    assert "k" not in rp._money_full(61200)


# ---------------------------------------------------------------------------
# Sections present
# ---------------------------------------------------------------------------

def test_report_has_all_built_sections():
    out = rp.build_report(_data())
    assert "1,284,300" in out          # combined NLV up top
    assert "uPnl" in out               # combined unrealized
    assert "U1234567" in out           # per-account section header
    assert "AAPL" in out               # positions
    assert "HHI" in out.upper()        # concentration
    assert "LIQUIDATION DISTANCE" in out.upper()  # whole-book risk (replaced bogus stress)


def test_report_positions_show_weight_and_compact_value():
    # Blotter row: weight_pct + abbreviated market value (MV column).
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}))
    assert "24.3" in out               # weight_pct (account-relative)
    assert "312k" in out               # market value, compact ($312,000 → 312k)


def test_report_positions_header_tagged_with_account():
    """Positions sub-header must name its account so it's unambiguous whose
    positions these are (Jeff: 'not obvious whose positions are whose')."""
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}))
    assert "📈 positions · U1234567" in out


def test_report_positions_price_from_row_not_join():
    # Price comes from the position ROW's market_price (1 decimal), never a
    # ticker join. AAPL row price 260.0 → '260.0' in the PRICE column.
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}))
    assert "260.0" in out


def test_report_cdr_uses_own_listing_price():
    # A CAD (CDR-style) line trades at a fraction of the US parent and must
    # show its OWN market_price, suffixed "-C" — not the US ticker quote.
    pos = {
        "positions": [
            {"symbol": "NVDA", "sec_type": "STK", "shares": 100,
             "avg_cost": 200.0, "market_price": 215.33, "market_value": 21533.0,
             "unrealized_pnl": 1533.0, "currency": "USD",
             "weight_pct": 50.0, "account": "U1234567"},
            {"symbol": "NVDA", "sec_type": "STK", "shares": 5000,
             "avg_cost": 40.0, "market_price": 49.11, "market_value": 245550.0,
             "unrealized_pnl": 45550.0, "currency": "CAD",
             "weight_pct": 50.0, "account": "U1234567"},
        ],
        "merged": [],
    }
    out = rp.build_report(_data(positions=pos, fx_rates={"USDCAD": 1.0}))
    assert "215.3" in out              # US parent price (1 dp)
    assert "49.1" in out               # CDR's OWN price, NOT the parent's
    assert "NVDA-C" in out             # CDR suffixed -C


def test_report_columns_aligned():
    # All _kv rows share CONTENT_W — values right-aligned to a common edge.
    out = rp.build_report(_data())
    kv_lines = [l for l in out.split("\n")
                if l.startswith(("  nlv", "  cash", "  bp", "  gpv",
                                 "  lev", "  util", "  cush"))]
    assert kv_lines, "expected aligned account rows"
    widths = {len(l) for l in kv_lines}
    assert widths == {rp.CONTENT_W}, f"misaligned widths: {widths}"


def test_unrealized_cad_sums_per_currency():
    # The core fix: unrealized must convert each row by its OWN currency.
    rows = [
        {"unrealized_pnl": 1000.0, "currency": "USD"},
        {"unrealized_pnl": 500.0, "currency": "CAD"},
    ]
    fx = {"USDCAD": 1.40}
    # 1000 USD * 1.40 + 500 CAD = 1400 + 500 = 1900
    assert abs(rp._unrealized_cad(rows, fx) - 1900.0) < 0.01


def test_report_per_account_no_cross_account_weight_blowup():
    # Two accounts each holding AAPL at 24.3% must NOT merge into 48.6% —
    # concentration is computed within each account, so top-3 stays ≤100.
    pos = _positions()
    pos["positions"].append(
        {"symbol": "AAPL", "sec_type": "STK", "shares": 300,
         "avg_cost": 180.0, "market_price": 260.0, "market_value": 78000.0,
         "unrealized_pnl": 24000.0, "currency": "USD",
         "weight_pct": 60.0, "account": "U7654321"},
    )
    summ = _summary()
    summ["accounts"].append({
        "account": "U7654321", "currency": "CAD", "nlv": 130000.0,
        "gpv": 130000.0, "cash": 0.0, "buying_power": 50000.0,
        "init_margin": 0.0, "maint_margin": 0.0, "excess_liquidity": 50000.0,
        "full_excess_liquidity": 50000.0, "cushion_pct": 40.0,
        "leverage": 1.0, "margin_util_pct": 0.0,
    })
    out = rp.build_report(_data(positions=pos, summary=summ,
                                pnl_by_account={"U1234567": _PNL_MD,
                                                "U7654321": _PNL_MD},
                                fx_rates={"USDCAD": 1.0}))
    # Both account headers present
    assert "U1234567" in out and "U7654321" in out
    # No concentration figure exceeds 100% (the old merge bug gave 147%)
    import re
    for m in re.finditer(r"top-3\s+([\d.]+)%", out):
        assert float(m.group(1)) <= 100.0, f"weight blowup: {m.group(1)}%"


# ---------------------------------------------------------------------------
# Mobile width — the hard rule
# ---------------------------------------------------------------------------

def test_report_mobile_width():
    out = rp.build_report(_data())
    for line in out.split("\n"):
        assert len(line) <= 39, f"line too wide ({len(line)}): {line!r}"


# ---------------------------------------------------------------------------
# Concentration math
# ---------------------------------------------------------------------------

def test_concentration_hhi_and_topn():
    # HHI = sum of squared weight fractions. top-3 = sum of 3 largest weights.
    weights = [24.3, 20.9, 15.7, 22.6]
    hhi = rp._hhi(weights)
    # Σ (w/100)^2 = .243^2+.209^2+.157^2+.226^2 ≈ 0.0590+0.0437+0.0246+0.0511
    assert abs(hhi - 0.1784) < 0.001
    assert abs(rp._top_n_weight(weights, 3) - 67.8) < 0.01  # 24.3+22.6+20.9


# ---------------------------------------------------------------------------
# Margin parsing
# ---------------------------------------------------------------------------

def test_margin_parse_pulls_key_distances():
    m = rp._parse_margin(_MARGIN_MD)
    assert m["cushion_pct"] == 8.04
    assert m["maint_drawdown_pct"] == 7.90  # before forced liquidation
    assert m["excess_init"] == 48300.0


def test_stress_parse_pulls_buffer():
    s = rp._parse_stress(_STRESS_MD)
    assert s["buffer"] == 74500.0
    assert "OK" in s["verdict"]


# ---------------------------------------------------------------------------
# Degraded / safety
# ---------------------------------------------------------------------------

def test_report_unhealthy_no_fabrication():
    out = rp.build_report(_data(healthy=False))
    assert "not responding" in out.lower() or "unavailable" in out.lower()
    # Must not invent numbers
    assert "1,284,300" not in out


def test_report_negative_cushion_flags_crit():
    s = _summary()
    s["accounts"][0]["cushion_pct"] = -2.0
    out = rp.build_report(_data(summary=s))
    assert "CRIT" in out.upper()


def test_report_missing_margin_md_degrades_gracefully():
    # If /api/margin failed, the report still renders §1/§3/§4, no crash.
    out = rp.build_report(_data(margin_md=None))
    assert "1,284,300" in out
    assert "AAPL" in out


# ---------------------------------------------------------------------------
# Bottom blocks: combined P&L + next dividend
# ---------------------------------------------------------------------------

def test_account_pnl_parse():
    p = rp._parse_account_pnl(_PNL_MD)
    assert p["unrealized"] == 116000.0
    assert p["realized"] == 4200.0


def test_account_stale_detects_cached_warning():
    cached = _PNL_MD + "\n⚠️ **CACHED DATA** — IB Gateway is offline."
    assert rp._account_stale(cached) is True
    assert rp._account_stale(_PNL_MD) is False
    assert rp._account_stale("") is False


def test_report_flags_stale_account():
    cached = _PNL_MD + "\n⚠️ **CACHED DATA** — IB Gateway is offline."
    out = rp.build_report(_data(pnl_by_account={"U1234567": cached}))
    assert "STALE" in out.upper()


def test_combined_unrealized_uses_positions_feed():
    # The fix: combined unrealized comes from summing the (stable) positions
    # feed per-currency, NOT the flickering account-pnl markdown. With USDCAD=1
    # the fixture's USD unrealized sums to 96000-1100+21000+0 = 115900.
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}))
    assert "115,900" in out


def test_dividend_rows_sorted_by_ex_date():
    rows = rp._dividend_rows(_DIVIDENDS_MD)
    # AAPL ex 06-04 is earlier than MSFT 07-13 → AAPL first.
    assert rows[0][0] == "AAPL"
    assert rows[0][1] == "2026-06-04"
    assert rows[1][0] == "MSFT"


def test_report_shows_dividend_calendar():
    out = rp.build_report(_data())
    assert "DIVIDENDS" in out
    assert "AAPL" in out
    assert "MSFT" in out


def test_report_still_mobile_width_with_extras():
    # The new bottom blocks + spacing must not break the 32-char rule.
    out = rp.build_report(_data())
    for line in out.split("\n"):
        assert len(line) <= 39, f"line too wide ({len(line)}): {line!r}"


# ---------------------------------------------------------------------------
# Per-account message selection
# ---------------------------------------------------------------------------

def _two_account_data():
    summ = _summary()
    summ["combined_nlv"] = 1414300.0
    summ["accounts"].append({
        "account": "U7654321", "currency": "CAD", "nlv": 130000.0,
        "gpv": 130000.0, "cash": 0.0, "buying_power": 50000.0,
        "init_margin": 0.0, "maint_margin": 0.0, "excess_liquidity": 50000.0,
        "full_excess_liquidity": 50000.0, "cushion_pct": 40.0,
        "leverage": 1.0, "margin_util_pct": 0.0,
    })
    pos = _positions()
    pos["positions"].append(
        {"symbol": "AAPL", "sec_type": "STK", "shares": 300, "avg_cost": 180.0,
         "market_price": 260.0, "market_value": 78000.0, "unrealized_pnl": 24000.0,
         "currency": "USD", "weight_pct": 60.0, "account": "U7654321"})
    return _data(summary=summ, positions=pos, fx_rates={"USDCAD": 1.0},
                 pnl_by_account={"U1234567": _PNL_MD, "U7654321": _PNL_MD})


def test_messages_both_emits_one_per_account():
    msgs = rp.build_report_messages(_two_account_data(), which="both")
    assert len(msgs) == 2
    assert "U1234567" in msgs[0]
    assert "U7654321" in msgs[1]
    # combined totals only on the first message
    assert "NLV" in msgs[0] and "NLV" not in msgs[1]


def test_messages_primary_only():
    msgs = rp.build_report_messages(_two_account_data(), which="primary")
    assert len(msgs) == 1
    assert "U1234567" in msgs[0]
    assert "U7654321" not in msgs[0]


def test_messages_secondary_only():
    msgs = rp.build_report_messages(_two_account_data(), which="secondary")
    assert len(msgs) == 1
    assert "U7654321" in msgs[0]
    assert "U1234567" not in msgs[0]


def test_messages_each_under_discord_cap():
    for which in ("both", "primary", "secondary"):
        for m in rp.build_report_messages(_two_account_data(), which=which):
            assert len(m) <= 2000, f"{which} message over cap: {len(m)}"


def test_messages_default_is_both():
    assert len(rp.build_report_messages(_two_account_data())) == 2


def test_messages_each_line_within_width():
    for m in rp.build_report_messages(_two_account_data(), which="both"):
        for line in m.split("\n"):
            assert len(line) <= 39, f"line too wide ({len(line)}): {line!r}"
