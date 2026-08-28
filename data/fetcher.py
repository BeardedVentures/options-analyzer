"""
data/fetcher.py -- All API calls and data retrieval.

Data source priority:
  1. Robinhood Agentic Trading MCP (primary as of 2026-08-27 -- official, real-time,
     full Greeks, via the brokerage account VEGA already trades on; see
     data/robinhood_mcp.py). Not yet confirmed against a live response -- run
     test_robinhood_mcp_connection.py before trusting this path in a real scan.
  2. Polygon.io (fallback -- Starter plan does NOT include quotes, see
     validate_polygon_connection; kept as a fallback link in the chain, not a fix)
  3. yfinance (last-resort fallback, free, no key required, BS-calculated Greeks)
  4. NewsAPI (news headlines)
  5. Tradier API (legacy -- inactive, kept for reference)

All functions cache results for the session to avoid redundant API calls.
All functions degrade gracefully -- log and continue, never crash.
"""

import re
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
# (ticker, min_dte, max_dte) already written to the chain-quality log this process.
_quality_recorded: set = set()
# Set once this run's first Polygon entitlement failure is logged, so a 403/no-quotes
# response warns ONCE per run instead of once per ticker (56 tickers would otherwise spam
# the log with the same root cause). Cleared in clear_cache() alongside the other run state.
_polygon_entitlement_warned: set = set()
# Same idea for Robinhood, plus a hard stop: once this run has established that the MCP
# path cannot serve (needs browser approval, SDK missing, server unreachable), every
# later ticker skips it instead of paying the same failure 56 times over.
_robinhood_unavailable_this_run: set = set()
# Tickers whose Robinhood fetch came back empty this run. A transport or auth failure is
# terminal and latches immediately; an empty ANSWER is ordinary (a thin name, an odd DTE
# window) and only counts toward this threshold.
_robinhood_failures: list = []
_ROBINHOOD_FAILURE_LIMIT = 5
# Set when NewsAPI answers 429. The free tier allows ~100 requests/day and a single scan
# makes 56 -- so the quota is gone inside the first cycle or two and every later call is
# a guaranteed round-trip to a refusal. run.log held 3,688 of them. Once rate-limited,
# stop asking for the rest of the run and use the yfinance headlines instead.
_newsapi_rate_limited: set = set()
_call_timestamps: List[float] = []

# One id per process, stamped onto every chain-quality reading, so the log can be sliced back
# into scans afterwards. A cycle is one process; without this the readings are an undifferentiated
# stream and "the worst ticker in the last scan" has no way to know where the last scan began.
_SCAN_ID = datetime.now().strftime("%Y%m%dT%H%M%S")

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


_SECRET_QS_RE = re.compile(r"(?i)\b(apikey|api_key|token|access_token|key|secret|password)=([^&\s\"']+)")


def redact_secrets(text: str) -> str:
    """Strip credentials out of a string before it is logged or persisted.

    requests puts the full request URL in its exception message, so a NewsAPI 429 carries
    the live API key. Those strings land in scan_log.json's api_calls[].error, which is a
    TRACKED file -- the key reached git history that way.
    """
    if not text:
        return text
    return _SECRET_QS_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", str(text))


def _log_api_call(source: str, ticker: str, success: bool, error: str = ""):
    """Append to the module-level API call log (used by scan_log)."""
    if not hasattr(_log_api_call, "calls"):
        _log_api_call.calls = []
    _log_api_call.calls.append({
        "source": source,
        "ticker": ticker,
        "success": success,
        "error": redact_secrets(error),
        "timestamp": datetime.now().isoformat(),
    })


def get_api_call_log() -> List[Dict]:
    return getattr(_log_api_call, "calls", [])


def clear_cache():
    """Clear session cache -- call at start of each scan."""
    _cache.clear()
    _quality_recorded.clear()
    _polygon_entitlement_warned.clear()
    _robinhood_unavailable_this_run.clear()
    _robinhood_failures.clear()
    _newsapi_rate_limited.clear()
    _log_api_call.calls = []


# ─────────────────────────────────────────────
# DATA SOURCE HEALTH CHECKS
# ─────────────────────────────────────────────

def validate_robinhood_connection(symbol: str = "SPY") -> Dict[str, Any]:
    """Probe the Robinhood Agentic Trading MCP server and return a health summary.

    Mirrors validate_polygon_connection's rigor deliberately: that function's original
    version only checked HTTP 200, which reported healthy=True through weeks of a Polygon
    plan that returned empty quotes on every contract (see VEGA_Polygon_Entitlement_Finding
    2026-08-26). Don't repeat that mistake here -- a call succeeding is not the same as a
    call returning a usable bid/ask.
    """
    if not getattr(config, "ROBINHOOD_MCP_ENABLED", True):
        return {
            "enabled": False,
            "healthy": True,   # disabled on purpose, not a failure
            "mode": "disabled",
            "reason": "ROBINHOOD_MCP_ENABLED is false",
        }

    health = {
        "enabled": True,
        "mode": "robinhood_mcp",
        "healthy": False,
        "reason": None,
    }
    try:
        from data import robinhood_mcp
        result = robinhood_mcp.fetch_put_chain(symbol, config.ROBINHOOD_MCP_URL)
        if result is None:
            health["reason"] = "fetch_put_chain returned None -- see WARNING log line for cause"
            return health
        quotes = result.get("quotes")
        # Structure not yet confirmed live (see module docstring) -- this only checks that
        # SOMETHING with plausible bid/ask-shaped content came back, not an exact schema.
        blob = json.dumps(quotes) if quotes is not None else ""
        if quotes and ('"bid"' in blob or '"bid_price"' in blob):
            health["healthy"] = True
            health["reason"] = "ok -- quote-shaped data present"
        else:
            health["reason"] = (
                "call succeeded but no bid-shaped field found in the response -- "
                "run test_robinhood_mcp_connection.py and check the real field names "
                "against _parse_robinhood_options()'s mapping"
            )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:                 # see _parse_robinhood_options on CancelledError
        health["reason"] = f"{type(exc).__name__}: {exc}"
    return health


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
        # limit=5, not 1 -- a single contract can legitimately have no active quote (far OTM,
        # no market maker interest); five gives the entitlement check something to find a
        # real bid/ask on before concluding the plan cannot see quotes at all.
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{symbol}",
            params={"limit": 5, "contract_type": "put", "apiKey": config.POLYGON_API_KEY},
            timeout=10,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and body.get("status") in ("OK", "DELAYED"):
            # A 200/OK response only proves the key authenticates and the endpoint exists --
            # it says nothing about whether the plan is entitled to quotes. 2026-08-26: the
            # Options Starter plan returned exactly this (200, status OK, real contracts with
            # open interest) while last_quote was empty on every one of them, because quotes
            # are a separate, higher-tier entitlement. This health check reported healthy=True
            # the entire time. Asserting a real bid or ask on at least one returned contract is
            # what actually proves the plan can be used for pricing.
            results = body.get("results", []) or []
            has_quote = any(
                (float((r_.get("last_quote") or {}).get("bid", 0) or 0) > 0
                 or float((r_.get("last_quote") or {}).get("ask", 0) or 0) > 0)
                for r_ in results
            )
            if not results:
                # No contracts at all for a symbol this liquid is itself suspicious, but it is
                # not an entitlement failure -- report healthy with a note rather than guessing.
                health["healthy"] = True
                health["reason"] = f"ok (no contracts returned for {symbol} -- unusual, verify manually)"
            elif has_quote:
                health["healthy"] = True
                health["reason"] = "ok -- quotes present"
            else:
                health["healthy"] = False
                health["reason"] = (
                    f"HTTP 200/{body.get('status')} but bid/ask empty on all {len(results)} "
                    f"contracts checked -- plan is very likely not entitled to options quotes "
                    f"(Starter/Developer lack this; Advanced tier includes it)"
                )
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


def _first_present(d: Dict, *keys, default=None):
    """Return the first of `keys` present (and not None) in dict `d`."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _find_contract_list(blob) -> List[Dict]:
    """Robinhood's raw MCP response shape isn't confirmed yet (see module docstring on
    validate_robinhood_connection / robinhood_mcp.py) -- this digs through the common
    wrapper shapes (bare list, or a dict with a 'quotes'/'options'/'results'/'data' key)
    so a small shape change doesn't require touching every caller."""
    if isinstance(blob, list):
        return blob
    if isinstance(blob, dict):
        for key in ("quotes", "options", "results", "data", "contracts"):
            val = blob.get(key)
            if isinstance(val, list):
                return val
    return []


def _parse_robinhood_options(ticker: str, current_price: float,
                             min_dte: int, max_dte: int,
                             option_type: str = "put",
                             expirations=None, strikes=None) -> List[Dict]:
    """Put contracts with real broker Greeks, from Robinhood's Agentic Trading MCP server.

    Field mapping is READ OFF A LIVE RESPONSE (2026-08-27), not inferred from tool names. The
    first version of this function mapped `bid`/`ask`/`greeks` from a two-call flow that does
    not exist; the server pairs contracts and quotes across two different tools and names every
    price field `*_price`:

        instrument  {id, chain_symbol, expiration_date, strike_price(str), type, tradability}
        quote       {instrument_id, bid_price(str), ask_price(str), mark_price(str),
                     implied_volatility(str), delta, gamma, theta, vega, rho (all str),
                     open_interest(int), volume(int), chance_of_profit_short(str), updated_at}

    Everything numeric arrives as a STRING, including the Greeks, which is the detail most
    likely to be missed: float() on each is not optional decoration.

    Emits the same record shape as the yfinance and Polygon parsers, so nothing downstream can
    tell which source it got -- with the Greeks now measured by the broker rather than
    Black-Scholes-estimated from a mid price.
    """
    # Default FALSE on the getattr too, not just in config: defence in depth for a Tier-1
    # path whose parser has never seen a real response.
    if not getattr(config, "ROBINHOOD_MCP_ENABLED", False):
        return []
    if _robinhood_unavailable_this_run:
        return []                       # already established this run; don't re-pay it

    try:
        # Imported inside the try: data/robinhood_mcp.py pulls the optional mcp SDK, and a
        # missing or broken optional dependency must degrade this source, not the scan.
        from data import robinhood_mcp
        result = robinhood_mcp.fetch_chain(ticker, config.ROBINHOOD_MCP_URL, option_type,
                                           spot=current_price,
                                           min_dte=min_dte, max_dte=max_dte,
                                           expirations=expirations, strikes=strikes)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:
        # BaseException, not Exception: the MCP transport surfaces failures from inside an
        # anyio task group as asyncio.CancelledError, which is a BaseException and therefore
        # slipped straight through an `except Exception` here. An unreachable data source must
        # never be able to end a scan.
        _robinhood_unavailable_this_run.add('failed')
        logger.warning(f"[fetcher] Robinhood MCP unavailable ({type(e).__name__}: {e}) -- "
                       f"falling back for the rest of this run.")
        _log_api_call("robinhood.mcp", ticker, False, str(e))
        return []

    if not result:
        # A single empty answer is NOT proof the source is down. On 2026-08-27 the first live
        # cycle called get_options_chain(ticker, 0, 200) to mark positions; that one query shape
        # failed, latched the source, and sent all 56 tickers to yfinance -- while the very same
        # ticker had been served correctly by Robinhood one second earlier. Require repeated
        # failures before writing the source off for the run.
        _robinhood_failures.append(ticker)
        logger.warning(f"[fetcher] Robinhood MCP returned nothing for {ticker} "
                       f"({len(_robinhood_failures)}/{_ROBINHOOD_FAILURE_LIMIT} before "
                       f"falling back for the run).")
        if len(_robinhood_failures) >= _ROBINHOOD_FAILURE_LIMIT:
            _robinhood_unavailable_this_run.add('no_result')
            logger.warning("[fetcher] Robinhood MCP failed %d times -- falling back to the "
                           "next tier for the rest of this run. Run "
                           "test_robinhood_mcp_connection.py to diagnose.",
                           len(_robinhood_failures))
        _log_api_call("robinhood.mcp", ticker, False, "no result")
        return []

    instruments = {i.get("id"): i for i in (result.get("instruments") or []) if i.get("id")}
    quotes = result.get("quotes") or []
    if not instruments or not quotes:
        _log_api_call("robinhood.mcp", ticker, False, "empty chain")
        return []

    def _f(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    def _i(x):
        try:
            return int(x) if x is not None else 0
        except (TypeError, ValueError):
            return 0

    today = datetime.now().date()
    records: List[Dict] = []
    unmatched = 0

    for q in quotes:
        inst = instruments.get(q.get("instrument_id"))
        if inst is None:
            unmatched += 1
            continue

        exp_str = inst.get("expiration_date")
        strike = _f(inst.get("strike_price"))
        if not exp_str or strike is None:
            continue
        try:
            dte = (datetime.strptime(exp_str, "%Y-%m-%d").date() - today).days
        except (TypeError, ValueError):
            continue
        if not (min_dte <= dte <= max_dte):
            continue

        bid, ask = _f(q.get("bid_price")) or 0.0, _f(q.get("ask_price")) or 0.0
        mid = _f(q.get("mark_price")) or _f(q.get("adjusted_mark_price"))
        if not mid and (bid + ask) > 0:
            mid = round((bid + ask) / 2, 4)
        # No two-sided market means no tradeable price. Same predicate the other parsers use.
        if bid <= 0 and ask <= 0:
            continue

        records.append({
            "ticker": ticker,
            "type": option_type,
            "strike": strike,
            "expiration": exp_str,
            "last_trade_date": exp_str,
            "dte": dte,
            "bid": bid,
            "ask": ask,
            "mid": mid or 0.0,
            "iv": _f(q.get("implied_volatility")),
            "volume": _i(q.get("volume")),
            "open_interest": _i(q.get("open_interest")),
            # Broker-computed, not Black-Scholes-derived. This is the reason to prefer this
            # source over yfinance at all.
            "delta": _f(q.get("delta")),
            "theta": _f(q.get("theta")),
            "gamma": _f(q.get("gamma")),
            "vega": _f(q.get("vega")),
            "rho": _f(q.get("rho")),
            # Robinhood's own probability that a short position expires worthless. Recorded for
            # comparison against VEGA's modelled POP; nothing gates on it.
            "rh_pop_short": _f(q.get("chance_of_profit_short")),
        })

    if unmatched:
        logger.debug("[fetcher] %s: %d quotes had no matching instrument", ticker, unmatched)
    _log_api_call("robinhood.mcp", ticker, len(records) > 0)
    logger.debug("[fetcher] Robinhood %s: %d %ss in DTE %s-%s", ticker, len(records),
                 option_type, min_dte, max_dte)
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
    raw_seen = 0        # contracts that passed the DTE/strike filters, before the quote check
    no_quote_count = 0  # of those, how many had bid == ask == mid == 0

    while True:
        try:
            r = requests.get(base_url, params=params, timeout=15)
            if r.status_code != 200:
                _log_api_call("polygon.options", ticker, False, f"HTTP {r.status_code}")
                if "http_error" not in _polygon_entitlement_warned:
                    _polygon_entitlement_warned.add("http_error")
                    try:
                        detail = r.json().get("message") or r.json().get("error") or r.text[:200]
                    except Exception:
                        detail = r.text[:200]
                    logger.warning(
                        f"[fetcher] Polygon options request failed (HTTP {r.status_code}): {detail} "
                        f"-- falling back to yfinance for the rest of this run. If this is 403/"
                        f"NOT_AUTHORIZED, the current plan tier does not include this endpoint; "
                        f"see config.POLYGON_API_KEY."
                    )
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
                raw_seen += 1
                if bid == 0 and ask == 0 and mid == 0:
                    no_quote_count += 1
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
    if raw_seen > 0 and no_quote_count == raw_seen and "no_quotes" not in _polygon_entitlement_warned:
        # Every contract in range came back with bid == ask == mid == 0. That is not "thin
        # market" -- it is the shape of a 200 OK response from a plan tier that returns
        # contracts (snapshot/aggregates) but is not entitled to quotes (NBBO bid/ask). A
        # per-ticker debug line here was silent for exactly this failure on 2026-08-26: the
        # Options Starter plan authenticates fine and returns real contracts with OI and
        # volume, but last_quote is always empty, so every record was silently dropped and
        # every ticker fell through to yfinance -- with nothing in the log to say why.
        _polygon_entitlement_warned.add("no_quotes")
        logger.warning(
            f"[fetcher] Polygon returned {raw_seen} {ticker} contracts with NO bid/ask on any "
            f"of them (last_quote empty). This plan tier is very likely not entitled to options "
            f"quotes -- falling back to yfinance for this run. Check the Polygon/Massive account: "
            f"quotes require the Options Advanced tier, not Starter or Developer."
        )
    return records


def _option_record_is_usable(opt: Dict) -> bool:
    """Is this one option record a real, quotable market?

    Extracted from _quality_filter_options so the SAME predicate can measure a chain without
    filtering it. The Polygon path does not filter, so a ratio expressed as
    len(after_filter)/len(before_filter) would read 1.000 there forever — a quality metric that
    is arithmetically incapable of reporting a problem on the primary data source. Measuring
    with the predicate directly is what lets the number be wrong.
    """
    bid = float(opt.get("bid", 0) or 0)
    ask = float(opt.get("ask", 0) or 0)
    mid = float(opt.get("mid", 0) or 0)
    volume = int(opt.get("volume", 0) or 0)
    oi = int(opt.get("open_interest", 0) or 0)

    if bid == 0 and ask == 0:                       # no market / stale
        return False
    if ask > 0 and bid > 0 and ask < bid:           # crossed market -- data error
        return False
    if mid > 0 and (ask - bid) / mid > 0.80:        # impossibly wide -- stale pricing
        return False
    if volume == 0 and oi == 0:                     # no activity at all
        return False
    return True


def measure_chain_quality(records: List[Dict]) -> tuple:
    """(raw_count, usable_count, usable_ratio) for a chain, without modifying it."""
    raw = len(records or [])
    usable = sum(1 for opt in (records or []) if _option_record_is_usable(opt))
    return raw, usable, (round(usable / raw, 4) if raw else 0.0)


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

    valid = [opt for opt in records if _option_record_is_usable(opt)]

    removed = len(records) - len(valid)
    if removed > 0:
        pct = removed / len(records) * 100
        level = logger.warning if pct > 30 else logger.debug
        level(
            f"[fetcher] {source} quality filter: removed {removed}/{len(records)} "
            f"({pct:.0f}%) stale/invalid option records for {ticker}"
        )

    return valid


def get_chain_by_expiry(ticker: str,
                        min_dte: int = None,
                        max_dte: int = None,
                        max_expirations: int = 6) -> Dict[str, List[Dict]]:
    """Chain grouped by expiration, for the volatility term structure.

    Spans a WIDER DTE window than the trading chain (default 5-120 vs the 25-45 the engine
    trades) because the whole point is comparing the front month against the back. Returns {}
    on any failure and never raises — a term-structure read is advisory and must not be able
    to break a scan.

    Implementation note: the brief specified reusing a helper called
    `_polygon_options_chain()`. No such function exists in this module; the real entry point
    is `get_options_chain()`, which handles the Polygon-then-yfinance fallback, the quality
    filter and caching. Building on the named-but-absent helper would have failed on import.
    """
    if min_dte is None:
        min_dte = int(getattr(config, "TERM_STRUCTURE_MIN_DTE", 5))
    if max_dte is None:
        max_dte = int(getattr(config, "TERM_STRUCTURE_MAX_DTE", 120))
    try:
        rows = get_options_chain(ticker, min_dte, max_dte)
        if not rows:
            return {}
        by_expiry: Dict[str, List[Dict]] = {}
        for r in rows:
            exp = r.get("expiration")
            if exp:
                by_expiry.setdefault(str(exp)[:10], []).append(r)
        # Sample ACROSS the DTE span, don't just take the nearest N. On SPY the nearest six
        # expirations are 5, 6, 7, 8, 9 and 16 DTE — six adjacent weeklies whose farthest
        # point does not even reach the 25-45 window the engine trades. Comparing front
        # against back is the entire purpose, so the endpoints must be far apart.
        keys = sorted(by_expiry.keys())
        if len(keys) > max_expirations:
            last = len(keys) - 1
            idxs = sorted({round(i * last / (max_expirations - 1))
                           for i in range(max_expirations)})
            keys = [keys[i] for i in idxs]
        return {k: by_expiry[k] for k in keys}
    except Exception as e:
        logger.warning(f"[fetcher] get_chain_by_expiry failed for {ticker}: {e}")
        return {}


def get_options_chain(ticker: str,
                      min_dte: int = None,
                      max_dte: int = None,
                      *,
                      apply_quality_gate: bool = True,
                      expirations=None,
                      strikes=None) -> List[Dict]:
    """
    Get options chain for a ticker within the DTE range.

    Priority:
      1. Robinhood Agentic Trading MCP -- real-time, full Greeks, official
      2. Polygon.io -- fallback (Starter plan lacks quotes as of 2026-08-26)
      3. yfinance   -- last-resort fallback, Black-Scholes Greeks

    Returns list of standardized option dicts.

    apply_quality_gate (default True) is the SELECTION contract: drop unquotable records and,
    if too little of the chain survives, return [] so no signal is built on a chain that is
    mostly absent. That is correct when choosing a NEW trade and wrong when marking one that is
    already open -- marking a vertical needs two specific strikes to quote, not a healthy chain,
    and returning [] there does not decline to score a position, it declines to MANAGE it.
    See _reprice_and_close_open() in auto_paper_cycle.py: with the gate on, PSX (5% quotable)
    and AMGN (7%) were never re-marked after 2026-08-13, and the DTE close rule that runs behind
    the same lookup was skipped with them. Pass False for the marking path only.

    The two views are cached separately. They are different answers to different questions and
    must never be able to serve each other.
    """
    if min_dte is None:
        min_dte = config.MIN_DTE
    if max_dte is None:
        max_dte = config.MAX_DTE

    # A targeted fetch returns a DELIBERATELY partial chain -- only the strikes and
    # expirations a caller named. It must never be served to, or from, a caller that asked
    # for the whole window.
    _tgt = ("_t%s" % hash((tuple(sorted(map(str, expirations or ()))),
                           tuple(sorted(map(float, strikes or ())))))) if (expirations or strikes) else ""
    cache_key = f"options_{ticker}_{min_dte}_{max_dte}_{'gated' if apply_quality_gate else 'raw'}{_tgt}"
    if cache_key in _cache:
        return _cache[cache_key]

    price_data = get_price_data(ticker, period="5d")
    current_price = float(price_data["Close"].iloc[-1]) if price_data is not None and not price_data.empty else None
    if not current_price:
        logger.error(f"[fetcher] Cannot get options for {ticker}: no current price")
        return []

    # Tier 1: Robinhood Agentic Trading MCP (real-time, full Greeks, official --
    # see data/robinhood_mcp.py; not yet confirmed against a live response, run
    # test_robinhood_mcp_connection.py first)
    records: List[Dict] = []
    chain_source = "none"
    if getattr(config, "ROBINHOOD_MCP_ENABLED", True):
        records = _parse_robinhood_options(ticker, current_price, min_dte, max_dte,
                                           expirations=expirations, strikes=strikes)
        if records:
            chain_source = "robinhood"

    # Tier 2: Polygon.io (fallback -- Starter plan lacks quotes as of 2026-08-26, see
    # validate_polygon_connection; kept as a link in the chain, not removed, in case
    # the plan is ever upgraded)
    if not records and config.POLYGON_API_KEY:
        records = _parse_polygon_options(ticker, current_price, min_dte, max_dte)
        if records:
            chain_source = "polygon"

    # Tier 3: yfinance fallback (BS-calculated Greeks)
    if not records:
        logger.debug(f"[fetcher] Robinhood/Polygon returned no data for {ticker} -- falling back to yfinance")
        raw_yf = _parse_yfinance_options(ticker, current_price, min_dte, max_dte)
        kept = _quality_filter_options(raw_yf, ticker, "yfinance")
        records = kept if apply_quality_gate else raw_yf
        chain_source = "yfinance"
        _log_api_call("yfinance.options", ticker, len(records) > 0)
        # Measure BEFORE the filter on this path: what arrived is the honest denominator.
        # The reading describes the CHAIN, so it is the same number whether or not this caller
        # asked for the filtered view -- otherwise the ungated path would log a perfect ratio.
        raw_count, usable_count, ratio = len(raw_yf), len(kept), (
            round(len(kept) / len(raw_yf), 4) if raw_yf else 0.0)
    else:
        # Polygon is not filtered, so measure it with the same predicate rather than
        # comparing a list to itself. See _option_record_is_usable.
        raw_count, usable_count, ratio = measure_chain_quality(records)

    # Once per chain, not once per view. Splitting the cache by apply_quality_gate made it
    # possible for one process to read the same chain twice and log the same reading twice,
    # which would double-count that ticker in every aggregate built on this file. The reading
    # describes the chain; how many callers asked for it is not part of the measurement.
    quality_key = (ticker, min_dte, max_dte)
    if quality_key not in _quality_recorded:
        _quality_recorded.add(quality_key)
        _record_chain_quality(ticker, chain_source, raw_count, usable_count, ratio)

    # The floor. Below it the chain is too thin to reason over, and every downstream signal —
    # IV rank, skew, term structure, the delta the strike is chosen on — becomes a statement
    # about the handful of contracts that happened to quote rather than about the underlying.
    # Returning [] empties the board for this ticker, which is the correct outcome: no read is
    # better than a confident read of nothing.
    floor = float(getattr(config, "CHAIN_QUALITY_MIN_RATIO", 0.30))
    if (apply_quality_gate and raw_count > 0 and ratio < floor
            and getattr(config, "CHAIN_QUALITY_GATE_ENABLED", True)):
        logger.warning(
            f"[fetcher] SKIP_DATA_QUALITY {ticker}: only {usable_count}/{raw_count} "
            f"({ratio:.0%}) of the {chain_source} chain is quotable, floor is {floor:.0%} "
            f"-- skipping this ticker rather than scoring a chain that is mostly absent."
        )
        _cache[cache_key] = []
        return []

    _cache[cache_key] = records
    return records


def _is_rate_limited(exc) -> bool:
    """Is this exception a 429? Checked off the response where possible, text otherwise --
    requests raises HTTPError whose str() carries the status, but a bare read of the code is
    more reliable when the response object survived."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    return "429" in str(exc)


def _record_chain_quality(ticker: str, chain_source: str, raw_count: int,
                          usable_count: int, ratio: float) -> None:
    """Persist the reading. Never raises -- instrumentation must not be able to fail a scan."""
    if not getattr(config, "CHAIN_QUALITY_LOG_ENABLED", True):
        return
    try:
        from data import data_quality_log
        data_quality_log.record(ticker, chain_source, raw_count, usable_count,
                                scan_id=_SCAN_ID)
    except Exception as e:                           # pragma: no cover - defensive
        logger.debug("[fetcher] chain-quality logging failed for %s: %s", ticker, e)


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


def get_last_earnings_date(ticker: str) -> Optional[datetime]:
    """
    Return the most recent PAST earnings date for a ticker (None if unavailable/ETF).

    Used by the post-earnings IV-crush detector (spec §3.5): a name that reported
    1–3 trading days ago while IV is still elevated is a premium-selling candidate.
    Best-effort — degrades to None if yfinance has no earnings history.
    """
    cache_key = f"last_earnings_{ticker}"
    if cache_key in _cache:
        return _cache[cache_key]

    etf_tickers = {item["ticker"] for item in config.WATCHLIST if item.get("type") == "ETF"}
    if ticker in etf_tickers:
        _cache[cache_key] = None
        return None

    _rate_limit()
    last_dt = None
    try:
        ed = yf.Ticker(ticker).earnings_dates
        if ed is not None and not ed.empty:
            now = pd.Timestamp.now(tz="UTC")
            idx = ed.index
            # Normalize to tz-aware UTC for a safe comparison
            try:
                idx_cmp = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
            except Exception:
                idx_cmp = idx
            past = ed[idx_cmp <= now]
            if not past.empty:
                last_dt = past.index.max().to_pydatetime()
        _log_api_call("yfinance.last_earnings", ticker, last_dt is not None)
    except Exception as e:
        logger.debug(f"[fetcher] Last earnings lookup failed for {ticker}: {e}")
        _log_api_call("yfinance.last_earnings", ticker, False, str(e))

    _cache[cache_key] = last_dt
    return last_dt


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

    # Tier 1: Robinhood. Official, already authenticated for chains, and it returns the full
    # article body rather than a truncated description -- so sentiment has real text to score.
    if getattr(config, "ROBINHOOD_MCP_ENABLED", False) and not _robinhood_unavailable_this_run:
        try:
            from data import robinhood_mcp
            for art in robinhood_mcp.fetch_news(ticker, config.ROBINHOOD_MCP_URL, limit=10):
                articles.append({
                    "title": art.get("title", ""),
                    "source": art.get("publisher", ""),
                    "published_at": art.get("published_at", ""),
                    "url": "",                      # the tool returns an id, not a link
                    # Prefer the full body; fall back to the preview when it is absent.
                    "description": art.get("content") or art.get("preview_text", ""),
                })
            if articles:
                _log_api_call("robinhood.news", ticker, True)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:                  # CancelledError is not an Exception
            logger.debug("[fetcher] Robinhood news unavailable for %s: %s", ticker, e)

    # Tier 2: NewsAPI -- skipped for the rest of the run once it has rate-limited us.
    if not articles and config.NEWS_API_KEY and not _newsapi_rate_limited:
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
            if _is_rate_limited(e):
                # ONE line, then silence. Sentiment quietly drops to keyword matching for the
                # session either way; what changes is that it stops costing 55 more round trips
                # per cycle and stops burying real errors under thousands of identical ones.
                _newsapi_rate_limited.add("429")
                logger.warning(
                    "[fetcher] NewsAPI is rate-limited (429) -- skipping it for the rest of "
                    "this run and using yfinance headlines. Sentiment scoring is on the "
                    "keyword fallback, not model-scored, until the quota resets or the plan "
                    "is upgraded.")
            else:
                logger.warning(f"[fetcher] NewsAPI error for {ticker}: {redact_secrets(str(e))}")
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

    if config.NEWS_API_KEY and not _newsapi_rate_limited:
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
            if _is_rate_limited(e):
                _newsapi_rate_limited.add("429")
                logger.warning("[fetcher] NewsAPI rate-limited on the macro query -- skipping "
                               "NewsAPI for the rest of this run.")
            else:
                logger.warning(f"[fetcher] Macro news error: {redact_secrets(str(e))}")
            _log_api_call("newsapi.macro", "MACRO", False, str(e))

    # Fallback. get_news() has had a yfinance tier since it was written; get_macro_news() never
    # did, so a single 429 on the macro query left market_context.macro_events an EMPTY LIST --
    # not degraded, absent. Regime context then scored with no macro input at all and nothing
    # said so. Index tickers carry the macro tape well enough to be worth more than nothing.
    if not articles:
        for proxy in ("SPY", "QQQ"):
            try:
                for art in get_news(proxy, hours=hours)[:5]:
                    articles.append({
                        "title": art.get("title", ""),
                        "source": art.get("source", ""),
                        "published_at": art.get("published_at", ""),
                        "url": art.get("url", ""),
                        "description": art.get("description", ""),
                        "macro_proxy": proxy,
                    })
            except Exception as e:                  # pragma: no cover - defensive
                logger.debug("[fetcher] macro fallback via %s failed: %s", proxy, e)
        if articles:
            logger.info("[fetcher] macro news served from index-proxy headlines (%d articles) "
                        "because the macro query was unavailable.", len(articles))

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


def get_call_options_chain(ticker: str, min_dte: int = None, max_dte: int = None,
                           *, apply_quality_gate: bool = True,
                           expirations=None, strikes=None) -> List[Dict]:
    """Live call chain within DTE. Session-cached.

    Source tiering matches get_options_chain: Robinhood first (real broker Greeks), yfinance
    as the fallback. Used by bear-call / iron-condor / lottery generators and by
    get_options_skew, which measures both wings.

    apply_quality_gate=False returns the unfiltered chain for the MARKING path only; see the
    note on get_options_chain. A call-side position has exactly the same right to be managed
    on a thin chain as a put-side one."""
    if min_dte is None:
        min_dte = config.MIN_DTE
    if max_dte is None:
        max_dte = config.MAX_DTE
    # A targeted fetch returns a DELIBERATELY partial chain -- only the strikes and
    # expirations a caller named. It must never be served to, or from, a caller that asked
    # for the whole window.
    _tgt = ("_t%s" % hash((tuple(sorted(map(str, expirations or ()))),
                           tuple(sorted(map(float, strikes or ())))))) if (expirations or strikes) else ""
    cache_key = f"calls_{ticker}_{min_dte}_{max_dte}_{'gated' if apply_quality_gate else 'raw'}{_tgt}"
    if cache_key in _cache:
        return _cache[cache_key]
    price_data = get_price_data(ticker, period="5d")
    current_price = float(price_data["Close"].iloc[-1]) if price_data is not None and not price_data.empty else None
    if not current_price:
        return []
    # Tier 1: Robinhood, exactly as on the put side. Until 2026-08-27 this function went
    # straight to yfinance, so bear-call spreads, iron-condor call legs, the lottery scanner
    # AND get_options_skew were all reading a 34-48%-quotable chain while bull puts ran on a
    # ~91% one. Skew scoring is disabled system-wide for precisely that reason, and it is
    # measured across BOTH wings -- so the call side was holding it down.
    records: List[Dict] = []
    chain_source = "none"
    if getattr(config, "ROBINHOOD_MCP_ENABLED", False):
        records = _parse_robinhood_options(ticker, current_price, min_dte, max_dte,
                                           option_type="call",
                                           expirations=expirations, strikes=strikes)
        if records:
            chain_source = "robinhood"

    if not records:
        # Tier 2: yfinance. Polygon is deliberately skipped here -- _parse_polygon_options is
        # hardcoded to contract_type=put, and the Starter plan returns no quotes at all
        # (see validate_polygon_connection), so wiring it in would add a call that cannot help.
        raw_yf = _parse_yfinance_calls(ticker, current_price, min_dte, max_dte)
        kept = raw_yf
        if apply_quality_gate:
            try:
                kept = _quality_filter_options(raw_yf, ticker, "yfinance")
            except Exception:
                kept = raw_yf
        records = kept
        chain_source = "yfinance"
        _log_api_call("yfinance.calls", ticker, len(records) > 0)
        raw_count, usable_count, ratio = len(raw_yf), len(kept), (
            round(len(kept) / len(raw_yf), 4) if raw_yf else 0.0)
    else:
        raw_count, usable_count, ratio = measure_chain_quality(records)

    # Record call-side chain quality too. It was never measured before, so the cockpit's
    # quality tile described only half the data the engine actually trades on.
    quality_key = (ticker, min_dte, max_dte, "call")
    if quality_key not in _quality_recorded:
        _quality_recorded.add(quality_key)
        _record_chain_quality(ticker, chain_source, raw_count, usable_count, ratio)

    floor = float(getattr(config, "CHAIN_QUALITY_MIN_RATIO", 0.30))
    if (apply_quality_gate and raw_count > 0 and ratio < floor
            and getattr(config, "CHAIN_QUALITY_GATE_ENABLED", True)):
        logger.warning(
            f"[fetcher] SKIP_DATA_QUALITY {ticker} calls: only {usable_count}/{raw_count} "
            f"({ratio:.0%}) of the {chain_source} chain is quotable, floor is {floor:.0%} "
            f"-- skipping this ticker rather than scoring a chain that is mostly absent."
        )
        _cache[cache_key] = []
        return []

    _cache[cache_key] = records
    return records


def get_options_skew(ticker: str, min_dte: int = None, max_dte: int = None,
                     target_delta: float = 0.30) -> Dict:
    """
    Measure IV skew for a ticker: IV(30-delta put) − IV(30-delta call), in vol POINTS.

    Positive → downside puts are richer than upside calls (normal equity skew); for a
    bull put spread this is edge (selling expensive insurance). See spec §3.3 and
    edge_calculator.compute_skew_score for how the raw value becomes a 0–15 component.

    Both wings are matched at the same expiration (nearest to SKEW_TARGET_DTE, default 30)
    to avoid a term-structure artifact. Returns a dict; skew_vol_pts is None when the
    chain is too thin to measure.
    """
    empty = {"ticker": ticker, "skew_vol_pts": None, "put_iv": None, "call_iv": None,
             "expiration": None, "put_delta": None, "call_delta": None}
    try:
        puts = get_options_chain(ticker, min_dte, max_dte)
        calls = get_call_options_chain(ticker, min_dte, max_dte)
        if not puts or not calls:
            return empty

        dte_by_exp: Dict[str, int] = {}
        for o in list(puts) + list(calls):
            exp = o.get("expiration")
            if exp and exp not in dte_by_exp and o.get("dte") is not None:
                dte_by_exp[exp] = int(o["dte"])

        common = ({p.get("expiration") for p in puts} & {c.get("expiration") for c in calls})
        common = {e for e in common if e}
        if not common:
            return empty

        target_dte = int(getattr(config, "SKEW_TARGET_DTE", 30))
        best_exp = min(common, key=lambda e: abs(dte_by_exp.get(e, 9999) - target_dte))

        put_pool = [p for p in puts if p.get("expiration") == best_exp and (p.get("iv") or 0) > 0]
        call_pool = [c for c in calls if c.get("expiration") == best_exp and (c.get("iv") or 0) > 0]
        if not put_pool or not call_pool:
            return empty

        put = min(put_pool, key=lambda o: abs(abs(float(o.get("delta") or 0)) - target_delta))
        call = min(call_pool, key=lambda o: abs(abs(float(o.get("delta") or 0)) - target_delta))
        piv = float(put.get("iv") or 0)
        civ = float(call.get("iv") or 0)
        if piv <= 0 or civ <= 0:
            return empty

        return {
            "ticker": ticker,
            "skew_vol_pts": round((piv - civ) * 100, 2),
            "put_iv": round(piv, 4),
            "call_iv": round(civ, 4),
            "expiration": best_exp,
            "put_delta": round(float(put.get("delta") or 0), 3),
            "call_delta": round(float(call.get("delta") or 0), 3),
        }
    except Exception as e:
        logger.warning(f"[fetcher] Skew computation failed for {ticker}: {e}")
        return empty
