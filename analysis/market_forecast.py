#!/usr/bin/env python3
"""market_forecast.py — a standing regime call on the whole market, at four horizons, graded.

WHAT THIS IS

One BULL / BEAR / NEUTRAL call per tracked index and per tracked crypto asset, at 24 hours,
one week, one month and six months. Two separate things happen with that call:

  * The cockpit's Forecast tab recomputes it live on every page load, so the screen always
    shows what the model believes RIGHT NOW off the latest closes. Nothing is written.
  * Once a day, near the close, the same call is written into the shared prediction ledger as
    a dated, falsifiable claim with a resolution date. That copy is what gets graded.

Those must stay separate. A screen that recomputes constantly and also writes constantly would
record the same day's opinion dozens of times at drifting anchors, and the ledger's hit rate
would then describe how often the page was refreshed.

WHY IT REUSES direction_forecast RATHER THAN INVENTING A SECOND MODEL

`direction_forecast` already solved the hard part and wrote down why: the probability is
derived from the band the asset's own volatility implies, not from a signal score, and the
signal is capped as an ANNUALISED information ratio so the same edge is worth mu = IR*sqrt(t)
at horizon t rather than a constant. Reimplementing that here would produce a second set of
constants that drift from the first. So the tilt comes from `direction_forecast.tilt()`
unchanged, and this module owns only what genuinely differs: the calendar, the horizons, and
the volatility window.

WHAT GENUINELY DIFFERS, AND WHY EACH ONE MATTERS

  CALENDAR. Equities compound over 252 sessions a year; crypto never closes and compounds over
  365. Annualising a crypto series on 252 understates its horizon sigma by sqrt(365/252) = 1.20
  — a 20% too-narrow flat band, which makes NEUTRAL 20% too hard to hit on exactly the assets
  that move most. Each asset declares its own calendar and its horizons are counted in its own
  days.

  VOLATILITY WINDOW. `direction_forecast` reads realised vol over a fixed 20 sessions at every
  horizon, which is defensible when the longest horizon is a month. It is not defensible at six
  months: a band for a 126-session horizon set from 20 sessions of vol inherits every twitch of
  the last month and calls it the next half year. The window here scales with the horizon and
  is capped at 120 periods, so the six-month band is set from roughly six months of behaviour.

  HONESTY ABOUT THE 24-HOUR CALL. `direction_forecast` RETIRED its one-day and overnight
  horizons on 2026-09-04 after measuring resolution 0.0000 over 96 effective samples — the
  forecasts did not distinguish one day from the next. This module carries a 24-hour horizon
  because it was asked for, on index and crypto series rather than on single names, and it is
  a different population. It is not a rehabilitation of that result and must not be read as
  one. It is tagged UNPROVEN in the UI, it is graded in its own bucket, and if it comes back
  at resolution ~0 past the gradeability floor the honest move is to retire it here too and
  record the numbers in this docstring the way direction_forecast did.

EVERY CLAIM IS WRITTEN ALONGSIDE ITS CLIMATOLOGY TWIN — the identical band and horizon with the
mean pinned at zero. Grading the two side by side is the only way the signal is charged for its
own existence instead of being credited with the base rate.

MEASURING INSTRUMENT ONLY. Nothing here reaches selection, sizing or execution. No gate reads
it, no strike moves because of it, and no order can be placed from it.

RUN:  python analysis/market_forecast.py            # print today's live board
      python analysis/market_forecast.py --record   # write today's dated claims
      python analysis/market_forecast.py --grade    # print the graded track record
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from analysis import direction_forecast as df  # noqa: E402
from analysis import predictions as pred  # noqa: E402

logger = logging.getLogger(__name__)

COHORT = "market_forecast_v1"

# The flat band, in sigmas, that makes up / down / flat EQUALLY likely under zero drift:
# P(|Z| < b) = 1/3. Inherited from direction_forecast rather than re-derived — see that
# module for why a half-sigma band makes "flat" the answer no matter what the signal says.
EQUAL_THIRDS_SIGMAS = df.EQUAL_THIRDS_SIGMAS

# Claim types. The "direction" PREFIX is load-bearing: predictions.is_direction_claim matches
# by prefix, so these are scored by the existing direction scorer the moment they are written
# and cannot silently fall through to "no scorer for claim type". The "_mkt_" segment keeps
# them in their own grading buckets — pooling an index's weekly call with the 56-ticker
# watchlist sweep's would average two populations with different base rates.
CLAIM_24H = "direction_mkt_24h"
CLAIM_1W = "direction_mkt_1w"
CLAIM_1M = "direction_mkt_1m"
CLAIM_6M = "direction_mkt_6m"

# (key, label, claim_type, equity periods, crypto periods)
#
# Crypto horizons are CALENDAR days and equity horizons are SESSIONS, so a week is 7 for one
# and 5 for the other. Both denote the same wall-clock span, which is the only way the two
# columns on the page mean the same thing.
HORIZONS = (
    ("24h", "24 hours", CLAIM_24H, 1, 1),
    ("1w", "1 week", CLAIM_1W, 5, 7),
    ("1m", "1 month", CLAIM_1M, 21, 30),
    ("6m", "6 months", CLAIM_6M, 126, 182),
)

HORIZON_KEYS = tuple(h[0] for h in HORIZONS)
CLAIM_TYPES = tuple(h[2] for h in HORIZONS)

# Horizons with no demonstrated skill anywhere in this system yet. Shown, graded, and labelled
# — never quietly presented as though the record supported them.
UNPROVEN_NOTE = {
    "24h": ("The one-day horizon was measured at resolution 0.0000 on single names and retired "
            "on 2026-09-04. This is the same horizon on indices and crypto. Treat it as an "
            "experiment being graded, not as a signal."),
    "1w": "Weekly direction has never cleared the gradeability floor with any skill. Unproven.",
    "1m": "Closest horizon to the 30-45 DTE trade window. Still unproven — grading below.",
    "6m": ("Six months of horizon means the first claim written today cannot be graded until "
           "roughly six months from now. Nothing here is evidence yet, by construction."),
}

DEFAULT_ASSETS = [
    {"ticker": "SPY", "name": "S&P 500", "klass": "equity", "group": "US stock market"},
    {"ticker": "QQQ", "name": "Nasdaq 100", "klass": "equity", "group": "US stock market"},
    {"ticker": "IWM", "name": "Russell 2000", "klass": "equity", "group": "US stock market"},
    {"ticker": "BTC-USD", "name": "Bitcoin", "klass": "crypto", "group": "Crypto"},
    {"ticker": "ETH-USD", "name": "Ethereum", "klass": "crypto", "group": "Crypto"},
]

# Periods per year per calendar. Crypto never closes; equities trade 252 sessions.
PERIODS_PER_YEAR = {"equity": 252.0, "crypto": 365.0}

# Enough history for the 50-period slow average plus room to measure vol against its own past.
MIN_CLOSES = 60
# Vol is estimated over a window that scales with the horizon, floored so a one-day claim still
# reads a month of behaviour and capped so a six-month claim does not need three years of data.
VOL_WINDOW_MIN = 20
VOL_WINDOW_MAX = 120

WORD = {"up": "BULL", "down": "BEAR", "flat": "NEUTRAL", "none": "NO CALL"}


def _cfg(name, default):
    return getattr(config, name, default)


def assets() -> List[Dict]:
    """The tracked assets, from config if it declares them."""
    declared = _cfg("MARKET_FORECAST_ASSETS", None)
    return list(declared) if declared else list(DEFAULT_ASSETS)


def periods_per_year(klass: str) -> float:
    return PERIODS_PER_YEAR.get(klass, 252.0)


def horizon_periods(klass: str, spec) -> int:
    """This asset's count of its OWN days for a horizon spec (key, label, type, eq, cr)."""
    return int(spec[4] if klass == "crypto" else spec[3])


def step_days(d: date, n: int, klass: str) -> date:
    """n of this asset's days forward. Crypto counts calendar days; equities skip weekends.

    Holidays are not modelled, deliberately and for the same reason direction_forecast does not
    model them: a claim landing on one resolves against the last bar at or before it, the
    scorer refuses a settling bar that collapses the horizon to zero, and the grace window
    absorbs the slip. A market calendar here would buy an off-by-one correction at the cost of
    a dependency and a failure mode.
    """
    if klass == "crypto":
        return d + timedelta(days=int(n))
    return df.next_trading_day(d, int(n))


def realised_vol(closes: Sequence[float], window: int, ppy: float) -> Optional[float]:
    """Annualised realised volatility as a decimal, from log returns, on this asset's calendar."""
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(ppy)


def forecast(asset: Dict, closes: Sequence[float], spec, today: Optional[date] = None,
             apply_tilt: bool = True) -> Dict:
    """One horizon's call for one asset, or an explicit abstention.

    `call` is always present and "NO CALL" is a first-class outcome. A forecaster that cannot
    decline has to guess, and a guess written into the ledger poisons the record it exists to
    grade.

    `apply_tilt=False` produces the CLIMATOLOGY baseline: identical band, identical horizon,
    mean pinned at zero.
    """
    today = today or date.today()
    key, label, claim_type, _eq, _cr = spec
    klass = asset.get("klass", "equity")
    ppy = periods_per_year(klass)
    n = horizon_periods(klass, spec)

    out = {
        "ticker": asset["ticker"], "name": asset.get("name", asset["ticker"]),
        "klass": klass, "group": asset.get("group", ""),
        "horizon": key, "horizon_label": label, "claim_type": claim_type,
        "horizon_periods": n, "expected": "none", "call": "NO CALL",
        "probability": None, "p_up": None, "p_down": None, "p_flat": None,
        "price_at_claim": None, "sigma_pct": None, "flat_band_pct": None,
        "vol_window": None, "ir": 0.0, "mu_sigmas": 0.0, "drivers": [], "reason": "",
        "score_on": None, "resolves_on": None,
    }

    closes = [float(c) for c in closes if c]
    if len(closes) < MIN_CLOSES:
        out["reason"] = (f"{len(closes)} closes available, {MIN_CLOSES} needed for the trend "
                         f"and vol reads. No call made.")
        return out

    spot = closes[-1]
    out["price_at_claim"] = round(spot, 4)

    # The vol window scales with the horizon: a six-month band set from twenty sessions is a
    # reading of last month wearing a half-year label.
    window = max(VOL_WINDOW_MIN, min(VOL_WINDOW_MAX, n))
    if len(closes) < window + 1:
        window = min(window, len(closes) - 1)
    out["vol_window"] = window

    vol = realised_vol(closes, window, ppy)
    if not vol or vol <= 0:
        out["reason"] = "no realised-vol reading; the flat band cannot be set. No call made."
        return out

    t_frac = n / ppy
    sigma = vol * math.sqrt(t_frac)
    out["sigma_pct"] = round(sigma * 100, 3)
    out["flat_band_pct"] = round(sigma * EQUAL_THIRDS_SIGMAS * 100, 3)

    # The signal is direction_forecast's, unchanged and capped there. It is an ANNUALISED
    # information ratio and is horizon-free by construction, so scaling it by sqrt(t) here is
    # what turns it into this horizon's tilt in sigmas.
    t = df.tilt(closes) if apply_tilt else {
        "ir": 0.0, "trend_pct": None, "above_slow_pct": None,
        "drivers": ["climatology baseline — mean pinned at zero"]}
    mu = float(t["ir"]) * math.sqrt(t_frac)
    out["ir"] = round(float(t["ir"]), 4)
    out["mu_sigmas"] = round(mu, 5)
    out["drivers"] = list(t["drivers"])
    out["trend_pct"] = t.get("trend_pct")
    out["above_slow_pct"] = t.get("above_slow_pct")

    # Round the two tails and take flat as the REMAINDER of the rounded pair. Three separately
    # rounded numbers sum to 0.9999, and a distribution that does not sum to one shows up later
    # as a Brier score nobody can reconcile.
    b = EQUAL_THIRDS_SIGMAS
    p_up = round(1.0 - df._phi(b - mu), 4)
    p_down = round(df._phi(-b - mu), 4)
    p_flat = round(max(0.0, 1.0 - p_up - p_down), 4)
    out["p_up"], out["p_down"], out["p_flat"] = p_up, p_down, p_flat

    expected, probability = max((("up", p_up), ("down", p_down), ("flat", p_flat)),
                                key=lambda kv: kv[1])
    out["expected"] = expected
    out["call"] = WORD[expected]
    out["probability"] = round(probability, 4)

    settle = step_days(today, n, klass)
    out["score_on"] = settle.isoformat()
    # Resolution is deferred one of the asset's days past settlement: the settling bar must be
    # COMPLETE before it is read, and the desk's last cycle fires before the close.
    out["resolves_on"] = step_days(settle, 1, klass).isoformat()

    out["reason"] = (f"{out['name']} {out['call']} over {label} at {probability:.0%}; "
                     f"flat band ±{out['flat_band_pct']:.2f}% "
                     f"(1σ {out['sigma_pct']:.2f}% over {n} {klass} days)"
                     + (f". {'; '.join(out['drivers'])}" if out["drivers"] else ""))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Prices
# ─────────────────────────────────────────────────────────────────────────────
_PERIOD = "2y"
_price_cache: Dict[str, tuple] = {}      # ticker -> (fetched_at_epoch, closes)


def cache_age_seconds(ticker: str) -> Optional[float]:
    hit = _price_cache.get(ticker)
    return (time.time() - hit[0]) if hit else None


def _default_lookup(ticker: str) -> List[float]:
    """Closes for one asset, newest last. Never raises; an empty list means abstain.

    yfinance rather than the Coinbase reader in data/crypto for the crypto names, deliberately:
    the RESOLVER (auto_paper_cycle._resolve_predictions) grades every claim off
    fetcher.get_price_data, so a claim anchored to a Coinbase close and settled against a
    yfinance bar would carry a venue basis in every single grade. The forecast and its grade
    must read the same series or the record measures the gap between two exchanges.

    THE CACHE HERE IS NOT AN OPTIMISATION, IT IS A CORRECTNESS FIX.

    data/fetcher._cache is keyed per (ticker, period) and has NO expiry — it is sized for a
    scan process that runs for two minutes and exits. The cockpit is a long-lived server. A
    board that recomputes on every page load would read the SAME cached frame for as long as
    the window stayed open, so a page advertising a live regime call would in fact be showing
    whatever the first page load happened to fetch. That is the identical failure this whole
    piece of work exists to remove, one layer down.

    So this keeps its own short TTL and, on expiry, evicts fetcher's entry for the key it is
    about to request. Reaching into that private dict is deliberate and narrow: the public
    alternative, fetcher.clear_cache(), drops option chains and fundamentals a concurrent scan
    is mid-way through using.
    """
    ttl = float(_cfg("MARKET_FORECAST_CACHE_MIN", 10)) * 60.0
    hit = _price_cache.get(ticker)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    try:
        from data import fetcher
        try:
            fetcher._cache.pop(f"price_{ticker}_{_PERIOD}", None)
        except Exception:
            pass          # a fetcher without that cache is fine; it just re-fetches anyway
        d = fetcher.get_price_data(ticker, period=_PERIOD)
        if d is None or getattr(d, "empty", True):
            return hit[1] if hit else []
        closes = [float(c) for c in d["Close"].tolist() if c and c == c]
        if closes:
            _price_cache[ticker] = (time.time(), closes)
        return closes
    except Exception as e:
        logger.debug("[market_forecast] %s price lookup failed: %s", ticker, e)
        # A network blip narrows the page; it must not blank a board that was reading fine a
        # minute ago. The stale copy is returned and the page labels its age.
        return hit[1] if hit else []


# ─────────────────────────────────────────────────────────────────────────────
# The live board — recomputed on every page load, writes nothing
# ─────────────────────────────────────────────────────────────────────────────
def live_board(today: Optional[date] = None, price_lookup=None) -> Dict:
    """Every asset at every horizon, computed fresh. Never raises."""
    today = today or date.today()
    price_lookup = price_lookup or _default_lookup
    rows, errors = [], []
    for a in assets():
        try:
            closes = price_lookup(a["ticker"])
        except Exception as e:
            closes = []
            errors.append(f"{a['ticker']}: {e}")
        cells = {}
        for spec in HORIZONS:
            try:
                cells[spec[0]] = forecast(a, closes, spec, today=today)
            except Exception as e:
                logger.debug("[market_forecast] %s %s failed: %s", a["ticker"], spec[0], e)
                cells[spec[0]] = {"ticker": a["ticker"], "horizon": spec[0], "call": "NO CALL",
                                  "expected": "none", "probability": None,
                                  "reason": f"forecast failed: {e}"}
        spot = closes[-1] if closes else None
        rows.append({
            "ticker": a["ticker"], "name": a.get("name", a["ticker"]),
            "klass": a.get("klass", "equity"), "group": a.get("group", ""),
            "spot": spot, "closes": len(closes), "cells": cells,
            "price_age_s": cache_age_seconds(a["ticker"]),
            "drivers": (cells.get("1m") or {}).get("drivers") or [],
        })
    return {"at": datetime.now().isoformat(), "asof_date": today.isoformat(),
            "rows": rows, "errors": errors, "horizons": HORIZONS}


# ─────────────────────────────────────────────────────────────────────────────
# The dated claim — written once a day, and the only copy that is graded
# ─────────────────────────────────────────────────────────────────────────────
def record_daily(today: Optional[date] = None, price_lookup=None,
                 baseline: Optional[bool] = None, force: bool = False) -> Dict:
    """Write one dated claim per asset per horizon. Idempotent per (asset, horizon, day).

    Gated on the hour for the same reason direction_forecast's sweep is: the claim is anchored
    to `price_at_claim` and settles on a later close, so anchoring it at 09:35 would hand the
    24-hour call several hours of the move it is supposed to be predicting. Near the close the
    latest bar is effectively today's close, which is the anchor these claims are defined
    against. Crypto is anchored on the same clock so the two groups on the page are comparable.
    """
    today = today or date.today()
    stats = {"assets": 0, "recorded": 0, "abstained": 0, "failed": 0, "skipped_hour": False}
    if not _cfg("MARKET_FORECAST_ENABLED", True):
        return stats
    after = int(os.getenv("VEGA_MARKET_FORECAST_AFTER_HOUR",
                          str(_cfg("MARKET_FORECAST_AFTER_HOUR", 14))))
    if not force and datetime.now().hour < after:
        stats["skipped_hour"] = True
        return stats
    if baseline is None:
        baseline = bool(_cfg("MARKET_FORECAST_RECORD_BASELINE", True))
    price_lookup = price_lookup or _default_lookup

    # One read and one write for the whole sweep — see predictions.batch().
    with pred.batch():
        for a in assets():
            stats["assets"] += 1
            try:
                closes = price_lookup(a["ticker"])
            except Exception as e:
                logger.debug("[market_forecast] %s lookup failed: %s", a["ticker"], e)
                stats["failed"] += 1
                continue
            if not closes:
                stats["failed"] += 1
                continue
            wrote = 0
            for spec in HORIZONS:
                for is_base in ((False, True) if baseline else (False,)):
                    fc = forecast(a, closes, spec, today=today, apply_tilt=not is_base)
                    if fc["expected"] == "none":
                        continue
                    ctype = f"{spec[2]}_baseline" if is_base else spec[2]
                    suffix = "-base" if is_base else ""
                    pid = pred.record(
                        trade_id=f"mktfc-{a['ticker']}-{today.isoformat()}{suffix}",
                        ticker=a["ticker"],
                        claim_type=ctype,
                        claim=fc["reason"],
                        probability=fc["probability"],
                        resolves_on=fc["resolves_on"],
                        context={
                            "expected": fc["expected"],
                            "call": fc["call"],
                            "price_at_claim": fc["price_at_claim"],
                            "flat_band_pct": fc["flat_band_pct"],
                            "score_on": fc["score_on"],
                            "score_field": "close",
                            "horizon": fc["horizon"],
                            "horizon_days": fc["horizon_periods"],
                            "calendar": fc["klass"],
                            "sigma_pct": fc["sigma_pct"],
                            "vol_window": fc["vol_window"],
                            "mu_sigmas": fc["mu_sigmas"], "ir": fc["ir"],
                            "p_up": fc["p_up"], "p_down": fc["p_down"], "p_flat": fc["p_flat"],
                            "baseline": is_base,
                            "asset_name": fc["name"], "group": fc["group"],
                            "cohort": COHORT,
                            "close_logic": COHORT,   # grade(cohort=...) filters on this key
                        },
                    )
                    if pid and not is_base:
                        wrote += 1
            stats["recorded"] += wrote
            if not wrote:
                stats["abstained"] += 1
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# The graded record
# ─────────────────────────────────────────────────────────────────────────────
def claims(rows: Optional[Sequence[Dict]] = None) -> List[Dict]:
    src = rows if rows is not None else pred.load()
    return [r for r in src if (r.get("context") or {}).get("cohort") == COHORT]


def track_record(rows: Optional[Sequence[Dict]] = None) -> Dict:
    """Per horizon: the signal's grade, its climatology twin's grade, and the gap.

    Reported per horizon rather than pooled. A 24-hour call and a six-month call have different
    base rates, different bands and different amounts of noise, and a single pooled hit rate
    over the four would describe none of them.
    """
    src = list(rows if rows is not None else pred.load())
    mine = claims(src)
    g = pred.grade(mine)
    by_type = g.get("by_type") or {}
    min_n = int(_cfg("PREDICTION_MIN_FOR_GRADE", 10))

    out = []
    for key, label, ctype, _eq, _cr in HORIZONS:
        sig = by_type.get(ctype)
        base = by_type.get(f"{ctype}_baseline")
        open_n = sum(1 for r in mine
                     if r.get("claim_type") == ctype and r.get("status") == "open")
        made = sum(1 for r in mine if r.get("claim_type") == ctype)
        nxt = sorted(str(r.get("resolves_on") or "")[:10] for r in mine
                     if r.get("claim_type") == ctype and r.get("status") == "open"
                     and str(r.get("resolves_on") or "")[:10] >= date.today().isoformat())
        out.append({
            "horizon": key, "label": label, "claim_type": ctype,
            "made": made, "open": open_n,
            "next_resolution": nxt[0] if nxt else None,
            "signal": sig, "baseline": base,
            # The number that decides whether any of this is worth keeping. Resolution is
            # what raw Brier cannot answer: a forecaster reciting the base rate scores a
            # respectable Brier and a resolution of zero.
            "resolution": (sig or {}).get("resolution"),
            "skill_vs_baseline": (
                round((sig["resolution"] or 0) - (base["resolution"] or 0), 4)
                if sig and base and sig.get("resolution") is not None
                and base.get("resolution") is not None else None),
            "gradeable": bool(sig and sig.get("gradeable")),
            "verdict": (sig or {}).get("verdict") or
                       (f"{open_n} claim(s) in flight, none resolved yet"
                        if open_n else "no claims written yet"),
            "note": UNPROVEN_NOTE.get(key, ""),
            "min_n": min_n,
        })
    return {"cohort": COHORT, "total": len(mine),
            "resolved": g.get("resolved", 0), "open": g.get("open", 0),
            "unresolvable": g.get("unresolvable", 0), "horizons": out}


def recent(limit: int = 40, rows: Optional[Sequence[Dict]] = None) -> List[Dict]:
    """The most recent NON-baseline claims, newest first. The twins are graded, not displayed."""
    mine = [r for r in claims(rows) if not (r.get("context") or {}).get("baseline")]
    return sorted(mine, key=lambda r: str(r.get("made_at") or ""), reverse=True)[:limit]


# ─────────────────────────────────────────────────────────────────────────────
def _print_board() -> None:
    b = live_board()
    print(f"\nMarket regime — live, {b['at'][:19]}\n")
    hdr = f"{'asset':<16}{'spot':>12}   " + "".join(f"{h[0]:>16}" for h in HORIZONS)
    print(hdr)
    print("-" * len(hdr))
    for r in b["rows"]:
        spot = f"{r['spot']:,.2f}" if r["spot"] else "—"
        line = f"{r['name'][:15]:<16}{spot:>12}   "
        for h in HORIZONS:
            c = r["cells"].get(h[0]) or {}
            p = c.get("probability")
            line += f"{(c.get('call') or '—') + (f' {p:.0%}' if p else ''):>16}"
        print(line)
    for e in b["errors"]:
        print(f"  ! {e}")
    print()


def _print_grades() -> None:
    t = track_record()
    print(f"\nTrack record — cohort {t['cohort']}: {t['total']} claims, "
          f"{t['resolved']} resolved, {t['open']} open\n")
    for h in t["horizons"]:
        print(f"  {h['label']:<10} made={h['made']:<5} open={h['open']:<5} "
              f"resolution={h['resolution'] if h['resolution'] is not None else '—'}")
        print(f"    {h['verdict']}")
    print()


if __name__ == "__main__":                            # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="VEGA market regime forecast")
    ap.add_argument("--record", action="store_true", help="write today's dated claims")
    ap.add_argument("--force", action="store_true", help="record regardless of the hour gate")
    ap.add_argument("--grade", action="store_true", help="print the graded track record")
    args = ap.parse_args()
    if args.record:
        print(record_daily(force=args.force))
    if args.grade:
        _print_grades()
    if not args.record and not args.grade:
        _print_board()
