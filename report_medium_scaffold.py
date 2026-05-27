"""SCAFFOLD ONLY — medium-effort /gateway report extensions.

Structure + signatures for the three "medium" additions from the data-source
review. NONE of these are wired into report.py or discord_bot.py yet; they are
stubs so the shape is agreed before logic is written. Each raises
NotImplementedError and documents its data source, cost, and open questions.

Build order is Jeff's call. Nothing here is imported anywhere.
"""
from __future__ import annotations

from typing import Optional

import report as rp


# ---------------------------------------------------------------------------
# 1. Sector / asset-class grouping
# ---------------------------------------------------------------------------
#
# Goal: a per-account (and combined) breakdown of weight by sector, e.g.
#   SECTORS
#   Semis      78.4%
#   Gold        7.5%
#   Cash/ST    14.1%
#
# Data: positions already carry symbol + weight_pct. There is NO sector field
# from IBKR/Yahoo in the current feed, so we need a symbol→sector map.
# OPEN QUESTION (Jeff): static table in-repo (simple, needs manual upkeep as
# holdings change) vs. a new MCP field/endpoint (more work, auto). Given the
# book is ~7 concentrated names, a static dict is probably right.

# Placeholder for the static map — to be filled with the actual holdings'
# sectors once the source decision is made. Generic examples only here.
_SECTOR_MAP: dict[str, str] = {
    # "AAPL": "Tech", "GLD": "Gold", "SGOV": "Cash/ST", ...
}


def _sector_of(symbol: str) -> str:
    """Map a symbol to its sector bucket. CDRs share the parent's sector."""
    raise NotImplementedError("sector grouping — pending source decision")


def sector_breakdown(rows: list[dict]) -> list[tuple[str, float]]:
    """Aggregate position weights into (sector, total_weight_pct), desc.

    Sums weight_pct by _sector_of(symbol). Unknown symbols bucket as 'Other'.
    Returns [] until _SECTOR_MAP is populated.
    """
    raise NotImplementedError


def render_sectors(rows: list[dict]) -> list[str]:
    """Render the SECTORS block as _kv lines (left-aligned, 39-col grid)."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 2. Technicals — /gateway ta <SYMBOL>
# ---------------------------------------------------------------------------
#
# Goal: a per-symbol technicals card (RSI, SMA20/50/200, 52w hi/lo, vs-MA%).
# Data: /api/technicals?symbol=<SYM> already exists (markdown). Per-symbol
# only — do NOT loop over all holdings (rate/latency cost). New subcommand
# `/gateway ta SYM`, separate from `report`.
#
# OPEN QUESTION: parse the markdown into fields (consistent layout?) vs. pass
# the markdown through lightly reformatted. Need to see live /api/technicals
# output shape before committing to a parser.

def parse_technicals(md: Optional[str]) -> dict:
    """Parse /api/technicals markdown → {rsi, sma20, sma50, sma200, hi52, lo52}."""
    raise NotImplementedError


def render_technicals(symbol: str, md: Optional[str]) -> str:
    """Render a single-symbol technicals card (own fenced message)."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 3. What-if / trade preview — /gateway whatif <buy|sell> <qty> <SYM>
# ---------------------------------------------------------------------------
#
# Goal: preview a hypothetical trade's margin/buying-power impact before
# placing it. Data: /api/what-if?action=&symbol=&quantity= already exists
# (markdown). READ-ONLY preview — never places an order.
#
# OPEN QUESTION: which account it targets (primary default? a param?), and
# exactly which impact fields IBKR returns in the what-if (init/maint delta,
# post-trade excess liq). Need the live shape.

def parse_what_if(md: Optional[str]) -> dict:
    """Parse /api/what-if markdown → margin/BP deltas."""
    raise NotImplementedError


def render_what_if(action: str, qty: int, symbol: str, md: Optional[str]) -> str:
    """Render the trade-preview card."""
    raise NotImplementedError
