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


def test_the_horizon_is_counted_in_trading_days_not_calendar_days():
    """`dte` is CALENDAR days everywhere in this codebase, but realised vol is annualised off
    daily BARS. Feeding a calendar count into a trading-day formula stretched every band by
    sqrt(365/252) = 1.204 — an "80%" window that actually covered 88%."""
    b = pp.project(100.0, 35, 30.0)
    assert b["trading_days"] == pytest.approx(24.2, abs=0.2)
    expected = 0.30 * math.sqrt(24.16 / 252)
    assert b["sigma_horizon"] == pytest.approx(expected, rel=0.01)


def test_a_calendar_horizon_is_narrower_than_the_naive_trading_day_read():
    b = pp.project(100.0, 35, 30.0)
    naive_high = 100.0 * math.exp(1.2816 * 0.30 * math.sqrt(35 / 252))
    assert b["high"] < naive_high


# ── The market's own band, read off the chain ─────────────────────────────────────────────────
# How a desk actually answers "where does the market think it will be", with no vol model at
# all: an option's delta is roughly the risk-neutral probability it finishes ITM, so a
# 10-delta put's STRIKE is the price the market gives a 10% chance of being below.

def _chain(strikes_deltas, iv=0.25, dte=35):
    return [{"strike": k, "delta": d, "iv": iv, "dte": dte} for k, d in strikes_deltas]


def _puts():   # strikes below spot 100
    return _chain([(95, -0.30), (90, -0.20), (85, -0.10), (80, -0.05)])


def _calls():  # strikes above spot 100
    return _chain([(105, 0.30), (110, 0.20), (115, 0.10), (120, 0.05)])


def test_the_band_edges_are_the_strikes_at_the_tail_deltas():
    """An 80% window puts 10% in each tail, so it is literally the 10-delta put strike and the
    10-delta call strike."""
    b = pp.implied_band_from_chain(_puts(), _calls(), 100.0, confidence=0.80)
    assert b["low"] == 85.0 and b["high"] == 115.0
    assert b["source"] == "chain"


def test_a_wider_confidence_reaches_further_out_the_chain():
    b90 = pp.implied_band_from_chain(_puts(), _calls(), 100.0, confidence=0.90)
    b80 = pp.implied_band_from_chain(_puts(), _calls(), 100.0, confidence=0.80)
    assert b90["low"] < b80["low"] and b90["high"] > b80["high"]


def test_the_market_band_can_be_asymmetric():
    """Equity puts trade richer than equidistant calls. A symmetric band asserts the opposite
    of what the chain says, and the asymmetry is real information."""
    puts = _chain([(88, -0.10)])          # downside edge 12% away
    calls = _chain([(108, 0.10)])         # upside edge only 8% away
    b = pp.implied_band_from_chain(puts, calls, 100.0)
    assert b["skew_pct"] > 0, "put skew should push the downside edge further from spot"


def test_both_sides_of_the_book_are_required():
    """Taking both edges from one side would mirror an asymmetry the market does not price."""
    assert pp.implied_band_from_chain(_puts(), [], 100.0) is None
    assert pp.implied_band_from_chain([], _calls(), 100.0) is None


def test_a_chain_that_never_reaches_the_tail_declines_to_answer():
    """Returning the closest available strike would report a 30% tail as a 10% one."""
    shallow_p = _chain([(97, -0.40), (95, -0.35)])
    shallow_c = _chain([(103, 0.40), (105, 0.35)])
    assert pp.implied_band_from_chain(shallow_p, shallow_c, 100.0, confidence=0.90) is None


def test_strikes_on_the_wrong_side_of_spot_are_ignored():
    puts = _puts() + _chain([(130, -0.10)])       # a put above spot is not a downside edge
    b = pp.implied_band_from_chain(puts, _calls(), 100.0)
    assert b["low"] == 85.0


# ── The comparison: VEGA against the market ───────────────────────────────────────────────────

def test_a_narrower_vega_band_favours_the_seller():
    """The premium-selling thesis drawn rather than scored: the market is paying for more
    movement than the engine expects."""
    mkt = pp.implied_band_from_chain(_puts(), _calls(), 100.0)     # 85-115
    vega = pp.project(100.0, 35, 20.0)
    c = pp.compare_bands(vega, mkt)
    assert c["width_ratio"] < 1.0 and c["favours_seller"] is True
    assert "pays for more move" in c["verdict"]


def test_a_wider_vega_band_does_not_favour_the_seller():
    mkt = pp.implied_band_from_chain(_puts(), _calls(), 100.0)
    c = pp.compare_bands(pp.project(100.0, 35, 90.0), mkt)
    assert c["favours_seller"] is False
    assert "VEGA expects more move" in c["verdict"]


def test_each_side_is_compared_separately():
    """A bull put only cares about the downside edge; one blended width would hide a name whose
    disagreement is entirely on the side the trade is not exposed to."""
    mkt = pp.implied_band_from_chain(_puts(), _calls(), 100.0)
    c = pp.compare_bands(pp.project(100.0, 35, 20.0), mkt)
    assert "downside_gap_pct" in c and "upside_gap_pct" in c


def test_comparison_needs_both_bands():
    assert pp.compare_bands(None, {"low": 1, "high": 2, "spot": 1}) is None
    assert pp.compare_bands(pp.project(100.0, 35, 20.0), None) is None


def test_the_card_shows_the_market_band_when_the_trade_carries_one():
    import re
    import vega_app
    h = vega_app._price_band_html({
        "ticker": "WMT", "price": 115.72, "dte": 35, "rv_forecast_pp": 24.6,
        "strat_type": "bull_put", "short": 104.0,
        "implied_band": {"spot": 115.72, "confidence": 0.8, "low": 105.0, "high": 130.0,
                         "low_pct": -9.3, "high_pct": 12.3, "low_delta": 0.128,
                         "high_delta": 0.118, "skew_pct": -3.08}})
    t = re.sub(r"<[^>]+>", " ", h)
    assert "Market expects" in t and "Disagreement" in t
    assert "carries the skew" in t
