"""Volatility surface — analysis/vol_surface.py.

VEGA read IV as one scalar per trade. The surface has two axes that both change what a
premium seller should do: term structure across expirations, and skew depth across strikes.

The tests that matter most here are the negative ones. A term-structure reader that cannot
say "unknown" will invent a slope from two illiquid quotes, and a slope is worth ±8 points of
edge score — so a confident wrong read is expensive.
"""
import datetime

import pytest

import config
from analysis.vol_surface import (
    _atm_iv,
    _iv_at_delta,
    get_skew_depth,
    get_term_structure,
)

TODAY = datetime.date(2026, 8, 5)


def _row(strike=100.0, iv=0.25, typ="put", delta=-0.30):
    """A chain row in THIS codebase's shape: type / iv / expiration, not Polygon's raw
    contract_type / implied_volatility. The original spec assumed the raw names, which appear
    nowhere in the repo and would have made every read silently empty."""
    return {"strike": strike, "iv": iv, "type": typ, "delta": delta}


def _exp(days):
    return (TODAY + datetime.timedelta(days=days)).isoformat()


def _chain(*pairs):
    """(dte, iv) -> {expiration: [rows]} with a strike ladder around 100."""
    out = {}
    for dte, iv in pairs:
        out[_exp(dte)] = [_row(strike=s, iv=iv) for s in (90.0, 100.0, 110.0)]
    return out


# ── Term structure ────────────────────────────────────────────────────────────────────────────

def test_upward_slope_when_back_months_price_richer():
    r = get_term_structure(_chain((30, 0.24), (73, 0.31)), 100.0, today=TODAY)
    assert r["slope"] == "upward"
    assert r["term_spread_pts"] == pytest.approx(7.0, abs=0.2)


def test_downward_slope_when_the_front_is_most_expensive():
    r = get_term_structure(_chain((30, 0.32), (73, 0.24)), 100.0, today=TODAY)
    assert r["slope"] == "downward"
    assert r["term_spread_pts"] < 0


def test_flat_slope_inside_the_band():
    r = get_term_structure(_chain((30, 0.25), (73, 0.256)), 100.0, today=TODAY)
    assert r["slope"] == "flat"


def test_event_spike_is_detected_in_the_middle():
    """A dated catalyst prints as one expiration far above the line its neighbours sit on."""
    r = get_term_structure(_chain((20, 0.24), (45, 0.52), (75, 0.28)), 100.0, today=TODAY)
    assert r["slope"] == "event_spike"
    assert r["event_expiry"] == _exp(45)


def test_a_high_front_month_is_downward_not_an_event_spike():
    """Endpoints are excluded from spike detection on purpose: an expensive front month IS
    the downward case, and conflating the two would double-penalise it."""
    r = get_term_structure(_chain((20, 0.52), (45, 0.26), (75, 0.24)), 100.0, today=TODAY)
    assert r["slope"] == "downward"
    assert r["event_expiry"] is None


def test_two_expirations_read_but_flag_low_confidence():
    r = get_term_structure(_chain((30, 0.24), (73, 0.31)), 100.0, today=TODAY)
    assert r["confidence"] == "low"


def test_three_expirations_earn_high_confidence():
    r = get_term_structure(_chain((20, 0.24), (45, 0.26), (75, 0.28)), 100.0, today=TODAY)
    assert r["confidence"] == "high"


def test_single_expiration_is_unknown_not_a_guess():
    r = get_term_structure(_chain((30, 0.24)), 100.0, today=TODAY)
    assert r["slope"] == "unknown"
    assert r["term_spread_pts"] is None


def test_empty_and_malformed_input_survive():
    for bad in ({}, None, {"not-a-date": [_row()]}):
        r = get_term_structure(bad, 100.0, today=TODAY)
        assert r["slope"] == "unknown"


def test_expirations_outside_the_window_are_ignored():
    """Weeklies and LEAPS are not the term structure a 25-45 DTE seller trades against."""
    r = get_term_structure(_chain((2, 0.90), (30, 0.24), (73, 0.31), (400, 0.10)),
                           100.0, today=TODAY)
    dtes = [p["dte"] for p in r["expirations"]]
    assert 2 not in dtes and 400 not in dtes
    assert dtes == sorted(dtes)


def test_rows_with_junk_iv_are_dropped():
    chain = {_exp(30): [_row(iv=0.0), _row(iv=None), _row(iv=9.9)]}
    assert get_term_structure(chain, 100.0, today=TODAY)["slope"] == "unknown"


def test_atm_iv_picks_the_strike_nearest_spot():
    rows = [_row(strike=90, iv=0.40), _row(strike=101, iv=0.20), _row(strike=130, iv=0.60)]
    assert _atm_iv(rows, 100.0) == pytest.approx(0.20)


def test_spike_is_measured_against_its_neighbours_not_the_mean():
    """Standard deviation is the wrong yardstick: the spike is part of the sample, so it
    inflates both the mean and the sigma it would have to clear. This 0.24 / 0.52 / 0.28
    curve — an unmistakable catalyst — sits at only 1.42 sigma and slipped under a 1.5 sigma
    test entirely."""
    r = get_term_structure(_chain((20, 0.24), (45, 0.52), (75, 0.28)), 100.0, today=TODAY)
    assert r["slope"] == "event_spike"


def test_event_threshold_is_config_driven(monkeypatch):
    chain = _chain((20, 0.24), (45, 0.34), (75, 0.28))   # ~8pt excess over the line
    monkeypatch.setattr(config, "TERM_STRUCTURE_EVENT_EXCESS_PTS", 15.0, raising=False)
    assert get_term_structure(chain, 100.0, today=TODAY)["slope"] != "event_spike"
    monkeypatch.setattr(config, "TERM_STRUCTURE_EVENT_EXCESS_PTS", 5.0, raising=False)
    assert get_term_structure(chain, 100.0, today=TODAY)["slope"] == "event_spike"


def test_largest_spike_wins_when_several_qualify():
    r = get_term_structure(_chain((20, 0.24), (40, 0.40), (60, 0.55), (80, 0.28)),
                           100.0, today=TODAY)
    assert r["event_expiry"] == _exp(60)


# ── Expiration sampling (data/fetcher.get_chain_by_expiry) ────────────────────────────────────

def test_chain_sampling_spans_the_dte_range_not_the_nearest_n(monkeypatch):
    """Live SPY exposed this: the nearest six expirations were 5, 6, 7, 8, 9 and 16 DTE — six
    adjacent weeklies whose farthest point did not even reach the 25-45 window the engine
    trades. Comparing front against back is the whole purpose, so the endpoints have to be
    far apart."""
    from data import fetcher

    rows = []
    for dte in (5, 6, 7, 8, 9, 16, 23, 30, 44, 72, 107, 117):
        exp = (TODAY + datetime.timedelta(days=dte)).isoformat()
        rows.append({"expiration": exp, "strike": 100.0, "iv": 0.25,
                     "type": "put", "delta": -0.3, "dte": dte})
    monkeypatch.setattr(fetcher, "get_options_chain", lambda *a, **k: rows)

    got = fetcher.get_chain_by_expiry("SPY", max_expirations=6)
    dtes = sorted((datetime.date.fromisoformat(k) - TODAY).days for k in got)
    assert len(got) <= 6
    assert dtes[0] == 5 and dtes[-1] == 117, f"endpoints not preserved: {dtes}"
    assert max(dtes) - min(dtes) > 90, f"sampled a narrow cluster: {dtes}"


def test_chain_by_expiry_returns_empty_on_failure(monkeypatch):
    """A term-structure read is advisory; it must never be able to break a scan."""
    from data import fetcher

    def _boom(*a, **k):
        raise RuntimeError("polygon down")
    monkeypatch.setattr(fetcher, "get_options_chain", _boom)
    assert fetcher.get_chain_by_expiry("SPY") == {}
    monkeypatch.setattr(fetcher, "get_options_chain", lambda *a, **k: [])
    assert fetcher.get_chain_by_expiry("SPY") == {}


# ── Skew depth ────────────────────────────────────────────────────────────────────────────────

def _side(typ, ivs):
    """ivs keyed by |delta|."""
    return [_row(typ=typ, delta=(-d if typ == "put" else d), iv=v) for d, v in ivs.items()]


def test_steep_put_skew_is_recognised():
    puts = _side("put", {0.20: 0.34, 0.30: 0.31, 0.40: 0.29})
    calls = _side("call", {0.20: 0.26, 0.30: 0.26, 0.40: 0.27})
    r = get_skew_depth(puts, calls, 100.0, "bull_put")
    assert r["skew_steepness"] == "steep"
    assert r["skew_20d"] == pytest.approx(8.0, abs=0.1)
    assert r["confidence"] == "high"


def test_flat_skew_is_recognised():
    puts = _side("put", {0.20: 0.263, 0.30: 0.26, 0.40: 0.26})
    calls = _side("call", {0.20: 0.26, 0.30: 0.26, 0.40: 0.26})
    assert get_skew_depth(puts, calls, 100.0, "bull_put")["skew_steepness"] == "flat"


def test_inverted_skew_is_recognised():
    puts = _side("put", {0.20: 0.22, 0.30: 0.24, 0.40: 0.25})
    calls = _side("call", {0.20: 0.32, 0.30: 0.30, 0.40: 0.28})
    assert get_skew_depth(puts, calls, 100.0, "bull_put")["skew_steepness"] == "inverted"


def test_one_sided_book_cannot_produce_skew():
    """Put-versus-call skew is not computable from one side. The original spec's single-chain
    signature could only ever have returned nonsense here."""
    puts = _side("put", {0.20: 0.34, 0.30: 0.31})
    assert get_skew_depth(puts, [], 100.0)["skew_steepness"] == "unknown"
    assert get_skew_depth([], puts, 100.0)["skew_steepness"] == "unknown"


def test_partial_curve_lowers_confidence():
    puts = _side("put", {0.20: 0.34})
    calls = _side("call", {0.20: 0.26})
    r = get_skew_depth(puts, calls, 100.0)
    assert r["confidence"] == "low"
    assert r["skew_30d"] is None


def test_delta_tolerance_rejects_a_far_contract():
    """A chain of deep-ITM quotes must not masquerade as a 20-delta read."""
    puts = [_row(typ="put", delta=-0.85, iv=0.40)]
    calls = [_row(typ="call", delta=0.85, iv=0.20)]
    assert get_skew_depth(puts, calls, 100.0)["skew_20d"] is None


def test_iv_at_delta_picks_the_closest_within_tolerance():
    rows = [_row(delta=-0.22, iv=0.31), _row(delta=-0.36, iv=0.28)]
    assert _iv_at_delta(rows, 0.20) == pytest.approx(0.31)


def test_strategy_flips_the_sign_of_the_premium():
    """Rich puts help a put seller and hurt a call seller; one number cannot mean both."""
    puts = _side("put", {0.20: 0.34, 0.30: 0.31, 0.40: 0.29})
    calls = _side("call", {0.20: 0.26, 0.30: 0.26, 0.40: 0.27})
    bp = get_skew_depth(puts, calls, 100.0, "bull_put")["strategy_premium"]
    bc = get_skew_depth(puts, calls, 100.0, "bear_call")["strategy_premium"]
    assert bp > 0 > bc


def test_output_shape_is_stable():
    r = get_skew_depth([], [], 100.0)
    for k in ("skew_20d", "skew_30d", "skew_40d", "skew_steepness",
              "strategy_premium", "confidence"):
        assert k in r


# ── Edge-score modifier ───────────────────────────────────────────────────────────────────────

def _score(**over):
    from analysis.edge_calculator import calculate_edge_score
    base = dict(ticker="T", strategy="bull_put_spread", technical_score=70,
                vrp_pct=5, edge_points=8, news_sentiment="NEUTRAL",
                earnings_days_away=99)
    base.update(over)
    return calculate_edge_score(**base)


@pytest.mark.parametrize("slope,expected", [
    ("upward", 5), ("flat", 2), ("downward", -5), ("event_spike", -8), ("unknown", 0),
])
def test_term_slope_moves_the_score(slope, expected):
    assert _score(term_slope=slope)["component_breakdown"]["term_structure"] == expected


def test_term_structure_is_passed_as_a_keyword_not_a_trade_dict():
    """calculate_edge_score takes explicit scalars and has no `trade` parameter. The brief's
    `trade.get("term_slope")` inside this function would have raised NameError on the first
    scored trade."""
    import inspect

    from analysis.edge_calculator import calculate_edge_score
    params = inspect.signature(calculate_edge_score).parameters
    assert "term_slope" in params and "event_expiry_flag" in params
    assert "trade" not in params


def test_event_inside_the_window_penalises_an_otherwise_benign_slope():
    assert _score(term_slope="flat", event_expiry_flag=True)[
        "component_breakdown"]["term_structure"] == -4


def test_event_and_spike_do_not_stack_into_a_double_penalty():
    """When the slope IS the spike, the two describe one observation."""
    assert _score(term_slope="event_spike", event_expiry_flag=True)[
        "component_breakdown"]["term_structure"] == -8


def test_no_slope_is_neutral():
    assert _score(term_slope=None)["component_breakdown"]["term_structure"] == 0


def test_disabling_term_structure_zeroes_the_component(monkeypatch):
    monkeypatch.setattr(config, "TERM_STRUCTURE_ENABLED", False, raising=False)
    assert _score(term_slope="event_spike")["component_breakdown"]["term_structure"] == 0


def test_event_spike_never_disqualifies():
    """Advisory during calibration — the heaviest penalty must still leave the trade eligible
    to be judged on everything else."""
    r = _score(term_slope="event_spike", event_expiry_flag=True)
    assert "term" not in (r.get("disqualification_reason") or "").lower()


def test_term_structure_is_a_bonus_component_not_a_base_one():
    """It sits outside the 0-100 base like skew and post-earnings, so it adjusts the final
    score rather than diluting the weights of the components it sits beside."""
    up = _score(term_slope="upward")
    flat = _score(term_slope="flat")
    assert up["base_score"] == flat["base_score"]
    assert up["total_score"] > flat["total_score"]
