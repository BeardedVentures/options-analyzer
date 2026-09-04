"""The quotes a recorded credit was derived from must survive into the ledger.

They did not, until 2026-09-04. All three builders put leg bid/ask on the trade dict --
main.py:1012, multi_strategy:340 and :417 -- and record_modeled_trades dropped every one, so
101 call-side recommendations carry no quote at all and none of them can be audited for fill
quality even in principle.

The gap matters because the two enumeration paths apply different quote standards: main.py
requires a two-sided market inside MAX_QUOTE_SPREAD_PCT; multi_strategy._tradeable requires only
`mid > 0` and a token of volume, with no spread test. The whole board since 2026-08-11 came from
the path that never asks whether the book can be crossed.
"""
import pytest

from analysis import outcome_logger as ol


def _trade(**kw):
    t = {"ticker": "TEST", "strategy": "bear_call_spread", "short_strike": 105.0,
         "long_strike": 110.0, "expiration": "2026-10-16", "dte": 35,
         "credit_per_share": 1.20, "natural_credit_per_share": 1.10, "spread_width": 5.0,
         "true_pop": 0.75, "edge_score": 61}
    t.update(kw)
    return t


def _row_for(trade):
    ol.record_modeled_trades("2026-09-04T14:35:00", "close", [trade])
    rows = [r for r in ol.load_records() if r.get("status") == "modeled"]
    assert rows, "nothing was recorded"
    return rows[-1]


def test_vertical_leg_quotes_are_persisted():
    r = _row_for(_trade(short_bid=1.30, short_ask=1.45, long_bid=0.18, long_ask=0.25))
    q = r["leg_quotes"]
    assert q["short_bid"] == 1.30 and q["short_ask"] == 1.45
    assert q["long_bid"] == 0.18 and q["long_ask"] == 0.25


def test_condor_wing_quotes_are_persisted():
    """The condor path emits only the short BID and long ASK per wing.

    That is what the natural credit is computed from; the other two sides are genuinely not
    captured upstream. Pinned so their absence reads as a known upstream gap rather than as a
    field this writer dropped.
    """
    r = _row_for(_trade(strategy="Iron Condor", short_strike=None, long_strike=None,
                        call_short_bid=0.90, call_long_ask=0.40,
                        put_short_bid=0.85, put_long_ask=0.35))
    q = r["leg_quotes"]
    assert q["call_short_bid"] == 0.90 and q["call_long_ask"] == 0.40
    assert q["put_short_bid"] == 0.85 and q["put_long_ask"] == 0.35
    assert "call_short_ask" not in q and "put_long_bid" not in q


def test_the_recorded_credit_is_reconstructible_from_the_quotes():
    """The point of the field: a later audit can re-derive the natural credit and judge the book."""
    r = _row_for(_trade(short_bid=1.30, short_ask=1.45, long_bid=0.18, long_ask=0.25,
                        natural_credit_per_share=1.05))
    q = r["leg_quotes"]
    assert q["short_bid"] - q["long_ask"] == pytest.approx(r["natural_credit_per_share"])


def test_quotes_are_stored_RAW_not_as_a_derived_verdict():
    """A stored ratio bakes in today's definition of "too wide".

    MAX_QUOTE_SPREAD_PCT is 0.35 while the chain-health predicate tolerates 0.80 — one number,
    two meanings, which is the defect this project keeps paying for. Raw quotes can be re-judged
    under any threshold later; a stored verdict cannot.
    """
    r = _row_for(_trade(short_bid=1.00, short_ask=2.00, long_bid=0.10, long_ask=0.20))
    q = r["leg_quotes"]
    assert set(q) == {"short_bid", "short_ask", "long_bid", "long_ask"}
    assert not any("spread" in k or "wide" in k or "ok" in k for k in q)
    # and the reader can compute the judgement themselves
    mid = (q["short_bid"] + q["short_ask"]) / 2
    assert (q["short_ask"] - q["short_bid"]) / mid == pytest.approx(0.6667, abs=1e-3)


def test_a_trade_with_no_quotes_records_null_not_an_empty_dict():
    """Absence must be legible. An empty dict reads as "asked and got nothing"."""
    r = _row_for(_trade())
    assert r["leg_quotes"] is None


def test_every_builder_field_name_is_covered():
    """A builder renaming a field would silently drop it again. Pin the names to the sources."""
    import inspect
    import main, multi_strategy
    src = inspect.getsource(ol.record_modeled_trades)
    emitted = set()
    for mod in (main, multi_strategy):
        text = inspect.getsource(mod)
        for name in ("short_bid", "short_ask", "long_bid", "long_ask",
                     "call_short_bid", "call_long_ask", "put_short_bid", "put_long_ask"):
            if f'"{name}"' in text:
                emitted.add(name)
    assert emitted, "no builder emits leg quotes — the sources moved"
    missing = [n for n in emitted if f'"{n}"' not in src]
    assert not missing, f"builders emit {missing} and the ledger writer does not persist them"
