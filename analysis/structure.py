#!/usr/bin/env python3
"""
structure.py — chart-structure reader for VEGA entry timing.

`entry_timing.py` answers "how stretched is momentum" from RSI/MACD/SMA. That is a scalar
read, and it cannot tell a shallow pause inside a strong advance from the second peak of a
double top — both can print RSI 55 below the 20-day. This module answers the different
question: **what shape is the chart making, and how far through that shape are we?**

It emits a phrase a trader can check against the chart in one glance — "early in a bull
flag", "late — second peak of a double top", "at support, third touch" — plus the measured
quantities behind it, so nothing is asserted that isn't computed.

Everything here is a heuristic over swing pivots. Heuristics misread charts, which is why
this output is advisory and why every reading carries an explicit `confidence`. Where a
pattern is ambiguous the module says so rather than guessing: UNREADABLE is a valid answer
and is strongly preferred over a confident wrong label.

Pure: plain sequences in, dict out. No pandas, no IO, no network — fully testable offline.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


# ── Pattern labels ──────────────────────────────────────────────────────────────
BULL_FLAG        = "BULL_FLAG"          # shallow pullback inside an advance
BEAR_FLAG        = "BEAR_FLAG"          # shallow bounce inside a decline
DOUBLE_TOP       = "DOUBLE_TOP"         # two comparable peaks, second rejected
DOUBLE_BOTTOM    = "DOUBLE_BOTTOM"      # two comparable troughs, second held
RANGE            = "RANGE"              # sideways band, no directional structure
UPTREND_EXTENDED = "UPTREND_EXTENDED"   # advancing with no pullback yet
DOWNTREND        = "DOWNTREND"          # lower highs and lower lows, still falling
PULLBACK         = "PULLBACK"           # a retracement that isn't a clean flag
UNREADABLE       = "UNREADABLE"         # not enough data, or no coherent shape

# ── Stage within the pattern ────────────────────────────────────────────────────
EARLY     = "EARLY"      # shape just started; premium has not repriced
MID       = "MID"        # developing
LATE      = "LATE"       # mature — the point premium sellers want
RESOLVING = "RESOLVING"  # shape is breaking one way or the other
NA        = "N/A"

# Swing structure labels
HH_HL = "HIGHER_HIGHS_HIGHER_LOWS"
LH_LL = "LOWER_HIGHS_LOWER_LOWS"
LH_HL = "CONTRACTING"      # lower highs into higher lows — coil / triangle
HH_LL = "BROADENING"       # widening swings — unstable
FLAT  = "FLAT"


def _cfg(name: str, default):
    return getattr(config, name, default)


# ── Primitives ──────────────────────────────────────────────────────────────────

def _atr_pct(highs: Sequence[float], lows: Sequence[float],
             closes: Sequence[float], window: int = 14) -> float:
    """Average true range as a fraction of price — the natural unit for "a move that counts"
    on this particular chart. A 3% wobble is noise in AMD and a real swing in WMT."""
    n = len(closes)
    if n < 2:
        return 0.0
    trs = []
    for i in range(max(1, n - window), n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if not trs or not closes[-1]:
        return 0.0
    return (sum(trs) / len(trs)) / closes[-1]


def _swing_threshold(highs, lows, closes) -> float:
    """Minimum leg size for a swing to be real, scaled to the instrument's own volatility
    and clamped so neither a placid ETF nor a violent single name produces nonsense."""
    mult = _cfg("STRUCTURE_ZIGZAG_ATR_MULT", 2.5)
    lo = _cfg("STRUCTURE_ZIGZAG_MIN_PCT", 0.03)
    hi = _cfg("STRUCTURE_ZIGZAG_MAX_PCT", 0.10)
    return max(lo, min(hi, mult * _atr_pct(highs, lows, closes)))


def _zigzag(highs: Sequence[float], lows: Sequence[float], threshold: float) -> List[Tuple[int, float, str]]:
    """Confirmed alternating swing pivots, as (index, price, 'H'|'L').

    Replaces k-bar fractals, which on daily data mark every two-day wiggle as a pivot. That
    made "the last two swing highs" meaningless: on a trending chart two adjacent noise
    pivots are as likely to be lower as higher, so QQQ at all-time highs classified as a
    DOWNTREND. A pivot here requires a countermove of `threshold` to confirm, so the swings
    it returns are the ones a person would actually draw.
    """
    n = min(len(highs), len(lows))
    if n < 3 or threshold <= 0:
        return []
    piv: List[Tuple[int, float, str]] = []
    direction = 0                       # +1 = seeking a high, -1 = seeking a low
    up_i, up_p = 0, highs[0]
    dn_i, dn_p = 0, lows[0]
    for i in range(1, n):
        if direction > 0:
            if highs[i] >= up_p:
                up_i, up_p = i, highs[i]
            elif up_p and (up_p - lows[i]) / up_p >= threshold:
                piv.append((up_i, up_p, "H"))
                direction, dn_i, dn_p = -1, i, lows[i]
        elif direction < 0:
            if lows[i] <= dn_p:
                dn_i, dn_p = i, lows[i]
            elif dn_p and (highs[i] - dn_p) / dn_p >= threshold:
                piv.append((dn_i, dn_p, "L"))
                direction, up_i, up_p = 1, i, highs[i]
        else:
            if highs[i] >= up_p:
                up_i, up_p = i, highs[i]
            if lows[i] <= dn_p:
                dn_i, dn_p = i, lows[i]
            if up_p and (up_p - lows[i]) / up_p >= threshold:
                piv.append((up_i, up_p, "H"))
                direction, dn_i, dn_p = -1, i, lows[i]
            elif dn_p and (highs[i] - dn_p) / dn_p >= threshold:
                piv.append((dn_i, dn_p, "L"))
                # Seed the next up-leg from this bar's HIGH, not its low — seeding from the
                # low understates the running extreme and delays the next pivot.
                direction, up_i, up_p = 1, i, highs[i]
    return piv




def _ema(values: Sequence[float], span: int) -> List[float]:
    if not values:
        return []
    a = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(a * float(v) + (1 - a) * out[-1])
    return out


def _macd_hist_series(closes: Sequence[float]) -> List[float]:
    """MACD histogram (12/26/9). The spec listed this as future work; it is the cleanest
    read on whether a pullback is still accelerating or has begun to flatten."""
    if len(closes) < 35:
        return []
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    macd = [f - s for f, s in zip(fast, slow)]
    signal = _ema(macd, 9)
    return [m - s for m, s in zip(macd, signal)]


def _momentum_flattening(closes: Sequence[float], bars: int = 3) -> Optional[bool]:
    """True when the MACD histogram is shrinking in magnitude over the last `bars` — the
    downward push is losing force, which is what "the flag is finishing" looks like."""
    hist = _macd_hist_series(closes)
    if len(hist) < bars + 1:
        return None
    tail = [abs(h) for h in hist[-(bars + 1):]]
    return all(tail[i] > tail[i + 1] for i in range(len(tail) - 1))


def _range_contracting(highs: Sequence[float], lows: Sequence[float], window: int) -> Optional[bool]:
    """True when the recent bar-range average is below the preceding window's. A healthy
    flag contracts; an expanding range during a pullback is distribution, not a pause."""
    if len(highs) < window * 2 or len(lows) < window * 2:
        return None
    def avg_range(h, l):
        rs = [a - b for a, b in zip(h, l)]
        return sum(rs) / len(rs) if rs else 0.0
    recent = avg_range(highs[-window:], lows[-window:])
    prior = avg_range(highs[-window * 2:-window], lows[-window * 2:-window])
    if prior <= 0:
        return None
    return recent < prior


def _volume_contracting(volumes: Optional[Sequence[float]], window: int) -> Optional[bool]:
    """A textbook flag sees volume dry up through the pause. Heavy volume on a pullback is
    supply changing hands — the opposite of a resting continuation pattern."""
    if not volumes or len(volumes) < window * 2:
        return None
    recent = sum(volumes[-window:]) / window
    prior = sum(volumes[-window * 2:-window]) / window
    if prior <= 0:
        return None
    return recent < prior


def _swing_structure(zh: List[Tuple[int, float]], zl: List[Tuple[int, float]]) -> str:
    """Classify the last two CONFIRMED swing highs and lows (zigzag, not raw fractals)."""
    if len(zh) < 2 or len(zl) < 2:
        return FLAT
    h_rising = zh[-1][1] > zh[-2][1]
    l_rising = zl[-1][1] > zl[-2][1]
    if h_rising and l_rising:
        return HH_HL
    if not h_rising and not l_rising:
        return LH_LL
    if not h_rising and l_rising:
        return LH_HL
    return HH_LL


def _level_read(price: float, highs: Sequence[float], lows: Sequence[float],
                supports: Optional[Sequence] = None,
                resistances: Optional[Sequence] = None) -> Optional[Dict]:
    """Nearest support/resistance the price is actually sitting on, with a touch count.
    A third touch of a level is materially more meaningful than a first.

    Accepts either bare prices or the rich level dicts from analysis/levels.py. Prefer the
    rich form: it carries the clustered touch count and strength, so this agrees with
    `nearest_support` instead of quietly disagreeing with it. Live AMT on 2026-08-05 read
    "at support $165.79 (12th touch)" from the raw price list while nearest_support was
    161.61 — two different answers to "which level are we on", from the same scan.
    """
    tol = _cfg("STRUCTURE_LEVEL_TOLERANCE_PCT", 0.02)
    if not price:
        return None

    # Announcing "at support" about a level the strength gate already rejected overstates it.
    # Live AMT 2026-08-05 headlined "at support $165.79" on a first touch scoring 11.7 — the
    # same level nearest_support had skipped for being under the 12.0 floor.
    min_str = _cfg("LEVELS_MIN_STRENGTH", 12.0)

    def _rich(levels, kind):
        out = []
        for lv in levels or ():
            if isinstance(lv, dict) and lv.get("price"):
                dist = abs(price - lv["price"]) / price
                strength = lv.get("strength")
                if dist <= tol and (strength is None or strength >= min_str):
                    out.append({"kind": kind, "price": round(float(lv["price"]), 2),
                                "distance_pct": round(dist, 4),
                                "touches": int(lv.get("touches") or 1),
                                "strength": strength})
        return out

    rich = _rich(supports, "support") + _rich(resistances, "resistance")
    if rich:
        return min(rich, key=lambda l: l["distance_pct"])

    def _is_rich(seq):
        return any(isinstance(x, dict) for x in (seq or ()))

    # Rich input that produced no qualifying level means "no level worth naming" — falling
    # through to the price-list path below would try to subtract a dict from a float.
    if _is_rich(supports) or _is_rich(resistances):
        return None

    best = None
    for kind, levels in (("support", supports or ()), ("resistance", resistances or ())):
        for lvl in levels:
            if not lvl:
                continue
            dist = abs(price - lvl) / price
            if dist <= tol and (best is None or dist < best["distance_pct"]):
                series = lows if kind == "support" else highs
                # Count distinct VISITS, not bars. A name that ranged near a level for two
                # months has one or two touches, not the 40-odd bars that sat inside the
                # tolerance band — consecutive near-bars are a single test of the level.
                touches, inside = 0, False
                for v in series:
                    near = abs(v - lvl) / lvl <= tol
                    if near and not inside:
                        touches += 1
                    inside = near
                best = {"kind": kind, "price": round(float(lvl), 2),
                        "distance_pct": round(dist, 4), "touches": int(touches)}
    return best


# ── Pattern detectors ───────────────────────────────────────────────────────────

def _double_pattern(closes, piv, zh, zl) -> Optional[Tuple[str, str, str]]:
    """Two comparable peaks (or troughs) separated by a meaningful trough (or peak).

    This is the case the RSI-only read cannot see, and the one Josh named: price back at a
    prior high with momentum no longer confirming is 'late', not 'extended'.
    """
    tol = _cfg("STRUCTURE_DOUBLE_TOLERANCE_PCT", 0.025)
    min_sep = _cfg("STRUCTURE_DOUBLE_MIN_BARS", 8)
    min_mid = _cfg("STRUCTURE_DOUBLE_MIN_MIDDLE_PCT", 0.03)
    price = closes[-1]

    if len(zh) >= 2:
        (i1, p1), (i2, p2) = zh[-2], zh[-1]
        mids = [p for i, p, k in piv if k == "L" and i1 < i < i2]
        if i2 - i1 >= min_sep and abs(p2 - p1) / max(p1, p2) <= tol and mids:
            trough = min(mids)
            if (max(p1, p2) - trough) / max(p1, p2) >= min_mid and price < max(p1, p2):
                depth = (max(p1, p2) - price) / max(p1, p2) * 100
                return (DOUBLE_TOP, LATE,
                        f"peaks at ${p1:,.2f} and ${p2:,.2f} ({i2 - i1} bars apart) with a "
                        f"${trough:,.2f} trough between; price {depth:.1f}% below the second")

    if len(zl) >= 2:
        (i1, t1), (i2, t2) = zl[-2], zl[-1]
        mids = [p for i, p, k in piv if k == "H" and i1 < i < i2]
        if i2 - i1 >= min_sep and abs(t2 - t1) / max(t1, t2) <= tol and mids:
            peak = max(mids)
            if (peak - min(t1, t2)) / peak >= min_mid and price > min(t1, t2):
                lift = (price - min(t1, t2)) / min(t1, t2) * 100
                return (DOUBLE_BOTTOM, LATE,
                        f"troughs at ${t1:,.2f} and ${t2:,.2f} ({i2 - i1} bars apart) with a "
                        f"${peak:,.2f} peak between; price {lift:.1f}% off the second")
    return None


def _flag(closes, piv, contracting) -> Optional[Tuple[str, str, str, float, float]]:
    """A flag is an impulse leg followed by a shallow, orderly counter-drift.

    Anchored on the last two CONFIRMED zigzag pivots, so the impulse is a leg someone would
    actually draw and `bars since` measures real elapsed time. With raw fractals the most
    recent detectable pivot is always ~k bars back, which made every flag report the same
    two-bar age regardless of the chart.

    Returns (pattern, stage, detail, impulse_pct, retracement_pct). Stage tracks how far the
    retracement has run — the direct proxy for how much premium has already repriced.
    """
    min_impulse = _cfg("STRUCTURE_FLAG_MIN_IMPULSE_PCT", 6.0)
    max_bars = _cfg("STRUCTURE_FLAG_MAX_BARS", 30)
    max_retrace = _cfg("STRUCTURE_FLAG_MAX_RETRACE_PCT", 70.0)
    price = closes[-1]
    n = len(closes)
    if len(piv) < 2:
        return None
    (i_prev, p_prev, k_prev), (i_last, p_last, k_last) = piv[-2], piv[-1]
    if k_prev == k_last:
        return None
    bars_since = n - 1 - i_last
    if bars_since > max_bars:
        return None
    range_note = (", range contracting" if contracting else
                  ", range still expanding" if contracting is False else "")

    # Bull flag: rally into a confirmed high, now drifting back down off it.
    if k_last == "H":
        lo, hi = p_prev, p_last
        if hi > lo > 0:
            impulse = (hi - lo) / lo * 100
            retrace = (hi - price) / (hi - lo) * 100
            if impulse >= min_impulse and 0 <= retrace <= max_retrace:
                stage = EARLY if retrace < 25 else MID if retrace < 50 else LATE
                return (BULL_FLAG, stage, bars_since,
                        f"{impulse:.1f}% advance off ${lo:,.2f} into ${hi:,.2f}, "
                        f"{retrace:.0f}% retraced over the {bars_since} bars since{range_note}",
                        impulse, retrace)

    # Bear flag: decline into a confirmed low, now bouncing off it.
    if k_last == "L":
        hi, lo = p_prev, p_last
        if hi > lo > 0:
            impulse = (hi - lo) / hi * 100
            retrace = (price - lo) / (hi - lo) * 100
            if impulse >= min_impulse and 0 <= retrace <= max_retrace:
                stage = EARLY if retrace < 25 else MID if retrace < 50 else LATE
                return (BEAR_FLAG, stage, bars_since,
                        f"{impulse:.1f}% decline off ${hi:,.2f} into ${lo:,.2f}, "
                        f"{retrace:.0f}% recovered over the {bars_since} bars since{range_note}",
                        impulse, retrace)
    return None


def _is_range(highs, lows, closes) -> Optional[str]:
    """Sideways band: the whole recent window fits inside a narrow envelope."""
    window = _cfg("STRUCTURE_RANGE_WINDOW", 30)
    max_band = _cfg("STRUCTURE_RANGE_MAX_BAND_PCT", 8.0)
    if len(closes) < window:
        return None
    hi, lo = max(highs[-window:]), min(lows[-window:])
    if lo <= 0:
        return None
    band = (hi - lo) / lo * 100
    if band <= max_band:
        pos = (closes[-1] - lo) / (hi - lo) * 100 if hi > lo else 50.0
        where = "upper" if pos > 66 else "lower" if pos < 33 else "middle"
        return (f"{band:.1f}% band over {window} bars, price in the {where} third")
    return None


# ── Public entry point ──────────────────────────────────────────────────────────

def detect_structure(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Optional[Sequence[float]] = None,
    supports: Optional[Sequence[float]] = None,
    resistances: Optional[Sequence[float]] = None,
) -> Dict:
    """Read the chart's shape and how far through it price has travelled.

    Returns:
        {
          "pattern":          str,    # BULL_FLAG / DOUBLE_TOP / RANGE / ...
          "stage":            str,    # EARLY / MID / LATE / RESOLVING / N/A
          "phrase":           str,    # "early in a bull flag" — the chip text
          "detail":           str,    # measured evidence behind the phrase
          "swing_structure":  str,
          "impulse_pct":      float | None,
          "retracement_pct":  float | None,
          "level":            {kind, price, distance_pct, touches} | None,
          "contracting":      bool | None,
          "volume_drying":    bool | None,
          "momentum_flattening": bool | None,
          "confidence":       str,    # HIGH / MEDIUM / LOW
        }
    """
    min_bars = _cfg("STRUCTURE_MIN_BARS", 40)

    highs = [float(x) for x in (highs or []) if x is not None]
    lows = [float(x) for x in (lows or []) if x is not None]
    closes = [float(x) for x in (closes or []) if x is not None]
    n = min(len(highs), len(lows), len(closes))
    highs, lows, closes = highs[-n:], lows[-n:], closes[-n:]

    blank = {
        "pattern": UNREADABLE, "stage": NA, "phrase": "structure unreadable",
        "detail": "", "swing_structure": FLAT, "impulse_pct": None,
        "retracement_pct": None, "level": None, "contracting": None,
        "volume_drying": None, "momentum_flattening": None, "confidence": "LOW",
    }
    if n < min_bars:
        blank["detail"] = f"only {n} bars available; {min_bars} needed for a structure read"
        return blank

    price = closes[-1]
    threshold = _swing_threshold(highs, lows, closes)
    piv = _zigzag(highs, lows, threshold)
    zh = [(i, p) for i, p, kind in piv if kind == "H"]
    zl = [(i, p) for i, p, kind in piv if kind == "L"]
    swing = _swing_structure(zh, zl)
    window = _cfg("STRUCTURE_CONTRACTION_WINDOW", 10)
    contracting = _range_contracting(highs, lows, window)
    vol_drying = _volume_contracting(volumes, window)
    flattening = _momentum_flattening(closes)
    level = _level_read(price, highs, lows, supports, resistances)

    pattern, stage, detail = UNREADABLE, NA, ""
    impulse_pct = retrace_pct = None
    bars_since_pivot = None
    confidence = "LOW"

    # Priority: a named reversal shape beats a generic continuation read, because it is the
    # more specific claim and the one that changes what a premium seller should do.
    dbl = _double_pattern(closes, piv, zh, zl)
    if dbl:
        pattern, stage, detail = dbl
        confidence = "MEDIUM"
    else:
        flag = _flag(closes, piv, contracting)
        if flag:
            pattern, stage, bars_since_pivot, detail, impulse_pct, retrace_pct = flag
            confidence = "HIGH" if (contracting and swing in (HH_HL, LH_LL)) else "MEDIUM"
            # Coherence: a flag is a PAUSE inside a trend, so it has to agree with the swing
            # structure. A "bull flag" printing lower highs into lower lows is the detector
            # fitting a continuation label onto a breakdown — exactly the misread that would
            # talk someone into selling puts under a failing chart. Demote it to LOW so
            # entry_timing reports it and refuses to act on it.
            expected = {BULL_FLAG: LH_LL, BEAR_FLAG: HH_HL}[pattern]
            deep = _cfg("STRUCTURE_FLAG_DEEP_RETRACE_PCT", 62.0)
            if swing == expected:
                confidence = "LOW"
                detail += "; swing structure contradicts the flag"
            elif retrace_pct is not None and retrace_pct >= deep:
                confidence = "LOW"
                detail += f"; {retrace_pct:.0f}% retracement is near failure, not a pause"
        else:
            rng = _is_range(highs, lows, closes)
            if rng:
                pattern, stage, detail, confidence = RANGE, MID, rng, "MEDIUM"
            elif swing == HH_HL:
                pattern, stage, confidence = UPTREND_EXTENDED, EARLY, "MEDIUM"
                detail = "higher highs and higher lows, no pullback of size yet"
            elif swing == LH_LL:
                pattern, stage, confidence = DOWNTREND, MID, "MEDIUM"
                detail = "lower highs and lower lows, decline still in force"
            else:
                # No confirmed swings at all. A relentless one-way move never produces a
                # countermove big enough to mark a pivot, so the swing test says FLAT — but
                # "advancing with no pullback yet" is the single most important thin-premium
                # state for a put seller, and reporting it as UNREADABLE threw it away.
                # Fall back to net displacement plus where price sits in its own range.
                trend_min = _cfg("STRUCTURE_TREND_MIN_NET_PCT", 8.0)
                net = ((price - closes[0]) / closes[0] * 100) if closes[0] else 0.0
                hi_w, lo_w = max(highs), min(lows)
                pos = ((price - lo_w) / (hi_w - lo_w)) if hi_w > lo_w else 0.5
                if net >= trend_min and pos >= 0.8:
                    pattern, stage, confidence = UPTREND_EXTENDED, EARLY, "MEDIUM"
                    detail = (f"{net:.1f}% higher over the window with no pullback big enough "
                              f"to mark a swing; price in the top {(1 - pos) * 100:.0f}% of its range")
                elif net <= -trend_min and pos <= 0.2:
                    pattern, stage, confidence = DOWNTREND, MID, "MEDIUM"
                    detail = (f"{abs(net):.1f}% lower over the window with no bounce big enough "
                              f"to mark a swing; price in the bottom {pos * 100:.0f}% of its range")
                else:
                    blank["swing_structure"] = swing
                    blank["level"] = level
                    blank["contracting"] = contracting
                    blank["detail"] = "no coherent pattern in the recent swings"
                    return blank

    # A mature shape sitting on a real level is the highest-information state there is —
    # promote it into the phrase rather than burying it in the detail line.
    phrase = _phrase(pattern, stage, level)

    # A LATE flag whose momentum has stopped falling is the textbook "flag is finishing"
    # tell. Record it; entry_timing uses it to firm up an OPTIMAL call.
    if pattern in (BULL_FLAG, BEAR_FLAG) and stage == LATE and flattening:
        confidence = "HIGH"

    return {
        "pattern": pattern, "stage": stage, "phrase": phrase, "detail": detail,
        "swing_structure": swing,
        "impulse_pct": round(impulse_pct, 1) if impulse_pct is not None else None,
        "retracement_pct": round(retrace_pct, 1) if retrace_pct is not None else None,
        "bars_since_pivot": bars_since_pivot,
        "level": level, "contracting": contracting, "volume_drying": vol_drying,
        "momentum_flattening": flattening, "confidence": confidence,
    }


_PATTERN_WORDS = {
    BULL_FLAG:        "a bull flag",
    BEAR_FLAG:        "a bear flag",
    DOUBLE_TOP:       "a double top",
    DOUBLE_BOTTOM:    "a double bottom",
    RANGE:            "a range",
    UPTREND_EXTENDED: "an extended uptrend",
    DOWNTREND:        "a downtrend",
    PULLBACK:         "a pullback",
    UNREADABLE:       "no clear pattern",
}

_STAGE_WORDS = {EARLY: "early in", MID: "midway through", LATE: "late in",
                RESOLVING: "resolving out of"}


def _phrase(pattern: str, stage: str, level: Optional[Dict]) -> str:
    """The one-line chip text: '<stage> <pattern>[, at <level>]'."""
    if pattern in (DOUBLE_TOP, DOUBLE_BOTTOM):
        base = f"second peak of {_PATTERN_WORDS[pattern]}" if pattern == DOUBLE_TOP \
            else f"second trough of {_PATTERN_WORDS[pattern]}"
    elif pattern == UPTREND_EXTENDED:
        base = "extended, no pullback yet"
    elif pattern == DOWNTREND:
        base = "in a downtrend"
    elif pattern == RANGE:
        base = "range-bound"
    else:
        base = f"{_STAGE_WORDS.get(stage, '')} {_PATTERN_WORDS.get(pattern, pattern)}".strip()
    if level:
        base += f", at {level['kind']} ${level['price']:,.2f} ({_ordinal(level['touches'])} touch)"
    return base


def _ordinal(n: int) -> str:
    """1 -> first ... then 4th, 21st, 42nd. Naive f'{n}th' produced '41th'."""
    words = {1: "first", 2: "second", 3: "third"}
    if n in words:
        return words[n]
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")
