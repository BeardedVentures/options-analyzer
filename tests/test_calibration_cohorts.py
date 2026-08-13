"""Calibration must be reported per cohort, never pooled.

The ledger holds three incompatible regimes. Pooled, they reported a 56.8pp calibration miss
and the cockpit showed it as "the" calibration gap. Split, the picture inverts:

    mid    | mid | credit_stop   n=18   -5.4pp   <- roughly calibrated
    natural| mid | credit_stop   n=41  -77.3pp
    natural| mid | ravens_v1     n= 5  -73.8pp

The pooled number described the FILL MODEL, not the POP model. vega_status has refused to pool
these since the cohorts were defined ("Cohorts are not comparable"); clv_tracker was the one
place still doing it, and it fed the cockpit.
"""
import json

import pytest

import clv_tracker as C


@pytest.fixture
def ledger(tmp_path):
    """summary() reads a ledger file, so the fixture writes one."""
    def _write(rows):
        p = tmp_path / "outcomes.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return p
    return _write


def _r(pop, outcome, opened, fill=None, close_logic=None):
    r = {"status": "closed", "outcome": outcome, "modeled_pop": pop,
         "opened_at": f"{opened}T12:00:00", "dte": 35,
         "modeled_credit_per_share": 1.0, "exit_price": 0.5}
    if fill:
        r["fill_model"] = fill
    if close_logic:
        r["close_logic"] = close_logic
    return r


def test_a_cohort_is_scored_on_its_own_records_only():
    good = [_r(0.75, "win", "2026-07-10") for _ in range(8)]
    bad = [_r(0.75, "loss", "2026-09-10") for _ in range(8)]
    cur = C.calibration_curve(good + bad, cohort=C._cohort_of(good[0]))
    graded = [b for b in cur if b["n"]]
    assert graded and all(b["n"] <= len(good) for b in graded)
    assert graded[0]["realized"] == 1.0, "the losing cohort leaked in"


def test_the_summary_names_the_cohort_its_headline_describes(ledger):
    s = C.summary(ledger([_r(0.75, "win", "2026-07-10"), _r(0.75, "loss", "2026-09-10")]))
    assert s["calibration_cohort"], "headline gap with no cohort attached is unreadable"
    assert s["calibration_cohort_n"]


def test_the_summary_says_how_many_cohorts_it_did_not_pool(ledger):
    """Without this the reader cannot tell a whole-ledger verdict from one regime of three."""
    rows = ([_r(0.75, "win", "2026-07-10") for _ in range(3)]
            + [_r(0.75, "loss", "2026-09-10") for _ in range(3)])
    assert C.summary(ledger(rows))["calibration_cohorts_present"] >= 2


def test_every_cohort_is_reported_not_just_the_headline(ledger):
    rows = ([_r(0.75, "win", "2026-07-10") for _ in range(3)]
            + [_r(0.75, "loss", "2026-09-10") for _ in range(3)])
    by = C.summary(ledger(rows))["calibration_by_cohort"]
    assert len(by) >= 2
    assert sum(c["n"] for c in by) == 6, "records went missing between cohorts"


def test_the_headline_reports_the_largest_cohort(ledger):
    rows = ([_r(0.75, "loss", "2026-09-10") for _ in range(9)]
            + [_r(0.75, "win", "2026-07-10") for _ in range(2)])
    s = C.summary(ledger(rows))
    assert s["calibration_cohort_n"] == 9


def test_pooling_is_still_available_when_explicitly_asked_for():
    rows = [_r(0.75, "win", "2026-07-10"), _r(0.75, "loss", "2026-09-10")]
    pooled = [b for b in C.calibration_curve(rows) if b["n"]]
    assert sum(b["n"] for b in pooled) == 2


def test_an_empty_ledger_reports_no_cohort_rather_than_a_fake_one(ledger):
    s = C.summary(ledger([]))
    assert s["calibration_gap_pp"] is None
    assert s["calibration_cohort"] is None


def test_the_summary_reports_how_much_of_the_ledger_is_analysable(ledger):
    """Zero eligible means every calibration number on the page is drawn from trades the
    system itself calls broken-thermometer readings. That has to be visible."""
    s = C.summary(ledger([_r(0.75, "win", "2026-07-10"), _r(0.75, "loss", "2026-09-10")]))
    assert "calibration_eligible_n" in s
    assert "calibration_lead_eligible" in s


def test_a_cohort_lookup_failure_does_not_silently_re_pool(monkeypatch):
    """A swallowed import returning one shared bucket is indistinguishable from a healthy
    single-regime ledger, and it suppresses the multi-cohort warning."""
    import analysis.outcome_logger as ol
    monkeypatch.setattr(ol, "cohort", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    assert C._cohort_of({}) == "cohort-unavailable"


def test_a_new_trade_records_its_basis_at_open_and_is_analysable(tmp_path, monkeypatch):
    """The unlock. Both fields were DERIVED from the open date afterwards, which only works
    while a cutoff date is the whole story — it breaks the moment a rule changes mid-week and
    it cannot describe a trade opened under a config later reverted. 0 of 64 closed trades
    passed analysis_eligible, so the cohort that could validate this system did not exist.
    Recorded at write time, it does."""
    import json
    from analysis import outcome_logger as ol
    p = tmp_path / "o.jsonl"
    for attr in ("OUTCOMES_FILE", "LEDGER", "_FILE"):
        if hasattr(ol, attr):
            monkeypatch.setattr(ol, attr, p)
    ol.open_paper_trade(ticker="TEST", short_strike=100, long_strike=95,
                        expiration="2026-09-18", entry_credit_per_share=1.0,
                        dte=35, delta=-0.2, contracts=1, source="test")
    row = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["fill_basis"] == "natural" and row["gate_basis"] == "natural"
    assert ol.analysis_eligible(row) is True


def test_the_basis_can_be_stated_explicitly_rather_than_inferred():
    """A caller that knows it filled at the mid must be able to say so."""
    import inspect
    from analysis import outcome_logger as ol
    sig = inspect.signature(ol.open_paper_trade)
    assert "fill_basis" in sig.parameters and "gate_basis" in sig.parameters
