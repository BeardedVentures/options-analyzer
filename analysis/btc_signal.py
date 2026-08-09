#!/usr/bin/env python3
"""
btc_signal.py — the cross-venue volatility read between Bitcoin and IBIT.

THE IDEA
IBIT options and BTC options price the same underlying risk in two different venues, quoted by
different participants, on different clocks. Deribit publishes DVOL — BTC's 30-day implied vol —
for free. IBIT's ATM IV comes off a chain VEGA already fetches. The difference between them is a
real, measurable quantity that costs nothing to obtain.

WHY THIS BEFORE A DIRECTIONAL FORECAST
A direction call has to beat a coin flip before it is worth anything, and proving that takes
sixty cycles. A spread only has to be MEASURABLE to be informative. This ships value immediately
and carries no validation debt.

WHY IBIT AND NOT COIN
Measured 2026-08-09: DVOL 34.24 vs IBIT ATM IV 32.72 — a 1.52 vol-point gap between two prices
for near-identical risk. Against COIN the same day: 65.23 vs 34.24, a 31-point gap. COIN is an
operating company with equity-specific risk, not a BTC tracker, and comparing its IV to BTC's
measures the difference in the assets rather than a difference in the pricing of one asset. Only
tickers in config.BTC_PROXY_TICKERS get this read.

ADVISORY BY CONSTRUCTION
This never appears in the gates dict. `qualified = all(gates.values())`, so a signal that is not
a gate cannot block a trade no matter what it says — structurally advisory rather than
advisory-by-flag, which is the stronger guarantee. It lands in the analysis block and on the
trade record, where the calibration engine can grade it once trades carrying it have closed.

THRESHOLDS ARE PROVISIONAL AND SAY SO
The band edges below are reasoned, not fitted — nothing has been graded yet. The RAW numbers are
what get persisted; the label is a convenience. When enough IBIT trades carry `iv_gap_pp` into
the ledger, the calibration engine sets these from outcomes and the provisional flag comes off.
Inventing a confident threshold now would launder a guess into something that reads as evidence.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def _cfg(name, default):
    return getattr(config, name, default)


def applies_to(ticker: str) -> bool:
    """Only underlyings that track BTC directly. See the module docstring on COIN."""
    if not _cfg("BTC_SIGNAL_ENABLED", True):
        return False
    return (ticker or "").upper() in {t.upper() for t in _cfg("BTC_PROXY_TICKERS", {"IBIT"})}


def evaluate(ticker: str, proxy_atm_iv: Optional[float], btc: Optional[Dict]) -> Dict:
    """The cross-venue read for one underlying.

    `proxy_atm_iv` is the ETF's ATM implied vol as a FRACTION (0.3272), matching how the rest of
    the codebase carries IV. DVOL arrives in vol POINTS (34.24). The conversion happens here,
    once, because mixing the two units silently produces a hundred-fold error that still looks
    like a plausible number.

    Returns a dict that is always shaped the same. `available` is False when either side is
    missing, and callers must treat that as absence of information rather than a neutral
    reading — the two are different and only one of them is honest.
    """
    out = {
        "available": False,
        "ticker": ticker,
        "proxy_iv_pp": None,
        "dvol": None,
        "iv_gap_pp": None,
        "btc_vrp_pp": None,
        "btc_rv_30d": None,
        "reading": "unavailable",
        "provisional": True,
        "note": "",
    }
    if not applies_to(ticker):
        out["note"] = f"{ticker} does not track BTC directly; cross-venue IV is not comparable."
        return out

    dvol = (btc or {}).get("dvol")
    if dvol is None or proxy_atm_iv is None:
        missing = "DVOL" if dvol is None else f"{ticker} ATM IV"
        out["note"] = f"{missing} unavailable — no cross-venue read this cycle."
        return out

    proxy_pp = round(float(proxy_atm_iv) * 100, 2)
    gap = round(float(dvol) - proxy_pp, 2)

    out.update({
        "available": True,
        "proxy_iv_pp": proxy_pp,
        "dvol": round(float(dvol), 2),
        "iv_gap_pp": gap,
        "btc_vrp_pp": (btc or {}).get("btc_vrp_pp"),
        "btc_rv_30d": (btc or {}).get("btc_rv_30d"),
        "reading": _reading(gap),
        "note": _narrate(ticker, proxy_pp, float(dvol), gap, (btc or {}).get("btc_vrp_pp")),
    })
    return out


def _reading(gap_pp: float) -> str:
    """Which surface is richer, from the perspective of someone SELLING premium on the ETF.

    gap = DVOL - proxy IV.  Positive means BTC's own options are pricing more vol than the
    ETF's are, so the seller is writing the cheaper of the two surfaces.
    """
    wide = float(_cfg("BTC_IV_GAP_WIDE_PP", 3.0))
    if gap_pp >= wide:
        return "etf_cheap"        # selling the cheaper surface — mildly unfavourable
    if gap_pp <= -wide:
        return "etf_rich"         # ETF premium rich vs BTC's own market — favourable
    return "aligned"              # the two venues agree; no cross-venue information


def _narrate(ticker: str, proxy_pp: float, dvol: float, gap: float,
             btc_vrp: Optional[float]) -> str:
    if gap >= float(_cfg("BTC_IV_GAP_WIDE_PP", 3.0)):
        lead = (f"BTC's own options price {gap:.1f} vol points MORE than {ticker}'s "
                f"({dvol:.1f} vs {proxy_pp:.1f}) — selling {ticker} premium here is writing "
                f"the cheaper of the two surfaces.")
    elif gap <= -float(_cfg("BTC_IV_GAP_WIDE_PP", 3.0)):
        lead = (f"{ticker} options price {abs(gap):.1f} vol points MORE than BTC's own "
                f"({proxy_pp:.1f} vs {dvol:.1f}) — the ETF surface is the rich one.")
    else:
        lead = (f"{ticker} at {proxy_pp:.1f} and BTC at {dvol:.1f} are within "
                f"{abs(gap):.1f} vol points — the venues agree, so there is no cross-venue "
                f"edge either way.")
    if btc_vrp is not None:
        lead += (f" BTC's own variance premium is {btc_vrp:+.1f}pp "
                 f"(implied over realised).")
    return lead + " Advisory only; thresholds are provisional and ungraded."
