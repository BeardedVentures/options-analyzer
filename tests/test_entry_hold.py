#!/usr/bin/env python3
"""ENTRY_HOLD: a real stop on opening positions, proven to stop.

The 2026-09-02 brief asked for confirmation that something actually prevents the first caps_v1
trade from opening -- and explicitly said not to accept a soft convention ("we just won't run
it"). The honest answer at that point was that NOTHING prevented it. Entry was not gated; it
merely was not happening, because the board has qualified almost nothing since 2026-08-10.

That is a bug holding the door, not a control. The moment one board qualifies,
_auto_open_from_board opens up to MAX_NEW_OPENS_PER_RUN positions with no further approval.

These tests exist because a stop nobody has watched stop something is not a stop.
"""
import json

import pytest

import config
import auto_paper_cycle as apc


@pytest.fixture
def board_that_would_open(tmp_path, monkeypatch):
    """A board carrying a live, fillable, fully-gated trade -- i.e. one the desk WOULD open.

    Without this the test proves nothing: a hold that blocks an empty board is
    indistinguishable from the drought it is meant to be independent of.
    """
    board = tmp_path / "scan_latest.json"
    board.write_text(json.dumps({
        "qualified_trades": [{
            "ticker": "ZZZZ", "strategy": "bull_put_spread",
            "short_strike": 100.0, "long_strike": 95.0, "expiration": "2026-10-16",
            "expiration_display": "2026-10-16", "dte": 40,
            "natural_credit_per_share": 1.20, "natural_credit_usd": 120.0,
            "credit_per_share": 1.35, "fill_basis": "natural", "quotes_live": True,
            "edge_score": 80, "vrp": 8.0, "chain_source": "robinhood",
            # `assessment_gates`, NOT `gates`. The first draft of this fixture used `gates`,
            # so the trade was rejected as "not fully gated" and the hold test passed for the
            # wrong reason -- proving only that a malformed board opens nothing.
            "assessment_gates": {k: True for k in getattr(config, "REQUIRED_GATES", ())},
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(apc, "BOARD_FILE", board)
    return board


def test_the_hold_blocks_a_board_that_would_otherwise_open(board_that_would_open, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_HOLD", True, raising=False)
    assert apc._auto_open_from_board() == 0


def test_the_hold_is_checked_before_the_board_is_read(monkeypatch):
    """A hold that only applies to boards it managed to parse has a hole in it. Point
    BOARD_FILE at a path that does not exist and the hold must still be the thing that
    returns, not the missing-file branch."""
    monkeypatch.setattr(config, "ENTRY_HOLD", True, raising=False)
    monkeypatch.setattr(apc, "BOARD_FILE", __import__("pathlib").Path("does-not-exist.json"))
    assert apc._auto_open_from_board() == 0

    import inspect
    src = inspect.getsource(apc._auto_open_from_board)
    assert src.index("ENTRY_HOLD") < src.index("BOARD_FILE.exists()")


def test_the_hold_writes_its_reason_where_the_operator_will_see_it(
        board_that_would_open, monkeypatch):
    """Silent refusal is how the Polygon guard sat jammed shut for 29 runs. If this stops a
    trade, the log has to say so and say why."""
    monkeypatch.setattr(config, "ENTRY_HOLD", True, raising=False)
    said = []
    monkeypatch.setattr(apc, "_log", lambda m, *a, **k: said.append(str(m)))
    apc._auto_open_from_board()
    joined = " ".join(said)
    assert "ENTRY HELD" in joined
    assert "ENTRY_HOLD = False" in joined, "the log must say how to lift it"
    assert len(joined) > 120, "a bare 'held' line is not a reason"


def test_releasing_the_hold_actually_reaches_the_board(board_that_would_open, monkeypatch):
    """The control must be able to read OFF. A flag that blocks in both positions is not a
    gate, it is an outage -- this is the non-zero half of the measurement."""
    monkeypatch.setattr(config, "ENTRY_HOLD", False, raising=False)
    said = []
    monkeypatch.setattr(apc, "_log", lambda m, *a, **k: said.append(str(m)))
    apc._auto_open_from_board()
    assert "ENTRY HELD" not in " ".join(said)


def test_the_hold_carries_a_reason_and_is_currently_ON():
    """Deliberately asserts the DEPLOYED state, not just the mechanism. If someone lifts the
    hold, this test turning red is the intended notification -- delete it in the same commit
    that lifts it, so releasing entry is a deliberate, reviewed act rather than a flag flip.

    Reads the SOURCE, not the runtime attribute: conftest turns ENTRY_HOLD off for every test
    so the entry caps stay covered, so the attribute here would always read False. What is
    committed is the thing this test is about."""
    import pathlib
    src = (pathlib.Path(config.__file__)).read_text(encoding="utf-8", errors="replace")
    assert "ENTRY_HOLD = True" in src, "entry hold has been lifted -- was that deliberate?"
    assert len(config.ENTRY_HOLD_REASON) > 100


def test_the_hold_changes_no_cohort_dimension():
    """ENTRY_HOLD gates entry TIMING, not selection. If it ever leaked into cohort() it would
    split caps_v1 the moment it was lifted -- exactly what the entry caps had to be given an
    epoch to avoid."""
    from analysis import outcome_logger
    key = outcome_logger.cohort({"fill_model": "natural", "chain_source": "robinhood"})
    assert "hold" not in key.lower()
    import inspect
    assert "ENTRY_HOLD" not in inspect.getsource(outcome_logger.cohort)
