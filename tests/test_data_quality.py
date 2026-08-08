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


def test_latest_scan_ignores_the_previous_scan(temp_log):
    dq.record("GDX", "yfinance", 100, 5, scan_id="old")     # 0.05, yesterday's disaster
    dq.record("SPY", "polygon", 100, 95, scan_id="new")
    s = dq.latest_scan(floor=0.30)
    assert s["count"] == 1 and s["worst_ticker"] == "SPY" and s["below_floor"] == 0


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
