"""The remaining S/R gaps closed on 2026-08-05.

Three places where levels were computed and then not used:

  1. `nearest_resistance` was a parameter of the bear-call phase detector and was never read
     in its body — so a bounce running into a twice-rejected ceiling scored identically to
     one in open air, while bull put had had AT_SUPPORT all along.
  2. Every order ticket says "exit if it breaks support on volume", but trade management was
     purely price/DTE based. Nothing watched, so the instruction was dead after entry.
  3. `approaching_support` was computed in data/technicals.py and consumed by nothing.
"""
import pytest

import config
from analysis.entry_timing import PHASE_AT_RESISTANCE, PHASE_AT_SUPPORT, assess_entry_timing


# ── 1. Bear call now reads resistance ─────────────────────────────────────────────────────────

def _bear_tech(**over):
    t = {"rsi": 56, "macd_crossover": "bullish", "price": 178.0, "sma20": 175.0,
         "trend": "DOWN", "nearest_resistance": 180.0}
    t.update(over)
    return t


def test_bear_call_recognises_price_at_resistance():
    r = assess_entry_timing("bear_call_spread", _bear_tech())
    assert r["phase"] == PHASE_AT_RESISTANCE
    assert r["readiness"] == "OPTIMAL"
    assert "resistance" in r["reason"].lower()


def test_bear_call_without_resistance_does_not_claim_one():
    r = assess_entry_timing("bear_call_spread", _bear_tech(nearest_resistance=None))
    assert r["phase"] != PHASE_AT_RESISTANCE


def test_distant_resistance_is_not_at_resistance():
    r = assess_entry_timing("bear_call_spread", _bear_tech(nearest_resistance=260.0))
    assert r["phase"] != PHASE_AT_RESISTANCE


def test_the_two_sides_are_now_symmetric():
    """Bull put had AT_SUPPORT from the start; bear call silently had no counterpart."""
    put = assess_entry_timing("bull_put_spread",
                              {"rsi": 47, "macd_crossover": "bearish", "price": 201.0,
                               "sma20": 210.0, "trend": "UP", "nearest_support": 200.0})
    call = assess_entry_timing("bear_call_spread", _bear_tech())
    assert put["phase"] == PHASE_AT_SUPPORT
    assert call["phase"] == PHASE_AT_RESISTANCE
    assert put["readiness"] == call["readiness"] == "OPTIMAL"


# ── 2. Management-time breach alerts ──────────────────────────────────────────────────────────

class _Series(list):
    @property
    def iloc(self):
        return self

    def tolist(self):
        return list(self)


class _FakeDF:
    """Minimal stand-in for the OHLCV frame the fetcher returns."""
    empty = False

    def __init__(self, spot):
        self._spot = spot

    def __getitem__(self, key):
        return _Series([self._spot] * 200)


def _stub_market(monkeypatch, spot, support_price=100.0):
    """Neutralise BOTH import paths into the alert helper.

    `from data import fetcher` resolves the package ATTRIBUTE when data.fetcher has already
    been imported (which it has, as soon as any other test imports main), so patching
    sys.modules alone silently leaves the real fetcher in place. That is not hypothetical:
    it made this test pass alone and fail in the suite, because a live lookup of the literal
    ticker "TEST" returned real data trading below the stubbed level.
    """
    import sys

    import analysis
    import data

    fake_fetcher = type(sys)("fetcher")
    # Levels are compared against a STRIKE, so _level_breach_alerts reads the RAW series
    # (2026-09-02). Both are stubbed: the raw one is what it calls, and the adjusted one is the
    # documented fallback, so a regression that quietly reverts to adjusted prices still fails
    # this test rather than passing on the fallback.
    fake_fetcher.get_raw_price_data = lambda t, period=None: _FakeDF(spot)
    fake_fetcher.get_price_data = lambda t, period=None: None
    monkeypatch.setattr(data, "fetcher", fake_fetcher, raising=False)
    monkeypatch.setitem(sys.modules, "data.fetcher", fake_fetcher)

    fake_levels = type(sys)("levels")
    fake_levels.find_levels = lambda *a, **k: {
        "support_levels": [{"price": support_price, "strength": 60.0, "touches": 3,
                            "last_touch_bars_ago": 5, "flipped": False}]}
    monkeypatch.setattr(analysis, "levels", fake_levels, raising=False)
    monkeypatch.setitem(sys.modules, "analysis.levels", fake_levels)


def test_breach_alert_fires_when_a_shielding_level_is_lost(monkeypatch):
    """Spot has fallen through a level that stood above the short strike: the structural
    thesis is broken and the strike is next in line."""
    import auto_paper_cycle as apc

    logged = []
    monkeypatch.setattr(apc, "_log", lambda m: logged.append(m))
    _stub_market(monkeypatch, spot=95.0)      # below the 100 level, above the 92 strike

    assert apc._level_breach_alerts("TEST", [{"id": "T1", "short_strike": 92.0}]) == 1
    assert any("LEVEL-ALERT" in m for m in logged)


def test_breach_alert_is_silent_when_the_shield_holds(monkeypatch):
    import auto_paper_cycle as apc

    monkeypatch.setattr(apc, "_log", lambda m: None)
    _stub_market(monkeypatch, spot=105.0)     # still above the level

    assert apc._level_breach_alerts("TEST", [{"id": "T1", "short_strike": 92.0}]) == 0


def test_breach_alert_ignores_levels_below_the_short_strike(monkeypatch):
    """A level under the strike was never a shield, so losing it is not this alert's news."""
    import auto_paper_cycle as apc

    monkeypatch.setattr(apc, "_log", lambda m: None)
    _stub_market(monkeypatch, spot=95.0, support_price=90.0)

    assert apc._level_breach_alerts("TEST", [{"id": "T1", "short_strike": 92.0}]) == 0


def test_breach_alerts_never_close_a_position():
    """The load-bearing constraint. Auto-closing on a support break would cut winners that
    dip and recover — a strategy change, not a gap fix, and Josh's call to make."""
    import inspect

    import auto_paper_cycle as apc
    src = inspect.getsource(apc._level_breach_alerts)
    assert "set_close" not in src
    assert "_apply_close_rules" not in src


def test_breach_alerts_are_config_gated(monkeypatch):
    import auto_paper_cycle as apc
    monkeypatch.setattr(config, "LEVEL_MANAGEMENT_ALERTS", False, raising=False)
    assert apc._level_breach_alerts("TEST", [{"id": "X", "short_strike": 1.0}]) == 0


def test_breach_alert_failure_is_swallowed(monkeypatch):
    """A bad level read must never break the paper cycle."""
    import sys

    import auto_paper_cycle as apc
    monkeypatch.setattr(apc, "_log", lambda m: None)
    boom = type(sys)("fetcher")
    def _raise(*a, **k):
        raise RuntimeError("network down")
    boom.get_raw_price_data = _raise
    boom.get_price_data = _raise
    monkeypatch.setitem(sys.modules, "data.fetcher", boom)
    assert apc._level_breach_alerts("TEST", [{"id": "X", "short_strike": 1.0}]) == 0


# ── 3. approaching_* signals ──────────────────────────────────────────────────────────────────

def test_approaching_signals_distinguish_absent_from_far():
    """None (no level found) and False (level far away) are different situations; the old
    boolean-only form collapsed them, which is part of why nothing consumed it."""
    import inspect

    from data import technicals
    src = inspect.getsource(technicals.calculate_all)
    assert "approaching_resistance" in src
    assert "None if not nearest_support" in src
