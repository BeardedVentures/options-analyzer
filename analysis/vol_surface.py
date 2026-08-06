#!/usr/bin/env python3
"""
vol_surface.py — reads the volatility surface in two dimensions instead of one number.

VEGA has read IV as a single scalar per trade (IV rank at the chosen expiration). That is one
thermometer. The surface has two axes and both are actionable for a premium seller:

  TERM STRUCTURE (across expirations). Back months pricier than front means general
  nervousness — a good environment to sell into. Front month most expensive means the market
  is pricing imminent event risk — the worst moment to open. A lone spike at one expiration
  surrounded by calm on either side is a specific dated catalyst (earnings, Fed, binary), and
  selling premium that expires across it is selling a coin flip, not selling variance.

  SKEW DEPTH (across strikes). How much more expensive OTM puts are than equidistant calls.
  Steep put skew means the market is paying up specifically for crash insurance, which is
  exactly what a bull-put seller is underwriting. VEGA already scored skew at one point
  (30-delta); a curve across 20/30/40-delta says whether that steepness is real or an artifact
  of one illiquid strike.

Data note: on Polygon Starter the chain is 15 minutes delayed, so every read here is stale by
that much. That is fine for a daily scan whose entry is next-day anyway, but it must never be
presented as live.

Pure: sequences in, dicts out. No IO, no network.
"""
from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _cfg(name: str, default):
    return getattr(config, name, default)


# Chain rows use `type`/`iv`/`expiration` (see data/fetcher._parse_polygon_options). The
# original spec assumed Polygon's raw names — contract_type / implied_volatility /
# expiration_date — which appear nowhere in this codebase and would have made every read
# silently empty.
def _iv(row: Dict) -> Optional[float]:
    v = row.get("iv")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0.01 < v < 5.0 else None


def _atm_iv(rows: Sequence[Dict], spot: float) -> Optional[float]:
    """Average IV of the strike nearest spot. Averaging puts and calls at that strike when
    both are present cancels a little of the put/call quoting asymmetry."""
    usable = [r for r in rows if _iv(r) is not None and r.get("strike")]
    if not usable or not spot:
        return None
    atm_strike = min(usable, key=lambda r: abs(float(r["strike"]) - spot))["strike"]
    ivs = [_iv(r) for r in usable if r.get("strike") == atm_strike]
    ivs = [v for v in ivs if v is not None]
    return sum(ivs) / len(ivs) if ivs else None


def get_term_structure(chain_by_expiry: Dict[str, List[Dict]],
                       spot_price: float,
                       today: Optional[datetime.date] = None) -> Dict:
    """ATM IV at each expiration, and what the shape of that curve means.

    Returns:
        {
          "expirations":     [{"date","dte","atm_iv"}, ...]  (sorted by dte)
          "slope":           upward | flat | downward | event_spike | unknown
          "front_iv","back_iv","term_spread_pts"   (back - front, in vol points)
          "event_expiry":    date string of an anomalous middle spike, else None
          "confidence":      high (>=3 expirations) | low
        }
    """
    today = today or datetime.date.today()
    min_dte = int(_cfg("TERM_STRUCTURE_MIN_DTE", 5))
    max_dte = int(_cfg("TERM_STRUCTURE_MAX_DTE", 120))

    points: List[Dict] = []
    for exp_str, rows in (chain_by_expiry or {}).items():
        try:
            exp_date = datetime.date.fromisoformat(str(exp_str)[:10])
        except (ValueError, TypeError):
            continue
        dte = (exp_date - today).days
        if dte < min_dte or dte > max_dte:
            continue
        atm = _atm_iv(rows, spot_price)
        if atm is None:
            continue
        points.append({"date": str(exp_str)[:10], "dte": dte, "atm_iv": round(atm, 4)})

    points.sort(key=lambda p: p["dte"])
    blank = {"expirations": points, "slope": "unknown", "front_iv": None, "back_iv": None,
             "term_spread_pts": None, "event_expiry": None, "confidence": "low"}
    if len(points) < 2:
        return blank

    front_iv = points[0]["atm_iv"]
    back_iv = points[-1]["atm_iv"]
    term_spread = (back_iv - front_iv) * 100.0

    # A dated catalyst shows up as one expiration priced well above the LINE ITS NEIGHBOURS
    # SIT ON — so measure the excess against the front-to-back interpolation at that DTE,
    # not against the mean of all points. Standard deviation is the wrong yardstick here: the
    # spike is part of the sample, so it inflates both the mean and the sigma it would have
    # to clear. A 0.24 / 0.52 / 0.28 curve — an unmistakable catalyst — sits at 1.42 sigma
    # and slips under a 1.5 sigma test entirely.
    #
    # Endpoints are excluded on purpose: an expensive front month IS the downward-slope case,
    # and flagging it as an event too would double-penalise the same observation.
    event_expiry = None
    if len(points) >= 3:
        span = points[-1]["dte"] - points[0]["dte"]
        excess_min = float(_cfg("TERM_STRUCTURE_EVENT_EXCESS_PTS", 5.0))
        best_excess = 0.0
        for p in points[1:-1]:
            t = ((p["dte"] - points[0]["dte"]) / span) if span else 0.0
            expected = front_iv + t * (back_iv - front_iv)
            excess = (p["atm_iv"] - expected) * 100.0
            if excess >= excess_min and excess > best_excess:
                event_expiry, best_excess = p["date"], excess

    band = float(_cfg("TERM_STRUCTURE_FLAT_BAND_PTS", 2.0))
    if event_expiry:
        slope = "event_spike"
    elif term_spread > band:
        slope = "upward"
    elif term_spread < -band:
        slope = "downward"
    else:
        slope = "flat"

    return {
        "expirations": points,
        "slope": slope,
        "front_iv": round(front_iv, 4),
        "back_iv": round(back_iv, 4),
        "term_spread_pts": round(term_spread, 1),
        "event_expiry": event_expiry,
        "confidence": "high" if len(points) >= 3 else "low",
    }


def _iv_at_delta(rows: Sequence[Dict], target: float) -> Optional[float]:
    """IV of the contract whose |delta| is nearest `target`. Returns None when nothing sits
    within a sane distance, so a chain with only deep-ITM quotes cannot masquerade as a
    20-delta read."""
    tol = float(_cfg("SKEW_DELTA_TOLERANCE", 0.08))
    best, best_d = None, None
    for r in rows:
        d = r.get("delta")
        v = _iv(r)
        if d is None or v is None:
            continue
        gap = abs(abs(float(d)) - target)
        if gap <= tol and (best_d is None or gap < best_d):
            best, best_d = v, gap
    return best


def get_skew_depth(puts: Sequence[Dict], calls: Sequence[Dict],
                   spot_price: float, strategy: str = "bull_put") -> Dict:
    """Put-minus-call IV at 20, 30 and 40 delta — a curve, not a single point.

    The spec signature took one `chain`; put-versus-call skew cannot be computed from one
    side of the book, so both are required. Values are in vol POINTS (IV difference x 100),
    matching the existing skew_vol_pts convention.

    Returns:
        {"skew_20d","skew_30d","skew_40d","skew_steepness","strategy_premium","confidence"}
    """
    out = {"skew_20d": None, "skew_30d": None, "skew_40d": None,
           "skew_steepness": "unknown", "strategy_premium": None, "confidence": "low"}
    if not puts or not calls:
        return out

    pairs = {}
    for label, target in (("skew_20d", 0.20), ("skew_30d", 0.30), ("skew_40d", 0.40)):
        p_iv = _iv_at_delta(puts, target)
        c_iv = _iv_at_delta(calls, target)
        if p_iv is not None and c_iv is not None:
            pairs[label] = round((p_iv - c_iv) * 100.0, 2)
    out.update(pairs)

    vals = [v for v in pairs.values()]
    if not vals:
        return out

    # Weight the wings for a put seller: the 20-delta point is closest to where a bull put's
    # short strike actually lives, so it should count for more than the 40-delta.
    if "bull_put" in (strategy or "") or "put" in (strategy or ""):
        w = {"skew_20d": 0.5, "skew_30d": 0.3, "skew_40d": 0.2}
    elif "bear_call" in (strategy or ""):
        w = {"skew_20d": -0.5, "skew_30d": -0.3, "skew_40d": -0.2}  # calls rich favours a bear call
    else:
        w = {k: 1.0 / len(pairs) for k in pairs}
    tw = sum(abs(w[k]) for k in pairs)
    prem = sum(pairs[k] * w[k] for k in pairs) / tw if tw else None

    steep = float(_cfg("SKEW_STEEP_PTS", 4.0))
    flat = float(_cfg("SKEW_FLAT_PTS", 1.0))
    ref = pairs.get("skew_20d", pairs.get("skew_30d", vals[0]))
    if ref >= steep:
        steepness = "steep"
    elif ref <= -flat:
        steepness = "inverted"
    elif ref >= flat:
        steepness = "normal"
    else:
        steepness = "flat"

    out.update({
        "skew_steepness": steepness,
        "strategy_premium": round(prem, 2) if prem is not None else None,
        "confidence": "high" if len(pairs) >= 3 else ("medium" if len(pairs) == 2 else "low"),
    })
    return out


# Plain-language readings for the cockpit. Kept here so the engine owns the meaning of its own
# outputs rather than the UI re-deriving it.
TERM_SLOPE_NOTE = {
    "upward": "Back months price richer than the front — general nervousness rather than a "
              "dated event. A constructive environment to sell into.",
    "flat": "IV is even across expirations — no term-structure signal either way.",
    "downward": "The front month is the most expensive — the market is pricing imminent risk. "
                "The weakest point in the cycle to open a new short-premium position.",
    "event_spike": "One expiration prices far above its neighbours — a dated catalyst sits in "
                   "that window. Selling premium across it is underwriting a binary event, "
                   "not variance.",
    "unknown": "Not enough expirations to read a term structure.",
}

SKEW_NOTE = {
    "steep": "Steep put skew — the market is paying up for downside protection, which is "
             "exactly what a put seller underwrites.",
    "normal": "Normal put skew — modest premium for downside protection.",
    "flat": "Flat skew — puts and calls priced alike, so there is no fear premium to harvest.",
    "inverted": "Inverted skew — calls richer than puts, unusual and typically a squeeze or "
                "takeover bid. Favours the call side, not the put side.",
    "unknown": "Skew unreadable from the available quotes.",
}
