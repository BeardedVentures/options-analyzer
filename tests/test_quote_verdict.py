"""The quote gate names WHICH quote failure, because the two need opposite responses.

On the 2026-09-03 close scan, `quote_not_tradeable` accounted for 501 of ~1,100 enumeration
rejections across the 19 tickers that produced no spread at all -- the largest single block in
the whole funnel -- and one counter covered both "this strike has no two-sided quote" and "the
quote is too wide to cross". The first is a data-path question; the second is the strategy
correctly refusing an illiquid strike. The scan-coverage metric reported 54/54 tickers healthy
on that same run, which makes telling them apart the entire question.

These tests pin the labels and, more importantly, pin that splitting them changed no decision.
"""
import pytest

import config
import main


def _opt(bid, ask, mid=None, **kw):
    o = {"type": "put", "strike": 100.0, "delta": -0.2,
         "bid": bid, "ask": ask, "mid": mid if mid is not None else (bid + ask) / 2}
    o.update(kw)
    return o


def _enum_reasons(opt):
    """The counters a real enumeration produces for this one contract."""
    diag = {}
    main.select_bull_put_pair([opt], 110.0, "TEST", diagnostics=diag)
    return dict(diag.get("top_reasons") or [])


# ── the labels ───────────────────────────────────────────────────────────────

def test_a_one_sided_quote_is_named_absent():
    """The realistic shape: a mid exists, one side of the book does not."""
    assert main._quote_verdict(_opt(0.0, 1.20, mid=0.60)) == "quote_absent"
    assert main._quote_verdict(_opt(1.00, 0.0, mid=0.50)) == "quote_absent"


def test_a_wide_but_present_quote_is_named_spread_too_wide():
    wide = config.MAX_QUOTE_SPREAD_PCT * 4 + 1.0
    assert main._quote_verdict(_opt(1.00, 1.00 * (1 + wide), mid=1.00)) == "quote_spread_too_wide"


def test_a_crossed_quote_is_named_crossed_not_wide():
    """ask < bid is a broken feed, not an illiquid strike, and must not read as one."""
    assert main._quote_verdict(_opt(2.00, 1.00, mid=1.50)) == "quote_crossed"


def test_a_tight_two_sided_quote_passes():
    assert main._quote_verdict(_opt(1.00, 1.02, mid=1.01)) is None


def test_a_zero_mid_never_reaches_the_quote_gate_at_all():
    """It is caught one gate EARLIER, as short_missing_price_delta.

    Worth pinning, because it changes how the production counters read: every one of the 501
    `quote_not_tradeable` rejections had a POSITIVE mid and a failing bid/ask, so those strikes
    were priced and simply not two-sidedly quotable. Strikes with no price at all are counted
    somewhere else entirely.
    """
    reasons = _enum_reasons(_opt(0.0, 0.0, mid=0.0))
    assert reasons == {"short_missing_price_delta": 1}


def test_the_split_labels_reach_the_production_counters():
    assert "short_quote_absent" in _enum_reasons(_opt(0.0, 1.20, mid=0.60))


# ── the decision is unchanged ────────────────────────────────────────────────

@pytest.mark.parametrize("bid,ask,mid", [
    (0.0, 0.0, 0.0), (0.0, 1.2, 0.6), (1.0, 0.0, 0.5), (2.0, 1.0, 1.5),
    (1.0, 1.02, 1.01), (1.0, 5.0, 3.0),
])
def test_splitting_the_counter_changed_no_gate_decision(bid, ask, mid):
    """`_quote_is_tradeable` is now defined as `_quote_verdict(...) is None`.

    Re-derived here from the original predicate so a future edit to one cannot silently drift
    from the other -- the split was a diagnostic change and must stay one.
    """
    o = _opt(bid, ask, mid=mid)
    b, a, m = float(o["bid"]), float(o["ask"]), float(o["mid"])
    if b <= 0 or a <= 0 or m <= 0:
        expected = False
    elif a < b:
        expected = False
    else:
        expected = ((a - b) / m) <= config.MAX_QUOTE_SPREAD_PCT
    assert main._quote_is_tradeable(o) is expected


def test_the_old_undifferentiated_counter_is_gone():
    """A stale name would keep aggregating both failures under one label somewhere."""
    src = open(main.__file__, encoding="utf-8-sig").read()
    assert '_bump("short_quote_not_tradeable")' not in src
    assert '_bump("long_quote_not_tradeable")' not in src
