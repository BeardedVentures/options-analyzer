"""Ledger integrity: duplicate-open guard, dedup-by-underlying, and set_close P&L.

The duplicate guard exists because META 570/565 was logged twice 24s apart on 2026-07-13 and
stopped out twice at -$256.16 — 41% of realized losses at the time were a phantom double-count.
"""
import pytest

import config
from analysis import outcome_logger as ol


# ── duplicate-open guard ──────────────────────────────────────────────────────────────────────

def _open(**kw):
    args = dict(ticker="NVDA", short_strike=100.0, long_strike=95.0,
                expiration="2026-09-18", entry_credit_per_share=1.20,
                dte=35, delta=-0.22, implied_pop=0.78, source="test")
    args.update(kw)
    return ol.open_paper_trade(**args)


def test_opening_a_new_spread_succeeds(temp_ledger, read_ledger):
    tid = _open()
    assert tid
    assert len(read_ledger()) == 1


def test_duplicate_open_is_refused(temp_ledger, read_ledger):
    _open()
    with pytest.raises(ValueError, match="already open"):
        _open()
    assert len(read_ledger()) == 1, "duplicate must not be written"


def test_duplicate_detected_across_strike_types(temp_ledger, read_ledger):
    """Strikes arrive as float from candidate JSON and str from form posts."""
    _open()
    with pytest.raises(ValueError):
        _open(short_strike="100", long_strike="95")
    assert len(read_ledger()) == 1


def test_allow_duplicate_permits_deliberate_add(temp_ledger, read_ledger):
    _open()
    _open(allow_duplicate=True)
    assert len(read_ledger()) == 2


def test_different_expiration_is_not_a_duplicate(temp_ledger, read_ledger):
    _open()
    _open(expiration="2026-10-16")
    assert len(read_ledger()) == 2


def test_reopen_after_close_is_allowed(temp_ledger, read_ledger):
    """Same-day re-entry AFTER the prior position closed is legitimate."""
    tid = _open()
    ol.set_close(tid, 0.40, "win", "test-close")
    _open()
    rows = read_ledger()
    assert len(rows) == 2
    assert sum(1 for r in rows if r["status"] == "open") == 1


# ── pop provenance ────────────────────────────────────────────────────────────────────────────

def test_pop_source_records_true_pop(temp_ledger, read_ledger):
    _open(true_pop=0.84, implied_pop=0.78)
    r = read_ledger()[-1]
    assert r["pop_source"] == "true_pop"
    assert r["modeled_pop"] == 0.84


def test_pop_source_records_implied_fallback(temp_ledger, read_ledger):
    _open(true_pop=None, implied_pop=0.78)
    r = read_ledger()[-1]
    assert r["pop_source"] == "implied_pop"
    assert r["modeled_pop"] == 0.78


# ── set_close P&L ─────────────────────────────────────────────────────────────────────────────

def test_set_close_win_pnl_is_net_of_commission(temp_ledger, read_ledger):
    tid = _open(entry_credit_per_share=1.50)
    ol.set_close(tid, 0.50, "win", "auto-target-profit")
    r = read_ledger()[-1]
    gross = (1.50 - 0.50) * 100
    assert r["status"] == "closed"
    assert r["realized_gross_pl_per_contract"] == pytest.approx(gross)
    assert r["realized_net_pl_per_contract"] == pytest.approx(
        gross - ol._round_trip_cost_per_contract())


def test_set_close_loss_pnl_is_negative(temp_ledger, read_ledger):
    tid = _open(entry_credit_per_share=1.50)
    ol.set_close(tid, 2.25, "loss", "auto-stop-loss")   # 1.5x stop
    r = read_ledger()[-1]
    gross = (1.50 - 2.25) * 100
    assert r["realized_gross_pl_per_contract"] == pytest.approx(gross)
    assert r["realized_net_pl_per_contract"] < gross      # commission makes it worse
    assert r["realized_net_pl_per_contract"] < 0


def test_set_close_unknown_id_returns_false(temp_ledger):
    assert ol.set_close("NO-SUCH-TRADE", 1.0, "win", "x") is False


def test_set_close_marks_status_and_reason(temp_ledger, read_ledger):
    tid = _open()
    ol.set_close(tid, 0.60, "win", "auto-target-profit")
    r = read_ledger()[-1]
    assert r["status"] == "closed"
    assert r["outcome"] == "win"
    assert r["exit_reason"] == "auto-target-profit"
    assert r["exit_price"] == pytest.approx(0.60)


# ── dedup by underlying (board display) ───────────────────────────────────────────────────────

def test_deduplicate_by_underlying_keeps_highest_edge():
    from main import _deduplicate_by_underlying
    trades = [
        {"ticker": "SPY", "edge_score": 60, "strategy": "bull_put_spread"},
        {"ticker": "SPY", "edge_score": 82, "strategy": "iron_condor"},
        {"ticker": "QQQ", "edge_score": 55, "strategy": "bull_put_spread"},
    ]
    kept = _deduplicate_by_underlying(trades)
    by_ticker = {t["ticker"]: t for t in kept}
    assert len(kept) == 2
    assert by_ticker["SPY"]["edge_score"] == 82
