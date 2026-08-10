"""Earnings blackout on the auto-open path.

Selling a 25-45 DTE credit spread through an earnings print turns a probabilistic edge into a
binary event bet. On 2026-08-03, 7 of 50 watchlist names reported inside the blackout window —
two of them that same morning — with no gate in place.

These tests used to target vega_candidates._earnings_clear / attach_earnings_gate, which were
correct and fail-closed but had no production caller: the gate contract had consolidated into
analysis.assessment.evaluate_gates, whose implementation passed unknown earnings OPEN. The
suite was green for weeks while the live scan did the opposite of what it asserted. They now
test the implementation the scan actually reaches — see test_candidate_gates.py for the
reachability check that makes that drift detectable rather than invisible.
"""
import config
import pytest

from analysis import assessment as A
from conftest import make_candidate, make_gates


def _spread(dte=30):
    return {"dte": dte, "short_strike": 100.0, "side": "put"}


def _ctx(earnings_days=None, has_earnings=None):
    return {"ticker": "AMD", "earnings_days": earnings_days, "has_earnings": has_earnings}


# ── _earnings_clear ───────────────────────────────────────────────────────────────────────────

def test_earnings_after_expiry_is_clear():
    assert A._earnings_clear(_spread(dte=30), _ctx(earnings_days=45)) is True


def test_earnings_before_expiry_is_blocked():
    assert A._earnings_clear(_spread(dte=30), _ctx(earnings_days=12)) is False


def test_earnings_on_expiry_is_blocked():
    """Same-day earnings still lands inside the position's life."""
    assert A._earnings_clear(_spread(dte=30), _ctx(earnings_days=30)) is False


def test_earnings_today_is_blocked():
    assert A._earnings_clear(_spread(dte=30), _ctx(earnings_days=0)) is False


def test_declared_no_earnings_always_clear():
    """An index ETF has no print to look up; failing closed on it would empty the board for
    exactly the names the strategy is safest on."""
    assert A._earnings_clear(_spread(), _ctx(has_earnings=False)) is True


def test_unknown_earnings_fails_closed_for_equities():
    """A missing date is a data gap; skipping one cycle beats selling into a print.

    This is the assertion the orphaned implementation made and the live one did not.
    """
    assert A._earnings_clear(_spread(), _ctx(earnings_days=None)) is False


def test_unknown_has_earnings_still_fails_closed():
    """has_earnings=None means "we do not know whether it reports" — not "it does not"."""
    assert A._earnings_clear(_spread(), _ctx(earnings_days=None, has_earnings=None)) is False


def test_kill_switch_disables_the_gate(monkeypatch):
    """A mass calendar outage must be recoverable without a code change."""
    monkeypatch.setattr(config, "EARNINGS_GATE_ENABLED", False)
    assert A._earnings_clear(_spread(), _ctx(earnings_days=5)) is True


# ── context resolution ────────────────────────────────────────────────────────────────────────

def test_load_context_resolves_declared_has_earnings():
    """SPY is DECLARED as having no earnings, so the gate must clear it without a lookup."""
    ctx = A.load_context("SPY", price_data=None, puts=[], tech={})
    assert ctx["has_earnings"] is False
    assert A._earnings_clear(_spread(), ctx) is True


def test_load_context_marks_unknown_earnings_source():
    """A passing gate must be distinguishable from a gate that never found out — without this
    field the earnings-gap audit cannot be run even retroactively."""
    ctx = A.load_context("AMD", price_data=None, puts=[], tech={})
    assert ctx["earnings_source"] == "unknown"


def test_load_context_records_lookup_source():
    ctx = A.load_context("AMD", price_data=None, puts=[], tech={}, earnings_days=20)
    assert ctx["earnings_source"] == "lookup"


def test_load_context_records_declared_none_source():
    ctx = A.load_context("SPY", price_data=None, puts=[], tech={})
    assert ctx["earnings_source"] == "declared_none"


# ── contract integration ──────────────────────────────────────────────────────────────────────

def test_earnings_clear_is_in_required_gates():
    assert "earnings_clear" in config.REQUIRED_GATES


def test_evaluate_gates_emits_earnings_clear_from_ctx():
    """The gate the contract enforces is the one these tests exercise."""
    ctx = dict(_ctx(earnings_days=5), spot=110.0, levels={})
    g = A.evaluate_gates({**_spread(), "short_leg": {}, "pop": 0.8}, ctx)
    assert g["earnings_clear"] is False


def test_blocked_earnings_candidate_is_rejected_by_auto_open():
    import auto_paper_cycle as apc
    c = make_candidate(gates=make_gates(earnings_clear=False))
    assert apc._candidate_passes_minimum(c) is False


def test_kill_switch_exists():
    assert hasattr(config, "EARNINGS_GATE_ENABLED")


# ── The unknown-vs-far sentinel (2026-08-10) ──────────────────────────────────────────────────

def test_the_999_sentinel_must_not_reach_the_gate():
    """data.fundamentals.days_until_earnings returns 999 for an UNKNOWN date, documented as
    "safe — won't block trade". That is right for the edge score's earnings-safety component
    and exactly wrong for a gate whose entire job is to refuse what it does not know.

    If the scan collapses unknown to 999 before handing it over, the fail-closed branch below
    becomes unreachable and the gate silently reverts to passing every name it has no data for.
    """
    import inspect
    import vega_candidates as vc

    # 999 clears the gate — as "far away" should.
    assert A._earnings_clear(_spread(dte=30), _ctx(earnings_days=999)) is True
    # ...which is precisely why unknown must arrive as None, not as 999.
    assert A._earnings_clear(_spread(dte=30), _ctx(earnings_days=None)) is False

    src = inspect.getsource(vc.main)
    assert "if _edt is not None else None" in src, (
        "the scan must preserve an unknown earnings date as None; mapping it through "
        "days_until_earnings first turns 'we never found out' into '999 days away'")
