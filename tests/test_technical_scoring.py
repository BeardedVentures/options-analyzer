"""Composite technical score and its delivery into edge scoring.

Two bugs found by audit on 2026-08-05, both live and both silent:

1. `strike_above_support` paid 20 points for the strike sitting BETWEEN spot and support —
   the exposed geometry — and 0 for the sheltered one. Compounding it, the old micro-low
   levels meant the strike was essentially always below `nearest_support`, so the branch paid
   a flat 0 to every bull put: 15 worse than the no-data neutral, a uniform drag carrying no
   information.

2. multi_strategy read `technical_score` from the technicals dict, which emits
   `composite_score`. It silently fell back to a hardcoded 50, so chart quality never reached
   bear call or iron condor edge scores at all.
"""
import pytest

from data.technicals import _composite_score


def _score(short_strike, nearest_support=95.0):
    return _composite_score(
        price=100.0, sma20=98, sma50=95, sma200=90, rsi=50, macd_hist=0.5,
        bb_lower=95, iv_rank=60, nearest_support=nearest_support,
        short_strike=short_strike,
    )["breakdown"]["strike_sheltered_by_support"]


# ── The inversion ─────────────────────────────────────────────────────────────────────────────

def test_sheltered_strike_outscores_exposed_strike():
    """Support ABOVE the short put is protective: price must break a defended level before
    the strike is threatened. Support BELOW it means nothing stands in the way."""
    sheltered = _score(85.0)      # strike well below support at 95
    exposed = _score(97.0)        # strike between spot and support
    assert sheltered > exposed
    assert exposed == 0


def test_exposed_geometry_scores_below_the_no_data_neutral():
    """Being able to see that a strike is unprotected should be worse than not knowing."""
    assert _score(97.0) < _score(None)


def test_deeper_cushion_scores_higher():
    """Partial credit is what makes the component discriminate; a flat pass/fail paid the
    same 0 to every trade once levels moved away from spot."""
    assert _score(85.0) > _score(92.0) > _score(94.9)


def test_score_is_capped_at_twenty():
    assert _score(10.0) == 20


def test_missing_data_is_neutral_not_zero():
    assert _score(None) == 15
    assert _score(90.0, nearest_support=None) == 15


def test_zero_strike_does_not_divide_by_zero():
    assert _score(0.0) == 15


# ── Delivery into edge scoring ────────────────────────────────────────────────────────────────

def test_technicals_emits_composite_score():
    """The key name the rest of the system must agree on."""
    import inspect

    from data import technicals
    src = inspect.getsource(technicals.calculate_all)
    assert '"composite_score"' in src


def test_multi_strategy_reads_the_key_technicals_actually_emits():
    """Guards the silent fallback: reading a non-existent key meant every bear call and
    condor scored an identical hardcoded 50 on the technical component."""
    import inspect

    import multi_strategy
    src = inspect.getsource(multi_strategy._edge_score)
    assert "composite_score" in src, "call-side edge score is not reading composite_score"


def test_call_side_technical_score_varies_with_chart_quality():
    """End-to-end: two different composite scores must produce two different edge inputs."""
    import multi_strategy

    captured = []

    class _Spy:
        @staticmethod
        def calculate_edge_points(*a, **k):
            return {"edge_points": 5}

        @staticmethod
        def calculate_edge_score(**kwargs):
            captured.append(kwargs["technical_score"])
            return {"total_score": 60, "component_breakdown": {}}

    original = multi_strategy.edge_calculator
    multi_strategy.edge_calculator = _Spy
    try:
        for score in (30, 85):
            multi_strategy._edge_score("TEST", "bear_call_spread", {"composite_score": score},
                                       10, 0.8, 0.75, "NEUTRAL", 99)
    finally:
        multi_strategy.edge_calculator = original

    assert captured == [30, 85], f"technical score not flowing through: {captured}"
