"""The shadow book — grading the trades the board recommended but the desk never opened.

The defect this exists to answer: between 2026-08-11 and 2026-08-17 the board produced eleven
recommendations and the desk opened none, so six days of the system's own output were recorded
and abandoned. 0 of 158 modeled rows had ever been graded.

The defect these tests exist to catch is narrower and nastier. counterfactuals.resolve measures
a breach as `Low <= short_strike` — correct for a put spread and silently WRONG for a call
spread, where the breach is `High >= short_strike`. A put-side-only grader run over call-side
trades does not error; it reports that every bear call held, forever. So the load-bearing test
here is not "does it grade" but "does a call-side trade grade DIFFERENTLY from a put-side one
on the same price series" — a metric that cannot come out wrong is not a measurement.
"""
import pandas as pd
import pytest

from analysis import shadow_book as sb


def _bars(rows):
    """rows: list of (date, high, low, close)."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {"High": [r[1] for r in rows], "Low": [r[2] for r in rows], "Close": [r[3] for r in rows]},
        index=idx)


# A series that rises through 110 and falls through 90 — so a short strike at either end is
# breached, and one at neither is not. The same bars drive every directional test below.
SWING = _bars([
    ("2026-08-02", 105, 95, 100),
    ("2026-08-03", 112, 99, 108),     # high pierces 110
    ("2026-08-04", 104, 88, 92),      # low pierces 90
    ("2026-08-05", 101, 96, 100),     # expiry bar, settles at 100
])

QUIET = _bars([
    ("2026-08-02", 102, 98, 100),
    ("2026-08-03", 103, 97, 101),
    ("2026-08-04", 102, 98, 99),
    ("2026-08-05", 101, 99, 100),
])


def _rec(**kw):
    base = {
        "status": "modeled", "ticker": "TEST", "strategy": "bull_put_spread",
        "scan_ts": "2026-08-01T09:30:00", "expiration": "2026-08-05",
        "short_strike": 90.0, "long_strike": 85.0, "spread_width": 5.0, "dte": 4,
    }
    base.update(kw)
    return base


# ── Directionality: the whole point ──────────────────────────────────────────

def test_put_side_breach_is_measured_on_the_low():
    out = sb.resolve(_rec(short_strike=90.0), SWING)
    assert out["breached_to_date"] is True
    assert out["breach_side"] == "put"
    assert out["held"] is False


def test_call_side_breach_is_measured_on_the_high_not_the_low():
    """The bug class. A put-side grader reads Low against 110, never breaches, and reports a
    bear call that blew through its strike as a winner."""
    out = sb.resolve(
        _rec(strategy="Bear Call Spread", short_strike=110.0, long_strike=115.0), SWING)
    assert out["breached_to_date"] is True, "high of 112 pierced the 110 call strike"
    assert out["breach_side"] == "call"
    assert out["held"] is False


def test_same_bars_grade_differently_by_side():
    """The metric must be able to come out both ways on ONE price series, or it proves nothing.

    Short the 90 put: breached (low 88). Short the 120 call: held (high never reached 120).
    Identical bars, opposite grades — that is what makes the grade a measurement.
    """
    put = sb.resolve(_rec(short_strike=90.0), SWING)
    call = sb.resolve(
        _rec(strategy="Bear Call Spread", short_strike=120.0, long_strike=125.0), SWING)
    assert put["held"] is False
    assert call["held"] is True
    assert put["held"] != call["held"]


def test_call_side_holds_when_price_stays_below_the_strike():
    out = sb.resolve(
        _rec(strategy="Bear Call Spread", short_strike=110.0, long_strike=115.0), QUIET)
    assert out["held"] is True
    assert out["breach_side"] is None


# ── Condors: four legs, one verdict ──────────────────────────────────────────

def _condor(put_short, call_short):
    return _rec(strategy="Iron Condor", short_strike=None, long_strike=None,
                legs={"put_short_strike": put_short, "put_long_strike": put_short - 5,
                      "call_short_strike": call_short, "call_long_strike": call_short + 5})


def test_condor_holds_only_if_neither_wing_is_breached():
    assert sb.resolve(_condor(80.0, 120.0), SWING)["held"] is True


def test_condor_breached_on_either_wing_is_a_loss_not_a_half_win():
    call_broken = sb.resolve(_condor(80.0, 110.0), SWING)
    put_broken = sb.resolve(_condor(90.0, 120.0), SWING)
    assert call_broken["held"] is False and call_broken["breach_side"] == "call"
    assert put_broken["held"] is False and put_broken["breach_side"] == "put"


def test_condor_breached_on_both_wings_names_both():
    out = sb.resolve(_condor(90.0, 110.0), SWING)
    assert out["breach_side"] == "put+call"
    assert out["held"] is False


def test_condor_missing_a_wing_is_unresolvable_not_graded_on_half():
    out = sb.resolve(
        _rec(strategy="Iron Condor", short_strike=None, long_strike=None,
             legs={"put_short_strike": 90.0}), SWING)
    assert out["unresolvable"] is not None


# ── Refusals: absence is reported, never inferred ────────────────────────────

def test_unexpired_trade_has_no_outcome():
    """held stays None rather than being guessed from the current mark."""
    out = sb.resolve(_rec(expiration="2026-12-19"), QUIET)
    assert out["held"] is None
    assert out["expired"] is False
    assert out["breached_to_date"] is False          # informational, still available


def test_missing_expiration_is_unresolvable():
    """The live defect: 81 of 158 modeled rows had expiration None."""
    assert sb.resolve(_rec(expiration=None), SWING)["unresolvable"] is not None
    assert sb.resolve(_rec(expiration="None"), SWING)["unresolvable"] is not None


def test_missing_short_strike_is_unresolvable():
    assert sb.resolve(_rec(short_strike=None), SWING)["unresolvable"] is not None


def test_bars_after_expiry_cannot_breach_the_trade():
    """A contract that already expired is not breached by what the underlying did later."""
    late = _bars([
        ("2026-08-02", 102, 98, 100),
        ("2026-08-05", 101, 99, 100),     # expiry, settles fine
        ("2026-08-06", 104, 70, 72),      # collapse AFTER expiry
    ])
    out = sb.resolve(_rec(short_strike=90.0), late)
    assert out["held"] is True, "the position was already closed when price collapsed"


# ── Pricing: refuse rather than use a price no fill could achieve ────────────

def test_no_natural_credit_means_no_pl():
    """modeled_credit_per_share is the mid on one path and the natural on the other, so it is
    never a valid basis for P/L — the defect that made the ledger's first 18 trades unusable."""
    rec = _rec(modeled_credit_per_share=0.50)        # mid only, no natural
    grade = sb.grade_pl(rec, sb.resolve(rec, SWING))
    assert grade["priced"] is False
    assert grade["pl_at_expiry"] is None
    assert "natural credit" in grade["unpriced_reason"]


def test_held_trade_keeps_the_full_natural_credit():
    rec = _rec(short_strike=80.0, natural_credit_per_share=0.40)
    grade = sb.grade_pl(rec, sb.resolve(rec, SWING))
    assert grade["priced"] is True
    assert grade["pl_at_expiry"] == pytest.approx(40.0)


def test_breached_trade_is_priced_off_settlement_and_capped_at_width():
    """Short the 110 put on a series settling at 100: intrinsic 10, wider than the 5 spread,
    so the loss is capped at width minus credit — that is what 'defined risk' means."""
    rec = _rec(short_strike=110.0, long_strike=105.0, spread_width=5.0,
               natural_credit_per_share=0.40)
    grade = sb.grade_pl(rec, sb.resolve(rec, SWING))
    assert grade["pl_at_expiry"] == pytest.approx((0.40 - 5.0) * 100)


def test_partial_breach_is_priced_off_intrinsic_not_the_full_width():
    """Settles at 100 against a 103 short put: intrinsic 3, inside the 5-wide spread."""
    rec = _rec(short_strike=103.0, long_strike=98.0, spread_width=5.0,
               natural_credit_per_share=0.40)
    grade = sb.grade_pl(rec, sb.resolve(rec, SWING))
    assert grade["pl_at_expiry"] == pytest.approx((0.40 - 3.0) * 100)


def test_unexpired_trade_is_not_priced():
    rec = _rec(expiration="2026-12-19", natural_credit_per_share=0.40)
    grade = sb.grade_pl(rec, sb.resolve(rec, QUIET))
    assert grade["priced"] is False
    assert grade["unpriced_reason"] == "not expired yet"


def test_stop_bound_is_smaller_than_riding_to_settlement():
    """The stop is a BOUND, not a measurement — but it must at least be the better of the two
    on a full-width loss, or the close logic it models makes no sense."""
    rec = _rec(short_strike=110.0, long_strike=105.0, spread_width=5.0,
               natural_credit_per_share=0.40)
    grade = sb.grade_pl(rec, sb.resolve(rec, SWING))
    assert grade["pl_at_stop"] > grade["pl_at_expiry"]


# ── Cohorts and reporting ────────────────────────────────────────────────────

def test_priced_and_unpriced_grades_never_share_a_cohort():
    priced = _rec(short_strike=80.0, natural_credit_per_share=0.40)
    unpriced = _rec(short_strike=80.0)
    kp = sb.cohort(priced, sb.grade_pl(priced, sb.resolve(priced, SWING)))
    ku = sb.cohort(unpriced, sb.grade_pl(unpriced, sb.resolve(unpriced, SWING)))
    assert kp != ku


def test_strategies_never_share_a_cohort():
    put = _rec()
    call = _rec(strategy="Bear Call Spread")
    empty = {"credit_basis": None}
    assert sb.cohort(put, empty) != sb.cohort(call, empty)


def test_summary_refuses_a_rate_below_min_sample(monkeypatch):
    monkeypatch.setattr(sb, "MIN_SAMPLE", 20)
    rows = [{"cohort": "shadow|bull_put_spread|natural", "held": True, "true_pop": 0.7,
             "priced": False}] * 3
    s = sb.summarize(rows)["shadow|bull_put_spread|natural"]
    assert s["sufficient"] is False
    assert s["expired"] == 3


def test_calibration_gap_compares_claim_against_outcome():
    """The number the whole exercise exists to produce: the board asserted 75%, four of four
    held, so it under-claimed by 25pp."""
    rows = [{"cohort": "c", "held": True, "true_pop": 0.75, "priced": False}] * 4
    s = sb.summarize(rows)["c"]
    assert s["hit_rate"] == pytest.approx(1.0)
    assert s["calibration_gap_pp"] == pytest.approx(25.0)


def test_pending_trades_are_excluded_from_the_hit_rate():
    """An unexpired trade must not count as a miss — it has no outcome yet."""
    rows = [{"cohort": "c", "held": True, "true_pop": 0.7, "priced": False},
            {"cohort": "c", "held": None, "true_pop": 0.7, "priced": False}]
    s = sb.summarize(rows)["c"]
    assert s["expired"] == 1 and s["pending"] == 1
    assert s["hit_rate"] == pytest.approx(1.0)


# ── The seam that caused this ────────────────────────────────────────────────

def test_multi_strategy_emits_the_expiration_key_consumers_read():
    """The live bug: _base wrote `expiration_display` and every consumer read `expiration`."""
    import multi_strategy
    base = multi_strategy._base(
        "TEST", "bear_call", 100.0, {}, "NEUTRAL", 30, "2026-09-18")
    assert base.get("expiration") == "2026-09-18"
    assert base.get("expiration_display") == "2026-09-18", "the cockpit still reads this one"


def test_modeled_row_records_a_positive_width_on_the_call_side():
    """short - long is NEGATIVE for a call spread; all 49 bear-call rows recorded it that way."""
    from analysis import outcome_logger as ol
    import tempfile, pathlib, json as _json
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "out.jsonl"
        orig = ol.OUTCOMES_FILE
        ol.OUTCOMES_FILE = path
        try:
            ol.record_modeled_trades("2026-08-18T09:30:00", "morning", [{
                "ticker": "TEST", "strategy": "Bear Call Spread",
                "short_strike": 110.0, "long_strike": 115.0, "width": 5.0,
                "expiration": "2026-09-18", "dte": 31, "credit_per_share": 0.5,
            }])
            row = _json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        finally:
            ol.OUTCOMES_FILE = orig
    assert row["spread_width"] == 5.0
    assert row["legs"]["short_strike"] == 110.0
    assert "None" not in row["id"], "a None expiration used to land in the trade id"
