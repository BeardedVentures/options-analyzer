"""Directional exposure, and the honest hedge for it.

A desk sells premium and delta-hedges continuously with shares — that is the whole trick, and
it is why their per-trade edge is small and repeatable. Retail cannot do it: the hedge has to
be rebalanced as delta moves, and a one-contract bull put carries ~15-20 share-equivalents,
which on a $115 stock is ~$2,000 of capital to neutralise a position whose entire max loss is
$68. Pretending otherwise would be the most expensive kind of advice.

So this states the exposure, aggregates it across the book, and suggests the structure rather
than the share trade.
"""
import pytest

from analysis import hedge


def test_a_bull_put_carries_long_exposure():
    """Short put profits as the stock rises. A reader told "0.20 delta" learns nothing; a
    reader told "behaves like owning 8 shares" learns the shape of the risk."""
    p = hedge.position_delta(-0.20, 1, strategy="bull_put")
    assert p["share_equivalent"] > 0 and p["direction"] == "long"


def test_a_bear_call_carries_short_exposure():
    p = hedge.position_delta(0.20, 1, strategy="bear_call")
    assert p["share_equivalent"] < 0 and p["direction"] == "short"


def test_a_condor_is_structurally_neutral():
    """Two offsetting wings — the structural version of a hedge, and the reason it is the
    affordable one."""
    assert hedge.position_delta(-0.20, 1, strategy="iron_condor")["direction"] == "neutral"


def test_the_long_leg_offsets_part_of_the_exposure():
    """Net delta is the SPREAD's, not the short leg's. Using the short leg alone overstates
    the exposure by the same mistake the CLV baseline made with theta."""
    p = hedge.position_delta(-0.30, 1, long_delta=-0.20, strategy="bull_put")
    assert p["share_equivalent"] == pytest.approx(10.0, abs=0.1)
    assert p["long_leg_estimated"] is False


def test_an_estimated_long_leg_is_labelled_as_one():
    p = hedge.position_delta(-0.20, 1, strategy="bull_put")
    assert p["long_leg_estimated"] is True


def test_exposure_scales_with_contracts():
    a = hedge.position_delta(-0.20, 1)["share_equivalent"]
    assert hedge.position_delta(-0.20, 3)["share_equivalent"] == pytest.approx(3 * a)


def test_no_delta_means_no_reading():
    assert hedge.position_delta(None, 1) is None


# ── The suggestion ────────────────────────────────────────────────────────────────────────────

def test_a_share_hedge_is_rejected_on_the_numbers_not_a_rule_of_thumb():
    """$926 of stock to protect $68 of max loss. Showing the ratio once teaches more than
    asserting that retail should not hedge."""
    h = hedge.hedge_suggestion({"ticker": "WMT", "short_delta": -0.20, "price": 115.72,
                                "max_loss_usd": 68.0, "strat_type": "bull_put"})
    assert h["share_hedge_worth_it"] is False
    assert h["hedge_cost_ratio"] > 10
    assert "iron condor" in h["suggestion"]


def test_a_proportionate_share_hedge_is_allowed_to_say_so():
    """The rule is the arithmetic, not a blanket ban — a low-priced name with a wide spread
    can be worth hedging."""
    h = hedge.hedge_suggestion({"ticker": "X", "short_delta": -0.20, "price": 50.0,
                                "max_loss_usd": 900.0, "strat_type": "bull_put"})
    assert h["share_hedge_worth_it"] is True
    assert "shares to flatten" in h["suggestion"]


def test_the_suggested_hedge_opposes_the_exposure():
    h = hedge.hedge_suggestion({"ticker": "WMT", "short_delta": -0.20, "price": 115.0,
                                "max_loss_usd": 68.0, "strat_type": "bull_put"})
    assert h["hedge_shares"] < 0 < h["share_equivalent"]


def test_the_structural_hedge_names_the_opposite_spread():
    h = hedge.hedge_suggestion({"ticker": "WMT", "short_delta": -0.20, "price": 115.0,
                                "max_loss_usd": 68.0, "strat_type": "bull_put"})
    assert "bear call" in h["suggestion"], "a bull put is hedged by the call side"


# ── Book level: the number no screen showed ───────────────────────────────────────────────────

def test_the_book_aggregates_direction_across_positions():
    """Fourteen bull puts are not fourteen independent bets. They are one large long position,
    and the correlation arrives exactly when it hurts."""
    opens = [{"ticker": t, "delta": -0.20, "contracts": 1, "strategy": "bull_put_spread"}
             for t in ("AAA", "BBB", "CCC")]
    b = hedge.book_delta(opens)
    assert b["positions"] == 3
    assert b["direction"] == "long"
    assert b["share_equivalent"] == pytest.approx(3 * 8.0, abs=0.5)


def test_opposing_positions_net_against_each_other():
    opens = [{"ticker": "AAA", "delta": -0.20, "contracts": 1, "strategy": "bull_put_spread"},
             {"ticker": "AAA", "delta": 0.20, "contracts": 1, "strategy": "bear_call_spread"}]
    assert hedge.book_delta(opens)["share_equivalent"] == pytest.approx(0.0, abs=0.1)


def test_the_book_groups_by_ticker_as_well_as_totalling():
    """Concentration in one name and concentration across the book are different problems
    with different fixes."""
    opens = [{"ticker": "AAA", "delta": -0.20, "contracts": 2, "strategy": "bull_put_spread"},
             {"ticker": "BBB", "delta": -0.20, "contracts": 1, "strategy": "bull_put_spread"}]
    by = hedge.book_delta(opens)["by_ticker"]
    assert by["AAA"] > by["BBB"], "the larger position must sort first"


def test_an_empty_book_is_neutral_not_an_error():
    b = hedge.book_delta([])
    assert b["positions"] == 0 and b["direction"] == "neutral"


# ── Wiring ────────────────────────────────────────────────────────────────────────────────────

def test_the_card_states_the_exposure_and_the_cost_of_removing_it():
    import re
    import vega_app
    t = re.sub(r"<[^>]+>", " ", vega_app._hedge_html(
        {"ticker": "WMT", "short_delta": -0.20, "price": 115.72, "max_loss_usd": 68.0,
         "strat_type": "bull_put"}))
    assert "Behaves like" in t and "shares" in t
    assert "max loss" in t


def test_a_card_without_delta_renders_nothing():
    import vega_app
    assert vega_app._hedge_html({"ticker": "X"}) == ""
