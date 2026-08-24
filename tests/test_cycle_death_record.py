"""A cycle that gets killed must leave evidence, not a gap.

21 of the 184 cycle runs since 2026-08-01 (11%) ended without a `Finished` line — no exit code,
no traceback, no Application-log crash event. On 2026-08-24 it was 2 of the 6 runs that did real
work, plus the 14:35 run that Task Scheduler killed at its repetition boundary. Every death is a
market-hours run: the 79 off-hours fires, which exit in a second on the market-closed guard,
have never died once.

The record that would name the killer is Microsoft-Windows-TaskScheduler/Operational, which is
disabled on this machine and needs elevation to enable. Until someone runs that command, this is
what can be captured without admin rights — and it is captured by the NEXT run, because a killed
process cannot run its own exit handler.
"""
import json
import os
import time

import pytest

import auto_paper_cycle as apc


@pytest.fixture
def cycle_paths(tmp_path, monkeypatch):
    """Point the lock, the death ledger and the cycle log at throwaway files."""
    monkeypatch.setattr(apc, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(apc, "LOCK_FILE", tmp_path / "auto_paper_cycle.lock")
    monkeypatch.setattr(apc, "DEATHS_FILE", tmp_path / "cycle_deaths.jsonl")
    monkeypatch.setattr(apc, "CYCLE_LOG", tmp_path / "auto_paper_cycle.log")
    lines = []
    monkeypatch.setattr(apc, "_log", lambda m: lines.append(str(m)))
    return tmp_path, lines


def _deaths(tmp_path):
    p = tmp_path / "cycle_deaths.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_a_lock_held_by_a_dead_pid_is_recorded_as_a_death(cycle_paths):
    """THE POINT. A killed run leaves its lock behind; the next run must say so."""
    tmp_path, lines = cycle_paths
    (tmp_path / "auto_paper_cycle.log").write_text(
        "line one\n[2026-08-24 14:35:01] Starting auto paper cycle\nlast thing it wrote\n",
        encoding="utf-8")
    # PID 1 is never a live VEGA cycle on Windows, and _pid_alive resolves it definitively.
    apc.LOCK_FILE.write_text("999999999", encoding="utf-8")

    assert apc._acquire_lock() is True, "a dead holder must not wedge the cycle"

    recs = _deaths(tmp_path)
    assert len(recs) == 1, f"expected one death record, got {recs}"
    r = recs[0]
    assert r["previous_pid"] == 999999999
    assert "no longer exists" in r["reason"]
    assert r["last_log_lines"][-1] == "last thing it wrote", \
        "the record must carry what the dead run was doing, or it is just a timestamp"
    assert any("PREVIOUS CYCLE DIED" in l for l in lines)


def test_an_old_lock_is_recorded_even_when_the_pid_is_unreadable(cycle_paths):
    """The 2026-08-24 14:35 shape: killed at the repetition boundary, lock left at mtime."""
    tmp_path, _lines = cycle_paths
    apc.LOCK_FILE.write_text("not-a-pid", encoding="utf-8")
    old = time.time() - 3600
    os.utime(apc.LOCK_FILE, (old, old))

    assert apc._acquire_lock() is True
    recs = _deaths(tmp_path)
    assert len(recs) == 1
    assert recs[0]["silent_for_seconds"] >= 3500


def test_a_live_holder_still_blocks_and_is_not_called_a_death(cycle_paths):
    """The guard must not turn a healthy overlapping run into a fake death record.

    This is the half that makes the test above mean something: if _acquire_lock recorded a death
    unconditionally, the first test would pass and the mechanism would be worthless.
    """
    tmp_path, lines = cycle_paths
    apc.LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")   # this very process is alive

    assert apc._acquire_lock() is False, "a live holder must still win the lock"
    assert _deaths(tmp_path) == []
    assert any("Another cycle appears active" in l for l in lines)


def test_pid_alive_distinguishes_this_process_from_a_dead_one():
    """_pid_alive must be capable of returning both answers, or the tests above are theatre."""
    assert apc._pid_alive(os.getpid()) is True
    assert apc._pid_alive(999999999) is False
