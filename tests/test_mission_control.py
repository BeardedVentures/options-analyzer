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


@pytest.fixture
def seeded_measurement_ledgers():
    """Give the Track Record panels their own data instead of the live ledgers'.

    These five tests used to render against logs/vega_predictions.jsonl and
    logs/vega_counterfactuals.jsonl as they happened to exist on the developer's machine. They
    passed because production data happened to contain enough resolved claims and matured
    counterfactuals to populate the panels — not because anything asserted it. That is a test
    that reports on the operator's disk, and it would have started failing the day the ledger
    was rotated, cleaned, or read on another machine.

    Seeds deliberately varied probabilities (0.55–0.92) so the forecast-spread ceiling has a
    real standard deviation to report, and one sole-failed-gate counterfactual per named gate so
    the value-of-information table has rows.
    """
    import json
    import config
    from analysis import counterfactuals as cf
    from analysis import predictions as pred

    claims = []
    for i, (p, correct) in enumerate(
            [(0.92, True), (0.85, True), (0.78, False), (0.66, True), (0.55, False)]):
        claims.append({
            "id": f"T{i}::strike_holds", "trade_id": f"T{i}", "ticker": f"TK{i}",
            "claim_type": "strike_holds", "claim": "x", "probability": p,
            "made_at": "2026-08-01T10:00:00", "resolves_on": "2026-08-10",
            "context": {"short_strike": 100.0}, "status": "resolved",
            "correct": correct, "resolved_at": "2026-08-10T16:00:00",
            "resolution_note": "seeded",
        })
    pred.PREDICTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    pred.PREDICTIONS_FILE.write_text(
        "\n".join(json.dumps(c) for c in claims) + "\n", encoding="utf-8")

    gates = list(getattr(config, "REQUIRED_GATES", ()))
    recs = []
    for i in range(6):                      # qualified baseline, some touched
        recs.append({"key": f"Q{i}", "ticker": f"Q{i}", "gates": {g: True for g in gates},
                     "failed_gates": [], "qualified": True, "sole_failed_gate": None,
                     "touched": i < 2, "horizon_complete": True, "horizon_days": 10})
    for gate in gates:                      # one blocked cohort per gate
        for i in range(6):
            recs.append({"key": f"{gate}{i}", "ticker": f"B{i}",
                         "gates": {g: (g != gate) for g in gates},
                         "failed_gates": [gate], "qualified": False,
                         "sole_failed_gate": gate, "touched": i < 4,
                         "horizon_complete": True, "horizon_days": 10})
    cf.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    cf.LEDGER.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return claims, recs


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

def test_system_status_aggregates_every_readout():
    """"Data quality" split in two on 2026-08-08. It had always measured how OLD the board was,
    which is a different question from how much of the chain was actually quotable — a board can
    be seconds fresh and built on a chain that was 20% there."""
    b = _board([_t()])
    t = _txt(vega_app._mc_system_status(b, b["trades"], "PROVISIONAL"))
    for lab in ("Hard gates", "Chain quality", "Board freshness",
                "Figures reconciled", "Confidence"):
        assert lab in t
    assert "Data quality" not in t


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


def test_every_row_expands_and_the_top_rows_carry_the_full_analysis():
    """Every row must still open — the toggle is how the board is read. But a full drawer is a
    complete analysis card, and a fast-scan board carries 150+ rows: at 154 the page rendered
    2.05 MB. The top MAX_DRAWERS get the real thing and the rest get a pointer, so the page
    stays openable without any row becoming unreachable."""
    h = vega_app.render("today")
    rows = h.count('class="vmain"')
    if rows:
        expected = min(rows, vega_app.MAX_DRAWERS)
        assert h.count('class="vdetail"') == rows, "a row with no drawer cannot be expanded"
        assert h.count("VEGA recommendation") == expected
        assert h.count("Why VEGA likes this trade") == expected
        # "Setup", not "action": VEGA hands the operator a trade to construct, it does not
        # issue an instruction. The distinction is the whole decision-support framing.
        assert h.count("Recommended setup") == expected


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


# ── Measurement panels on Track Record (2026-08-10) ───────────────────────────────────────────
#
# The measurement work shipped this session had no UI at all. The only grading panel in the app
# filtered claims to the BTC forecast cohort, so the 30 claims the TRADE engine has written were
# invisible: made, stored, and shown nowhere. Counterfactuals had zero references.

def test_track_grades_trade_claims_not_only_btc_ones(seeded_measurement_ledgers):
    txt = _txt(vega_app.view_track())
    assert "Forecast ledger" in txt
    assert "Resolution" in txt, "the column raw Brier cannot substitute for must be shown"


def test_track_shows_whether_the_gates_earn_their_place(seeded_measurement_ledgers):
    txt = _txt(vega_app.view_track())
    assert "Gate value" in txt
    for gate in ("earnings clear", "support shelter", "liquidity"):
        assert gate in txt, f"{gate} missing from the value-of-information table"


def test_an_unresolved_ledger_says_so_rather_than_showing_an_empty_table(seeded_measurement_ledgers):
    """An empty table under a live heading reads as 'measured, found nothing'. It is the
    opposite — nothing has come due yet, and the two are not the same claim."""
    txt = _txt(vega_app.view_track())
    assert "Nothing has resolved yet" in txt or "Correct" in txt


def test_the_gate_panel_refuses_to_report_before_the_horizon_closes(seeded_measurement_ledgers):
    """Judging spreads that have not lived the full window would report that the gates avoid
    nothing, when what actually happened is that nothing has had time to happen."""
    txt = _txt(vega_app.view_track())
    assert "Not yet measurable" in txt or "Baseline" in txt


def test_the_forecast_spread_ceiling_is_surfaced(seeded_measurement_ledgers):
    """Resolution is capped by how much the forecasts vary. A ledger whose claims all sit
    between 0.70 and 0.85 cannot demonstrate discrimination however right it is — that is a
    fact about the engine, not the sample size, and waiting will not fix it."""
    txt = _txt(vega_app.view_track())
    assert "Forecast spread" in txt


def test_the_panels_degrade_instead_of_taking_the_page_down(monkeypatch):
    """A measurement panel is advisory. Neither may be able to break Track Record."""
    import analysis.counterfactuals as cf

    def boom(*a, **k):
        raise RuntimeError("ledger gone")

    monkeypatch.setattr(cf, "value_of_information", boom)
    html = vega_app.view_track()
    assert "Track Record" in _txt(html)
    assert "unavailable" in _txt(html).lower()


@pytest.mark.parametrize("view", vega_app.VIEWS)
def test_every_view_still_renders(view):
    html = vega_app.render(view)
    assert "<html" in html and len(html) > 500


# ── Brief merged into Today · Bitcoin made a board (2026-08-10) ───────────────────────────────

def test_brief_is_gone_and_old_links_still_work():
    """Brief rendered the SAME trades from the SAME artifact as Today and said so in its own
    intro. A tab that answers no question no other tab answers is a layout, not a section — and
    a bookmark to it must not 404."""
    assert "brief" not in vega_app.VIEWS
    assert not hasattr(vega_app, "view_brief")
    assert "<html" in vega_app.render("brief")      # falls back to Today


def test_the_order_tickets_survived_the_merge():
    """The one thing Brief genuinely added was the executable ticket and the position size for
    the current tier. Dropping the tab must not drop those."""
    txt = _txt(vega_app.render("today"))
    assert "Order tickets" in txt


def test_bitcoin_leads_with_something_tradeable():
    """The page about Bitcoin contained no Bitcoin trade you could take, while IBIT was passing
    all eleven gates in the same scan the rest of the app was reading."""
    html = vega_app.view_bitcoin()
    txt = _txt(html)
    assert "Tradeable now" in txt
    assert txt.index("Tradeable now") < txt.index("Why"), "trades come before the research"


def test_bitcoin_still_shows_the_volatility_read_underneath():
    """The research is the REASON a trade here might be worth taking. It becomes context for
    the table, never a substitute for it."""
    txt = _txt(vega_app.view_bitcoin())
    for term in ("DVOL", "Variance premium", "Cross-venue"):
        assert term in txt


def test_the_btc_board_reads_the_same_snapshot_the_robot_trades_from():
    """A board showing one engine's output while the desk opens from another is the two-engine
    divergence this codebase has fought four enforcement leaks over."""
    import inspect
    src = inspect.getsource(vega_app._tradeable_block)
    assert "_latest_candidates" in src
    assert "natural_credit" in src, "credit must be the fillable basis, not the mid"


def test_the_btc_board_degrades_without_a_snapshot(monkeypatch):
    monkeypatch.setattr(vega_app, "_latest_candidates", lambda: (None, None))
    assert "No candidate snapshot" in _txt(vega_app._tradeable_block("IBIT"))


def test_a_modelled_after_hours_price_is_never_shown_as_fillable():
    """The whole point of pricing on the natural basis is that the board never quotes a price
    its reader cannot get — and after the close nobody can get any of them.

    Asserted against the real render rather than a hand-built board: a synthetic trade dict
    that drifts from what main.py emits would pass while the live page said nothing.
    """
    import inspect
    src = inspect.getsource(vega_app.view_today)
    assert 'fill_basis") == "modelled"' in src, "the board must detect a modelled credit"
    assert "Markets closed" in src and "indicative" in src

    from analysis.assessment import quotes_are_live
    html = vega_app.render("today")
    shown = "Markets closed" in html
    trades = vega_app.load_board().get("trades") or []
    modelled = any(t.get("fill_basis") == "modelled" for t in trades)
    assert shown == modelled, (
        "the banner must appear exactly when a modelled credit is on the board "
        f"(shown={shown}, modelled={modelled}, live={quotes_are_live()})")


def test_the_adapter_carries_the_fill_basis_through():
    """Dropping it here would let a modelled price reach the board looking exactly like a
    fillable one."""
    import inspect
    assert '"fill_basis"' in inspect.getsource(vega_app._adapt_engine)
