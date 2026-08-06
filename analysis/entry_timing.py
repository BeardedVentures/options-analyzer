#!/usr/bin/env python3
"""
entry_timing.py — Pattern phase detector for VEGA entry timing.

For premium-selling structures the dollar credit collected for a GIVEN DELTA TARGET is
richest when the underlying is at the extreme of its short-term move: for a bull put, at
the bottom of a pullback inside an uptrend; for a bear call, at the top of a bounce inside
a downtrend. That is when put/call skew steepens and short-dated IV expands.

Scope note (important, and the reason the language here is conservative): VEGA targets a
DELTA BAND (0.16–0.30), not a fixed strike. At the dip the 0.25-delta strike moves down
with the stock, so the "same strike is worth far more" effect is NOT what this module
captures — the strike re-anchors. What is left is the IV-expansion and skew-steepening
component, which is real but materially smaller. Expect roughly 10–25% more credit for the
same delta and width, not the 30–60% you would see holding the strike fixed.

This is ADVISORY ONLY. It never blocks a trade — see strategies.evaluate(), where timing is
appended with advisory=True and is excluded from the `qualified` computation. That matters
because multi_strategy.py and lottery_scanner.py hard-return None on `not ev["qualified"]`;
a non-advisory timing row would silently delete bear call / condor / lottery candidates.

No IO — pure function of the existing tech dict. Fully testable without live data.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

try:
    # Single source of truth for the STRONG_UP|UP|NEUTRAL|DOWN|STRONG_DOWN vocabulary.
    from strategies import normalize_trend
except Exception:  # pragma: no cover - keeps this module importable standalone
    def normalize_trend(trend):  # type: ignore[misc]
        return str(trend).strip().lower() if trend else ""


# ── Phase labels ────────────────────────────────────────────────────────────────
PHASE_EXTENDED          = "EXTENDED"           # overbought in an uptrend — thin put premium
PHASE_EARLY_PULLBACK    = "EARLY_PULLBACK"     # first leg down — premium still compressed
PHASE_MID_PULLBACK      = "MID_PULLBACK"       # falling, approaching the entry window
PHASE_REVERSAL_SETUP    = "REVERSAL_SETUP"     # late pullback / late bounce — OPTIMAL
PHASE_OVERSOLD_BOUNCE   = "OVERSOLD_BOUNCE"    # deep oversold inside an uptrend — OPTIMAL (bull put)
PHASE_OVERBOUGHT_FADE   = "OVERBOUGHT_FADE"    # extended bounce inside a downtrend — OPTIMAL (bear call)
PHASE_AT_SUPPORT        = "AT_SUPPORT"         # at identifiable support — OPTIMAL (bull put)
PHASE_AT_RESISTANCE     = "AT_RESISTANCE"      # at identifiable resistance — OPTIMAL (bear call)
PHASE_EARLY_BOUNCE      = "EARLY_BOUNCE"       # bounce just starting — too early (bear call)
PHASE_RANGE_CENTER      = "RANGE_CENTER"       # mid-range — OPTIMAL (condor)
PHASE_RANGE_EDGE        = "RANGE_EDGE"         # directional extreme — poor (condor)
PHASE_TREND_CONFLICT    = "TREND_CONFLICT"     # RSI extreme is trend continuation, not a dip
PHASE_NEUTRAL           = "NEUTRAL"            # flat / unreadable

# Phase → (readiness label, icon) shown in the cockpit chip.
READINESS: Dict[str, Tuple[str, str]] = {
    PHASE_EXTENDED:        ("CAUTION", "⚠"),
    PHASE_EARLY_PULLBACK:  ("EARLY",   "⚠"),
    PHASE_EARLY_BOUNCE:    ("EARLY",   "⚠"),
    PHASE_MID_PULLBACK:    ("WATCH",   "●"),
    PHASE_REVERSAL_SETUP:  ("OPTIMAL", "✓"),
    PHASE_OVERSOLD_BOUNCE: ("OPTIMAL", "✓"),
    PHASE_OVERBOUGHT_FADE: ("OPTIMAL", "✓"),
    PHASE_AT_SUPPORT:      ("OPTIMAL", "✓"),
    PHASE_AT_RESISTANCE:   ("OPTIMAL", "✓"),
    PHASE_RANGE_CENTER:    ("OPTIMAL", "✓"),
    PHASE_RANGE_EDGE:      ("CAUTION", "⚠"),
    PHASE_TREND_CONFLICT:  ("CAUTION", "⚠"),
    PHASE_NEUTRAL:         ("NEUTRAL", "–"),
}

# Readiness levels that count as "timing is acceptable". EARLY / CAUTION fall outside and
# raise a warning — they never block. See module docstring.
TIMING_GATE_PASS = {"OPTIMAL", "NEUTRAL", "WATCH"}

# Severity ordering, so structure can cap or promote a momentum-derived readiness.
_RANK = {"CAUTION": 0, "EARLY": 1, "NEUTRAL": 2, "WATCH": 3, "OPTIMAL": 4}
_UNRANK = {v: k for k, v in _RANK.items()}

# Icon by readiness, so a structure-adjusted rating still gets the right glyph (the phase
# it came from may now disagree with the final rating).
_READINESS_ICON = {"OPTIMAL": "✓", "WATCH": "●", "NEUTRAL": "–", "EARLY": "⚠", "CAUTION": "⚠"}


def _cap(readiness: str, ceiling: str) -> str:
    """Structure vetoes momentum: never rate better than `ceiling`."""
    return _UNRANK[min(_RANK.get(readiness, 2), _RANK[ceiling])]


def _promote(readiness: str, floor: str) -> str:
    """Structure corroborates momentum: rate at least `floor`."""
    return _UNRANK[max(_RANK.get(readiness, 2), _RANK[floor])]


# What each chart shape means for each side of the premium trade. RSI alone cannot see any
# of this — a shallow pause inside an advance and the second peak of a double top can both
# print RSI 55 below the 20-day, and they call for opposite actions.
#
# ceiling = structure disagrees, hold the rating down; floor = structure agrees, lift it.
_STRUCTURE_POLICY = {
    "bull_put": {
        ("BULL_FLAG", "LATE"):  ("floor", "OPTIMAL",
                                 "the flag is mature — this is where put skew has repriced"),
        ("BULL_FLAG", "MID"):   ("floor", "WATCH",
                                 "the flag is developing; more premium if it extends"),
        ("BULL_FLAG", "EARLY"): ("ceiling", "EARLY",
                                 "the pause has barely started — puts are still cheap"),
        ("DOUBLE_TOP", None):   ("ceiling", "CAUTION",
                                 "price was rejected at a prior peak; a short put sits under "
                                 "a failed breakout, not under a resting flag"),
        ("UPTREND_EXTENDED", None): ("ceiling", "EARLY",
                                     "no pullback of size yet, so nothing has repriced"),
        ("DOWNTREND", None):    ("ceiling", "CAUTION",
                                 "lower highs and lower lows — the pullback is the trend"),
        ("DOUBLE_BOTTOM", None): ("floor", "OPTIMAL",
                                  "a second trough held; the floor under the short strike is real"),
    },
    "bear_call": {
        ("BEAR_FLAG", "LATE"):  ("floor", "OPTIMAL",
                                 "the bounce is mature — call premium has repriced"),
        ("BEAR_FLAG", "MID"):   ("floor", "WATCH", "the bounce is developing"),
        ("BEAR_FLAG", "EARLY"): ("ceiling", "EARLY",
                                 "the bounce has barely started — calls are still cheap"),
        ("DOUBLE_TOP", None):   ("floor", "OPTIMAL",
                                 "price was rejected at a prior peak — the ceiling above the "
                                 "short call is confirmed, not assumed"),
        ("DOUBLE_BOTTOM", None): ("ceiling", "CAUTION",
                                  "a base is forming; selling calls into it fights a floor"),
        ("UPTREND_EXTENDED", None): ("ceiling", "CAUTION",
                                     "higher highs and higher lows — wrong tape for short calls"),
    },
    "iron_condor": {
        ("RANGE", None):        ("floor", "OPTIMAL", "a defined band is exactly the condor's thesis"),
        ("BULL_FLAG", None):    ("ceiling", "WATCH", "a continuation shape threatens the call wing"),
        ("BEAR_FLAG", None):    ("ceiling", "WATCH", "a continuation shape threatens the put wing"),
        ("DOUBLE_TOP", None):   ("ceiling", "WATCH", "a directional reversal is in progress"),
        ("DOUBLE_BOTTOM", None): ("ceiling", "WATCH", "a directional reversal is in progress"),
        ("UPTREND_EXTENDED", None): ("ceiling", "CAUTION", "trending tape breaks a range trade"),
        ("DOWNTREND", None):    ("ceiling", "CAUTION", "trending tape breaks a range trade"),
    },
}


def _apply_structure(readiness: str, kind: str, structure: Optional[Dict],
                     phase: str = "") -> tuple[str, str]:
    """Fold the chart-shape read into the momentum-derived readiness.

    Returns (readiness, note). Two guards keep a heuristic from overruling an observation:
    a LOW-confidence read never moves the rating, and no shape can promote a trade whose
    REGIME contradicts the thesis. Without the second, a double bottom lifted a confirmed
    downtrend from CAUTION to OPTIMAL — the pattern is real, but "the tape is against you"
    is a measurement and "this looks like a base" is a guess.
    """
    if not structure:
        return readiness, ""
    pattern = structure.get("pattern")
    stage = structure.get("stage")
    policy = _STRUCTURE_POLICY.get(kind, {})
    rule = policy.get((pattern, stage)) or policy.get((pattern, None))
    if not rule:
        return readiness, ""
    if structure.get("confidence") == "LOW":
        return readiness, f"Structure ({structure.get('phrase','')}) noted but too weak to weigh."
    direction, bound, why = rule
    if direction == "floor" and phase == PHASE_TREND_CONFLICT:
        # The headline already names the pattern; don't repeat it here.
        return readiness, ("that pattern would normally favour this entry, but the regime "
                           "contradicts the thesis — not upgrading on a pattern read alone")
    moved = _cap(readiness, bound) if direction == "ceiling" else _promote(readiness, bound)
    return moved, why


def _cfg(name: str, default):
    return getattr(config, name, default)


def _rsi_phase_bull_put(
    rsi: float,
    macd_crossover: Optional[str],
    price: float,
    sma20: Optional[float],
    sma50: Optional[float],
    nearest_support: Optional[float],
    trend: Optional[str],
) -> Tuple[str, str]:
    """Bull-put pattern phase. Best credit is at the END of a pullback inside an uptrend —
    RSI in the 40s, price at/below SMA20 or on support. RSI 60+ means the pullback has not
    matured and put skew has not steepened yet."""
    oversold  = _cfg("BULL_PUT_OVERSOLD_RSI", 38)
    optimal   = _cfg("BULL_PUT_OPTIMAL_RSI_MAX", 52)
    early_min = _cfg("BULL_PUT_EARLY_RSI_MIN", 58)
    extended  = _cfg("BULL_PUT_EXTENDED_RSI_MIN", 68)
    supp_pct  = _cfg("ENTRY_TIMING_SUPPORT_PROXIMITY_PCT", 0.03)

    bucket = normalize_trend(trend)
    macd_bearish = (macd_crossover or "").lower() == "bearish"
    price_below_sma20 = bool(sma20) and bool(price) and price < sma20
    near_support = (
        bool(nearest_support) and bool(price)
        and abs(price - nearest_support) / price < supp_pct
    )

    def _depth() -> float:
        return ((sma20 - price) / sma20 * 100) if (sma20 and price) else 0.0

    # A low RSI is only a dip-buy if the higher-timeframe trend is still constructive.
    # In a downtrend it is a breakdown, and selling puts into it is catching a knife.
    # strategies.py already gates bull_put to up/flat, but this function is public and its
    # reason string must not assert an uptrend that is not there.
    if bucket == "down" and rsi < optimal:
        return PHASE_TREND_CONFLICT, (
            f"RSI {rsi:.0f} but the regime reads {str(trend).lower()} — this is trend "
            f"continuation, not a pullback inside an uptrend. Low RSI here is a breakdown; "
            f"the richer put premium is compensation for real downside risk, not an edge."
        )

    # Deep oversold inside an uptrend — maximum fear already realised.
    if rsi <= oversold:
        return PHASE_OVERSOLD_BOUNCE, (
            f"RSI {rsi:.0f} — deeply oversold within a constructive regime. Put skew is at "
            f"or near its local peak, so credit per unit of delta is close to its maximum."
        )

    # At support — a natural floor reference for the short strike.
    if near_support and rsi < optimal:
        return PHASE_AT_SUPPORT, (
            f"RSI {rsi:.0f}, price within {supp_pct:.0%} of support ${nearest_support:.2f}. "
            f"The short strike has a clear floor to sit beneath. Optimal zone."
        )

    # Late pullback — the flag is mature.
    if rsi < optimal - 4 and macd_bearish and price_below_sma20:
        return PHASE_REVERSAL_SETUP, (
            f"RSI {rsi:.0f}, price {_depth():.1f}% below SMA20, MACD bearish — the pullback "
            f"is mature and put skew has steepened. This is the entry window."
        )

    if rsi < optimal and price_below_sma20:
        return PHASE_REVERSAL_SETUP, (
            f"RSI {rsi:.0f}, price {_depth():.1f}% below SMA20 — late in the consolidation. "
            f"Timing is favourable."
        )

    # Mid-pullback — approaching the window.
    if rsi < early_min - 3 and macd_bearish:
        return PHASE_MID_PULLBACK, (
            f"RSI {rsi:.0f}, MACD bearish — pullback in progress. Timing is approaching "
            f"optimal; a further leg down or a support touch would improve the credit."
        )

    if rsi < early_min and price_below_sma20:
        return PHASE_MID_PULLBACK, (
            f"RSI {rsi:.0f}, price below SMA20 — mid-consolidation. Entry is reasonable; "
            f"somewhat more credit is available if the pullback deepens."
        )

    # Early pullback — premium has not repriced yet.
    if early_min <= rsi < extended and macd_bearish:
        return PHASE_EARLY_PULLBACK, (
            f"RSI {rsi:.0f} with MACD turning bearish — early in the pullback. Put skew has "
            f"not steepened yet; the same delta typically pays 10–25% more once RSI works "
            f"below {optimal:.0f}. Deferring risks the setup never triggering."
        )

    if early_min <= rsi < extended:
        return PHASE_EARLY_PULLBACK, (
            f"RSI {rsi:.0f} — early in the consolidation from recent highs. Put premium is "
            f"still compressed. Target zone: RSI < {optimal:.0f} or a touch of SMA20."
        )

    # Extended / overbought — the most compressed put premium.
    if rsi >= extended:
        return PHASE_EXTENDED, (
            f"RSI {rsi:.0f} — extended. Selling puts here collects the least credit per unit "
            f"of delta. If the uptrend is intact the pullback has not started, so this is the "
            f"wrong point in the cycle to be short puts."
        )

    return PHASE_NEUTRAL, f"RSI {rsi:.0f} — no clear pattern phase. Standard entry."


def _rsi_phase_bear_call(
    rsi: float,
    macd_crossover: Optional[str],
    price: float,
    sma20: Optional[float],
    sma50: Optional[float],
    nearest_resistance: Optional[float],
    trend: Optional[str],
) -> Tuple[str, str]:
    """Bear-call pattern phase — the inverse of bull put. Best credit is when the stock is
    bouncing INTO resistance inside a downtrend: RSI climbing toward 60–65, price reclaiming
    SMA20 from below, MACD crossing bullish. That is when call premium is richest."""
    optimal_min = _cfg("BEAR_CALL_OPTIMAL_RSI_MIN", 58)
    extended    = _cfg("BEAR_CALL_EXTENDED_RSI_MIN", 65)
    early_max   = _cfg("BEAR_CALL_EARLY_RSI_MAX", 45)

    bucket = normalize_trend(trend)
    macd_bullish = (macd_crossover or "").lower() == "bullish"
    price_above_sma20 = bool(sma20) and bool(price) and price > sma20
    prox = _cfg("ENTRY_TIMING_SUPPORT_PROXIMITY_PCT", 0.03)
    near_resistance = (
        bool(nearest_resistance) and bool(price)
        and abs(price - nearest_resistance) / price < prox
    )

    # A high RSI is only a fade candidate if the regime is actually bearish. In an uptrend
    # it is strength, and selling calls into it is fighting the tape.
    if bucket == "up" and rsi > optimal_min:
        return PHASE_TREND_CONFLICT, (
            f"RSI {rsi:.0f} but the regime reads {str(trend).lower()} — this is trend "
            f"strength, not a bounce inside a downtrend. Selling calls here fights the tape; "
            f"the richer call premium reflects genuine upside risk."
        )

    # At resistance — the mirror of the bull put's AT_SUPPORT branch. `nearest_resistance`
    # was accepted by this function and never read, so a bounce running straight into a
    # twice-rejected ceiling scored the same as one in open air.
    if near_resistance and rsi > optimal_min - 8:
        return PHASE_AT_RESISTANCE, (
            f"RSI {rsi:.0f}, price within {prox:.0%} of resistance ${nearest_resistance:.2f}. "
            f"The short call has a tested ceiling to sit above. Optimal zone."
        )

    # Extended bounce — maximum call premium.
    if rsi >= extended:
        return PHASE_OVERBOUGHT_FADE, (
            f"RSI {rsi:.0f} — extended to the upside within a weak regime. Call premium is at "
            f"or near its local peak. Strong bear-call entry timing."
        )

    # Mature bounce into resistance.
    if rsi > optimal_min and macd_bullish and price_above_sma20:
        return PHASE_REVERSAL_SETUP, (
            f"RSI {rsi:.0f}, price above SMA20, MACD bullish — the bounce is mature and call "
            f"premium has repriced upward. Optimal bear-call entry zone."
        )

    if rsi > optimal_min - 3 and price_above_sma20:
        return PHASE_REVERSAL_SETUP, (
            f"RSI {rsi:.0f}, price above SMA20 — bounce progressing into resistance, timing "
            f"is favourable."
        )

    # Mid-bounce — approaching. Reached whether or not price has reclaimed SMA20.
    if rsi > early_max + 5 and macd_bullish:
        return PHASE_MID_PULLBACK, (
            f"RSI {rsi:.0f}, MACD bullish — bounce in progress. Timing is approaching "
            f"optimal; call premium improves as RSI works above {optimal_min:.0f}."
        )

    if rsi > early_max + 5:
        return PHASE_MID_PULLBACK, (
            f"RSI {rsi:.0f} — mid-range within the downtrend. Entry is reasonable; more call "
            f"premium is available if the bounce extends."
        )

    # Early / no bounce — call premium compressed.
    if rsi <= early_max and macd_bullish:
        return PHASE_EARLY_BOUNCE, (
            f"RSI {rsi:.0f} with MACD just turning bullish — early in the bounce. Call skew "
            f"has not repriced; waiting for RSI > {optimal_min:.0f} typically pays 10–25% "
            f"more for the same delta."
        )

    return PHASE_EARLY_BOUNCE, (
        f"RSI {rsi:.0f} — deeply oversold within the downtrend. Call premium is compressed "
        f"down here; the bounce has not happened yet."
    )


def _rsi_phase_condor(rsi: float, trend: Optional[str]) -> Tuple[str, str]:
    """Iron condor wants a range-bound tape: mid-range RSI and no directional extreme."""
    lo = _cfg("CONDOR_RANGE_RSI_MIN", 44)
    hi = _cfg("CONDOR_RANGE_RSI_MAX", 57)
    if lo <= rsi <= hi:
        return PHASE_RANGE_CENTER, (
            f"RSI {rsi:.0f} — mid-range, neither wing is under immediate pressure. Condor "
            f"timing is favourable."
        )
    if rsi < lo - 4 or rsi > hi + 8:
        return PHASE_RANGE_EDGE, (
            f"RSI {rsi:.0f} — directional extreme. One wing is far closer to being tested "
            f"than the other; condor timing is poor here."
        )
    return PHASE_MID_PULLBACK, (
        f"RSI {rsi:.0f} — transitional, drifting off mid-range. Condor timing is acceptable "
        f"but not ideal."
    )


def assess_entry_timing(
    strategy: str,
    tech: Dict,
    current_price: Optional[float] = None,
    structure: Optional[Dict] = None,
) -> Dict:
    """Assess where the underlying sits in its pattern relative to the ideal entry window.

    Two independent reads are combined. Momentum (RSI/MACD/SMA) says how stretched price is;
    `structure` (from analysis/structure.py, optional) says what SHAPE the chart is making
    and how far through it we are. Structure can cap or promote the momentum rating — see
    _STRUCTURE_POLICY — because a shallow pause inside an advance and the second peak of a
    double top look identical to RSI and call for opposite actions.

    Returns:
        {
          "phase":            str,   # EXTENDED / EARLY_PULLBACK / REVERSAL_SETUP / ...
          "readiness":        str,   # OPTIMAL / WATCH / EARLY / CAUTION / NEUTRAL
          "readiness_icon":   str,   # glyph for the cockpit chip
          "timing_gate_pass": bool,  # False for EARLY / CAUTION — ADVISORY, never blocks
          "headline":         str,   # "Early in a bull flag" — the one-line chip text
          "reason":           str,   # human-readable explanation
          "rsi_at_signal":    float,
          "target_rsi":       str,   # guidance on when timing would be better
          "structure":        dict,  # the full structure read, {} when unavailable
          "momentum_readiness": str, # rating before structure was folded in
        }
    """
    tech = tech or {}
    rsi = tech.get("rsi")
    rsi = 50.0 if rsi is None else float(rsi)
    macd_crossover = tech.get("macd_crossover")
    price = current_price or tech.get("price") or tech.get("current_price") or 0.0
    sma20 = tech.get("sma20")
    sma50 = tech.get("sma50")
    nearest_support = tech.get("nearest_support")
    nearest_resistance = tech.get("nearest_resistance")
    trend = tech.get("trend")

    strat_lower = (strategy or "").lower().replace(" ", "_").replace("-", "_")

    # Order matters: check condor before the generic "put"/"call" substring tests, since an
    # iron condor contains both legs and would otherwise be misrouted.
    if "condor" in strat_lower:
        phase, reason = _rsi_phase_condor(rsi, trend)
        target_rsi = (f"RSI {_cfg('CONDOR_RANGE_RSI_MIN', 44)}-{_cfg('CONDOR_RANGE_RSI_MAX', 57)} "
                      f"(range-bound regime)")
        kind = "iron_condor"
    elif "bear_call" in strat_lower or "call_spread" in strat_lower:
        phase, reason = _rsi_phase_bear_call(
            rsi, macd_crossover, price, sma20, sma50, nearest_resistance, trend)
        target_rsi = f"RSI > {_cfg('BEAR_CALL_OPTIMAL_RSI_MIN', 58)} on a bounce into resistance"
        kind = "bear_call"
    elif "bull_put" in strat_lower or "put_spread" in strat_lower:
        phase, reason = _rsi_phase_bull_put(
            rsi, macd_crossover, price, sma20, sma50, nearest_support, trend)
        target_rsi = (f"RSI < {_cfg('BULL_PUT_OPTIMAL_RSI_MAX', 52)} with MACD bearish, "
                      f"or a support touch")
        kind = "bull_put"
    else:
        # Long-call lottery and anything unrecognised: no premium-selling timing thesis
        # applies, so stay silent rather than emitting a misleading chip.
        phase, reason = PHASE_NEUTRAL, "No entry-timing thesis applies to this structure."
        target_rsi = "n/a"
        kind = ""

    momentum_readiness = READINESS.get(phase, ("NEUTRAL", "–"))[0]
    readiness, struct_note = _apply_structure(momentum_readiness, kind, structure, phase)
    icon = _READINESS_ICON.get(readiness, "–")

    struct = structure or {}
    # An unreadable chart has nothing to say — fall back to the momentum phase rather than
    # headlining the cockpit chip with "Structure unreadable".
    phrase = (struct.get("phrase") or "") if struct.get("pattern") != "UNREADABLE" else ""
    headline = phrase[:1].upper() + phrase[1:] if phrase else \
        (phase.replace("_", " ").title() if phase != PHASE_NEUTRAL else "Standard entry")

    # Lead with the shape, then the measurement, then why it matters for this strategy.
    parts = []
    if phrase:
        detail = struct.get("detail") or ""
        parts.append(f"{headline}." + (f" {detail[:1].upper()}{detail[1:]}." if detail else ""))
    if struct_note:
        parts.append(f"{struct_note[:1].upper()}{struct_note[1:]}.")
    parts.append(reason)
    if readiness != momentum_readiness:
        parts.append(f"Momentum alone read {momentum_readiness}; structure moved it to {readiness}.")

    return {
        "phase": phase,
        "readiness": readiness,
        "readiness_icon": icon,
        "timing_gate_pass": readiness in TIMING_GATE_PASS,
        "headline": headline,
        "reason": " ".join(p for p in parts if p),
        "rsi_at_signal": round(rsi, 1),
        "target_rsi": target_rsi,
        "structure": struct,
        "momentum_readiness": momentum_readiness,
    }


if __name__ == "__main__":
    # Self-test. Each case carries its own price so the SMA20 relationship is the one the
    # case actually intends — the spec's original harness forced price below SMA20 for every
    # case, which made the bear-call bounce tests unreachable.
    tests = [
        # (label, strategy, tech, expected_readiness)
        ("bull put: deep oversold",  "bull_put_spread",
         {"rsi": 34, "macd_crossover": "bearish", "price": 196, "sma20": 200, "trend": "UP"}, "OPTIMAL"),
        ("bull put: late pullback",  "bull_put_spread",
         {"rsi": 44, "macd_crossover": "bearish", "price": 205, "sma20": 210, "trend": "UP"}, "OPTIMAL"),
        ("bull put: at support",     "bull_put_spread",
         {"rsi": 47, "macd_crossover": "bearish", "price": 201, "sma20": 210,
          "nearest_support": 200, "trend": "UP"}, "OPTIMAL"),
        ("bull put: mid pullback",   "bull_put_spread",
         {"rsi": 53, "macd_crossover": "bearish", "price": 196, "sma20": 200, "trend": "UP"}, "WATCH"),
        ("bull put: early pullback", "bull_put_spread",
         {"rsi": 62, "macd_crossover": "bearish", "price": 204, "sma20": 200, "trend": "UP"}, "EARLY"),
        ("bull put: extended",       "bull_put_spread",
         {"rsi": 70, "macd_crossover": "bullish", "price": 215, "sma20": 200, "trend": "STRONG_UP"}, "CAUTION"),
        ("bull put: knife catch",    "bull_put_spread",
         {"rsi": 30, "macd_crossover": "bearish", "price": 180, "sma20": 200, "trend": "STRONG_DOWN"}, "CAUTION"),
        ("bear call: overbought",    "bear_call_spread",
         {"rsi": 68, "macd_crossover": "bullish", "price": 184, "sma20": 180, "trend": "DOWN"}, "OPTIMAL"),
        ("bear call: mature bounce", "bear_call_spread",
         {"rsi": 60, "macd_crossover": "bullish", "price": 178, "sma20": 175, "trend": "DOWN"}, "OPTIMAL"),
        ("bear call: mid bounce",    "bear_call_spread",
         {"rsi": 52, "macd_crossover": "bullish", "price": 172, "sma20": 175, "trend": "DOWN"}, "WATCH"),
        ("bear call: early bounce",  "bear_call_spread",
         {"rsi": 38, "macd_crossover": "bearish", "price": 185, "sma20": 190, "trend": "DOWN"}, "EARLY"),
        ("bear call: fights tape",   "bear_call_spread",
         {"rsi": 66, "macd_crossover": "bullish", "price": 195, "sma20": 190, "trend": "STRONG_UP"}, "CAUTION"),
        ("condor: mid-range",        "iron_condor",
         {"rsi": 50, "macd_crossover": None, "price": 100, "sma20": 100, "trend": "NEUTRAL"}, "OPTIMAL"),
        ("condor: directional",      "iron_condor",
         {"rsi": 72, "macd_crossover": "bullish", "price": 110, "sma20": 100, "trend": "UP"}, "CAUTION"),
        ("lottery: no thesis",       "long_call_lottery",
         {"rsi": 62, "macd_crossover": "bullish", "price": 110, "sma20": 100, "trend": "UP"}, "NEUTRAL"),
        ("missing RSI defaults 50",  "bull_put_spread",
         {"macd_crossover": None, "price": 100, "sma20": 100, "trend": "UP"}, "NEUTRAL"),
    ]

    passed = 0
    for label, strategy, tech, expected in tests:
        r = assess_entry_timing(strategy, tech)
        ok = r["readiness"] == expected
        passed += ok
        print(f"{'OK ' if ok else '!! '}{label:26} -> {r['readiness']:8} "
              f"(expected {expected:8}) phase={r['phase']}")

    print(f"\n{passed}/{len(tests)} entry-timing tests passed")
    if passed != len(tests):
        raise SystemExit(1)
