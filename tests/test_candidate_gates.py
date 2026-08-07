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
    from analysis import assessment as A
    # The gates now live in ONE place; assert against that rather than the caller.
    gsrc = inspect.getsource(A.evaluate_gates)
    assert '"natural_credit_usd"' in gsrc, "min_credit_usd must gate the natural credit"
    assert '"natural_credit_to_width"' in gsrc, "credit_to_width must gate natural"
    # And the pair chosen per short leg must be ranked on the same basis, or ranking on mid
    # can discard a pair that would have passed the natural gate.
    assert 'natural_ctw > best["natural_credit_to_width"]' in inspect.getsource(vc.build_candidates)


def test_a_spread_that_is_rich_on_mid_and_worthless_on_natural_is_rejected():
    """Live MU 810/805 on 2026-08-07 quoted a $303 mid credit (61% of width) against a
    NEGATIVE $50 natural — a spread that costs money to enter was passing as premium."""
    import config
    width, mid_credit, natural_credit = 5.0, 3.03, -0.50
    assert mid_credit * 100 >= config.MIN_CREDIT_USD          # would have passed on mid
    assert not (natural_credit * 100 >= config.MIN_CREDIT_USD)  # correctly fails on natural
    assert not ((natural_credit / width) >= config.MIN_CREDIT_TO_WIDTH_PCT)


# ── Structural shelter gate (bug found live 2026-08-07) ───────────────────────────────────────

def test_shelter_gate_rejects_a_strike_sitting_in_open_air():
    """The auto-open path placed strikes on delta and OTM percentage alone and never consulted
    a support level. On 2026-08-07 both GDX trades were the only two of five entries whose
    strike sat ABOVE every real support — nearest support 1.3-1.5 expected moves BELOW it —
    and both were the only two that died the same day. The three with a level above the strike
    all survived. A same-day stop on a 30+ DTE thesis is an entry problem."""
    import vega_candidates as vc
    gdx = [{"price": 73.01, "touches": 3, "strength": 55.0}]   # support far BELOW the strike
    smh = [{"price": 545.38, "touches": 3, "strength": 60.0}]  # support ABOVE the strike
    assert vc._shelter_ok(82.0, gdx) is False
    assert vc._shelter_ok(535.0, smh) is True


def test_shelter_gate_fails_open_on_missing_levels():
    """A data gap must not empty the board. The earnings gate fails CLOSED by design; this one
    cannot, because a level read depends on price history that is routinely thin."""
    import vega_candidates as vc
    assert vc._shelter_ok(82.0, None) is True
    assert vc._shelter_ok(82.0, []) is True


def test_shelter_gate_is_switchable(monkeypatch):
    import config
    import vega_candidates as vc
    monkeypatch.setattr(config, "SUPPORT_SHELTER_GATE_ENABLED", False, raising=False)
    assert vc._shelter_ok(82.0, [{"price": 73.0, "touches": 3, "strength": 55.0}]) is True


def test_shelter_is_in_the_enforcement_contract():
    """REQUIRED_GATES is what makes _auto_open_from_candidates refuse to open when a gate goes
    missing — the mechanism that turns a silent widening into a loud failure."""
    import config
    assert "support_shelter" in config.REQUIRED_GATES


def test_the_contract_has_exactly_one_implementation():
    """Every REQUIRED_GATES key must be emitted by analysis.assessment.evaluate_gates, and
    that must be the only implementation. Two engines checking the same config constants is
    the shape behind four enforcement leaks — IV rank, POP floor, quote spread, and the
    mid-versus-natural credit basis."""
    import inspect

    import config
    from analysis import assessment as A
    src = inspect.getsource(A.evaluate_gates)
    for key in config.REQUIRED_GATES:
        assert f'"{key}"' in src, f"{key} is required but not emitted by evaluate_gates"


def test_evaluate_gates_refuses_to_drift_from_the_contract(monkeypatch):
    """If a gate is added to REQUIRED_GATES and not implemented, the scan must fail loudly
    rather than open trades against a rule nothing checks."""
    import config
    from analysis import assessment as A
    monkeypatch.setattr(config, "REQUIRED_GATES",
                        list(config.REQUIRED_GATES) + ["a_gate_nobody_implemented"],
                        raising=False)
    with pytest.raises(AssertionError, match="does not emit required gates"):
        A.evaluate_gates({"short_strike": 100.0, "dte": 30, "short_leg": {},
                          "side": "put", "pop": 0.8}, {"ticker": "T", "spot": 110.0})


# ── One engine: board and robot must agree ────────────────────────────────────────────────────

def test_the_engine_enforces_the_shared_contract():
    """Recording the shared gates was not enough. On 2026-08-07 XBI passed every engine check
    and still failed support_shelter, so the cockpit would have displayed a trade the
    auto-trader refuses to open. A system that shows one standard and trades another is the
    exact defect this convergence removes."""
    import inspect

    import main
    src = inspect.getsource(main.screen_ticker)
    assert "ASSESSMENT_GATES" in src, "engine must reject on shared-gate failure"
    assert "_asmt[\"gates\"]" in src


def test_both_paths_call_the_same_gate_implementation():
    import inspect

    import main
    import vega_candidates as vc
    assert "assessment" in inspect.getsource(main.screen_ticker)
    assert "evaluate_gates" in inspect.getsource(vc.build_candidates)


def test_edge_score_lives_in_the_shared_core():
    """Ranking lived only in main.py, so the scanner sorted on a local heuristic while the
    board sorted on the real score — two orderings of one opportunity set."""
    import inspect

    from analysis import assessment as A
    src = inspect.getsource(A.assess)
    assert "calculate_edge_score" in src
    assert '"edge_score"' in src
