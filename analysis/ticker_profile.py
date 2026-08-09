#!/usr/bin/env python3
"""
ticker_profile.py — what this system knows about THIS underlying, as opposed to underlyings.

THE PROBLEM
Every gate in REQUIRED_GATES is a textbook rule applied identically to 56 names: 0.30 delta cap,
15% credit-to-width, 25-45 DTE, $25 credit, 5% OTM buffer. They answer "is this spread
well-FORMED". None of them answers "is this a good setup FOR THIS ASSET, right now" — and the
difference is not academic. Two examples this system has already produced:

  · MIN_CREDIT_USD $25 was 0.03% of spot on SPY and 0.68% on IBIT. Same rule, twenty times
    stricter, purely because of share price. It kept IBIT out of the book for its entire life.
  · estimate_atm_iv's near-ATM window was a flat 3% of spot: 138 contracts on SPY, 10 on IBIT.
    Same rule, a fourteen-fold difference in how trustworthy the resulting number is.

Both were invisible until someone asked why one specific ticker behaved oddly. A profile makes
the per-asset character explicit and measurable instead of leaving it implicit in constants
that were chosen with a $500 stock in mind.

TWO KINDS OF KNOWLEDGE, DELIBERATELY SEPARATED

  DECLARED — structural facts a human knows and data cannot infer quickly. That IBIT holds spot
  Bitcoin and therefore has no earnings and no idiosyncratic business risk; that COIN is an
  operating company whose vol is levered to crypto but is not crypto; that TLT is a rates
  instrument. These are stated once, and they are claims about the WORLD.

  LEARNED — what this system has actually observed about the name: its own IV distribution, its
  realised vol, the credit its chain typically supports, how often its chain is even readable.
  These are claims about the DATA, and they carry their own sample size.

The separation matters because they fail differently. A declared fact is wrong if the world
changes; a learned fact is wrong if the sample is thin. Mixing them produces a profile that
cannot tell you which kind of wrong it is.

HONEST ABOUT WHAT IT DOES NOT KNOW
`confidence` is driven by observation count, and a name with three observations says so rather
than reporting a percentile. IBIT has 3 IV observations, all from this week; SPY has 23. A
profile that presented both as "IV rank" would be laundering a guess into a number. This is the
same discipline muninn.py applies to recovery rates and for the same reason.

ADVISORY. Nothing here gates anything. It is measurement and context, surfaced so the operator
and the calibration engine can both see it. Turning any of it into a rule is a separate,
deliberate decision that should be made from graded outcomes, not from a plausible story.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _cfg(name, default):
    return getattr(config, name, default)


# ── DECLARED: facts about the world, not about our data ───────────────────────
# Keep this small and only for things that genuinely change how a setup should be read. A
# profile that restates the watchlist note adds nothing; one that says "this name has no
# earnings because it holds a commodity" changes which gates are meaningful.
DECLARED: Dict[str, Dict] = {
    "IBIT": {
        "kind": "commodity_etf",
        "tracks": "BTC",
        "has_earnings": False,
        "reference_vol": "deribit_dvol",
        "note": ("Holds spot Bitcoin. No earnings, no business risk, no sector rotation — its "
                 "entire distribution is BTC's. Its IV should be read against BTC's own options "
                 "market (DVOL), which is why the cross-venue gap exists: with 3 days of IV "
                 "history of its own, borrowing Bitcoin's is the only honest reference."),
    },
    "COIN": {
        "kind": "equity",
        "tracks": "crypto_beta",
        "has_earnings": True,
        "reference_vol": None,
        "note": ("An operating company levered to crypto, NOT a Bitcoin tracker. Its IV ran 31 "
                 "vol points above BTC's on 2026-08-09 — that gap measures the company, not a "
                 "mispricing of Bitcoin, which is why it is excluded from the cross-venue read. "
                 "It carries earnings; IBIT does not."),
    },
    "TLT": {
        "kind": "bond_etf", "tracks": "long_rates", "has_earnings": False,
        "reference_vol": None,
        "note": "Long-duration Treasuries. Moves on rates, not on equity risk appetite.",
    },
    "SPY": {
        "kind": "broad_market_etf", "tracks": "sp500", "has_earnings": False,
        "reference_vol": "vix",
        "note": ("The reference asset. Deepest chain on the watchlist, penny-wide strikes near "
                 "the money, and its ATM IV should track VIX closely — a large divergence is a "
                 "data problem, not a market one."),
    },
    "QQQ": {
        "kind": "broad_market_etf", "tracks": "nasdaq100", "has_earnings": False,
        "reference_vol": "vxn",
        "note": "Nasdaq-100. Structurally higher vol than SPY; do not read its IV on SPY's scale.",
    },
}


def declared(ticker: str) -> Dict:
    d = dict(DECLARED.get((ticker or "").upper(), {}))
    d.setdefault("kind", "unknown")
    d.setdefault("has_earnings", None)   # None = unknown, NOT False. Absence is not a negative.
    return d


# ── LEARNED: what we have actually observed about this name ───────────────────

def _iv_history(ticker: str) -> List[Dict]:
    f = BASE_DIR / _cfg("IV_HISTORY_DIR", "data/iv_history") / f"{ticker.upper()}.json"
    try:
        rows = json.loads(f.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _chain_quality(ticker: str) -> Optional[float]:
    """How readable this ticker's chain has actually been, from the quality log."""
    try:
        from data import data_quality_log as dq
        rows = [r for r in dq.read_recent(2000) if r.get("ticker") == ticker.upper()]
        if not rows:
            return None
        return round(sum(r.get("usable_ratio", 0) for r in rows) / len(rows), 3)
    except Exception:
        return None


def learned(ticker: str, close=None) -> Dict:
    """Observed character. Every field is None until there is enough to say it."""
    from data import technicals

    hist = _iv_history(ticker)
    ivs = [float(r["iv"]) for r in hist if isinstance(r, dict) and r.get("iv")]

    rv = None
    if close is not None:
        try:
            v = technicals._historical_vol(close, 30)
            rv = round(float(v) * 100, 2) if v else None
        except Exception:
            rv = None

    # Drop implausible stored observations before describing the name, using the same rule
    # calculate_iv_rank applies — one definition of "is this reading believable".
    clean, dropped = (ivs, 0)
    if close is not None and ivs:
        try:
            clean, dropped = technicals._plausible_iv_samples(ivs, close)
        except Exception:
            clean, dropped = ivs, 0

    n = len(clean)
    min_n = int(_cfg("PROFILE_MIN_OBSERVATIONS", 20))
    out = {
        "iv_observations": n,
        "iv_observations_dropped": dropped,
        "iv_median_pct": round(sorted(clean)[n // 2] * 100, 2) if n else None,
        "iv_low_pct": round(min(clean) * 100, 2) if n else None,
        "iv_high_pct": round(max(clean) * 100, 2) if n else None,
        "realised_vol_pct": rv,
        "chain_usable_ratio": _chain_quality(ticker),
        "sufficient": n >= min_n,
        "min_observations": min_n,
    }
    out["confidence"] = ("none" if n == 0 else
                         "low" if n < min_n // 2 else
                         "provisional" if n < min_n else "usable")
    return out


# A profile is a property of the TICKER, not of a candidate, but assess() runs once per
# surviving candidate — five times for SPY on a normal scan. Each call re-read the ticker's IV
# history and scanned the whole data-quality log for the same answer. Cached for the process,
# which is one scan: auto_paper_cycle is a fresh process per cycle, so the cache cannot go
# stale across runs and there is nothing to invalidate.
_profile_cache: Dict[str, Dict] = {}


def clear_cache() -> None:
    """For tests and for any caller that wants a genuinely fresh read."""
    _profile_cache.clear()


def profile(ticker: str, close=None) -> Dict:
    """The full picture for one underlying: what we were told, what we have seen, and how far
    either can be trusted."""
    key = (ticker or "").upper()
    if key in _profile_cache:
        return _profile_cache[key]
    d, l = declared(ticker), learned(ticker, close)
    p = {"ticker": (ticker or "").upper(), "declared": d, "learned": l}
    p["headline"] = _headline(p)
    p["cautions"] = _cautions(p)
    _profile_cache[key] = p
    return p


def _headline(p: Dict) -> str:
    d, l = p["declared"], p["learned"]
    bits = []
    if d.get("note"):
        bits.append(d["note"])
    if l["confidence"] == "none":
        bits.append("No IV history recorded yet — nothing can be said about whether today's "
                    "premium is rich or cheap for this name.")
    elif not l["sufficient"]:
        bits.append(f"Only {l['iv_observations']} usable IV observations "
                    f"({l['min_observations']} needed) — its own IV range is not yet known, so "
                    f"any richness read is borrowed or approximate.")
    elif l["iv_median_pct"] is not None:
        bits.append(f"Typical ATM IV runs {l['iv_median_pct']:.0f}% "
                    f"(seen {l['iv_low_pct']:.0f}-{l['iv_high_pct']:.0f}%)"
                    + (f" against {l['realised_vol_pct']:.0f}% realised."
                       if l["realised_vol_pct"] else "."))
    return " ".join(bits)


def _cautions(p: Dict) -> List[str]:
    """The things that would make a textbook read of this name wrong. This is the part that
    earns the module: a caution is a place where the generic rule and the specific asset
    disagree."""
    d, l, out = p["declared"], p["learned"], []

    if not l["sufficient"] and l["iv_observations"] > 0:
        out.append(f"IV rank is unreliable here — {l['iv_observations']} of "
                   f"{l['min_observations']} observations. Treat any IV-rank gate as untested "
                   f"for this ticker.")
    if l["iv_observations_dropped"]:
        out.append(f"{l['iv_observations_dropped']} stored IV observations were dropped as bad "
                   f"quotes; the remaining sample is smaller than the file suggests.")
    if d.get("has_earnings") is False:
        out.append("No earnings, so the earnings gate can never bind — it is not evidence of "
                   "safety on this name, just an inapplicable rule.")
    if d.get("reference_vol") == "deribit_dvol":
        out.append("Read its IV against BTC's DVOL rather than its own thin history — the "
                   "cross-venue gap on the Bitcoin page is the intended reference.")
    ratio = l.get("chain_usable_ratio")
    if ratio is not None and ratio < float(_cfg("CHAIN_QUALITY_GOOD_RATIO", 0.70)):
        out.append(f"Its chain has averaged only {ratio:.0%} quotable — every signal derived "
                   f"from it describes the survivors more than the market.")
    return out
