"""Entry timing (pattern phase) — analysis/entry_timing.py + its wiring into strategies.evaluate.

Origin: Josh's QQQ bull put, opened 2026-07-15 with RSI ~62 and closed 2026-08-04 at the 50%
target. Every hard gate passed, but the signal fired early in the pullback — put skew had not
steepened yet, so the credit collected for that delta was thinner than it would have been at the
2026-07-28/30 dip. This module exists to say so at signal time.

The load-bearing property under test is that timing is ADVISORY. multi_strategy.py (bear call,
iron condor) and lottery_scanner.py both do `if not ev["qualified"]: return None`. If a failing
timing row ever counts toward `qualified`, those candidates silently vanish from the board with
no error and no log line — the worst possible failure mode for a scanner.
"""
import pytest

import config
import strategies
from analysis.entry_timing import (
    PHASE_AT_SUPPORT,
    PHASE_EARLY_BOUNCE,
    PHASE_EARLY_PULLBACK,
    PHASE_EXTENDED,
    PHASE_OVERBOUGHT_FADE,
    PHASE_OVERSOLD_BOUNCE,
    PHASE_RANGE_CENTER,
    PHASE_TREND_CONFLICT,
    assess_entry_timing,
)


def _tech(**overrides):
    """An uptrending name mid-consolidation; tests move only what they mean to move."""
    t = {"rsi": 50.0, "macd_crossover": "bearish", "price": 196.0,
         "sma20": 200.0, "sma50": 190.0, "trend": "UP"}
    t.update(overrides)
    return t


# ── The originating QQQ trade ─────────────────────────────────────────────────────────────────

def test_qqq_july_15_entry_flags_early():
    """RSI 62 above SMA20 with MACD rolling over: the pullback had not matured."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=62, price=700.0, sma20=694.0,
                                                     sma50=680.0, trend="UP"),
                            current_price=700.0)
    assert r["phase"] == PHASE_EARLY_PULLBACK
    assert r["readiness"] == "EARLY"
    assert r["timing_gate_pass"] is False


def test_qqq_july_29_dip_is_optimal():
    """Same name ~2 weeks later at the dip floor near support — the entry the spec argues for."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=41, price=684.0, sma20=694.0,
                                                     sma50=680.0, nearest_support=672.0,
                                                     trend="UP"),
                            current_price=684.0)
    assert r["readiness"] == "OPTIMAL"


# ── Bull put phases ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rsi,expected_phase", [
    (34, PHASE_OVERSOLD_BOUNCE),
    (44, "REVERSAL_SETUP"),
    (62, PHASE_EARLY_PULLBACK),
    (72, PHASE_EXTENDED),
])
def test_bull_put_phase_ladder(rsi, expected_phase):
    assert assess_entry_timing("bull_put_spread", _tech(rsi=rsi))["phase"] == expected_phase


def test_bull_put_at_support_when_price_hugs_the_floor():
    r = assess_entry_timing("bull_put_spread", _tech(rsi=47, price=201.0, nearest_support=200.0))
    assert r["phase"] == PHASE_AT_SUPPORT
    assert r["readiness"] == "OPTIMAL"


def test_low_rsi_in_a_downtrend_is_not_a_dip_buy():
    """A falling knife is not a mature pullback. Without the trend check the module would call
    a STRONG_DOWN breakdown 'deeply oversold within the uptrend' and label it OPTIMAL."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=30, price=180.0, trend="STRONG_DOWN"))
    assert r["phase"] == PHASE_TREND_CONFLICT
    assert r["readiness"] == "CAUTION"


# ── Bear call phases (inverted thesis) ────────────────────────────────────────────────────────

def test_bear_call_optimal_on_an_extended_bounce():
    r = assess_entry_timing("bear_call_spread",
                            _tech(rsi=68, macd_crossover="bullish", price=184.0,
                                  sma20=180.0, trend="DOWN"))
    assert r["phase"] == PHASE_OVERBOUGHT_FADE
    assert r["readiness"] == "OPTIMAL"


def test_bear_call_early_when_the_bounce_has_not_started():
    r = assess_entry_timing("bear_call_spread",
                            _tech(rsi=38, price=185.0, sma20=190.0, trend="DOWN"))
    assert r["phase"] == PHASE_EARLY_BOUNCE
    assert r["timing_gate_pass"] is False


def test_bear_call_into_strength_flags_trend_conflict():
    r = assess_entry_timing("bear_call_spread",
                            _tech(rsi=66, macd_crossover="bullish", price=195.0,
                                  sma20=190.0, trend="STRONG_UP"))
    assert r["phase"] == PHASE_TREND_CONFLICT


# ── Routing ───────────────────────────────────────────────────────────────────────────────────

def test_condor_routes_before_the_put_call_substring_tests():
    """'iron_condor' contains neither 'bull_put' nor 'bear_call', but a condor label that ever
    picks up a leg word must not fall through to a directional thesis."""
    r = assess_entry_timing("iron_condor", _tech(rsi=50, macd_crossover=None, trend="NEUTRAL"))
    assert r["phase"] == PHASE_RANGE_CENTER


def test_lottery_gets_no_timing_thesis():
    r = assess_entry_timing("long_call_lottery", _tech(rsi=62))
    assert r["readiness"] == "NEUTRAL"
    assert r["timing_gate_pass"] is True


def test_missing_rsi_defaults_to_neutral_not_a_crash():
    r = assess_entry_timing("bull_put_spread", {"trend": "UP"})
    assert r["rsi_at_signal"] == 50.0
    assert r["timing_gate_pass"] is True


def test_empty_tech_is_survivable():
    assert assess_entry_timing("bull_put_spread", {})["timing_gate_pass"] is True
    assert assess_entry_timing("bull_put_spread", None)["timing_gate_pass"] is True


# ── The regression that matters: advisory must never disqualify ───────────────────────────────

def _passing_ctx(**overrides):
    ctx = {"dte": 30, "short_delta": -0.23, "credit_to_width": 0.57, "iv_rank": 68,
           "trend": "up", "pop": 0.84, "sentiment": "NEUTRAL"}
    ctx.update(overrides)
    return ctx


@pytest.mark.parametrize("strategy,ctx_extra", [
    ("bull_put", {}),
    ("bear_call", {"short_delta": 0.20, "credit_to_width": 0.22,
                   "trend": "down", "sentiment": "NEGATIVE"}),
    ("iron_condor", {"short_delta": 0.14, "credit_to_width": 0.32, "iv_rank": 48,
                     "trend": "flat", "pop": 0.70}),
])
def test_failing_timing_never_blocks(strategy, ctx_extra):
    """multi_strategy.py returns None on `not qualified` — if this breaks, bear calls and
    condors disappear from the board whenever RSI sits in the wrong part of its range."""
    timing = {"readiness": "EARLY", "phase": "EARLY_PULLBACK", "readiness_icon": "!",
              "timing_gate_pass": False, "rsi_at_signal": 62.0}
    ev = strategies.evaluate(strategy, _passing_ctx(entry_timing=timing, **ctx_extra))
    assert ev["qualified"] is True
    row = next(c for c in ev["criteria"] if c["label"].startswith("Entry timing"))
    assert row["advisory"] is True
    assert row["ok"] is False          # still renders amber in the cockpit


def test_hard_gates_still_block_when_timing_is_optimal():
    """The advisory carve-out must be surgical: it exempts the timing row, nothing else."""
    timing = {"readiness": "OPTIMAL", "phase": "REVERSAL_SETUP",
              "timing_gate_pass": True, "rsi_at_signal": 44.0}
    ev = strategies.evaluate("bull_put", _passing_ctx(sentiment="NEGATIVE", entry_timing=timing))
    assert ev["qualified"] is False


def test_no_timing_row_when_assessment_absent():
    """Disabled module / failed assessment returns {} — evaluate() must stay byte-identical."""
    ev = strategies.evaluate("bull_put", _passing_ctx(entry_timing={}))
    assert ev["qualified"] is True
    assert not any(c["label"].startswith("Entry timing") for c in ev["criteria"])


def test_existing_criteria_rows_are_not_marked_advisory():
    """Only timing is exempt; every pre-existing gate must still count toward `qualified`."""
    ev = strategies.evaluate("bull_put", _passing_ctx())
    assert all(not c.get("advisory") for c in ev["criteria"])


# ── Config wiring (the spec shipped these as dead constants) ──────────────────────────────────

def test_thresholds_are_read_from_config_not_hardcoded(monkeypatch):
    """RSI 62 is EARLY under the default 58 threshold; raising the threshold must move it."""
    assert assess_entry_timing("bull_put_spread", _tech(rsi=62))["phase"] == PHASE_EARLY_PULLBACK
    monkeypatch.setattr(config, "BULL_PUT_EARLY_RSI_MIN", 65, raising=False)
    monkeypatch.setattr(config, "BULL_PUT_OPTIMAL_RSI_MAX", 63, raising=False)
    assert assess_entry_timing("bull_put_spread", _tech(rsi=62))["phase"] != PHASE_EARLY_PULLBACK


# ── Cockpit render path ───────────────────────────────────────────────────────────────────────

def test_cockpit_surfaces_timing_end_to_end():
    """The spec claimed the criteria panel would render 'automatically'. It does — but only
    because entry_timing is also threaded through vega_app's card normalizer."""
    import vega_app

    timing = assess_entry_timing("bull_put_spread", _tech(rsi=62, price=700.0, sma20=694.0),
                                 current_price=700.0)
    ev = strategies.evaluate("bull_put", _passing_ctx(entry_timing=timing))
    card = {"ticker": "QQQ", "strat_type": "bull_put", "short": 690, "long": 685,
            "credit_ps": 1.45, "exp": "2026-09-19", "nearest_support": 672.0,
            "criteria": ev["criteria"], "news_check": ev["news_check"],
            "entry_timing": timing, "breakevens": [688.55],
            "gates_passed": 8, "gates_total": 8}

    # gate_detail_table is what the drawer actually calls; _criteria_panel is currently
    # unreferenced, so asserting only on it would have passed while showing the user nothing.
    assert "Entry timing" in vega_app.gate_detail_table(card)
    assert "chart structure" in vega_app._timing_block(card)
    assert "TIMING" in vega_app._brief_ticket(card)

    # A clean setup leaves the order ticket alone.
    card["entry_timing"] = assess_entry_timing("bull_put_spread", _tech(rsi=44))
    assert "TIMING" not in vega_app._brief_ticket(card)


# ── Structure blended into the momentum read ──────────────────────────────────────────────────

def _struct(pattern, stage="LATE", confidence="MEDIUM", phrase="a shape", detail=""):
    return {"pattern": pattern, "stage": stage, "confidence": confidence,
            "phrase": phrase, "detail": detail}


def test_structure_absent_leaves_the_momentum_read_untouched():
    tech = _tech(rsi=62)
    assert (assess_entry_timing("bull_put_spread", tech)["readiness"]
            == assess_entry_timing("bull_put_spread", tech, structure=None)["readiness"])


def test_early_flag_caps_an_optimistic_momentum_read():
    """RSI can say OPTIMAL while the pause has barely begun. The shape wins: nothing has
    repriced yet, so the credit is still thin."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("BULL_FLAG", "EARLY", phrase="early in a bull flag"))
    assert r["momentum_readiness"] == "OPTIMAL"
    assert r["readiness"] == "EARLY"
    assert r["timing_gate_pass"] is False
    assert "Early in a bull flag" in r["headline"]


def test_late_flag_promotes_a_lukewarm_momentum_read():
    r = assess_entry_timing("bull_put_spread", _tech(rsi=53),
                            structure=_struct("BULL_FLAG", "LATE", phrase="late in a bull flag"))
    assert r["momentum_readiness"] == "WATCH"
    assert r["readiness"] == "OPTIMAL"


def test_double_top_vetoes_a_bull_put():
    """The case RSI cannot see: price rejected at a prior peak. A short put here sits under
    a failed breakout, not under a resting flag."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("DOUBLE_TOP", phrase="second peak of a double top"))
    assert r["readiness"] == "CAUTION"
    assert r["timing_gate_pass"] is False


def test_double_top_favours_a_bear_call():
    """Same shape, opposite side of the trade — the ceiling above the short call is confirmed."""
    r = assess_entry_timing("bear_call_spread",
                            _tech(rsi=52, macd_crossover="bullish", price=178, sma20=175,
                                  trend="DOWN"),
                            structure=_struct("DOUBLE_TOP", phrase="second peak of a double top"))
    assert r["readiness"] == "OPTIMAL"


def test_extended_uptrend_caps_a_bull_put_at_early():
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("UPTREND_EXTENDED", "EARLY",
                                              phrase="extended, no pullback yet"))
    assert r["readiness"] == "EARLY"


def test_range_confirms_a_condor_and_a_trend_vetoes_it():
    ranged = assess_entry_timing("iron_condor", _tech(rsi=50, trend="NEUTRAL"),
                                 structure=_struct("RANGE", "MID", phrase="range-bound"))
    assert ranged["readiness"] == "OPTIMAL"
    trending = assess_entry_timing("iron_condor", _tech(rsi=50, trend="NEUTRAL"),
                                   structure=_struct("UPTREND_EXTENDED", "EARLY"))
    assert trending["readiness"] == "CAUTION"


def test_low_confidence_structure_is_reported_but_never_acts():
    """A misread shape must not override a measured RSI — this is the guard that keeps a
    heuristic from overruling an observation."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("DOUBLE_TOP", confidence="LOW",
                                              phrase="second peak of a double top"))
    assert r["readiness"] == "OPTIMAL"          # unchanged by the weak read
    assert "too weak to weigh" in r["reason"]


def test_structure_cannot_promote_past_a_regime_conflict():
    """Live WMT: trend DOWN (momentum CAUTION) with a double bottom, promoted straight to
    OPTIMAL. 'The tape is against you' is a measurement; 'this looks like a base' is a
    guess, and the guess must not overrule the measurement."""
    tech = _tech(rsi=46, price=180.0, trend="STRONG_DOWN")
    assert assess_entry_timing("bull_put_spread", tech)["phase"] == PHASE_TREND_CONFLICT
    r = assess_entry_timing("bull_put_spread", tech,
                            structure=_struct("DOUBLE_BOTTOM",
                                              phrase="second trough of a double bottom"))
    assert r["readiness"] == "CAUTION"
    assert "regime contradicts" in r["reason"]


def test_a_ceiling_still_applies_under_a_regime_conflict():
    """Only promotion is blocked — structure that makes things worse must still bite."""
    tech = _tech(rsi=46, price=180.0, trend="STRONG_DOWN")
    r = assess_entry_timing("bull_put_spread", tech,
                            structure=_struct("DOUBLE_TOP", phrase="second peak of a double top"))
    assert r["readiness"] == "CAUTION"


def test_unreadable_structure_does_not_headline_the_chip():
    """Live ADBE headlined as 'Structure unreadable', which tells the trader nothing."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=62),
                            structure=_struct("UNREADABLE", "N/A", confidence="LOW",
                                              phrase="structure unreadable"))
    assert "unreadable" not in r["headline"].lower()
    assert r["headline"] == "Early Pullback"


def test_unknown_pattern_does_not_move_the_rating():
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("UNREADABLE", "N/A", phrase="structure unreadable"))
    assert r["readiness"] == "OPTIMAL"


def test_reason_records_when_structure_overruled_momentum():
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("BULL_FLAG", "EARLY", phrase="early in a bull flag"))
    assert "Momentum alone read OPTIMAL" in r["reason"]
    assert "structure moved it to EARLY" in r["reason"]


def test_icon_follows_the_final_rating_not_the_original_phase():
    """The phase that produced the momentum rating may now disagree with the final one; a
    green tick beside a CAUTION verdict would be actively misleading."""
    r = assess_entry_timing("bull_put_spread", _tech(rsi=44),
                            structure=_struct("DOUBLE_TOP", phrase="second peak of a double top"))
    assert r["readiness"] == "CAUTION"
    assert r["readiness_icon"] == "⚠"


def test_timing_block_is_reachable_from_the_drawer():
    """The panel was first hung off _criteria_panel, which nothing calls — it rendered
    correctly in isolation and was invisible in the cockpit. Assert it is actually wired
    into detail_drawer(), the function the board really uses."""
    import inspect

    import vega_app

    assert "_timing_block(" in inspect.getsource(vega_app.detail_drawer)
