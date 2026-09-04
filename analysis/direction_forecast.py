#!/usr/bin/env python3
"""direction_forecast.py — dated directional claims about the watchlist, at four horizons.

WHAT THIS IS FOR, AND WHAT IT IS NOT FOR

It is not an alpha engine. VEGA already tested a drift input and rejected it: price_projection
sets drift to zero and records why — sector relative strength showed rank correlation with
forward returns of +0.01 to -0.04 at every horizon, none significant. Nothing here contradicts
that. The bands VEGA draws are still zero-drift; this module does not feed them.

It exists because of a measurement problem. Every claim VEGA makes today matures at a 30-45 day
expiry: 39 claims in the ledger, none resolved, the first maturing 2026-08-23. At that cadence
the question "do VEGA's probabilities mean anything?" takes quarters to answer, and that
question is what blocks everything else — the clean cohort is 0 of 30, the shadow book prices 0
of 165. A one-day claim matures in ONE day. Fifty-six tickers times four horizons is a few
hundred gradeable claims a week, so the calibration read arrives in a fortnight instead of a
year, and it arrives whether or not the model is any good.

A null result is the expected result and is worth having. If direction_1d comes back with
resolution ~0, that is a real finding, cheaply bought, and the honest thing is to stop there.

THE MODEL IS DELIBERATELY SMALL — the same decision btc_forecast made and for the same reason.
Trend against a longer trend, position against the slow average, and where realised vol sits
against its own recent history. That is all. It is not expected to be good. It is expected to
be WRITTEN DOWN, so that a year from now there is a graded record instead of an impression.

PROBABILITIES COME FROM THE BAND, NOT FROM A SCORE

The tempting shortcut is to map a signal score onto a confidence — "trend is up, call it 60%".
That number is unanchored, and against a half-sigma flat band it is not even in the right
range: under zero drift P(up) is about 31%, not 50%, because most of the mass lands inside the
band. A model asserting 60% on a 31% base rate is not bullish, it is miscalibrated, and Brier
would take a year to say so politely.

So the claim is derived from the distribution the band already implies:

    p_up   = 1 - PHI(band_sigmas - mu_sigmas)
    p_down = PHI(-band_sigmas - mu_sigmas)
    p_flat = 1 - p_up - p_down

`mu_sigmas` is the ONLY thing the signal is allowed to move, it is capped hard, and at mu = 0
this reduces exactly to climatology. That makes the baseline free: grade the same claims with
the tilt zeroed and any difference in resolution is what the signal actually contributed.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from analysis import predictions as pred  # noqa: E402

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252

# (claim_type, trading days ahead, settles on which price, flat band in sigmas)
#
# The band widens with the horizon because the asset's own sigma does. A fixed percentage band
# would make "flat" unreachable at one day and unmissable at one month — the defect the BTC
# claim hit from the other direction, where a 1% band against a 6.7% two-week move turned the
# claim into a coin flip on noise while still reporting a hit rate that looked like skill.
# The flat band, in sigmas, that makes up / down / flat EQUALLY likely under zero drift:
#     P(|Z| < b) = 1/3  ->  PHI(b) = 2/3  ->  b = 0.4307
#
# Not a cosmetic choice. At the obvious half-sigma band, flat carries 38.3% against 30.8% a
# side, so the most likely outcome is flat NO MATTER WHAT the signal says — the capped tilt
# cannot close a 7.5-point gap at any horizon, and every claim the module ever made would read
# "flat". A constant forecast has zero resolution BY CONSTRUCTION: it would grade as beautifully
# calibrated and would have measured nothing, which is the exact failure this module exists to
# avoid rather than reproduce.
#
# At equal thirds the three outcomes start tied and the signal decides which way the claim
# falls, so the claim actually varies with what the model believes — the precondition for the
# Brier decomposition to have anything to decompose.
EQUAL_THIRDS_SIGMAS = 0.4307

# RETIRED 2026-09-04, WITH THE NUMBERS, SO NOBODY REBUILDS THEM
# ------------------------------------------------------------
# `direction_1d` and `direction_overnight` are deliberately absent from this tuple. They ran
# from 2026-08-15 to 2026-09-04 and were measured to carry NO information whatsoever:
#
#     claim type                    raw    effective   hit%   Brier    skill   RESOLUTION
#     direction_overnight           336        96      18.5   0.174   -0.154     0.0000
#     direction_overnight_baseline  336        96      64.6   0.326   -0.427     0.0000
#     direction_1d                  336        96      29.8   0.211   -0.007     0.0000
#     direction_1d_baseline         336        96      36.9   0.234   -0.006     0.0000
#
# RESOLUTION is the number that matters and it is the reason for the retirement. Resolution
# 0.0000 is what shuffling the outcomes produces: the forecasts do not distinguish one day from
# another. Read the hit rates only alongside it -- the tilted and baseline variants predict
# DIFFERENT CATEGORIES (the baseline always says "flat", a wide and easy target), so the 18.5%
# vs 64.6% gap is not evidence the tilt is harmful, and Brier actually favours the tilt. Both
# carry zero skill; that is the finding.
#
# These were not retired early. Effective sample size after clustering by overlapping horizon
# and market factor is 96 blocks each, against a gradeability floor of 10. They had a fair test
# and failed it.
#
# 1W AND 1M ARE KEPT, AND THE ASYMMETRY IS DELIBERATE AND UNCOMFORTABLE: the two horizons being
# retired are the two that are PROVEN worthless, and the two being kept are UNPROVEN.
# `direction_1w` sits at effective N 16 with resolution 0.0013; `direction_1m` has never
# resolved a single claim and cannot before 2026-09-24. So the honest statement is that this
# channel currently has zero demonstrated skill at ANY horizon, and the horizon closest to the
# 35-DTE trade window has not spoken yet. Retire the rest on the same rule -- resolution ~0 at
# an effective N past the floor -- and record the numbers here when you do.
#
# Restoring a horizon means re-adding its tuple entry; nothing else was deleted, and the claim
# types and scorers remain in `predictions` so the historical rows stay gradeable.
HORIZONS = (
    (pred.DIRECTION_1W, 5, "close", EQUAL_THIRDS_SIGMAS),
    (pred.DIRECTION_1M, 21, "close", EQUAL_THIRDS_SIGMAS),
)

# The cap on the signal, expressed as an ANNUALISED information ratio — drift per unit of
# annual sigma. It is not a per-horizon sigma cap, and the difference is not cosmetic.
#
# Drift accumulates linearly in time; sigma grows with its square root. So the same edge is
# worth mu_sigmas = IR * sqrt(t) at horizon t, NOT a constant. Holding the sigma tilt fixed
# across horizons — the first version of this module did — implies a drift of 0.2 sigma in a
# single session, about 0.45% a day on a 25-vol name, which annualises past 100%. The error is
# largest exactly where the signal is weakest.
#
# At IR 0.25 the tilt is ~0.016 sigma at one day and ~0.072 at one month. The one-day claim is
# therefore almost pure climatology, which is the honest answer: a 20/50 moving-average cross
# has close to nothing to say about tonight's gap.
MAX_TILT_IR = float(getattr(config, "DIRECTION_MAX_TILT_IR",
                            getattr(config, "DIRECTION_MAX_TILT_SIGMAS", 0.25)))

MIN_CLOSES = 60          # 50 for the slow average, plus room to measure vol against its own past


def _cfg(name, default):
    return getattr(config, name, default)


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sma(values: Sequence[float], n: int) -> Optional[float]:
    return sum(values[-n:]) / n if len(values) >= n else None


def realised_vol(closes: Sequence[float], window: int = 20) -> Optional[float]:
    """Annualised realised volatility as a decimal, from log returns."""
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)


def next_trading_day(d: date, n: int = 1) -> date:
    """n weekdays forward. Holidays are NOT modelled.

    Deliberate: a claim landing on a holiday resolves against the last bar at or before it
    (see predictions._bar_on) and the resolve grace window absorbs the slip. Wiring a market
    calendar in here would add a dependency and a failure mode to buy an off-by-one-day
    correction on a claim whose whole point is being graded in bulk.
    """
    out = d
    while n > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out


def tilt(closes: Sequence[float]) -> Dict:
    """The signal as an ANNUALISED information ratio. Small on purpose, capped hard.

    Horizon-free by construction: `forecast` scales it by sqrt(t) to get that horizon's tilt in
    sigmas. Returning a per-horizon number here is what produced a one-day drift claim of
    0.2 sigma — see MAX_TILT_IR.

    Three reads, each worth a fraction of a sigma:
      trend      — fast average against slow, the direction of the medium-term drift
      position   — spot against the slow average, whether price is extended or recovering
      vol state  — realised vol against its own recent past. Expanding vol is not directional,
                   so it does not push the mean; it SHRINKS the tilt, because a signal measured
                   on a quiet tape means less on a violent one.
    """
    out = {"ir": 0.0, "drivers": [], "trend_pct": None, "above_slow_pct": None}
    fast, slow = _sma(closes, 20), _sma(closes, 50)
    if fast is None or slow is None:
        return out
    spot = closes[-1]

    trend_pct = (fast / slow - 1) * 100
    above_pct = (spot / slow - 1) * 100
    out["trend_pct"] = round(trend_pct, 2)
    out["above_slow_pct"] = round(above_pct, 2)

    mu = 0.0
    if abs(trend_pct) >= 0.25:
        step = 0.12 if trend_pct > 0 else -0.12
        mu += step
        out["drivers"].append(
            f"20d average {trend_pct:+.2f}% vs the 50d — trend {'up' if trend_pct > 0 else 'down'}")
    if abs(above_pct) >= 1.0:
        step = 0.08 if above_pct > 0 else -0.08
        mu += step
        out["drivers"].append(
            f"spot {above_pct:+.2f}% against the 50d average")

    fast_vol, slow_vol = realised_vol(closes, 10), realised_vol(closes, 40)
    if fast_vol and slow_vol and slow_vol > 0:
        ratio = fast_vol / slow_vol
        if ratio > 1.15:
            mu *= 0.5
            out["drivers"].append(
                f"realised vol expanding ({ratio:.2f}x its 40d) — signal halved, not reversed")
        elif ratio < 0.85:
            out["drivers"].append(f"realised vol compressing ({ratio:.2f}x its 40d)")

    out["ir"] = round(max(-MAX_TILT_IR, min(MAX_TILT_IR, mu)), 4)
    return out


def forecast(ticker: str, closes: Sequence[float], claim_type: str, days: int,
             score_field: str, band_sigmas: float, today: Optional[date] = None,
             apply_tilt: bool = True) -> Dict:
    """One horizon's claim for one ticker, or an explicit abstention.

    `expected` is always present and "none" is a first-class outcome, not a failure — a
    forecaster that cannot decline has to guess, and a guess written into the ledger poisons
    the record it was built to grade.

    `apply_tilt=False` produces the CLIMATOLOGY baseline: identical band, identical horizon,
    mean pinned at zero. Grading the two side by side is how the signal gets charged for its
    own existence rather than credited with the base rate.
    """
    today = today or date.today()
    out = {
        "ticker": ticker, "claim_type": claim_type, "expected": "none",
        "probability": None, "horizon_days": days, "score_field": score_field,
        "flat_band_pct": None, "price_at_claim": None, "sigma_pct": None,
        "mu_sigmas": 0.0, "ir": 0.0, "drivers": [], "reason": "",
        "p_up": None, "p_down": None, "p_flat": None,
    }
    closes = [float(c) for c in closes if c]
    if len(closes) < MIN_CLOSES:
        out["reason"] = (f"{len(closes)} closes available, {MIN_CLOSES} needed for the trend "
                         f"and vol reads. No claim made.")
        return out

    spot = closes[-1]
    out["price_at_claim"] = round(spot, 4)

    vol = realised_vol(closes, 20)
    if not vol or vol <= 0:
        out["reason"] = "no realised-vol reading; the flat band cannot be set. No claim made."
        return out

    sigma = vol * math.sqrt(days / TRADING_DAYS_PER_YEAR)      # decimal, this horizon
    out["sigma_pct"] = round(sigma * 100, 3)
    out["flat_band_pct"] = round(sigma * band_sigmas * 100, 3)

    t = tilt(closes) if apply_tilt else {"ir": 0.0, "trend_pct": None, "above_slow_pct": None,
                                         "drivers": ["climatology baseline — mean pinned at zero"]}
    # The signal is an annualised information ratio; this horizon's tilt in sigmas is IR*sqrt(t).
    mu = float(t["ir"]) * math.sqrt(days / TRADING_DAYS_PER_YEAR)
    out["ir"] = round(float(t["ir"]), 4)
    out["mu_sigmas"] = round(mu, 5)
    out["drivers"] = t["drivers"]
    out["trend_pct"] = t.get("trend_pct")
    out["above_slow_pct"] = t.get("above_slow_pct")

    # Round the two tails, then take flat as the REMAINDER of the rounded pair rather than
    # rounding it independently. Three separately-rounded numbers sum to 0.9999, and a
    # distribution that does not sum to one is the kind of small dishonesty that shows up later
    # as a Brier score nobody can reconcile.
    p_up = round(1.0 - _phi(band_sigmas - mu), 4)
    p_down = round(_phi(-band_sigmas - mu), 4)
    p_flat = round(max(0.0, 1.0 - p_up - p_down), 4)
    out["p_up"], out["p_down"], out["p_flat"] = p_up, p_down, p_flat

    expected, probability = max((("up", p_up), ("down", p_down), ("flat", p_flat)),
                                key=lambda kv: kv[1])
    out["expected"] = expected
    out["probability"] = round(probability, 4)
    out["reason"] = (f"{expected.upper()} at {probability:.0%} over {days} trading day(s); "
                     f"flat band ±{out['flat_band_pct']:.2f}%"
                     + (f". {'; '.join(t['drivers'])}" if t["drivers"] else ""))
    return out


def claim_dates(days: int, today: Optional[date] = None) -> Dict[str, str]:
    """When the claim settles, and when it is SAFE TO READ.

    These are different dates and conflating them is how a one-day claim ends up scored on a
    two-day move. The settling bar must be complete before resolution runs, and the desk's last
    cycle fires before the close, so resolution is deferred one trading day past settlement.
    """
    today = today or date.today()
    settle = next_trading_day(today, days)
    return {"score_on": settle.isoformat(),
            "resolves_on": next_trading_day(settle, 1).isoformat()}


def record_ticker(ticker: str, closes: Sequence[float], today: Optional[date] = None,
                  horizons=HORIZONS, baseline: Optional[bool] = None) -> List[str]:
    """Write one claim per horizon for one ticker. Returns the ids actually recorded."""
    today = today or date.today()
    if baseline is None:
        baseline = bool(_cfg("DIRECTION_RECORD_BASELINE", True))
    ids: List[str] = []

    for claim_type, days, score_field, band_sigmas in horizons:
        for is_baseline in ((False, True) if baseline else (False,)):
            fc = forecast(ticker, closes, claim_type, days, score_field, band_sigmas,
                          today=today, apply_tilt=not is_baseline)
            if fc["expected"] == "none":
                logger.debug("[direction] %s %s abstained: %s", ticker, claim_type, fc["reason"])
                continue
            dates = claim_dates(days, today)
            suffix = "-base" if is_baseline else ""
            ctype = f"{claim_type}_baseline" if is_baseline else claim_type
            pid = pred.record(
                trade_id=f"fc-{ticker}-{today.isoformat()}{suffix}",
                ticker=ticker,
                claim_type=ctype,
                claim=fc["reason"],
                probability=fc["probability"],
                resolves_on=dates["resolves_on"],
                context={
                    "expected": fc["expected"],
                    "price_at_claim": fc["price_at_claim"],
                    "flat_band_pct": fc["flat_band_pct"],
                    "score_on": dates["score_on"],
                    "score_field": score_field,
                    "horizon_days": days,
                    "sigma_pct": fc["sigma_pct"],
                    "mu_sigmas": fc["mu_sigmas"], "ir": fc["ir"],
                    "p_up": fc["p_up"], "p_down": fc["p_down"], "p_flat": fc["p_flat"],
                    "baseline": is_baseline,
                    "cohort": "direction_forecast_v1",
                    "close_logic": "direction_forecast_v1",
                },
            )
            if pid:
                ids.append(pid)
    return ids


def record_watchlist(today: Optional[date] = None, tickers: Optional[Sequence[str]] = None,
                     price_lookup=None) -> Dict:
    """Write claims for the whole watchlist. Returns counts, never raises."""
    today = today or date.today()
    if tickers is None:
        tickers = [w["ticker"].upper() for w in getattr(config, "WATCHLIST", [])]
    if price_lookup is None:
        from data import fetcher
        price_lookup = lambda tk: fetcher.get_price_data(tk, period="1y")  # noqa: E731

    stats = {"tickers": len(tickers), "recorded": 0, "abstained": 0, "failed": 0}
    # One read and one write for the whole sweep. Un-batched, record() re-read and re-wrote the
    # entire ledger for each of the ~449 claims this produces; see predictions.batch().
    with pred.batch():
        for tk in tickers:
            try:
                df = price_lookup(tk)
                if df is None or getattr(df, "empty", True):
                    stats["failed"] += 1
                    continue
                closes = [float(c) for c in df["Close"].tolist()]
                ids = record_ticker(tk, closes, today=today)
                stats["recorded"] += len(ids)
                if not ids:
                    stats["abstained"] += 1
            except Exception as e:
                logger.debug("[direction] %s failed: %s", tk, e)
                stats["failed"] += 1
    return stats


if __name__ == "__main__":                            # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(record_watchlist())
