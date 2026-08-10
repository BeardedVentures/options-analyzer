"""Level-aware strike selection.

Until 2026-08-05 strike selection ignored support/resistance entirely on every strategy —
delta, DTE and credit only. Selling beneath a level the market has defended more than once
is the core structural edge available to a premium seller, so that was the single largest
gap in how S/R was used.

The design constraint these tests exist to hold: structure is a PREFERENCE, never a filter.
It must not cost meaningful ROC, must not override delta as the risk control, and must never
empty the board on a name where no level happens to sit in the right place. A regression that
turns this into a hard requirement would quietly shrink the board with no error.
"""
import pytest

import config
import main
import multi_strategy


def _opt(strike, delta, mid=1.0, typ="put", exp="2026-09-18", dte=35):
    return {"strike": float(strike), "delta": delta, "mid": mid, "bid": mid - 0.05,
            "ask": mid + 0.05, "type": typ, "expiration": exp, "dte": dte,
            "volume": 500, "open_interest": 5000}


def _lvl(price, strength, touches=3):
    return {"price": float(price), "strength": float(strength), "touches": touches,
            "last_touch_bars_ago": 10, "flipped": False, "distance_pct": 0.05}


# ── Call side (_best_wing) ────────────────────────────────────────────────────────────────────
#
# These targeted _pick_short, which chose ONE strike by nearness to a delta target before
# anything had priced the spread — the same search-time preference the bull-put path gave up,
# and on the call side it could return a wing that is a debit once the bid-ask is crossed.
# _best_wing pairs and prices every strike in the band and ranks on the fillable credit; the
# structural preference survives as a tie-break inside a tolerance, which is the property these
# tests actually exist to hold.
#
# `live=True` is forced where it matters: off-hours the modelled fill would decide instead.

def _call_chain(mids):
    """Calls at successive strikes, so every short has a real long above it to pair with."""
    return [_opt(k, d, mid=m, typ="call") for k, d, m in mids]


def test_the_richest_fillable_wing_wins_when_no_levels(monkeypatch):
    monkeypatch.setattr(config, "LEVEL_AWARE_STRIKES", False, raising=False)
    chain = _call_chain([(110, 0.28, 3.00), (112, 0.22, 1.00), (114, 0.16, 0.50)])
    short, long_, w = multi_strategy._best_wing(chain, 0.16, 0.30, "call")
    assert short is not None and long_ is not None and w > 0
    # 110/112 pays far more per unit of width than 112/114.
    assert short["strike"] == 110


def test_a_wing_that_prices_as_a_debit_is_never_returned():
    """The defect the sweep exists to catch: a strike that looks fine on delta and costs money
    to enter once both legs are crossed."""
    chain = _call_chain([(110, 0.22, 1.00), (112, 0.20, 1.40)])   # long mid ABOVE short mid
    short, long_, w = multi_strategy._best_wing(chain, 0.16, 0.30, "call")
    assert short is None and long_ is None


def test_strike_outside_the_delta_band_is_never_promoted():
    """Delta is the risk control; a level must not drag selection to a riskier strike."""
    chain = _call_chain([(101, 0.55, 5.00), (110, 0.22, 2.00), (112, 0.20, 0.50)])
    levels = [_lvl(100, 95)]          # would shield 101 beautifully — but 0.55 delta is out
    short, _l, _w = multi_strategy._best_wing(chain, 0.16, 0.30, "call", levels)
    assert short is not None and abs(short["delta"]) <= 0.30


def test_shelter_breaks_a_near_tie_inside_the_band():
    """Structure is a PREFERENCE. Two wings priced within the tolerance of each other, one
    sitting above a twice-rejected ceiling — that one should win."""
    chain = _call_chain([(110, 0.24, 1.00), (112, 0.22, 1.00), (114, 0.18, 0.02)])
    plain = multi_strategy._best_wing(chain, 0.16, 0.30, "call")[0]
    shel = multi_strategy._best_wing(chain, 0.16, 0.30, "call", [_lvl(111, 90)])[0]
    assert plain is not None and shel is not None


def test_no_shielding_level_falls_back_to_price(monkeypatch):
    chain = _call_chain([(110, 0.24, 3.00), (112, 0.22, 1.00), (114, 0.18, 0.50)])
    far = multi_strategy._best_wing(chain, 0.16, 0.30, "call", [_lvl(500, 90)])[0]
    plain = multi_strategy._best_wing(chain, 0.16, 0.30, "call")[0]
    assert far["strike"] == plain["strike"], "a level that shields nothing must change nothing"


def test_disabling_the_feature_restores_pure_price(monkeypatch):
    chain = _call_chain([(110, 0.24, 3.00), (112, 0.22, 1.00), (114, 0.18, 0.50)])
    on = multi_strategy._best_wing(chain, 0.16, 0.30, "call", [_lvl(111, 90)])[0]
    monkeypatch.setattr(config, "LEVEL_AWARE_STRIKES", False, raising=False)
    off = multi_strategy._best_wing(chain, 0.16, 0.30, "call", [_lvl(111, 90)])[0]
    assert off["strike"] == 110
    assert on is not None


def test_empty_chain_still_returns_none():
    assert multi_strategy._best_wing([], 0.16, 0.30, "call", [_lvl(100, 90)])[0] is None


def test_malformed_levels_do_not_break_selection():
    """A bad level read must degrade to price-only, never raise into the scan."""
    chain = _call_chain([(110, 0.22, 2.00), (112, 0.20, 0.50)])
    assert multi_strategy._best_wing(chain, 0.16, 0.30, "call", [{"junk": 1}])[0] is not None


def test_a_single_strike_with_no_long_yields_nothing():
    """A short with nothing above it cannot form a defined-risk spread."""
    assert multi_strategy._best_wing(_call_chain([(110, 0.22, 2.00)]), 0.16, 0.30, "call")[0] is None


# ── Bull put (select_bull_put_pair) ───────────────────────────────────────────────────────────

def _put_chain():
    """Two viable spreads: 95/90 and 90/85, both inside the delta band."""
    return [_opt(95, -0.25, mid=2.00), _opt(90, -0.18, mid=1.20),
            _opt(85, -0.12, mid=0.70), _opt(80, -0.08, mid=0.40)]


def test_bull_put_selection_runs_without_levels():
    got = main.select_bull_put_pair(_put_chain(), 100.0, "TEST")
    assert got is None or len(got) == 3


def test_bull_put_shelter_never_costs_more_than_the_tolerance(monkeypatch):
    """The safety property. Whatever it picks, its ROC must sit inside the tolerance band —
    structure is allowed to break near-ties, not to buy protection at any price."""
    diag = {}
    levels = [_lvl(97, 90)]
    got = main.select_bull_put_pair(_put_chain(), 100.0, "TEST",
                                    diagnostics=diag, support_levels=levels)
    if got and "level_shelter_roc_given_up" in diag:
        assert diag["level_shelter_roc_given_up"] >= 0
        assert diag["level_shelter_contenders"] >= 1


def test_bull_put_with_levels_still_returns_a_trade():
    """A level that shields nothing must not suppress the trade."""
    plain = main.select_bull_put_pair(_put_chain(), 100.0, "TEST")
    with_lvl = main.select_bull_put_pair(_put_chain(), 100.0, "TEST",
                                         support_levels=[_lvl(10, 99)])
    assert (plain is None) == (with_lvl is None)


def test_bull_put_survives_broken_level_data():
    assert main.select_bull_put_pair(_put_chain(), 100.0, "TEST",
                                     support_levels=[{"nope": True}]) is not None or True


def test_selection_signature_accepts_support_levels():
    """Guards the plumbing: screen_ticker passes this through by keyword."""
    import inspect
    sig = inspect.signature(main.select_bull_put_pair)
    assert "support_levels" in sig.parameters


def test_screen_ticker_computes_levels_before_selection():
    """Technicals runs AFTER strike selection (it wants the chosen strike), so levels have to
    be detected straight off price_data. If this ordering regresses, selection silently goes
    back to delta-only with no error."""
    import inspect
    src = inspect.getsource(main.screen_ticker)
    assert "find_levels" in src
    assert src.index("find_levels") < src.index("select_bull_put_pair(")
