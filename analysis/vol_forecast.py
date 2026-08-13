#!/usr/bin/env python3
"""vol_forecast.py — what realised volatility will be over the life of the trade.

WHY THIS EXISTS

VEGA sells variance. The number that decides whether a spread is worth writing is the variance
risk premium: what the options charge versus what the underlying actually delivers.

    vrp = implied_vol - realised_vol

The trade pays off against the realised vol of the NEXT ~35 days. VEGA was subtracting the
realised vol of the LAST 35 days. Those are not the same number, and the difference is not
random — measured over 35,774 observations across 20 names and 8 years:

    vol state today          trailing RV error vs what actually happened
    COMPRESSING (<0.85x)     -5.54pp   trailing UNDERSTATES  -> VRP reads too POSITIVE
    stable                   -1.34pp
    EXPANDING   (>1.15x)    +10.39pp   trailing OVERSTATES   -> VRP reads too NEGATIVE

So the engine was systematically too eager after a quiet stretch and too timid after a violent
one — taking trades where premium looked rich because vol was about to rise, and refusing
trades where premium looked thin because vol was about to fall. Unconditionally the bias is
only -0.13pp, which is why it never showed up in an average: the two errors are large,
opposite, and cancel.

WHAT REPLACES IT

Volatility mean-reverts, and its persistence is one of the most robust facts available for
free: sector vol rank correlation is +0.62 at one month and +0.78 at three (p ~ 1e-207),
while sector RETURN rank correlation is +0.01 and not significant at any horizon tested. This
module forecasts the thing that is forecastable and does not attempt the thing that is not.

    forecast = long_run + PHI * (recent - long_run)

PHI was fitted on a 60% train split and held out: 0.55, i.e. roughly half of any deviation
from the longer-run level decays inside the horizon.

SECTOR CONTEXT

A stock's sector carries information beyond the stock's own history — adding sector vol to the
regression improved out-of-sample MAE from 11.32 to 10.94. It enters as a small multiplicative
nudge, never as a level: if a name's sector is cooling, the name is likelier to cool with it.
Deliberately weak (SECTOR_WEIGHT), because the sector is context, not the subject.

NOT A DIRECTION MODEL. Nothing here predicts which way anything goes, and sector relative
strength is not used at all — it was tested and does not persist. This forecasts dispersion.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# Fitted on a 60/40 train/test split over 8 years and 20 mixed names. 1.00 would mean "no
# reversion, trust the trailing window"; 0.00 would mean "ignore recent vol entirely".
PHI = float(getattr(config, "VOL_REVERSION_PHI", 0.55))

# How much the sector's own expected drift moves the name's forecast. Small on purpose: the
# sector is context. At 0.30 a sector expected to cool 10% pulls the name down 3%.
SECTOR_WEIGHT = float(getattr(config, "VOL_SECTOR_WEIGHT", 0.30))

# Guard rails. A forecast outside these is a data failure, not a market view.
MIN_VOL_PP = 1.0
MAX_VOL_PP = 400.0

EXPANDING = "EXPANDING"
COMPRESSING = "COMPRESSING"
STABLE = "stable"


def vol_state(recent_pp: Optional[float], long_run_pp: Optional[float]) -> str:
    """Where this name's vol sits against its own longer-run level."""
    if not recent_pp or not long_run_pp or long_run_pp <= 0:
        return STABLE
    r = recent_pp / long_run_pp
    return EXPANDING if r > 1.15 else COMPRESSING if r < 0.85 else STABLE


def _revert(recent: float, long_run: float, phi: float = PHI) -> float:
    return long_run + phi * (recent - long_run)


def forecast_rv(recent_pp: Optional[float],
                long_run_pp: Optional[float],
                sector_recent_pp: Optional[float] = None,
                sector_long_run_pp: Optional[float] = None,
                phi: Optional[float] = None) -> Optional[Dict]:
    """Expected realised vol over the coming horizon, in vol POINTS.

    Returns a dict carrying the forecast AND its inputs, so a caller can show its work and a
    reviewer can tell a model output from a passthrough. None when there is not enough history
    to say anything — absence, never a silent fallback to the trailing number, because the
    whole point is that the trailing number is the thing being corrected.
    """
    if not recent_pp or recent_pp <= 0:
        return None
    phi = PHI if phi is None else phi
    # With no long-run window the honest forecast IS the recent level: no reversion can be
    # estimated, so nothing is claimed beyond what is observed.
    lr = long_run_pp if (long_run_pp and long_run_pp > 0) else recent_pp
    f = _revert(float(recent_pp), float(lr), phi)

    sector_adj = 1.0
    if sector_recent_pp and sector_long_run_pp and sector_recent_pp > 0:
        s_f = _revert(float(sector_recent_pp), float(sector_long_run_pp), phi)
        drift = s_f / float(sector_recent_pp)          # <1 = sector expected to cool
        sector_adj = 1.0 + SECTOR_WEIGHT * (drift - 1.0)
        f *= sector_adj

    f = max(MIN_VOL_PP, min(MAX_VOL_PP, f))
    return {
        "forecast_pp": round(f, 2),
        "recent_pp": round(float(recent_pp), 2),
        "long_run_pp": round(float(lr), 2),
        "state": vol_state(recent_pp, long_run_pp),
        "phi": phi,
        "sector_adj": round(sector_adj, 4),
        "shift_pp": round(f - float(recent_pp), 2),   # how far this moves VRP, and which way
    }


def vrp_forecast(implied_pp: Optional[float], fc: Optional[Dict]) -> Optional[float]:
    """The variance premium the trade will actually be paid against.

    implied - FORECAST realised, rather than implied - trailing realised. Positive means the
    options are charging more than the underlying is expected to deliver over the holding
    period, which is the only version of that sentence a premium seller can act on.
    """
    if implied_pp is None or not fc:
        return None
    return round(float(implied_pp) - float(fc["forecast_pp"]), 2)


# ── Sector context ────────────────────────────────────────────────────────────
# config.TICKER_SECTORS already groups the watchlist for the position-concentration cap. The
# same grouping is reused here rather than inventing a second taxonomy that would drift from
# it — one map, two consumers. Sectors with no listed proxy simply get no adjustment.
SECTOR_PROXY: Dict[str, str] = {
    "technology": "XLK", "tech_etf": "XLK", "cybersecurity": "XLK",
    "financials": "XLF", "healthcare": "XLV", "healthcare_etf": "XLV",
    "biotech": "XBI", "consumer_cyclical": "XLY", "consumer_staples": "XLP",
    "energy": "XLE", "industrials": "XLI", "materials": "XLB",
    "utilities": "XLU", "reits": "XLRE", "communications": "XLC",
    "broad_market": "SPY", "commodities": "GLD", "fixed_income": "TLT",
    # crypto is deliberately absent: its vol is driven by its own market, and the equity
    # sector complex says nothing about it. See the DECLARED cross-venue layer instead.
}

_SECTOR_CACHE: Dict[str, Dict] = {}


def sector_proxy_for(ticker: str) -> Optional[str]:
    """The ETF whose volatility stands in for this name's sector, or None."""
    sec = (getattr(config, "TICKER_SECTORS", {}) or {}).get((ticker or "").upper())
    return SECTOR_PROXY.get(sec) if sec else None


def sector_vol(proxy: str, fetch=None) -> Optional[Dict]:
    """Recent and long-run realised vol for one sector proxy, cached per process.

    Cached because a 56-name scan would otherwise pull the same eleven ETFs fifty-six times.
    Returns None on any failure: the forecast degrades to own-vol-only, which is still better
    than the trailing number it replaces, so a sector outage costs precision and not the
    signal.
    """
    proxy = (proxy or "").upper()
    if not proxy:
        return None
    if proxy in _SECTOR_CACHE:
        return _SECTOR_CACHE[proxy]
    try:
        import math
        if fetch is None:
            from data import fetcher as _f
            fetch = lambda t: _f.get_price_data(t, period="1y")
        px = fetch(proxy)
        if px is None or getattr(px, "empty", True):
            return None
        closes = [float(c) for c in px["Close"].values if c and float(c) > 0]
        if len(closes) < 130:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

        def _rv(n):
            w = rets[-n:]
            if len(w) < 5:
                return None
            m = sum(w) / len(w)
            v = sum((r - m) ** 2 for r in w) / (len(w) - 1)
            return math.sqrt(v) * math.sqrt(252) * 100

        out = {"proxy": proxy, "recent_pp": _rv(int(getattr(config, "VRP_HV_WINDOW", 35))),
               "long_run_pp": _rv(126)}
        out["state"] = vol_state(out["recent_pp"], out["long_run_pp"])
        _SECTOR_CACHE[proxy] = out
        return out
    except Exception as e:
        logger.debug("[vol_forecast] sector vol failed for %s: %s", proxy, e)
        return None


def reset_sector_cache() -> None:
    """Drop the per-process sector cache. Called at the top of a scan so a long-running
    cockpit does not price tomorrow's board off yesterday's sector vol."""
    _SECTOR_CACHE.clear()


def for_ticker(ticker: str, recent_pp: Optional[float], long_run_pp: Optional[float],
               fetch=None) -> Optional[Dict]:
    """forecast_rv with this ticker's sector context filled in automatically."""
    proxy = sector_proxy_for(ticker)
    sec = sector_vol(proxy, fetch=fetch) if proxy else None
    fc = forecast_rv(recent_pp, long_run_pp,
                     (sec or {}).get("recent_pp"), (sec or {}).get("long_run_pp"))
    if fc is not None:
        fc["sector_proxy"] = proxy
        fc["sector_state"] = (sec or {}).get("state")
    return fc
