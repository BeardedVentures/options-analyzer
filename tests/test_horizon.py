"""Horizon calibration — analysis/horizon.py.

Every structural module analysed a fixed 180 days with fixed indicator periods, and none of
structure / levels / entry_timing / huginn contained a single reference to DTE. A 25-day
spread and a 45-day spread got an identical technical read, and a support level six months
out was weighted the same as one price will touch next week.

Two questions are asked of every structural fact, and the tests below exist to keep both
honest:

  1. Is it REACHABLE inside this trade's life? Distance measured in the expected move the
     option market is pricing over the REMAINING days — the only unit that already knows both
     how volatile the name is and how long this particular trade has.
  2. Will it RESOLVE in time? A bull flag needing 20 more bars is real and useless to a
     12-day spread at the same time.
"""
import pytest

import config
from analysis.horizon import (
    calibrate,
    calibrated_lookback_bars,
    classify_reach,
    expected_move,
    pattern_fits_horizon,
    sigmas_away,
    strategy_sides,
)


def _lvl(price, touches=3, strength=60.0):
    return {"price": float(price), "touches": touches, "strength": strength}


# ── Expected move ─────────────────────────────────────────────────────────────────────────────

def test_expected_move_scales_with_sqrt_of_time():
    """The whole point: a 45-day trade prices a bigger move than a 25-day one on identical
    IV, so identical structure means different things to each."""
    a = expected_move(100.0, 0.30, 25)
    b = expected_move(100.0, 0.30, 100)
    assert b == pytest.approx(a * 2, rel=0.02)      # 4x the days = 2x the move


def test_expected_move_scales_with_vol():
    assert expected_move(100.0, 0.60, 30) == pytest.approx(
        2 * expected_move(100.0, 0.30, 30), rel=1e-6)


def test_expected_move_needs_all_three_inputs():
    for args in ((0, 0.3, 30), (100.0, None, 30), (100.0, 0.3, 0), (100.0, 0.3, None)):
        assert expected_move(*args) is None


# ── Reachability ──────────────────────────────────────────────────────────────────────────────

def test_sigmas_away_is_signed():
    em = expected_move(100.0, 0.30, 30)
    assert sigmas_away(100.0, 90.0, em) < 0 < sigmas_away(100.0, 110.0, em)


@pytest.mark.parametrize("sig,expected", [
    (0.2, "in_play"), (-0.4, "in_play"),
    (0.8, "likely_tested"), (-1.0, "likely_tested"),
    (1.5, "reachable"), (-2.0, "reachable"),
    (2.5, "out_of_reach"), (-4.0, "out_of_reach"),
])
def test_reach_classification(sig, expected):
    assert classify_reach(sig) == expected


def test_unknown_reach_when_move_cannot_be_priced():
    assert classify_reach(None) == "unknown"
    assert sigmas_away(100.0, 90.0, None) is None


def test_distant_levels_are_excluded_from_play():
    """A level three expected moves away is scenery — it cannot be tested before expiry, so
    it is neither a shield nor a threat."""
    c = calibrate("bull_put_spread", spot=100.0, iv=0.20, dte=30,
                  support_levels=[_lvl(98), _lvl(60)])
    prices = [l["price"] for l in c["levels_in_play"]]
    assert 98.0 in prices and 60.0 not in prices


# ── Strategy scoping ──────────────────────────────────────────────────────────────────────────

def test_strategy_decides_which_side_is_even_asked_about():
    assert strategy_sides("bull_put_spread") == ["down"]
    assert strategy_sides("bear_call_spread") == ["up"]
    assert strategy_sides("iron_condor") == ["down", "up"]


def test_bull_put_ignores_resistance():
    """A bull put is a claim about the downside. Resistance above is not its problem."""
    c = calibrate("bull_put_spread", spot=100.0, iv=0.25, dte=30,
                  support_levels=[_lvl(96)], resistance_levels=[_lvl(104)])
    assert {l["side"] for l in c["levels_in_play"]} == {"support"}


def test_bear_call_ignores_support():
    c = calibrate("bear_call_spread", spot=100.0, iv=0.25, dte=30,
                  support_levels=[_lvl(96)], resistance_levels=[_lvl(104)])
    assert {l["side"] for l in c["levels_in_play"]} == {"resistance"}


def test_condor_asks_about_both_wings():
    c = calibrate("iron_condor", spot=100.0, iv=0.25, dte=30,
                  support_levels=[_lvl(96)], resistance_levels=[_lvl(104)])
    assert {l["side"] for l in c["levels_in_play"]} == {"support", "resistance"}


# ── Lookback calibration ──────────────────────────────────────────────────────────────────────

def test_lookback_scales_with_the_trade_horizon():
    """A 25-day spread is decided by the last few months, not by last winter."""
    assert calibrated_lookback_bars(25) < calibrated_lookback_bars(60)


def test_lookback_is_clamped_at_both_ends():
    assert calibrated_lookback_bars(1) >= config.HORIZON_LOOKBACK_MIN_BARS
    assert calibrated_lookback_bars(9999) <= config.HORIZON_LOOKBACK_MAX_BARS


def test_missing_dte_falls_back_to_the_fixed_window():
    assert calibrated_lookback_bars(None) == config.STRUCTURE_LOOKBACK_DAYS


# ── Pattern resolution clock ──────────────────────────────────────────────────────────────────

def test_pattern_that_cannot_resolve_in_time_is_flagged():
    """A flag building 30 bars needs roughly 30 more. A 10-day spread does not get to see it."""
    r = pattern_fits_horizon({"pattern": "BULL_FLAG", "bars_since_pivot": 30}, dte=10)
    assert r["fits"] is False
    assert "will not complete before expiry" in r["note"]


def test_pattern_that_resolves_in_time_is_confirmed():
    r = pattern_fits_horizon({"pattern": "BULL_FLAG", "bars_since_pivot": 5}, dte=45)
    assert r["fits"] is True
    assert "can play out inside this trade" in r["note"]


def test_calendar_dte_is_converted_to_trading_bars():
    """45 calendar days is about 31 trading days, not 45. Comparing bars to calendar days
    would overstate the time a pattern has to resolve by roughly a third."""
    r = pattern_fits_horizon({"pattern": "BULL_FLAG", "bars_since_pivot": 5}, dte=45)
    assert 28 <= r["bars_available"] <= 33


def test_shapeless_pattern_has_no_clock():
    r = pattern_fits_horizon({"pattern": "UNREADABLE", "bars_since_pivot": None}, dte=30)
    assert r["fits"] is None


def test_missing_dte_or_structure_is_survivable():
    assert pattern_fits_horizon({}, None)["fits"] is None
    assert pattern_fits_horizon(None, 30)["fits"] is None


# ── The narrative ─────────────────────────────────────────────────────────────────────────────

def test_narrative_states_the_move_the_side_and_the_clock():
    c = calibrate("bull_put_spread", spot=100.0, iv=0.30, dte=30, short_strike=94.0,
                  support_levels=[_lvl(96)],
                  structure={"pattern": "BULL_FLAG", "bars_since_pivot": 4})
    t = c["plain_english"]
    assert "30 days left" in t
    assert "downside" in t
    assert "expected moves away" in t
    assert "$96" in t
    assert "resolves" in t or "resolve" in t


def test_narrative_says_so_when_no_level_is_reachable():
    c = calibrate("bull_put_spread", spot=100.0, iv=0.10, dte=15,
                  support_levels=[_lvl(50)])
    assert "neither help nor threat" in c["plain_english"]


def test_narrative_degrades_honestly_without_iv():
    c = calibrate("bull_put_spread", spot=100.0, iv=None, dte=30)
    assert "cannot be measured" in c["plain_english"]
    assert c["expected_move"] is None


def test_strike_inside_the_expected_move_is_called_in_play():
    """A 0.16-0.30 delta band encodes this but never states it. A strike half an expected
    move away is genuinely reachable and should be said out loud."""
    c = calibrate("bull_put_spread", spot=100.0, iv=0.40, dte=45, short_strike=93.0)
    assert c["strike_reach"] in ("in_play", "likely_tested")
    assert "genuinely in play" in c["plain_english"]


def test_strike_beyond_the_expected_move_is_called_out_of_reach():
    c = calibrate("bull_put_spread", spot=100.0, iv=0.12, dte=20, short_strike=80.0)
    assert c["strike_reach"] == "out_of_reach"
    assert "beyond what the market prices as reachable" in c["plain_english"]


def test_levels_in_play_are_ordered_nearest_first():
    c = calibrate("bull_put_spread", spot=100.0, iv=0.30, dte=40,
                  support_levels=[_lvl(90), _lvl(98), _lvl(94)])
    sig = [abs(l["sigmas"]) for l in c["levels_in_play"]]
    assert sig == sorted(sig)


def test_output_shape_is_stable():
    c = calibrate("bull_put_spread", spot=100.0, iv=0.3, dte=30)
    for k in ("dte", "expected_move", "expected_move_pct", "sides", "lookback_bars",
              "strike_sigmas", "strike_reach", "levels_in_play", "pattern_horizon",
              "plain_english"):
        assert k in c


def test_engine_attaches_the_horizon_after_entry_timing():
    """It reads the structure entry_timing produces, so ordering matters — referencing it
    inside the trade dict raised UnboundLocalError on the first live run."""
    import inspect

    import main
    src = inspect.getsource(main.screen_ticker)
    assert src.index('trade["entry_timing"]') < src.index('trade["horizon"]')
