"""Liveness: every channel must be able to say it has produced nothing.

The failure this module exists to prevent is a measurement that reports zero forever without
saying so. A liveness check that cannot itself return CRITICAL would reproduce that failure one
level up, so every check below is exercised in BOTH directions: it must return OK on data that
grades, and CRITICAL on data that does not. A test that only asserts the healthy path would be
the same tautology the project has been bitten by before.
"""
import json

import pytest

from analysis import liveness as L


def _write(tmp_path, monkeypatch, name, rows):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(L, "LOG_DIR", tmp_path)
    return p


# ── counterfactual ledger ─────────────────────────────────────────────────────

def test_counterfactuals_report_CRITICAL_when_nothing_ever_matured(tmp_path, monkeypatch):
    """The literal 2026-09-02 state: 2,726 rows, horizon_complete False on every one, and
    structurally unable to change."""
    rows = [{"scan_date": "2026-01-05", "horizon_complete": False, "touched": None}
            for _ in range(50)]
    _write(tmp_path, monkeypatch, "vega_counterfactuals.jsonl", rows)
    r = L._check_counterfactuals()
    assert r["status"] == L.CRITICAL and r["graded"] == 0
    assert "NOT ONE" in r["reason"]


def test_counterfactuals_report_OK_once_rows_mature(tmp_path, monkeypatch):
    """The other direction. Without this the CRITICAL test proves only that the function can
    say no."""
    rows = [{"scan_date": "2026-01-05", "horizon_complete": True, "touched": False}]
    _write(tmp_path, monkeypatch, "vega_counterfactuals.jsonl", rows)
    assert L._check_counterfactuals()["status"] == L.OK


def test_young_counterfactuals_are_STARVED_not_CRITICAL(tmp_path, monkeypatch):
    """A row that has not had time to mature is not evidence of a broken instrument, and
    calling it CRITICAL every cycle is how an operator learns to ignore the file."""
    from datetime import datetime
    rows = [{"scan_date": datetime.now().date().isoformat(),
             "horizon_complete": False, "touched": None}]
    _write(tmp_path, monkeypatch, "vega_counterfactuals.jsonl", rows)
    assert L._check_counterfactuals()["status"] == L.STARVED


# ── shadow book ───────────────────────────────────────────────────────────────

def test_shadow_book_reports_CRITICAL_when_expired_rows_are_unpriced(tmp_path, monkeypatch):
    rows = [{"expired": True, "priced": False} for _ in range(5)]
    _write(tmp_path, monkeypatch, "vega_shadow_book.jsonl", rows)
    r = L._check_shadow_book()
    assert r["status"] == L.CRITICAL and r["graded"] == 0


def test_shadow_book_reports_OK_when_something_is_priced(tmp_path, monkeypatch):
    rows = [{"expired": True, "priced": True}, {"expired": True, "priced": False}]
    _write(tmp_path, monkeypatch, "vega_shadow_book.jsonl", rows)
    assert L._check_shadow_book()["status"] == L.OK


def test_unexpired_shadow_rows_are_STARVED(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "vega_shadow_book.jsonl",
           [{"expired": False, "priced": False}])
    assert L._check_shadow_book()["status"] == L.STARVED


# ── prediction ledger ─────────────────────────────────────────────────────────

def test_predictions_report_CRITICAL_when_none_ever_resolve(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl",
           [{"status": "open"} for _ in range(10)])
    assert L._check_predictions()["status"] == L.CRITICAL


def test_predictions_report_OK_but_carry_the_censoring_caveat(tmp_path, monkeypatch):
    """Liveness and interpretability are different questions. This channel PRODUCES; its
    resolved slice still is not a random sample, and the caveat must travel with the number."""
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl",
           [{"status": "resolved"}, {"status": "open"}])
    r = L._check_predictions()
    assert r["status"] == L.OK and r["graded"] == 1
    assert "CENSORED" in r["note"]


# ── caps cohort: starved vs broken ────────────────────────────────────────────

def test_caps_cohort_is_STARVED_not_CRITICAL_while_the_hold_is_on(monkeypatch):
    """A deliberate policy that produces zero is not a broken instrument."""
    import config
    from analysis import outcome_logger as ol
    monkeypatch.setattr(config, "ENTRY_HOLD", True, raising=False)
    monkeypatch.setattr(ol, "load_records", lambda: [])
    r = L._check_caps_cohort()
    assert r["status"] == L.STARVED
    assert "ENTRY_HOLD" in r["reason"]


def test_caps_cohort_becomes_CRITICAL_when_the_hold_is_lifted_and_it_is_still_zero(monkeypatch):
    """The transition that matters. With no hold to explain it, a cohort that records nothing
    is a fault -- and this is the assertion that makes the STARVED branch above meaningful
    rather than a permanent excuse."""
    import config
    from analysis import outcome_logger as ol
    monkeypatch.setattr(config, "ENTRY_HOLD", False, raising=False)
    monkeypatch.setattr(ol, "load_records", lambda: [])
    assert L._check_caps_cohort()["status"] == L.CRITICAL


# ── the registry itself ───────────────────────────────────────────────────────

def test_the_decision_ledger_is_registered_even_though_it_does_not_exist():
    """An unregistered channel is exactly what this module exists to catch: a measurement
    everyone assumes is running because nothing can see that it is not."""
    assert "decision_ledger" in L._no_production_ledgers
    assert L.check_all()["decision_ledger"]["status"] == L.NOT_BUILT


def test_all_five_channels_are_registered():
    """Five instruments were reporting nothing on 2026-09-02 and only four had been listed."""
    names = set(L.CHANNELS) | set(L._no_production_ledgers)
    assert names == {"counterfactual_ledger", "shadow_book", "caps_cohort",
                     "prediction_ledger", "decision_ledger"}, names


def test_a_check_that_raises_is_reported_CRITICAL_not_swallowed(monkeypatch):
    """A check that dies must not read as a passing channel."""
    def boom():
        raise RuntimeError("nope")
    monkeypatch.setitem(L.CHANNELS, "shadow_book", boom)
    r = L.check_all()["shadow_book"]
    assert r["status"] == L.CRITICAL and "the check itself failed" in r["reason"]


def test_report_puts_the_worst_first():
    results = {
        "a_ok": {"status": L.OK, "graded": 3},
        "b_critical": {"status": L.CRITICAL, "graded": 0, "reason": "x"},
        "c_starved": {"status": L.STARVED, "graded": 0, "reason": "y"},
    }
    lines = L.report(results)
    assert "CRITICAL" in lines[0] and "OK" in lines[-1]
