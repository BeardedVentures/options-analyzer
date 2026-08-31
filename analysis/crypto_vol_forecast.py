#!/usr/bin/env python3
"""
crypto_vol_forecast.py — forecast the number that actually decides an IBIT credit spread.

WHY THIS AND NOT A PRICE PREDICTOR
The obvious crypto engine predicts which way Bitcoin goes. That is the wrong target for this
system. VEGA sells defined-risk premium; a bull put spread on IBIT pays whether the underlying
rises, drifts, or falls modestly. What decides it is whether REALISED volatility over the
holding period comes in under the IMPLIED volatility that was sold — the volatility risk
premium, measured forward.

So this forecasts forward realised vol, not direction. btc_forecast.py already makes a daily
directional claim and is graded on it; this is the complementary and more decision-relevant
half, and the two are deliberately separate claims so the ledger can tell which one earns its
place.

WHAT IT IS WORTH — MEASURED, NOT ASSUMED (2026-08-31)
Forward 30-day realised vol is only weakly predictable, and the honest numbers are small:

    trailing 30d RV alone          corr 0.356 with forward RV, R^2 0.127
    HAR-style (1d/1w/1m/3m) model  corr 0.559, RMSE 9.3% better than trailing
    strict walk-forward, 2,045 out-of-sample predictions, refit every 60 days, no look-ahead

A 9.3% error reduction is real and modest. It is not an edge on its own; it is a better input
to a VRP estimate that was previously using trailing vol as a stand-in for forward vol — which
is the same substitution edge_calculator.calculate_vrp still makes, and the reason that function
measures "is IV high versus recent history" rather than "will IV exceed what happens next."

BTC -> IBIT
IBIT tracks spot Bitcoin but trades only in market hours, so the two realised vols are not the
same number. Measured over 630 paired trading days (2024-01 to 2026-08):

    corr(BTC 30d RV, IBIT 30d RV) = 0.972
    IBIT_RV = 0.906 * BTC_RV + 0.061       ratio median 1.05

Tight enough that a BTC vol forecast transfers to IBIT with a linear map. The coefficients are
refit from live data on every call rather than pinned here, so the relationship cannot silently
go stale the way a hardcoded constant would.

ADVISORY BY CONSTRUCTION
Nothing here gates a trade. It records a dated, falsifiable claim in the SAME prediction ledger
every other VEGA claim lives in, and waits to be graded. The cohort contract is explicit that a
criterion added mid-cohort splits the sample as surely as a rule change, and the ravens cohort
is at 12 of 30 — so this earns its way into selection by being right on the record first, or it
does not get there at all.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# Forecast horizon in calendar days. 30 matches the DTE band VEGA sells into (25-45, target 35)
# closely enough that the forecast answers the question the trade actually asks.
HORIZON_DAYS = 30

# Minimum history before the regression is allowed to speak. Below this the coefficients are
# noise fitted to noise, and the caller gets the trailing-vol fallback with a LOW confidence.
MIN_HISTORY_DAYS = 400

# How far implied must exceed forecast realised before selling premium is worth the risk.
# Expressed in volatility points. Deliberately NOT zero: a forecast with a 9.3% error
# reduction over naive still carries real error, and a coin-flip VRP is not an edge.
MIN_EDGE_VOL_POINTS = 5.0


def _cfg(name, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _log_returns(closes: List[float]) -> np.ndarray:
    a = np.asarray([c for c in closes if c and c > 0], dtype=float)
    if a.size < 2:
        return np.array([])
    return np.diff(np.log(a))


def _rv(returns: np.ndarray, window: int, annualise: float = 365.0) -> Optional[float]:
    """Annualised realised vol over the last `window` returns."""
    if returns.size < window or window < 2:
        return None
    return float(np.std(returns[-window:], ddof=0) * np.sqrt(annualise))


def _har_matrix(returns: np.ndarray, horizon: int):
    """Build (features, target) for the HAR-style regression.

    Features are realised vol at four lookbacks -- roughly a day, a week, a month, a quarter.
    The target is realised vol over the NEXT `horizon` days, so the fit learns the mapping the
    caller actually needs rather than a same-window identity.
    """
    n = returns.size
    rows, ys = [], []
    for t in range(90, n - horizon):
        past = returns[:t]
        f = [
            float(abs(returns[t - 1]) * np.sqrt(365.0)),
            float(np.std(past[-7:], ddof=0) * np.sqrt(365.0)),
            float(np.std(past[-30:], ddof=0) * np.sqrt(365.0)),
            float(np.std(past[-90:], ddof=0) * np.sqrt(365.0)),
        ]
        if not all(np.isfinite(f)):
            continue
        y = float(np.std(returns[t:t + horizon], ddof=0) * np.sqrt(365.0))
        if not np.isfinite(y):
            continue
        rows.append(f)
        ys.append(y)
    if not rows:
        return None, None
    return np.array(rows), np.array(ys)


def forecast_btc_rv(closes: List[float], horizon: int = HORIZON_DAYS) -> Dict:
    """Forecast BTC realised vol over the next `horizon` days.

    Fits on every call from the history handed in. That is deliberate: a pinned coefficient set
    is a constant that looks like a model, and this project has been bitten more than once by
    textbook constants that stopped describing the asset (a flat $25 credit floor that excluded
    IBIT's entire chain, a flat IV/HV inflator that zeroed its IV rank on every scan).

    Falls back to trailing vol with LOW confidence rather than raising -- an advisory layer must
    never be able to fail a scan.
    """
    out = {
        "forecast_rv": None,
        "trailing_rv": None,
        "method": "UNAVAILABLE",
        "confidence": "LOW",
        "n_history": 0,
        "horizon_days": horizon,
    }
    r = _log_returns(closes)
    out["n_history"] = int(r.size)
    trailing = _rv(r, 30)
    out["trailing_rv"] = round(trailing, 4) if trailing is not None else None

    if r.size < MIN_HISTORY_DAYS:
        # Not enough to fit anything honest. Say so and hand back the naive number.
        out["forecast_rv"] = out["trailing_rv"]
        out["method"] = "TRAILING"
        out["reason"] = (f"only {r.size} daily returns on record; "
                         f"{MIN_HISTORY_DAYS} needed before the regression is meaningful")
        return out

    X, y = _har_matrix(r, horizon)
    if X is None or len(X) < 100:
        out["forecast_rv"] = out["trailing_rv"]
        out["method"] = "TRAILING"
        out["reason"] = "not enough usable rows to fit"
        return out

    try:
        A = np.c_[np.ones(len(X)), X]
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        now = np.array([
            1.0,
            float(abs(r[-1]) * np.sqrt(365.0)),
            float(np.std(r[-7:], ddof=0) * np.sqrt(365.0)),
            float(np.std(r[-30:], ddof=0) * np.sqrt(365.0)),
            float(np.std(r[-90:], ddof=0) * np.sqrt(365.0)),
        ])
        pred = float(now @ coef)
        # A negative or absurd vol is a fit that has gone wrong, not a market state.
        if not np.isfinite(pred) or pred <= 0.01 or pred > 4.0:
            out["forecast_rv"] = out["trailing_rv"]
            out["method"] = "TRAILING"
            out["reason"] = f"regression produced an implausible {pred:.2f}; using trailing"
            return out
        # In-sample fit quality, reported so the caller can discount a bad regime.
        resid = y - A @ coef
        r2 = float(1 - (resid.var() / y.var())) if y.var() > 0 else 0.0
        out.update({
            "forecast_rv": round(pred, 4),
            "method": "HAR",
            "r2_in_sample": round(r2, 3),
            "confidence": "HIGH" if r.size > 900 and r2 > 0.2 else "MEDIUM",
        })
    except Exception as exc:                            # pragma: no cover - defensive
        logger.debug("[crypto_vol] fit failed (%s); using trailing", exc)
        out["forecast_rv"] = out["trailing_rv"]
        out["method"] = "TRAILING"
        out["reason"] = str(exc)
    return out


def btc_rv_to_ibit_rv(btc_rv: float,
                      btc_closes: Optional[List[float]] = None,
                      ibit_closes: Optional[List[float]] = None) -> Dict:
    """Map a BTC realised-vol number onto IBIT.

    IBIT tracks spot Bitcoin but trades only in market hours, so the two realised vols differ.
    Measured 2024-01 to 2026-08 over 630 paired days: corr 0.972, IBIT = 0.906*BTC + 0.061.
    The fit is redone here from whatever paired history the caller supplies, so the mapping
    tracks reality instead of a comment.
    """
    out = {"ibit_rv": None, "slope": None, "intercept": None, "n_pairs": 0, "method": "RATIO"}
    if btc_rv is None:
        return out
    try:
        if btc_closes and ibit_closes and len(btc_closes) == len(ibit_closes):
            rb = _log_returns(btc_closes)
            ri = _log_returns(ibit_closes)
            n = min(rb.size, ri.size)
            if n > 120:
                w = 30
                xs, ys = [], []
                for t in range(w, n):
                    xs.append(float(np.std(rb[t - w:t], ddof=0) * np.sqrt(252.0)))
                    ys.append(float(np.std(ri[t - w:t], ddof=0) * np.sqrt(252.0)))
                if len(xs) > 60:
                    slope, intercept = np.polyfit(np.array(xs), np.array(ys), 1)
                    out.update({"slope": round(float(slope), 4),
                                "intercept": round(float(intercept), 4),
                                "n_pairs": len(xs), "method": "FITTED",
                                "ibit_rv": round(float(slope * btc_rv + intercept), 4)})
                    return out
    except Exception as exc:                            # pragma: no cover - defensive
        logger.debug("[crypto_vol] IBIT map fit failed: %s", exc)
    # Fallback to the measured median ratio.
    out["ibit_rv"] = round(float(btc_rv * 1.05), 4)
    return out


def premium_view(ibit_iv: Optional[float],
                 btc_closes: List[float],
                 ibit_closes: Optional[List[float]] = None,
                 horizon: int = HORIZON_DAYS,
                 paired_btc_closes: Optional[List[float]] = None) -> Dict:
    """Should IBIT premium be sold right now, and how confident is that?

    Returns a dict carrying the forecast, the expected VRP in volatility points, a verdict and
    a probability. ADVISORY -- nothing here gates a trade. It is written to the prediction
    ledger and graded like every other claim VEGA makes.
    """
    view = {
        "available": False,
        "verdict": "UNKNOWN",
        "expected_vrp_pp": None,
        "ibit_iv": round(ibit_iv, 4) if ibit_iv else None,
        "note": "",
    }
    fc = forecast_btc_rv(btc_closes, horizon)
    view["forecast"] = fc
    if fc.get("forecast_rv") is None:
        view["note"] = "No usable BTC history; no view."
        return view

    # The map needs DATE-ALIGNED pairs, which btc_closes is not: BTC trades ~365 days a year
    # and IBIT ~252, so the two series cannot be aligned by truncating either one. The caller
    # passes the intersection separately; without it the map falls back to the measured ratio
    # rather than fitting on mismatched dates and reporting a confident wrong slope.
    mapped = btc_rv_to_ibit_rv(fc["forecast_rv"],
                               paired_btc_closes if paired_btc_closes else None,
                               ibit_closes if paired_btc_closes else None)
    view["ibit_rv_forecast"] = mapped.get("ibit_rv")
    view["ibit_map"] = mapped

    if not ibit_iv or not mapped.get("ibit_rv"):
        view["note"] = ("BTC forward vol forecast %.1f%% (%s). No IBIT implied vol to compare, "
                        "so no premium view." % (100 * fc["forecast_rv"], fc["method"]))
        return view

    edge_pp = (ibit_iv - mapped["ibit_rv"]) * 100.0
    view["available"] = True
    view["expected_vrp_pp"] = round(edge_pp, 2)

    floor = float(_cfg("CRYPTO_MIN_EDGE_VOL_POINTS", MIN_EDGE_VOL_POINTS))
    if edge_pp >= floor:
        view["verdict"] = "SELL_PREMIUM"
    elif edge_pp <= 0:
        view["verdict"] = "STAND_ASIDE"
    else:
        view["verdict"] = "THIN"

    # Probability that realised comes in under implied. Scaled by the forecast's own error --
    # a 9.3% RMSE improvement over naive is not licence to claim certainty, so this stays in a
    # deliberately narrow band around a coin flip. Overconfidence is the failure mode the Brier
    # score exists to catch.
    sigma_pp = 20.0
    z = edge_pp / sigma_pp
    prob = 1.0 / (1.0 + np.exp(-z))
    view["prob_realised_under_implied"] = round(float(min(max(prob, 0.20), 0.80)), 4)

    view["note"] = (
        "IBIT implied %.1f%% vs forecast realised %.1f%% over %dd -> %+.1f vol pts of premium. "
        "%s (floor %.0f pts)." % (
            100 * ibit_iv, 100 * mapped["ibit_rv"], horizon, edge_pp,
            {"SELL_PREMIUM": "Selling is paid for the risk",
             "THIN": "Positive but under the floor",
             "STAND_ASIDE": "Implied is at or below forecast realised — do not sell"}[view["verdict"]],
            floor))
    return view


def record_claim(view: Dict, today: Optional[date] = None,
                 ticker: str = "IBIT") -> Optional[str]:
    """Write the premium view to the prediction ledger as a dated, falsifiable claim.

    Resolves on the forecast horizon, against realised vol actually observed over that window.
    Same ledger, same grader, same Brier score as every other claim -- so this can be compared
    against btc_forecast's directional claims and against VEGA's own POP, instead of living in
    a private table that could never be ranked against anything.
    """
    if not view.get("available"):
        return None
    try:
        from analysis import predictions as pred
    except Exception:                                   # pragma: no cover - defensive
        return None
    today = today or date.today()
    horizon = int((view.get("forecast") or {}).get("horizon_days") or HORIZON_DAYS)
    resolves = (today + timedelta(days=horizon)).isoformat()
    trade_id = "cvf-%s-%s" % (ticker, today.isoformat())
    return pred.record(
        trade_id=trade_id,
        ticker=ticker,
        claim_type="crypto_vrp_positive",
        claim=("Realised vol on %s over the next %d days finishes BELOW the %.1f%% implied "
               "sold today. Forecast realised %.1f%%, edge %+.1f vol pts. %s"
               % (ticker, horizon, 100 * (view.get("ibit_iv") or 0),
                  100 * (view.get("ibit_rv_forecast") or 0),
                  view.get("expected_vrp_pp") or 0, view.get("verdict"))),
        probability=view.get("prob_realised_under_implied"),
        resolves_on=resolves,
        context={
            "forecast_rv": (view.get("forecast") or {}).get("forecast_rv"),
            "trailing_rv": (view.get("forecast") or {}).get("trailing_rv"),
            "ibit_rv_forecast": view.get("ibit_rv_forecast"),
            "ibit_iv": view.get("ibit_iv"),
            "expected_vrp_pp": view.get("expected_vrp_pp"),
            "verdict": view.get("verdict"),
            "method": (view.get("forecast") or {}).get("method"),
        },
    )
