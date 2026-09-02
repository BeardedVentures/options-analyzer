"""Re-resolving a counterfactual from the ledger row, without its snapshot.

WHY. output/candidates/ kept 20 files against a cycle that writes 7 a day -- 2.9 trading days
of retention against a 10-day horizon. build() only re-resolved rows whose snapshot survived,
so every row was frozen ~7 trading days before it could mature. Measured 2026-09-02: 2,726
rows, horizon_complete False on all of them, and no path by which that could ever change.

The snapshot was never required. These tests hold the line that a ledger row alone is enough.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from analysis import counterfactuals as cf


def _bars(lows, start="2026-08-06"):
    idx = pd.bdate_range(start=start, periods=len(lows))
    return pd.DataFrame({"Open": lows, "High": [x * 1.01 for x in lows],
                         "Low": lows, "Close": lows, "Volume": [1] * len(lows)}, index=idx)


def _ledger(tmp_path, rows):
    p = tmp_path / "cf.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _row(**kw):
    # The key must be the real dedup_key, not a placeholder: build() matches a snapshot
    # candidate to its existing ledger row by exactly this string.
    base = {"key": "XYZ-100.0/95.0-2026-09-18",
            "ticker": "XYZ", "scan_date": "2026-08-06", "short_strike": 100.0,
            "long_strike": 95.0, "expiration": "2026-09-18", "horizon_days": 10,
            "horizon_complete": False, "touched": None, "days_observed": 1,
            "gates": {"pop": True}, "failed_gates": [], "qualified": True}
    base.update(kw)
    return base


def test_a_row_with_no_snapshot_is_re_resolved_not_frozen(tmp_path):
    """The whole repair. Before this, build() marked the row and moved on forever."""
    led = _ledger(tmp_path, [_row()])
    # 12 sessions after the scan, dipping to 98 -- through the 100 short strike.
    bars = _bars([105] * 6 + [98] + [105] * 6)

    cf.build(snapshot_dir=tmp_path / "no_snapshots", ledger=led, fetch=lambda tk: bars)

    out = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(out) == 1
    assert out[0]["source_snapshot_missing"] is True
    assert out[0]["horizon_complete"] is True, "a row with 10+ observed sessions must mature"
    assert out[0]["touched"] is True
    assert out[0]["resolution_method"] == "ledger_replay"


def test_replay_is_tagged_so_it_cannot_pool_with_forward_resolution(tmp_path):
    """Two measurements produced by different methods are two populations until shown
    otherwise -- the same rule the cohort key enforces on trades."""
    led = _ledger(tmp_path, [_row()])
    cf.build(snapshot_dir=tmp_path / "none", ledger=led, fetch=lambda tk: _bars([105] * 13))
    out = json.loads(led.read_text(encoding="utf-8").splitlines()[0])
    assert out["resolution_method"] == "ledger_replay"
    assert out["touched"] is False, "untouched must resolve to False, never stay None"


def test_an_untouched_horizon_resolves_to_False_not_None(tmp_path):
    """None means 'not yet measurable'. Leaving a matured row at None is how 2,726 rows
    reported nothing while looking merely young."""
    led = _ledger(tmp_path, [_row()])
    cf.build(snapshot_dir=tmp_path / "none", ledger=led, fetch=lambda tk: _bars([105] * 15))
    out = json.loads(led.read_text(encoding="utf-8").splitlines()[0])
    assert out["horizon_complete"] is True and out["touched"] is False


def test_a_row_still_short_of_its_horizon_stays_unmatured(tmp_path):
    """Guards the tests above from passing for the wrong reason: prove the maturity flag can
    still be False when the window genuinely has not elapsed."""
    led = _ledger(tmp_path, [_row()])
    cf.build(snapshot_dir=tmp_path / "none", ledger=led, fetch=lambda tk: _bars([105] * 4))
    out = json.loads(led.read_text(encoding="utf-8").splitlines()[0])
    assert out["horizon_complete"] is False and out["touched"] is None


def test_a_row_missing_its_strike_is_kept_frozen_not_dropped(tmp_path):
    """An unresolvable row is still an observation. Dropping it would delete history."""
    led = _ledger(tmp_path, [_row(short_strike=None)])
    cf.build(snapshot_dir=tmp_path / "none", ledger=led, fetch=lambda tk: _bars([105] * 15))
    out = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(out) == 1
    assert out[0].get("resolution_method") is None
    assert out[0]["source_snapshot_missing"] is True


def test_touch_is_measured_on_the_low_not_the_close(tmp_path):
    """A delta breach fires intraday; the spread does not care that price recovered by 4pm."""
    led = _ledger(tmp_path, [_row()])
    bars = _bars([105] * 13)
    bars.iloc[5, bars.columns.get_loc("Low")] = 99.0     # dips through, closes above
    cf.build(snapshot_dir=tmp_path / "none", ledger=led, fetch=lambda tk: bars)
    assert json.loads(led.read_text(encoding="utf-8").splitlines()[0])["touched"] is True


def test_raw_prices_are_requested_for_strike_comparison():
    """A strike is a fixed number in RAW price space. auto_adjust=True back-adjusts history
    for dividends, so an adjusted Low sits below the low that really traded and the touch test
    fires early -- measured 2026-09-02 at 0.49-0.87% on XOM, CVX and JNJ, and 0.000% on names
    with no ex-div in the window. Systematic, one direction, worst on dividend payers."""
    import inspect
    src = inspect.getsource(cf.build)
    assert "get_raw_price_data" in src
    assert "get_price_data(" not in src.replace("get_raw_price_data(", "")


def test_an_earlier_recorded_sighting_is_not_overwritten_by_a_later_one(tmp_path, monkeypatch):
    """dedup_key excludes scan_date, and first_sightings() picks the earliest SURVIVING
    snapshot -- so as snapshots age out, "earliest surviving" advances and the row's scan_date,
    gates and outcome were silently rewritten to a LATER moment. Five rows had already drifted
    by 2026-09-02. That contradicts this module's own contract and shortens the observation
    window. The ledger is the durable record: an earlier recorded sighting wins."""
    prior = _row(scan_date="2026-08-06", gates={"pop": True}, snapshot="old.json")
    led = _ledger(tmp_path, [prior])

    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    # The only surviving snapshot sees the SAME spread, later, with different gate results.
    (snap_dir / "candidates_2026-08-20_0900.json").write_text(json.dumps({
        "rows": [{"price": 105, "ctx": {"iv_rank": 50}, "candidates": [
            {"ticker": "XYZ", "short_strike": 100.0, "long_strike": 95.0,
             "expiration": "2026-09-18", "gates": {"pop": False}}]}]}), encoding="utf-8")

    cf.build(snapshot_dir=snap_dir, ledger=led, fetch=lambda tk: _bars([105] * 20))

    out = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(out) == 1, "the two sightings must still collapse to one row"
    assert out[0]["scan_date"] == "2026-08-06", "the earlier sighting must survive"
    assert out[0]["gates"] == {"pop": True}, "gates must stay as first seen"
    assert out[0]["horizon_complete"] is True, "the outcome must still be refreshed"


def test_a_later_sighting_is_still_recorded_when_it_is_genuinely_new(tmp_path):
    """Guards the test above from over-reaching: a spread with no prior ledger row must still
    be written from whatever snapshot sees it."""
    led = _ledger(tmp_path, [])
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    (snap_dir / "candidates_2026-08-20_0900.json").write_text(json.dumps({
        "rows": [{"price": 105, "ctx": {"iv_rank": 50}, "candidates": [
            {"ticker": "XYZ", "short_strike": 100.0, "long_strike": 95.0,
             "expiration": "2026-09-18", "gates": {"pop": False}}]}]}), encoding="utf-8")

    cf.build(snapshot_dir=snap_dir, ledger=led, fetch=lambda tk: _bars([105] * 20))

    out = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(out) == 1 and out[0]["scan_date"] == "2026-08-20"
    assert out[0]["resolution_method"] == "snapshot"
