"""Forward-looking caps on how entries may cluster, and the silent branch that hid why they didn't.

Why these exist. The cohort's premise is 30 INDEPENDENT observations; the ledger says it was not
getting them. 65 of 79 paper entries (82%) were opened in the same MINUTE as at least one other,
in batches of up to five, and 10 of the 11 open positions share one expiration. Four spreads
opened from a single board snapshot are four readings of one market moment — same regime, same
news, same scan's data quality, increasingly the same settlement date. That inflates apparent
sample size without adding information.

The caps are forward-looking only: they gate NEW opens and never touch an existing position, and
they do not redefine what counts as a valid observation.

The last section is a different bug found while writing these. Between 2026-08-06 and 08-19,
eleven cycles ran the open path and logged NOTHING — no open, no rejection, no reason. Cause: the
board kept re-qualifying tickers the desk already held, every one hit an unlogged `continue`, and
the result was indistinguishable in the log from a genuinely empty board. Two very different
problems ("the market offers nothing" vs "our book is saturated") wrote the same blank line, and
the first reading is what a week of session notes recorded.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import auto_paper_cycle as apc
import config
from analysis import outcome_logger as ol


def board_trade(ticker, short=100.0, long=95.0, exp="2026-12-18", edge=50):
    """A board row that passes every check, so a test isolates the one cap it targets."""
    return {
        "ticker": ticker, "strategy": "bull_put_spread",
        "short_strike": short, "long_strike": long, "expiration": exp,
        "dte": 45, "delta": -0.20, "edge_score": edge,
        "natural_credit_per_share": 1.00, "credit_per_share": 1.10,
        "fill_basis": "live", "iv_rank": 60, "implied_pop": 0.80, "true_pop": 0.80,
        "assessment_gates": {k: True for k in config.REQUIRED_GATES},
    }


@pytest.fixture
def desk(tmp_path, monkeypatch, temp_ledger):
    """Point the opener at a throwaway board + ledger and capture its log."""
    lines = []
    monkeypatch.setattr(apc, "_log", lambda m: lines.append(str(m)))

    board_file = tmp_path / "scan_latest.json"
    monkeypatch.setattr(apc, "BOARD_FILE", board_file)

    def set_board(trades):
        board_file.write_text(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "market_context": {"vix": {"current": 15.2}},
            "qualified_trades": trades,
        }), encoding="utf-8")

    # Env overrides would mask the config defaults these tests are about.
    for var in ("VEGA_MAX_NEW_PER_RUN", "VEGA_MAX_NEW_PER_DAY", "VEGA_MAX_OPEN_TOTAL"):
        monkeypatch.delenv(var, raising=False)
    return set_board, lines


def opened_tickers(read_ledger):
    return [r["ticker"] for r in read_ledger() if r.get("status") == "open"]


# ── per-run cap ───────────────────────────────────────────────────────────────────────────────

def test_a_single_run_cannot_open_a_batch(desk, read_ledger, monkeypatch):
    """THE REGRESSION. Six qualified names, one board, one minute.

    Under the previous undocumented default of 5 this opened five positions sharing a timestamp —
    the shape that produced the 2026-08-10 four-trade batch with nothing objecting.
    """
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 2)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 99)
    set_board, _ = desk
    set_board([board_trade(t, exp=f"2026-1{i}-18") for i, t in enumerate("ABCDEF", start=1)])

    opened = apc._auto_open_from_board()

    assert opened == 2, "a single board snapshot must not become a batch of six"
    assert len(opened_tickers(read_ledger)) == 2


def test_the_per_run_cap_reads_config_not_a_bare_literal(desk, read_ledger, monkeypatch):
    """The number has to be tunable in one visible place, not buried in an env default."""
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 1)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 99)
    set_board, _ = desk
    set_board([board_trade(t, exp=f"2026-1{i}-18") for i, t in enumerate("ABC", start=1)])
    assert apc._auto_open_from_board() == 1


def test_an_env_override_still_wins(desk, monkeypatch):
    """Operational override survives — the config value is the default, not a ceiling."""
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 1)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 99)
    monkeypatch.setenv("VEGA_MAX_NEW_PER_RUN", "3")
    set_board, _ = desk
    set_board([board_trade(t, exp=f"2026-1{i}-18") for i, t in enumerate("ABCD", start=1)])
    assert apc._auto_open_from_board() == 3


# ── per-day cap ───────────────────────────────────────────────────────────────────────────────

def test_the_day_cap_counts_positions_already_opened_today(desk, read_ledger, monkeypatch):
    """A per-RUN cap alone is not a diversification rule.

    With hourly cycles, a cap of 2 per run still permits 14 entries inside one session — which is
    one volatility regime, one news cycle and one day's data quality. The day cap is the one that
    actually separates observations.
    """
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 2)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 3)
    set_board, lines = desk

    set_board([board_trade(t, exp=f"2026-1{i}-18") for i, t in enumerate("AB", start=1)])
    assert apc._auto_open_from_board() == 2

    set_board([board_trade(t, exp=f"2026-1{i}-18") for i, t in enumerate("CD", start=1)])
    assert apc._auto_open_from_board() == 1, "the third open exhausts the day"

    set_board([board_trade("E", exp="2026-11-20")])
    assert apc._auto_open_from_board() == 0
    assert any("Daily entry cap reached" in l for l in lines)


def test_yesterdays_entries_do_not_consume_todays_budget(desk, read_ledger, monkeypatch):
    """The cap is per day, not a rolling window — a stale count would freeze entries forever."""
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 1)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 2)
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    tid = ol.open_paper_trade(ticker="OLD", short_strike=50.0, long_strike=45.0,
                              expiration="2026-12-18", entry_credit_per_share=1.0, dte=45,
                              fill_model="natural", source="test")
    rows = ol._read_all()
    for r in rows:
        if r["id"] == tid:
            r["opened_at"] = yesterday
    ol._write_all(rows)

    set_board, _ = desk
    set_board([board_trade("NEW", exp="2026-11-20")])
    assert apc._auto_open_from_board() == 1


# ── expiration-concentration cap ──────────────────────────────────────────────────────────────

def test_one_expiration_cannot_absorb_the_whole_book(desk, read_ledger, monkeypatch):
    """The correlation the original brief never surfaced.

    10 of 11 live positions settle on 2026-09-18. Distinct tickers and distinct entry days do not
    make those independent: one gap through a shared settlement week resolves the entire book at
    once, and per-trade views cannot see it.
    """
    monkeypatch.setattr(config, "MAX_OPEN_PER_EXPIRATION", 2)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 9)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    set_board, lines = desk
    set_board([board_trade(t, exp="2026-12-18") for t in "ABCD"])

    opened = apc._auto_open_from_board()

    assert opened == 2
    assert any("already expire 2026-12-18" in l for l in lines)


def test_the_cap_counts_positions_that_were_already_open(desk, read_ledger, monkeypatch):
    """It must read the live book, not just this run's tally, or it resets every cycle."""
    monkeypatch.setattr(config, "MAX_OPEN_PER_EXPIRATION", 2)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 9)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    ol.open_paper_trade(ticker="HELD1", short_strike=50.0, long_strike=45.0,
                        expiration="2026-12-18", entry_credit_per_share=1.0, dte=45,
                        fill_model="natural", source="test")
    ol.open_paper_trade(ticker="HELD2", short_strike=60.0, long_strike=55.0,
                        expiration="2026-12-18", entry_credit_per_share=1.0, dte=45,
                        fill_model="natural", source="test")
    set_board, _ = desk
    set_board([board_trade("NEW", exp="2026-12-18")])

    assert apc._auto_open_from_board() == 0


def _backdate_all_open(before=None):
    """Move every open position's opened_at to before the entry-rules epoch.

    Reaches into the (throwaway) ledger rather than going through open_paper_trade, which always
    stamps 'now' — the legacy book this models was written weeks before the caps existed.
    """
    before = before or "2026-08-06T09:38:00"
    rows = ol._read_all()
    for r in rows:
        if r.get("status") == "open":
            r["opened_at"] = before
    ol._write_all(rows)


def test_the_legacy_book_does_not_spend_the_new_cohorts_expiration_budget(desk, read_ledger,
                                                                          monkeypatch):
    """THE DROUGHT, 2026-08-21 → 2026-08-24.

    Six of seven open positions expire 2026-09-18. All seven were opened 08-04 to 08-07, all are
    gate_basis 'mid', and not one is in the cohort being validated. Counting them against
    MAX_OPEN_PER_EXPIRATION meant every board candidate was refused —
    "SKIP CRWD — 6 position(s) already expire 2026-09-18, cap is 4" — on every cycle for four
    days. The cap added to stop correlated entries had become the reason there were no entries.

    A position from a previous entry regime is not an observation in this cohort, so it cannot
    make this cohort's observations correlated.
    """
    monkeypatch.setattr(config, "MAX_OPEN_PER_EXPIRATION", 4)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 9)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    for i in range(6):
        ol.open_paper_trade(ticker=f"OLD{i}", short_strike=50.0 + i, long_strike=45.0 + i,
                            expiration="2026-09-18", entry_credit_per_share=1.0, dte=45,
                            fill_model="natural", source="test")
    _backdate_all_open()

    set_board, lines = desk
    set_board([board_trade("CRWD", exp="2026-09-18")])

    assert apc._auto_open_from_board() == 1, (
        "a legacy position from a previous entry regime must not block the current cohort")
    assert "CRWD" in opened_tickers(read_ledger)
    assert any("predate" in l for l in lines), "the exclusion must be logged, not silent"


def test_the_cap_still_binds_within_the_current_epoch(desk, read_ledger, monkeypatch):
    """The other half. If the fix above simply stopped counting, the cap would be dead.

    Same six positions on one expiration — but opened under the CURRENT rules this time. These
    are cohort observations, they are correlated, and the cap must refuse.
    """
    monkeypatch.setattr(config, "MAX_OPEN_PER_EXPIRATION", 4)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 9)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    for i in range(6):
        ol.open_paper_trade(ticker=f"CUR{i}", short_strike=50.0 + i, long_strike=45.0 + i,
                            expiration="2026-09-18", entry_credit_per_share=1.0, dte=45,
                            fill_model="natural", source="test")
    # deliberately NOT back-dated

    set_board, lines = desk
    set_board([board_trade("CRWD", exp="2026-09-18")])

    assert apc._auto_open_from_board() == 0
    assert any("already expire 2026-09-18" in l for l in lines)


def test_a_different_expiration_is_still_free(desk, read_ledger, monkeypatch):
    """The cap steers entries apart; it must not become a general stop on trading."""
    monkeypatch.setattr(config, "MAX_OPEN_PER_EXPIRATION", 1)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 9)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    set_board, _ = desk
    set_board([board_trade("A", exp="2026-12-18"), board_trade("B", exp="2026-12-18"),
               board_trade("C", exp="2027-01-15")])

    apc._auto_open_from_board()

    assert sorted(opened_tickers(read_ledger)) == ["A", "C"]


def test_the_caps_never_touch_an_existing_position(desk, read_ledger, monkeypatch):
    """Forward-looking only. A book already past the cap is left exactly as it is.

    The live book is at 10 on one expiration against a cap of 4. If the cap reached backwards it
    would close six positions to satisfy a rule they were never opened under — destroying the
    cohort it was added to protect.
    """
    monkeypatch.setattr(config, "MAX_OPEN_PER_EXPIRATION", 1)
    for i in range(3):
        ol.open_paper_trade(ticker=f"HELD{i}", short_strike=50.0 + i, long_strike=45.0 + i,
                            expiration="2026-12-18", entry_credit_per_share=1.0, dte=45,
                            fill_model="natural", source="test")
    before = read_ledger()
    set_board, _ = desk
    set_board([board_trade("NEW", exp="2026-12-18")])

    apc._auto_open_from_board()

    after = read_ledger()
    assert len(after) == len(before)
    assert all(r["status"] == "open" for r in after)


# ── the silent branch ─────────────────────────────────────────────────────────────────────────

def test_a_saturated_book_no_longer_looks_like_an_empty_board(desk, read_ledger, monkeypatch):
    """The bug this section exists for.

    Eleven cycles between 08-06 and 08-19 ran the open path and produced not one line. The board
    had qualified trades; they were all for tickers already held; each hit an unlogged `continue`.
    In the log that is identical to "the market offered nothing" — and that is the reading a week
    of session notes recorded. Distinguishing the two changes the diagnosis completely.
    """
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 5)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    ol.open_paper_trade(ticker="META", short_strike=540.0, long_strike=535.0,
                        expiration="2026-09-18", entry_credit_per_share=0.85, dte=43,
                        fill_model="natural", source="test")
    set_board, lines = desk
    set_board([board_trade("META", short=540.0, long=535.0, exp="2026-09-18")])

    opened = apc._auto_open_from_board()

    assert opened == 0
    blob = "\n".join(lines)
    assert "already holds" in blob, "a saturated book must not log as an empty board"
    assert "META" in blob
    assert "saturated book, not an empty board" in blob


def test_a_genuinely_empty_board_still_says_so(desk):
    """The other half of the distinction — the two states must not collapse the other way."""
    set_board, lines = desk
    set_board([])
    assert apc._auto_open_from_board() == 0
    assert any("no qualified trades" in l for l in lines)


def test_a_board_whose_trades_are_all_ungated_says_which(desk, monkeypatch):
    """A third state that used to be silent when it coincided with an already-open ticker."""
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_RUN", 5)
    monkeypatch.setattr(config, "MAX_NEW_OPENS_PER_DAY", 9)
    t = board_trade("ZZZ", exp="2026-12-18")
    t["assessment_gates"] = {}
    set_board, lines = desk
    set_board([t])

    assert apc._auto_open_from_board() == 0
    blob = "\n".join(lines)
    assert "not fully gated" in blob
    assert "none was openable" in blob
