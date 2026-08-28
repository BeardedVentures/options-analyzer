"""Chain data quality: the measurement, the log, and the floor gate.

Context. Every signal VEGA emits is a statement about an options chain, and nothing recorded
how much of that chain was actually quotable. The yfinance path discards 30-45% of records as
stale; an IV rank or a skew read over the remainder describes the survivors, not the market,
and it looked exactly as confident either way.

The first test below is the one that matters. The obvious implementation of `usable_ratio` is
len(after_filter) / len(before_filter) — and because the Polygon path does not filter, that
expression is pinned at 1.000 there forever. It would have passed every test anyone thought to
write while being arithmetically incapable of reporting a problem on the primary data source.
So the suite proves the metric can be non-zero on Polygon before it proves anything else.
"""
import json

import pytest

import config
from data import fetcher
from data import data_quality_log as dq


def opt(bid=1.00, ask=1.10, mid=1.05, volume=500, oi=1000):
    return {"bid": bid, "ask": ask, "mid": mid, "volume": volume, "open_interest": oi}


@pytest.fixture
def temp_log(tmp_path, monkeypatch):
    f = tmp_path / "data_quality_log.json"
    monkeypatch.setattr(dq, "LOG_FILE", str(f))
    return f


# ── The metric must be able to be wrong ───────────────────────────────────────────────────────

def test_quality_is_measured_by_predicate_not_by_comparing_a_list_to_itself():
    """An unfiltered chain must still be able to score badly.

    This is the whole design constraint. If the ratio were derived from the filter having run,
    a source that does not filter would report a perfect chain no matter what arrived.
    """
    junk = [opt(bid=0, ask=0, mid=0)] * 7 + [opt()] * 3     # nothing filtered these out
    raw, usable, ratio = fetcher.measure_chain_quality(junk)
    assert (raw, usable) == (10, 3)
    assert ratio == 0.3, "an unfiltered chain of mostly dead quotes must not read as healthy"


def test_a_genuinely_clean_chain_reads_clean():
    raw, usable, ratio = fetcher.measure_chain_quality([opt()] * 12)
    assert (raw, usable, ratio) == (12, 12, 1.0)


def test_empty_chain_is_zero_not_a_crash():
    assert fetcher.measure_chain_quality([]) == (0, 0, 0.0)
    assert fetcher.measure_chain_quality(None) == (0, 0, 0.0)


@pytest.mark.parametrize("bad, why", [
    (opt(bid=0, ask=0, mid=0), "no market on either side"),
    (opt(bid=2.00, ask=1.00, mid=1.50), "crossed market — a data error"),
    (opt(bid=0.10, ask=2.00, mid=1.05), "spread wider than 80% of mid — stale"),
    (opt(volume=0, oi=0), "no volume and no open interest — nobody is there"),
])
def test_each_unusable_shape_is_rejected(bad, why):
    assert fetcher._option_record_is_usable(bad) is False, why


def test_the_filter_and_the_measurement_agree():
    """_quality_filter_options and measure_chain_quality must never disagree about one record,
    or the log would describe a chain other than the one the scan traded."""
    chain = [opt(), opt(bid=0, ask=0, mid=0), opt(), opt(volume=0, oi=0)]
    kept = fetcher._quality_filter_options(list(chain), "TEST", "yfinance")
    _, usable, _ = fetcher.measure_chain_quality(chain)
    assert len(kept) == usable == 2


# ── The log ───────────────────────────────────────────────────────────────────────────────────

def test_record_writes_a_readable_row(temp_log):
    row = dq.record("GDX", "yfinance", raw_count=240, usable_count=138, scan_id="s1")
    assert row["usable_ratio"] == 0.575
    assert row["score"] == 57
    assert json.loads(temp_log.read_text(encoding="utf-8"))[0]["ticker"] == "GDX"


def test_record_never_raises_on_a_bad_path(monkeypatch):
    monkeypatch.setattr(dq, "LOG_FILE", "/nonexistent-dir/nope/data_quality_log.json")
    row = dq.record("SPY", "polygon", 100, 90)   # must not raise
    assert row["usable_ratio"] == 0.9


def test_a_corrupt_log_does_not_take_down_a_scan(temp_log):
    temp_log.write_text("{ this is not json", encoding="utf-8")
    row = dq.record("SPY", "polygon", 10, 10)
    assert row["ticker"] == "SPY"
    assert json.loads(temp_log.read_text(encoding="utf-8"))[-1]["ticker"] == "SPY"


def test_log_is_bounded(temp_log, monkeypatch):
    monkeypatch.setattr(dq, "MAX_ROWS", 5)
    for i in range(9):
        dq.record(f"T{i}", "polygon", 10, 10, scan_id="s1")
    rows = json.loads(temp_log.read_text(encoding="utf-8"))
    assert len(rows) == 5 and rows[0]["ticker"] == "T4"


# ── The scan summary ──────────────────────────────────────────────────────────────────────────

def test_latest_scan_reports_the_worst_ticker_not_the_average(temp_log):
    """An average hides the ticker that is actually broken — which is the one that decides
    whether a trade opened on a chain that was mostly not there."""
    dq.record("SPY", "polygon", 200, 190, scan_id="s2")     # 0.95
    dq.record("QQQ", "polygon", 200, 180, scan_id="s2")     # 0.90
    dq.record("GDX", "yfinance", 240, 48, scan_id="s2")     # 0.20 — below floor
    s = dq.latest_scan(floor=0.30)
    assert s["count"] == 3
    assert s["worst_ticker"] == "GDX"
    assert s["worst_ratio"] == 0.2
    assert s["below_floor"] == 1
    assert s["sources"] == {"polygon": 2, "yfinance": 1}


def test_latest_scan_ignores_an_earlier_cycle(temp_log):
    """Yesterday's disaster must not pollute today's read."""
    dq.record("GDX", "yfinance", 100, 5, scan_id="old")     # 0.05, yesterday's disaster
    rows = json.loads(temp_log.read_text(encoding="utf-8"))
    rows[0]["timestamp"] = "2026-08-01T09:00:00"            # genuinely a previous cycle
    temp_log.write_text(json.dumps(rows), encoding="utf-8")
    dq.record("SPY", "polygon", 100, 95, scan_id="new")
    s = dq.latest_scan(floor=0.30)
    assert s["count"] == 1 and s["worst_ticker"] == "SPY" and s["below_floor"] == 0


def test_latest_scan_covers_the_whole_cycle_not_just_the_last_batch(temp_log):
    """The regression that made the tile lie. One cycle writes several scan_ids: the wide
    engine scan first, then a mark loop over only the tickers already holding positions. The
    mark loop writes last and reads healthy by construction, so summarising the newest scan_id
    reported a clean board while the same cycle had skipped tickers outright."""
    dq.record("JNJ", "yfinance", 214, 85, scan_id="engine")   # 0.40 — skipped
    dq.record("ABBV", "yfinance", 203, 83, scan_id="engine")  # 0.41 — skipped
    dq.record("SPY", "yfinance", 200, 190, scan_id="marks")   # the mark loop, healthy
    s = dq.latest_scan(floor=0.50)
    assert s["below_floor"] == 2, "the engine scan's skipped tickers must still be counted"
    assert s["below_floor_tickers"] == ["ABBV", "JNJ"]
    assert s["worst_ticker"] == "JNJ"
    assert set(s["scan_ids"]) == {"engine", "marks"}


def test_a_ticker_is_represented_by_its_worst_reading_in_the_cycle(temp_log):
    """A ticker is recorded once per DTE window. The floor asks whether it was skipped, so the
    worst reading is the one that answers the question."""
    dq.record("AMGN", "yfinance", 48, 16, scan_id="engine")   # 0.33
    dq.record("AMGN", "yfinance", 331, 300, scan_id="engine")  # 0.91
    s = dq.latest_scan(floor=0.50)
    assert s["count"] == 1 and s["below_floor"] == 1 and s["worst_ratio"] == 0.3333


def test_latest_scan_on_an_empty_log_is_empty_not_an_exception(temp_log):
    s = dq.latest_scan()
    assert s["count"] == 0 and s["worst_ratio"] is None


@pytest.mark.parametrize("ratio, expected", [
    (0.95, "green"), (0.70, "green"), (0.69, "amber"),
    (0.30, "amber"), (0.29, "red"), (0.0, "red"), (None, "unknown"),
])
def test_band_boundaries(ratio, expected):
    assert dq.band(ratio, floor=0.30) == expected


# ── The floor gate ────────────────────────────────────────────────────────────────────────────

def _stub_chain_fetch(monkeypatch, records, source="polygon"):
    """Drive get_options_chain without network. Price comes from a stubbed frame."""
    import pandas as pd
    monkeypatch.setattr(fetcher, "_cache", {})
    monkeypatch.setattr(fetcher, "get_price_data",
                        lambda *a, **k: pd.DataFrame({"Close": [100.0]}))
    if source == "polygon":
        monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key", raising=False)
        monkeypatch.setattr(fetcher, "_parse_polygon_options", lambda *a, **k: records)
    else:
        monkeypatch.setattr(config, "POLYGON_API_KEY", "", raising=False)
        monkeypatch.setattr(fetcher, "_parse_yfinance_options", lambda *a, **k: records)
        monkeypatch.setattr(fetcher, "_log_api_call", lambda *a, **k: None)


def test_a_chain_below_the_floor_returns_empty(monkeypatch, temp_log):
    """The correct outcome for a chain that is mostly absent is no read at all. Scoring it
    produces a number that looks like every other number on the board and is not one."""
    _stub_chain_fetch(monkeypatch, [opt(bid=0, ask=0, mid=0)] * 9 + [opt()])
    assert fetcher.get_options_chain("GDX", 25, 45) == []
    assert dq.latest_scan(floor=0.30)["worst_ratio"] == 0.1


def test_a_healthy_chain_passes_through_untouched(monkeypatch, temp_log):
    chain = [opt()] * 20
    _stub_chain_fetch(monkeypatch, chain)
    assert len(fetcher.get_options_chain("SPY", 25, 45)) == 20
    assert dq.latest_scan(floor=0.30)["below_floor"] == 0


def test_the_floor_can_be_switched_off_without_losing_the_measurement(monkeypatch, temp_log):
    """Instrumentation and enforcement are separate decisions. Turning the gate off must still
    leave the reading in the log — otherwise disabling the gate also blinds the operator."""
    monkeypatch.setattr(config, "CHAIN_QUALITY_GATE_ENABLED", False, raising=False)
    _stub_chain_fetch(monkeypatch, [opt(bid=0, ask=0, mid=0)] * 9 + [opt()])
    assert len(fetcher.get_options_chain("GDX", 25, 45)) == 10
    assert dq.latest_scan(floor=0.30)["worst_ratio"] == 0.1


def test_the_yfinance_path_is_measured_before_its_filter(monkeypatch, temp_log):
    """On yfinance the denominator must be what ARRIVED, not what survived — otherwise the
    ratio is 1.000 there too and the log describes the filter rather than the market."""
    _stub_chain_fetch(monkeypatch, [opt(bid=0, ask=0, mid=0)] * 6 + [opt()] * 4, source="yfinance")
    monkeypatch.setattr(config, "CHAIN_QUALITY_GATE_ENABLED", False, raising=False)
    out = fetcher.get_options_chain("GDX", 25, 45)
    assert len(out) == 4                                    # the filter did run
    row = dq.read_recent()[-1]
    assert (row["raw_count"], row["usable_count"]) == (10, 4)
    assert row["chain_source"] == "yfinance"


def test_skew_scoring_stays_off_until_quality_is_proven():
    """P0-2 turned skew scoring off pending this instrumentation. It is a deliberate state, not
    a leftover — assert it so re-enabling has to be a decision someone makes on purpose."""
    assert config.SKEW_SCORING_ENABLED is False
    assert config.CHAIN_QUALITY_MIN_RATIO > 0


# ── Surviving a concurrent writer ─────────────────────────────────────────────────────────────
#
# On 2026-08-25 this log went from 886 KB to 10 KB in one cycle, and it was the fifth such wipe
# since 2026-08-13. Two failures compounded. `record` wrote through a temp path with a FIXED
# name, so two processes recording at once truncated and flushed over each other and left a
# complete JSON array with a longer document's tail attached ("Extra data: line N column 2").
# `_read_all` then caught the decode error, returned [], and the caller appended its handful of
# new rows to that empty list and wrote it back — destroying every reading ever taken. The file
# is gitignored and unbacked, so none of it was recoverable.
#
# These tests pin both halves: the corrupt shape must be salvaged rather than discarded, and
# two writers must not be able to produce that shape in the first place.

def _corrupt_like_the_real_incident(path, rows):
    """Write the exact byte pattern the incident produced: a valid array, then a longer
    document's tail. Reproduced from the logged error rather than invented."""
    path.write_text(json.dumps(rows, indent=2) + '\n  {"ticker": "LEFTOVER"}\n]',
                    encoding="utf-8")


def test_a_corrupt_log_is_salvaged_not_silently_emptied(temp_log):
    _corrupt_like_the_real_incident(temp_log, [{"ticker": f"T{i}", "usable_ratio": 0.9}
                                               for i in range(40)])
    assert len(dq._read_all()) == 40, "the leading array is intact and must be recovered"


def test_recording_onto_a_corrupt_log_keeps_the_history(temp_log):
    """The actual data-loss path: append after a failed read. This returned a 1-row file."""
    _corrupt_like_the_real_incident(temp_log, [{"ticker": f"T{i}", "usable_ratio": 0.9}
                                               for i in range(40)])
    dq.record("NEW", "yfinance", 100, 80)
    assert len(json.loads(temp_log.read_text(encoding="utf-8"))) == 41


def test_an_unsalvageable_log_is_quarantined_rather_than_overwritten(temp_log, monkeypatch):
    temp_log.write_text("this is not json at all", encoding="utf-8")
    assert dq._read_all() == []
    dq.record("NEW", "yfinance", 100, 80)
    saved = list(temp_log.parent.glob("data_quality_log.json.corrupt-*"))
    assert len(saved) == 1, "the unreadable bytes must be kept for inspection"
    assert saved[0].read_text(encoding="utf-8") == "this is not json at all"


def test_the_temp_path_is_unique_per_process(temp_log, monkeypatch):
    """A shared temp name is the mechanism behind every wipe above. Two processes must never
    be able to select the same one."""
    seen = []
    real_open = dq.open if hasattr(dq, "open") else open

    def spy(path, *a, **k):
        if str(path).endswith(".tmp"):
            seen.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    monkeypatch.setattr(dq.os, "getpid", lambda: 1111)
    dq.record("A", "yfinance", 10, 9)
    monkeypatch.setattr(dq.os, "getpid", lambda: 2222)
    dq.record("B", "yfinance", 10, 9)
    assert len(set(seen)) == 2, f"both writers used the same temp path: {seen}"


def test_a_locked_destination_is_retried_not_dropped(temp_log, monkeypatch):
    """Windows refuses os.replace while any other process holds the destination open — even
    for reading. Those collisions were silently losing readings."""
    calls = {"n": 0}
    real_replace = dq.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(dq.os, "replace", flaky)
    dq.record("RETRY", "yfinance", 10, 9)
    assert calls["n"] == 3
    assert json.loads(temp_log.read_text(encoding="utf-8"))[-1]["ticker"] == "RETRY"


def test_no_temp_files_are_left_behind(temp_log):
    dq.record("A", "yfinance", 10, 9)
    assert not list(temp_log.parent.glob("*.tmp"))


def test_two_writers_cannot_hold_the_lock_at_once(temp_log):
    """Per-process temp paths stopped the corruption; only the lock stops the LOSSES. A
    five-writer stress run still dropped about a fifth of its readings to WinError 5 before
    this existed, because os.replace fails while another process holds the destination open."""
    with dq._exclusive():
        with pytest.raises(TimeoutError):
            with dq._exclusive(timeout=0.2):
                pass


def test_the_lock_is_released_even_when_the_write_raises(temp_log, monkeypatch):
    monkeypatch.setattr(dq, "_append_locked", lambda row: (_ for _ in ()).throw(IOError("boom")))
    dq.record("A", "yfinance", 10, 9)          # must not propagate
    with dq._exclusive(timeout=0.2):           # and must not still be held
        pass


def test_a_stale_lock_is_broken_not_waited_on(temp_log, monkeypatch):
    """A killed scan must not wedge instrumentation for every run that follows."""
    lock = str(temp_log) + ".lock"
    open(lock, "w").close()
    monkeypatch.setattr(dq, "LOCK_STALE_SECONDS", -1)   # i.e. already stale
    with dq._exclusive(timeout=0.5):
        pass
    dq.record("A", "yfinance", 10, 9)
    assert len(dq._read_all()) == 1


def test_a_held_lock_does_not_lose_the_reading_it_blocks(temp_log):
    """The lock must serialise, not discard: a writer that waits still lands its row."""
    dq.record("FIRST", "yfinance", 10, 9)
    dq.record("SECOND", "yfinance", 10, 9)
    assert [r["ticker"] for r in dq._read_all()] == ["FIRST", "SECOND"]


# ── NewsAPI rate limiting ─────────────────────────────────────────────────────────────────
#
# The free tier allows ~100 requests/day. One scan makes 56, and the cycle runs 7 times a day,
# so the quota is gone inside the first cycle or two and every later call is a guaranteed
# round trip to a refusal. run.log held 3,688 of them on 2026-08-27. Sentiment silently
# dropped to keyword matching for the whole session either way; what the breaker changes is
# that it stops paying 55 more round trips per cycle and stops burying real errors.

def test_a_429_stops_newsapi_for_the_rest_of_the_run(monkeypatch, caplog):
    import requests
    from data import fetcher

    class _Resp:
        status_code = 429

    def rate_limited(*a, **k):
        err = requests.exceptions.HTTPError("429 Client Error: Too Many Requests")
        err.response = _Resp()
        raise err

    monkeypatch.setattr(fetcher.requests, "get", rate_limited)
    monkeypatch.setattr(fetcher, "_parse_yfinance_options", lambda *a, **k: [])
    fetcher._newsapi_rate_limited.clear()
    fetcher._cache.clear()

    with caplog.at_level("WARNING"):
        for tk in ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]:
            try:
                fetcher.get_news(tk)
            except Exception:
                pass

    assert fetcher._newsapi_rate_limited, "the breaker never engaged"
    assert caplog.text.count("rate-limited") == 1, (
        "one line per run, not one per ticker: %d" % caplog.text.count("rate-limited"))


def test_a_non_429_error_is_still_reported_per_ticker(monkeypatch, caplog):
    """The breaker must not swallow ordinary failures -- a timeout on one ticker says nothing
    about the next one."""
    import requests
    from data import fetcher

    def boom(*a, **k):
        raise requests.exceptions.Timeout("connection timed out")

    monkeypatch.setattr(fetcher.requests, "get", boom)
    fetcher._newsapi_rate_limited.clear()
    fetcher._cache.clear()

    with caplog.at_level("WARNING"):
        for tk in ["SPY", "QQQ"]:
            try:
                fetcher.get_news(tk)
            except Exception:
                pass
    assert not fetcher._newsapi_rate_limited, "a timeout is not a rate limit"


def test_the_breaker_resets_between_scans(monkeypatch):
    from data import fetcher
    fetcher._newsapi_rate_limited.add("429")
    fetcher.clear_cache()
    assert not fetcher._newsapi_rate_limited


def test_macro_news_falls_back_instead_of_returning_empty(monkeypatch):
    """get_news() has had a yfinance tier since it was written; get_macro_news() never did.

    So a single 429 on the macro query left market_context.macro_events an EMPTY LIST -- not
    degraded, absent. Regime context then scored with no macro input at all, and nothing said
    so. That is the same shape as every other silent-degradation bug in this system: the number
    looked like an answer.
    """
    import requests
    from data import fetcher

    class _Resp:
        status_code = 429

    def rate_limited(*a, **k):
        err = requests.exceptions.HTTPError("429 Client Error: Too Many Requests")
        err.response = _Resp()
        raise err

    monkeypatch.setattr(fetcher.requests, "get", rate_limited)
    monkeypatch.setattr(fetcher, "get_news",
                        lambda t, hours=24: [{"title": f"{t} headline", "source": "x",
                                              "published_at": "", "url": "",
                                              "description": ""}])
    fetcher._newsapi_rate_limited.clear()
    fetcher._cache.clear()

    out = fetcher.get_macro_news()
    assert out, "macro_events came back empty on a 429 -- the original regression"
    assert any(a.get("macro_proxy") for a in out), "fallback articles should be marked as proxies"


def test_macro_fallback_is_not_used_when_newsapi_works(monkeypatch):
    """The fallback must not mask a working primary."""
    from data import fetcher

    class _OK:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"articles": [{"title": "real macro", "source": {"name": "NewsAPI"},
                                  "publishedAt": "", "url": ""}]}

    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _OK())
    fetcher._newsapi_rate_limited.clear()
    fetcher._cache.clear()
    out = fetcher.get_macro_news()
    assert out and not any(a.get("macro_proxy") for a in out)
