#!/usr/bin/env python3
"""
horizon.py — calibrates the technical read to the trade's own timeline and strategy.

Every structural module in VEGA analysed a fixed 180 days of history with fixed indicator
periods, and none of them — structure, levels, entry_timing, huginn — contained a single
reference to DTE. So a 25-day spread and a 45-day spread got an identical technical read,
and a support level six months out was weighted the same as one price will touch next week.

config.py already states the principle for volatility ("HV lookback should match expected
DTE so VRP is relevant to the holding period", VRP_HV_WINDOW). It was never extended to
structure. This module extends it.

Two questions are asked of every structural fact:

  1. IS IT REACHABLE? A level three expected-moves away cannot be tested before expiry, so it
     is not a risk and it is not a shield — it is scenery. Distance is measured in sigmas of
     the move the option market itself is pricing over the remaining life of THIS trade, not
     in percent and not in ATRs of arbitrary length.

  2. WILL IT RESOLVE IN TIME? A bull flag that needs another 20 bars to break out is
     irrelevant to a spread with 12 days left. The pattern may be real and still be useless,
     and a system that says "late in a bull flag" without checking the clock is describing a
     chart rather than a trade.

Strategy decides which side of the chart is even being asked about: a bull put is a claim
about the downside, a bear call about the upside, a condor about both.

Pure: numbers in, dict out.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

TRADING_DAYS = 252.0


def _cfg(name, default):
    return getattr(config, name, default)


def expected_move(spot: float, iv: float, dte: int, sigmas: float = 1.0) -> Optional[float]:
    """The 1-sigma move the option market is pricing over `dte` calendar days.

    spot * iv * sqrt(dte/365). This is the unit everything else here is measured in, because
    it is the only distance measure that already knows both how volatile this name is and how
    long this particular trade has to live.
    """
    if not spot or not iv or not dte or dte <= 0:
        return None
    return float(spot) * float(iv) * math.sqrt(float(dte) / 365.0) * sigmas


def sigmas_away(price: float, level: float, em: Optional[float]) -> Optional[float]:
    """Signed distance from price to a level, in expected moves. Negative = below."""
    if em is None or em <= 0 or price is None or level is None:
        return None
    return (float(level) - float(price)) / em


def classify_reach(sig: Optional[float]) -> str:
    """How much a level matters over this trade's life."""
    if sig is None:
        return "unknown"
    a = abs(sig)
    if a <= 0.5:
        return "in_play"        # price is essentially at it; it will be tested
    if a <= 1.0:
        return "likely_tested"  # inside the 1-sigma move
    if a <= 2.0:
        return "reachable"      # a 2-sigma event gets there
    return "out_of_reach"       # scenery for a trade this short


def calibrated_lookback_bars(dte: Optional[int]) -> int:
    """How much history is relevant to a trade of this length.

    Scaled to the horizon rather than fixed: a 25-day spread is decided by the last few
    months, not by what happened last winter. Clamped so the swing detector always has enough
    bars to find real pivots.
    """
    mult = float(_cfg("HORIZON_LOOKBACK_DTE_MULT", 4.0))
    lo = int(_cfg("HORIZON_LOOKBACK_MIN_BARS", 60))
    hi = int(_cfg("HORIZON_LOOKBACK_MAX_BARS", 250))
    if not dte or dte <= 0:
        return int(_cfg("STRUCTURE_LOOKBACK_DAYS", 180))
    return int(max(lo, min(hi, round(dte * mult))))


def strategy_sides(strategy: str) -> List[str]:
    """Which side(s) of the chart this structure is actually a claim about."""
    s = (strategy or "").lower()
    if "condor" in s:
        return ["down", "up"]
    if "bear_call" in s or "call_spread" in s:
        return ["up"]
    return ["down"]


# Roughly how much longer each shape needs before it resolves, expressed as a multiple of the
# bars it has already been forming. Continuation patterns tend to break out within about the
# time they took to build; a topping structure is already resolving.
_RESOLUTION_MULT = {
    "BULL_FLAG": 1.0, "BEAR_FLAG": 1.0,
    "DOUBLE_TOP": 0.5, "DOUBLE_BOTTOM": 0.5,
    "RANGE": 2.0,
    "UPTREND_EXTENDED": 1.5, "DOWNTREND": 1.5,
}


def pattern_fits_horizon(structure: Dict, dte: Optional[int]) -> Dict:
    """Will this pattern still be a pattern by expiry, or does the trade end first?

    A shape that needs another 20 bars to break out tells you nothing about a spread with 12
    days left — it is real and irrelevant at the same time, and that distinction never
    survived into any output before now.
    """
    out = {"fits": None, "bars_needed": None, "bars_available": None, "note": ""}
    if not structure or not dte or dte <= 0:
        return out
    pattern = structure.get("pattern")
    formed = structure.get("bars_since_pivot")
    mult = _RESOLUTION_MULT.get(pattern)
    # Calendar DTE -> trading bars.
    bars_available = int(round(dte * (TRADING_DAYS / 365.0)))
    out["bars_available"] = bars_available
    if mult is None or formed is None:
        out["note"] = "Pattern has no meaningful resolution clock."
        return out

    needed = int(round(max(1.0, formed * mult)))
    out["bars_needed"] = needed
    out["fits"] = needed <= bars_available
    if out["fits"]:
        out["note"] = (f"The shape has been building {formed} bars and typically resolves in "
                       f"about {needed} more; {bars_available} trading days remain, so it can "
                       f"play out inside this trade.")
    else:
        out["note"] = (f"The shape has been building {formed} bars and typically needs about "
                       f"{needed} more to resolve, but only {bars_available} trading days "
                       f"remain. It will not complete before expiry — real, but not "
                       f"actionable on this timeline.")
    return out


def calibrate(strategy: str, spot: float, iv: Optional[float], dte: Optional[int],
              short_strike: Optional[float] = None,
              support_levels: Optional[Sequence[Dict]] = None,
              resistance_levels: Optional[Sequence[Dict]] = None,
              structure: Optional[Dict] = None) -> Dict:
    """The full horizon-calibrated read for one proposed or open trade.

    Returns expected move over the remaining life, where the short strike sits inside it,
    which levels are actually in play on the side the strategy cares about, whether the
    detected pattern can resolve in time, and a plain sentence saying all of it.
    """
    em = expected_move(spot, iv, dte)
    sides = strategy_sides(strategy)
    out: Dict = {
        "dte": dte,
        "expected_move": round(em, 2) if em else None,
        "expected_move_pct": round(em / spot * 100, 2) if (em and spot) else None,
        "sides": sides,
        "lookback_bars": calibrated_lookback_bars(dte),
        "strike_sigmas": None,
        "strike_reach": "unknown",
        "levels_in_play": [],
        "pattern_horizon": pattern_fits_horizon(structure or {}, dte),
        "plain_english": "",
    }

    if short_strike is not None and em:
        sig = sigmas_away(spot, short_strike, em)
        out["strike_sigmas"] = round(sig, 2) if sig is not None else None
        out["strike_reach"] = classify_reach(sig)

    # Only the side(s) the strategy is a claim about.
    pool: List[Dict] = []
    if "down" in sides:
        pool += [dict(l, side="support") for l in (support_levels or [])]
    if "up" in sides:
        pool += [dict(l, side="resistance") for l in (resistance_levels or [])]
    for lv in pool:
        sig = sigmas_away(spot, lv.get("price"), em)
        reach = classify_reach(sig)
        if reach in ("in_play", "likely_tested", "reachable"):
            out["levels_in_play"].append({
                "side": lv.get("side"), "price": lv.get("price"),
                "touches": lv.get("touches"), "strength": lv.get("strength"),
                "sigmas": round(sig, 2) if sig is not None else None, "reach": reach,
            })
    out["levels_in_play"].sort(key=lambda l: abs(l["sigmas"] or 99))
    out["plain_english"] = _narrate(strategy, out, spot)
    return out


def _narrate(strategy: str, c: Dict, spot: float) -> str:
    """One paragraph a person can check against the chart, scoped to this trade's clock."""
    if not c.get("expected_move"):
        return "No implied volatility available, so the trade's own horizon cannot be measured."

    label = {"down": "downside", "up": "upside"}
    side_txt = " and ".join(label[s] for s in c["sides"])
    bits = [f"Over the {c['dte']} days left, the options market is pricing a move of about "
            f"±${c['expected_move']:,.2f} ({c['expected_move_pct']:.1f}%). "
            f"This structure is a claim about the {side_txt}."]

    sig, reach = c.get("strike_sigmas"), c.get("strike_reach")
    if sig is not None:
        if reach == "out_of_reach":
            bits.append(f"The short strike sits {abs(sig):.1f} expected moves away — beyond "
                        f"what the market prices as reachable before expiry.")
        elif reach == "reachable":
            bits.append(f"The short strike is {abs(sig):.1f} expected moves away — it takes an "
                        f"outsized move to reach it.")
        else:
            bits.append(f"The short strike is only {abs(sig):.1f} expected moves away — well "
                        f"inside the range the market is pricing, so it is genuinely in play.")

    lvls = c.get("levels_in_play") or []
    if lvls:
        top = lvls[0]
        bits.append(f"The nearest level that can actually be tested in this window is the "
                    f"{top['side']} at ${top['price']:,.2f} "
                    f"({top['touches']} touches, {abs(top['sigmas']):.1f}σ away).")
    else:
        bits.append("No support or resistance on that side is close enough to be tested "
                    "before expiry, so structure gives this trade neither help nor threat.")

    ph = c.get("pattern_horizon") or {}
    if ph.get("note"):
        bits.append(ph["note"])
    return " ".join(bits)
