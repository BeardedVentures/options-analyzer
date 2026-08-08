"""Ravens alerts and the chain-quality tile in the cockpit (P1-2, P1-6).

The build plan specified these cards keyed on `thesis_status`, with a red WOLF card, and
stated that HOLD_TENSION and MUNINN_BLIND "do not exist anywhere in the codebase". Both halves
are backwards, and the first test below is the guard against re-introducing that reading.

WOLF resolves to WOLF_CLOSE in odin.synthesize and auto_paper_cycle closes the position inside
the same cycle, so a WOLF card on an OPEN position is unreachable by construction. The
recommendations that persist on an open position are exactly HOLD_TENSION and MUNINN_BLIND —
odin.py:64 and odin.py:54 — appended to the trade by _record_raven_alert. They mean the same
thing operationally: the system has declined to act and is handing the decision to the operator.
That was written to the ledger, echoed to a log file, and shown nowhere.
"""
import inspect

import pytest

import vega_app as app
from analysis import odin as O


def _pos(recommendation=None, alerts=None, **over):
    r = {"id": "T1", "ticker": "QQQ", "short_strike": 560.0, "long_strike": 555.0,
         "expiration": "2026-09-18", "dte": 30, "actual_fill_credit": 1.10,
         "spread_width": 5.0, "contracts": 1, "status": "open"}
    if alerts is None and recommendation:
        alerts = [{"at": "2026-08-07T10:00:00", "recommendation": recommendation,
                   "confidence": "medium", "plain_english": "The ravens disagree.",
                   "huginn_status": "VIOLATED", "muninn_probability": 0.55}]
    r["raven_alerts"] = alerts or []
    r.update(over)
    return r


# ── The states are real, and they are the ones that survive to the cockpit ────────────────────

def test_the_two_states_the_cockpit_renders_are_the_two_odin_can_leave_on_an_open_position():
    """WOLF_CLOSE and CLOSE close the trade in the same cycle they are raised, so they can
    never be read off an OPEN position. Rendering a card for them would be dead UI — the exact
    failure the plan warned about, from the opposite direction."""
    src = inspect.getsource(O.synthesize)
    for rec in ("HOLD_TENSION", "MUNINN_BLIND"):
        assert f'"{rec}"' in src, f"{rec} must exist in odin for the cockpit to key on it"
    assert set(app._RAVEN_CARDS) == {"HOLD_TENSION", "MUNINN_BLIND"}


def test_odin_still_produces_hold_tension_for_the_divergence_case():
    """Thought says broken, memory says recovered. Guards the card against the day someone
    renames the recommendation and leaves the cockpit matching a string nothing emits."""
    out = O.synthesize({"thesis_status": "VIOLATED", "reason": "support gone"},
                       {"sufficient": True, "recovery_probability": 0.60, "comparable_count": 20},
                       {})
    assert out["recommendation"] == "HOLD_TENSION"
    assert out["recommendation"] in app._RAVEN_CARDS


def test_odin_still_produces_muninn_blind_when_memory_has_nothing():
    out = O.synthesize({"thesis_status": "VIOLATED", "reason": "support gone"},
                       {"sufficient": False, "reason": "no comparable trades"}, {})
    assert out["recommendation"] == "MUNINN_BLIND"
    assert out["recommendation"] in app._RAVEN_CARDS


# ── Rendering ─────────────────────────────────────────────────────────────────────────────────

def test_a_hold_tension_position_renders_an_amber_card():
    html = app.raven_alerts([_pos("HOLD_TENSION")])
    assert "rav tension" in html
    assert "Ravens disagree" in html
    assert "QQQ" in html and "560.0/555.0" in html
    assert "The ravens disagree." in html
    assert "55% recovered" in html


def test_a_muninn_blind_position_renders_a_grey_card_and_says_memory_is_empty():
    html = app.raven_alerts([_pos("MUNINN_BLIND", alerts=[{
        "at": "2026-08-07T10:00:00", "recommendation": "MUNINN_BLIND", "confidence": "low",
        "plain_english": "No historical base rate.", "huginn_status": "UNDER_PRESSURE",
        "muninn_probability": None}])])
    assert "rav blind" in html
    assert "Memory is blind" in html
    assert "no comparable history" in html


def test_a_quiet_book_renders_nothing_at_all():
    """An alert strip that is always present stops being an alert."""
    assert app.raven_alerts([_pos(), _pos()]) == ""
    assert app.raven_alerts([]) == ""
    assert app.raven_alerts(None) == ""


def test_closing_recommendations_never_render():
    """WOLF_CLOSE on an open position means the close failed, not that a card is due. Rendering
    it would invite the operator to deliberate over a decision already taken."""
    for rec in ("WOLF_CLOSE", "CLOSE", "HOLD"):
        assert app.raven_alerts([_pos(rec)]) == ""


def test_only_the_latest_alert_per_position_is_shown_with_a_repeat_count():
    """A position under sustained strain re-alerts every cycle. Eleven copies of one
    disagreement would bury the other ten positions."""
    alerts = [{"at": f"2026-08-0{d}T10:00:00", "recommendation": "HOLD_TENSION",
               "confidence": "medium", "plain_english": f"read {d}",
               "huginn_status": "VIOLATED", "muninn_probability": 0.5} for d in (1, 2, 3)]
    html = app.raven_alerts([_pos(alerts=alerts)])
    assert html.count("rav tension") == 1
    assert "read 3" in html and "read 1" not in html
    assert "raised 3x" in html


def test_the_header_counts_positions_not_alerts():
    html = app.raven_alerts([_pos("HOLD_TENSION"), _pos("MUNINN_BLIND"), _pos()])
    assert "2 positions" in html
    html_one = app.raven_alerts([_pos("HOLD_TENSION")])
    assert "1 position" in html_one and "1 positions" not in html_one


def test_alerts_render_above_the_position_table():
    """Cards must be visible without scrolling. A decision the system refused to make is not
    a footnote to the position list."""
    html = app.open_section([_pos("HOLD_TENSION")])
    assert html.index("rav tension") < html.index("<h2>Open positions</h2>") < html.index("<table>")


def test_a_malformed_alert_does_not_break_the_page():
    html = app.raven_alerts([_pos(alerts=[{"recommendation": "HOLD_TENSION"}])])
    assert "rav tension" in html          # falls back to the built-in gloss
    assert "Hold deliberately or close deliberately" in html


def test_alert_text_is_escaped():
    html = app.raven_alerts([_pos(alerts=[{
        "at": "2026-08-07T10:00:00", "recommendation": "HOLD_TENSION",
        "plain_english": "<script>alert(1)</script>", "huginn_status": "VIOLATED"}])])
    assert "<script>" not in html


@pytest.mark.parametrize("ts", [None, "", "not-a-date", 12345])
def test_an_unreadable_timestamp_degrades_to_blank(ts):
    assert app._raven_age(ts) == ""


# ── Chain quality tile (P1-6) ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_dq(tmp_path, monkeypatch):
    from data import data_quality_log as dq
    monkeypatch.setattr(dq, "LOG_FILE", str(tmp_path / "dq.json"))
    return dq


def test_the_tile_reports_the_worst_ticker_not_the_mean(temp_dq):
    """A mean of 82% across 56 tickers is compatible with the one name you were about to trade
    being 20% quotable — and that name is the only one the number needed to warn about."""
    temp_dq.record("SPY", "polygon", 200, 196, scan_id="s1")
    temp_dq.record("QQQ", "polygon", 200, 190, scan_id="s1")
    temp_dq.record("GDX", "yfinance", 240, 48, scan_id="s1")
    val, col, sub = app._chain_quality_cell()
    assert val == "20%"
    assert col == "var(--red)"
    assert "GDX" in sub and "1 below floor" in sub


def test_a_healthy_scan_reads_green(temp_dq):
    temp_dq.record("SPY", "polygon", 200, 196, scan_id="s1")
    val, col, sub = app._chain_quality_cell()
    assert val == "98%" and col == "var(--green)" and "below floor" not in sub


def test_no_readings_yet_says_so_rather_than_claiming_health(temp_dq):
    val, col, sub = app._chain_quality_cell()
    assert val == "—" and sub == "no readings yet"


def test_board_freshness_kept_its_own_cell_under_its_real_name(temp_dq):
    """The old cell was labelled "Data quality" and measured how old the board was. Renaming it
    rather than replacing it: a board can be seconds old AND built on a chain that was mostly
    absent, and both facts are worth showing. Asserted on the rendered strip, because the label
    the operator reads is the thing that was wrong."""
    temp_dq.record("GDX", "yfinance", 240, 48, scan_id="s1")
    html = app._mc_system_status({"source": "engine", "at": "2026-08-07T10:00:00"}, [], "beta")
    assert ">Board freshness<" in html
    assert ">Chain quality<" in html
    assert ">Data quality<" not in html, "the misleading label must be gone from the UI"
    assert "20%" in html and "GDX" in html


# ── Payoff diagram, two modes (P1-7) ──────────────────────────────────────────────────────────

def _svg(**kw):
    return app.payoff_svg(100.0, 95.0, 1.20, 5.0, **kw)


def test_both_modes_mark_max_profit_and_max_loss():
    """The break-even dot alone shows where the trade turns and not what it risks getting
    there — and on a credit spread the loss endpoint is by far the larger number."""
    for mode in ("candidate", "position"):
        svg = _svg(mode=mode)
        assert svg.count("<circle") == 3, "break-even, max profit and max loss"
        assert 'fill="#00C97A"' in svg and 'fill="#F0455A"' in svg


def test_candidate_mode_shows_cushion_and_no_pl():
    svg = _svg(spot=110.0, mode="candidate")
    assert "9.1% cushion" in svg
    assert "$" not in svg


def test_cushion_can_be_passed_explicitly():
    assert "12.0% cushion" in _svg(mode="candidate", cushion_pct=0.12)


def test_position_mode_shows_unrealized_and_mark_not_cushion():
    svg = _svg(mode="position", unrealized=-45.16, current_mark=1.55)
    assert "$-45" in svg and "@$1.55" in svg
    assert "cushion" not in svg


def test_a_winning_position_reads_green_and_a_losing_one_red():
    assert 'fill="#00C97A">$+80' in _svg(mode="position", unrealized=80.0)
    assert 'fill="#F0455A">$-80' in _svg(mode="position", unrealized=-80.0)


def test_the_mark_is_never_plotted_as_a_point_on_the_curve():
    """This curve is P/L AT EXPIRY. A position's mark today carries time value the curve does
    not describe, so drawing it on the line would assert a P/L the trade is not at. It is a
    label, and the circle count proves no fourth point was added."""
    assert _svg(mode="position", unrealized=-45.0, current_mark=1.55).count("<circle") == 3


def test_an_unmarked_position_renders_without_labels():
    svg = _svg(mode="position")
    assert svg.startswith("<svg") and "$" not in svg


def test_the_default_mode_is_unchanged_for_existing_callers():
    assert _svg() == _svg(mode="candidate")


def test_a_junk_spread_still_renders_nothing():
    assert app.payoff_svg(None, 95.0, 1.20, 5.0) == ""
    assert app.payoff_svg(100.0, 95.0, 1.20, 0) == ""


def test_open_positions_use_position_mode():
    src = inspect.getsource(app.open_section)
    assert 'mode="position"' in src
    assert "unrealized=" in src and "current_mark=" in src
