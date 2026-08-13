"""The buyer's edge on the Momentum tab.

This tab had no edge metric at all. It ranked nothing, scored nothing, and labelled every card
HIGH conviction — 19 of 19 in the 2026-08-11 scan — so the badge carried no information and
the ordering was whatever order config listed the watchlist in.

The premium-selling side of VEGA rests on one number: implied above realised means the options
are expensive and worth writing. Buying a call is the same test with the sign flipped, and
that number was computable here all along.
"""
import math

import pytest

from lottery_scanner import vol_edge


def _series(daily_vol, n=200, start=100.0):
    """Deterministic alternating walk with a known daily sigma."""
    out, px = [], start
    for i in range(n):
        px *= math.exp(daily_vol if i % 2 == 0 else -daily_vol)
        out.append(px)
    return out


def test_realised_above_implied_is_a_positive_edge_for_the_buyer():
    """A stock delivering 40% vol against options priced at 20% is a cheap move to buy."""
    s = _series(0.40 / math.sqrt(252))
    assert vol_edge(s, 0.20) > 0


def test_realised_below_implied_is_negative():
    """However good the chart looks, the buyer is paying up for a move the stock is not making."""
    s = _series(0.10 / math.sqrt(252))
    assert vol_edge(s, 0.50) < 0


def test_the_edge_is_returned_in_vol_points():
    s = _series(0.40 / math.sqrt(252))
    assert vol_edge(s, 0.20) == pytest.approx(20.0, abs=2.0)


def test_a_missing_iv_is_absence_not_zero():
    """A zero would rank as neutral and sort above genuinely negative names."""
    s = _series(0.30 / math.sqrt(252))
    assert vol_edge(s, None) is None
    assert vol_edge(s, 0) is None


def test_too_little_history_is_absence():
    assert vol_edge([100.0] * 10, 0.30) is None


def test_iv_is_accepted_as_a_fraction_or_as_points():
    """The chain carries IV as a fraction; some callers carry vol points. Mixing them silently
    produces a hundred-fold error that still looks like a plausible number."""
    s = _series(0.40 / math.sqrt(252))
    assert vol_edge(s, 0.20) == pytest.approx(vol_edge(s, 20.0), abs=0.1)


def test_the_scan_ranks_by_edge_and_keeps_unmeasurable_cards_last():
    """Unranked before this: the top card was whichever ticker config listed first. A card with
    no measurable edge is still worth showing, it just cannot claim the options are cheap."""
    cards = [{"ticker": "A", "vol_edge_pp": -3.0}, {"ticker": "B", "vol_edge_pp": None},
             {"ticker": "C", "vol_edge_pp": 12.0}]
    cards.sort(key=lambda c: (c.get("vol_edge_pp") is not None, c.get("vol_edge_pp") or -999),
               reverse=True)
    assert [c["ticker"] for c in cards] == ["C", "A", "B"]


# ── Review regressions ────────────────────────────────────────────────────────────────────────

def test_a_genuine_zero_edge_is_not_treated_as_missing():
    """`or -999` mapped a real 0.0pp edge to the sentinel, sorting it below -8.0pp — an
    inversion hitting exactly the cards on the decision boundary."""
    import lottery_scanner as L
    cards = [{"ticker": "Z", "vol_edge_pp": 0.0}, {"ticker": "N", "vol_edge_pp": None},
             {"ticker": "B", "vol_edge_pp": -8.0}, {"ticker": "G", "vol_edge_pp": 12.0}]
    cards.sort(key=L._vol_edge_sort_key, reverse=True)
    assert [c["ticker"] for c in cards] == ["G", "Z", "B", "N"]


def test_the_realised_window_matches_the_horizon_the_option_prices():
    """A hardcoded 60-day lookback against ~35-day implied ranks a name that spiked two months
    ago and has since gone quiet straight to the top — the mismatch VRP_HV_WINDOW exists to
    prevent."""
    import inspect
    import config
    import lottery_scanner as L
    assert "VRP_HV_WINDOW" in inspect.getsource(L.vol_edge)
    s = _series(0.40 / math.sqrt(252), n=int(config.VRP_HV_WINDOW) + 5)
    assert L.vol_edge(s, 0.20) is not None, "the default window must fit its own minimum history"
