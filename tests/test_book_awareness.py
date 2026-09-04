"""Book awareness reads ONE store: the one the cohort counts from.

On 2026-09-04 it did not. It asked the JARVIS tower for open outcomes and got 200 rows across
25 distinct tickers -- WMT x30, GS x22, QCOM x18 -- against a real book of four. So 25 of a
54-name watchlist carried an ALREADY IN POSITION flag, wrong in both directions: 24 names
falsely held, and 3 of the 4 genuinely held missing.

Three audits classified this as display-only, because `already_in_position` reaches only
verdict text and two display paths and `ALLOW_SAME_TICKER` gates no code. That classification
was correct about the code and wrong about the consequence: the operator reads the board and
declines trades on it. These tests pin the store, not the plumbing.
"""
from pathlib import Path

import pytest

import main
from analysis import outcome_logger as ol


def _row(ticker, status="open", **kw):
    r = {"id": f"{ticker}-1", "ticker": ticker, "status": status,
         "short_strike": 100.0, "long_strike": 95.0, "spread_width": 5.0,
         "natural_credit_per_share": 1.0}
    r.update(kw)
    return r


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Write rows into the isolated outcome ledger conftest already redirects."""
    def _write(rows):
        ol._write_all(rows)
        return rows
    return _write


# ── the store ────────────────────────────────────────────────────────────────

def test_open_tickers_come_from_the_outcome_ledger(ledger):
    ledger([_row("NKE"), _row("AMGN"), _row("SPY", status="closed"),
            _row("WMT", status="modeled")])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    assert tickers == {"NKE", "AMGN"}
    assert len(rows) == 2


def test_modeled_and_closed_rows_are_not_open_positions(ledger):
    """`modeled` is a recommendation the board made, not a position anyone holds.

    178 of the ledger's rows are modeled. Counting them as held would reproduce the phantom
    book from a different direction.
    """
    ledger([_row("WMT", status="modeled"), _row("GS", status="closed"),
            _row("QCOM", status="filled")])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    assert tickers == set() and rows == []


def test_book_awareness_does_not_consult_the_network(ledger, monkeypatch):
    """The tower is not a second source of truth. If it is reachable it must not matter."""
    import urllib.request
    def boom(*a, **k):
        raise AssertionError("book awareness must not make a network call")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ledger([_row("NKE")])
    assert main._get_open_position_tickers(Path("logs"))[0] == {"NKE"}


def test_an_unreadable_ledger_yields_no_flags_and_never_raises(monkeypatch):
    """Advisory: a ledger that cannot be read must not take the scan down."""
    monkeypatch.setattr(ol, "load_records", lambda: (_ for _ in ()).throw(OSError("gone")))
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    assert tickers == set() and rows == []


# ── the flag ─────────────────────────────────────────────────────────────────

def test_only_genuinely_held_tickers_are_flagged(ledger):
    ledger([_row("NKE")])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    trades = [{"ticker": "NKE"}, {"ticker": "WMT"}]
    out, _ = main._annotate_book_awareness(trades, tickers, rows)
    held = {t["ticker"]: t["already_in_position"] for t in out}
    assert held == {"NKE": True, "WMT": False}
    assert any("ALREADY IN POSITION" in w for w in out[0].get("warnings", []))
    assert not out[1].get("warnings")


# ── book risk ────────────────────────────────────────────────────────────────

def test_book_risk_is_derived_when_the_row_carries_no_max_loss_field(ledger):
    """Outcome rows record width and credit, not max_loss.

    Reading only `max_loss_usd` returned $0.00 for a book of four real spreads -- which reads
    as "no exposure" rather than as "not computed", and that difference is the whole point of
    showing the number.
    """
    ledger([_row("NKE", spread_width=5.0, natural_credit_per_share=1.0)])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    _, risk = main._annotate_book_awareness([], tickers, rows)
    assert risk == pytest.approx(400.0)          # (5.00 - 1.00) * 100


def test_an_explicit_max_loss_field_still_wins(ledger):
    ledger([_row("NKE", max_loss_usd=250.0)])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    _, risk = main._annotate_book_awareness([], tickers, rows)
    assert risk == pytest.approx(250.0)


def test_an_unpriceable_row_is_counted_not_folded_in_as_zero(ledger, caplog):
    """A row with nothing to price from must make the total a FLOOR, and say so."""
    ledger([_row("NKE", spread_width=5.0, natural_credit_per_share=1.0),
            _row("AMGN", spread_width=None, natural_credit_per_share=None,
                 modeled_credit_per_share=None)])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    with caplog.at_level("WARNING"):
        _, risk = main._annotate_book_awareness([], tickers, rows)
    assert risk == pytest.approx(400.0)
    assert any("FLOOR" in r.message or "FLOOR" in str(r.msg) for r in caplog.records)


def test_a_call_side_width_does_not_produce_negative_risk(ledger):
    """Short strike below long on the call side made spread_width negative for 49 rows once."""
    ledger([_row("NKE", spread_width=-5.0, natural_credit_per_share=1.0)])
    tickers, rows = main._get_open_position_tickers(Path("logs"))
    _, risk = main._annotate_book_awareness([], tickers, rows)
    assert risk == pytest.approx(400.0)
