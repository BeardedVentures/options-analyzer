"""The cycle produced counterfactual snapshots for two weeks and never recorded them.

`vega_candidates.py --no-open` runs every hour for exactly one reason: it is the only thing that
stores full gate results for candidates that FAILED, which is the raw material for asking whether
a gate earns its place. Nothing ever called analysis/counterfactuals.build(), so the ledger's last
write was 2026-08-10 and the cockpit's Gate value panel spent two weeks telling the operator to
run it by hand.

Wired 2026-08-24. These tests pin the three properties that make the wiring safe rather than the
fact that a call exists.
"""
from datetime import datetime
from unittest.mock import patch

import pytest

import auto_paper_cycle as apc
import config


@pytest.fixture
def quiet(monkeypatch):
    lines = []
    monkeypatch.setattr(apc, "_log", lambda m: lines.append(str(m)))
    return lines


def _at_hour(h):
    """Patch only the hour the gate reads, leaving the rest of datetime alone."""
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 24, h, 30, 0)
    return patch.object(apc, "datetime", _DT)


def test_it_records_near_the_close(quiet, monkeypatch):
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_ENABLED", True)
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_AFTER_HOUR", 14)
    monkeypatch.delenv("VEGA_COUNTERFACTUAL_AFTER_HOUR", raising=False)
    with _at_hour(14), patch("analysis.counterfactuals.build", return_value=639) as b:
        assert apc._record_counterfactuals() == {"resolved": 639}
    assert b.call_count == 1
    assert any("COUNTERFACTUALS resolved=639" in l for l in quiet)


def test_it_does_not_run_on_every_hourly_fire(quiet, monkeypatch):
    """THE COST PROPERTY. build() fetches 6 months of history per ticker; at ~56 tickers that is
    the most expensive thing in the cycle, and an intraday re-resolve returns the same answer
    because counterfactuals settle against DAILY bars."""
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_ENABLED", True)
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_AFTER_HOUR", 14)
    monkeypatch.delenv("VEGA_COUNTERFACTUAL_AFTER_HOUR", raising=False)
    with patch("analysis.counterfactuals.build") as b:
        for h in (8, 9, 10, 11, 12, 13):
            with _at_hour(h):
                assert apc._record_counterfactuals() == {}
        assert b.call_count == 0, "the recorder must not run on every hourly cycle"


def test_a_failure_is_advisory_and_never_takes_the_cycle_down(quiet, monkeypatch):
    """Every measurement panel in this cycle is advisory. A ledger problem must not stop the
    desk from marking and closing real positions."""
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_ENABLED", True)
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_AFTER_HOUR", 14)
    monkeypatch.delenv("VEGA_COUNTERFACTUAL_AFTER_HOUR", raising=False)
    with _at_hour(15), patch("analysis.counterfactuals.build",
                             side_effect=RuntimeError("price feed down")):
        assert apc._record_counterfactuals() == {}          # returns, does not raise
    assert any("Counterfactual recording failed" in l for l in quiet)


def test_it_can_be_switched_off(quiet, monkeypatch):
    monkeypatch.setattr(config, "COUNTERFACTUAL_RECORD_ENABLED", False)
    with _at_hour(15), patch("analysis.counterfactuals.build") as b:
        assert apc._record_counterfactuals() == {}
        assert b.call_count == 0


def test_the_cycle_actually_calls_it():
    """Guards the wiring itself: the function above is worthless if nothing invokes it, which is
    precisely the state this ledger was in for two weeks."""
    import inspect
    src = inspect.getsource(apc.run_cycle if hasattr(apc, "run_cycle") else apc.main)
    assert "_record_counterfactuals()" in src, \
        "the cycle does not call _record_counterfactuals — the original bug, restored"
