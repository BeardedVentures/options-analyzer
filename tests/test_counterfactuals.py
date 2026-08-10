"""What happened to the trades VEGA refused to take.

The trade ledger can only say whether the picks were good. Eleven gates decide every entry and
not one has ever been measured against an outcome, because a rejected candidate leaves no
record of what it would have done. The scan snapshots turn out to be that record already.

The load-bearing property is the same one muninn has: a rate computed from too few
observations must report itself as insufficient rather than as a number. A gate deleted on the
strength of a 3-of-4 touch rate would be worse than a gate never measured.
"""
import json
import pathlib
from datetime import datetime, timedelta

import pandas as pd
import pytest

import config
from analysis import counterfactuals as cf


def _prices(lows, start="2026-08-01", close=None):
    idx = pd.to_datetime([datetime.fromisoformat(start) + timedelta(days=i)
                          for i in range(len(lows))])
    closes = close if close is not None else [l + 1.0 for l in lows]
    return pd.DataFrame({"Low": lows, "Close": closes, "High": [l + 2.0 for l in lows]},
                        index=idx)


def _cand(**over):
    c = {"ticker": "TEST", "short_strike": 100.0, "long_strike": 95.0,
         "expiration": "2026-09-18", "dte": 39, "scan_date": "2026-08-01",
         "snapshot": "candidates_2026-08-01_0900.json",
         "gates": {k: True for k in config.REQUIRED_GATES}}
    c.update(over)
    return c


# ── Resolution ────────────────────────────────────────────────────────────────────────────────

def test_touch_is_measured_on_the_low_not_the_close():
    """A spread does not care that price recovered by 4pm — the delta breach that closes it
    fires intraday."""
    px = _prices([105.0, 99.0, 106.0], close=[105.0, 106.0, 106.0])
    assert cf.resolve(_cand(), px, horizon_days=2)["touched"] is True


def test_untouched_when_price_stays_clear():
    assert cf.resolve(_cand(), _prices([105.0, 104.0, 107.0]), horizon_days=2)["touched"] is False


def test_only_days_after_the_scan_count():
    """Price action before the scan is not an outcome of the scan. A low on the scan day itself
    was already visible to the engine that judged the spread."""
    px = _prices([90.0, 105.0, 106.0], start="2026-08-01")
    assert cf.resolve(_cand(scan_date="2026-08-01"), px, horizon_days=2)["touched"] is False


def test_expiry_is_unresolved_until_it_arrives():
    """held_at_expiry is the cleaner measure and the slower one. None means not yet, and must
    never be confused with False."""
    r = cf.resolve(_cand(expiration="2026-12-31"), _prices([105.0, 104.0, 107.0]),
                   horizon_days=2)
    assert r["held_at_expiry"] is None and r["close_at_expiry"] is None
    assert r["touched"] is False           # the horizon measure still answers


def test_expiry_resolves_once_past():
    px = _prices([105.0] * 5, start="2026-08-01")
    r = cf.resolve(_cand(expiration="2026-08-03"), px, horizon_days=2)
    assert r["held_at_expiry"] is True and r["close_at_expiry"] == 106.0


def test_missing_history_resolves_to_unknown_not_to_false():
    r = cf.resolve(_cand(), None)
    assert r["touched"] is None and r["days_observed"] == 0
    assert r["horizon_complete"] is False


# ── Deduplication ─────────────────────────────────────────────────────────────────────────────

def test_the_same_spread_across_scans_counts_once():
    """A 39-DTE spread reappears in every scan until it drifts out of the delta band. Counting
    each sighting would weight a long-lived candidate 20x against a one-day one and make every
    sample size a fiction."""
    seen = [_cand(scan_date="2026-08-01"), _cand(scan_date="2026-08-02"),
            _cand(scan_date="2026-08-03")]
    assert len(cf.first_sightings(seen)) == 1


def test_dedup_keeps_the_FIRST_sighting():
    """The question is what a decision made at that moment would have led to. A later sighting
    has already had part of the outcome happen to it."""
    seen = [_cand(scan_date="2026-08-05"), _cand(scan_date="2026-08-01")]
    assert cf.first_sightings(seen)[0]["scan_date"] == "2026-08-01"


def test_different_strikes_are_different_spreads():
    seen = [_cand(short_strike=100.0), _cand(short_strike=105.0)]
    assert len(cf.first_sightings(seen)) == 2


# ── The single-gate sample ────────────────────────────────────────────────────────────────────

def _rec(sole=None, touched=False, qualified=False):
    gates = {k: True for k in config.REQUIRED_GATES}
    if sole:
        gates[sole] = False
    return cf._record(_cand(gates=gates), {"touched": touched, "min_low_since": 1.0,
                                           "days_observed": 99, "horizon_complete": True,
                                           "held_at_expiry": None, "close_at_expiry": None})


def test_a_candidate_failing_one_gate_is_the_only_clean_read_on_it():
    assert _rec(sole="liquidity")["sole_failed_gate"] == "liquidity"


def test_a_candidate_failing_several_gates_prices_none_of_them():
    gates = {k: True for k in config.REQUIRED_GATES}
    gates["liquidity"] = gates["pop"] = False
    r = cf._record(_cand(gates=gates), {"touched": True, "horizon_complete": True})
    assert r["sole_failed_gate"] is None
    assert set(r["failed_gates"]) == {"liquidity", "pop"}


def test_a_clean_candidate_is_marked_qualified():
    r = cf._record(_cand(), {"touched": False, "horizon_complete": True})
    assert r["qualified"] is True and r["sole_failed_gate"] is None


# ── Value of information ──────────────────────────────────────────────────────────────────────

def test_a_thin_sample_reports_insufficient_rather_than_a_number():
    """The whole value of this module is telling a gate that earns its place from one that does
    not, and a 3-of-4 touch rate cannot do that. Same refusal muninn makes."""
    recs = [_rec(qualified=True) for _ in range(30)]
    recs += [_rec(sole="liquidity", touched=True) for _ in range(3)]
    v = cf.value_of_information(recs)
    assert v["gates"]["liquidity"]["verdict"] == "insufficient"
    assert "lift_pp" not in v["gates"]["liquidity"]


def test_a_gate_whose_rejects_fare_worse_earns_its_place():
    recs = [_rec(touched=False) for _ in range(30)]                       # qualified, 0% touched
    recs += [_rec(sole="pop", touched=True) for _ in range(cf.MIN_GATE_SAMPLE)]
    v = cf.value_of_information(recs)
    assert v["gates"]["pop"]["verdict"] == "earns_its_place"
    assert v["gates"]["pop"]["lift_pp"] == 100.0


def test_a_gate_whose_rejects_fare_no_worse_has_no_measured_value():
    """The finding that matters: a gate rejecting spreads that behave just as well is costing
    opportunity and buying nothing. It belongs in the ranking function, not the contract."""
    recs = [_rec(touched=True) for _ in range(30)]                        # qualified, 100% touched
    recs += [_rec(sole="support_shelter", touched=False) for _ in range(cf.MIN_GATE_SAMPLE)]
    v = cf.value_of_information(recs)
    assert v["gates"]["support_shelter"]["verdict"] == "no_measured_value"
    assert v["gates"]["support_shelter"]["lift_pp"] == -100.0


def test_unresolved_candidates_never_enter_a_rate():
    """touched=None is 'we do not know yet', and averaging it in as a False would make every
    gate look better the less data it had."""
    recs = [_rec(touched=False) for _ in range(10)]
    recs += [cf._record(_cand(), {"touched": None, "horizon_complete": True})
             for _ in range(90)]
    assert cf._touch_rate(recs) == 0.0        # the 90 unknowns are excluded, not counted as wins


def test_every_required_gate_appears_in_the_report():
    v = cf.value_of_information([_rec() for _ in range(5)])
    assert set(v["gates"]) == set(config.REQUIRED_GATES)


def test_the_report_states_its_own_limits():
    """The sample is the top 3 per ticker by credit-to-width, and touch is not loss. A report
    that omits that reads as a verdict on the gates rather than as evidence about them."""
    v = cf.value_of_information([_rec() for _ in range(5)])
    joined = " ".join(v["caveats"]).lower()
    assert "top 3" in joined and "touch is not loss" in joined


# ── Round trip ────────────────────────────────────────────────────────────────────────────────

def test_build_resolves_snapshots_end_to_end(tmp_path):
    snap = tmp_path / "snaps"
    snap.mkdir()
    (snap / "candidates_2026-08-01_0900.json").write_text(json.dumps({
        "meta": {"vix": 15.0},
        "rows": [{"ticker": "TEST", "price": 110.0, "ctx": {"iv_rank": 55},
                  "candidates": [{"ticker": "TEST", "short_strike": 100.0, "long_strike": 95.0,
                                  "expiration": "2026-09-18", "dte": 39,
                                  "gates": {k: True for k in config.REQUIRED_GATES}}]}],
    }), encoding="utf-8")
    ledger = tmp_path / "cf.jsonl"

    n = cf.build(snapshot_dir=snap, ledger=ledger,
                 fetch=lambda tk: _prices([105.0, 99.0] * 8, start="2026-08-02"))
    assert n == 1

    rows = cf.load(ledger)
    assert rows[0]["touched"] is True and rows[0]["qualified"] is True
    assert rows[0]["iv_rank"] == 55           # carried off the enclosing row's ctx


def test_build_is_regenerable_not_append_only(tmp_path):
    """An unexpired candidate's outcome legitimately CHANGES as time passes. Appending would
    accumulate superseded rows; the snapshots are the immutable record, this is a derived view."""
    snap = tmp_path / "snaps"
    snap.mkdir()
    (snap / "candidates_2026-08-01_0900.json").write_text(json.dumps({
        "meta": {}, "rows": [{"ticker": "TEST", "price": 110.0, "candidates": [
            {"ticker": "TEST", "short_strike": 100.0, "long_strike": 95.0,
             "expiration": "2026-09-18", "dte": 39, "gates": {}}]}],
    }), encoding="utf-8")
    ledger = tmp_path / "cf.jsonl"
    px = _prices([105.0, 104.0], start="2026-08-02")

    cf.build(snapshot_dir=snap, ledger=ledger, fetch=lambda tk: px)
    cf.build(snapshot_dir=snap, ledger=ledger, fetch=lambda tk: px)
    assert len(cf.load(ledger)) == 1


def test_missing_snapshot_dir_is_empty_not_an_error():
    assert list(cf.iter_snapshot_candidates(cf.BASE_DIR / "__no_such_dir__")) == []


# ── The fixed horizon (found by the first real run, 2026-08-10) ───────────────────────────────

def test_a_spread_that_has_not_lived_the_horizon_is_unresolved_not_untouched():
    """The failure this module produced on its own first run.

    It resolved 639 real spreads and reported 0% touched on every gate — which reads as "none
    of the eleven gates avoids anything" and actually meant the median spread had been observed
    for TWO days. A 39-DTE spread at 0.20 delta is not expected to be touched in two days;
    nothing had had time to happen. Counting that as untouched manufactures a confident zero
    out of an empty window.
    """
    r = cf.resolve(_cand(), _prices([105.0, 104.0, 103.0]), horizon_days=10)
    assert r["horizon_complete"] is False
    assert r["touched"] is None, "too early must be None, never False"
    assert r["touched_to_date"] is False, "the informational read still answers"


def test_maturing_spreads_are_excluded_from_every_rate():
    mature = [_rec(touched=True) for _ in range(30)]
    young = [cf._record(_cand(), {"touched": None, "touched_to_date": False,
                                  "horizon_complete": False}) for _ in range(500)]
    v = cf.value_of_information(mature + young)
    assert v["n_records"] == 30 and v["n_maturing"] == 500
    assert v["qualified_touch_rate"] == 1.0, "the 500 young spreads must not dilute the rate"


def test_the_horizon_is_identical_for_every_spread():
    """Window length is confounded with scan date: a gate whose blocked candidates came from
    the oldest scan would look worse than one whose came from today purely through exposure.
    A fixed horizon is what makes the per-gate comparison mean anything."""
    old = cf.resolve(_cand(scan_date="2026-08-01"), _prices([105.0] * 30), horizon_days=5)
    new = cf.resolve(_cand(scan_date="2026-08-20"), _prices([105.0] * 30), horizon_days=5)
    assert old["horizon_days"] == new["horizon_days"] == 5


def test_the_report_says_too_early_rather_than_printing_a_zero():
    young = [cf._record(_cand(), {"touched": None, "horizon_complete": False})
             for _ in range(600)]
    text = cf.report(young)
    assert "NOT YET MEASURABLE" in text
    assert "still maturing" in text


def test_a_pruned_snapshot_does_not_delete_its_observation():
    """This ledger outlives its own source.

    output/candidates/ is gitignored under a comment calling it a regenerated artifact
    directory, and it is not — a past scan cannot be re-run, so a pruned snapshot is a
    permanently lost observation. A wholesale rewrite would silently erase the counterfactual
    history the moment anyone cleaned that folder.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        snap, ledger = pathlib.Path(td) / "snaps", pathlib.Path(td) / "cf.jsonl"
        snap.mkdir()
        payload = {"meta": {}, "rows": [{"ticker": "TEST", "price": 110.0, "candidates": [
            {"ticker": "TEST", "short_strike": 100.0, "long_strike": 95.0,
             "expiration": "2026-09-18", "dte": 39, "gates": {}}]}]}
        f = snap / "candidates_2026-08-01_0900.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        px = _prices([105.0] * 20, start="2026-08-02")

        cf.build(snapshot_dir=snap, ledger=ledger, fetch=lambda tk: px)
        assert len(cf.load(ledger)) == 1

        f.unlink()                                   # the snapshot is pruned
        cf.build(snapshot_dir=snap, ledger=ledger, fetch=lambda tk: px)

        rows = cf.load(ledger)
        assert len(rows) == 1, "the observation must survive its snapshot"
        assert rows[0]["source_snapshot_missing"] is True


def test_a_surviving_snapshot_is_re_resolved_not_frozen():
    """The other half: an unexpired spread's outcome changes as time passes, so a row whose
    snapshot is still there must be rewritten, not preserved."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        snap, ledger = pathlib.Path(td) / "snaps", pathlib.Path(td) / "cf.jsonl"
        snap.mkdir()
        (snap / "candidates_2026-08-01_0900.json").write_text(json.dumps(
            {"meta": {}, "rows": [{"ticker": "TEST", "price": 110.0, "candidates": [
                {"ticker": "TEST", "short_strike": 100.0, "long_strike": 95.0,
                 "expiration": "2026-09-18", "dte": 39, "gates": {}}]}]}), encoding="utf-8")

        cf.build(snapshot_dir=snap, ledger=ledger,
                 fetch=lambda tk: _prices([105.0] * 20, start="2026-08-02"))
        assert cf.load(ledger)[0]["touched"] is False

        # Price later trades through the strike; the same spread must now read touched.
        cf.build(snapshot_dir=snap, ledger=ledger,
                 fetch=lambda tk: _prices([105.0] * 5 + [99.0] * 15, start="2026-08-02"))
        rows = cf.load(ledger)
        assert len(rows) == 1 and rows[0]["touched"] is True
        assert not rows[0].get("source_snapshot_missing")
