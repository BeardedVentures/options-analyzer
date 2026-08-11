"""Opportunity density, the Today's-call ladder, and pattern direction.

Three things the board could not previously say, all of them about CONTEXT rather than about
any single setup:

1. How much was looked at. Five rows read identically whether they are the best five of two
   thousand structures or the only five a thin session could build, and without the
   denominator neither an edge score nor a "Stand Aside" can be interpreted at all.

2. Whether "Stand Aside" means "take nothing today" or "conditions are mediocre". It was
   carrying both, which is why it appeared over a board that was simultaneously recommending
   a trade — the operator reads that as a contradiction, and a status word that contradicts
   the page under it trains them to ignore the word.

3. Whether the chart agrees with the trade. A bull put under a chart making lower highs is a
   real conflict, and nothing surfaced it.

The pattern-direction tests deliberately assert against the vocabulary `structure._classify`
can actually emit. A map keyed on textbook names no detector produces ('cup and handle',
'ascending triangle') passes its own unit tests and returns NEUTRAL for every live setup,
which is the "0.000 error" failure: a metric that cannot come out non-zero.
"""
import pytest

import main
import vega_app
from analysis import structure as S


# ── Density funnel ────────────────────────────────────────────────────────────────────────────

def _tech(n):
    return {"structures_considered": n}


def test_scan_summary_emits_all_four_counts():
    s = main.build_scan_summary(
        ["AAA", "BBB"], {"AAA": _tech(100), "BBB": _tech(40)},
        [{"edge_score": 92}, {"edge_score": 70}, {"edge_score": 30}])
    for k in ("total_scanned", "total_qualified", "high_edge_count", "exceptional_count"):
        assert k in s, f"missing {k}"
    assert s["total_scanned"] == 140
    assert s["total_qualified"] == 3
    assert s["high_edge_count"] == 2      # 92 and 70 clear 65
    assert s["exceptional_count"] == 1    # only 92 clears 80


def test_structures_counted_are_pairs_not_tickers():
    """The funnel's headline number is the one most easily faked. Two tickers that enumerated
    140 structures between them must not report 2."""
    s = main.build_scan_summary(["AAA", "BBB"], {"AAA": _tech(100), "BBB": _tech(40)}, [])
    assert s["total_scanned"] == 140
    assert s["tickers_scanned"] == 2


def test_a_ticker_that_enumerated_nothing_contributes_nothing():
    s = main.build_scan_summary(["AAA", "BBB"], {"AAA": _tech(12)}, [])
    assert s["total_scanned"] == 12


def test_density_bar_renders_nothing_without_engine_counts():
    """A board written before scan_summary existed, or a fast rescan, has no denominator.
    Inferring one from the row count would state a total the engine never measured."""
    assert vega_app._mc_density_bar({"scan_summary": {}}, [{"edge_score": 80}]) == ""
    assert vega_app._mc_density_bar({}, []) == ""


def test_density_bar_shows_the_engine_counts_when_present():
    h = vega_app._mc_density_bar(
        {"scan_summary": {"total_scanned": 1842, "total_qualified": 27,
                          "high_edge_count": 6, "exceptional_count": 1,
                          "tickers_scanned": 56}}, [])
    assert "1,842" in h and "27" in h and "structures scanned" in h


# ── Today's call ladder ───────────────────────────────────────────────────────────────────────

def _t(**over):
    c = {"ticker": "AAA", "edge_score": 60, "priority": 60, "true_pop": 0.8, "vrp": 3.0}
    c.update(over)
    return c


def _board(trades, **over):
    b = {"trades": trades, "context": {"vix": {"current": 15.0, "trend": "flat"}},
         "regime": {"regime_flag": "NORMAL", "trade_suppressed": False}}
    b.update(over)
    return b


def _txt(h):
    import re
    return re.sub(r"<[^>]+>", " ", h).replace("&middot;", ".")


def test_stand_aside_is_reserved_for_boards_with_nothing_on_them():
    """The contradiction this ladder exists to remove: the words "Stand Aside" over a page
    that is recommending a trade below them."""
    b = _board([_t() for _ in range(6)])
    assert "Stand Aside" not in _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))


def test_a_deep_but_unremarkable_board_is_cautious_not_selective():
    b = _board([_t(edge_score=70, priority=70) for _ in range(6)])
    t = _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))
    assert "Cautious" in t


def test_a_thin_board_is_still_selective():
    b = _board([_t(edge_score=60, priority=60)])
    assert "Selective" in _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))


def test_premium_card_shows_the_vrp_behind_the_word_thin():
    b = _board([_t(vrp=1.8)])
    assert "VRP +1.8pp" in _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))


def test_kpi_cards_carry_a_visible_legend():
    b = _board([_t()])
    t = _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))
    assert "Score guide" in t and "Favorable" in t


# ── Pattern direction ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,expected", [
    (S.BULL_FLAG, S.BULLISH), (S.DOUBLE_BOTTOM, S.BULLISH),
    (S.UPTREND_EXTENDED, S.BULLISH), (S.BEAR_FLAG, S.BEARISH),
    (S.DOUBLE_TOP, S.BEARISH), (S.DOWNTREND, S.BEARISH),
    (S.RANGE, S.NEUTRAL), (S.PULLBACK, S.NEUTRAL),
])
def test_every_emittable_pattern_has_a_direction(label, expected):
    assert S.get_pattern_direction(label) == expected


def test_the_map_covers_exactly_what_the_detector_can_emit():
    """The guard against a map of textbook names that never matches a live reading."""
    emittable = {S.BULL_FLAG, S.BEAR_FLAG, S.DOUBLE_TOP, S.DOUBLE_BOTTOM, S.RANGE,
                 S.UPTREND_EXTENDED, S.DOWNTREND, S.PULLBACK, S.UNREADABLE}
    assert set(S.PATTERN_DIRECTION) == emittable


def test_direction_reads_the_rendered_phrase_too():
    """The UI carries the phrase, the candidate carries the label; both call this."""
    assert S.get_pattern_direction("second trough of a double bottom") == S.BULLISH
    assert S.get_pattern_direction("second peak of a double top") == S.BEARISH
    assert S.get_pattern_direction("early in a bull flag") == S.BULLISH
    assert S.get_pattern_direction("in a downtrend") == S.BEARISH


def test_unreadable_is_unknown_not_neutral():
    """Collapsing these would let a data gap silently clear the contradiction check."""
    assert S.get_pattern_direction(S.UNREADABLE) is None
    assert S.get_pattern_direction("cup and handle") is None
    assert S.get_pattern_direction(None) is None
    assert S.PATTERN_DIRECTION[S.RANGE] == S.NEUTRAL


# ── Thesis contradiction ──────────────────────────────────────────────────────────────────────

def test_a_bullish_chart_contradicts_a_bear_call():
    assert S.check_thesis_contradiction(S.BULLISH, "bear_call_spread") is not None


def test_a_bullish_chart_does_not_contradict_a_bull_put():
    assert S.check_thesis_contradiction(S.BULLISH, "bull_put_spread") is None


def test_a_bearish_chart_contradicts_a_bull_put():
    assert S.check_thesis_contradiction(S.BEARISH, "bull_put_spread") is not None


def test_a_condor_contradicts_nothing():
    """It is hurt by a move either way, so no directional read conflicts with it."""
    assert S.check_thesis_contradiction(S.BULLISH, "iron_condor") is None
    assert S.check_thesis_contradiction(S.BEARISH, "iron_condor") is None


@pytest.mark.parametrize("pat,strat", [
    (None, "bull_put_spread"), (S.NEUTRAL, "bull_put_spread"),
    (S.BULLISH, None), (S.BULLISH, "some_strategy_we_do_not_know"),
])
def test_the_check_fails_open_on_anything_it_cannot_read(pat, strat):
    """A warning invented from missing data teaches the operator to dismiss warnings, which
    costs more than the one it would have caught."""
    assert S.check_thesis_contradiction(pat, strat) is None
