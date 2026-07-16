"""
data/fetcher.py -- All API calls and data retrieval.

Data source priority:
  1. Polygon.io (primary, free tier, 15-min delayed, real Greeks)
  2. yfinance (fallback, free, no key required, BS-calculated Greeks)
  3. NewsAPI (news headlines)
  4. Tradier API (legacy -- inactive, kept for reference)

All functions cache results for the session to avoid redundant API calls.
All functions degrade gracefully -- log and continue, never crash.
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
    """Simple rate limiter -- sleep between calls."""
    time.sleep(RATE_LIMIT_DELAY)


def _last_trade_date(exp_date):
    """Return the last tradable day for a listed option expiration date."""
    if exp_date.weekday() == 5:
        return exp_date - timedelta(days=1)
    return exp_date


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
    """Clear session cache -- call at start of each scan."""
    _cache.clear()
    _log_api_call.calls = []


# ─────────────────────────────────────────────
# DATA SOURCE HEALTH CHECKS
# ─────────────────────────────────────────────

def validate_polygon_connection(symbol: str = "SPY") -> Dict[str, Any]:
    """Probe Polygon.io free tier and return a health summary."""
    if not config.POLYGON_API_KEY:
        return {
            "enabled": False,
            "healthy": True,   # no key = graceful yfinance fallback, not a failure
            "mode": "yfinance_only",
            "reason": "POLYGON_API_KEY not set -- using yfinance fallback",
        }

    health = {
        "enabled": True,
        "mode": "polygon_delayed_15m",
        "healthy": False,
        "reason": None,
    }
    try:
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{symbol}",
            params={"limit": 1, "contract_type": "put", "apiKey": config.POLYGON_API_KEY},
            timeout=10,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and body.get("status") in ("OK", "DELAYED"):
            health["healthy"] = True
            health["reason"] = "ok"
        else:
            health["reason"] = f"HTTP {r.status_code}: {body.get('message', body.get('error', ''))}"
    except Exception as exc:
        health["reason"] = str(exc)
    return health


def validate_tradier_connection(symbol: str = "SPY") -> Dict[str, Any]:
    """Probe the configured Tradier environment and return a health summary."""
    if not config.TRADIER_API_KEY:
        return {
            "enabled": False,
            "healthy": False,
            "mode": "disabled",
            "reason": "TRADIER_API_KEY not set",
        }

    base = "https://sandbox.tradier.com" if config.TRADIER_SANDBOX else "https://api.tradier.com"
    headers = {
        "Authorization": f"Bearer {config.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    health = {
        "enabled": True,
        "mode": "sandbox" if config.TRADIER_SANDBOX else "live",
        "healthy": False,
        "profile_status": None,
        "expirations_status": None,
        "reason": None,
    }

    try:
        profile_resp = requests.get(f"{base}/v1/user/profile", headers=headers, timeout=10)
        health["profile_status"] = profile_resp.status_code
        if profile_resp.status_code != 200:
            health["reason"] = f"profile probe failed: {profile_resp.status_code}"
            return health

        expirations_resp = requests.get(
            f"{base}/v1/markets/options/expirations",
            headers=headers,
            params={"symbol": symbol, "includeAllRoots": "true"},
            timeout=10,
        )
        health["expirations_status"] = expirations_resp.status_code
        if expirations_resp.status_code != 200:
            health["reason"] = f"expirations probe failed: {expirations_resp.status_code}"
            return health

        expirations = expirations_resp.json().get("expirations", {}).get("date", [])
        if isinstance(expirations, str):
            expirations = [expirations]
        if not expirations:
            health["reason"] = "no expirations returned"
            return health

        health["healthy"] = True
        health["reason"] = "ok"
        return health

    except Exception as exc:
        health["reason"] = str(exc)
        return health


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

        # Sanity check: reject obvious data glitches before they can flow into the scan.
        # The most common failure mode is a wildly incorrect last close (for example, a
        # single-digit print on a four-digit ticker). Compare the most recent close to the
        # recent median and previous close; if both disagree badly, treat the series as bad.
        closes = data["Close"].dropna()
        if len(closes) >= 3:
            last_close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            recent_median = float(closes.tail(min(len(closes), 20)).median())
            if last_close > 0 and recent_median > 0 and prev_close > 0:
                median_gap = abs(last_close - recent_median) / recent_median
                prev_gap = abs(last_close - prev_close) / prev_close
                if median_gap >= 0.75 and prev_gap >= 0.50:
                    logger.warning(
                        f"[fetcher] Suspicious price series for {ticker}: "
                        f"last={last_close:.2f}, prev={prev_close:.2f}, median={recent_median:.2f} "
                        f"-- rejecting as likely data glitch"
                    )
                    _log_api_call("yfinance.price", ticker, False, "Suspicious price glitch")
                    return None

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
            last_trade_date = _last_trade_date(exp_date)
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
                    "last_trade_date": last_trade_date.isoformat(),
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
    """Fetch options with Greeks from Tradier API (legacy)."""
    if not config.TRADIER_API_KEY:
        return []

    base = ("https://sandbox.tradier.com" if config.TRADIER_SANDBOX
            else "https://api.tradier.com")
    headers = {
        "Authorization": f"Bearer {config.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    last_trade_date = None
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

            last_trade_date = _last_trade_date(exp_date)
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
                    "last_trade_date": last_trade_date.isoformat(),
                })

        except Exception as e:
            logger.warning(f"[fetcher] Tradier chain error {ticker} {exp_str}: {e}")

    _log_api_call("tradier.chains", ticker, len(records) > 0)
    return records


def _parse_polygon_options(ticker: str, current_price: float,
                            min_dte: int, max_dte: int) -> List[Dict]:
    """
    Fetch puts from Polygon /v3/snapshot/options -- real Greeks, 15-min delayed.
    Uses cursor-based pagination; caps at 10 pages (~2,500 contracts) per ticker.
    """
    if not config.POLYGON_API_KEY:
        return []

    today = datetime.now().date()
    from_date = today + timedelta(days=min_dte)
    to_date = today + timedelta(days=max_dte)

    base_url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {
        "contract_type": "put",
        "expiration_date.gte": from_date.isoformat(),
        "expiration_date.lte": to_date.isoformat(),
        "limit": 250,
        "apiKey": config.POLYGON_API_KEY,
    }

    records: List[Dict] = []
    pages = 0

    while True:
        try:
            r = requests.get(base_url, params=params, timeout=15)
            if r.status_code != 200:
                _log_api_call("polygon.options", ticker, False, f"HTTP {r.status_code}")
                return records
            data = r.json()
            pages += 1

            for opt in data.get("results", []) or []:
                details    = opt.get("details") or {}
                greeks     = opt.get("greeks") or {}
                last_quote = opt.get("last_quote") or {}
                day_data   = opt.get("day") or {}

                exp_str = details.get("expiration_date", "")
                if not exp_str:
                    continue
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if not (min_dte <= dte <= max_dte):
                    continue

                strike = float(details.get("strike_price", 0) or 0)
                if strike <= 0:
                    continue

                bid = float(last_quote.get("bid", 0) or 0)
                ask = float(last_quote.get("ask", 0) or 0)
                mid = float(last_quote.get("midpoint", 0) or 0)
                if mid == 0 and (bid + ask) > 0:
                    mid = round((bid + ask) / 2, 2)
                if bid == 0 and ask == 0 and mid == 0:
                    continue

                records.append({
                    "ticker":          ticker,
                    "type":            "put",
                    "strike":          strike,
                    "expiration":      exp_str,
                    "last_trade_date": _last_trade_date(exp_date).isoformat(),
                    "dte":             dte,
                    "bid":             bid,
                    "ask":             ask,
                    "mid":             mid,
                    "iv":              float(opt.get("implied_volatility", 0) or 0),
                    "volume":          int(day_data.get("volume", 0) or 0),
                    "open_interest":   int(opt.get("open_interest", 0) or 0),
                    "delta":           float(greeks.get("delta", 0) or 0),
                    "theta":           float(greeks.get("theta", 0) or 0),
                    "gamma":           float(greeks.get("gamma", 0) or 0),
                    "vega":            float(greeks.get("vega", 0) or 0),
                })

            next_url = data.get("next_url")
            if not next_url or pages >= 10:
                break
            # Follow cursor -- apiKey must be re-appended
            base_url = next_url
            params = {"apiKey": config.POLYGON_API_KEY}
            time.sleep(0.12)   # stay within free-tier rate limit (5 req/min)

        except Exception as e:
            _log_api_call("polygon.options", ticker, False, str(e))
            return records

    _log_api_call("polygon.options", ticker, len(records) > 0)
    logger.debug(f"[fetcher] Polygon options for {ticker}: {len(records)} puts in DTE {min_dte}-{max_dte} ({pages} pages)")
    return records


def _quality_filter_options(records: List[Dict], ticker: str, source: str) -> List[Dict]:
    """
    Filter out stale or unusable option records from the yfinance fallback.

    Removes:
      - Records with both bid=0 AND ask=0 (stale / no market)
      - Records with ask < bid (data error)
      - Records with impossibly wide bid/ask spread (> 80% of mid -- stale price)
      - Records with zero volume AND zero open interest (no market activity)

    Logs a warning if more than 30% of records are filtered out.
    """
    if not records:
        return records

    valid = []
    for opt in records:
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        mid = float(opt.get("mid", 0) or 0)
        volume = int(opt.get("volume", 0) or 0)
        oi = int(opt.get("open_interest", 0) or 0)

        # Both sides zero -- no market / stale
        if bid == 0 and ask == 0:
            continue
        # Crossed market -- data error
        if ask > 0 and bid > 0 and ask < bid:
            continue
        # Impossibly wide spread -- stale pricing (> 80% of mid)
        if mid > 0 and (ask - bid) / mid > 0.80:
            continue
        # No market activity at all -- liquidity concern
        if volume == 0 and oi == 0:
            continue

        valid.append(opt)

    removed = len(records) - len(valid)
    if removed > 0:
        pct = removed / len(records) * 100
        level = logger.warning if pct > 30 else logger.debug
        level(
            f"[fetcher] {source} quality filter: removed {removed}/{len(records)} "
            f"({pct:.0f}%) stale/invalid option records for {ticker}"
        )

    return valid


def get_options_chain(ticker: str,
                      min_dte: int = None,
                      max_dte: int = None) -> List[Dict]:
    """
    Get options chain for a ticker within the DTE range.

    Priority:
      1. Polygon.io -- real Greeks, 15-min delayed, free tier
      2. yfinance   -- fallback, Black-Scholes Greeks

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

    # Tier 1: Polygon.io (real Greeks, 15-min delayed)
    records: List[Dict] = []
    if config.POLYGON_API_KEY:
        records = _parse_polygon_options(ticker, current_price, min_dte, max_dte)

    # Tier 2: yfinance fallback (BS-calculated Greeks)
    if not records:
        logger.debug(f"[fetcher] Polygon returned no data for {ticker} -- falling back to yfinance")
        records = _parse_yfinance_options(ticker, current_price, min_dte, max_dte)
        records = _quality_filter_options(records, ticker, "yfinance")
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


# -------------------------------------------------
# NEWS
# -------------------------------------------------

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


# ─────────────────────────────────────────────
# CALL OPTIONS (for bear-call spreads, iron condors, lottery) — yfinance
# ─────────────────────────────────────────────
def _parse_yfinance_calls(ticker: str, current_price: float,
                          min_dte: int, max_dte: int) -> List[Dict]:
    """Parse yfinance CALL chain into standardized records (mirror of the puts parser).
    Delta positive for calls. 15-min delayed, BS Greeks."""
    records = []
    today = datetime.now().date()
    try:
        yticker = yf.Ticker(ticker)
        expirations = yticker.options
        if not expirations:
            return []
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            last_trade_date = _last_trade_date(exp_date)
            if not (min_dte <= dte <= max_dte):
                continue
            _rate_limit()
            try:
                chain = yticker.option_chain(exp_str)
            except Exception as e:
                logger.warning(f"[fetcher] Could not load call chain {ticker} {exp_str}: {e}")
                continue
            T = dte / 365.25
            for _, row in chain.calls.iterrows():
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
                delta = _bs_delta(current_price, strike, T, iv, "call")
                theta = _bs_theta(current_price, strike, T, iv, "call")
                records.append({
                    "ticker": ticker, "type": "call", "strike": strike, "expiration": exp_str,
                    "last_trade_date": last_trade_date.isoformat(), "dte": dte,
                    "bid": bid, "ask": ask, "mid": mid, "iv": iv,
                    "volume": volume, "open_interest": oi, "delta": delta, "theta": theta,
                })
        logger.debug(f"[fetcher] Calls for {ticker}: {len(records)} in DTE {min_dte}-{max_dte}")
        return records
    except Exception as e:
        logger.error(f"[fetcher] yfinance calls error for {ticker}: {e}")
        return []


def get_call_options_chain(ticker: str, min_dte: int = None, max_dte: int = None) -> List[Dict]:
    """Live call chain within DTE (yfinance, 15-min delayed). Session-cached.
    Used by bear-call / iron-condor / lottery generators. NOTE: first live use should be
    spot-checked against your broker (new code path, not yet validated on live calls data)."""
    if min_dte is None:
        min_dte = config.MIN_DTE
    if max_dte is None:
        max_dte = config.MAX_DTE
    cache_key = f"calls_{ticker}_{min_dte}_{max_dte}"
    if cache_key in _cache:
        return _cache[cache_key]
    price_data = get_price_data(ticker, period="5d")
    current_price = float(price_data["Close"].iloc[-1]) if price_data is not None and not price_data.empty else None
    if not current_price:
        return []
    records = _parse_yfinance_calls(ticker, current_price, min_dte, max_dte)
    try:
        records = _quality_filter_options(records, ticker, "yfinance")
    except Exception:
        pass
    _log_api_call("yfinance.calls", ticker, len(records) > 0)
    _cache[cache_key] = records
    return records
