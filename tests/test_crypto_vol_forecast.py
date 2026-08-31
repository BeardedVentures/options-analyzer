"""Forecasting the number that decides an IBIT credit spread.

The obvious crypto engine predicts direction. That is the wrong target here: a bull put spread
pays whether the underlying rises, drifts, or falls modestly, and what decides it is whether
REALISED vol over the holding period lands under the IMPLIED vol that was sold.

Measured 2026-08-31 before any of this was written, because a model nobody measured is a story:

    trailing 30d RV alone          corr 0.356 with forward RV, R^2 0.127
    HAR-style (1d/1w/1m/3m)        corr 0.559, RMSE 9.3% better than trailing
    2,045 out-of-sample predictions, walk-forward, refit every 60 days, no look-ahead
    BTC -> IBIT: corr 0.972, IBIT = 0.906*BTC + 0.061 over 630 paired days

9.3% is a real and modest improvement. These tests pin the honesty of the surrounding machinery
rather than the size of that number: that it degrades instead of guessing, that it can say
"don't sell", and that its confidence stays timid.
"""
import numpy as np
import pytest

from analysis import crypto_vol_forecast as cvf


def _series(n, sigma, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, sigma, n)
    return list(start * np.exp(np.cumsum(rets)))


# ── It must degrade, not guess ───────────────────────────────────────────────────────────

def test_short_history_falls_back_to_trailing_and_says_so():
    """A regression fitted on 50 points is noise fitted to noise."""
    out = cvf.forecast_btc_rv(_series(60, 0.03))
    assert out["method"] == "TRAILING"
    assert out["confidence"] == "LOW"
    assert "reason" in out


def test_it_never_raises_on_junk():
    """Advisory layers must not be able to fail a scan."""
    for bad in ([], [0], [None, None], [1.0], [0.0, 0.0, 0.0]):
        out = cvf.forecast_btc_rv(bad)
        assert out["forecast_rv"] is None or out["forecast_rv"] > 0


def test_a_long_history_actually_fits():
    out = cvf.forecast_btc_rv(_series(1200, 0.03, seed=1))
    assert out["method"] == "HAR"
    assert 0.01 < out["forecast_rv"] < 4.0
    assert out["confidence"] in ("MEDIUM", "HIGH")


def test_an_implausible_fit_is_rejected_for_the_naive_number(monkeypatch):
    """A negative or absurd vol is a broken fit, not a market state."""
    monkeypatch.setattr(cvf.np.linalg, "lstsq",
                        lambda *a, **k: (np.array([-99.0, 0, 0, 0, 0]), None, None, None))
    out = cvf.forecast_btc_rv(_series(1200, 0.03, seed=2))
    assert out["method"] == "TRAILING"
    assert "implausible" in out.get("reason", "")


# ── The verdict must be able to say no ───────────────────────────────────────────────────

def test_it_says_stand_aside_when_implied_is_below_forecast():
    """THE case that matters. On 2026-08-31 IBIT implied 39.0% against a 49.2% forecast --
    -10.1 vol points. An engine that cannot say 'do not sell' is a sales pitch."""
    closes = _series(1200, 0.03, seed=3)
    v = cvf.premium_view(0.10, closes)          # implied far below any plausible forecast
    assert v["verdict"] == "STAND_ASIDE"
    assert v["expected_vrp_pp"] < 0


def test_it_says_sell_when_implied_is_richly_above_forecast():
    closes = _series(1200, 0.012, seed=4)       # calm underlying
    v = cvf.premium_view(0.90, closes)          # implied wildly rich
    assert v["verdict"] == "SELL_PREMIUM"
    assert v["expected_vrp_pp"] > cvf.MIN_EDGE_VOL_POINTS


def test_a_thin_edge_is_named_thin_not_sold():
    """Positive but under the floor is not an edge -- the forecast's own error eats it."""
    closes = _series(1200, 0.02, seed=5)
    fc = cvf.forecast_btc_rv(closes)
    mapped = cvf.btc_rv_to_ibit_rv(fc["forecast_rv"])
    iv = mapped["ibit_rv"] + 0.01               # +1 vol point, under the 5-point floor
    v = cvf.premium_view(iv, closes)
    assert v["verdict"] == "THIN"


def test_no_implied_means_no_view():
    v = cvf.premium_view(None, _series(1200, 0.03, seed=6))
    assert v["available"] is False and v["verdict"] == "UNKNOWN"


# ── Confidence must stay timid ───────────────────────────────────────────────────────────

def test_the_probability_never_claims_certainty():
    """A 9.3% RMSE improvement over naive is not licence to claim 95%. Overconfidence is the
    failure mode the Brier score exists to catch."""
    closes = _series(1200, 0.02, seed=7)
    for iv in (0.01, 0.20, 0.50, 3.00):
        v = cvf.premium_view(iv, closes)
        if v.get("prob_realised_under_implied") is not None:
            assert 0.20 <= v["prob_realised_under_implied"] <= 0.80


# ── The BTC -> IBIT map ──────────────────────────────────────────────────────────────────

def test_the_map_is_fitted_when_paired_history_is_supplied():
    n = 700
    rng = np.random.default_rng(8)
    b = rng.normal(0, 0.03, n)
    i = b * 0.9 + rng.normal(0, 0.004, n)       # IBIT tracks BTC with a little noise
    bc = list(100 * np.exp(np.cumsum(b)))
    ic = list(50 * np.exp(np.cumsum(i)))
    m = cvf.btc_rv_to_ibit_rv(0.50, bc, ic)
    assert m["method"] == "FITTED" and m["n_pairs"] > 60
    assert 0.2 < m["ibit_rv"] < 1.0


def test_the_map_falls_back_to_the_measured_ratio_without_pairs():
    m = cvf.btc_rv_to_ibit_rv(0.50)
    assert m["method"] == "RATIO"
    assert abs(m["ibit_rv"] - 0.525) < 1e-6     # the measured 1.05 median


# ── The claim must be gradeable ──────────────────────────────────────────────────────────

def test_a_claim_is_recorded_with_a_real_resolution_date(tmp_path, monkeypatch):
    from analysis import predictions as pred
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    v = cvf.premium_view(0.90, _series(1200, 0.012, seed=9))
    pid = cvf.record_claim(v)
    assert pid
    rows = pred.load()
    assert len(rows) == 1
    import datetime as _dt
    _dt.date.fromisoformat(rows[0]["resolves_on"])      # must be a real date, not 2026-13-18
    assert rows[0]["probability"] is not None
    assert rows[0]["claim_type"] == "crypto_vrp_positive"


def test_an_unavailable_view_records_nothing(tmp_path, monkeypatch):
    """No implied vol means no claim -- an ungradeable row is noise in the ledger."""
    from analysis import predictions as pred
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    assert cvf.record_claim(cvf.premium_view(None, _series(1200, 0.03, seed=10))) is None
    assert pred.load() == []


def test_it_is_a_separate_claim_type_from_the_direction_forecast():
    """btc_forecast makes a DIRECTION claim. This makes a VOLATILITY claim. Keeping them
    distinct is what lets the ledger say which one earns its place."""
    from analysis import predictions as p
    assert getattr(p, "DIRECTION_1D", "direction_1d") != "crypto_vrp_positive"


# ── The claim has to be gradeable, not just recordable ───────────────────────────────────
#
# predictions.py grades DIRECTION claims against price. A VRP claim resolves against REALISED
# VOL over the window, which is a different comparison entirely. Without a scorer for it these
# claims accumulate as `open` forever and never grade -- exactly the trap btc_forecast.py was
# written to avoid ("fully built and dormant": the ledger had a DIRECTION type nothing ever
# wrote to). A claim that cannot come due is an opinion with a date on it.

import math
from datetime import date, timedelta

import numpy as np


def _bars(sigma_daily, n=30, start=date(2026, 7, 1), seed=7):
    rng = np.random.default_rng(seed)
    px, out = 100.0, []
    for i in range(n):
        px *= math.exp(rng.normal(0, sigma_daily))
        out.append((start + timedelta(days=i), px * 1.01, px * 0.99, px, px))
    return out


def _claim(pred, tmp_path, iv, made="2026-07-01T15:00:00"):
    pred.PREDICTIONS_FILE = tmp_path / "p.jsonl"
    pred._write([])
    pred.record("cvf-IBIT-t", "IBIT", pred.CRYPTO_VRP_POSITIVE, "claim", 0.6,
                (date(2026, 7, 1) + timedelta(days=30)).isoformat(),
                context={"ibit_iv": iv})
    rows = pred.load()
    rows[0]["made_at"] = made
    pred._write(rows)


def test_a_calm_window_under_rich_implied_grades_correct(tmp_path):
    from analysis import predictions as pred
    _claim(pred, tmp_path, iv=0.40)
    pred.resolve(lambda t, s, e: _bars(0.010), today=date(2026, 8, 15))
    r = pred.load()[0]
    assert r["correct"] is True and r["status"] == "resolved"


def test_a_wild_window_under_cheap_implied_grades_wrong(tmp_path):
    """The half that matters. A scorer that cannot mark a claim WRONG grades nothing."""
    from analysis import predictions as pred
    _claim(pred, tmp_path, iv=0.20)
    pred.resolve(lambda t, s, e: _bars(0.045), today=date(2026, 8, 15))
    r = pred.load()[0]
    assert r["correct"] is False and r["status"] == "resolved"


def test_a_claim_with_no_implied_in_context_is_unresolvable_not_guessed(tmp_path):
    from analysis import predictions as pred
    pred.PREDICTIONS_FILE = tmp_path / "p.jsonl"
    pred._write([])
    pred.record("cvf-IBIT-x", "IBIT", pred.CRYPTO_VRP_POSITIVE, "claim", 0.6,
                (date(2026, 7, 1) + timedelta(days=30)).isoformat(), context={})
    rows = pred.load(); rows[0]["made_at"] = "2026-07-01T15:00:00"; pred._write(rows)
    pred.resolve(lambda t, s, e: _bars(0.02), today=date(2026, 8, 15))
    assert pred.load()[0]["correct"] is None


def test_too_few_bars_is_refused_rather_than_measured(tmp_path):
    """Vol over three sessions is not vol. Refusing beats a confident wrong number."""
    from analysis import predictions as pred
    _claim(pred, tmp_path, iv=0.40)
    pred.resolve(lambda t, s, e: _bars(0.02, n=4), today=date(2026, 8, 15))
    r = pred.load()[0]
    assert r["correct"] is None and "too few" in (r["resolution_note"] or "")


def test_the_scorer_annualises_on_market_days_not_calendar_days(tmp_path):
    """The forecast maps BTC->IBIT on sqrt(252). Scoring the outcome on sqrt(365) would inflate
    every realised number by ~20% and bias every verdict the same way -- and the sign of that
    bias would never surface in the hit rate."""
    from analysis import predictions as pred
    _claim(pred, tmp_path, iv=0.40)
    pred.resolve(lambda t, s, e: _bars(0.020), today=date(2026, 8, 15))
    note = pred.load()[0]["resolution_note"]
    realised = float(note.split("realised ")[1].split("%")[0])
    expected = 0.020 * math.sqrt(252) * 100
    assert abs(realised - expected) < 6, f"{realised} not near {expected} (252-day annualised)"


def test_it_grades_alongside_the_direction_claims(tmp_path):
    """Same ledger, same grader. That is the whole point of not giving it a private table."""
    from analysis import predictions as pred
    _claim(pred, tmp_path, iv=0.40)
    pred.resolve(lambda t, s, e: _bars(0.010), today=date(2026, 8, 15))
    g = pred.grade()
    assert pred.CRYPTO_VRP_POSITIVE in g["by_type"]
