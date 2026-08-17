"""Boundary contracts — the generalized fix for two recurring defect classes.

FIELD-NAME MISMATCH: producer writes `vrp_pp`, consumer reads `vrp`. The largest component of
the edge score read zero on every trade the auto-trader opened, for weeks, and nothing failed.

SILENT NONE: edge_score was None on 100% of real paper trades; support_level_at_entry was null
on every managed position. Both found by after-the-fact audit, months later.

Both are invisible because `float(None or 0)` is happy to produce a zero, and a zero that
should have been a measurement is indistinguishable from a measurement of zero. Patching
instances did not stop recurrence; this makes the class fail loudly at the seam instead.
"""
import math

import pytest

from analysis import contracts as C


SPEC = (("ticker", "str"), ("credit", "num"), ("size", "num0"))


def _ok():
    return {"ticker": "WMT", "credit": 1.25, "size": 2}


# ── The two failures this exists to catch ─────────────────────────────────────────────────────

def test_none_is_not_zero():
    """The whole reason for the module. `float(None or 0)` yields 0.0 and looks like data."""
    p = C.validate(dict(_ok(), credit=None), SPEC, "t")
    assert p and "None" in p[0]


def test_a_missing_field_is_caught_not_defaulted():
    """The field-name mismatch class: the consumer reads a key the producer never wrote."""
    rec = _ok(); del rec["credit"]
    assert any("missing" in x for x in C.validate(rec, SPEC, "t"))


def test_nan_is_refused():
    """NaN survives every comparison it meets and renders as a plausible value — the same bug
    already found in the vol-index feed."""
    assert C.validate(dict(_ok(), credit=float("nan")), SPEC, "t")


def test_infinity_is_refused():
    assert C.validate(dict(_ok(), credit=float("inf")), SPEC, "t")


def test_a_negative_where_non_negative_is_required_is_caught():
    assert C.validate(dict(_ok(), size=-1), SPEC, "t")


def test_an_empty_string_is_not_a_ticker():
    assert C.validate(dict(_ok(), ticker="  "), SPEC, "t")


def test_a_clean_record_passes_silently():
    assert C.validate(_ok(), SPEC, "t") == []


def test_every_problem_is_reported_not_just_the_first():
    """Fixing one field per round trip across three runs is how these get abandoned."""
    assert len(C.validate({"ticker": "", "credit": None}, SPEC, "t")) == 3


# ── Two severities, deliberately different ────────────────────────────────────────────────────

def test_a_write_raises():
    """A ledger row that cannot be graded is worse than no row — it looks like data and gets
    averaged into a base rate by someone who was not there when it was written."""
    with pytest.raises(C.ContractError):
        C.enforce(dict(_ok(), credit=None), SPEC, "ledger")


def test_the_raise_names_every_field_and_why():
    with pytest.raises(C.ContractError) as e:
        C.enforce({"ticker": "X"}, SPEC, "ledger")
    assert "credit" in str(e.value) and "size" in str(e.value)


def test_a_read_rejects_without_raising():
    """A malformed candidate must not take down a scan of 56 names."""
    assert C.accept(dict(_ok(), credit=None), SPEC, "scan") is False
    assert C.accept(_ok(), SPEC, "scan") is True


# ── The three real boundaries ─────────────────────────────────────────────────────────────────

def test_the_ledger_refuses_a_trade_it_could_never_grade(tmp_path, monkeypatch):
    """dte is required because the CLV baseline scales by term — a row without it is excluded
    from the scorecard entirely, so writing one is collecting a number nobody can use."""
    from analysis import outcome_logger as ol
    for a in ("OUTCOMES_FILE", "LEDGER", "_FILE"):
        if hasattr(ol, a):
            monkeypatch.setattr(ol, a, tmp_path / "o.jsonl")
    with pytest.raises(C.ContractError):
        ol.open_paper_trade(ticker="X", short_strike=100.0, long_strike=95.0,
                            expiration="2026-09-18", entry_credit_per_share=1.0,
                            dte=None, contracts=1, source="test")


def test_the_open_contract_requires_cohort_identity():
    """A trade whose fill/gate basis is unknown cannot be placed in a cohort, and a trade
    outside a cohort cannot inform a base rate."""
    fields = {f for f, _ in C.OPEN_TRADE}
    assert {"fill_basis", "gate_basis"} <= fields


def test_the_select_contract_requires_computable_risk():
    """States as a contract what the 2026-08-16 sizing fix decided locally: a candidate whose
    max loss cannot be computed cannot be shown to fit the account."""
    assert ("max_loss_usd", "num0") in C.SELECT_CANDIDATE


def test_the_auto_trader_screens_at_the_boundary_before_gating():
    """A gate reading a missing field through `or 0` cannot tell an absent measurement from a
    real zero — so the boundary check must come first."""
    import inspect
    import auto_paper_cycle as apc
    src = inspect.getsource(apc._candidate_passes_minimum)
    assert "SELECT_CANDIDATE" in src
    assert src.index("SELECT_CANDIDATE") < src.index("REQUIRED_GATES")
