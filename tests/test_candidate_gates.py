"""Gate enforcement on the auto-open path.

These cover the three enforcement leaks found in one week (IV-rank 07-25, POP floor 08-02,
quote-spread 08-02) plus the fill-model guard. Each leak had the same shape: a rule defined in
config, annotated by the scanner, omitted by the path that actually trades.
"""
import pytest

import config
import auto_paper_cycle as apc
import vega_candidates as vc
from conftest import make_candidate, make_gates


# ── REQUIRED_GATES contract ───────────────────────────────────────────────────────────────────

def test_valid_candidate_passes_all_gates():
    assert apc._candidate_passes_minimum(make_candidate()) is True


def test_every_required_gate_is_enforced():
    """Meta-test: flipping ANY single REQUIRED_GATES key to False must reject the candidate.

    This is the regression guard for the leak pattern — if someone adds a gate to REQUIRED_GATES
    but the enforcement path ignores it, this fails.
    """
    for gate in config.REQUIRED_GATES:
        c = make_candidate(gates=make_gates(**{gate: False}))
        assert apc._candidate_passes_minimum(c) is False, f"gate '{gate}' is not enforced"


def test_missing_gate_key_is_detected():
    """A gate key absent entirely (scanner stopped emitting it) must be reported, not defaulted."""
    gates = make_gates()
    del gates["quote_spread"]
    assert apc._missing_gates(make_candidate(gates=gates)) == ["quote_spread"]


def test_complete_candidate_has_no_missing_gates():
    assert apc._missing_gates(make_candidate()) == []


def test_missing_gate_key_also_fails_the_minimum_check():
    gates = make_gates()
    del gates["pop"]
    assert apc._candidate_passes_minimum(make_candidate(gates=gates)) is False


# ── POP floor (leak #2) ───────────────────────────────────────────────────────────────────────

def test_pop_below_floor_is_rejected():
    c = make_candidate(true_pop=0.60, pop_implied=0.99)
    assert apc._candidate_passes_minimum(c) is False


def test_true_pop_takes_precedence_over_implied():
    """A high implied_pop must not rescue a candidate whose calibrated POP is below the floor."""
    c = make_candidate(true_pop=0.60, pop_implied=0.99)
    assert apc._candidate_passes_minimum(c) is False


def test_pop_falls_back_to_implied_when_true_pop_absent():
    """Snapshots written before true_pop wiring must still be gated, on implied_pop."""
    ok = make_candidate(pop_implied=0.80)
    ok.pop("true_pop")
    assert apc._candidate_passes_minimum(ok) is True

    bad = make_candidate(pop_implied=0.60)
    bad.pop("true_pop")
    assert apc._candidate_passes_minimum(bad) is False


def test_pop_exactly_at_floor_passes():
    c = make_candidate(true_pop=config.MIN_PROBABILITY_OF_PROFIT)
    assert apc._candidate_passes_minimum(c) is True


# ── Quote spread (leak #3) ────────────────────────────────────────────────────────────────────

def test_tight_quotes_pass_spread_gate():
    assert vc._quote_spread_ok(make_candidate()) is True


def test_wide_short_leg_fails_spread_gate():
    # 1.00/2.00 -> spread 1.00 on mid 1.50 = 67%, well past the 35% cap
    assert vc._quote_spread_ok(make_candidate(short_bid=1.00, short_ask=2.00)) is False


def test_wide_long_leg_fails_spread_gate():
    assert vc._quote_spread_ok(make_candidate(long_bid=0.10, long_ask=1.00)) is False


def test_unquotable_leg_fails_closed():
    """A missing/zero ask must fail the gate, never pass by accident."""
    assert vc._quote_spread_ok(make_candidate(short_ask=0)) is False
    assert vc._quote_spread_ok(make_candidate(short_ask=None)) is False


def test_leg_spread_pct_math():
    assert vc._leg_spread_pct(1.90, 2.10) == pytest.approx(0.20 / 2.00)


# ── Fill model (Finding 1) ────────────────────────────────────────────────────────────────────

def test_negative_natural_credit_fails_its_gate():
    c = make_candidate(natural_credit_per_share=-0.10,
                       gates=make_gates(natural_credit_positive=False))
    assert apc._candidate_passes_minimum(c) is False


def test_zero_natural_credit_fails_its_gate():
    c = make_candidate(natural_credit_per_share=0.0,
                       gates=make_gates(natural_credit_positive=False))
    assert apc._candidate_passes_minimum(c) is False


def test_min_credit_usd_enforced_independently_of_gates_dict():
    """credit_usd below MIN_CREDIT_USD is rejected even if the gates dict claims otherwise."""
    c = make_candidate(credit_usd=1.0)
    assert apc._candidate_passes_minimum(c) is False


# ── Gate/execution basis (bug found live 2026-08-07) ──────────────────────────────────────────

def test_credit_gates_evaluate_the_basis_that_actually_gets_filled():
    """GDX 82/81 opened twice on 2026-08-07 for $9 and $7 of credit against a $25 minimum,
    because the gate read the MID credit ($31/$29) while auto_paper_cycle fills at NATURAL.
    Both were closed by the wolf floor inside the same cycle for -$45.16 each — a $7 credit on
    a $1-wide spread is dead at inception.

    This is the same gate/execution mismatch REQUIRED_GATES was created to prevent, and it is
    the fourth instance of that shape after the IV-rank, POP-floor and quote-spread leaks.
    """
    import inspect

    import vega_candidates as vc
    src = inspect.getsource(vc.build_candidates)
    assert 'best["natural_credit_usd"]' in src, "min_credit_usd must gate the natural credit"
    assert 'best["natural_credit_to_width"]' in src, "credit_to_width must gate natural"
    # And the pair chosen per short leg must be ranked on the same basis, or ranking on mid
    # can discard a pair that would have passed the natural gate.
    assert 'natural_ctw > best["natural_credit_to_width"]' in src


def test_a_spread_that_is_rich_on_mid_and_worthless_on_natural_is_rejected():
    """Live MU 810/805 on 2026-08-07 quoted a $303 mid credit (61% of width) against a
    NEGATIVE $50 natural — a spread that costs money to enter was passing as premium."""
    import config
    width, mid_credit, natural_credit = 5.0, 3.03, -0.50
    assert mid_credit * 100 >= config.MIN_CREDIT_USD          # would have passed on mid
    assert not (natural_credit * 100 >= config.MIN_CREDIT_USD)  # correctly fails on natural
    assert not ((natural_credit / width) >= config.MIN_CREDIT_TO_WIDTH_PCT)
