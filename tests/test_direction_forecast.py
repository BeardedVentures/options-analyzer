"""Directional claims at four horizons, and the scorers that mark them.

This module is a MEASUREMENT INSTRUMENT, not an alpha engine — VEGA already tested a drift
input and rejected it (price_projection: sector relative strength, rank correlation +0.01 to
-0.04, none significant). Its job is to produce claims that mature in days instead of quarters,
so the question "do VEGA's probabilities mean anything?" gets an answer in a fortnight.

Which makes the tests here mostly about not fooling ourselves:

  * probabilities come from the band, so at zero tilt they MUST equal climatology exactly —
    if the signal cannot be switched off, it can never be charged for its own existence;
  * a one-day claim must be scored on ONE day. The obvious implementation resolves it on the
    following session and reads the last bar in the window, silently grading a two-day move;
  * an overnight claim is close-to-OPEN. Scored on the next close it quietly becomes a
    one-and-a-half day claim that still calls itself overnight;
  * a long call that only has to be SOLD is made whole on a touch; one HELD to expiry is made
    whole only at settlement. The gap between those is most of the variance.
"""
import math
from datetime import date

import pytest

from analysis import direction_forecast as df
from analysis import predictions as pred


# ── The model ────────────────────────────────────────────────────────────────

def _flat_series(n=120, px=100.0):
    """No trend, tiny alternating wiggle so realised vol is > 0 but the averages are equal."""
    return [px + (0.5 if i % 2 else -0.5) for i in range(n)]


def _uptrend(n=120, px=100.0, step=0.4):
    return [px + i * step for i in range(n)]


def test_phi_matches_the_normal_cdf():
    assert df._phi(0.0) == pytest.approx(0.5)
    assert df._phi(1.96) == pytest.approx(0.975, abs=1e-3)
    assert df._phi(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_probabilities_sum_to_one():
    fc = df.forecast("T", _flat_series(), pred.DIRECTION_1D, 1, "close", 0.5)
    assert fc["p_up"] + fc["p_down"] + fc["p_flat"] == pytest.approx(1.0, abs=1e-6)


def test_zero_tilt_is_exactly_symmetric():
    """Climatology: with no drift, up and down must be equally likely. If they are not, the
    band arithmetic is wrong and every probability downstream inherits it."""
    fc = df.forecast("T", _uptrend(), pred.DIRECTION_1W, 5, "close", df.EQUAL_THIRDS_SIGMAS, apply_tilt=False)
    assert fc["p_up"] == pytest.approx(fc["p_down"], abs=1e-9)
    assert fc["mu_sigmas"] == 0.0


def test_climatology_is_an_even_three_way_split():
    """The reason probabilities are derived rather than assigned, and the reason the band is
    0.4307 sigma: at equal thirds nothing is favoured before the signal speaks. A model
    asserting 60% on a 33% base rate is not confident, it is miscalibrated."""
    fc = df.forecast("T", _flat_series(), pred.DIRECTION_1M, 21, "close",
                     df.EQUAL_THIRDS_SIGMAS, apply_tilt=False)
    for k in ("p_up", "p_down", "p_flat"):
        assert fc[k] == pytest.approx(1 / 3, abs=0.002), k


def test_a_half_sigma_band_would_make_every_claim_flat():
    """Documents WHY the band is not the obvious 0.5. Flat wins by 7.5 points there and the
    capped tilt cannot close it, so every claim would be 'flat' — perfectly calibrated, zero
    resolution, nothing measured."""
    fc = df.forecast("T", _uptrend(step=5.0), pred.DIRECTION_1M, 21, "close", 0.5)
    assert fc["expected"] == "flat"
    thirds = df.forecast("T", _uptrend(step=5.0), pred.DIRECTION_1M, 21, "close",
                         df.EQUAL_THIRDS_SIGMAS)
    assert thirds["expected"] == "up", "at equal thirds the signal decides"


def test_the_signal_can_actually_move_the_answer():
    """A tilt that changes nothing is not a signal. Same series, tilt on vs off."""
    series = _uptrend()
    live = df.forecast("T", series, pred.DIRECTION_1M, 21, "close", df.EQUAL_THIRDS_SIGMAS, apply_tilt=True)
    base = df.forecast("T", series, pred.DIRECTION_1M, 21, "close", df.EQUAL_THIRDS_SIGMAS, apply_tilt=False)
    assert live["mu_sigmas"] > 0, "a clean uptrend must produce a positive tilt"
    assert live["ir"] > 0
    assert live["p_up"] > base["p_up"]
    assert live["p_down"] < base["p_down"]


def test_downtrend_tilts_the_other_way():
    down = list(reversed(_uptrend()))
    fc = df.forecast("T", down, pred.DIRECTION_1M, 21, "close", df.EQUAL_THIRDS_SIGMAS)
    assert fc["mu_sigmas"] < 0
    assert fc["p_down"] > fc["p_up"]


def test_tilt_is_capped():
    """A weak signal must not be able to manufacture a strong-looking probability."""
    violent = _uptrend(step=5.0)
    assert abs(df.tilt(violent)["ir"]) <= df.MAX_TILT_IR + 1e-9


def test_tilt_scales_with_the_square_root_of_time():
    """Drift accumulates linearly, sigma with sqrt(t) — so the tilt in SIGMAS must grow with
    sqrt(t). Holding it constant (the first version of this module) implied a one-day drift of
    0.2 sigma, over 100% annualised, and was most wrong where the signal is weakest."""
    s = _uptrend()
    d1 = df.forecast("T", s, pred.DIRECTION_1D, 1, "close", 0.5)
    d21 = df.forecast("T", s, pred.DIRECTION_1M, 21, "close", 0.5)
    assert d1["ir"] == pytest.approx(d21["ir"]), "the signal itself is horizon-free"
    assert d21["mu_sigmas"] > d1["mu_sigmas"]
    assert d21["mu_sigmas"] / d1["mu_sigmas"] == pytest.approx(math.sqrt(21), rel=0.02)


def test_one_day_tilt_is_negligible():
    """A 20/50 moving-average cross has essentially nothing to say about tonight's gap, and the
    arithmetic must say so rather than dressing it up."""
    d1 = df.forecast("T", _uptrend(step=5.0), pred.DIRECTION_1D, 1, "close", 0.5)
    assert abs(d1["mu_sigmas"]) < 0.02


def test_expanding_vol_shrinks_the_signal_without_reversing_it():
    calm = _uptrend(n=120, step=0.4)
    wild = calm[:-10] + [calm[-10] + (12 if i % 2 else -9) * (i + 1) for i in range(10)]
    t_calm, t_wild = df.tilt(calm), df.tilt(wild)
    if t_wild["ir"] and t_calm["ir"]:
        assert abs(t_wild["ir"]) <= abs(t_calm["ir"])


def test_band_widens_with_the_horizon():
    """A fixed percentage band would make 'flat' unreachable at one day and unmissable at one
    month. It must scale with sqrt(time)."""
    s = _flat_series()
    d1 = df.forecast("T", s, pred.DIRECTION_1D, 1, "close", 0.5)["flat_band_pct"]
    d21 = df.forecast("T", s, pred.DIRECTION_1M, 21, "close", 0.5)["flat_band_pct"]
    assert d21 > d1
    assert d21 / d1 == pytest.approx(math.sqrt(21), rel=0.02)


def test_abstains_rather_than_guessing_on_thin_history():
    fc = df.forecast("T", [100.0] * 10, pred.DIRECTION_1D, 1, "close", 0.5)
    assert fc["expected"] == "none"
    assert fc["probability"] is None


def test_abstains_when_volatility_cannot_be_measured():
    """A dead-flat series has zero realised vol, so the band is undefined — and a zero-width
    flat band would make every claim 'up' or 'down' at 50%, which is noise wearing a number."""
    fc = df.forecast("T", [100.0] * 120, pred.DIRECTION_1D, 1, "close", 0.5)
    assert fc["expected"] == "none"


# ── Dates: settlement is not resolution ──────────────────────────────────────

def test_settlement_precedes_resolution():
    d = df.claim_dates(1, today=date(2026, 8, 19))       # Wednesday
    assert d["score_on"] == "2026-08-20"
    assert d["resolves_on"] == "2026-08-21"


def test_dates_skip_weekends():
    d = df.claim_dates(1, today=date(2026, 8, 21))       # Friday
    assert d["score_on"] == "2026-08-24"                 # Monday
    assert d["resolves_on"] == "2026-08-25"


def test_one_week_is_five_trading_days_not_seven_calendar_days():
    d = df.claim_dates(5, today=date(2026, 8, 19))
    assert d["score_on"] == "2026-08-26"


# ── Scoring ──────────────────────────────────────────────────────────────────

def _bars(rows):
    """rows: (iso_date, high, low, close) or (iso_date, high, low, close, open)."""
    return [(date.fromisoformat(r[0]),) + tuple(r[1:]) for r in rows]


WEEK = _bars([
    ("2026-08-19", 101, 99, 100, 100),
    ("2026-08-20", 106, 100, 105, 100),      # +5% on the settling day
    ("2026-08-21", 108, 90,  92,  91),       # then gives it all back
])


def test_one_day_claim_is_scored_on_one_day():
    """The bug this exists to stop. The claim is made 08-19, settles on 08-20's close and is
    read on 08-21 — so the window contains 08-21's bar. Without score_on the scorer reads the
    LAST close (92) and marks a +5% call wrong on a two-day move."""
    r = {"claim_type": pred.DIRECTION_1D, "context": {
        "price_at_claim": 100, "expected": "up", "flat_band_pct": 1.0,
        "score_on": "2026-08-20", "score_field": "close"}}
    correct, note = pred._score(r, WEEK)
    assert correct is True, note
    assert "+5.0%" in note


def test_without_score_on_the_same_claim_grades_the_wrong_window():
    """Proves the fix is load-bearing rather than decorative — and that legacy claims (the BTC
    forecast) keep their original behaviour."""
    r = {"claim_type": pred.DIRECTION, "context": {
        "price_at_claim": 100, "expected": "up", "flat_band_pct": 1.0}}
    correct, _ = pred._score(r, WEEK)
    assert correct is False


def test_overnight_claim_settles_on_the_open():
    """Close 100 on 08-20 -> open 91 on 08-21 is a -9% gap. Scored on 08-21's CLOSE (92) it
    would read -8%; the direction survives but the magnitude does not, and on a marginal gap
    the direction would flip too."""
    r = {"claim_type": pred.DIRECTION_OVERNIGHT, "context": {
        "price_at_claim": 100, "expected": "down", "flat_band_pct": 1.0,
        "score_on": "2026-08-21", "score_field": "open"}}
    correct, note = pred._score(r, WEEK)
    assert correct is True
    assert "-9.0%" in note


def test_overnight_claim_refuses_when_no_open_is_supplied():
    """An absent open must be a refusal, not a silent fallback to the close."""
    r = {"claim_type": pred.DIRECTION_OVERNIGHT, "context": {
        "price_at_claim": 100, "expected": "down", "flat_band_pct": 1.0,
        "score_on": "2026-08-21", "score_field": "open"}}
    no_opens = [b[:4] for b in WEEK]
    correct, note = pred._score(r, no_opens)
    assert correct is None
    assert "opening price" in note


def test_missing_settling_bar_is_unresolvable_not_wrong():
    r = {"claim_type": pred.DIRECTION_1W, "context": {
        "price_at_claim": 100, "expected": "up", "flat_band_pct": 1.0,
        "score_on": "2026-07-01", "score_field": "close"}}
    correct, note = pred._score(r, WEEK)
    assert correct is None and "no bar" in note


def test_baseline_claims_reach_the_same_scorer():
    """The climatology twin has its own claim type so it grades separately. If the dispatch
    missed it, a whole population would be marked unresolvable and the comparison would
    silently vanish."""
    assert pred.is_direction_claim("direction_1d_baseline")
    r = {"claim_type": "direction_1d_baseline", "context": {
        "price_at_claim": 100, "expected": "up", "flat_band_pct": 1.0,
        "score_on": "2026-08-20", "score_field": "close"}}
    assert pred._score(r, WEEK)[0] is True


# ── The lottery-tab claim ────────────────────────────────────────────────────

def test_move_exceeds_on_a_touch():
    """A call you can SELL is made whole the moment price trades through the level — high of
    108 clears a 6% move even though it settled at 92."""
    r = {"claim_type": pred.MOVE_EXCEEDS, "context": {
        "price_at_claim": 100, "move_pct": 6.0, "direction": "up", "basis": "touch"}}
    correct, note = pred._score(r, WEEK)
    assert correct is True and "touch" in note


def test_move_exceeds_on_settlement_is_a_different_answer():
    """Same claim, same bars, HELD to expiry: it settles at 92 and never earns the move. If
    these two agreed, the basis field would be decorative."""
    r = {"claim_type": pred.MOVE_EXCEEDS, "context": {
        "price_at_claim": 100, "move_pct": 6.0, "direction": "up", "basis": "settle",
        "score_on": "2026-08-21", "score_field": "close"}}
    assert pred._score(r, WEEK)[0] is False


def test_move_exceeds_downside():
    r = {"claim_type": pred.MOVE_EXCEEDS, "context": {
        "price_at_claim": 100, "move_pct": 8.0, "direction": "down", "basis": "touch"}}
    assert pred._score(r, WEEK)[0] is True          # low of 90 is -10%


def test_move_exceeds_needs_its_context():
    r = {"claim_type": pred.MOVE_EXCEEDS, "context": {"price_at_claim": 100}}
    assert pred._score(r, WEEK)[0] is None


# ── Band coverage ────────────────────────────────────────────────────────────

def test_band_contains_inside_and_outside():
    inside = {"claim_type": pred.BAND_CONTAINS, "context": {
        "band_low": 90, "band_high": 95, "score_on": "2026-08-21"}}
    outside = {"claim_type": pred.BAND_CONTAINS, "context": {
        "band_low": 99, "band_high": 110, "score_on": "2026-08-21"}}
    assert pred._score(inside, WEEK)[0] is True
    assert pred._score(outside, WEEK)[0] is False


def test_band_contains_without_a_band_is_unresolvable():
    r = {"claim_type": pred.BAND_CONTAINS, "context": {"score_on": "2026-08-21"}}
    assert pred._score(r, WEEK)[0] is None


# ── Recording ────────────────────────────────────────────────────────────────

def test_record_ticker_writes_a_live_and_baseline_claim_per_horizon(tmp_path, monkeypatch):
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    ids = df.record_ticker("TEST", _uptrend(), today=date(2026, 8, 19))
    rows = pred.load()
    types = {r["claim_type"] for r in rows}
    assert len(ids) == len(df.HORIZONS) * 2
    for ct, *_ in df.HORIZONS:
        assert ct in types and f"{ct}_baseline" in types


def test_recorded_claims_carry_what_the_scorer_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    df.record_ticker("TEST", _uptrend(), today=date(2026, 8, 19))
    for r in pred.load():
        c = r["context"]
        assert c["score_on"] < r["resolves_on"], "must settle before it is read"
        for k in ("expected", "price_at_claim", "flat_band_pct", "score_field"):
            assert c.get(k) is not None, f"{r['claim_type']} missing {k}"
        assert r["probability"] is not None


def test_recording_is_idempotent_within_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    df.record_ticker("TEST", _uptrend(), today=date(2026, 8, 19))
    before = len(pred.load())
    df.record_ticker("TEST", _uptrend(), today=date(2026, 8, 19))
    assert len(pred.load()) == before


def test_abstaining_ticker_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    assert df.record_ticker("TEST", [100.0] * 10, today=date(2026, 8, 19)) == []
    assert pred.load() == []


def test_watchlist_survives_a_dead_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(pred, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    def lookup(tk):
        raise RuntimeError("data source down")
    stats = df.record_watchlist(today=date(2026, 8, 19), tickers=["AAA", "BBB"],
                                price_lookup=lookup)
    assert stats["failed"] == 2 and stats["recorded"] == 0
