"""The decision ledger — what the operator did with what VEGA recommended.

The paper ledger is a censored sample: it holds the trades that were TAKEN and knows nothing
about the ones waved through. That missing half is the only record that could ever answer
whether the operator's overrides add information or destroy it.

These tests pin the one property that makes the ledger worth keeping: a decision is stored
WITH the entry state that made it. A row saying "rejected WMT on the 11th" cannot be graded
against anything — by the time anyone looks the chain has moved and the setup no longer
exists. Instrumentation added after the fact cannot be backfilled, which is why the
2026-08-10 snapshot carries no earnings_source and why that audit could not be run.
"""
import pytest

import vega_app
from analysis import decisions as dec


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "decisions.jsonl"


def _snap(**over):
    s = {"strategy": "bull_put_spread", "short_strike": 117.0, "long_strike": 116.0,
         "expiration": "2026-09-18", "dte": 38, "credit_per_share": 0.32,
         "credit_usd": 32.0, "max_loss_usd": 68.0, "delta": -0.21,
         "true_pop": 0.84, "pop_implied": 0.72, "edge_score": 78.0,
         "iv_rank": 76.0, "vrp": 10.7, "spot": 120.5, "roi": 0.47}
    s.update(over)
    return s


# ── The entry state is the whole point ────────────────────────────────────────────────────────

def test_a_decision_carries_the_state_that_makes_it_gradeable(ledger):
    row = dec.record(dec.REJECT, "WMT", _snap(), ledger=ledger)
    for k in ("short_strike", "long_strike", "expiration", "credit_per_share",
              "true_pop", "pop_implied", "edge_score", "delta"):
        assert row.get(k) is not None, f"{k} was dropped — the row cannot be graded later"


def test_pop_gap_is_derived_when_the_caller_omits_it(ledger):
    """The one number the reject ledger exists to grade must not depend on the UI
    remembering to post it."""
    row = dec.record(dec.REJECT, "WMT", _snap(), ledger=ledger)
    assert row["pop_gap"] == pytest.approx(0.12)


def test_a_negative_gap_is_recorded_not_discarded(ledger):
    row = dec.record(dec.REJECT, "WMT", _snap(true_pop=0.60, pop_implied=0.72), ledger=ledger)
    assert row["pop_gap"] == pytest.approx(-0.12)


def test_form_strings_survive_the_trip(ledger):
    """Everything arrives from an HTML form as a string; float('') would raise and take the
    whole POST down over one absent optional."""
    row = dec.record(dec.WATCH, "wmt",
                     {"short_strike": "117.0", "delta": "", "true_pop": "0.84",
                      "edge_score": None, "expiration": "2026-09-18"},
                     ledger=ledger)
    assert row["short_strike"] == 117.0
    assert row["delta"] is None and row["edge_score"] is None
    assert row["ticker"] == "WMT"


def test_a_decision_with_no_snapshot_still_records(ledger):
    """Degrading to a bare row beats refusing the click and losing the signal entirely."""
    row = dec.record(dec.WATCH, "WMT", ledger=ledger)
    assert row["ticker"] == "WMT" and row["decision"] == dec.WATCH


# ── Ledger integrity ──────────────────────────────────────────────────────────────────────────

def test_the_ledger_is_append_only(ledger):
    dec.record(dec.WATCH, "AAA", _snap(), ledger=ledger)
    dec.record(dec.REJECT, "BBB", _snap(), ledger=ledger)
    dec.record(dec.WATCH, "CCC", _snap(), ledger=ledger)
    rows = dec.load(ledger)
    assert [r["ticker"] for r in rows] == ["AAA", "BBB", "CCC"]
    assert len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_an_unknown_decision_is_refused_rather_than_stored(ledger):
    """Three spellings of "reject" in one ledger cannot be grouped, and the failure would not
    surface until someone tried to analyse it months later."""
    with pytest.raises(ValueError):
        dec.record("dismissed", "WMT", _snap(), ledger=ledger)
    assert dec.load(ledger) == []


def test_a_missing_ticker_is_refused(ledger):
    with pytest.raises(ValueError):
        dec.record(dec.WATCH, "", _snap(), ledger=ledger)


def test_a_corrupt_line_does_not_make_the_history_unreadable(ledger):
    dec.record(dec.WATCH, "AAA", _snap(), ledger=ledger)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write("{half a row from a killed process\n")
    dec.record(dec.REJECT, "BBB", _snap(), ledger=ledger)
    assert [r["ticker"] for r in dec.load(ledger)] == ["AAA", "BBB"]


def test_load_of_a_ledger_that_does_not_exist_is_empty_not_an_error(tmp_path):
    assert dec.load(tmp_path / "nope.jsonl") == []


# ── Reading it back ───────────────────────────────────────────────────────────────────────────

def test_the_watchlist_shows_one_row_per_setup_but_keeps_every_touch(ledger):
    """A setup watched three times is one watchlist item; the repeats stay in the ledger
    because the repeat is itself a signal about conviction."""
    for _ in range(3):
        dec.record(dec.WATCH, "AAA", _snap(), ledger=ledger)
    dec.record(dec.WATCH, "BBB", _snap(short_strike=50.0), ledger=ledger)
    assert len(dec.watching(ledger)) == 2
    assert len(dec.load(ledger)) == 4


def test_rejects_do_not_appear_on_the_watchlist(ledger):
    dec.record(dec.REJECT, "AAA", _snap(), ledger=ledger)
    assert dec.watching(ledger) == []


def test_summary_compares_the_two_sides_of_the_operators_judgement(ledger):
    """If rejects score no lower than watches, the overrides are noise. If they score HIGHER,
    something is wrong with either the score or the operator — worth knowing early."""
    dec.record(dec.WATCH, "AAA", _snap(edge_score=80.0), ledger=ledger)
    dec.record(dec.REJECT, "BBB", _snap(edge_score=60.0), ledger=ledger)
    dec.record(dec.REJECT, "CCC", _snap(edge_score=70.0), ledger=ledger)
    s = dec.summary(ledger=ledger)
    assert s["watch_count"] == 1 and s["reject_count"] == 2
    assert s["watch_mean_edge"] == 80.0
    assert s["reject_mean_edge"] == 65.0


def test_summary_reports_its_own_sample_size(ledger):
    """A mean over three rows is not a finding, and a summary that hides the count invites
    one to be read as such."""
    s = dec.summary(ledger=ledger)
    assert s["total"] == 0
    assert s["watch_mean_edge"] is None and s["reject_mean_edge"] is None


# ── The asymmetry card's direction tag (A4-4) ─────────────────────────────────────────────────

def test_the_tag_distinguishes_momentum_from_a_counter_trend_bet():
    """A long call is bullish by construction, so a BULLISH tag on every card would be the
    same decoration the fixed HIGH badge was. What varies is whether the chart agrees."""
    assert "with the trend" in vega_app._lottery_direction_tag("up")
    assert "Counter-trend" in vega_app._lottery_direction_tag("down")
    assert "Counter-trend" in vega_app._lottery_direction_tag("STRONG_DOWN")
    assert "No clear trend" in vega_app._lottery_direction_tag("sideways")


def test_no_tag_is_shown_when_the_trend_is_unknown():
    assert vega_app._lottery_direction_tag("") == ""
    assert vega_app._lottery_direction_tag(None) == ""


def test_asymmetry_cards_offer_the_decisions_they_can_honour():
    """The single-long-call paper path is not wired, so the card offers Watch and Reject
    rather than a Log button that does nothing."""
    h = vega_app._lottery_actions({"ticker": "SPY", "strike": 600.0, "premium_usd": 389.0})
    assert 'action="/watch"' in h and 'action="/reject"' in h
    assert "SPY" in h


def test_the_pop_gap_mean_survives_rounding(ledger):
    """pop_gap is a 0-1 fraction and edge_score is 0-100. Sharing one rounding rule rounded
    every realistic gap (0.02-0.12) straight to 0.0 and silently deleted the single number
    this ledger exists to produce."""
    for g in (0.043, 0.061, 0.028):
        dec.record(dec.REJECT, "AAA", _snap(true_pop=0.5 + g, pop_implied=0.5), ledger=ledger)
    assert dec.summary(ledger=ledger)["reject_mean_pop_gap_pp"] == pytest.approx(4.4, abs=0.05)


def test_the_gap_mean_carries_its_unit_in_its_name(ledger):
    """So the next reader cannot re-scale a value that is already in percentage points."""
    dec.record(dec.REJECT, "AAA", _snap(), ledger=ledger)
    assert "reject_mean_pop_gap_pp" in dec.summary(ledger=ledger)
