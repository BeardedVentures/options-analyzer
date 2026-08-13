"""The projected price window at expiry.

A credit spread is a bet about a RANGE, and the board showed a strike, a breakeven and a
probability without ever drawing the distribution those came from.

The method is coverage-tested rather than assumed. On 14,800 held-out observations across 20
names and 8 years — does a claimed X% window actually contain the price X% of the time?

    target   lognormal+trailing   lognormal+FORECAST   empirical+forecast
      50%          50.9%                52.7%                55.6%
      80%          79.5%                81.5%                83.0%
      90%          87.9%                89.7%                92.0%

Lognormal on FORECAST vol tracks the target within ~1.7pp and errs slightly wide, which is the
right direction for something a person sizes risk against. Empirical quantiles over-cover by
3pp — a window wider than it claims quietly stops being a constraint.
"""
import math

import pytest

from analysis import price_projection as pp


def test_a_band_is_symmetric_in_log_space():
    """Zero drift. A band that leans is a band making a direction call, and sector relative
    strength was tested as a drift input and rejected (corr +0.01, not significant)."""
    b = pp.project(100.0, 35, 30.0)
    # Equal log distances either side of spot. Asserted in log space rather than on the
    # rounded dollar prices, which is where the property actually lives.
    down = math.log(b["spot"] / b["low"])
    up = math.log(b["high"] / b["spot"])
    # rel tolerance covers cent-rounding of the two prices, which is the only asymmetry here.
    assert down == pytest.approx(up, rel=1e-3)


def test_more_confidence_means_a_wider_window():
    narrow = pp.project(100.0, 35, 30.0, confidence=0.50)
    wide = pp.project(100.0, 35, 30.0, confidence=0.90)
    assert wide["low"] < narrow["low"] and wide["high"] > narrow["high"]


def test_more_time_means_a_wider_window():
    assert pp.project(100.0, 45, 30.0)["high"] > pp.project(100.0, 25, 30.0)["high"]


def test_more_vol_means_a_wider_window():
    assert pp.project(100.0, 35, 60.0)["high"] > pp.project(100.0, 35, 20.0)["high"]


def test_the_band_scales_with_sqrt_time():
    """The one property that makes a horizon projection defensible rather than fitted."""
    a = pp.project(100.0, 35, 30.0)["sigma_horizon"]
    b = pp.project(100.0, 140, 30.0)["sigma_horizon"]
    assert b / a == pytest.approx(2.0, rel=0.01)


def test_a_missing_input_yields_no_band_at_all():
    """A band drawn from a guessed volatility is worse than none — it looks identical to a
    real one."""
    assert pp.project(None, 35, 30.0) is None
    assert pp.project(100.0, None, 30.0) is None
    assert pp.project(100.0, 35, None) is None
    assert pp.project(100.0, 35, 0) is None
    assert pp.project(100.0, 0, 30.0) is None


def test_the_measured_coverage_is_reported_not_the_claimed_one():
    """80% is the promise; 81.5% is what the held-out sample delivered. The UI shows the
    second, because the first is an assertion and the second is evidence."""
    b = pp.project(100.0, 35, 30.0, confidence=0.80)
    assert b["measured_coverage"] == pytest.approx(0.815, abs=0.001)


def test_the_z_score_matches_the_normal_quantile():
    assert pp._z_for(0.80) == pytest.approx(1.2816, abs=0.001)
    assert pp._z_for(0.90) == pytest.approx(1.6449, abs=0.001)


# ── Strike position: what connects the band to the trade ──────────────────────────────────────

def test_a_strike_beyond_the_band_edge_reads_as_outside():
    b = pp.project(100.0, 35, 30.0)               # 80% low ~ 86.6
    sp = pp.strike_position(b, 80.0, "put")
    assert sp["inside_band"] is False
    assert sp["cushion_pct"] > 0
    assert "beyond" in sp["note"]


def test_a_strike_inside_the_band_says_so():
    b = pp.project(100.0, 35, 30.0)
    sp = pp.strike_position(b, 94.0, "put")
    assert sp["inside_band"] is True
    assert sp["cushion_pct"] < 0
    assert "inside where the projection expects" in sp["note"]


def test_the_call_side_is_threatened_from_above():
    """A bear call is breached by a rally, so its band edge is the HIGH one. Reusing the put
    geometry would mark every call spread safe."""
    b = pp.project(100.0, 35, 30.0)
    assert pp.strike_position(b, 120.0, "call")["inside_band"] is False
    assert pp.strike_position(b, 106.0, "call")["inside_band"] is True


def test_no_strike_means_no_strike_reading():
    assert pp.strike_position(pp.project(100.0, 35, 30.0), None) is None
    assert pp.strike_position(None, 95.0) is None


# ── The wiring ────────────────────────────────────────────────────────────────────────────────

def test_the_band_uses_the_same_forecast_the_edge_score_uses():
    """If the band and the VRP were built from different volatilities the page would tell the
    reader two incompatible stories about how far the stock can travel."""
    import inspect
    import vega_app
    assert "rv_forecast_pp" in inspect.getsource(vega_app._price_band)


def test_a_card_without_a_forecast_renders_nothing_rather_than_a_guess():
    import vega_app
    assert vega_app._price_band({"ticker": "X", "price": 100.0, "dte": 35}) is None
    assert vega_app._price_band_html({"ticker": "X", "price": 100.0, "dte": 35}) == ""


def test_the_rendered_band_states_that_direction_is_not_predicted():
    import re
    import vega_app
    h = vega_app._price_band_html({"ticker": "WMT", "price": 115.13, "dte": 35,
                                   "rv_forecast_pp": 24.6, "strat_type": "bull_put",
                                   "short": 104.0})
    t = re.sub(r"<[^>]+>", " ", h)
    assert "Direction is not predicted" in t
    assert "wrong about one time in" in t, "a confidence band must state its own failure rate"
