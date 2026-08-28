"""durable_write: the one place VEGA decides how a file gets replaced.

Two separate incidents produced this module, and each test below pins one of them.

data/data_quality_log.json was destroyed five times between 2026-08-13 and 2026-08-25 (the last
wipe: 886 KB -> 10 KB, gitignored, unrecoverable). logs/scan_log.json corrupted on 2026-07-08 and
the wreckage is still on disk as scan_log.json.corrupt.bak. Both were read-modify-write through a
SHARED temp path, from processes that run concurrently every cycle.

The subtle half is the lock. Per-process temp paths alone stop the corruption but not the losses:
on Windows os.replace fails outright while another process holds the destination open, so a
five-writer stress run still dropped about a fifth of its records after five retries each.
Retrying cannot fix read-modify-write -- the read is already stale by the time the write is
refused -- so the test that matters is the one asserting two writers cannot overlap.
"""
import os

import pytest

import durable_write as dw


def test_the_temp_path_is_unique_per_process(tmp_path, monkeypatch):
    seen = []
    real = dw.replace_with_retry

    def spy(src, dst, **kw):
        seen.append(str(src))
        return real(src, dst, **kw)

    monkeypatch.setattr(dw, "replace_with_retry", spy)
    target = tmp_path / "f.json"
    monkeypatch.setattr(dw.os, "getpid", lambda: 1111)
    dw.atomic_write_text(target, "a")
    monkeypatch.setattr(dw.os, "getpid", lambda: 2222)
    dw.atomic_write_text(target, "b")
    assert len(set(seen)) == 2, "two writers chose the same temp path: %s" % seen


def test_a_locked_destination_is_retried_not_dropped(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(32, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(dw.os, "replace", flaky)
    target = tmp_path / "f.json"
    dw.atomic_write_text(target, "written")
    assert calls["n"] == 3
    assert target.read_text(encoding="utf-8") == "written"


def _always_denied(src, dst):
    raise OSError(5, "Access is denied")


def test_a_failed_write_leaves_the_original_untouched(tmp_path, monkeypatch):
    target = tmp_path / "f.json"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(dw, "REPLACE_ATTEMPTS", 1)
    monkeypatch.setattr(dw.os, "replace", _always_denied)
    with pytest.raises(OSError):
        dw.atomic_write_text(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"


def test_no_temp_file_survives_a_failed_write(tmp_path, monkeypatch):
    target = tmp_path / "f.json"
    monkeypatch.setattr(dw, "REPLACE_ATTEMPTS", 1)
    monkeypatch.setattr(dw.os, "replace", _always_denied)
    with pytest.raises(OSError):
        dw.atomic_write_text(target, "x")
    assert not list(tmp_path.glob("*.tmp"))


def test_two_writers_cannot_hold_the_lock_at_once(tmp_path):
    """The one that actually stops data loss. Everything else only stops corruption."""
    target = tmp_path / "f.json"
    with dw.exclusive(target):
        with pytest.raises(TimeoutError):
            with dw.exclusive(target, timeout=0.2):
                pass


def test_the_lock_is_released_when_the_body_raises(tmp_path):
    target = tmp_path / "f.json"
    with pytest.raises(ValueError):
        with dw.exclusive(target):
            raise ValueError("boom")
    with dw.exclusive(target, timeout=0.2):
        pass


def test_a_stale_lock_is_broken_not_waited_on(tmp_path, monkeypatch):
    """A killed cycle must not wedge every write that follows it."""
    target = tmp_path / "f.json"
    open(str(target) + ".lock", "w").close()
    monkeypatch.setattr(dw, "LOCK_STALE_SECONDS", -1)
    with dw.exclusive(target, timeout=0.5):
        pass


def test_locks_on_different_files_do_not_block_each_other(tmp_path):
    with dw.exclusive(tmp_path / "a.json"):
        with dw.exclusive(tmp_path / "b.json", timeout=0.2):
            pass


def test_the_write_actually_lands(tmp_path):
    """Guard against a suite where every assertion above could pass on a no-op."""
    target = tmp_path / "f.json"
    dw.atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    dw.atomic_write_text(target, "goodbye")
    assert target.read_text(encoding="utf-8") == "goodbye"
