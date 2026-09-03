"""data_quality_log: JSONL append, bounded tail reads, age-based retention.

WHY THE FORMAT CHANGED. The retention window could not simply be raised. _append_locked was a
full read-modify-write -- read every row, append, truncate, json.dump the whole list with
indent=2, atomically replace -- PER ROW, under a lock, from several concurrent cycle processes.
At the measured 1,391 rows/trading day a 120-day window under that writer would have meant
rewriting tens of megabytes on every single append, inside a cycle with ~28 minutes of headroom
against PT45M. Trading a silent evidence loss for a silent runtime creep is not a fix.

WHY THE WINDOW CHANGED. MAX_ROWS = 5000 was reasoned from "~56 rows per scan, several times a
day". Actual: 1,391/trading day, so the window was 3.6 TRADING DAYS, and the file already
covered only 08-31..09-03 -- having quietly eaten the 08-27..08-30 provenance that
outcome_logger.entry_vendor_basis pointed at as the recovery path. Retention on a provenance
record is now sized against the longest lookback that might query it.
"""
import json
import os

import pytest

from data import data_quality_log as dq


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "LOG_FILE", str(tmp_path / "q.jsonl"))
    monkeypatch.setattr(dq, "LEGACY_LOG_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(dq, "LOCK_FILE", str(tmp_path / "q.lock"), raising=False)
    return tmp_path


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


# ── the writer ────────────────────────────────────────────────────────────────

def test_a_write_APPENDS_rather_than_rewriting(log):
    """The whole runtime argument. If this regresses to a rewrite, the retention window becomes
    a serialization bottleneck in the live cycle again."""
    dq.record("AAA", "robinhood", raw_count=10, usable_count=9)
    size_after_one = os.path.getsize(dq.LOG_FILE)
    dq.record("BBB", "robinhood", raw_count=10, usable_count=9)
    size_after_two = os.path.getsize(dq.LOG_FILE)

    assert size_after_two > size_after_one, "second write must extend the file"
    # A rewrite of two rows would be smaller than one row plus a whole second copy; an append is
    # almost exactly one row larger.
    assert size_after_two < size_after_one * 2 + 200
    assert len(_rows(dq.LOG_FILE)) == 2


def test_one_corrupt_line_costs_one_reading_not_the_file(log):
    """A corrupt byte in an array file poisons every row after it -- this log was destroyed five
    times that way between 2026-08-13 and 2026-08-25. In JSONL the blast radius is one line."""
    dq.record("AAA", "robinhood", raw_count=10, usable_count=9)
    with open(dq.LOG_FILE, "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
    dq.record("CCC", "robinhood", raw_count=10, usable_count=9)

    rows = dq._read_all()
    assert [r["ticker"] for r in rows] == ["AAA", "CCC"]


# ── bounded reads ─────────────────────────────────────────────────────────────

def test_read_recent_returns_the_TAIL(log):
    for i in range(50):
        dq.record(f"T{i:03d}", "robinhood", raw_count=10, usable_count=9)
    tail = dq.read_recent(5)
    assert [r["ticker"] for r in tail] == ["T045", "T046", "T047", "T048", "T049"]


def test_the_tail_reader_does_not_read_the_whole_file(log, monkeypatch):
    """Guards the performance claim rather than asserting it in a comment: _read_tail must not
    be implemented as _read_all()[-n:], which is what it replaced."""
    for i in range(200):
        dq.record(f"T{i:03d}", "robinhood", raw_count=10, usable_count=9)
    monkeypatch.setattr(dq, "_read_all",
                        lambda: pytest.fail("_read_tail must not call _read_all"))
    assert len(dq.read_recent(3)) == 3


def test_a_tail_larger_than_the_file_returns_everything(log):
    dq.record("AAA", "robinhood", raw_count=10, usable_count=9)
    assert len(dq.read_recent(10_000)) == 1


def test_reading_an_absent_log_is_empty_not_an_error(log):
    assert dq.read_recent(10) == []
    assert dq._read_all() == []


# ── retention ─────────────────────────────────────────────────────────────────

def test_compaction_drops_rows_older_than_the_window(log):
    dq.record("OLD", "robinhood", raw_count=10, usable_count=9)
    dq.record("NEW", "robinhood", raw_count=10, usable_count=9)
    # Backdate the first row past the window.
    rows = dq._read_all()
    rows[0]["timestamp"] = "2020-01-01T00:00:00"
    with open(dq.LOG_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    res = dq.compact(max_age_days=30)

    assert res["dropped_age"] == 1
    assert [r["ticker"] for r in dq._read_all()] == ["NEW"]


def test_compaction_keeps_everything_inside_the_window(log):
    """The other direction -- otherwise the test above only proves it can delete."""
    for i in range(5):
        dq.record(f"T{i}", "robinhood", raw_count=10, usable_count=9)
    res = dq.compact(max_age_days=120)
    assert res["dropped_age"] == 0 and res["after"] == 5


def test_the_row_cap_is_a_backstop_applied_AFTER_age(log):
    """Age is the policy. The cap exists only so a runaway writer cannot fill the disk between
    compactions, and it must not be what normally decides retention."""
    for i in range(10):
        dq.record(f"T{i}", "robinhood", raw_count=10, usable_count=9)
    res = dq.compact(max_age_days=120, max_rows=4)
    assert res["dropped_age"] == 0
    assert res["dropped_cap"] == 6
    assert [r["ticker"] for r in dq._read_all()] == ["T6", "T7", "T8", "T9"]


def test_the_window_is_sized_against_a_trade_lifecycle_not_disk():
    """The failure being prevented: 5000 rows was reasoned as 'a quarter' and was really 3.6
    trading days. MAX_DTE is 45, so a trade plus the lag before anyone analyses it has to fit."""
    import config
    assert dq.MAX_AGE_DAYS >= 2 * int(getattr(config, "MAX_DTE", 45))


# ── migration ─────────────────────────────────────────────────────────────────

def test_the_legacy_array_file_is_migrated_once_and_preserved(log):
    legacy = dq.LEGACY_LOG_FILE
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump([{"ticker": "OLD1", "timestamp": "2026-09-01T00:00:00"},
                   {"ticker": "OLD2", "timestamp": "2026-09-02T00:00:00"}], f)

    rows = dq._read_all()

    assert [r["ticker"] for r in rows] == ["OLD1", "OLD2"]
    assert not os.path.exists(legacy), "the legacy file must be moved aside"
    assert os.path.exists(legacy + ".migrated"), "and preserved, not deleted"


def test_migration_does_not_re_run_once_jsonl_exists(log):
    dq.record("NEW", "robinhood", raw_count=10, usable_count=9)
    with open(dq.LEGACY_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump([{"ticker": "STALE"}], f)
    assert [r["ticker"] for r in dq._read_all()] == ["NEW"]


def test_a_zero_row_cap_keeps_NOTHING_rather_than_everything(log):
    """`kept[-0:]` is `kept[0:]` -- the whole list. Without an explicit guard, max_rows=0
    silently keeps every row AND reports nothing dropped, which is no trim and no complaint.
    Found by an existing test failing for the wrong reason on 2026-09-03."""
    for i in range(3):
        dq.record(f"T{i}", "robinhood", raw_count=10, usable_count=9)
    res = dq.compact(max_age_days=120, max_rows=0)
    assert res["after"] == 0 and res["dropped_cap"] == 3
    assert dq._read_all() == []
