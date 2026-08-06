"""Prediction ledger — analysis/predictions.py.

VEGA recorded what HAPPENED and never what it CLAIMED, so nothing it asserts was ever marked.
"The strike is 0.52 sigma away", "EARLY: premium improves below RSI 50", "event spike at Sep
11", "3-touch support holds" — all falsifiable inside a known window, all discarded the moment
they were printed. That is why the calibration engine could only grade modelled POP: it was
the one prediction that happened to be stored.

The property these tests protect is that a claim must be scorable against reality, not against
the system's own opinion of itself. Every scorer here resolves against price history.
"""
import datetime
import json
import tempfile
from pathlib import Path

import pytest

import config
from analysis import predictions as P


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch):
    """Never touch the real ledger."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", Path(tempfile.mkdtemp()) / "p.jsonl")


def _bars(*closes, high=None, low=None):
    """(date, high, low, close) rows."""
    return [(datetime.date(2026, 7, 1) + datetime.timedelta(days=i),
             high if high is not None else c + 1,
             low if low is not None else c - 1, c)
            for i, c in enumerate(closes)]


def _trade(**over):
    t = {"ticker": "AAA", "short_strike": 100.0, "expiration": "2026-08-01",
         "current_price": 110.0, "true_pop": 0.80, "p_max_profit": 0.72,
         "strategy": "bull_put_spread", "close_logic": "ravens_v1"}
    t.update(over)
    return t


# ── Recording ─────────────────────────────────────────────────────────────────────────────────

def test_a_claim_without_a_horizon_is_an_opinion_not_a_prediction():
    assert P.record("T1", "AAA", P.STRIKE_HOLDS, "x", 0.8, None) is None


def test_claims_are_deduplicated_per_trade_and_type():
    P.record("T1", "AAA", P.STRIKE_HOLDS, "x", 0.8, "2026-08-01")
    P.record("T1", "AAA", P.STRIKE_HOLDS, "x", 0.9, "2026-08-01")
    assert len(P.load()) == 1


def test_the_engine_already_makes_five_falsifiable_claims_per_trade():
    """Nothing new is being predicted here — assertions the engine already makes are being
    written down instead of discarded."""
    ids = P.record_trade_predictions(
        _trade(support_levels=[{"price": 105.0, "touches": 3, "strength": 70.0}],
               entry_timing={"readiness": "EARLY", "timing_gate_pass": False},
               event_expiry_flag=True, event_expiry_date="2026-07-25"),
        "T1")
    kinds = {i.split("::")[1] for i in ids}
    assert kinds == {P.STRIKE_HOLDS, P.STRIKE_UNTOUCHED, P.LEVEL_HOLDS,
                     P.TIMING_IMPROVES, P.EVENT_REALISED}


def test_confidence_is_stored_so_calibration_is_possible():
    """Accuracy alone cannot separate a well-calibrated 60% from an overconfident 95%."""
    P.record_trade_predictions(_trade(), "T1")
    rows = P.load()
    assert all(r["probability"] is not None for r in rows)
    assert next(r for r in rows if r["claim_type"] == P.STRIKE_HOLDS)["probability"] == 0.80


def test_disabled_ledger_records_nothing(monkeypatch):
    monkeypatch.setattr(config, "PREDICTION_LEDGER_ENABLED", False, raising=False)
    assert P.record("T1", "AAA", P.STRIKE_HOLDS, "x", 0.8, "2026-08-01") is None


# ── Scoring against reality ───────────────────────────────────────────────────────────────────

_seq = [0]


def _resolve_one(claim_type, ctx, bars, prob=0.8):
    """Unique trade id per call — claims are deduplicated per (trade, type), so reusing one id
    made every later case silently read the FIRST case's result."""
    _seq[0] += 1
    tid = f"T{_seq[0]}"
    P.record(tid, "AAA", claim_type, "c", prob, "2026-07-10", ctx)
    P.resolve(lambda t, a, b: bars, today=datetime.date(2026, 8, 6))
    return next(r for r in P.load() if r["trade_id"] == tid)


def test_strike_holds_scores_against_the_settle():
    ctx = {"short_strike": 100.0, "strategy": "bull_put_spread"}
    assert _resolve_one(P.STRIKE_HOLDS, ctx, _bars(110, 108, 105))["correct"] is True
    assert _resolve_one(P.STRIKE_HOLDS, ctx, _bars(110, 108, 95))["correct"] is False


def test_strike_holds_flips_for_the_call_side():
    """The same settle is a win for one side and a loss for the other."""
    bars = _bars(95)
    put = _resolve_one(P.STRIKE_HOLDS, {"short_strike": 100.0, "strategy": "bull_put_spread"}, bars)
    call = _resolve_one(P.STRIKE_HOLDS, {"short_strike": 100.0, "strategy": "bear_call_spread"}, bars)
    assert put["correct"] is False and call["correct"] is True


def test_untouched_is_stricter_than_holds():
    """Price can dip through a strike and still settle above it — two different claims."""
    bars = [(datetime.date(2026, 7, 1), 112, 95, 110),
            (datetime.date(2026, 7, 2), 112, 108, 111)]
    ctx = {"short_strike": 100.0, "strategy": "bull_put_spread"}
    assert _resolve_one(P.STRIKE_HOLDS, ctx, bars)["correct"] is True
    assert _resolve_one(P.STRIKE_UNTOUCHED, ctx, bars)["correct"] is False


def test_level_holds_scores_on_closes_not_wicks():
    ctx = {"level": 100.0, "side": "support"}
    wick = [(datetime.date(2026, 7, 1), 110, 95, 105)]      # dipped, closed above
    close_below = [(datetime.date(2026, 7, 1), 110, 95, 98)]
    assert _resolve_one(P.LEVEL_HOLDS, ctx, wick)["correct"] is True
    assert _resolve_one(P.LEVEL_HOLDS, ctx, close_below)["correct"] is False


def test_event_realised_checks_for_an_actual_move():
    ctx = {"move_threshold_pct": 4.0}
    assert _resolve_one(P.EVENT_REALISED, ctx, _bars(100, 101, 102))["correct"] is False
    assert _resolve_one(P.EVENT_REALISED, ctx, _bars(100, 92, 93))["correct"] is True


def test_timing_claim_is_scored_on_movement_toward_the_short_side():
    ctx = {"short_strike": 100.0, "strategy": "bull_put_spread", "price_at_claim": 110.0}
    assert _resolve_one(P.TIMING_IMPROVES, ctx, _bars(110, 104, 106))["correct"] is True
    assert _resolve_one(P.TIMING_IMPROVES, ctx, _bars(110, 115, 118))["correct"] is False


def test_direction_claim():
    ctx = {"price_at_claim": 100.0, "expected": "up"}
    assert _resolve_one(P.DIRECTION, ctx, _bars(100, 105, 108))["correct"] is True
    assert _resolve_one(P.DIRECTION, ctx, _bars(100, 95, 90))["correct"] is False


# ── Resolution mechanics ──────────────────────────────────────────────────────────────────────

def test_claims_before_their_horizon_are_left_alone():
    P.record("T1", "AAA", P.STRIKE_HOLDS, "c", 0.8, "2026-12-31",
             {"short_strike": 100.0, "strategy": "bull_put_spread"})
    P.resolve(lambda t, a, b: _bars(110), today=datetime.date(2026, 8, 6))
    assert P.load()[0]["status"] == "open"


def test_missing_price_history_is_unresolvable_not_wrong():
    """An unmarkable claim must never be scored as a miss — that would punish the model for a
    data outage."""
    P.record("T1", "AAA", P.STRIKE_HOLDS, "c", 0.8, "2026-07-10",
             {"short_strike": 100.0, "strategy": "bull_put_spread"})
    P.resolve(lambda t, a, b: [], today=datetime.date(2026, 8, 6))
    r = P.load()[0]
    assert r["status"] == "unresolvable" and r["correct"] is None


def test_a_lookup_that_raises_is_survivable():
    P.record("T1", "AAA", P.STRIKE_HOLDS, "c", 0.8, "2026-07-10", {"short_strike": 100.0})
    def boom(*a):
        raise RuntimeError("network")
    stats = P.resolve(boom, today=datetime.date(2026, 8, 6))
    assert stats["unresolvable"] == 1


def test_resolution_is_idempotent():
    P.record("T1", "AAA", P.STRIKE_HOLDS, "c", 0.8, "2026-07-10",
             {"short_strike": 100.0, "strategy": "bull_put_spread"})
    P.resolve(lambda t, a, b: _bars(110), today=datetime.date(2026, 8, 6))
    second = P.resolve(lambda t, a, b: _bars(90), today=datetime.date(2026, 8, 6))
    assert second["checked"] == 0
    assert P.load()[0]["correct"] is True     # not re-scored


# ── Grading ───────────────────────────────────────────────────────────────────────────────────

def _seed(n, correct, prob, ctype=P.STRIKE_HOLDS):
    rows = []
    for i in range(n):
        rows.append({"id": f"x{i}", "trade_id": f"t{i}", "ticker": "AAA",
                     "claim_type": ctype, "claim": "c", "probability": prob,
                     "status": "resolved", "correct": correct, "context": {}})
    return rows


def test_unresolved_claims_do_not_count_toward_a_grade():
    g = P.grade(_seed(5, True, 0.8) + [{"claim_type": P.STRIKE_HOLDS, "status": "open",
                                        "correct": None, "context": {}}])
    assert g["by_type"][P.STRIKE_HOLDS]["n"] == 5


def test_small_samples_are_not_graded():
    g = P.grade(_seed(3, True, 0.8))
    assert g["by_type"][P.STRIKE_HOLDS]["gradeable"] is False
    assert "not gradeable yet" in g["by_type"][P.STRIKE_HOLDS]["verdict"]


def test_overconfidence_is_named_not_just_accuracy():
    """A predictor right 50% of the time while claiming 95% is the dangerous failure, and
    accuracy alone cannot distinguish it from a well-calibrated 50%."""
    # 80% right while claiming 95%: the direction is genuinely useful, the certainty is not.
    # A 50/95 split would be caught earlier and more harshly by the Brier check, which is
    # correct — at a coin-flip hit rate the claim is not merely overconfident, it is noise.
    g = P.grade(_seed(16, True, 0.95) + _seed(4, False, 0.95))
    v = g["by_type"][P.STRIKE_HOLDS]
    assert v["bias_pp"] > 10
    assert "overconfident" in v["verdict"]


def test_underconfidence_is_named_too():
    g = P.grade(_seed(18, True, 0.55) + _seed(2, False, 0.55))
    v = g["by_type"][P.STRIKE_HOLDS]
    assert v["bias_pp"] < -10
    assert "deserves more weight" in v["verdict"]


def test_well_calibrated_reads_as_calibrated():
    g = P.grade(_seed(8, True, 0.8) + _seed(2, False, 0.8))
    assert "well calibrated" in g["by_type"][P.STRIKE_HOLDS]["verdict"]


def test_a_claim_type_worse_than_a_coin_flip_is_called_out():
    g = P.grade(_seed(6, True, 0.9) + _seed(14, False, 0.9))
    v = g["by_type"][P.STRIKE_HOLDS]
    assert v["brier"] > 0.25
    assert "not adding information" in v["verdict"]


def test_grading_can_be_scoped_to_a_cohort():
    """The legacy cohort was closed by a mechanism that no longer exists; its claims must be
    separable from the ravens cohort."""
    old = _seed(10, False, 0.9)
    for r in old:
        r["context"] = {"close_logic": "credit_stop"}
    new = _seed(10, True, 0.9)
    for r in new:
        r["context"] = {"close_logic": "ravens_v1"}
    assert P.grade(old + new, cohort="ravens_v1")["by_type"][P.STRIKE_HOLDS]["hit_rate"] == 100.0
    assert P.grade(old + new, cohort="credit_stop")["by_type"][P.STRIKE_HOLDS]["hit_rate"] == 0.0


def test_empty_ledger_grades_cleanly():
    g = P.grade([])
    assert g["total_claims"] == 0 and g["by_type"] == {}


# ── Wiring ────────────────────────────────────────────────────────────────────────────────────

def test_the_cycle_records_and_resolves():
    """A ledger that only accumulates never teaches anything — both halves must be wired."""
    import inspect

    import auto_paper_cycle as apc
    assert "record_trade_predictions" in inspect.getsource(apc._auto_open_from_candidates)
    assert "_resolve_predictions()" in inspect.getsource(apc.main)
