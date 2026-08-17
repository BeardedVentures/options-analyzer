"""Both fill prices are recorded, so the mid-vs-natural gap is measurable per trade."""
import pytest

from analysis import outcome_logger as ol


def _open(**kw):
    args = dict(ticker="NVDA", short_strike=100.0, long_strike=95.0,
                expiration="2026-09-18", entry_credit_per_share=0.90, source="test")
    args.update(kw)
    return ol.open_paper_trade(dte=35, **args)


def test_both_fill_prices_are_stored(temp_ledger, read_ledger):
    _open(natural_credit_per_share=0.90, mid_credit_per_share=1.20)
    r = read_ledger()[-1]
    assert r["natural_credit_per_share"] == 0.90
    assert r["mid_credit_per_share"] == 1.20


def test_trade_is_scored_on_the_natural_fill(temp_ledger, read_ledger):
    """actual_fill_credit — what P&L is computed from — must be the natural price."""
    _open(entry_credit_per_share=0.90, natural_credit_per_share=0.90, mid_credit_per_share=1.20)
    r = read_ledger()[-1]
    assert r["actual_fill_credit"] == 0.90
    assert r["actual_fill_credit"] != r["mid_credit_per_share"]


def test_fill_gap_is_derivable(temp_ledger, read_ledger):
    _open(natural_credit_per_share=0.90, mid_credit_per_share=1.20)
    r = read_ledger()[-1]
    gap = (r["mid_credit_per_share"] - r["natural_credit_per_share"]) / r["mid_credit_per_share"]
    assert gap == pytest.approx(0.25)


def test_fields_default_to_none_when_not_supplied(temp_ledger, read_ledger):
    """Legacy/manual entries have no recoverable fill detail — null, not zero."""
    _open()
    r = read_ledger()[-1]
    assert r["natural_credit_per_share"] is None
    assert r["mid_credit_per_share"] is None
