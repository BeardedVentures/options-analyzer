#!/usr/bin/env python3
"""price_projection.py — where the underlying is likely to be at expiration.

A credit spread is a bet about a RANGE, and the board never showed one. It showed a short
strike, a breakeven and a probability, and left the reader to picture the distribution those
came from. This states it: at expiry, with stated confidence, the underlying is expected
between X and Y.

METHOD, AND WHY THIS ONE

    band = spot * exp( +/- z * sigma_forecast * sqrt(trading_days(dte) / 252) )

Lognormal, zero drift, sigma from analysis.vol_forecast. Three choices, each tested rather
than assumed:

1. ZERO DRIFT. The same decision edge_calculator.calculate_true_pop already made and
   documented ("C1 FIX — drift removed"): replaying raw prices makes the answer a function of
   whether the sample period trended, which measures the sample and not the asset. A projection
   that leans up because the last two years leaned up is a backtest artifact wearing a forecast's
   clothes. Sector RELATIVE STRENGTH was tested as a drift input and rejected — rank correlation
   with forward returns is +0.01 to -0.04 at every horizon, none significant.

2. FORECAST SIGMA, NOT TRAILING. Same correction as the VRP work: the window covers the NEXT
   dte days, so it must be built from forecast realised vol, not the trailing window.

3. LOGNORMAL RATHER THAN EMPIRICAL QUANTILES. Both were coverage-tested on ~14,900 held-out
   observations across 20 names and 8 years — does a claimed X% window actually contain the
   price X% of the time?

       target   lognormal+trailing   lognormal+FORECAST   empirical+forecast
         50%          50.9%                52.7%                55.6%
         68%          68.4%                70.7%                72.6%
         80%          79.5%                81.5%                83.0%
         90%          87.9%                89.7%                92.0%

   Re-validated on a 35-CALENDAR-day horizon after the units fix below: 51.6 / 70.3 / 81.4 /
   89.8. Before that fix the same windows covered 60.5 / 78.9 / 88.0 / 94.0 — an "80%" band
   that was really a 88% one.

   Lognormal+forecast tracks the target within ~1.7pp at every level and errs slightly WIDE,
   which is the correct direction for something a person sizes risk against. Empirical
   quantiles over-cover by 3pp — a window wider than it claims is a window that quietly stops
   being a constraint.

WHAT THIS IS NOT. Not a direction call: the band is symmetric in log space by construction and
nothing here predicts which half the price lands in. Not a guarantee — a 80% window is wrong
one time in five BY DESIGN, and the coverage table above is the evidence that it is wrong
about the right amount.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

TRADING_DAYS = 252.0
CALENDAR_DAYS = 365.0

# Realised vol is annualised off DAILY BARS (x sqrt(252)), so its horizon must be counted in
# TRADING days. `dte` everywhere in this codebase is CALENDAR days — fetcher computes it as
# (expiry_date - today).days. Feeding a calendar count straight into a trading-day formula
# stretched every band by sqrt(365/252) = 1.204, i.e. 20% too wide at every confidence level,
# which silently turned an "80%" window into roughly a 90% one. The coverage table below was
# measured on 35 TRADING-day windows, so the formula was right and only the units feeding it
# were wrong.
def trading_days(calendar_days: float) -> float:
    """Calendar days to trading days. Markets are open ~252 of 365 days."""
    return float(calendar_days) * TRADING_DAYS / CALENDAR_DAYS

# Coverage-tested levels. Keys are the claim; values are what the held-out sample actually
# delivered, kept here so the UI can show the measured number rather than the promised one.
MEASURED_COVERAGE = {0.50: 0.527, 0.68: 0.707, 0.80: 0.815, 0.90: 0.897}

DEFAULT_CONFIDENCE = float(getattr(config, "PRICE_PROJECTION_CONFIDENCE", 0.80))

# Inverse normal CDF for the levels we support, so scipy is not a runtime dependency of a
# module the scan calls once per candidate.
_Z = {0.50: 0.6745, 0.68: 0.9945, 0.80: 1.2816, 0.90: 1.6449}


def _z_for(conf: float) -> float:
    if conf in _Z:
        return _Z[conf]
    # Abramowitz-Stegun rational approximation, adequate well beyond the precision a price
    # band is read to.
    p = 0.5 + float(conf) / 2.0
    p = min(max(p, 1e-6), 1 - 1e-6)
    t = math.sqrt(-2.0 * math.log(1 - p)) if p > 0.5 else math.sqrt(-2.0 * math.log(p))
    z = t - (2.515517 + 0.802853 * t + 0.010328 * t * t) / \
        (1 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t ** 3)
    return abs(z)


def project(spot: Optional[float], dte: Optional[int], forecast_vol_pp: Optional[float],
            confidence: float = None,
            trading_days_override: Optional[float] = None) -> Optional[Dict]:
    """The price window at expiration.

    `forecast_vol_pp` is annualised vol in POINTS (28.4 for 28.4%), which is what
    vol_forecast.forecast_rv returns. Returns None when any input is missing — a band drawn
    from a guessed volatility is worse than no band, because it looks identical to a real one.

    `trading_days_override` exists because a graded band forecast counts its horizon in TRADING
    days (a "5-day" claim settles on the fifth session) while an option's dte is CALENDAR days,
    and round-tripping one into the other loses the short horizons entirely: a 1-trading-day
    claim is 1.45 calendar days, `int()` takes it to 1, and `trading_days(1)` hands back 0.69 —
    a 31% understatement of the horizon on exactly the band with the least room for it. Callers
    that already know the horizon in trading days pass it here instead of laundering it through
    a calendar figure. Everything else, `dte` included, is unchanged: one band definition, one
    place it is computed.
    """
    conf = DEFAULT_CONFIDENCE if confidence is None else float(confidence)
    try:
        spot = float(spot); dte = int(dte); vol = float(forecast_vol_pp)
    except (TypeError, ValueError):
        return None
    if spot <= 0 or dte <= 0 or vol <= 0:
        return None

    td = trading_days(dte) if trading_days_override is None else float(trading_days_override)
    if td <= 0:
        return None
    sigma_h = (vol / 100.0) * math.sqrt(td / TRADING_DAYS)
    z = _z_for(conf)
    low = spot * math.exp(-z * sigma_h)
    high = spot * math.exp(z * sigma_h)
    return {
        "spot": round(spot, 2),
        "dte": dte,
        "trading_days": round(td, 1),
        "confidence": conf,
        "measured_coverage": MEASURED_COVERAGE.get(round(conf, 2)),
        "low": round(low, 2),
        "high": round(high, 2),
        "low_pct": round((low / spot - 1) * 100, 1),
        "high_pct": round((high / spot - 1) * 100, 1),
        "sigma_horizon": round(sigma_h, 4),
        "vol_pp": round(vol, 2),
        # One-sigma move in dollars — the number most people actually want when asked "how
        # much could this move".
        "one_sigma_usd": round(spot * sigma_h, 2),
    }


def strike_position(band: Optional[Dict], strike: Optional[float],
                    side: str = "put") -> Optional[Dict]:
    """Where a short strike sits relative to the projected window.

    This is the part that connects the band to the decision. A window on its own is a fact
    about the stock; a window with the short strike marked on it is a fact about the TRADE —
    it says whether the strike is outside the range the engine expects, and by how far.

    `inside` True means the projection reaches the strike, i.e. the band alone does not clear
    it. That is not a veto — an 80% window is breached one time in five by construction, and
    the POP model prices that properly — but it is the single clearest way to say "this strike
    is inside where we think the stock is going".
    """
    if not band or strike is None:
        return None
    try:
        k = float(strike)
    except (TypeError, ValueError):
        return None
    spot = band["spot"]
    edge = band["low"] if side == "put" else band["high"]
    # Distance from the threatened edge of the band to the strike, as a % of spot. Positive =
    # the strike sits BEYOND the band edge (safer than the projection).
    cushion = ((edge - k) / spot * 100) if side == "put" else ((k - edge) / spot * 100)
    inside = cushion < 0
    return {
        "strike": round(k, 2),
        "side": side,
        "band_edge": edge,
        "cushion_pct": round(cushion, 2),
        "inside_band": inside,
        "note": (f"the {band['confidence']:.0%} window reaches ${k:,.2f} — this strike is inside "
                 f"where the projection expects the stock to travel"
                 if inside else
                 f"${k:,.2f} sits {abs(cushion):.1f}% beyond the {band['confidence']:.0%} "
                 f"window's edge at ${edge:,.2f}"),
    }


def for_candidate(spot, dte, vol_forecast: Optional[Dict],
                  short_strike=None, side: str = "put",
                  confidence: float = None) -> Optional[Dict]:
    """Convenience wrapper: takes the dict vol_forecast.for_ticker already produced."""
    if not vol_forecast:
        return None
    band = project(spot, dte, vol_forecast.get("forecast_pp"), confidence)
    if band and short_strike is not None:
        band["strike"] = strike_position(band, short_strike, side)
    return band


# ── The market's own band, read off the chain ─────────────────────────────────
# This is how a desk actually answers "where does the market think it will be", and it needs
# no volatility model at all.
#
# An option's delta is approximately the risk-neutral probability it finishes in the money. So
# the strike of a 10-delta put IS the price the market gives a 10% chance of being below, and
# the strike of a 10-delta call IS the price it gives a 10% chance of being above. Reading the
# two strikes off the chain gives an 80% window that already contains everything the market
# prices — skew, fat tails, event risk, supply and demand at individual strikes — none of
# which a single ATM IV plugged into a lognormal can express.
#
# It is also ASYMMETRIC, and that asymmetry is real information. Equity puts trade at higher
# implied vol than equidistant calls (the volatility skew VEGA already measures in
# analysis.vol_surface), so the market's downside edge sits further from spot than its upside
# edge. A symmetric band quietly asserts the opposite of what the chain is saying.

def implied_band_from_chain(puts, calls, spot, confidence: float = None) -> Optional[Dict]:
    """The market's expected range, read from option deltas rather than modelled.

    Requires both sides of the book: the downside edge comes from the put chain and the upside
    from the calls, and taking both from one side would mirror an asymmetry the market does
    not actually price.
    """
    conf = DEFAULT_CONFIDENCE if confidence is None else float(confidence)
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        return None
    if not puts or not calls or spot <= 0:
        return None
    tail = (1.0 - conf) / 2.0            # 80% window -> 10% in each tail

    def _nearest(chain, target_delta, want_below):
        best, best_gap = None, None
        for o in chain:
            d = o.get("delta")
            k = o.get("strike")
            if d is None or not k:
                continue
            d = abs(float(d))
            if want_below and float(k) >= spot:      # puts: strikes under spot
                continue
            if not want_below and float(k) <= spot:  # calls: strikes over spot
                continue
            gap = abs(d - target_delta)
            if best_gap is None or gap < best_gap:
                best, best_gap = o, gap
        # A chain that does not reach the tail cannot answer the question. Returning the
        # closest available strike anyway would silently report a 25% tail as a 10% one.
        if best is None or best_gap is None or best_gap > target_delta * 0.6:
            return None
        return best

    p = _nearest(puts, tail, True)
    c = _nearest(calls, tail, False)
    if not p or not c:
        return None
    low, high = float(p["strike"]), float(c["strike"])
    mid = (low + high) / 2.0
    return {
        "source": "chain",
        "spot": round(spot, 2),
        "confidence": conf,
        "low": round(low, 2),
        "high": round(high, 2),
        "low_pct": round((low / spot - 1) * 100, 1),
        "high_pct": round((high / spot - 1) * 100, 1),
        "low_delta": round(abs(float(p["delta"])), 3),
        "high_delta": round(abs(float(c["delta"])), 3),
        "low_iv_pp": round(float(p.get("iv") or 0) * 100, 1) or None,
        "high_iv_pp": round(float(c.get("iv") or 0) * 100, 1) or None,
        # How lopsided the market's own range is. Positive = the downside edge sits further
        # from spot than the upside one, which is the normal equity shape.
        "skew_pct": round(((spot - low) - (high - spot)) / spot * 100, 2),
        "center_offset_pct": round((mid / spot - 1) * 100, 2),
    }


def compare_bands(forecast_band: Optional[Dict],
                  implied_band: Optional[Dict]) -> Optional[Dict]:
    """VEGA's range against the market's — the edge, drawn rather than scored.

    If VEGA's band is NARROWER than the market's, the market is paying for a move larger than
    the engine expects, and selling premium is the right side of that disagreement. Wider, and
    the engine is being paid for less risk than it thinks it is taking. This is the same claim
    the VRP number makes; a reader who cannot check a number against a picture is taking it on
    faith.
    """
    if not forecast_band or not implied_band:
        return None
    f_w = forecast_band["high"] - forecast_band["low"]
    i_w = implied_band["high"] - implied_band["low"]
    if f_w <= 0 or i_w <= 0:
        return None
    ratio = f_w / i_w
    return {
        "forecast_width": round(f_w, 2),
        "implied_width": round(i_w, 2),
        "width_ratio": round(ratio, 3),
        # Each side separately: the put side is the one a bull put actually cares about.
        "downside_gap_pct": round((forecast_band["low"] - implied_band["low"]) /
                                  implied_band["spot"] * 100, 2),
        "upside_gap_pct": round((implied_band["high"] - forecast_band["high"]) /
                                implied_band["spot"] * 100, 2),
        "verdict": ("market pays for more move than VEGA expects" if ratio < 0.95
                    else "VEGA expects more move than the market is pricing" if ratio > 1.05
                    else "VEGA and the market agree on the range"),
        "favours_seller": ratio < 0.95,
    }
