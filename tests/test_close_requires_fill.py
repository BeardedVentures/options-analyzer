"""`set_close` refuses to record a trade that was never filled.

It used to fall back to `modeled_credit_per_share or 0.0`, log a warning, and write the row as
`closed`. The warning was honest and the row was not: nothing downstream could tell that trade
from one that happened, and `analysis_eligible()` gates on fill_model and gate_basis — neither
of which knows whether a fill occurred — so it would have counted toward the cohort.

Level 1, not Level 2: `status == "closed"` did not mean what every consumer assumes.

Blast radius when this landed: ZERO. 75 closed rows, all carrying a float actual_fill_credit,
none at 0.0 — the path had never fired. Fixed as a landmine, not as a repair.
"""
import pytest

from analysis import outcome_logger as ol


def _open(**kw):
    return ol.open_paper_trade("TEST", 100.0, 95.0, "2026-10-16",
                               entry_credit_per_share=1.00, dte=35, delta=-0.22,
                               source="manual", **kw)


def _row(tid):
    return next(r for r in ol.load_records() if r.get("id") == tid)


def _strip_fill(tid):
    rows = ol._read_all()
    for r in rows:
        if r.get("id") == tid:
            r["actual_fill_credit"] = None
            r["modeled_credit_per_share"] = 1.25
    ol._write_all(rows)


# ── the refusal ──────────────────────────────────────────────────────────────

def test_a_trade_with_no_fill_is_not_closed(caplog):
    tid = _open(); _strip_fill(tid)
    with caplog.at_level("ERROR"):
        assert ol.set_close(tid, 0.50, "win", "target") is False
    r = _row(tid)
    assert r["status"] == "open"                      # untouched, not half-written
    assert r.get("realized_gross_pl_per_contract") is None
    assert r.get("outcome") is None
    assert any("REFUSING" in str(x.msg) or "REFUSING" in x.getMessage() for x in caplog.records)


def test_the_refusal_leaves_nothing_partially_written():
    """A half-closed row would be worse than either outcome."""
    tid = _open(); _strip_fill(tid)
    before = dict(_row(tid))
    ol.set_close(tid, 0.50, "win", "target")
    assert _row(tid) == before


def test_a_normal_close_still_works_and_is_stamped_actual():
    tid = _open()
    assert ol.set_close(tid, 0.50, "win", "target") is True
    r = _row(tid)
    assert r["status"] == "closed"
    assert r["fill_provenance"] == "actual"
    assert r["realized_gross_pl_per_contract"] == pytest.approx(50.0)


# ── the explicit override ────────────────────────────────────────────────────

def test_the_override_closes_but_stamps_the_row_forever():
    tid = _open(); _strip_fill(tid)
    assert ol.set_close(tid, 0.50, "win", "target", allow_modeled_fallback=True) is True
    r = _row(tid)
    assert r["status"] == "closed"
    assert r["fill_provenance"] == "modeled_fallback"
    # priced off the MODELLED credit, which is the thing being flagged
    assert r["realized_gross_pl_per_contract"] == pytest.approx((1.25 - 0.50) * 100)


def test_the_override_is_never_the_default():
    import inspect
    sig = inspect.signature(ol.set_close)
    assert sig.parameters["allow_modeled_fallback"].default is False


# ── the cohort consequence, which is the point ───────────────────────────────

def test_a_modeled_fallback_close_cannot_enter_the_analysis_cohort():
    """This is the defect: without it, a fictional close counts toward the 30-trade gate."""
    tid = _open(fill_model="natural"); _strip_fill(tid)
    ol.set_close(tid, 0.50, "win", "target", allow_modeled_fallback=True)
    r = _row(tid)
    assert r["fill_model"] == "natural"          # would otherwise pass every existing condition
    assert ol.gate_basis(r) == "natural"
    assert ol.analysis_eligible(r) is False


def test_absence_of_the_field_means_actual_not_unknown():
    """Every row written before the field existed carried a real fill — 75 of 75, verified
    2026-09-04. There is no third possibility, so absence must not exclude historical rows."""
    tid = _open(fill_model="natural")
    ol.set_close(tid, 0.50, "win", "target")
    rows = ol._read_all()
    for r in rows:
        r.pop("fill_provenance", None)
    ol._write_all(rows)
    assert ol.analysis_eligible(_row(tid)) is True


def test_the_silent_zero_branch_is_gone():
    """`or 0.0` made a row with no modelled credit either close at an entry of zero — its
    "realised" P/L the exit price with the sign flipped, its outcome arbitrary."""
    tid = _open()
    rows = ol._read_all()
    for r in rows:
        if r.get("id") == tid:
            r["actual_fill_credit"] = None
            r["modeled_credit_per_share"] = None
    ol._write_all(rows)
    assert ol.set_close(tid, 0.50, "loss", "stop") is False
    assert _row(tid)["status"] == "open"
