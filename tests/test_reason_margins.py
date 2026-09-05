"""Rejection counters now record BY HOW MUCH, not just how many.

The counters answer "which gate kills the most" and cannot answer "and by how much" — which is
the question that decides whether a threshold is nearly right or badly wrong.

Measured 2026-09-04: `quote_spread` is 33.8% of enumeration rejections and only 6 of 441
one-gate-away spreads. Both are true because it filters LEGS before pairing while
`credit_to_width` filters the SPREADS those legs become. A leg killed at the quote gate never
gets to fail or pass anything downstream, so a near-miss count taken over surviving spreads
structurally cannot see it. The margin is the only way to size a stage that filters upstream of
the stage being counted.
"""
import pytest

import config
import main


def _leg(bid, ask, mid=None, delta=-0.20, strike=95.0, vol=500, oi=1000, **kw):
    o = {"type": "put", "strike": strike, "delta": delta, "bid": bid, "ask": ask,
         "mid": mid if mid is not None else (bid + ask) / 2,
         "volume": vol, "open_interest": oi, "expiration": "2026-10-16", "dte": 35}
    o.update(kw)
    return o


def _diag(chain, price=100.0):
    d = {}
    main.select_bull_put_pair(chain, price, "TEST", diagnostics=d)
    return d


# ── the helpers ──────────────────────────────────────────────────────────────

def test_spread_ratio_matches_the_gate_it_reports_on():
    assert main._spread_ratio(_leg(1.00, 2.00, mid=1.50)) == pytest.approx(0.6667, abs=1e-3)
    assert main._spread_ratio(_leg(0.53, 0.55, mid=0.54)) == pytest.approx(0.037, abs=1e-3)


def test_spread_ratio_refuses_a_one_sided_book():
    assert main._spread_ratio(_leg(0.0, 1.20, mid=0.60)) is None
    assert main._spread_ratio(_leg(2.00, 1.00, mid=1.50)) is None


def test_quantiles_describe_the_distribution_not_a_threshold():
    q = main._quantiles([0.40, 0.45, 0.50, 0.60, 0.90])
    assert q["n"] == 5 and q["min"] == 0.40 and q["max"] == 0.90
    assert q["median"] == 0.50
    # deliberately no "would_pass_at_X" key — that would privilege a candidate constant
    assert not any("pass" in k or "admit" in k for k in q)


def test_quantiles_on_an_empty_list_is_empty_not_zero():
    assert main._quantiles([]) == {}
    assert main._quantiles([None, None]) == {}


# ── the margins reach the diagnostics ────────────────────────────────────────

def test_a_too_wide_quote_records_how_wide():
    wide = config.MAX_QUOTE_SPREAD_PCT + 0.30
    chain = [_leg(1.00, 1.00 * (1 + wide), mid=1.00)]
    m = _diag(chain).get("reason_margins", {})
    assert "short_quote_spread_too_wide" in m
    assert m["short_quote_spread_too_wide"]["median"] == pytest.approx(wide, abs=1e-3)


def test_the_margin_says_a_near_miss_is_near():
    """The whole point: 0.36 against a 0.35 cap is a different fact from 1.75 against it."""
    near = [_leg(1.00, 1.00 * (1 + config.MAX_QUOTE_SPREAD_PCT + 0.01), mid=1.00)]
    far = [_leg(1.00, 1.00 * (1 + config.MAX_QUOTE_SPREAD_PCT + 1.40), mid=1.00)]
    mn = _diag(near)["reason_margins"]["short_quote_spread_too_wide"]["median"]
    mf = _diag(far)["reason_margins"]["short_quote_spread_too_wide"]["median"]
    assert mn < config.MAX_QUOTE_SPREAD_PCT + 0.05 < mf


def test_a_thin_strike_records_its_open_interest():
    chain = [_leg(1.00, 1.05, vol=1, oi=13)]
    m = _diag(chain).get("reason_margins", {})
    assert m["short_liquidity_below_floor"]["median"] == 13


def test_delta_rejections_record_the_delta():
    hi = [_leg(1.00, 1.05, delta=-0.80)]
    m = _diag(hi).get("reason_margins", {})
    assert m["short_delta_too_high"]["median"] == pytest.approx(0.80)


def test_counters_and_margins_agree_on_n():
    wide = config.MAX_QUOTE_SPREAD_PCT + 0.30
    chain = [_leg(1.00, 1.00 * (1 + wide), mid=1.00, strike=95.0),
             _leg(1.00, 1.00 * (1 + wide), mid=1.00, strike=94.0)]
    d = _diag(chain)
    counts = dict(d["top_reasons"])
    assert d["reason_margins"]["short_quote_spread_too_wide"]["n"] == counts["short_quote_spread_too_wide"]


# ── measurement only ─────────────────────────────────────────────────────────

def test_no_gate_reads_the_margins():
    """The rejection is already decided by the time _bump is called."""
    import inspect
    src = inspect.getsource(main.select_bull_put_pair)
    i = src.find("diag_margins")
    assert i > 0
    for bad in ("if diag_margins", "diag_margins[", "diag_margins.get("):
        assert bad not in src, f"a gate is reading the margins: {bad}"


def test_recording_a_margin_changes_no_rejection():
    """Same chain, same verdict, with and without a margin available."""
    priced = [_leg(1.00, 1.80, mid=1.40)]
    unpriced = [_leg(1.00, 1.80, mid=0.0)]      # mid 0 -> no ratio computable
    a, b = _diag(priced), _diag(unpriced)
    assert a["valid_pairs_count"] == b["valid_pairs_count"] == 0
    # the second still counts its rejection, it simply has no number to attach
    assert dict(b["top_reasons"])
