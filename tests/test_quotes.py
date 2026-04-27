"""Tests for build_quotes formatter — narrow 3-column table for /gateway quote."""
import brief as bf


def test_build_quotes_basic_layout():
    prices = {
        "MU":   {"price": 89.42, "change_pct": 1.2},
        "AVGO": {"price": 1832.10, "change_pct": -0.3},
        "NVDA": {"price": 876.45, "change_pct": 0.5},
        "GOOG": {"price": 145.22, "change_pct": -0.1},
    }
    out = bf.build_quotes(["MU", "AVGO", "NVDA", "GOOG"], prices, [])
    assert "MU" in out
    assert "AVGO" in out
    assert "+1.20%" in out
    assert "-0.30%" in out
    assert "$89.42" in out
    assert "$1,832.10" in out


def test_build_quotes_mobile_width():
    """Every line must fit a narrow mobile code block (≤ 32 chars)."""
    prices = {
        "MU":   {"price": 89.42, "change_pct": 1.2},
        "AVGO": {"price": 1832.10, "change_pct": -0.3},
    }
    out = bf.build_quotes(["MU", "AVGO"], prices, [])
    for line in out.split("\n"):
        assert len(line) <= 32, f"line too wide ({len(line)}): {line!r}"


def test_build_quotes_preserves_user_order():
    prices = {
        "A": {"price": 1.0, "change_pct": 0.0},
        "B": {"price": 2.0, "change_pct": 0.0},
        "C": {"price": 3.0, "change_pct": 0.0},
    }
    out = bf.build_quotes(["C", "A", "B"], prices, [])
    body = out.split("\n")
    # Header is index 1; rows follow.
    rows = [line for line in body if line.startswith(("A", "B", "C"))]
    assert rows[0].startswith("C")
    assert rows[1].startswith("A")
    assert rows[2].startswith("B")


def test_build_quotes_missing_symbol():
    prices = {"MU": {"price": 89.42, "change_pct": 1.2}}
    out = bf.build_quotes(["MU", "ZZZ"], prices, [])
    assert "no data: ZZZ" in out
    # The row still renders with placeholders
    assert "ZZZ" in out
    assert "—" in out


def test_build_quotes_computes_pct_from_prev_close():
    prices = {"MU": {"price": 88.0, "prev_close": 80.0}}
    out = bf.build_quotes(["MU"], prices, [])
    # 88 over 80 → +10.00%
    assert "+10.00%" in out


def test_build_quotes_no_chg_data():
    prices = {"MU": {"price": 89.42}}
    out = bf.build_quotes(["MU"], prices, [])
    # No change_pct, no prev_close → render dash
    assert "$89.42" in out
    assert "—" in out


def test_build_quotes_mcp_unreachable():
    out = bf.build_quotes(["MU"], {}, ["mcp prices fetch failed"])
    assert "⚠️" in out
    assert "fetch failed" in out


def test_build_quotes_sub_dollar_precision():
    prices = {"X": {"price": 0.1234, "change_pct": 1.0}}
    out = bf.build_quotes(["X"], prices, [])
    assert "$0.1234" in out


def test_fmt_quote_price_thresholds():
    assert bf._fmt_quote_price(1832.10) == "$1,832.10"
    assert bf._fmt_quote_price(89.42) == "$89.42"
    assert bf._fmt_quote_price(0.42) == "$0.4200"
