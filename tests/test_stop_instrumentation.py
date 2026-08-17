"""Which stop fired, recorded on the trade.

Two stops coexist and the ledger could not tell them apart:

    STOP_LOSS_MULTIPLIER  1.5x credit  — legacy, fires when the ravens cannot judge
    WOLF_STOP_MULTIPLIER  3.0x credit  — the ravens' hard floor

45 of 65 closed trades exited via a stop, and every cohort analysis so far has implicitly
assumed they were the same rule. They are not. A trade stopped at 1.5x and one stopped at 3.0x
are different experiments, and pooling them makes the close logic ungradeable — which is
hypothesis B in the open-questions appendix, the one the Ravens framework was built to answer
and still cannot.

This is instrumentation, not a behaviour change. No stop moved; the record now says which one
governed the exit.
"""
import pytest

import config
from analysis import outcome_logger as ol


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "o.jsonl"
    for attr in ("OUTCOMES_FILE", "LEDGER", "_FILE"):
        if hasattr(ol, attr):
            monkeypatch.setattr(ol, attr, p)
    return p


def _open(**over):
    kw = dict(ticker="TEST", short_strike=100.0, long_strike=95.0, expiration="2026-09-18",
              entry_credit_per_share=1.00, dte=35, delta=-0.20, contracts=1, source="test")
    kw.update(over)
    return ol.open_paper_trade(**kw)


def _row(p):
    import json
    return json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_a_legacy_stop_records_its_multiplier(ledger):
    tid = _open()
    ol.set_close(tid, 1.50, "loss", "auto-stop-loss", effective_stop_multiplier=1.5)
    assert _row(ledger)["effective_stop_multiplier"] == 1.5


def test_a_wolf_stop_records_a_different_one(ledger):
    tid = _open()
    ol.set_close(tid, 3.00, "loss", "wolf-stop", effective_stop_multiplier=3.0)
    assert _row(ledger)["effective_stop_multiplier"] == 3.0


def test_a_profit_target_carries_no_multiplier(ledger):
    """A field that only applies to stops must not appear on exits it never governed —
    otherwise the analysis that conditions on it silently includes non-stop closes."""
    tid = _open()
    ol.set_close(tid, 0.50, "win", "auto-target-profit")
    assert _row(ledger)["effective_stop_multiplier"] is None


def test_a_dte_close_carries_no_multiplier(ledger):
    tid = _open()
    ol.set_close(tid, 0.80, "scratch", "auto-dte-window")
    assert _row(ledger)["effective_stop_multiplier"] is None


def test_the_two_stops_are_genuinely_different_rules():
    """If these ever converge the instrumentation is pointless — and if one is edited to match
    the other by accident, this fails rather than the distinction quietly vanishing."""
    legacy = float(getattr(config, "STOP_LOSS_MULTIPLIER"))
    wolf = float(getattr(config, "WOLF_STOP_MULTIPLIER"))
    assert legacy != wolf
    assert wolf > legacy, "the ravens' floor is meant to be the LOOSER of the two"


def test_both_close_paths_stamp_the_field():
    """The legacy path is _apply_close_rules; the wolf path is the ravens close. Either one
    failing to stamp leaves a stop-exited trade indistinguishable from the other regime."""
    import inspect
    import auto_paper_cycle as apc
    src = inspect.getsource(apc)
    assert src.count("effective_stop_multiplier=") >= 2, \
        "both stop paths must record which rule fired"


def test_existing_closes_read_as_unknown_not_as_a_default(ledger):
    """History is not rewritten. 65 trades closed before this field existed and must read as
    absent rather than acquiring a multiplier nobody recorded — the same rule that keeps
    close_cohort honest about legacy rows."""
    import json
    ledger.write_text(json.dumps({"id": "old", "status": "closed", "outcome": "loss",
                                  "exit_reason": "auto-stop-loss"}) + "\n", encoding="utf-8")
    rows = [r for r in ol.load_records() if r.get("id") == "old"]
    assert rows and rows[0].get("effective_stop_multiplier") is None
