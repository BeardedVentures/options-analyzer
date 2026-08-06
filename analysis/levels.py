#!/usr/bin/env python3
"""
levels.py — support / resistance detection for VEGA.

Replaces the 2-bar-fractal scan that data/technicals.py used to do inline. That method
marked every two-day wiggle as a level, which made `nearest_support` systematically a
micro-low sitting right under spot rather than a price the market actually defends. Measured
on 2026-08-05: QQQ's nearest support was 0.0% below spot, XLE 0.3%, WMT 0.6%, and QQQ's three
"levels" (720.06 / 720 / 710.08) spanned 1.4% — one price area consuming every slot, with the
first two being the same level duplicated by round-number injection.

That mattered downstream: entry_timing's AT_SUPPORT test (3% proximity) and structure.py's
level read (2%) both fired almost constantly, and the order ticket told you to exit on a
0.02% move.

Three changes fix it:

  1. **Zigzag pivots, not fractals.** A swing must be confirmed by a countermove scaled to the
     instrument's own volatility, so the pivots are the ones a person would draw.
  2. **Clustering.** Repeated tests of the same area collapse into ONE level whose strength
     grows with each touch — which is what makes a level a level.
  3. **Strength scoring.** Touch count, recency, and polarity flips (an old ceiling becoming a
     floor) rank levels, so `nearest_support` can skip a one-touch accident.

Pure: plain sequences in, dict out. No pandas, no IO.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from analysis.structure import _atr_pct, _swing_threshold, _zigzag


def _cfg(name: str, default):
    return getattr(config, name, default)


def _cluster(pivots: List[tuple], tol: float, n_bars: int) -> List[Dict]:
    """Collapse pivots that sit at the same price into one level.

    Pivots arrive as (index, price, 'H'|'L'). Two tests of $107 three weeks apart are one
    level tested twice, not two levels — and that repetition is exactly what distinguishes a
    real level from a random low.
    """
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: p[1])
    clusters: List[List[tuple]] = [[ordered[0]]]
    for piv in ordered[1:]:
        anchor = clusters[-1][0][1]
        if anchor > 0 and abs(piv[1] - anchor) / anchor <= tol:
            clusters[-1].append(piv)
        else:
            clusters.append([piv])

    out: List[Dict] = []
    for group in clusters:
        # Recency-weighted price: where the level sits NOW matters more than where it sat in
        # March, and levels drift.
        weights = [1.0 + (idx / n_bars) for idx, _, _ in group]
        price = sum(p * w for (_, p, _), w in zip(group, weights)) / sum(weights)
        last_idx = max(idx for idx, _, _ in group)
        kinds = {k for _, _, k in group}
        out.append({
            "price": round(price, 2),
            "touches": len(group),
            "last_touch_bars_ago": n_bars - 1 - last_idx,
            "flipped": len(kinds) > 1,      # acted as both floor and ceiling
        })
    return out


def _strength(level: Dict, n_bars: int) -> float:
    """0-100. Touches dominate, recency modulates, a polarity flip adds conviction.

    A level tested four times and defended last week is not the same object as a single low
    from five months ago, and ranking them identically is what made `nearest_support`
    unusable for anything that mattered.
    """
    touch_w = _cfg("LEVELS_TOUCH_WEIGHT", 22.0)
    flip_bonus = _cfg("LEVELS_FLIP_BONUS", 12.0)
    half_life = max(1.0, float(_cfg("LEVELS_RECENCY_HALFLIFE_BARS", 60)))

    score = min(70.0, touch_w * level["touches"])
    score += flip_bonus if level["flipped"] else 0.0
    # Exponential recency decay on the most recent touch.
    score *= 0.5 ** (level["last_touch_bars_ago"] / half_life)
    return round(max(0.0, min(100.0, score)), 1)


def find_levels(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    lookback: Optional[int] = None,
) -> Dict:
    """Detect support/resistance.

    Returns a dict that is a SUPERSET of the legacy shape, so existing consumers keep working:

        supports / resistances        list[float]  — legacy: prices, nearest-first
        nearest_support / _resistance float | None — legacy: closest QUALIFYING level
        52w_high / 52w_low            float

        support_levels / resistance_levels  list[dict] — new, with the evidence:
            {price, touches, last_touch_bars_ago, flipped, strength, distance_pct}

    `nearest_*` skips levels below LEVELS_MIN_STRENGTH so a one-touch accident cannot become
    the number the order ticket prints as an invalidation.
    """
    highs = [float(x) for x in (highs or []) if x is not None]
    lows = [float(x) for x in (lows or []) if x is not None]
    closes = [float(x) for x in (closes or []) if x is not None]
    n = min(len(highs), len(lows), len(closes))
    empty = {"supports": [], "resistances": [], "nearest_support": None,
             "nearest_resistance": None, "support_levels": [], "resistance_levels": [],
             "52w_high": None, "52w_low": None}
    if n < _cfg("LEVELS_MIN_BARS", 40):
        if n:
            empty["52w_high"] = round(max(highs[-252:]), 2)
            empty["52w_low"] = round(min(lows[-252:]), 2)
        return empty

    look = int(lookback or _cfg("LEVELS_LOOKBACK_BARS", 180))
    H, L, C = highs[-look:], lows[-look:], closes[-look:]
    nb = len(C)
    price = C[-1]

    piv = _zigzag(H, L, _swing_threshold(H, L, C))
    # Cluster width scales with volatility: a 1% band is one level on QQQ and three on AMD.
    tol = max(_cfg("LEVELS_CLUSTER_MIN_PCT", 0.010),
              _cfg("LEVELS_CLUSTER_ATR_MULT", 0.8) * _atr_pct(H, L, C))
    clusters = _cluster(piv, tol, nb)

    for lv in clusters:
        lv["strength"] = _strength(lv, nb)
        lv["distance_pct"] = round(abs(price - lv["price"]) / price, 4) if price else None

    min_strength = _cfg("LEVELS_MIN_STRENGTH", 12.0)
    sup = sorted([l for l in clusters if l["price"] < price],
                 key=lambda l: price - l["price"])
    res = sorted([l for l in clusters if l["price"] > price],
                 key=lambda l: l["price"] - price)

    def _nearest(levels):
        for lv in levels:
            if lv["strength"] >= min_strength:
                return lv["price"]
        return levels[0]["price"] if levels else None

    keep = int(_cfg("LEVELS_KEEP_PER_SIDE", 3))
    return {
        # Legacy shape — prices only, nearest first.
        "supports": [l["price"] for l in sup[:keep]],
        "resistances": [l["price"] for l in res[:keep]],
        "nearest_support": _nearest(sup),
        "nearest_resistance": _nearest(res),
        # New shape — the evidence behind each level.
        "support_levels": sup[:keep],
        "resistance_levels": res[:keep],
        "52w_high": round(max(highs[-252:]), 2),
        "52w_low": round(min(lows[-252:]), 2),
    }


def strike_cushion(short_strike: float, levels: Sequence[Dict], side: str,
                   min_buffer_pct: float = 0.0) -> Optional[Dict]:
    """Does a real level stand between spot and this short strike?

    This is the geometry that actually matters for a credit spread, and the one the old
    `strike_above_support` score had backwards. For a bull put you want support ABOVE the
    short strike, so price must break a defended level before the strike is threatened. For a
    bear call you want resistance BELOW the short call.

    `min_buffer_pct` discards shelters too thin to be real. Live SPY on 2026-08-05 offered a
    3-touch support 0.16% above a candidate strike: breaking that support lands directly on
    the strike, so it shields nothing. Callers that SPEND something for shelter (strike
    selection trades away ROC) should pass a floor; the scoring path leaves it at 0 because
    partial credit for a thin cushion is still informative there.

    Returns {level, strength, touches, buffer_pct} for the best shielding level, or None.
    """
    if not short_strike or not levels:
        return None
    if side == "put":
        shields = [l for l in levels if l["price"] > short_strike]
    else:
        shields = [l for l in levels if l["price"] < short_strike]
    if min_buffer_pct > 0:
        shields = [l for l in shields
                   if abs(l["price"] - short_strike) / short_strike >= min_buffer_pct]
    if not shields:
        return None
    best = max(shields, key=lambda l: l["strength"])
    return {
        "level": best["price"],
        "strength": best["strength"],
        "touches": best["touches"],
        "buffer_pct": round(abs(best["price"] - short_strike) / short_strike, 4),
    }
