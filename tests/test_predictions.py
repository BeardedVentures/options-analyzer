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


@pytest.mark.parametrize("bad", ["2026-13-18", "2026-02-30", "not-a-date", "18-12-2026", ""])
def test_a_claim_that_can_never_come_due_is_refused_at_write_time(bad):
    """`2026-13-18` lived in the live ledger from 2026-08-20 because nothing checked here.

    It was written without complaint, reached the resolver, and could only be marked
    unresolvable — a permanently dead row that three audits chased as a quote-feed glitch.
    A resolution date that is not a date can never come due, so the claim can never be graded.
    """
    assert P.record("T1", "AAA", P.STRIKE_HOLDS, "x", 0.8, bad) is None
    assert P.load() == []


def test_a_valid_horizon_is_still_accepted():
    """The guard above must reject bad dates WITHOUT rejecting good ones."""
    assert P.record("T1", "AAA", P.STRIKE_HOLDS, "x", 0.8, "2026-12-18") is not None
    assert len(P.load()) == 1


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
    """n resolved claims, each on a DISTINCT ticker and a distinct day.

    The distinctness is load-bearing, not decoration. `grade()` judges gradeability on the
    CLUSTERED count, and twenty claims that share one ticker and one date are one observation
    however many rows they occupy -- so a fixture that stamped them all "AAA" with no date
    would be asking these tests to assert calibration language about a sample of one. Spreading
    them is what an honest twenty-observation sample looks like; the probabilities and outcomes
    each test cares about are untouched.
    """
    rows = []
    for i in range(n):
        rows.append({"id": f"x{i}", "trade_id": f"t{i}", "ticker": f"AA{i:03d}",
                     "claim_type": ctype, "claim": "c", "probability": prob,
                     "made_at": f"2026-0{1 + i // 28}-{1 + i % 28:02d}T12:00:00",
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
    assert "record_trade_predictions" in inspect.getsource(apc._auto_open_from_board)
    assert "_resolve_predictions()" in inspect.getsource(apc.main)


# ── Brier decomposition (2026-08-10) ──────────────────────────────────────────────────────────

def _pairs(spec):
    """spec: list of (probability, outcome) — outcome True = the claim was correct."""
    return [(p, o) for p, o in spec]


def test_the_decomposition_identity_holds():
    """BS = reliability - resolution + uncertainty.

    This is the test that makes the metric able to be WRONG. Three numbers that merely look
    plausible would pass any assertion about their ranges; only the identity proves they were
    computed from the same data by the formula they claim to be.
    """
    import random as _r
    rng = _r.Random(7)
    for trial in range(25):
        pairs = [(round(rng.uniform(0.05, 0.95), 3), rng.random() < 0.6) for _ in range(60)]
        d = P.decompose(pairs, bootstrap=0)
        lhs = d["brier"]
        rhs = d["reliability"] - d["resolution"] + d["uncertainty"] + d["residual"]
        assert lhs == pytest.approx(rhs, abs=1e-9), f"trial {trial}: {lhs} != {rhs}"


def test_a_forecaster_who_says_the_base_rate_about_everything_has_zero_resolution():
    """The failure raw Brier cannot see.

    Every claim gets the same 70%, and 70% of them come true. Perfectly calibrated, respectable
    Brier, and it knows NOTHING about which individual trade will work. This is the case the
    verdict has to catch, because 'well calibrated' is exactly what raw Brier calls it.
    """
    pairs = [(0.70, True)] * 70 + [(0.70, False)] * 30
    d = P.decompose(pairs, bootstrap=0)
    assert d["resolution"] == pytest.approx(0.0, abs=1e-9)
    assert d["reliability"] == pytest.approx(0.0, abs=1e-9)
    assert d["skill"] == pytest.approx(0.0, abs=1e-9)
    assert d["brier"] == pytest.approx(d["uncertainty"], abs=1e-9)


def test_a_forecaster_who_separates_winners_from_losers_has_resolution():
    pairs = [(0.95, True)] * 50 + [(0.05, False)] * 50
    d = P.decompose(pairs, bootstrap=0)
    assert d["resolution"] >= 0.19
    assert d["skill"] > 0.7
    assert d["brier"] < 0.01


def test_confident_and_wrong_is_punished_through_reliability_not_resolution():
    """Backwards forecasts still DISCRIMINATE — they are just pointed the wrong way. The
    decomposition should show high resolution and terrible reliability, which is a different
    problem from having no signal, and needs a different fix."""
    pairs = [(0.95, False)] * 50 + [(0.05, True)] * 50
    d = P.decompose(pairs, bootstrap=0)
    assert d["resolution"] >= 0.19         # it separates the groups
    assert d["reliability"] > 0.7         # ...and is wrong about which is which
    assert d["skill"] < 0


def test_uncertainty_is_fixed_by_the_base_rate_alone():
    """It is the same for every model on the same data — nothing a forecaster does moves it."""
    a = P.decompose([(0.9, True)] * 60 + [(0.1, False)] * 40, bootstrap=0)
    b = P.decompose([(0.5, True)] * 60 + [(0.5, False)] * 40, bootstrap=0)
    assert a["uncertainty"] == pytest.approx(b["uncertainty"], abs=1e-9)
    assert a["uncertainty"] == pytest.approx(0.6 * 0.4, abs=1e-9)


def test_clustered_forecasts_are_reported_as_clustered():
    """Resolution is capped by how much the forecasts actually vary. VEGA's claims sit between
    0.70 and 0.85, so the ceiling may be the binding constraint rather than the model — and the
    report has to say which."""
    tight = P.decompose([(0.72, True), (0.73, True), (0.74, False), (0.71, True)] * 10,
                           bootstrap=0)
    wide = P.decompose([(0.10, False), (0.40, True), (0.70, True), (0.95, True)] * 10,
                          bootstrap=0)
    assert tight["forecast_spread"] < 0.05
    assert wide["forecast_spread"] > 0.25


def test_equal_count_bins_survive_clustered_forecasts():
    """Equal-WIDTH bins would drop every VEGA claim into one cell, and a single bin makes
    resolution identically zero by construction — reporting 'no discrimination' when the bins
    simply could not see any."""
    pairs = [(0.71, False)] * 25 + [(0.84, True)] * 25
    d = P.decompose(pairs, bootstrap=0)
    assert d["n_bins"] >= 2
    assert d["resolution"] >= 0.19, "the two groups differ completely and must be separable"


def test_the_bootstrap_interval_is_reported_and_deterministic():
    """A calibration report that moves when you re-run it is not a report."""
    pairs = [(0.8, True)] * 30 + [(0.3, False)] * 30
    a = P.decompose(pairs, bootstrap=400)
    b = P.decompose(pairs, bootstrap=400)
    assert a["resolution_ci"] == b["resolution_ci"] is not None
    assert a["resolution_ci"][0] <= a["resolution"] <= a["resolution_ci"][1]


def test_noise_is_not_mistaken_for_skill():
    """Resolution is a sum of squares, so a forecaster that knows NOTHING still scores above
    zero — and its bootstrap interval can sit entirely above zero too. Measured here: 40 random
    forecasts gave resolution 0.019 with a CI of [0.004, 0.055], which "excludes zero" for a
    model with no signal at all.

    The permutation test is what actually answers it: shuffling outcomes against forecasts
    breaks any real association while keeping both margins, so the shuffled scores are the
    distribution of resolution under "knows nothing".

    Asserted as a FALSE-POSITIVE RATE over many independent noise draws, not as p > 0.05 on
    one lucky seed. A single draw can legitimately land in the tail — that is what a 5% test
    means — so a test that pinned one seed would be asserting luck and would break the first
    time anything downstream of the RNG changed.
    """
    import random as _r
    rng = _r.Random(11)
    flagged = 0
    trials = 40
    for _ in range(trials):
        pairs = [(round(rng.uniform(0.6, 0.9), 3), rng.random() < 0.75) for _ in range(40)]
        d = P.decompose(pairs, bootstrap=400)
        assert d["resolution"] > 0, "noise is never exactly zero — that is the whole trap"
        if d["resolution_p"] is not None and d["resolution_p"] < 0.05:
            flagged += 1
    # A correctly calibrated 5% test flags ~2 of 40. Anything near half would mean the null
    # distribution is wrong and noise is being sold as skill.
    assert flagged <= trials * 0.20, (
        f"{flagged}/{trials} pure-noise samples read as discrimination — the permutation null "
        f"is miscalibrated")


def test_real_signal_clears_the_permutation_test():
    """The other side: a forecaster that genuinely separates outcomes must be detected."""
    d = P.decompose([(0.9, True)] * 25 + [(0.2, False)] * 25, bootstrap=1000)
    assert d["resolution_p"] < 0.01


def test_the_residual_is_reported_so_the_identity_is_checkable():
    """Three terms that silently do not add up to the Brier they claim to decompose would be
    unfalsifiable. Binning continuous forecasts leaves a remainder; it is exposed, not hidden."""
    d = P.decompose([(0.71, True), (0.79, False), (0.83, True), (0.62, True)] * 10, bootstrap=0)
    assert d["residual"] is not None
    assert d["brier"] == pytest.approx(
        d["reliability"] - d["resolution"] + d["uncertainty"] + d["residual"], abs=1e-4)


def test_empty_input_reports_nothing_rather_than_zero():
    d = P.decompose([], bootstrap=0)
    assert d["n"] == 0 and d["brier"] is None and d["resolution"] is None


def test_grade_exposes_the_decomposition_per_claim_type():
    rows = [{"claim_type": "strike_holds", "status": "resolved", "correct": i < 7,
             "probability": 0.9 if i < 7 else 0.2, "context": {}} for i in range(10)]
    g = P.grade(rows)
    d = g["by_type"]["strike_holds"]
    assert d["resolution"] is not None and d["reliability"] is not None
    assert d["skill"] is not None and d["resolution_ci"] is not None


def test_the_verdict_says_when_a_type_does_not_discriminate():
    """'78% correct, well calibrated' is what a base-rate parrot scores too. The verdict has to
    name the difference or the ledger launders ignorance as calibration."""
    # Distinct ticker and day per row: gradeability is judged on the CLUSTERED count, and ten
    # rows sharing one name on one date are one observation, not ten.
    rows = [{"claim_type": "strike_holds", "status": "resolved", "correct": i < 7,
             "ticker": f"AA{i:03d}", "made_at": f"2026-01-{1 + i:02d}T12:00:00",
             "probability": 0.7, "context": {}} for i in range(10)]
    v = P.grade(rows)["by_type"]["strike_holds"]["verdict"]
    assert "does NOT discriminate" in v
    assert "hardly vary" in v          # and names the clustered-forecast reason


def test_the_verdict_credits_a_type_that_does_discriminate():
    rows = [{"claim_type": "strike_holds", "status": "resolved", "correct": i < 15,
             "ticker": f"AA{i:03d}", "made_at": f"2026-01-{1 + i:02d}T12:00:00",
             "probability": 0.95 if i < 15 else 0.05, "context": {}} for i in range(20)]
    v = P.grade(rows)["by_type"]["strike_holds"]["verdict"]
    assert "DISCRIMINATES" in v


# -- The daily sweep -----------------------------------------------------------------------------
#
# record() re-read and re-wrote the ENTIRE ledger per claim. Invisible at 43 rows; not invisible
# from 2026-08-25, when fixing the close scan let the daily direction sweep complete for the first
# time and write 449 claims in one run (56 tickers x 4 horizons x forecast+baseline). Measured:
# 10.5 s per sweep at 491 rows, 396 s at 50,000 rows. The cycle's Task Scheduler
# ExecutionTimeLimit is 30 minutes and a cycle already runs 11-16, so the quadratic term would
# have started killing the close scan again within months -- the same daily failure that had just
# been fixed, arriving from a different direction and looking nothing like a regression.

def test_batch_writes_once_regardless_of_claim_count(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    writes = {"n": 0}
    real = P._write

    def counting(rows):
        writes["n"] += 1
        return real(rows)

    monkeypatch.setattr(P, "_write", counting)
    with P.batch():
        for i in range(50):
            P.record("t%d" % i, "SPY", "direction_1d", "c", 0.5, "2026-09-01")
    assert writes["n"] == 1, "the sweep wrote %d times, not once" % writes["n"]
    assert len(P.load()) == 50


def test_batch_still_refuses_duplicate_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    with P.batch():
        for _ in range(5):
            P.record("t1", "SPY", "direction_1d", "c", 0.5, "2026-09-01")
    assert len(P.load()) == 1


def test_batch_does_not_drop_rows_written_by_someone_else_meanwhile(tmp_path, monkeypatch):
    """The flush re-reads under the lock, so a concurrent writer is merged, not clobbered.

    The sweep fetches a year of prices per ticker between claims, so the batch stays open for
    minutes. Holding the lock across it would block every other writer; keeping the in-memory
    copy and overwriting at the end would silently delete whatever landed in between.
    """
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    P.record("early", "SPY", "direction_1d", "c", 0.5, "2026-09-01")
    with P.batch():
        P.record("mine", "SPY", "direction_1d", "c", 0.5, "2026-09-01")
        rows = P._read()                      # another process appends mid-batch
        rows.append({"id": "interloper::direction_1d", "trade_id": "interloper",
                     "ticker": "QQQ", "claim_type": "direction_1d", "status": "open"})
        P._write(rows)
    assert {r["id"] for r in P.load()} == {
        "early::direction_1d", "mine::direction_1d", "interloper::direction_1d"}


def test_record_outside_a_batch_still_writes_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    P.record("t1", "SPY", "direction_1d", "c", 0.5, "2026-09-01")
    assert len(P.load()) == 1


def test_a_sweep_that_dies_partway_keeps_the_claims_it_made(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    with pytest.raises(ValueError):
        with P.batch():
            P.record("t1", "SPY", "direction_1d", "c", 0.5, "2026-09-01")
            raise ValueError("ticker fetch blew up mid-sweep")
    assert len(P.load()) == 1


def test_batch_state_is_cleared_after_use(tmp_path, monkeypatch):
    """A leaked buffer would make every later record() a silent no-op."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    with P.batch():
        pass
    assert P._BUFFER is None and P._BUFFER_IDS is None


def test_a_bad_resolution_date_is_still_refused_inside_a_batch(tmp_path, monkeypatch):
    """The 2026-13-18 guard must not be bypassed by the fast path."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    with P.batch():
        assert P.record("t1", "C", "direction_1d", "c", 0.5, "2026-13-18") is None
    assert P.load() == []


# -- Clustered sample size ------------------------------------------------------------------------
#
# A raw resolved count is not a sample size when the rows overlap in time and correlate across
# names. Reporting one is the defect these tests exist to prevent.

def test_many_claims_on_one_ticker_day_are_one_observation():
    rows = [{"claim_type": "c", "ticker": "SPY", "made_at": "2026-09-01T12:00:00",
             "status": "resolved", "correct": True, "probability": 0.8,
             "context": {"horizon_days": 1}} for _ in range(50)]
    c = P.cluster_sample(rows)
    assert c["n_raw"] == 50
    assert c["n_ticker_days"] == 1
    assert c["n_effective"] == 1


def test_overlapping_horizons_do_not_count_as_independent_draws():
    """A 21-day claim written every session shares 20 of its 21 days with yesterday's.

    Ninety of them are closer to four draws than ninety. This is the same error found in the
    HAR work, where 2,045 daily-stepped 30-day windows were an effective ~68 blocks.
    """
    rows = [{"claim_type": "c", "ticker": "SPY", "made_at": f"2026-09-{d:02d}T12:00:00",
             "status": "resolved", "correct": True, "probability": 0.8,
             "context": {"horizon_days": 21}} for d in range(1, 22)]
    c = P.cluster_sample(rows)
    assert c["n_raw"] == 21
    assert c["n_ticker_days"] == 21          # 21 distinct ticker-days...
    assert c["n_time_blocks"] == 1           # ...but one non-overlapping 21-day window
    assert c["n_effective"] == 1


def test_the_longest_horizon_present_sets_the_overlap_not_the_average():
    """Averaging would credit the sample with independence the long claims destroy."""
    rows = [{"claim_type": "c", "ticker": "SPY", "made_at": f"2026-09-{d:02d}T12:00:00",
             "status": "resolved", "correct": True, "probability": 0.8,
             "context": {"horizon_days": 1 if d % 2 else 21}} for d in range(1, 22)]
    assert P.cluster_sample(rows)["horizon_days"] == 21


def test_correlated_names_collapse_into_one_cross_sectional_cluster():
    """SPY, QQQ and IWM are one bet on one market. When it gaps they miss together."""
    rows = [{"claim_type": "c", "ticker": tk, "made_at": "2026-09-01T12:00:00",
             "status": "resolved", "correct": True, "probability": 0.8,
             "context": {"horizon_days": 1, "sector_proxy": "SPY"}}
            for tk in ("SPY", "QQQ", "IWM")]
    c = P.cluster_sample(rows)
    assert c["n_raw"] == 3
    assert c["n_clusters"] == 1
    assert c["n_effective"] == 1


def test_distinct_clusters_on_distinct_days_do_accumulate():
    rows = []
    for d in range(1, 11):
        for proxy in ("SPY", "XLE", "XLV"):
            rows.append({"claim_type": "c", "ticker": f"{proxy}{d}",
                         "made_at": f"2026-09-{d:02d}T12:00:00", "status": "resolved",
                         "correct": True, "probability": 0.8,
                         "context": {"horizon_days": 1, "sector_proxy": proxy}})
    c = P.cluster_sample(rows)
    assert c["n_time_blocks"] == 10 and c["n_clusters"] == 3
    assert c["n_effective"] == 30


def test_effective_count_never_exceeds_the_raw_count():
    rows = [{"claim_type": "c", "ticker": f"T{i}", "made_at": f"2026-09-{1 + i:02d}T12:00:00",
             "status": "resolved", "correct": True, "probability": 0.8,
             "context": {"horizon_days": 1, "sector_proxy": f"P{i}"}} for i in range(5)]
    c = P.cluster_sample(rows)
    assert c["n_effective"] <= c["n_raw"] == 5


def test_grade_reports_both_counts_side_by_side():
    """A headline quoting only the raw number is the defect. Both, always."""
    rows = [{"claim_type": "strike_holds", "ticker": "SPY",
             "made_at": "2026-09-01T12:00:00", "status": "resolved",
             "correct": i < 14, "probability": 0.8,
             "context": {"horizon_days": 21}} for i in range(20)]
    d = P.grade(rows)["by_type"]["strike_holds"]
    assert d["n"] == 20
    assert d["n_effective"] == 1
    assert d["gradeable"] is False               # 20 raw rows, one real observation
    assert "INDEPENDENT" in d["verdict"]
    assert "20 resolved" in d["verdict"]


def test_empty_input_does_not_divide_by_zero():
    c = P.cluster_sample([])
    assert c["n_effective"] == 0 and c["n_raw"] == 0
