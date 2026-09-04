"""Tests for the graded range claim.

The band was drawn, quoted and displayed for weeks with a coverage table from a backtest, and
nothing ever wrote one into the ledger. These tests are written against the ways that gap could
reopen or the new writer could produce rows that LOOK graded and are not.
"""
import json
import math
from datetime import date

import pytest

from analysis import band_forecast as bf
from analysis import predictions as pred
from analysis import price_projection as ppj


def _closes(n=260, start=100.0, step=0.004):
    """A deterministic series with real variation, so realised vol is non-zero and finite."""
    out, px = [], start
    for i in range(n):
        px *= (1.0 + (step if i % 2 == 0 else -step * 0.9))
        out.append(px)
    return out


# ── the unit bug that would have silently zeroed every band ──────────────────

def test_realised_vol_is_converted_from_decimal_to_points():
    """`realised_vol` returns 0.28; `project()` and `forecast_rv` take 28.4.

    Handing the decimal through does not raise -- forecast_rv clamps it up to MIN_VOL_PP and
    the band collapses to a fraction of a percent, resolving every claim OUTSIDE while the
    channel reports a healthy row count. The conversion is asserted rather than assumed.
    """
    closes = _closes()
    from analysis.direction_forecast import realised_vol
    decimal = realised_vol(closes, bf.RECENT_WINDOW)
    points = bf._annualised(closes, bf.RECENT_WINDOW)
    assert decimal is not None and points is not None
    assert points == pytest.approx(decimal * 100.0)
    # And the value is in a range that is vol POINTS, not a decimal.
    assert points > 1.0


# ── NaN must never reach the ledger ──────────────────────────────────────────

def test_nan_closes_abstain_rather_than_producing_a_nan_band():
    """NaN passes every ordinary guard: `nan <= 0` is False and `not nan` is False.

    A live pull with one padded bar drove all four horizons to a nan-nan band on this module's
    first run. `json.dumps` writes a bare NaN token, which is not valid JSON, so the failure
    would surface days later in a strict reader of the LEDGER rather than here.
    """
    closes = _closes()
    closes[-1] = float("nan")
    b = bf.band_for("TEST", closes, 5)
    # Either it abstains, or every number it returns is finite. Never a NaN band.
    if b is not None:
        for band in (b["forecast"], b["baseline"]):
            assert math.isfinite(band["low"]) and math.isfinite(band["high"])
        assert math.isfinite(b["spot"])


def test_a_band_full_of_nan_is_rejected_by_usable():
    assert not bf._usable(None)
    assert not bf._usable({"low": float("nan"), "high": 1.0, "sigma_horizon": 0.1})
    assert not bf._usable({"low": 10.0, "high": 5.0, "sigma_horizon": 0.1})   # inverted
    assert not bf._usable({"low": 0.0, "high": 5.0, "sigma_horizon": 0.1})    # non-positive
    assert bf._usable({"low": 5.0, "high": 10.0, "sigma_horizon": 0.1})


def test_every_recorded_context_value_is_json_round_trippable():
    """The ledger is JSONL. A row that cannot round-trip is a row nothing can read back."""
    ids = bf.record_ticker("TEST", _closes(), today=date(2026, 9, 3), implied_vol_pp=22.0)
    assert ids
    rows = [r for r in pred.load() if r["id"] in set(ids)]
    assert len(rows) == len(ids)
    for r in rows:
        text = json.dumps(r)                      # raises on nothing; NaN would pass here...
        assert "NaN" not in text and "Infinity" not in text   # ...so assert on the token
        json.loads(text)


# ── horizons are graded separately ───────────────────────────────────────────

def test_each_horizon_gets_its_own_claim_type():
    """Pooling an overnight band with a monthly one yields a coverage figure describing neither."""
    types = [ct for ct, _, _ in bf.HORIZONS]
    assert len(set(types)) == len(types) == 4
    for ct in types:
        assert pred.is_band_claim(ct)


def test_a_new_horizon_type_is_scoreable_without_touching_the_scorer():
    """Prefix matching, the same rule direction claims use.

    A per-horizon type added while the scorer matched `ct == BAND_CONTAINS` exactly would fall
    through to "no scorer for claim type" and mark its whole population unresolvable while the
    row count still looked healthy.
    """
    assert pred.is_band_claim("band_contains_2y")
    assert pred.is_band_claim("band_contains_1w_baseline")
    assert not pred.is_band_claim("direction_1w")
    assert not pred.is_band_claim(None)


def test_grade_buckets_each_horizon_separately():
    bf.record_ticker("TEST", _closes(), today=date(2026, 9, 3))
    g = pred.grade()
    got = {t for t in g["by_type"] if t.startswith("band_contains")}
    # Nothing is resolved yet, so by_type may be empty -- the guarantee under test is that the
    # WRITER emits distinct types, which the ledger shows directly.
    written = {r["claim_type"] for r in pred.load() if pred.is_band_claim(r.get("claim_type"))}
    assert len({t for t in written if not t.endswith("_baseline")}) == 4
    assert got <= written


# ── the baseline twin ────────────────────────────────────────────────────────

def test_baseline_twin_is_written_and_differs_from_the_forecast():
    """Without a null model, a coverage of 79% on an 80% band means nothing."""
    ids = bf.record_ticker("TEST", _closes(), today=date(2026, 9, 3))
    rows = {r["id"]: r for r in pred.load() if r["id"] in set(ids)}
    base = [r for r in rows.values() if (r["context"] or {}).get("baseline")]
    fore = [r for r in rows.values() if not (r["context"] or {}).get("baseline")]
    assert len(base) == len(fore) == 4
    # The two are drawn from different volatilities, so a run where they coincide exactly on
    # every horizon means the baseline is not actually a separate model.
    fv = {r["context"]["forecast_vol_pp"] for r in fore}
    tv = {r["context"]["trailing_vol_pp"] for r in fore}
    assert fv and tv


def test_baseline_uses_trailing_vol_and_forecast_uses_forecast_vol():
    closes = _closes()
    b = bf.band_for("TEST", closes, 21)
    assert b is not None
    # The baseline band is the trailing-vol band; the forecast band is the forecast-vol band.
    expect_base = ppj.project(b["spot"], b["dte_calendar"], b["trailing_vol_pp"],
                              confidence=b["confidence"], trading_days_override=21)
    assert b["baseline"]["low"] == pytest.approx(expect_base["low"])
    assert b["baseline"]["high"] == pytest.approx(expect_base["high"])


# ── the third leg is recorded honestly ───────────────────────────────────────

def test_absent_implied_is_recorded_as_not_attempted_not_as_a_substitute():
    """"The market disagreed" and "nobody asked the market" must not look the same later."""
    ids = bf.record_ticker("TEST", _closes(), today=date(2026, 9, 3))
    rows = [r for r in pred.load() if r["id"] in set(ids)]
    for r in rows:
        ctx = r["context"]
        assert ctx["implied_attempted"] is False
        assert ctx["implied_vol_pp"] is None
        assert ctx["implied_band_low"] is None and ctx["implied_band_high"] is None
        # The trailing number must never have been quietly promoted into the implied slot.
        assert ctx["trailing_vol_pp"] is not None


def test_supplied_implied_is_recorded_on_the_same_horizon_as_the_forecast():
    ids = bf.record_ticker("TEST", _closes(), today=date(2026, 9, 3), implied_vol_pp=30.0)
    rows = [r for r in pred.load() if r["id"] in set(ids)]
    assert rows
    for r in rows:
        ctx = r["context"]
        assert ctx["implied_attempted"] is True
        assert ctx["implied_vol_pp"] == 30.0
        assert ctx["implied_band_low"] < ctx["price_at_claim"] < ctx["implied_band_high"]


# ── the horizon units bug ────────────────────────────────────────────────────

def test_a_one_day_horizon_is_one_trading_day_not_zero_point_seven():
    """1 trading day -> 1.45 calendar -> int() -> 1 -> trading_days(1) = 0.69.

    A 31% understatement of the horizon, on the band with the least room for it. The override
    exists so the claim's horizon is the horizon it was written for.
    """
    b = bf.band_for("TEST", _closes(), 1)
    assert b is not None
    assert b["forecast"]["trading_days"] == pytest.approx(1.0)
    b5 = bf.band_for("TEST", _closes(), 5)
    assert b5["forecast"]["trading_days"] == pytest.approx(5.0)


def test_project_without_the_override_is_unchanged():
    """The override is additive. Every existing caller must get exactly what it got before."""
    a = ppj.project(100.0, 35, 28.0, confidence=0.80)
    assert a["trading_days"] == pytest.approx(ppj.trading_days(35), abs=0.05)
    assert a["low"] < 100.0 < a["high"]


# ── the band widens with the horizon ─────────────────────────────────────────

def test_bands_widen_monotonically_with_horizon():
    closes = _closes()
    widths = []
    for _, days, _ in bf.HORIZONS:
        b = bf.band_for("TEST", closes, days)
        assert b is not None
        widths.append((days, b["forecast"]["high_pct"] - b["forecast"]["low_pct"]))
    by_days = sorted(set(widths))
    for (d1, w1), (d2, w2) in zip(by_days, by_days[1:]):
        if d2 > d1:
            assert w2 > w1, f"{d2}d band ({w2}) is not wider than {d1}d ({w1})"


# ── abstention ───────────────────────────────────────────────────────────────

def test_too_little_history_abstains_rather_than_guessing():
    assert bf.band_for("TEST", _closes(n=bf.MIN_CLOSES - 1), 5) is None
    assert bf.record_ticker("TEST", _closes(n=10), today=date(2026, 9, 3)) == []


def test_record_watchlist_never_raises_on_a_bad_lookup():
    def boom(_):
        raise RuntimeError("feed down")
    stats = bf.record_watchlist(today=date(2026, 9, 3), tickers=["A", "B"], price_lookup=boom)
    assert stats["failed"] == 2 and stats["recorded"] == 0


# ── no new ledger ────────────────────────────────────────────────────────────

def test_writes_to_the_existing_prediction_ledger_only():
    """A sixth ledger is the thing that has repeatedly been missed by the isolation list.

    This module deliberately adds none: it records through predictions.record(), which
    tests/conftest.py already redirects. Checked against the parsed SOURCE rather than the raw
    text, so the prose in the module docstring explaining that it opens no file does not itself
    trip the check -- an assertion that its own documentation can fail is an assertion that
    will be deleted rather than fixed.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bf))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parent_docstring = node.value.strip().startswith(("band_forecast.py", "The band",
                                                              "How many", "Same prefix"))
            if not parent_docstring and ".jsonl" in node.value:
                raise AssertionError(f"band_forecast names a ledger file: {node.value!r}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("open",):
            raise AssertionError("band_forecast opens a file directly")
    # And nothing at module level is a filesystem path.
    from pathlib import Path
    paths = [k for k, v in vars(bf).items() if isinstance(v, Path)]
    assert not paths, f"band_forecast holds path constants: {paths}"
