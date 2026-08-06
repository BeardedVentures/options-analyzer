"""Chart-structure reader — analysis/structure.py.

Origin: the RSI-only timing read could not distinguish a shallow pause inside an advance
from the second peak of a double top. Both print RSI ~55 below the 20-day and call for
opposite actions from a premium seller. This module reads the shape instead.

Every series here is synthetic and deterministic, so a failure means the detector changed
behaviour — not that the market moved.

The load-bearing property is that a WRONG read is worse than NO read. A confident bad label
("late in a bull flag" on a breakdown) would talk someone into selling puts under a failing
chart. So the coherence guards below matter as much as the happy paths.
"""
import pytest

import config
from analysis.structure import (
    BEAR_FLAG,
    BULL_FLAG,
    DOUBLE_BOTTOM,
    DOUBLE_TOP,
    DOWNTREND,
    HH_HL,
    LH_LL,
    RANGE,
    UNREADABLE,
    UPTREND_EXTENDED,
    _atr_pct,
    _momentum_flattening,
    _range_contracting,
    _swing_threshold,
    _zigzag,
    detect_structure,
)


def _ohlc(closes, spread=0.5):
    """Wrap a close series in plausible highs/lows."""
    return [c + spread for c in closes], [c - spread for c in closes], list(closes)


def _leg(start, end, bars):
    """Linear price leg from `start` to `end` over `bars` bars (excludes `start`)."""
    step = (end - start) / bars
    return [start + step * (i + 1) for i in range(bars)]


# ── Primitives ────────────────────────────────────────────────────────────────────────────────

def test_atr_pct_scales_with_bar_range():
    highs = [102] * 30
    lows = [98] * 30
    closes = [100] * 30
    assert _atr_pct(highs, lows, closes) == pytest.approx(0.04, abs=0.005)


def test_swing_threshold_is_clamped_both_ways():
    """A placid ETF must not produce noise swings; a violent name must still produce some."""
    calm_h, calm_l, calm_c = _ohlc([100] * 60, spread=0.01)
    assert _swing_threshold(calm_h, calm_l, calm_c) == config.STRUCTURE_ZIGZAG_MIN_PCT

    wild_h = [130] * 60
    wild_l = [70] * 60
    wild_c = [100] * 60
    assert _swing_threshold(wild_h, wild_l, wild_c) == config.STRUCTURE_ZIGZAG_MAX_PCT


def test_zigzag_ignores_noise_below_threshold():
    """The whole reason fractals were replaced: a 1% wiggle is not a swing."""
    closes = []
    for i in range(60):
        closes.append(100 + (1 if i % 2 else -1))
    h, l, c = _ohlc(closes, spread=0.05)
    assert _zigzag(h, l, 0.10) == []


def test_zigzag_confirms_a_real_reversal():
    closes = _leg(100, 130, 20) + _leg(130, 100, 20)
    h, l, c = _ohlc(closes)
    piv = _zigzag(h, l, 0.10)
    kinds = [k for _, _, k in piv]
    assert "H" in kinds
    high_price = max(p for _, p, k in piv if k == "H")
    assert high_price == pytest.approx(130.5, abs=1.0)


def test_zigzag_alternates_kinds():
    closes = _leg(100, 130, 15) + _leg(130, 100, 15) + _leg(100, 135, 15) + _leg(135, 105, 15)
    h, l, c = _ohlc(closes)
    kinds = [k for _, _, k in _zigzag(h, l, 0.08)]
    assert all(a != b for a, b in zip(kinds, kinds[1:])), f"non-alternating: {kinds}"


def test_momentum_flattening_detects_a_decaying_histogram():
    # A decelerating decline: steps shrink, so |MACD histogram| should be shrinking.
    closes = [100.0]
    step = 3.0
    for _ in range(60):
        closes.append(closes[-1] - step)
        step *= 0.85
    assert _momentum_flattening(closes) is True


def test_range_contracting_reads_a_narrowing_band():
    highs = [110] * 10 + [103] * 10
    lows = [90] * 10 + [97] * 10
    assert _range_contracting(highs, lows, 10) is True
    assert _range_contracting(highs[::-1], lows[::-1], 10) is False


# ── Guard rails ───────────────────────────────────────────────────────────────────────────────

def test_too_few_bars_is_unreadable_not_a_guess():
    h, l, c = _ohlc(_leg(100, 120, 10))
    r = detect_structure(h, l, c)
    assert r["pattern"] == UNREADABLE
    assert r["confidence"] == "LOW"
    assert "bars available" in r["detail"]


def test_empty_and_none_inputs_survive():
    for args in (([], [], []), (None, None, None)):
        assert detect_structure(*args)["pattern"] == UNREADABLE


def test_flat_line_does_not_invent_a_pattern():
    h, l, c = _ohlc([100.0] * 120, spread=0.01)
    assert detect_structure(h, l, c)["pattern"] in (RANGE, UNREADABLE)


# ── Patterns ──────────────────────────────────────────────────────────────────────────────────

def test_bull_flag_early_vs_late_tracks_retracement():
    """Stage is retracement-driven, because retracement is the proxy for how much premium
    on the threatened side has already repriced."""
    base = _leg(100, 100, 40) + _leg(100, 130, 25)          # 30-point impulse to 130
    shallow = detect_structure(*_ohlc(base + _leg(130, 126, 4)))   # ~13% retraced
    deep = detect_structure(*_ohlc(base + _leg(130, 113, 8)))      # ~57% retraced

    assert shallow["pattern"] == BULL_FLAG and shallow["stage"] == "EARLY"
    assert deep["pattern"] == BULL_FLAG and deep["stage"] == "LATE"
    assert deep["retracement_pct"] > shallow["retracement_pct"]
    assert shallow["impulse_pct"] == pytest.approx(30, abs=6)


def test_bear_flag_is_the_mirror_image():
    series = _leg(130, 130, 40) + _leg(130, 100, 25) + _leg(100, 112, 8)
    r = detect_structure(*_ohlc(series))
    assert r["pattern"] == BEAR_FLAG
    assert r["stage"] in ("MID", "LATE")


def test_double_top_beats_a_generic_continuation_read():
    """The case Josh named: back at a prior peak is 'late', not 'extended'."""
    series = (_leg(100, 100, 30) + _leg(100, 130, 15) + _leg(130, 112, 12)
              + _leg(112, 129, 15) + _leg(129, 120, 8))
    r = detect_structure(*_ohlc(series))
    assert r["pattern"] == DOUBLE_TOP
    assert r["stage"] == "LATE"
    assert "second" in r["phrase"]


def test_double_bottom_detected():
    series = (_leg(130, 130, 30) + _leg(130, 100, 15) + _leg(100, 118, 12)
              + _leg(118, 101, 15) + _leg(101, 110, 8))
    r = detect_structure(*_ohlc(series))
    assert r["pattern"] == DOUBLE_BOTTOM


def test_steady_advance_reads_as_extended_not_as_a_flag():
    r = detect_structure(*_ohlc(_leg(100, 160, 120)))
    assert r["pattern"] == UPTREND_EXTENDED
    assert r["swing_structure"] in (HH_HL, "FLAT")


def test_steady_decline_reads_as_downtrend():
    r = detect_structure(*_ohlc(_leg(160, 100, 120)))
    assert r["pattern"] in (DOWNTREND, BEAR_FLAG)


def test_sideways_band_reads_as_range():
    closes = [100 + (2 if i % 6 < 3 else -2) for i in range(120)]
    r = detect_structure(*_ohlc(closes))
    assert r["pattern"] in (RANGE, UNREADABLE)


# ── Coherence: a confident wrong label is the failure mode that matters ───────────────────────

def test_bull_flag_contradicted_by_swing_structure_is_demoted():
    """A 'bull flag' printing lower highs into lower lows is the detector fitting a
    continuation label onto a breakdown. It must not carry actionable confidence."""
    series = (_leg(140, 100, 40)          # established decline: LH/LL
              + _leg(100, 118, 20)        # bounce that looks like an impulse
              + _leg(118, 110, 6))        # small fade off it
    r = detect_structure(*_ohlc(series))
    if r["pattern"] == BULL_FLAG and r["swing_structure"] == LH_LL:
        assert r["confidence"] == "LOW"
        assert "contradicts" in r["detail"]


def test_near_failure_retracement_is_demoted():
    series = _leg(100, 100, 40) + _leg(100, 130, 20) + _leg(130, 110, 10)   # ~67% retraced
    r = detect_structure(*_ohlc(series))
    if r["pattern"] == BULL_FLAG:
        assert r["retracement_pct"] >= 60
        assert r["confidence"] == "LOW"


def test_retracement_beyond_max_is_not_a_flag_at_all():
    series = _leg(100, 100, 40) + _leg(100, 130, 20) + _leg(130, 101, 12)
    assert detect_structure(*_ohlc(series))["pattern"] != BULL_FLAG


# ── Level read ────────────────────────────────────────────────────────────────────────────────

def test_level_read_counts_touches():
    """A third touch of a level is materially different from a first."""
    # Repeated tags of ~100, ending ON the level — a level only counts when price is at it.
    series = []
    for _ in range(4):
        series += _leg(110, 100, 8) + _leg(100, 110, 8)
    series += _leg(110, 100, 8)
    h, l, c = _ohlc(series, spread=0.2)
    r = detect_structure(h, l, c, supports=[99.8])
    assert r["level"] is not None
    assert r["level"]["kind"] == "support"
    assert r["level"]["touches"] >= 2
    assert "touch" in r["phrase"]


def test_touches_count_visits_not_bars():
    """A name that ranged near a level for months has a couple of touches, not the 40-odd
    bars that sat inside the tolerance band. Live data reported a '41th touch'."""
    # 60 consecutive bars pinned to the level, then one clean departure and return.
    series = [100.0] * 60 + _leg(100, 115, 10) + _leg(115, 100, 10)
    h, l, c = _ohlc(series, spread=0.1)
    r = detect_structure(h, l, c, supports=[99.9])
    assert r["level"] is not None
    assert r["level"]["touches"] <= 3, f"counted bars, not visits: {r['level']['touches']}"


@pytest.mark.parametrize("n,expected", [
    (1, "first"), (2, "second"), (3, "third"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (22, "22nd"), (23, "23rd"), (41, "41st"), (111, "111th"),
])
def test_ordinals_are_well_formed(n, expected):
    from analysis.structure import _ordinal
    assert _ordinal(n) == expected


def test_rich_levels_agree_with_nearest_support():
    """structure.py used to answer "which level are we on" from the raw price list while
    nearest_support answered from the strength-ranked one. Live 2026-08-05: AMT read "at
    support $165.79 (12th touch)" while nearest_support was 161.61 — two answers, one scan."""
    h, l, c = _ohlc(_leg(100, 100, 60) + _leg(100, 120, 30) + _leg(120, 101, 20))
    rich = [{"price": 100.5, "touches": 4, "strength": 70.0}]
    r = detect_structure(h, l, c, supports=rich)
    assert r["level"] is not None
    assert r["level"]["price"] == 100.5
    assert r["level"]["touches"] == 4          # clustered count, not a bar tally


def test_weak_rich_level_does_not_claim_at_support():
    """Announcing "at support" about a level the strength gate already rejected overstates
    it — AMT headlined a first touch scoring 11.7 against a 12.0 floor."""
    h, l, c = _ohlc(_leg(100, 100, 60) + _leg(100, 120, 30) + _leg(120, 101, 20))
    weak = [{"price": 100.5, "touches": 1, "strength": 2.0}]
    r = detect_structure(h, l, c, supports=weak)
    assert r["level"] is None
    assert "at support" not in r["phrase"]


def test_rich_levels_do_not_fall_through_to_the_price_path():
    """A dict reaching the float-based fallback raised TypeError: float - dict."""
    h, l, c = _ohlc(_leg(100, 130, 90))
    r = detect_structure(h, l, c, supports=[{"price": 1.0, "touches": 1, "strength": 0.0}])
    assert r["level"] is None                  # no crash, no bogus level


def test_bare_price_lists_still_work():
    """Legacy callers pass plain floats; that path must survive."""
    series = []
    for _ in range(4):
        series += _leg(110, 100, 8) + _leg(100, 110, 8)
    series += _leg(110, 100, 8)
    r = detect_structure(*_ohlc(series, spread=0.2), supports=[99.8])
    assert r["level"] is not None and r["level"]["kind"] == "support"


def test_level_far_from_price_is_ignored():
    h, l, c = _ohlc(_leg(100, 160, 120))
    assert detect_structure(h, l, c, supports=[50.0])["level"] is None


# ── Config wiring ─────────────────────────────────────────────────────────────────────────────

def test_disabling_via_min_bars_is_honoured(monkeypatch):
    h, l, c = _ohlc(_leg(100, 130, 60))
    assert detect_structure(h, l, c)["pattern"] != UNREADABLE
    monkeypatch.setattr(config, "STRUCTURE_MIN_BARS", 500, raising=False)
    assert detect_structure(h, l, c)["pattern"] == UNREADABLE


def test_impulse_threshold_is_read_from_config(monkeypatch):
    series = _leg(100, 100, 40) + _leg(100, 130, 20) + _leg(130, 126, 4)
    assert detect_structure(*_ohlc(series))["pattern"] == BULL_FLAG
    monkeypatch.setattr(config, "STRUCTURE_FLAG_MIN_IMPULSE_PCT", 95.0, raising=False)
    assert detect_structure(*_ohlc(series))["pattern"] != BULL_FLAG


def test_output_shape_is_stable():
    """The cockpit and entry_timing both index this dict; keys must not silently vanish."""
    r = detect_structure(*_ohlc(_leg(100, 130, 80)))
    for key in ("pattern", "stage", "phrase", "detail", "swing_structure", "impulse_pct",
                "retracement_pct", "level", "contracting", "volume_drying",
                "momentum_flattening", "confidence"):
        assert key in r, f"missing {key}"
