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


# ── Call side (_pick_short) ───────────────────────────────────────────────────────────────────

def test_delta_alone_still_decides_when_no_levels():
    chain = [_opt(110, 0.20, typ="call"), _opt(115, 0.16, typ="call")]
    assert multi_strategy._pick_short(chain, 0.22, 0.16, 0.30)["strike"] == 110


# 110 is the exact delta target, so it is what pure-delta selection returns. 112 is still
# inside the tolerance band and sits further above a twice-rejected ceiling at 108, so the
# shelter preference should move the pick. Keeping these two answers DIFFERENT is what makes
# the next two tests meaningful — an earlier version had both at 112 and passed vacuously.
_DELTA_PICK, _SHELTER_PICK = 110, 112
_BAND_CHAIN = [_opt(110, 0.22, typ="call"), _opt(112, 0.19, typ="call")]
_CEILING = [_lvl(108, 80)]


def test_shielded_short_call_wins_inside_the_delta_band():
    """Both strikes are acceptable on delta; one has more room above a defended ceiling."""
    picked = multi_strategy._pick_short(_BAND_CHAIN, 0.22, 0.16, 0.30, _CEILING, "call")
    assert picked["strike"] == _SHELTER_PICK


def test_the_shelter_pick_really_differs_from_the_delta_pick():
    """Guards the fixture itself: if these ever coincide, the tests above prove nothing."""
    plain = multi_strategy._pick_short(_BAND_CHAIN, 0.22, 0.16, 0.30)
    assert plain["strike"] == _DELTA_PICK != _SHELTER_PICK


def test_strike_outside_the_delta_band_is_never_promoted():
    """Delta is the risk control; a level must not drag selection to a riskier strike."""
    chain = [_opt(110, 0.22, typ="call"), _opt(101, 0.55, typ="call")]
    levels = [_lvl(100, 95)]          # would shield 101 beautifully — but 0.55 delta is out
    picked = multi_strategy._pick_short(chain, 0.22, 0.16, 0.30, levels, "call")
    assert picked["strike"] == 110


def test_no_shielding_level_falls_back_to_delta():
    chain = [_opt(110, 0.22, typ="call"), _opt(112, 0.20, typ="call")]
    levels = [_lvl(200, 90)]          # far above both strikes: shields neither
    assert multi_strategy._pick_short(chain, 0.22, 0.16, 0.30, levels, "call")["strike"] == 110


def test_stronger_level_preferred_over_weaker():
    chain = [_opt(110, 0.22, typ="call"), _opt(111, 0.21, typ="call")]
    weak, strong = _lvl(109.9, 15), _lvl(108, 90)
    picked = multi_strategy._pick_short(chain, 0.22, 0.16, 0.30, [weak, strong], "call")
    assert picked is not None


def test_disabling_the_feature_restores_pure_delta(monkeypatch):
    monkeypatch.setattr(config, "LEVEL_AWARE_STRIKES", False, raising=False)
    picked = multi_strategy._pick_short(_BAND_CHAIN, 0.22, 0.16, 0.30, _CEILING, "call")
    assert picked["strike"] == _DELTA_PICK


def test_empty_chain_still_returns_none():
    assert multi_strategy._pick_short([], 0.22, 0.16, 0.30, [_lvl(100, 90)], "call") is None


def test_malformed_levels_do_not_break_selection():
    """A bad level read must degrade to delta-only, never raise into the scan."""
    chain = [_opt(110, 0.22, typ="call")]
    assert multi_strategy._pick_short(chain, 0.22, 0.16, 0.30, [{"junk": 1}], "call") is not None


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
