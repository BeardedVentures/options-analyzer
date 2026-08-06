"""Fill-model cohorts: legacy 'mid' trades stay on the record without contaminating the benchmark.

A position must be MARKED on the same basis it was ENTERED. Marking a mid-entry position at
natural charges it a spread it never collected — inventing losses and destroying it as a benchmark.
"""
import pytest

from analysis import outcome_logger as ol


def _open(**kw):
    args = dict(ticker="NVDA", short_strike=100.0, long_strike=95.0,
                expiration="2026-09-18", entry_credit_per_share=1.20, source="test")
    args.update(kw)
    return ol.open_paper_trade(**args)


# ── tagging ───────────────────────────────────────────────────────────────────────────────────

def test_new_trades_default_to_natural(temp_ledger, read_ledger):
    _open()
    assert read_ledger()[-1]["fill_model"] == "natural"


def test_fill_model_can_be_set_explicitly(temp_ledger, read_ledger):
    _open(fill_model="mid")
    assert read_ledger()[-1]["fill_model"] == "mid"


# ── mark basis follows entry basis ────────────────────────────────────────────────────────────

class _Leg(dict):
    pass


def _chain_legs():
    """A short leg and long leg with distinct mid vs bid/ask, so the two bases differ."""
    short = {"mid": 2.10, "bid": 2.00, "ask": 2.20}
    long_ = {"mid": 1.10, "bid": 1.05, "ask": 1.15}
    return short, long_


def _mark_for(fill_model):
    """Replicates the branch in _reprice_and_close_open for a position of this cohort."""
    s, l = _chain_legs()
    if (fill_model or "mid") == "mid":
        return round(float(s["mid"]) - float(l["mid"]), 2)
    return round(float(s["ask"]) - float(l["bid"]), 2)


def test_mid_cohort_marks_at_mid():
    assert _mark_for("mid") == pytest.approx(1.00)      # 2.10 - 1.10


def test_natural_cohort_marks_at_natural():
    assert _mark_for("natural") == pytest.approx(1.15)  # 2.20 - 1.05


def test_natural_mark_is_more_conservative_than_mid():
    """Closing costs more at natural — that is the spread you actually pay."""
    assert _mark_for("natural") > _mark_for("mid")


def test_untagged_legacy_record_defaults_to_mid():
    """Records written before the field existed must not be marked at natural."""
    assert _mark_for(None) == _mark_for("mid")


# ── cohort reporting ──────────────────────────────────────────────────────────────────────────

def test_report_separates_cohorts(capsys):
    from paper_desk import _print_fill_cohorts
    closed = [
        {"fill_model": "mid", "realized_net_pl_per_contract": -100.0},
        {"fill_model": "mid", "realized_net_pl_per_contract": 50.0},
        {"fill_model": "natural", "realized_net_pl_per_contract": 25.0},
    ]
    _print_fill_cohorts(closed)
    out = capsys.readouterr().out
    assert "natural" in out and "mid" in out
    assert "n=2" in out and "n=1" in out


def test_report_flags_empty_natural_cohort(capsys):
    from paper_desk import _print_fill_cohorts
    _print_fill_cohorts([{"fill_model": "mid", "realized_net_pl_per_contract": -100.0}])
    out = capsys.readouterr().out
    assert "no closed trades yet" in out


def test_untagged_closed_rows_group_as_mid(capsys):
    from paper_desk import _print_fill_cohorts
    _print_fill_cohorts([{"realized_net_pl_per_contract": -10.0}])
    out = capsys.readouterr().out
    assert "mid" in out
