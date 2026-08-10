"""Raw entry state on the trade record (P1-3, P1-4).

The ledger already stored the engine's CONCLUSIONS at entry — edge_score, vrp, technical_score,
term_slope, skew_steepness, vix, iv_rank. It stored none of the measurements those conclusions
were drawn from, so a calibration run could ask "was the score right?" and never "was the score
wrong because the inputs were wrong, or because the weighting was?".

pop_gap is the sharpest of the four. true_pop - pop_implied IS the edge every trade asserts —
the probability the engine believes it sees that the market's delta does not — and it was
computed nowhere and stored nowhere. The central claim of the system was the one number the
ledger could never grade.
"""
import math

import pytest

import auto_paper_cycle as apc
import vega_candidates as vc
from analysis import outcome_logger as ol
from analysis.horizon import expected_move
from conftest import make_candidate


# ── pop_gap on the candidate ──────────────────────────────────────────────────────────────────

def test_pop_gap_is_the_difference_the_engine_is_claiming():
    c = {"true_pop": 0.81, "pop_implied": 0.78}
    assert vc.set_pop_gap(c) == 0.03


def test_a_negative_pop_gap_is_recorded_not_discarded():
    """The engine being LESS confident than the market is a real and gradeable claim. Clamping
    it at zero would hide exactly the trades most worth explaining."""
    c = {"true_pop": 0.70, "pop_implied": 0.78}
    assert vc.set_pop_gap(c) == pytest.approx(-0.08)


def test_no_true_pop_means_no_claim_not_a_zero_claim():
    """The fast scan carries no calibrated POP. 0.0 would read as "the engine claimed no edge",
    which is a different statement from "the engine made no claim" — and the calibration engine
    has to be able to separate them."""
    assert vc.set_pop_gap({"pop_implied": 0.78}) is None


def test_set_pop_gap_is_idempotent():
    """It is called from build_candidates AND from attach_true_pop, because true_pop arrives on
    different paths at different times."""
    c = {"true_pop": 0.81, "pop_implied": 0.78}
    assert vc.set_pop_gap(c) == vc.set_pop_gap(c) == 0.03


def test_attach_true_pop_fills_the_gap_that_build_time_could_not():
    """build_candidates runs BEFORE attach_true_pop, so its own set_pop_gap call necessarily
    sees true_pop=None. If the gap were only computed at build time it would be null forever —
    the exact shape of the bug this field exists to fix."""
    import inspect
    assert "set_pop_gap" in inspect.getsource(vc.attach_true_pop)
    assert "set_pop_gap" in inspect.getsource(vc.build_candidates)


# ── entry state extraction ────────────────────────────────────────────────────────────────────

def test_entry_state_reads_everything_the_ctx_already_had():
    c = make_candidate(dte=35, spot=100.0, true_pop=0.81, pop_implied=0.78, pop_gap=0.03)
    s = apc._entry_state(c, {"atm_iv": 0.2800, "rv": 0.2150})
    assert s["atm_iv_at_entry"] == 0.28
    assert s["rv_at_entry"] == 0.215
    assert s["pop_gap_at_entry"] == 0.03
    assert s["expected_move_at_entry"] == pytest.approx(100.0 * 0.28 * math.sqrt(35 / 365), abs=1e-4)


def test_expected_move_uses_the_same_unit_as_the_rest_of_the_board():
    """Calendar days (365), matching analysis.horizon. A 252-trading-day denominator would
    overstate the move by ~20% and make it silently incomparable with the strike distances and
    level cushions the cockpit already shows in sigma."""
    s = apc._entry_state(make_candidate(dte=35, spot=100.0), {"atm_iv": 0.30})
    assert s["expected_move_at_entry"] == pytest.approx(expected_move(100.0, 0.30, 35), abs=1e-4)
    wrong_252 = 100.0 * 0.30 * math.sqrt(35 / 252)
    assert s["expected_move_at_entry"] != pytest.approx(wrong_252, abs=1e-3)


def test_entry_state_falls_back_to_the_leg_iv_when_ctx_has_none():
    s = apc._entry_state(make_candidate(short_iv=0.31, spot=100.0), {})
    assert s["atm_iv_at_entry"] == 0.31


@pytest.mark.parametrize("ctx", [{}, {"rv": 0.21}, {"atm_iv": 0.28}, {"atm_iv": None, "rv": None}])
def test_every_field_degrades_independently(ctx):
    """A missing IV must not cost the trade its pop_gap. Partial data is the normal case."""
    c = make_candidate(true_pop=0.81, pop_implied=0.78)
    s = apc._entry_state(c, ctx)
    assert set(s) == {"atm_iv_at_entry", "rv_at_entry", "expected_move_at_entry",
                      "pop_gap_at_entry", "btc_iv_gap_pp", "btc_vrp_pp"}
    assert s["pop_gap_at_entry"] == 0.03          # computable regardless of the vol context


def test_entry_state_survives_junk_without_raising():
    s = apc._entry_state({"dte": "not-a-number", "spot": "x"}, {"atm_iv": "n/a", "rv": None})
    assert all(v is None for v in s.values())


def test_a_fast_scan_candidate_records_no_edge_claim():
    s = apc._entry_state(make_candidate(true_pop=None, pop_gap=None, spot=100.0), {})
    assert s["pop_gap_at_entry"] is None


# ── the ledger record ─────────────────────────────────────────────────────────────────────────

def test_the_four_fields_reach_the_ledger(temp_ledger, read_ledger):
    tid = ol.open_paper_trade(
        ticker="TEST", short_strike=100, long_strike=95, expiration="2026-09-18",
        entry_credit_per_share=0.90, dte=35,
        atm_iv_at_entry=0.28, rv_at_entry=0.215,
        expected_move_at_entry=8.67, pop_gap_at_entry=0.03,
    )
    r = next(x for x in read_ledger() if x["id"] == tid)
    assert (r["atm_iv_at_entry"], r["rv_at_entry"]) == (0.28, 0.215)
    assert (r["expected_move_at_entry"], r["pop_gap_at_entry"]) == (8.67, 0.03)


def test_the_fields_are_present_and_null_when_unknown(temp_ledger, read_ledger):
    """Present-and-null is a different fact from absent. A calibration run must be able to tell
    "this trade recorded no IV" from "this trade predates the field"."""
    tid = ol.open_paper_trade(
        ticker="TEST", short_strike=100, long_strike=95, expiration="2026-09-18",
        entry_credit_per_share=0.90, dte=35)
    r = next(x for x in read_ledger() if x["id"] == tid)
    for k in ("atm_iv_at_entry", "rv_at_entry", "expected_move_at_entry", "pop_gap_at_entry"):
        assert k in r and r[k] is None


def test_the_open_path_actually_threads_them_through():
    """The fields existing on the signature proves nothing if auto_paper_cycle never passes
    them — which is precisely how vrp and edge_score sat unlogged through 58 trades."""
    import inspect
    src = inspect.getsource(apc)
    for k in ("atm_iv_at_entry", "rv_at_entry", "expected_move_at_entry", "pop_gap_at_entry"):
        assert f"{k}=_entry[" in src, f"{k} is not threaded into open_paper_trade"
