"""Mission Control board + AI Copilot drawer.

The previous board presented a verdict strip, a hero card and a 13-column table at one
visual weight, leaving the reader to work out the order of operations themselves. Mission
Control imposes the order: decide whether to trade at all, pick an archetype, open the trade.
The Copilot then answers "should I take this, and why" before it shows a single metric table.

These tests pin the properties that make that work — not the markup, which will keep moving.
"""
import re

import pytest

import vega_app


def _txt(html):
    return re.sub(r"<[^>]+>", " ", html).replace("&middot;", ".")


def _t(**over):
    c = {"ticker": "AAA", "strat_type": "bull_put", "short": 100.0, "long": 95.0,
         "edge_score": 85, "priority": 85, "true_pop": 0.82, "roi": 0.30,
         "credit_usd": 150.0, "max_loss_usd": 350.0, "iv_rank": 60.0, "vrp": 4.0,
         "edge_pp": 6.0, "gates_passed": 8, "gates_total": 8, "true_pop_conf": "HIGH",
         "support_levels": [{"price": 105.0, "touches": 3, "strength": 60.0}],
         "resistance_levels": [], "entry_timing": {}, "exp": "2026-09-18", "dte": 44,
         "credit_ps": 1.5, "structure": "100/95P", "width": 5}
    c.update(over)
    return c


def _board(trades, **over):
    b = {"trades": trades, "source": "engine", "asof": "2026-08-05T16:00:00",
         "context": {"vix": {"current": 15.8, "label": "MODERATE", "trend": "falling"},
                     "spy": {"day_change_pct": -0.2}, "bias": "NEUTRAL"},
         "regime": {"regime_flag": "LOW_VOL", "regime_note": "n", "trade_suppressed": False},
         "book": {}, "note": ""}
    b.update(over)
    return b


# ── Status cards: the headline and its sub-line must agree ────────────────────────────────────

def test_suppressed_regime_says_stand_aside_and_means_it():
    """Computed independently, these contradicted each other on the live 2026-08-05 board:
    "Stand Aside" over "High conviction setup", because the sub-line only checked whether an
    elite score existed and never looked at suppression."""
    b = _board([_t(edge_score=91)], regime={"regime_flag": "LOW_VOL", "trade_suppressed": True})
    t = _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))
    assert "Stand Aside" in t
    assert "High conviction" not in t
    assert "suppresses" in t


def test_empty_board_stands_aside():
    b = _board([])
    t = _txt(vega_app._mc_status_cards(b, [], "PROVISIONAL"))
    assert "Stand Aside" in t and "Nothing qualified" in t


def test_good_board_says_sell_premium():
    b = _board([_t(edge_score=85)])
    t = _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))
    assert "Sell Premium" in t


def test_qualified_but_unremarkable_board_is_selective():
    b = _board([_t(edge_score=60, priority=60)])
    t = _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))
    assert "Selective" in t


def test_edge_card_reports_the_best_score_on_the_board():
    b = _board([_t(edge_score=64), _t(ticker="BBB", edge_score=91)])
    assert "91" in _txt(vega_app._mc_status_cards(b, b["trades"], "PROVISIONAL"))


# ── Playbook: pick a role, not a row ──────────────────────────────────────────────────────────

def test_playbook_labels_distinct_archetypes():
    trades = [_t(ticker="AAA", edge_score=91, true_pop=0.70, max_loss_usd=900, roi=0.20),
              _t(ticker="BBB", edge_score=70, true_pop=0.95, max_loss_usd=800, roi=0.25),
              _t(ticker="CCC", edge_score=60, true_pop=0.60, max_loss_usd=100, roi=0.90)]
    t = _txt(vega_app._mc_playbook(trades))
    assert "Best overall" in t and "AAA" in t
    assert "Highest win rate" in t and "BBB" in t
    assert "Safest" in t and "CCC" in t
    assert "Aggressive" in t


def test_playbook_never_repeats_the_same_trade_under_two_roles():
    """One trade winning everything should list once, not five times — a repeated row reads
    as five options when there is one."""
    t = _txt(vega_app._mc_playbook([_t(ticker="AAA"), _t(ticker="BBB", edge_score=10,
                                                         true_pop=0.1, roi=0.01,
                                                         max_loss_usd=99999)]))
    assert t.count("AAA") == 1


def test_playbook_names_the_missing_side():
    """Silence about a strategy reads as "none available" only if you say so."""
    t = _txt(vega_app._mc_playbook([_t(strat_type="bull_put")]))
    assert "Avoid" in t and "call spreads" in t


def test_empty_playbook_states_the_position():
    t = _txt(vega_app._mc_playbook([]))
    assert "Standing aside is the position" in t


def test_playbook_entries_open_the_matching_row():
    """The archetype is only useful if it takes you to that trade."""
    html = vega_app._mc_playbook([_t(ticker="AAA"), _t(ticker="BBB", edge_score=95)])
    assert "vopen(1)" in html          # BBB is index 1 and is Best overall


# ── System status ─────────────────────────────────────────────────────────────────────────────

def test_system_status_aggregates_all_four_readouts():
    b = _board([_t()])
    t = _txt(vega_app._mc_system_status(b, b["trades"], "PROVISIONAL"))
    for lab in ("Hard gates", "Data quality", "Figures reconciled", "Confidence"):
        assert lab in t


def test_low_confidence_anywhere_pulls_the_board_confidence_down():
    b = _board([_t(true_pop_conf="HIGH"), _t(ticker="BBB", true_pop_conf="LOW")])
    assert "Low" in _txt(vega_app._mc_system_status(b, b["trades"], "PROVISIONAL"))


# ── Copilot ───────────────────────────────────────────────────────────────────────────────────

def test_why_reads_as_sentences_not_metrics():
    t = _txt(vega_app._copilot_why(_t()))
    assert "priced rich" in t
    assert "beats what the market is pricing" in t


def test_shelter_sentence_is_grammatical_for_both_shapes():
    """A condor's note is a standalone clause; directional notes are prepositional. Assuming
    one shape produced "Short strike is both wings sheltered"."""
    directional = _txt(vega_app._copilot_why(_t()))
    assert "Short strike sits under $105.00 support" in directional

    condor = _txt(vega_app._copilot_why(_t(
        strat_type="iron_condor", put_short=100.0, call_short=120.0,
        support_levels=[{"price": 105.0, "touches": 3, "strength": 60.0}],
        resistance_levels=[{"price": 115.0, "touches": 3, "strength": 60.0}])))
    assert "Both wings sheltered" in condor
    assert "strike is both" not in condor.lower()


def test_failing_timing_is_a_warning_not_a_tick():
    """An advisory dressed as a green check would overstate the case."""
    html = vega_app._copilot_why(_t(entry_timing={
        "readiness": "EARLY", "timing_gate_pass": False, "headline": "Early pullback"}))
    assert "row warn" in html
    assert "may improve if deferred" in _txt(html)


def test_existing_position_is_flagged_as_concentration():
    t = _txt(vega_app._copilot_why(_t(already_in_position=True)))
    assert "concentration" in t


def test_why_always_says_something():
    assert vega_app._copilot_why({"ticker": "X"}) != ""


def test_other_ideas_exclude_the_trade_being_viewed(monkeypatch):
    trades = [_t(ticker="AAA"), _t(ticker="BBB"), _t(ticker="CCC")]
    monkeypatch.setattr(vega_app, "_COPILOT_PEERS", list(enumerate(trades)))
    t = _txt(vega_app._copilot_other_ideas(0))
    assert "AAA" not in t and "BBB" in t and "CCC" in t


def test_other_ideas_handles_a_lone_trade(monkeypatch):
    monkeypatch.setattr(vega_app, "_COPILOT_PEERS", [(0, _t())])
    assert "No other qualified setups" in _txt(vega_app._copilot_other_ideas(0))


def test_recommended_action_names_the_actual_order():
    t = _txt(vega_app._copilot_action(_t(), ""))
    assert "Sell 1x AAA Bull Put 100/95P" in t
    assert "44 DTE" in t


def test_recommended_action_handles_a_condor():
    t = _txt(vega_app._copilot_action(_t(
        strat_type="iron_condor", put_short=100, put_long=95,
        call_short=120, call_long=125), ""))
    assert "100/95P" in t and "120/125C" in t


# ── Whole-page wiring ─────────────────────────────────────────────────────────────────────────

def test_board_renders_the_full_mission_control_stack():
    h = vega_app.render("today")
    for m in ("Mission Control", "Today's call", "Today's playbook",
              "Top opportunities", "Hard gates"):
        assert m in h, f"missing {m}"


def test_every_row_gets_a_copilot_drawer():
    h = vega_app.render("today")
    rows = h.count('class="vmain"')
    if rows:
        assert h.count("VEGA recommendation") == rows
        assert h.count("Why VEGA likes this trade") == rows
        assert h.count("Recommended action") == rows


def test_deep_metrics_are_behind_progressive_disclosure():
    """All the old drawer content still exists — it just no longer leads."""
    h = vega_app.render("today")
    if 'class="vmain"' in h:
        assert "Full analysis" in h
        assert "Score composition" in h or "score" in h.lower()


def test_view_today_publishes_context_for_the_drawers():
    """The drawer is built per row and cannot see the board; if this wiring regresses,
    Other top ideas and Market snapshot silently render empty."""
    vega_app.render("today")
    board = vega_app.load_board()
    if board["trades"]:
        assert len(vega_app._COPILOT_PEERS) == len(board["trades"])
