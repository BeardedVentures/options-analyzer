"""The credit floor scales with the underlying's price (step 0, 2026-08-09).

A flat dollar floor is not price-neutral. $25 against SPY at $773 is 0.03% of spot; against
IBIT at $36.80 it is 0.68% — the same rule, twenty times stricter, purely because the share
price is lower. That is an artifact, not a risk judgement, and it kept IBIT out of the book
entirely: on 2026-08-09 all 17 IBIT candidates were blocked by min_credit_usd, and the best
spread on the board carried $23 against the $25 floor while PASSING credit_to_width at 0.230.

Four places enforce this floor. Every prior enforcement leak in this system came from one rule
being re-implemented rather than shared, so the last test here asserts none of them hard-code it.
"""
import inspect

import pytest

import config
import auto_paper_cycle as apc
from analysis import assessment as A
from conftest import make_candidate, make_ctx


# ── The scaling rule ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spot, expected", [
    (773.0, 25.0),   # SPY — unchanged
    (153.6, 25.0),   # COIN — unchanged
    (100.0, 25.0),   # exactly the reference price
    (99.0, 24.75),   # just below: begins to scale
    (60.0, 15.0),    # scaled value 15.0, at the hard bottom
    (36.8, 15.0),    # IBIT — clamped to the hard bottom
    (5.0, 15.0),     # a penny-ish underlying cannot scale to nothing
])
def test_floor_scales_with_price_and_is_clamped_at_both_ends(spot, expected):
    assert config.min_credit_usd_for(spot) == expected


def test_the_floor_can_never_be_looser_than_the_flat_rule():
    """This change must be monotone: nothing that qualifies today may stop qualifying, and
    nothing above the reference price may newly qualify."""
    for spot in (100, 250, 500, 773, 5000):
        assert config.min_credit_usd_for(spot) == config.MIN_CREDIT_USD


def test_an_unknown_price_gets_the_strict_flat_floor():
    """A snapshot written before candidates carried `spot` must not open a trade the current
    contract would refuse. Missing data resolves toward strictness."""
    for bad in (None, 0, -5, "", "n/a"):
        assert config.min_credit_usd_for(bad) == config.MIN_CREDIT_USD


def test_the_hard_bottom_is_still_worth_more_than_the_fees():
    """$15 against a ~$2.16 round-trip is ~7x fees — a genuine viability test, which the flat
    $25 never was at 11.6x. If this ratio ever drops below ~4x the floor stops meaning anything."""
    from analysis.outcome_logger import _round_trip_cost_per_contract
    assert config.MIN_CREDIT_USD_FLOOR >= 4 * _round_trip_cost_per_contract()


def test_scaling_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setattr(config, "CREDIT_FLOOR_SCALES_WITH_PRICE", False)
    assert config.min_credit_usd_for(36.8) == config.MIN_CREDIT_USD


# ── The gate honours it ───────────────────────────────────────────────────────────────────────

def test_an_ibit_shaped_candidate_now_clears_the_credit_gate():
    """The live 35/34 spread: $23 natural credit, 0.230 credit-to-width, on a $36.80 underlying.
    It passed every other gate and was excluded by two dollars."""
    c = make_candidate(short_strike=35.0, long_strike=34.0, width=1.0,
                       natural_credit_per_share=0.23, credit_per_share=0.26,
                       short_delta=-0.30, dte=35)
    gates = A.evaluate_gates(c, make_ctx(spot=36.80, ticker="IBIT"))
    assert gates["min_credit_usd"] is True
    assert gates["credit_to_width"] is True


def test_the_same_credit_on_an_expensive_underlying_is_still_refused():
    """$23 on a $773 underlying is a different trade and stays blocked. The floor moved for
    cheap names only."""
    c = make_candidate(natural_credit_per_share=0.23, width=5.0)
    gates = A.evaluate_gates(c, make_ctx(spot=773.0, ticker="SPY"))
    assert gates["min_credit_usd"] is False


def test_a_genuinely_worthless_credit_is_still_refused_on_a_cheap_underlying():
    c = make_candidate(short_strike=35.0, long_strike=34.0, width=1.0,
                       natural_credit_per_share=0.09)      # the GDX shape: $9
    gates = A.evaluate_gates(c, make_ctx(spot=36.80, ticker="IBIT"))
    assert gates["min_credit_usd"] is False


# ── One definition, four consumers ────────────────────────────────────────────────────────────

def test_the_auto_open_path_computes_the_same_floor_as_the_gate():
    """auto_paper_cycle enforces the floor independently of the gates dict. If it read a
    different number, a floor relaxed in the contract would stay enforced here — the exact
    shape of the IV-rank, POP-floor and quote-spread leaks."""
    c = make_candidate(spot=36.80)
    assert apc._min_credit_floor(c) == config.min_credit_usd_for(36.80) == 15.0


def test_a_snapshot_without_spot_falls_back_to_the_strict_floor():
    c = make_candidate()
    c.pop("spot", None)
    assert apc._min_credit_floor(c) == config.MIN_CREDIT_USD


def test_candidates_carry_the_price_they_were_built_against():
    """Without this the auto-open path re-checking a JSON snapshot would compute a different
    floor from the gate that already passed it, because only the enclosing ROW knew the price."""
    import vega_candidates as vc
    assert '"spot"' in inspect.getsource(vc.build_candidates)


def _code_only(fn) -> str:
    """Source with comments and docstrings stripped — the assertion is about what the code
    DOES, and the surrounding commentary necessarily names the constant it replaced."""
    lines = []
    for ln in inspect.getsource(fn).splitlines():
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        lines.append(ln.split("  #")[0])
    body = "\n".join(lines)
    for quote in ('"""', "'''"):
        parts = body.split(quote)
        body = "".join(parts[::2]) if len(parts) > 2 else body
    return body


@pytest.mark.parametrize("fn, label", [
    (A.evaluate_gates, "assessment.evaluate_gates"),
    (apc._candidate_passes_minimum, "auto_paper_cycle._candidate_passes_minimum"),
])
def test_no_enforcement_site_hard_codes_the_floor(fn, label):
    """Four places enforce this floor. Every prior enforcement leak came from one rule being
    re-implemented rather than shared, so no site may read the raw constant."""
    assert "MIN_CREDIT_USD" not in _code_only(fn), (
        f"{label} must read the floor from config.min_credit_usd_for, not re-implement it")


def test_all_four_enforcement_sites_route_through_the_one_definition():
    import main
    from analysis import strike_validator as sv
    import vega_candidates as vc  # noqa: F401 - stamps `spot` so the floor is computable
    assert "min_credit_usd_for" in _code_only(A._min_credit_floor)
    assert "min_credit_usd_for" in _code_only(apc._min_credit_floor)
    assert "min_credit_usd_for" in inspect.getsource(sv.validate_strike)
    assert "min_credit_usd_for" in inspect.getsource(main.select_bull_put_pair)
