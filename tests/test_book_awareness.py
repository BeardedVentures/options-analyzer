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


# ── decay alerts: the second consumer of the same absent file ────────────────
#
# `compute_decay_alerts` read open_positions.json too, which has never existed, so it returned
# [] on every close scan and NEVER FIRED. Silently -- an empty alert list is indistinguishable
# from "nothing has decayed yet". Found only because book awareness was fixed first.

def _pos(ticker="NKE", entry=1.00, mark=0.20, **kw):
    r = {"ticker": ticker, "status": "open", "strategy": "bull_put_spread",
         "short_strike": 100.0, "long_strike": 95.0, "expiration": "2026-09-18",
         "actual_fill_credit": entry, "current_mark": mark, "mark_status": "LIVE"}
    r.update(kw)
    return r


def test_decay_alerts_read_ledger_field_names():
    """entry_credit -> actual_fill_credit, current_price -> current_mark."""
    alerts = main.compute_decay_alerts([_pos(entry=1.00, mark=0.20)])   # 80% of max
    assert len(alerts) == 1 and alerts[0]["ticker"] == "NKE"


def test_a_position_short_of_target_does_not_alert():
    import config
    entry, mark = 1.00, 1.00 * (1 - config.TARGET_PROFIT_PCT) + 0.10
    assert main.compute_decay_alerts([_pos(entry=entry, mark=mark)]) == []


def test_a_stale_mark_never_produces_a_decay_alert():
    """AMGN sat at MARK-UNAVAILABLE for five days on a last-good mark of 0.91.

    Alerting off that would tell the operator to close a position on a price nobody is quoting
    -- and `_reprice_and_close_open` already refuses to evaluate stop/target in that state, so
    an alert path that ignored it would contradict the engine.
    """
    stale = _pos(entry=1.00, mark=0.05, mark_status="DATA_UNAVAILABLE")
    assert main.compute_decay_alerts([stale]) == []


def test_a_row_predating_the_mark_instrumentation_still_alerts():
    """Absence of mark_status is not a stated problem. Fail open on shape, closed on status."""
    old = _pos(entry=1.00, mark=0.20)
    del old["mark_status"]
    assert len(main.compute_decay_alerts([old])) == 1


def test_the_natural_credit_is_the_fallback_entry_basis():
    r = _pos(entry=None, mark=0.20)
    r["actual_fill_credit"] = None
    r["natural_credit_per_share"] = 1.00
    assert len(main.compute_decay_alerts([r])) == 1


def test_a_row_with_no_usable_price_is_skipped_not_crashed():
    r = _pos(entry=None, mark=None)
    r["actual_fill_credit"] = None
    r["natural_credit_per_share"] = None
    r["current_mark"] = None
    assert main.compute_decay_alerts([r]) == []


def test_the_dead_file_reader_is_gone():
    """One absent path, two consumers, found separately. Neither may come back.

    Checked against the parsed source with DOCSTRINGS EXCLUDED, because the comments explaining
    why the file is gone name it deliberately -- an assertion its own documentation can trip is
    an assertion that gets deleted rather than fixed.
    """
    import ast
    assert not hasattr(main, "load_open_positions")
    tree = ast.parse(open(main.__file__, encoding="utf-8-sig").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            assert "open_positions.json" not in node.value, (
                f"main.py still references the absent file in code: {node.value!r}")
