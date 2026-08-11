#!/usr/bin/env python3
"""cross_venue.py — read an ETF's implied vol against the reference its own asset publishes.

An ETF's options and the underlying asset's own options price one risk in two venues. Where
the underlying has a published vol index, the gap between them is information the ETF's thin
IV history cannot supply: IBIT has three days of its own IV, and Bitcoin has a continuous
30-day index for free.

`btc_signal.py` did this for BTC with the thresholds and the narration hardcoded. This is the
same read for any asset that DECLARES a reference, and the generalisation is the point: every
value comes from `ticker_profile.DECLARED` and NOTHING is shared between assets.

THREE THINGS A SHARED IMPLEMENTATION GETS WRONG, all of them silent:

1. The noise floor. BTC's ordinary venue basis is ~1.5pp, so 2.0 is a sane floor. GLD's whole
   gap lives inside half a point, and GDX routinely sits 6+ points above GVZ purely on miner
   beta. One shared threshold would report gold noise as signal several times a week and hide
   every real move in GDX behind an unreachable bar.

2. Whether the reference is INDEPENDENT. GVZ is computed from GLD's own option chain. A gap
   there is not two venues disagreeing — it is our IV reconstruction disagreeing with CBOE's,
   and the likelier one to be wrong is ours. It is a data-quality check wearing an edge's
   clothes, and it must never be narrated as an opportunity.

3. Trading hours. DVOL is 24/7 and IBIT's options are not. A gap measured at 20:00 compares a
   live number against one frozen at the close, and the "widening" is the clock.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from analysis.ticker_profile import cross_venue as declared_cross_venue

logger = logging.getLogger(__name__)

# Readings. Deliberately not "buy"/"sell": this layer never enters the gates dict.
ALIGNED = "aligned"
ETF_RICH = "etf_rich"        # the ETF's surface is the expensive one — favourable to a seller
ETF_CHEAP = "etf_cheap"      # the reference is richer — the seller is writing the cheap side
QUALITY = "quality_check"    # reference is derived from the ETF; a gap means one of us is wrong
UNAVAILABLE = "unavailable"


def _cfg(name, default):
    return getattr(config, name, default)


def ref_value(raw) -> Optional[float]:
    """The number out of a reference, whichever shape it arrived in.

    Deribit DVOL is populated as a bare float; the published indices come from
    data.vol_indices as a dated dict. Both are legitimate and neither is worth normalising at
    the source — the dated dict exists precisely so a staleness check is possible, and
    flattening it to a float at populate time would throw that away.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def evaluate(ticker: str, proxy_atm_iv: Optional[float], ctx: Optional[Dict] = None) -> Dict:
    """The cross-venue read for one underlying, entirely from its DECLARED config.

    `proxy_atm_iv` is the ETF's ATM implied vol as a FRACTION (0.3272), matching how the rest
    of the codebase carries IV. References arrive in vol POINTS (34.24). The conversion happens
    here, once, because mixing the two units silently produces a hundred-fold error that still
    looks like a plausible number.

    Always the same shape. `available` is False whenever either side is missing, and callers
    must treat that as absence of information rather than a neutral reading.
    """
    ctx = ctx or {}
    tk = (ticker or "").upper()
    out: Dict = {
        "available": False, "ticker": tk, "enabled": False,
        "ref_name": None, "ref_signal": None, "source": None, "hours": None,
        "derived_from": None, "noise_floor_pp": None, "drivers": [],
        "proxy_iv_pp": None, "ref_pp": None, "gap_pp": None,
        "reading": UNAVAILABLE, "is_quality_check": False,
        "blocked_reason": None, "note": "",
    }

    cv = declared_cross_venue(tk)
    if cv is None:
        out["note"] = (f"{tk} has no declared cross-venue reference — its IV is not comparable "
                       f"to another venue's.")
        return out

    out.update({
        "enabled": cv["enabled"], "ref_name": cv["ref_name"], "ref_signal": cv["ref_signal"],
        "source": cv["source"], "hours": cv["hours"], "derived_from": cv["derived_from"],
        "noise_floor_pp": cv["noise_floor_pp"], "drivers": cv["drivers"],
        "blocked_reason": cv["blocked_reason"],
        "is_quality_check": _self_derived(tk, cv["derived_from"]),
    })

    if not cv["enabled"]:
        out["note"] = cv["blocked_reason"] or (
            f"{tk} has a declared reference ({cv['ref_name']}) that is not currently fed.")
        return out

    ref_pp = ref_value(ctx.get(cv["ref_signal"]))
    if ref_pp is None or proxy_atm_iv is None:
        missing = cv["ref_name"] if ref_pp is None else f"{tk} ATM IV"
        out["note"] = f"{missing} unavailable — no cross-venue read this cycle."
        return out

    proxy_pp = round(float(proxy_atm_iv) * 100, 2)
    gap = round(ref_pp - proxy_pp, 2)
    floor = float(cv["noise_floor_pp"] if cv["noise_floor_pp"] is not None else 2.0)

    out.update({
        "available": True,
        "proxy_iv_pp": proxy_pp,
        "ref_pp": round(ref_pp, 2),
        "gap_pp": gap,
        "reading": _reading(gap, floor, _self_derived(tk, cv["derived_from"])),
        "note": _narrate(tk, proxy_pp, ref_pp, gap, floor, cv),
    })
    # Provenance of the reference reading, so a card can say how old its number is. Absent for
    # the bare-float sources, which are live by construction.
    raw = ctx.get(cv["ref_signal"])
    if isinstance(raw, dict):
        out["ref_asof"] = raw.get("asof")
        out["ref_age_days"] = raw.get("age_days")
    return out


def _self_derived(ticker: str, derived_from: Optional[str]) -> bool:
    """Is the reference computed from THIS ticker's own options?

    The distinction is not pedantic and getting it wrong inverts the meaning of the card. GVZ
    is built from GLD's chain, so:

      GLD vs GVZ — the same options, priced twice. A gap is a reconstruction error, ours or
                   CBOE's, and calling it an edge would be claiming to arbitrage arithmetic.
      GDX vs GVZ — miners against bullion. Two genuinely different risks, and the spread is
                   real information about equity beta and operating leverage.

    Keying on `bool(derived_from)` alone put GDX in the first bucket and narrated an
    eleven-point miner-beta spread as "the likelier number to be wrong is ours".
    """
    if not derived_from:
        return False
    return str(derived_from).upper().startswith(str(ticker or "").upper())


def _reading(gap_pp: float, floor_pp: float, derived: bool) -> str:
    """Which surface is richer, from the perspective of someone SELLING premium on the ETF.

    gap = reference - proxy IV. Positive means the underlying's own options price more vol than
    the ETF's, so the seller is writing the cheaper of the two surfaces.

    A DERIVED reference can never produce a directional reading. GVZ is built from GLD's chain,
    so "GLD is rich versus GVZ" is not a trade — it is our number disagreeing with CBOE's on
    the same options, and the honest label says so.
    """
    if abs(gap_pp) < floor_pp:
        return ALIGNED
    if derived:
        return QUALITY
    return ETF_CHEAP if gap_pp > 0 else ETF_RICH


def _narrate(ticker: str, proxy_pp: float, ref_pp: float, gap: float,
             floor: float, cv: Dict) -> str:
    """One sentence a person can check against the two numbers beside it."""
    ref = cv["ref_name"] or cv["ref_signal"]
    if abs(gap) < floor:
        lead = (f"{ticker} at {proxy_pp:.1f} and {ref} at {ref_pp:.1f} are within "
                f"{abs(gap):.1f} vol points, inside this asset's {floor:.1f}pp noise floor — "
                f"the venues agree, so there is no cross-venue information either way.")
    elif _self_derived(ticker, cv["derived_from"]):
        lead = (f"{ticker} at {proxy_pp:.1f} differs from {ref} at {ref_pp:.1f} by "
                f"{abs(gap):.1f} vol points. {ref} is computed from "
                f"{cv['derived_from'].replace('_', ' ')} — the same options, priced twice — so "
                f"this is not two venues disagreeing. It is a reconstruction check, and the "
                f"likelier number to be wrong is ours.")
    elif cv["derived_from"]:
        lead = (f"{ticker} at {proxy_pp:.1f} sits {abs(gap):.1f} vol points "
                f"{'below' if gap > 0 else 'above'} {ref} at {ref_pp:.1f}. {ref} is computed "
                f"from {cv['derived_from'].replace('_', ' ')}, a different asset — the spread "
                f"is this name's own risk on top of that one, not a mispricing of either.")
    elif gap > 0:
        lead = (f"{ref} prices {gap:.1f} vol points MORE than {ticker}'s options "
                f"({ref_pp:.1f} vs {proxy_pp:.1f}) — selling {ticker} premium here is writing "
                f"the cheaper of the two surfaces.")
    else:
        lead = (f"{ticker} options price {abs(gap):.1f} vol points MORE than {ref} "
                f"({proxy_pp:.1f} vs {ref_pp:.1f}) — the ETF surface is the rich one.")

    # The clock caveat, stated only where it can actually bite.
    if cv.get("hours") == "24_7":
        lead += (f" {ref} trades continuously while {ticker}'s options do not, so a gap read "
                 f"outside equity hours is partly the clock.")
    return lead
