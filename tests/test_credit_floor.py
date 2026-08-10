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


# ── The fillable credit, on every strategy path (2026-08-10) ──────────────────────────────────

def test_natural_credit_is_sell_the_bid_buy_the_ask():
    """A credit spread cannot be filled at the mid — you cross the spread on BOTH legs."""
    from analysis.assessment import natural_credit
    n = natural_credit({"bid": 2.00, "ask": 2.40}, {"bid": 1.00, "ask": 1.30}, width=5.0)
    assert n["natural_credit_per_share"] == pytest.approx(0.70)     # 2.00 - 1.30, not 2.20 - 1.15
    assert n["natural_credit_usd"] == pytest.approx(70.0)
    assert n["natural_credit_to_width"] == pytest.approx(0.14)


def test_an_unquotable_leg_gives_zero_not_none():
    """Zero cannot clear a floor. None might sail through a gate that defaults to passing."""
    from analysis.assessment import natural_credit
    n = natural_credit({"bid": None, "ask": None}, {"bid": 1.0, "ask": 1.2}, width=5.0)
    assert n["natural_credit_per_share"] <= 0
    assert n["natural_credit_usd"] is not None


def test_a_spread_that_is_a_debit_at_real_prices_is_caught():
    """Measured live 2026-08-10: WMT 122/123 quoted an $11 MID credit and a NEGATIVE $31
    natural — the bid-ask on the two legs exceeded the whole theoretical credit. It was on the
    board as a qualified trade."""
    from analysis.assessment import natural_credit
    n = natural_credit({"bid": 0.60, "ask": 1.05}, {"bid": 0.55, "ask": 0.91}, width=1.0)
    assert n["natural_credit_per_share"] < 0


@pytest.mark.parametrize("mod, fn", [("main", "select_bull_put_pair"), ("multi_strategy", "build_bear_call"),
                          ("multi_strategy", "build_iron_condor"),
                          ("vega_candidates", "build_candidates")])
def test_no_strategy_path_still_gates_on_the_mid(mod, fn):
    """The bull-put path was corrected on 2026-08-07; bear calls and condors were not, and then
    became the entire board once the put side started pricing honestly. Every generator must
    reach the one definition."""
    import importlib, inspect
    m = importlib.import_module(mod)
    f = getattr(m, fn, None)
    if f is None:
        pytest.skip(f"{mod}.{fn} not present")
    assert "natural_credit" in inspect.getsource(f), (
        f"{mod}.{fn} does not use analysis.assessment.natural_credit — it is pricing on mids")


# ── Quote freshness (2026-08-10) ──────────────────────────────────────────────────────────────

def test_stale_quotes_do_not_produce_a_fillable_price():
    """After the close the book is unmaintained and the bid-ask blows out. Measured on GOOG
    335/330: the short leg's spread went $0.25 -> $0.90 and the long's $0.20 -> $0.60 between
    14:47 and 18:03, and the fillable credit fell from $100 to $30 with no move in the
    underlying. Gating on that number reports an empty board as though the market were paying
    nothing."""
    from analysis.assessment import fill_basis
    wide = fill_basis({"bid": 4.90, "ask": 5.80, "mid": 5.35},
                      {"bid": 4.00, "ask": 4.60, "mid": 4.30}, 5.0, live=False)
    assert wide["fill_basis"] == "modelled" and wide["quotes_live"] is False
    # The observed (meaningless) natural is still recorded, never used as the decision.
    assert wide["observed_natural_per_share"] == pytest.approx(0.30)
    assert wide["natural_credit_per_share"] > wide["observed_natural_per_share"]


def test_live_quotes_use_the_real_fill():
    from analysis.assessment import fill_basis
    live = fill_basis({"bid": 5.55, "ask": 5.80, "mid": 5.675},
                      {"bid": 4.35, "ask": 4.55, "mid": 4.45}, 5.0, live=True)
    assert live["fill_basis"] == "natural" and live["quotes_live"] is True
    assert live["natural_credit_per_share"] == pytest.approx(1.00)   # 5.55 - 4.55


def test_the_modelled_ratio_is_config_driven_and_documented():
    """A single global haircut is a textbook constant of exactly the kind this codebase keeps
    finding. It is only defensible while it stays explicitly provisional and measured."""
    import config
    assert 0 < config.MODELLED_FILL_RATIO <= 1.0


def test_market_hours_decide_the_basis():
    import datetime as dt
    from analysis.assessment import quotes_are_live
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        pytest.skip("no tz database")
    et = ZoneInfo("America/New_York")
    assert quotes_are_live(dt.datetime(2026, 8, 10, 11, 0, tzinfo=et)) is True    # Monday 11:00
    assert quotes_are_live(dt.datetime(2026, 8, 10, 18, 3, tzinfo=et)) is False   # after close
    assert quotes_are_live(dt.datetime(2026, 8, 10, 9, 0, tzinfo=et)) is False    # pre-open
    assert quotes_are_live(dt.datetime(2026, 8, 8, 11, 0, tzinfo=et)) is False    # Saturday
