"""The one-line thesis under each board row.

The board slimmed from 12 columns to 8 and gained a thesis line so the "why" is legible
without opening the row. That line was originally built from VRP, edge and a raw cushion
percentage — none of which say whether the market has ever defended the price beneath the
short strike. A 9% cushion to nothing is a weaker trade than a 4% cushion under a level
bought three times, and the row had no way to show the difference.

It now leads with what changes a decision: a timing warning (the only reason on the line NOT
to act), then the structural shelter, then premium richness.
"""
import re

import pytest

import vega_app


def _text(html):
    return re.sub(r"<[^>]+>", "", html).replace("&middot;", "·").strip()


def _lvl(price, touches=3, strength=60.0):
    return {"price": float(price), "touches": touches, "strength": strength,
            "last_touch_bars_ago": 10, "flipped": False}


def _card(**over):
    c = {"ticker": "TEST", "strat_type": "bull_put", "short": 100.0,
         "support_levels": [_lvl(105)], "resistance_levels": [],
         "vrp": 4.5, "edge_pp": 3.0, "cushion_pct": 9.0,
         "true_pop": 0.83, "roi": 0.30, "iv_rank": 60.0, "entry_timing": {}}
    c.update(over)
    return c


# ── Shelter ───────────────────────────────────────────────────────────────────────────────────

def test_bull_put_names_the_level_above_the_short_strike():
    note = vega_app._shelter_note(_card())
    assert "under $105.00 support" in note
    assert "3x" in note


def test_bear_call_names_the_level_below_the_short_call():
    note = vega_app._shelter_note(_card(strat_type="bear_call", short=100.0,
                                        support_levels=[], resistance_levels=[_lvl(95)]))
    assert "over $95.00 resistance" in note


def test_unsheltered_strike_says_nothing():
    """A strike above every support has no shield; inventing one would be worse than silence."""
    assert vega_app._shelter_note(_card(short=110.0)) == ""


def test_illusory_shelter_is_rejected():
    """Same min-buffer rule as strike selection: a level 0.1% off the strike shields nothing."""
    assert vega_app._shelter_note(_card(short=100.0, support_levels=[_lvl(100.1)])) == ""


def test_missing_levels_are_survivable():
    assert vega_app._shelter_note(_card(support_levels=None)) == ""
    assert vega_app._shelter_note(_card(short=None)) == ""


# ── Condor has two wings ──────────────────────────────────────────────────────────────────────

def _condor(**over):
    c = {"ticker": "TEST", "strat_type": "iron_condor", "put_short": 100.0, "call_short": 120.0,
         "support_levels": [_lvl(105)], "resistance_levels": [_lvl(115)], "entry_timing": {}}
    c.update(over)
    return c


def test_condor_reports_both_wings_covered():
    assert vega_app._shelter_note(_condor()) == "both wings sheltered"


def test_condor_names_the_open_wing():
    """Reporting only the put wing is a half-truth: an unprotected call wing is the side that
    gets tested first in a rally, and the old single-sided read hid it."""
    put_only = vega_app._shelter_note(_condor(resistance_levels=[]))
    assert "call wing open" in put_only and "put wing under $105.00" in put_only

    call_only = vega_app._shelter_note(_condor(support_levels=[]))
    assert "put wing open" in call_only and "call wing over $115.00" in call_only


def test_condor_with_neither_wing_sheltered_is_silent():
    assert vega_app._shelter_note(_condor(support_levels=[], resistance_levels=[])) == ""


# ── Ordering and composition ──────────────────────────────────────────────────────────────────

def test_timing_warning_leads_when_timing_disagrees():
    """The only reason on this line NOT to act belongs first. The metric-only ordering led
    with VRP even when entry timing said the pullback had barely started."""
    t = _text(vega_app._row_thesis(_card(entry_timing={
        "readiness": "EARLY", "timing_gate_pass": False})))
    assert t.startswith("Early timing")


def test_clean_timing_spends_no_slot_saying_nothing_is_wrong():
    t = _text(vega_app._row_thesis(_card(entry_timing={
        "readiness": "OPTIMAL", "timing_gate_pass": True})))
    assert "timing" not in t.lower()
    assert t.startswith("under $105.00 support")


def test_shelter_replaces_the_bare_cushion_number():
    """A raw cushion percentage is what the line said before it could name a level."""
    t = _text(vega_app._row_thesis(_card()))
    assert "support" in t
    assert "cushion" not in t


def test_bare_cushion_still_shows_when_no_level_anchors_it():
    t = _text(vega_app._row_thesis(_card(short=110.0, vrp=None, edge_pp=None)))
    assert "9% cushion" in t


def test_line_is_capped_at_three_parts():
    t = _text(vega_app._row_thesis(_card(entry_timing={
        "readiness": "EARLY", "timing_gate_pass": False})))
    assert len(t.split("·")) <= 3


def test_falls_back_through_pop_roc_ivr():
    bare = _card(short=110.0, support_levels=[], vrp=None, edge_pp=None, cushion_pct=None)
    assert "true POP" in _text(vega_app._row_thesis(bare))
    assert "ROC" in _text(vega_app._row_thesis({**bare, "true_pop": None}))
    assert "IV rank" in _text(vega_app._row_thesis({**bare, "true_pop": None, "roi": None}))


def test_empty_card_renders_nothing_rather_than_an_empty_box():
    assert vega_app._row_thesis({"ticker": "X", "strat_type": "bull_put"}) == ""


def test_thesis_reaches_the_rendered_board():
    """Guards the wiring: the helper can be perfect and still never appear on the page."""
    import inspect
    assert "_row_thesis(" in inspect.getsource(vega_app.board_table)
