#!/usr/bin/env python3
"""
btc_forecast.py — one falsifiable directional claim about Bitcoin per day.

WHAT THIS IS FOR
The ATLAS spec proposed a 60-cycle dry run before any BTC signal could inform a trade, with a
new forecast table and a new outcome logger to support it. None of that needs building:
predictions.py already defines a DIRECTION claim type, scores it, and grades it with a Brier
score and a confidence-bias verdict — and nothing has ever recorded one. It was fully built and
dormant.

So this module does the smallest honest thing: it makes a dated, probability-carrying claim
about which way BTC goes, writes it to the SAME ledger VEGA's own claims live in, and lets the
existing grader mark it. The validation clock starts on day one instead of after a fusion engine
exists, and ATLAS is graded on the same scale as everything else rather than in a private table
that could never be compared against anything.

THE MODEL IS DELIBERATELY SMALL
Trend versus a longer trend, plus where realised vol sits against implied. That is it. It is
not expected to be good. It is expected to be WRITTEN DOWN — a mediocre forecast that is graded
beats a sophisticated one that evaporates, and it establishes the baseline any later fundamental
layer has to beat. If on-chain data is added and the Brier score does not improve, the ablation
was free.

THE CONFIDENCE IS THE POINT
A claim without a probability cannot be calibrated, only counted. The probability here is
deliberately timid — it moves in a narrow band around a coin flip, because a model this simple
has no business claiming 80%. Overconfidence is the failure mode the Brier score exists to
catch, and starting humble means the verdict will say "underconfident, this deserves more
weight" rather than quietly rewarding a guess.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

TICKER = "BTC-USD"
COHORT = "btc_forecast_v1"


def _cfg(name, default):
    return getattr(config, name, default)


def _sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def forecast(candles: Optional[List[Dict]] = None, btc: Optional[Dict] = None) -> Dict:
    """Today's directional claim, or an explicit abstention.

    Returns a dict that ALWAYS carries `expected` in {up, down, flat, none}. "none" is a first
    class outcome, not a failure: a forecaster that cannot decline is one that must guess, and
    a guess logged as a claim poisons the very record being built to grade it.
    """
    from data import crypto
    btc = btc if btc is not None else crypto.snapshot()
    candles = candles if candles is not None else crypto.get_btc_candles(120)

    out = {
        "asset": "BTC",
        "at": datetime.now().isoformat(),
        "expected": "none",
        "probability": None,
        "horizon_days": int(_cfg("BTC_FORECAST_HORIZON_DAYS", 14)),
        "flat_band_pct": None,
        "price_at_claim": None,
        "drivers": [],
        "reason": "",
    }

    closes = [c["close"] for c in (candles or []) if c.get("close")]
    spot = btc.get("btc_spot") or (closes[-1] if closes else None)
    dvol = btc.get("dvol")

    if len(closes) < 51 or not spot:
        out["reason"] = (f"Only {len(closes)} daily closes available; the trend read needs 50. "
                         f"No claim made.")
        return out

    out["price_at_claim"] = round(float(spot), 2)

    # The flat band is the asset's OWN 1-sigma move over the horizon, not a fixed percentage.
    # At 34 vol over 14 days BTC moves ±6.7%, so the ledger's default ±1% band would make
    # "flat" unreachable and turn the claim into a coin flip on noise. Half a sigma is a
    # genuinely undecided outcome for this asset.
    horizon = out["horizon_days"]
    vol_pp = dvol if dvol is not None else btc.get("btc_rv_30d")
    if vol_pp:
        sigma = float(vol_pp) * math.sqrt(horizon / 365.0)
        out["flat_band_pct"] = round(sigma * float(_cfg("BTC_FLAT_BAND_SIGMAS", 0.5)), 2)
    else:
        out["flat_band_pct"] = 3.0
        out["drivers"].append("no vol reading; flat band defaulted to ±3%")

    fast, slow = _sma(closes, 20), _sma(closes, 50)
    if fast is None or slow is None:
        out["reason"] = "Not enough history for both moving averages. No claim made."
        return out

    trend_pct = (fast / slow - 1) * 100
    above = (spot / slow - 1) * 100

    score = 0.0
    if trend_pct > 0:
        score += 1
        out["drivers"].append(f"20d SMA is {trend_pct:+.1f}% vs the 50d — trend up")
    else:
        score -= 1
        out["drivers"].append(f"20d SMA is {trend_pct:+.1f}% vs the 50d — trend down")

    if above > 0:
        score += 0.5
        out["drivers"].append(f"spot is {above:+.1f}% above the 50d")
    else:
        score -= 0.5
        out["drivers"].append(f"spot is {above:+.1f}% below the 50d")

    # Vol regime is a CONFIDENCE modifier, never a direction. A high implied-over-realised
    # premium says the market is paying up for protection; it does not say which way price goes,
    # and treating it as directional is how a volatility signal gets laundered into a price call.
    vrp = btc.get("btc_vrp_pp")
    conviction_penalty = 0.0
    if vrp is not None and vrp > float(_cfg("BTC_HIGH_VRP_PP", 8.0)):
        conviction_penalty = 0.03
        out["drivers"].append(f"implied is {vrp:+.1f}pp over realised — crowded protection, "
                              f"conviction trimmed")

    if abs(trend_pct) < float(_cfg("BTC_TREND_FLAT_PCT", 0.5)):
        out["expected"] = "flat"
        out["drivers"].append("the two averages are within half a percent — no trend to call")
    else:
        out["expected"] = "up" if score > 0 else "down"

    # Deliberately timid. A 20/50 crossover has no business claiming 80%, and an overconfident
    # claim is worse than a wrong one because the Brier score punishes it twice.
    base = float(_cfg("BTC_FORECAST_BASE_PROB", 0.52))
    step = float(_cfg("BTC_FORECAST_PROB_STEP", 0.03))
    p = base + step * (abs(score) - 1.0) - conviction_penalty
    out["probability"] = round(min(max(p, 0.50), float(_cfg("BTC_FORECAST_MAX_PROB", 0.62))), 4)

    out["reason"] = (f"BTC {out['expected'].upper()} over {horizon}d at "
                     f"{out['probability']*100:.0f}% confidence "
                     f"(±{out['flat_band_pct']:.1f}% counts as flat). "
                     + "; ".join(out["drivers"]) + ".")
    return out


def record_daily(fc: Optional[Dict] = None, today: Optional[date] = None) -> Optional[str]:
    """Write today's claim to the shared prediction ledger. One per day; re-running is a no-op.

    The claim id is date-keyed rather than trade-keyed because a forecast is not a trade. The
    ledger's `trade_id` field is opaque — nothing parses it — so a forecast id slots in without
    a schema change, and the cohort tag keeps these separate from trade-derived claims when
    grading.
    """
    if not _cfg("BTC_FORECAST_ENABLED", True):
        return None
    today = today or date.today()
    fc = fc if fc is not None else forecast()

    if fc.get("expected") in (None, "none") or fc.get("probability") is None:
        logger.info("[btc_forecast] abstained: %s", fc.get("reason"))
        return None

    from analysis import predictions as pred
    resolves = today + timedelta(days=int(fc["horizon_days"]))
    return pred.record(
        trade_id=f"btcfc-{today.isoformat()}",
        ticker=TICKER,
        claim_type=pred.DIRECTION,
        claim=fc["reason"],
        probability=fc["probability"],
        resolves_on=resolves.isoformat(),
        context={
            "expected": fc["expected"],
            "price_at_claim": fc["price_at_claim"],
            # Travels with the claim so the scorer uses BTC's own volatility rather than the
            # equity default. Without it the ledger's ±1% band applies and "flat" is unreachable.
            "flat_band_pct": fc["flat_band_pct"],
            "cohort": COHORT,
            "close_logic": COHORT,   # grade(cohort=...) filters on this key today
            "horizon_days": fc["horizon_days"],
            "drivers": fc["drivers"],
        },
    )
