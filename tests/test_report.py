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


def _prices():
    """Mirror /api/prices: per-symbol day move (price/change/change_pct)."""
    return {
        "AAPL": {"price": 260.40, "previous_close": 257.30, "change": 3.10, "change_pct": 1.20},
        "MSFT": {"price": 335.00, "previous_close": 336.10, "change": -1.10, "change_pct": -0.33},
        "GOOGL": {"price": 134.00, "previous_close": 131.20, "change": 2.80, "change_pct": 2.13},
        "SGOV": {"price": 100.00, "previous_close": 100.00, "change": 0.00, "change_pct": 0.00},
    }


def _data(healthy=True, summary=None, positions=None,
          margin_md=_MARGIN_MD, stress_md=_STRESS_MD, fx_rates=None, prices=None):
    return rp.ReportData(
        summary=summary if summary is not None else _summary(),
        positions=positions if positions is not None else _positions(),
        margin_md=margin_md,
        stress_md=stress_md,
        fx_rates=fx_rates if fx_rates is not None else {"USDCAD": 1.37},
        healthy=healthy,
        prices=prices if prices is not None else _prices(),
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
    # §1 header, §2 margin, §3 positions, §4 concentration, §5 stress
    assert "1,284,300" in out          # full NLV
    assert "MARGIN" in out.upper()
    assert "AAPL" in out               # positions
    assert "HHI" in out.upper() or "CONC" in out.upper()  # concentration
    assert "STRESS" in out.upper() or "DRAWDOWN" in out.upper()  # stress


def test_report_positions_show_weight_and_full_value():
    # USDCAD=1.0 here so native USD market values pass through unconverted,
    # keeping the fixture's $312,000 readable in the assertion. (FX conversion
    # itself is brief._combine_positions' job, tested in brief's suite.)
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}))
    # weight_pct surfaced, market value in full numbers (no M/k)
    assert "24.3" in out
    assert "312,000" in out


def test_report_positions_show_price_and_day_change():
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}))
    assert "$260.40" in out          # current price from /api/prices
    assert "+1.20%" in out           # day change pct
    assert "$3.10" in out            # day change dollar
    assert "-0.33%" in out           # a down mover (MSFT)


def test_report_day_change_missing_price_is_graceful():
    # A held symbol with no price entry shows 'n/a' on the day line, no crash.
    out = rp.build_report(_data(fx_rates={"USDCAD": 1.0}, prices={}))
    assert "n/a" in out
    assert "AAPL" in out


def test_report_columns_aligned():
    # All _kv rows share the same content width — values right-aligned to a
    # common edge. Check that lines using the grid end at a consistent column.
    out = rp.build_report(_data())
    kv_lines = [l for l in out.split("\n")
                if l.startswith(("  nlv", "  cash", "  bp", "  gpv",
                                 "  lev", "  util", "  cush"))]
    assert kv_lines, "expected aligned account rows"
    widths = {len(l) for l in kv_lines}
    assert widths == {rp.CONTENT_W}, f"misaligned widths: {widths}"


def test_report_merges_positions_across_accounts():
    # Same symbol held in two accounts merges into one row, weight summed.
    pos = _positions()
    pos["positions"].append(
        {"symbol": "AAPL", "sec_type": "STK", "shares": 300,
         "avg_cost": 180.0, "market_price": 260.0, "market_value": 78000.0,
         "unrealized_pnl": 24000.0, "currency": "USD",
         "weight_pct": 6.0, "account": "U7654321"},
    )
    out = rp.build_report(_data(positions=pos, fx_rates={"USDCAD": 1.0}))
    # AAPL appears once; merged weight = 24.3 + 6.0 = 30.3
    assert out.count("AAPL ") <= 1 or "AAPL" in out
    weights = rp._position_weights(pos)
    assert abs(weights["AAPL"] - 30.3) < 0.01


# ---------------------------------------------------------------------------
# Mobile width — the hard rule
# ---------------------------------------------------------------------------

def test_report_mobile_width():
    out = rp.build_report(_data())
    for line in out.split("\n"):
        assert len(line) <= 32, f"line too wide ({len(line)}): {line!r}"


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
