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

def test_shadow_book_is_CRITICAL_only_when_the_WRITER_is_still_at_fault(tmp_path, monkeypatch):
    """Expired-and-unpriced is not by itself a fault, and treating it as one misdirected three
    reviews at a bug that had already been fixed.

    The question CRITICAL is supposed to answer is "is something still producing rows that can
    never be graded". A row written after the fill-basis fix with no natural credit answers yes.
    """
    rows = [{"expired": True, "priced": False, "scan_date": "2026-08-28",
             "natural_credit_per_share": None} for _ in range(5)]
    _write(tmp_path, monkeypatch, "vega_shadow_book.jsonl", rows)
    r = L._check_shadow_book()
    assert r["status"] == L.CRITICAL and r["graded"] == 0
    assert "writer is producing unpriceable rows again" in r["reason"]


def test_shadow_book_unpriced_rows_that_all_predate_the_fix_are_a_closed_set(tmp_path, monkeypatch):
    """Same observable state -- expired, unpriced, zero graded -- and a different verdict.

    These rows are unrecoverable rather than wrong, and the channel says so instead of naming a
    cause that no longer exists. A CRITICAL that points at a fixed bug trains the reader to
    discount the channel, which is more expensive than the missing grade.
    """
    rows = [{"expired": True, "priced": False, "scan_date": "2026-08-04",
             "natural_credit_per_share": None} for _ in range(5)]
    _write(tmp_path, monkeypatch, "vega_shadow_book.jsonl", rows)
    r = L._check_shadow_book()
    assert r["status"] == L.STARVED and r["graded"] == 0
    assert "CLOSED historical set" in r["reason"]
    assert "predates the fill-basis fix" in r["reason"]


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


def test_every_measurement_channel_is_registered():
    """Five instruments were reporting nothing on 2026-09-02 and only four had been listed.

    Pinned as an exact set rather than a count, so ADDING a channel without registering it
    fails here. That is the whole failure mode: `band_forecast` shared a ledger file with the
    direction claims, so `prediction_ledger` read OK on 3,000 rows while the band scorer had
    never been fed once. A starved channel hiding inside a healthy one is invisible to
    everything except an explicit list.
    """
    names = set(L.CHANNELS) | set(L._no_production_ledgers)
    assert names == {"counterfactual_ledger", "shadow_book", "caps_cohort",
                     "prediction_ledger", "band_forecast", "decision_ledger"}, names


def test_band_forecast_is_checked_separately_from_the_ledger_it_shares(tmp_path, monkeypatch):
    """A file full of direction claims must not make the band channel read as healthy."""
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl",
           [{"claim_type": "direction_1w", "status": "resolved"} for _ in range(500)])
    assert L._check_predictions()["status"] == L.OK
    band = L._check_band_forecasts()
    assert band["status"] == L.STARVED and band["graded"] == 0
    assert "no range claims recorded yet" in band["reason"]


def test_band_forecast_reports_its_horizons_so_a_missing_one_is_visible(tmp_path, monkeypatch):
    rows = [{"claim_type": ct, "status": "resolved"}
            for ct in ("band_contains_1d", "band_contains_1w", "band_contains_1w_baseline")]
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl", rows)
    r = L._check_band_forecasts()
    assert r["status"] == L.OK and r["graded"] == 3
    # Baselines are not horizons; a horizon that stops being written goes missing from this list.
    assert r["horizons"] == ["band_contains_1d", "band_contains_1w"]


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


# ── band-channel heartbeat ────────────────────────────────────────────────────
#
# The half that matters is STALENESS. Every other check asks whether the sweep produced
# anything WHEN IT RAN; none of them can see it no longer being called. A heartbeat that cannot
# return CRITICAL reproduces, one level up, the exact defect it exists to catch.

import datetime as _dt


def _band_rows(day, n=8, status="resolved"):
    return [{"claim_type": "band_contains_1w", "status": status,
             "made_at": f"{day}T14:50:00", "ticker": f"T{i}"} for i in range(n)]


def test_a_stale_band_channel_is_CRITICAL_even_though_the_ledger_is_full(tmp_path, monkeypatch):
    """The failure this catches: the sweep stops being CALLED. Rows stay healthy, and stop."""
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl", _band_rows("2026-08-20"))
    r = L._check_band_forecasts()
    assert r["status"] == L.CRITICAL
    assert "no longer being CALLED" in r["reason"]
    assert r["stale_weekdays"] > 2


def test_a_fresh_band_channel_is_OK(tmp_path, monkeypatch):
    today = _dt.date.today().isoformat()
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl", _band_rows(today))
    r = L._check_band_forecasts()
    assert r["status"] == L.OK and r["stale_weekdays"] == 0


def test_a_weekend_is_not_staleness(tmp_path, monkeypatch):
    """Counted in weekdays, so a Friday write is not two days stale on Sunday."""
    friday = _dt.date(2026, 9, 4)
    sunday = _dt.date(2026, 9, 6)
    assert L._weekdays_since(friday.isoformat(), sunday) == 0


def test_one_market_holiday_does_not_trip_the_staleness_alarm():
    """`next_trading_day` does not model holidays, so the tolerance carries them.

    A CRITICAL that fires every Labor Day is a CRITICAL nobody reads by November -- which is
    how the shadow_book message stopped being believed.
    """
    # The scenario that actually occurs: the sweep writes on Friday, Monday is a market
    # holiday so no cycle runs, and the check fires on Tuesday MORNING -- before that day's
    # sweep, which is gated to the afternoon. Two weekdays have passed and nothing is wrong.
    friday = _dt.date(2026, 9, 4)
    tuesday_after_a_monday_holiday = _dt.date(2026, 9, 8)
    assert L._weekdays_since(friday.isoformat(), tuesday_after_a_monday_holiday) == 2
    assert 2 <= int(getattr(L.config, "BAND_STALE_MAX_WEEKDAYS", 2))

    # And a genuinely missed session still trips: Thursday write, Friday's cycle dead, holiday
    # Monday, checked Tuesday -- three weekdays, which the tolerance does not absorb.
    thursday = _dt.date(2026, 9, 3)
    assert L._weekdays_since(thursday.isoformat(), tuesday_after_a_monday_holiday) == 3


def test_staleness_outranks_every_other_band_verdict(tmp_path, monkeypatch):
    """A stale channel must not be able to report OK on the strength of old resolved rows."""
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl",
           _band_rows("2026-01-05", n=500, status="resolved"))
    assert L._check_band_forecasts()["status"] == L.CRITICAL


def test_an_unreadable_made_at_does_not_crash_or_falsely_alarm(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "vega_predictions.jsonl",
           [{"claim_type": "band_contains_1w", "status": "resolved", "made_at": "garbage"}])
    r = L._check_band_forecasts()
    assert r["status"] in (L.OK, L.STARVED)      # never CRITICAL on an unparseable date
