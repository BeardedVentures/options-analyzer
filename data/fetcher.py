"""
data/fetcher.py — All API calls and data retrieval.

Data source priority:
  1. yfinance (primary, free, no key required)
  2. Tradier API (secondary, free sandbox tier, better Greeks)
  3. NewsAPI (news headlines)

All functions cache results for the session to avoid redundant API calls.
All functions degrade gracefully — log and continue, never crash.
"""

import time
import logging
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

import pandas as pd
import numpy as np
import requests
import yfinance as yf
from scipy.stats import norm

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Session-level in-memory cache
# ─────────────────────────────────────────────
_cache: Dict[str, Any] = {}
_call_timestamps: List[float] = []

RATE_LIMIT_DELAY = 0.25   # seconds between yfinance calls
MIN_CALL_INTERVAL = 0.1   # minimum seconds between any API call


def _rate_limit():
    """Simple rate limiter — sleep between calls."""
    time.sleep(RATE_LIMIT_DELAY)


def _log_api_call(source: str, ticker: str, success: bool, error: str = ""):
    """Append to the module-level API call log (used by scan_log)."""
    if not hasattr(_log_api_call, "calls"):
        _log_api_call.calls = []
    _log_api_call.calls.append({
        "source": source,
        "ticker": ticker,
        "success": success,
        "error": error,
        "timestamp": datetime.now().isoformat(),
    })


def get_api_call_log() -> List[Dict]:
    return getattr(_log_api_call, "calls", [])


def clear_cache():
    """Clear session cache — call at start of each scan."""
    _cache.clear()
    _log_api_call.calls = []


# ─────────────────────────────────────────────
# PRICE DATA
# ─────────────────────────────────────────────

def get_price_data(ticker: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV historical data from yfinance.

    Returns DataFrame with columns: Open, High, Low, Close, Volume
    Returns None on failure.
    """
    cache_key = f"price_{ticker}_{period}"
    if cache_key in _cache:
        return _cache[cache_key]

    _rate_limit()
    try:
        yticker = yf.Ticker(ticker)
        data = yticker.history(period=period, auto_adjust=True)

        if data is None or data.empty:
            logger.warning(f"[fetcher] No price data returned for {ticker}")
            _log_api_call("yfinance.price", ticker, False, "Empty response")
            return None

        # Ensure standard column names
        data.index = pd.to_datetime(data.index)
        data = data[["Open", "High", "Low", "Close", "Volume"]].copy()
        data.dropna(subset=["Close"], inplace=True)

        _cache[cache_key] = data
        _log_api_call("yfinance.price", ticker, True)
        logger.debug(f"[fetcher] Price data for {ticker}: {len(data)} rows")
        return data

    except Exception as e:
        logger.error(f"[fetcher] Error fetching price data for {ticker}: {e}")
        _log_api_call("yfinance.price", ticker, False, str(e))
        return None


# ─────────────────────────────────────────────
# OPTIONS CHAIN
# ─────────────────────────────────────────────

def _bs_delta(S: float, K: float, T: float, sigma: float,
              option_type: str = "put") -> Optional[float]:
    """
    Black-Scholes delta calculation.
    S = underlying price, K = strike, T = years to expiration,
    sigma = annualized IV, r = risk-free rate from config.
    """
    try:
        r = config.RISK_FREE_RATE
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return None
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        if option_type == "put":
            return float(norm.cdf(d1) - 1)    # negative for puts
        else:
            return float(norm.cdf(d1))         # positive for calls
    except Exception:
        return None


def _bs_theta(S: float, K: float, T: float, sigma: float,
              option_type: str = "put") -> Optional[float]:
    """Black-Scholes theta (per day)."""
    try:
        r = config.RISK_FREE_RATE
        if T <= 0 or sigma <= 0:
            return None
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        pdf_d1 = norm.pdf(d1)
        theta_annual = -(S * pdf_d1 * sigma) / (2 * np.sqrt(T))
        if option_type == "put":
            theta_annual += r * K * np.exp(-r * T) * norm.cdf(-d2)
        else:
            theta_annual -= r * K * np.exp(-r * T) * norm.cdf(d2)
        return float(theta_annual / 365)
    except Exception:
        return None


def _parse_yfinance_options(ticker: str, current_price: float,
                            min_dte: int, max_dte: int) -> List[Dict]:
    """
    Parse yfinance options chain into standardized option records.
    Calculates delta via Black-Scholes when not provided.
    """
    records = []
    today = datetime.now().date()

    try:
        yticker = yf.Ticker(ticker)
        expirations = yticker.options

        if not expirations:
            logger.warning(f"[fetcher] No option expirations for {ticker}")
            return []

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days

            if not (min_dte <= dte <= max_dte):
                continue

            _rate_limit()
            try:
                chain = yticker.option_chain(exp_str)
            except Exception as e:
                logger.warning(f"[fetcher] Could not load chain {ticker} {exp_str}: {e}")
                continue

            T = dte / 365.25  # years

            for _, row in chain.puts.iterrows():
                strike = float(row.get("strike", 0))
                bid = float(row.get("bid", 0) or 0)
                ask = float(row.get("ask", 0) or 0)
                iv = float(row.get("impliedVolatility", 0) or 0)
                raw_vol = row.get("volume", 0)
                volume = int(raw_vol) if pd.notna(raw_vol) else 0
                raw_oi = row.get("openInterest", 0)
                oi = int(raw_oi) if pd.notna(raw_oi) else 0
                mid = round((bid + ask) / 2, 2) if (bid + ask) > 0 else 0

                if strike <= 0 or mid <= 0:
                    continue

                delta = _bs_delta(current_price, strike, T, iv, "put")
                theta = _bs_theta(current_price, strike, T, iv, "put")

                records.append({
                    "ticker": ticker,
                    "type": "put",
                    "strike": strike,
                    "expiration": exp_str,
                    "dte": dte,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "iv": iv,
                    "volume": volume,
                    "open_interest": oi,
                    "delta": delta,
                    "theta": theta,
                })

        logger.debug(f"[fetcher] Options for {ticker}: {len(records)} puts in DTE range {min_dte}-{max_dte}")
        return records

    except Exception as e:
        logger.error(f"[fetcher] yfinance options error for {ticker}: {e}")
        return []


def _parse_tradier_options(ticker: str, current_price: float,
                           min_dte: int, max_dte: int) -> List[Dict]:
    """Fetch options with Greeks from Tradier API."""
    if not config.TRADIER_API_KEY:
        return []

    base = ("https://sandbox.tradier.com" if config.TRADIER_SANDBOX
            else "https://api.tradier.com")
    headers = {
        "Authorization": f"Bearer {config.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    # Step 1: get expiration dates
    today = datetime.now().date()
    records = []

    try:
        r = requests.get(
            f"{base}/v1/markets/options/expirations",
            headers=headers,
            params={"symbol": ticker, "includeAllRoots": "true"},
            timeout=10,
        )
        r.raise_for_status()
        expirations = r.json().get("expirations", {}).get("date", [])
        if isinstance(expirations, str):
            expirations = [expirations]
    except Exception as e:
        logger.warning(f"[fetcher] Tradier expirations error {ticker}: {e}")
        _log_api_call("tradier.expirations", ticker, False, str(e))
        return []

    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if not (min_dte <= dte <= max_dte):
                continue

            time.sleep(0.3)
            r = requests.get(
                f"{base}/v1/markets/options/chains",
                headers=headers,
                params={"symbol": ticker, "expiration": exp_str, "greeks": "true"},
                timeout=10,
            )
            r.raise_for_status()
            options_data = r.json().get("options", {}).get("option", [])
            if not options_data:
                continue

            for opt in options_data:
                if opt.get("option_type") != "put":
                    continue
                greeks = opt.get("greeks") or {}
                bid = float(opt.get("bid", 0) or 0)
                ask = float(opt.get("ask", 0) or 0)
                mid = round((bid + ask) / 2, 2) if (bid + ask) > 0 else 0

                records.append({
                    "ticker": ticker,
                    "type": "put",
                    "strike": float(opt.get("strike", 0)),
                    "expiration": exp_str,
                    "dte": dte,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "iv": float(opt.get("implied_volatility") or greeks.get("mid_iv") or greeks.get("smv_vol") or 0),
                    "volume": int(opt.get("volume", 0) or 0),
                    "open_interest": int(opt.get("open_interest", 0) or 0),
                    "delta": float(greeks.get("delta", 0) or 0),
                    "theta": float(greeks.get("theta", 0) or 0),
                    "gamma": float(greeks.get("gamma", 0) or 0),
                    "vega": float(greeks.get("vega", 0) or 0),
                })

        except Exception as e:
            logger.warning(f"[fetcher] Tradier chain error {ticker} {exp_str}: {e}")

    _log_api_call("tradier.chains", ticker, len(records) > 0)
    return records


def get_options_chain(ticker: str,
                      min_dte: int = None,
                      max_dte: int = None) -> List[Dict]:
    """
    Get options chain for a ticker within the DTE range.
    Tries Tradier first (has real Greeks), falls back to yfinance + BS delta.

    Returns list of standardized option dicts.
    """
    if min_dte is None:
        min_dte = config.MIN_DTE
    if max_dte is None:
        max_dte = config.MAX_DTE

    cache_key = f"options_{ticker}_{min_dte}_{max_dte}"
    if cache_key in _cache:
        return _cache[cache_key]

    price_data = get_price_data(ticker, period="5d")
    current_price = float(price_data["Close"].iloc[-1]) if price_data is not None and not price_data.empty else None
    if not current_price:
        logger.error(f"[fetcher] Cannot get options for {ticker}: no current price")
        return []

    # Try Tradier first
    records = []
    if config.TRADIER_API_KEY:
        records = _parse_tradier_options(ticker, current_price, min_dte, max_dte)

    # Fall back to yfinance
    if not records:
        records = _parse_yfinance_options(ticker, current_price, min_dte, max_dte)
        _log_api_call("yfinance.options", ticker, len(records) > 0)

    _cache[cache_key] = records
    return records


# ─────────────────────────────────────────────
# EARNINGS DATE
# ─────────────────────────────────────────────

def get_earnings_date(ticker: str) -> Optional[datetime]:
    """
    Return the next upcoming earnings date for a ticker.
    Returns None if unavailable or not applicable (ETFs).
    """
    cache_key = f"earnings_{ticker}"
    if cache_key in _cache:
        return _cache[cache_key]

    # ETFs don't have earnings
    etf_tickers = {item["ticker"] for item in config.WATCHLIST if item.get("type") == "ETF"}
    if ticker in etf_tickers:
        _cache[cache_key] = None
        return None

    _rate_limit()
    try:
        yticker = yf.Ticker(ticker)
        cal = yticker.calendar

        earnings_dt = None

        if cal is not None:
            if isinstance(cal, dict):
                # yfinance >= 0.2.x returns a plain dict
                dates = cal.get("Earnings Date", [])
                if not isinstance(dates, (list, tuple)):
                    dates = [dates]
                future = [d for d in dates if pd.notna(d) and pd.Timestamp(d) >= pd.Timestamp.now()]
                if future:
                    earnings_dt = pd.Timestamp(min(future)).to_pydatetime()
            elif hasattr(cal, "empty") and not cal.empty:
                # older yfinance returns a DataFrame
                if "Earnings Date" in cal.index:
                    dates = cal.loc["Earnings Date"]
                    if hasattr(dates, "__iter__"):
                        future = [d for d in dates if pd.notna(d) and pd.Timestamp(d) >= pd.Timestamp.now()]
                        if future:
                            earnings_dt = pd.Timestamp(min(future)).to_pydatetime()
                    elif pd.notna(dates):
                        earnings_dt = pd.Timestamp(dates).to_pydatetime()

        if earnings_dt is None:
            # Try earnings_dates property
            try:
                ed = yticker.earnings_dates
                if ed is not None and not ed.empty:
                    now = pd.Timestamp.now(tz="UTC")
                    future = ed[ed.index >= now]
                    if not future.empty:
                        earnings_dt = future.index[0].to_pydatetime()
            except Exception:
                pass

        _cache[cache_key] = earnings_dt
        _log_api_call("yfinance.earnings", ticker, True)
        return earnings_dt

    except Exception as e:
        logger.warning(f"[fetcher] Earnings date error for {ticker}: {e}")
        _log_api_call("yfinance.earnings", ticker, False, str(e))
        _cache[cache_key] = None
        return None


# ─────────────────────────────────────────────
# VIX
# ─────────────────────────────────────────────

def get_vix() -> Dict:
    """
    Return VIX current level and 5-day trend.
    Returns dict: {current, trend, label, history}
    """
    cache_key = "vix"
    if cache_key in _cache:
        return _cache[cache_key]

    _rate_limit()
    try:
        vix_data = yf.Ticker("^VIX").history(period="10d", auto_adjust=True)
        if vix_data is None or vix_data.empty:
            raise ValueError("Empty VIX data")

        current = float(vix_data["Close"].iloc[-1])
        week_ago = float(vix_data["Close"].iloc[-5]) if len(vix_data) >= 5 else current
        trend = "rising" if current > week_ago * 1.02 else ("falling" if current < week_ago * 0.98 else "stable")

        if current < 15:
            label = "LOW"
        elif current < 20:
            label = "MODERATE"
        elif current < 30:
            label = "ELEVATED"
        else:
            label = "HIGH"

        result = {
            "current": round(current, 2),
            "week_ago": round(week_ago, 2),
            "change": round(current - week_ago, 2),
            "trend": trend,
            "label": label,
            "history": vix_data["Close"].tail(5).round(2).tolist(),
        }
        _cache[cache_key] = result
        _log_api_call("yfinance.vix", "^VIX", True)
        return result

    except Exception as e:
        logger.error(f"[fetcher] VIX error: {e}")
        _log_api_call("yfinance.vix", "^VIX", False, str(e))
        result = {"current": 0, "trend": "unknown", "label": "UNKNOWN", "history": []}
        _cache[cache_key] = result
        return result


# ─────────────────────────────────────────────
# NEWS
# ─────────────────────────────────────────────

def get_news(ticker: str, hours: int = 24) -> List[Dict]:
    """
    Fetch recent news headlines for a ticker.
    Tries NewsAPI first, falls back to yfinance .news property.

    Returns list of {title, source, published_at, url}
    """
    cache_key = f"news_{ticker}_{hours}"
    if cache_key in _cache:
        return _cache[cache_key]

    articles = []

    # Tier 1: NewsAPI
    if config.NEWS_API_KEY:
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            params = {
                "q": ticker,
                "from": cutoff,
                "sortBy": "publishedAt",
                "language": "en",
                "apiKey": config.NEWS_API_KEY,
                "pageSize": 10,
            }
            r = requests.get("https://newsapi.org/v2/everything", params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            for art in data.get("articles", []):
                articles.append({
                    "title": art.get("title", ""),
                    "source": art.get("source", {}).get("name", ""),
                    "published_at": art.get("publishedAt", ""),
                    "url": art.get("url", ""),
                    "description": art.get("description", ""),
                })
            _log_api_call("newsapi", ticker, True)
        except Exception as e:
            logger.warning(f"[fetcher] NewsAPI error for {ticker}: {e}")
            _log_api_call("newsapi", ticker, False, str(e))

    # Tier 2: yfinance fallback
    if not articles:
        try:
            _rate_limit()
            yticker = yf.Ticker(ticker)
            raw = yticker.news or []
            for item in raw[:10]:
                articles.append({
                    "title": item.get("title", ""),
                    "source": item.get("publisher", ""),
                    "published_at": datetime.fromtimestamp(
                        item.get("providerPublishTime", 0)
                    ).isoformat(),
                    "url": item.get("link", ""),
                    "description": item.get("summary", ""),
                })
            _log_api_call("yfinance.news", ticker, True)
        except Exception as e:
            logger.warning(f"[fetcher] yfinance news error for {ticker}: {e}")
            _log_api_call("yfinance.news", ticker, False, str(e))

    _cache[cache_key] = articles
    return articles


def get_macro_news(hours: int = 24) -> List[Dict]:
    """
    Fetch macro news: Fed, inflation, GDP, market conditions.
    """
    cache_key = f"macro_news_{hours}"
    if cache_key in _cache:
        return _cache[cache_key]

    articles = []
    topics = ["Federal Reserve", "inflation CPI", "GDP", "market crash", "rate hike"]

    if config.NEWS_API_KEY:
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            query = " OR ".join(f'"{t}"' for t in topics)
            params = {
                "q": query,
                "from": cutoff,
                "sortBy": "publishedAt",
                "language": "en",
                "apiKey": config.NEWS_API_KEY,
                "pageSize": 15,
            }
            r = requests.get("https://newsapi.org/v2/everything", params=params, timeout=10)
            r.raise_for_status()
            for art in r.json().get("articles", []):
                articles.append({
                    "title": art.get("title", ""),
                    "source": art.get("source", {}).get("name", ""),
                    "published_at": art.get("publishedAt", ""),
                    "url": art.get("url", ""),
                })
            _log_api_call("newsapi.macro", "MACRO", True)
        except Exception as e:
            logger.warning(f"[fetcher] Macro news error: {e}")
            _log_api_call("newsapi.macro", "MACRO", False, str(e))

    _cache[cache_key] = articles
    return articles
