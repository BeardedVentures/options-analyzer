"""
data/fundamentals.py — Fundamental data processing.

Sources: yfinance (free, no key needed).
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import pandas as pd
import yfinance as yf

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

_cache: Dict[str, Any] = {}


def get_fundamentals(ticker: str) -> Dict[str, Any]:
    """
    Fetch fundamental data for a ticker from yfinance.

    Returns dict with: sector, market_cap, pe_ratio, beta, earnings_date,
    dividend_yield, short_float, description.
    """
    cache_key = f"fundamentals_{ticker}"
    if cache_key in _cache:
        return _cache[cache_key]

    result = {
        "ticker": ticker,
        "sector": None,
        "industry": None,
        "market_cap": None,
        "pe_ratio": None,
        "forward_pe": None,
        "beta": None,
        "dividend_yield": None,
        "short_float": None,
        "earnings_date": None,
        "description": None,
        "is_etf": False,
    }

    # Detect ETF
    etf_set = {item["ticker"] for item in config.WATCHLIST if item.get("type") == "ETF"}
    result["is_etf"] = ticker in etf_set

    try:
        yticker = yf.Ticker(ticker)
        info = yticker.info or {}

        result["sector"] = info.get("sector") or info.get("fundFamily")
        result["industry"] = info.get("industry") or info.get("category")
        result["market_cap"] = info.get("marketCap")
        result["pe_ratio"] = info.get("trailingPE")
        result["forward_pe"] = info.get("forwardPE")
        result["beta"] = info.get("beta")
        result["dividend_yield"] = info.get("dividendYield")
        result["short_float"] = info.get("shortPercentOfFloat")
        result["description"] = (info.get("longBusinessSummary") or "")[:300]

        logger.debug(f"[fundamentals] Fetched data for {ticker}")

    except Exception as e:
        logger.warning(f"[fundamentals] Error fetching fundamentals for {ticker}: {e}")

    _cache[cache_key] = result
    return result


def days_until_earnings(earnings_dt: Optional[datetime]) -> int:
    """
    Return number of calendar days until earnings.
    Returns 999 if earnings_dt is None (safe — won't block trade).
    """
    if earnings_dt is None:
        return 999

    try:
        now = datetime.now(timezone.utc)
        # Ensure earnings_dt is timezone-aware
        if earnings_dt.tzinfo is None:
            earnings_dt = earnings_dt.replace(tzinfo=timezone.utc)
        delta = earnings_dt - now
        days = delta.days
        return max(0, days)
    except Exception as e:
        logger.warning(f"[fundamentals] days_until_earnings error: {e}")
        return 999


def format_market_cap(market_cap: Optional[float]) -> str:
    """Format market cap for display."""
    if not market_cap:
        return "N/A"
    if market_cap >= 1e12:
        return f"${market_cap / 1e12:.1f}T"
    elif market_cap >= 1e9:
        return f"${market_cap / 1e9:.1f}B"
    elif market_cap >= 1e6:
        return f"${market_cap / 1e6:.1f}M"
    return f"${market_cap:,.0f}"
