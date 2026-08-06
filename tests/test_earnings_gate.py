"""Earnings blackout on the auto-open path.

Selling a 25-45 DTE credit spread through an earnings print turns a probabilistic edge into a
binary event bet. On 2026-08-03, 7 of 50 watchlist names reported inside the blackout window —
two of them that same morning — with no gate in place.
"""
import datetime

import pytest

import config
import vega_candidates as vc
from conftest import make_candidate, make_gates


EXP = "2026-09-04"
D = datetime.date


# ── _earnings_clear ───────────────────────────────────────────────────────────────────────────

def test_earnings_after_expiry_is_clear():
    assert vc._earnings_clear(D(2026, 10, 28), EXP, is_etf=False) is True


def test_earnings_before_expiry_is_blocked():
    assert vc._earnings_clear(D(2026, 8, 26), EXP, is_etf=False) is False


def test_earnings_on_expiry_is_blocked():
    """Same-day earnings still lands inside the position's life."""
    assert vc._earnings_clear(D(2026, 9, 4), EXP, is_etf=False) is False


def test_etf_always_clear():
    assert vc._earnings_clear(None, EXP, is_etf=True) is True


def test_unknown_earnings_fails_closed_for_equities():
    """A missing date is a data gap; skipping one cycle beats selling into a print."""
    assert vc._earnings_clear(None, EXP, is_etf=False) is False


def test_malformed_expiration_fails_closed():
    assert vc._earnings_clear(D(2026, 10, 28), "not-a-date", is_etf=False) is False


def test_accepts_datetime_as_well_as_date():
    assert vc._earnings_clear(datetime.datetime(2026, 10, 28, 16, 0), EXP, is_etf=False) is True


# ── attach_earnings_gate ──────────────────────────────────────────────────────────────────────

def _cands(n=2):
    return [make_candidate(expiration=EXP, gates=make_gates()) for _ in range(n)]


def test_attach_sets_gate_and_counts_blocked():
    cands = _cands()
    blocked = vc.attach_earnings_gate(cands, "AMD", D(2026, 8, 26), is_etf=False)
    assert blocked == 2
    assert all(c["gates"]["earnings_clear"] is False for c in cands)


def test_attach_records_earnings_date():
    cands = _cands(1)
    vc.attach_earnings_gate(cands, "AMD", D(2026, 8, 26), is_etf=False)
    assert cands[0]["earnings_date"] == "2026-08-26"


def test_attach_refreshes_gate_counts():
    """gates_total must grow when the gate is added after build_candidates.

    build_candidates does NOT emit earnings_clear — attach_earnings_gate adds it — so the counts
    it computed are stale by one until this runs. Mirror that shape rather than the fixture's
    already-complete gates dict.
    """
    gates = make_gates()
    del gates["earnings_clear"]
    c = make_candidate(expiration=EXP, gates=gates,
                       gates_passed=len(gates), gates_total=len(gates))
    before = c["gates_total"]

    vc.attach_earnings_gate([c], "AMD", D(2026, 8, 26), is_etf=False)

    assert c["gates_total"] == before + 1
    assert c["gates_passed"] == sum(1 for v in c["gates"].values() if v)
    assert c["gates_passed"] == before          # the added gate failed, so passed count is flat


def test_attach_clear_case_blocks_nothing():
    cands = _cands()
    assert vc.attach_earnings_gate(cands, "MSFT", D(2026, 10, 28), is_etf=False) == 0
    assert all(c["gates"]["earnings_clear"] for c in cands)


def test_etf_candidates_pass_with_no_date():
    cands = _cands()
    assert vc.attach_earnings_gate(cands, "SPY", None, is_etf=True) == 0


# ── contract integration ──────────────────────────────────────────────────────────────────────

def test_earnings_clear_is_in_required_gates():
    assert "earnings_clear" in config.REQUIRED_GATES


def test_blocked_earnings_candidate_is_rejected_by_auto_open():
    import auto_paper_cycle as apc
    c = make_candidate(gates=make_gates(earnings_clear=False))
    assert apc._candidate_passes_minimum(c) is False


def test_kill_switch_exists():
    """A mass calendar outage must be recoverable without a code change."""
    assert hasattr(config, "EARNINGS_GATE_ENABLED")
