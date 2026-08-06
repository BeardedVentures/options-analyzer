"""Support / resistance detection — analysis/levels.py.

Origin: the old inline 2-bar-fractal scan in data/technicals.py marked every two-day wiggle
as a level. Measured on 2026-08-05, `nearest_support` sat 0.0% below spot for QQQ, 0.3% for
XLE and 0.6% for WMT — i.e. it was yesterday's low, not a level. Everything downstream
inherited that: entry_timing's AT_SUPPORT (3% proximity) fired on nearly every name, and the
order ticket printed "exit if it breaks support" against a 0.02% move.

The property that makes a level a level is that the market tested it MORE THAN ONCE. So the
tests below care mostly about clustering and strength ranking, not about exact prices.
"""
import pytest

import config
from analysis.levels import _cluster, _strength, find_levels, strike_cushion


def _bars(closes, spread=0.4):
    return [c + spread for c in closes], [c - spread for c in closes], list(closes)


def _leg(a, b, n):
    return [a + (b - a) / n * (i + 1) for i in range(n)]


def _sawtooth(low, high, cycles, bars_per_leg=12):
    """Repeated tests of the same floor and ceiling — the canonical 'real level' shape."""
    out = [float(high)]
    for _ in range(cycles):
        out += _leg(high, low, bars_per_leg) + _leg(low, high, bars_per_leg)
    return out


# ── Clustering ────────────────────────────────────────────────────────────────────────────────

def test_repeated_tests_collapse_into_one_level():
    """Four visits to ~$100 are ONE level tested four times, not four levels. The old code
    kept them separately and burned all three output slots on a single price area."""
    h, l, c = _bars(_sawtooth(100, 120, cycles=4))
    r = find_levels(h, l, c)
    near_100 = [lv for lv in r["support_levels"] + r["resistance_levels"]
                if abs(lv["price"] - 100) / 100 < 0.03]
    assert len(near_100) == 1, f"did not cluster: {near_100}"
    assert near_100[0]["touches"] >= 3


def test_distinct_prices_stay_distinct():
    piv = [(10, 100.0, "L"), (30, 100.5, "L"), (50, 140.0, "H")]
    out = _cluster(piv, tol=0.01, n_bars=60)
    assert len(out) == 2
    assert {o["touches"] for o in out} == {2, 1}


def test_cluster_price_is_recency_weighted():
    """Levels drift; where a level sits now matters more than where it sat months ago."""
    out = _cluster([(0, 100.0, "L"), (99, 101.0, "L")], tol=0.05, n_bars=100)
    assert len(out) == 1
    assert out[0]["price"] > 100.5


def test_polarity_flip_is_recorded():
    out = _cluster([(10, 100.0, "H"), (60, 100.2, "L")], tol=0.01, n_bars=100)
    assert out[0]["flipped"] is True


# ── Strength ──────────────────────────────────────────────────────────────────────────────────

def test_more_touches_scores_higher():
    a = {"touches": 1, "last_touch_bars_ago": 10, "flipped": False}
    b = {"touches": 4, "last_touch_bars_ago": 10, "flipped": False}
    assert _strength(b, 180) > _strength(a, 180)


def test_recent_touch_scores_higher_than_a_stale_one():
    recent = {"touches": 2, "last_touch_bars_ago": 5, "flipped": False}
    stale = {"touches": 2, "last_touch_bars_ago": 170, "flipped": False}
    assert _strength(recent, 180) > _strength(stale, 180) * 2


def test_flip_adds_conviction():
    plain = {"touches": 2, "last_touch_bars_ago": 10, "flipped": False}
    flipped = {"touches": 2, "last_touch_bars_ago": 10, "flipped": True}
    assert _strength(flipped, 180) > _strength(plain, 180)


def test_strength_is_bounded():
    huge = {"touches": 50, "last_touch_bars_ago": 0, "flipped": True}
    assert 0 <= _strength(huge, 180) <= 100


# ── nearest_* quality gate ────────────────────────────────────────────────────────────────────

def test_nearest_support_skips_a_weak_accident():
    """The whole point. A single stale low must not become the number the order ticket
    prints as an invalidation when a well-tested level sits just below it."""
    # Strong, repeatedly-tested floor at 100; one incidental dip to ~108 long ago.
    closes = _sawtooth(100, 120, cycles=4) + _leg(120, 108, 6) + _leg(108, 118, 6)
    closes += _sawtooth(100, 120, cycles=2)[1:]
    closes += _leg(120, 112, 5)
    h, l, c = _bars(closes)
    r = find_levels(h, l, c)
    ns = r["nearest_support"]
    assert ns is not None
    chosen = [lv for lv in r["support_levels"] if lv["price"] == ns]
    assert chosen and chosen[0]["strength"] >= config.LEVELS_MIN_STRENGTH


def test_nearest_falls_back_when_everything_is_weak():
    """Something is better than nothing — but only after the quality gate finds no taker."""
    h, l, c = _bars(_leg(100, 130, 80))
    r = find_levels(h, l, c)
    if r["support_levels"]:
        assert r["nearest_support"] is not None


def test_supports_are_below_and_resistances_above_spot():
    h, l, c = _bars(_sawtooth(100, 120, cycles=4) + _leg(120, 110, 5))
    r = find_levels(h, l, c)
    price = c[-1]
    assert all(lv["price"] < price for lv in r["support_levels"])
    assert all(lv["price"] > price for lv in r["resistance_levels"])


def test_supports_ordered_nearest_first():
    h, l, c = _bars(_sawtooth(100, 130, cycles=4) + _leg(130, 120, 5))
    sup = find_levels(h, l, c)["support_levels"]
    dists = [abs(c[-1] - lv["price"]) for lv in sup]
    assert dists == sorted(dists)


# ── Contract / guard rails ────────────────────────────────────────────────────────────────────

def test_legacy_keys_are_preserved():
    """data/technicals.py, entry_timing, structure, synthesizer and vega_app all read the
    legacy float-list shape; it must survive the rewrite."""
    h, l, c = _bars(_sawtooth(100, 120, cycles=4))
    r = find_levels(h, l, c)
    for key in ("supports", "resistances", "nearest_support", "nearest_resistance",
                "52w_high", "52w_low", "support_levels", "resistance_levels"):
        assert key in r
    assert all(isinstance(x, float) for x in r["supports"])
    assert all(isinstance(x, float) for x in r["resistances"])


def test_too_little_history_returns_no_levels():
    h, l, c = _bars(_leg(100, 110, 10))
    r = find_levels(h, l, c)
    assert r["supports"] == [] and r["nearest_support"] is None


def test_empty_input_survives():
    r = find_levels([], [], [])
    assert r["nearest_support"] is None and r["support_levels"] == []


def test_keep_per_side_is_honoured(monkeypatch):
    h, l, c = _bars(_sawtooth(100, 130, cycles=5))
    monkeypatch.setattr(config, "LEVELS_KEEP_PER_SIDE", 1, raising=False)
    r = find_levels(h, l, c)
    assert len(r["support_levels"]) <= 1 and len(r["resistances"]) <= 1


def test_flat_series_does_not_crash():
    h, l, c = _bars([100.0] * 200, spread=0.01)
    assert find_levels(h, l, c)["support_levels"] is not None


# ── strike_cushion: the geometry the old score had backwards ──────────────────────────────────

def test_put_cushion_requires_support_ABOVE_the_short_strike():
    """For a bull put you want price to break a defended level BEFORE the strike is
    threatened — so support must sit above the strike. data/technicals.py scored the
    opposite for months."""
    levels = [{"price": 105.0, "strength": 60.0, "touches": 3},
              {"price": 90.0, "strength": 55.0, "touches": 3}]
    shielded = strike_cushion(100.0, levels, "put")
    assert shielded is not None and shielded["level"] == 105.0
    assert shielded["buffer_pct"] == pytest.approx(0.05, abs=1e-6)

    # A strike beneath every level is maximally shielded — everything stands over it.
    assert strike_cushion(85.0, levels, "put")["level"] == 105.0
    # The unshielded case is a strike ABOVE every support: price reaches it untested.
    assert strike_cushion(110.0, levels, "put") is None


def test_call_cushion_requires_resistance_BELOW_the_short_strike():
    levels = [{"price": 95.0, "strength": 60.0, "touches": 3},
              {"price": 130.0, "strength": 55.0, "touches": 3}]
    shielded = strike_cushion(100.0, levels, "call")
    assert shielded is not None and shielded["level"] == 95.0
    # Unshielded: a short call BELOW every resistance is reached before any level is tested.
    assert strike_cushion(90.0, levels, "call") is None


def test_cushion_prefers_the_strongest_shield():
    levels = [{"price": 105.0, "strength": 20.0, "touches": 1},
              {"price": 112.0, "strength": 80.0, "touches": 4}]
    assert strike_cushion(100.0, levels, "put")["level"] == 112.0


def test_min_buffer_rejects_an_illusory_shield():
    """Live SPY 2026-08-05: a 3-touch support sat 0.16% above a candidate strike. Breaking
    that support lands directly on the strike, so it shelters nothing — and without a floor,
    selection paid 1.4 points of ROC for it."""
    levels = [{"price": 100.16, "strength": 55.0, "touches": 3}]
    assert strike_cushion(100.0, levels, "put") is not None          # scoring: partial credit
    assert strike_cushion(100.0, levels, "put", min_buffer_pct=0.005) is None  # selection: no


def test_min_buffer_keeps_a_genuine_shield():
    levels = [{"price": 103.0, "strength": 55.0, "touches": 3}]
    assert strike_cushion(100.0, levels, "put", min_buffer_pct=0.005) is not None


def test_cushion_handles_missing_inputs():
    assert strike_cushion(None, [{"price": 1, "strength": 1, "touches": 1}], "put") is None
    assert strike_cushion(100.0, [], "put") is None
