#!/usr/bin/env python3
"""band_forecast.py — the range claim, written down and graded.

WHAT THIS IS FOR

VEGA's premium-selling thesis rests on one quantity: how far the underlying is going to move.
Every strike, every credit floor, every POP is downstream of it. Until this module existed the
system measured that quantity exactly once — through the outcome of a credit spread, which is
binary, weeks-lagging, fill-model-contaminated, and produced at a rate of a few a month. A
volatility forecast cannot be calibrated on a few dozen binary events a year.

A graded RANGE claim measures the same forecast directly. It scores whether the close landed
inside a band the engine drew at a stated confidence, it needs no fill model, it risks no
capital, and it produces a scorable event per ticker per horizon per day.

WHAT WAS ALREADY HERE, AND WHAT WAS NOT

Nearly all of it was already here, which is the uncomfortable part:

  * `price_projection.project()` draws the band — lognormal, zero drift, forecast sigma — and
    carries a coverage table from ~14,900 held-out observations (50%->52.7, 68%->70.7,
    80%->81.5, 90%->89.7).
  * `predictions.BAND_CONTAINS` and a working scorer for it have existed since the band was
    drawn.
  * `direction_forecast` already sweeps the watchlist on four horizons with a baseline twin.

What did not exist was anything that WROTE a band claim. On 2026-09-03 the prediction ledger
held 3,606 rows and not one of them was a band. The coverage numbers the UI quotes are a
backtest, and a backtest is not production evidence: it cannot see a live data path that
degrades, a vol forecast that stops updating, or a spot that is stale. This module closes that
gap and nothing more — it introduces no new band mathematics, because a second definition of
the band is exactly the defect that made `credit_per_share` meaningless.

THE BASELINE TWIN, AND WHY IT IS NOT OPTIONAL

Each horizon is written twice: once from the mean-reverting FORECAST vol, once from TRAILING
realised vol. The trailing band is the null model — "the next stretch looks like the recent
past" — and it is free to compute. Without it a coverage of 79% on an 80% band means nothing,
because the trailing band may deliver 79% too, in which case the forecast machinery is
decoration. This is the same device `direction_forecast` uses for the same reason, and both
twins are scored by one rule so the comparison is real.

HORIZONS ARE NEVER POOLED

One claim type per horizon. `predictions.grade()` buckets by claim_type, so separate types is
all separate grading takes. Pooling an overnight band with a monthly one yields a coverage
figure describing neither. Some of these horizons will turn out to be forecastable and some
will not — overnight gap risk on a single name is close to a coin flip — and finding out WHICH
is the deliverable, not an assumption to be built on.

THE THIRD LEG

`price_projection` states the engine's band. The chain states the market's. Where a caller can
supply the implied volatility it was quoted, it is recorded alongside as `implied_vol_pp` and
the implied band is derived on the identical horizon, so the disagreement between the two is
auditable per claim. Where no chain was fetched the field is NULL and the claim records that it
was not attempted — never a trailing number wearing an implied label.

WHAT THIS DELIBERATELY DOES NOT DO

It does not move a strike, a delta, or a gate. A band that has never been graded in production
is a hypothesis; letting an ungraded hypothesis choose a strike is how a disciplined system
becomes a conviction trade. The claims are recorded and displayed. Whether any horizon earns
the right to influence selection is a decision made later, against these rows, per horizon.

WHAT A WALK-FORWARD OF THIS WRITER ALREADY SHOWS

Before this module was wired into the cycle it was run over 8 names and ~51 anchors each, every
seventh session across two years, resolved against real bars (3,328 claims, 0 unresolvable).
Against a claimed 80%:

    horizon      coverage     baseline twin
    1d              78.8%        77.2%
    1w              86.1%        83.2%
    1m              86.1%        83.9%
    overnight       95.4%        94.5%

Two things follow, and both are recorded here so they are not rediscovered as surprises:

1. THE OVERNIGHT BAND IS KNOWN-WIDE. It is drawn with a full session of sigma but settles on
   the OPEN, so it is charged for a whole day's variance and graded on the gap alone — hence
   95% coverage on an 80% claim. That is a mis-specification, not a win. It is left as written
   rather than tuned, because shrinking a horizon until its coverage hits the target IS fitting
   to the validation sample, and because the claim as stated is at least true and gradeable.
   Confirming this against production rows, then deciding whether the overnight horizon gets a
   gap-variance estimate or gets retired, is the first calibration question this channel owes.

2. SKILL AND RESOLUTION ARE MEANINGLESS FOR THIS CHANNEL, BY CONSTRUCTION. Every band claim
   carries the SAME probability — the stated confidence — so there is no spread of forecasts
   for resolution to measure and `grade()` will report ~0 discrimination forever. That is not a
   defect to be chased. Band claims measure CALIBRATION (does an 80% band contain price 80% of
   the time); the direction claims are where discrimination is the question. Reading the band
   channel's `resolution` as a verdict on the vol forecast would be a category error.

The walk-forward is not the point of the module and does not replace it: it cannot see a live
data path that degrades, and its 416 raw claims per horizon are ~6 independent observations
after clustering. Production rows are what this exists to accumulate.

LEDGER: writes to the EXISTING logs/vega_predictions.jsonl through predictions.record(). No new
ledger file, therefore no new entry in liveness._no_production_ledgers and no new hole in
tests/conftest.py's isolation list — the two places a sixth ledger has previously been missed.
"""
from __future__ import annotations

import logging
import math
from datetime import date
from typing import Dict, List, Optional, Sequence

import config
from analysis import predictions as pred
from analysis import price_projection as ppj
from analysis import vol_forecast as vf
from analysis.direction_forecast import claim_dates, realised_vol

logger = logging.getLogger(__name__)

# (claim_type, trading days forward, which bar field settles it)
#
# Identical horizons and settlement rules to direction_forecast, deliberately: the two channels
# are asking different questions about the SAME day, and a shared settlement calendar is what
# lets a later analysis ask whether the days a direction call missed are the days the band
# missed. Different score_on dates would make that join silently wrong rather than impossible.
HORIZONS = (
    (pred.BAND_CONTAINS_OVERNIGHT, 1, "open"),
    (pred.BAND_CONTAINS_1D, 1, "close"),
    (pred.BAND_CONTAINS_1W, 5, "close"),
    (pred.BAND_CONTAINS_1M, 21, "close"),
)

# Vol windows. `recent` is the near-term level, `long_run` is what it reverts toward — the two
# inputs vol_forecast.forecast_rv is defined against.
RECENT_WINDOW = 20
LONG_RUN_WINDOW = 120

# 50 for the slow average plus room to measure it, matching direction_forecast's floor. Below
# this the long-run window is not a long run and the reversion term is fitting noise.
MIN_CLOSES = 60


def _cfg(name, default):
    return getattr(config, name, default)


def _finite(x) -> Optional[float]:
    """The value as a float, or None if it is NaN, infinite, or not a number at all.

    NaN defeats every ordinary guard in this module and does it silently. `nan <= 0` is False,
    so a NaN spot passes a positivity check; `not nan` is False, so a NaN vol passes a
    truthiness check; and `max(1.0, min(400.0, nan))` in vol_forecast returns NaN rather than
    clamping. A live yfinance pull with a padded final row was enough to drive all four horizons
    to a `nan`-`nan` band on the first run of this module.

    It would not have raised. `json.dumps` emits a bare `NaN` token, which is not valid JSON —
    so every downstream reader that uses a strict parser would fail on the LEDGER rather than
    here, days later, with nothing pointing back at the pull that caused it. Absence is the only
    safe representation of an unusable number, so every numeric input passes through this.
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _usable(band: Optional[Dict]) -> bool:
    """A band is usable only when every number a grader will read back is finite and ordered.

    The last guard before the ledger. `project()` is arithmetic over floats and will hand back
    a well-formed dict full of NaN just as readily as a real band, and a NaN low/high resolves
    every claim as OUTSIDE -- a channel that looks like it is measuring and is not.
    """
    if not band:
        return False
    lo, hi = _finite(band.get("low")), _finite(band.get("high"))
    return (lo is not None and hi is not None and lo > 0 and hi > lo
            and _finite(band.get("sigma_horizon")) is not None)


def confidence() -> float:
    """The band's stated confidence. One knob, defaulting to price_projection's own."""
    return float(_cfg("BAND_FORECAST_CONFIDENCE", ppj.DEFAULT_CONFIDENCE))


def _annualised(closes: Sequence[float], window: int) -> Optional[float]:
    """Annualised realised vol in POINTS over `window` sessions, or None.

    `direction_forecast.realised_vol` returns a DECIMAL (0.284); `vol_forecast.forecast_rv` and
    `price_projection.project` both take POINTS (28.4). The conversion is here and nowhere else.

    This is not a hypothetical tidy-up. Handing the decimal straight through does not raise: it
    is a positive float, it passes every `> 0` guard, and `forecast_rv` then clamps it up to
    MIN_VOL_PP = 1.0 — so a 28-vol name silently becomes a 1-vol name and the 80% band collapses
    to about ±0.3%. Every claim would resolve OUTSIDE, and the ledger would report a confidently
    calibrated-looking channel that had measured nothing but its own unit error.
    """
    rv = _finite(realised_vol(closes, window))
    return None if rv is None else rv * 100.0


def band_for(ticker: str,
             closes: Sequence[float],
             days: int,
             conf: Optional[float] = None,
             implied_vol_pp: Optional[float] = None,
             sector_fetch=None) -> Optional[Dict]:
    """The band this ticker is expected to sit inside `days` trading sessions from now.

    Returns None rather than a guess whenever an input is missing. A band drawn from a
    substituted volatility is indistinguishable from a real one once it is in the ledger, and
    a coverage statistic computed over a mixture of the two describes nothing.
    """
    # Dropped, not zero-filled: a padded bar is a hole in the series, and a hole priced at zero
    # produces a -100% log return that dwarfs every real move in the window.
    closes = [c for c in (_finite(c) for c in closes) if c is not None and c > 0]
    if len(closes) < MIN_CLOSES:
        return None
    spot = closes[-1]

    recent = _annualised(closes, RECENT_WINDOW)
    long_run = _annualised(closes, min(LONG_RUN_WINDOW, len(closes) - 1))
    if not recent or recent <= 0:
        return None

    fc = vf.for_ticker(ticker, recent, long_run, fetch=sector_fetch)
    fc_pp = _finite((fc or {}).get("forecast_pp"))
    if not fc_pp or fc_pp <= 0:
        return None

    # Round the volatilities BEFORE drawing, not after recording. The row stores these numbers
    # to 2dp, and a reader who recomputes the band from the stored inputs must land on the
    # stored band — otherwise the claim's own context does not reproduce the claim, which is
    # precisely the "field says one thing, number came from another" defect this project has
    # been burned by. The precision given up is a hundredth of a vol point.
    recent = round(recent, 2)
    long_run = round(long_run, 2) if long_run else long_run
    conf = confidence() if conf is None else float(conf)
    # `days` is already TRADING days; hand it straight to project() rather than converting to a
    # calendar figure and back. `dte` is still passed because project() validates on it and
    # reports it, and a reader comparing this row to an option needs the calendar number.
    dte_cal = max(1, int(round(days * ppj.CALENDAR_DAYS / ppj.TRADING_DAYS)))

    fcast = ppj.project(spot, dte_cal, fc_pp, confidence=conf, trading_days_override=days)
    base = ppj.project(spot, dte_cal, recent, confidence=conf, trading_days_override=days)
    if not _usable(fcast) or not _usable(base):
        return None

    iv_pp = _finite(implied_vol_pp)
    implied = None
    if iv_pp and iv_pp > 0:
        implied = ppj.project(spot, dte_cal, iv_pp, confidence=conf,
                              trading_days_override=days)
        if not _usable(implied):
            implied, iv_pp = None, None

    return {
        "spot": spot,
        "days": days,
        "dte_calendar": dte_cal,
        "confidence": conf,
        "forecast": fcast,
        "baseline": base,
        "implied": implied,
        "forecast_vol_pp": fc_pp,
        "trailing_vol_pp": recent,
        "long_run_vol_pp": long_run,
        "implied_vol_pp": round(iv_pp, 2) if iv_pp else None,
        "vol_state": fc.get("state"),
        "sector_proxy": fc.get("sector_proxy"),
    }


def _claim_text(ticker: str, b: Dict, band: Dict, days: int, is_baseline: bool) -> str:
    src = "trailing realised" if is_baseline else "forecast"
    vol = b["trailing_vol_pp"] if is_baseline else b["forecast_vol_pp"]
    txt = (f"{ticker} settles between {band['low']:.2f} and {band['high']:.2f} "
           f"({band['low_pct']:+.1f}% / {band['high_pct']:+.1f}%) "
           f"{days} trading day(s) out, at {b['confidence']:.0%} confidence - "
           f"{src} vol {vol:.1f}, spot {b['spot']:.2f}")
    iv = b.get("implied_vol_pp")
    if iv:
        gap = vol - iv
        txt += (f"; chain implies {iv:.1f} ({gap:+.1f} vol pts "
                f"{'rich' if gap < 0 else 'cheap'} vs this view)")
    return txt


def record_ticker(ticker: str,
                  closes: Sequence[float],
                  today: Optional[date] = None,
                  horizons=HORIZONS,
                  baseline: Optional[bool] = None,
                  implied_vol_pp: Optional[float] = None,
                  sector_fetch=None) -> List[str]:
    """Write one band claim per horizon (plus its baseline twin). Returns the ids recorded."""
    today = today or date.today()
    if baseline is None:
        baseline = bool(_cfg("BAND_RECORD_BASELINE", True))
    ids: List[str] = []

    for claim_type, days, score_field in horizons:
        b = band_for(ticker, closes, days, implied_vol_pp=implied_vol_pp,
                     sector_fetch=sector_fetch)
        if b is None:
            logger.debug("[band] %s %s abstained: no band", ticker, claim_type)
            continue
        dates = claim_dates(days, today)
        for is_baseline in ((False, True) if baseline else (False,)):
            band = b["baseline"] if is_baseline else b["forecast"]
            ctype = f"{claim_type}_baseline" if is_baseline else claim_type
            suffix = "-base" if is_baseline else ""
            imp = b.get("implied") or {}
            pid = pred.record(
                trade_id=f"bf-{ticker}-{today.isoformat()}{suffix}",
                ticker=ticker,
                claim_type=ctype,
                claim=_claim_text(ticker, b, band, days, is_baseline),
                # The stated confidence IS the claimed probability of landing inside. That is
                # what makes this Brier-scorable against `correct`, and what makes a band that
                # is honestly 60% while claiming 80% show up as overconfidence rather than as
                # a merely disappointing hit rate.
                probability=b["confidence"],
                resolves_on=dates["resolves_on"],
                context={
                    # What the scorer reads.
                    "band_low": band["low"],
                    "band_high": band["high"],
                    "score_on": dates["score_on"],
                    "score_field": score_field,
                    # What a later audit reads.
                    "horizon_days": days,
                    "price_at_claim": b["spot"],
                    "confidence": b["confidence"],
                    "band_low_pct": band["low_pct"],
                    "band_high_pct": band["high_pct"],
                    "band_width_pct": round(band["high_pct"] - band["low_pct"], 2),
                    "sigma_horizon": band["sigma_horizon"],
                    "backtest_coverage": band.get("measured_coverage"),
                    # The three legs of the comparison, on every row, qualified or not, so the
                    # relationship between them is auditable later rather than reconstructed.
                    "forecast_vol_pp": b["forecast_vol_pp"],
                    "trailing_vol_pp": b["trailing_vol_pp"],
                    "long_run_vol_pp": b["long_run_vol_pp"],
                    "implied_vol_pp": b["implied_vol_pp"],
                    "implied_band_low": imp.get("low"),
                    "implied_band_high": imp.get("high"),
                    "implied_attempted": bool(b.get("implied_vol_pp")),
                    "vol_state": b["vol_state"],
                    "sector_proxy": b["sector_proxy"],
                    "baseline": is_baseline,
                    "cohort": "band_forecast_v1",
                    "close_logic": "band_forecast_v1",
                },
            )
            if pid:
                ids.append(pid)
    return ids


def record_watchlist(today: Optional[date] = None,
                     tickers: Optional[Sequence[str]] = None,
                     price_lookup=None,
                     implied_lookup=None) -> Dict:
    """Write band claims for the whole watchlist. Returns counts, never raises.

    `implied_lookup(ticker) -> vol points or None` is optional. When it is absent every claim
    records `implied_attempted: False` rather than a substituted number — the difference
    between "the market disagreed" and "nobody asked the market" has to survive into the row.
    """
    today = today or date.today()
    if tickers is None:
        tickers = [w["ticker"].upper() for w in getattr(config, "WATCHLIST", [])]
    if price_lookup is None:
        from data import fetcher
        price_lookup = lambda tk: fetcher.get_price_data(tk, period="1y")  # noqa: E731

    stats = {"tickers": len(tickers), "recorded": 0, "abstained": 0, "failed": 0,
             "with_implied": 0}
    # One read and one write for the whole sweep, for the same reason direction_forecast
    # batches: un-batched, record() re-reads and re-writes the entire ledger per claim.
    with pred.batch():
        for tk in tickers:
            try:
                df = price_lookup(tk)
                if df is None or getattr(df, "empty", True):
                    stats["failed"] += 1
                    continue
                closes = [float(c) for c in df["Close"].tolist()]
                iv = None
                if implied_lookup is not None:
                    try:
                        iv = implied_lookup(tk)
                    except Exception:
                        iv = None
                if iv:
                    stats["with_implied"] += 1
                ids = record_ticker(tk, closes, today=today, implied_vol_pp=iv)
                stats["recorded"] += len(ids)
                if not ids:
                    stats["abstained"] += 1
            except Exception as e:
                logger.debug("[band] %s failed: %s", tk, e)
                stats["failed"] += 1
    return stats


if __name__ == "__main__":                            # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(record_watchlist())
