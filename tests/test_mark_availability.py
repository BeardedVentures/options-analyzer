"""An open position that cannot be re-priced must SAY so, and must still be manageable.

The bug this pins, observed live. PSX stopped being marked on 2026-08-13, AMGN and XLE on
2026-08-14, and each one logged the same line every cycle for a week:

    Reprice: strike not found in chain for XLE-56.0/55.0-... (chain depth: 0 strikes)

"chain depth: 0" was not yfinance going dark. `_reprice_and_close_open()` marks positions with
`fetcher.get_options_chain()`, which enforces the SELECTION contract: if less than
CHAIN_QUALITY_MIN_RATIO (0.50) of the chain is quotable it returns [] so no signal is built on
a chain that is mostly absent. Correct for choosing a new trade. Applied to a position already
held it is a category error — marking a vertical needs two specific strikes to quote, not a
healthy chain — and PSX (5% quotable) and AMGN (7%) could never clear it again.

Two consequences, and the second is the dangerous one:

  1. The mark froze. The ledger kept `current_mark` and a `marked_at` from days earlier, so
     "this position has not moved" and "we have no idea where this position is" were the same
     row, and nothing anywhere distinguished them.
  2. The close rules never ran. Target, stop AND the mechanical DTE-window close all live
     inside the `if s and l:` branch of that lookup. A position that goes unpriceable rides to
     expiration with nothing managing it, silently.

So the fix has two halves and this file tests both: the mark path gets an ungated view of the
chain, and a position that still cannot be priced enters an explicit DATA_UNAVAILABLE state
whose consequences are logged rather than skipped.
"""
import pytest

import auto_paper_cycle as apc
import config
from analysis import outcome_logger as ol


WIDTH = 5.0


def leg(strike, exp="2026-09-18", bid=1.00, ask=1.10, volume=500, oi=1000):
    return {"strike": strike, "expiration": exp, "bid": bid, "ask": ask,
            "mid": round((bid + ask) / 2, 2), "volume": volume, "open_interest": oi}


def dead_leg(strike, exp="2026-09-18"):
    """A contract with no market at all — the state that must pause a position."""
    return {"strike": strike, "expiration": exp, "bid": 0.0, "ask": 0.0, "mid": 0.0,
            "volume": 0, "open_interest": 0}


def junk(n, exp="2026-09-18"):
    """Padding that drags a chain's quotable ratio below the selection floor.

    Deliberately strikes the position does NOT hold: the whole point is that a chain can be
    mostly dead while the two legs we care about quote perfectly well.
    """
    return [dead_leg(900.0 + i, exp) for i in range(n)]


@pytest.fixture
def open_psx(temp_ledger):
    """One open natural-basis bull put, mirroring the live PSX row."""
    tid = ol.open_paper_trade(
        ticker="PSX", short_strike=185.0, long_strike=180.0, expiration="2026-09-18",
        entry_credit_per_share=1.00, dte=43, fill_model="natural", source="test",
    )
    return tid


@pytest.fixture
def wire(monkeypatch):
    """Feed _reprice_and_close_open a chain of our choosing and capture what it logs.

    Patches get_options_chain on the fetcher module itself because the function under test
    imports it locally (`from data import fetcher`) at call time.
    """
    from data import fetcher

    seen = {"kwargs": None}
    lines = []

    def install(chain):
        def fake(ticker, min_dte=None, max_dte=None, **kw):
            seen["kwargs"] = kw
            # Reproduce the real contract: the selection gate empties a thin chain.
            if kw.get("apply_quality_gate", True):
                raw = len(chain)
                usable = sum(1 for o in chain if float(o.get("bid") or 0) > 0
                             or float(o.get("ask") or 0) > 0)
                floor = float(getattr(config, "CHAIN_QUALITY_MIN_RATIO", 0.30))
                if raw and (usable / raw) < floor:
                    return []
            return list(chain)
        monkeypatch.setattr(fetcher, "get_options_chain", fake)

    monkeypatch.setattr(apc, "_log", lambda m: lines.append(str(m)))
    monkeypatch.setattr(apc, "_level_breach_alerts", lambda *a, **k: 0)
    monkeypatch.setattr(apc, "_ravens_or_legacy_close", lambda *a, **k: False)
    return install, lines, seen


# ── half one: a thin chain must not stop a position being priced ──────────────────────────────

def test_position_is_marked_from_a_chain_too_thin_to_trade(open_psx, wire, read_ledger):
    """THE REGRESSION. Legs quote fine; the rest of the chain is dead. Must mark.

    Before the fix this chain is 2/42 quotable, get_options_chain returns [], and the position
    logs "chain depth: 0 strikes" and is skipped — which is exactly what PSX did every cycle
    from 2026-08-13 onward.
    """
    install, lines, _ = wire
    install([leg(185.0, bid=0.80, ask=0.90), leg(180.0, bid=0.30, ask=0.40)] + junk(40))

    marked, _ = apc._reprice_and_close_open()

    assert marked == 1, (
        "a position whose own two legs are quoting must be marked no matter how much of the "
        "rest of the chain is dead:\n" + "\n".join(lines)
    )
    row = read_ledger()[0]
    assert row["current_mark"] == pytest.approx(0.60)      # short ask 0.90 − long bid 0.30
    assert row["mark_status"] == ol.MARK_LIVE
    assert not ol.mark_is_stale(row)


def test_marking_asks_for_the_ungated_chain(open_psx, wire):
    """The selection floor and the marking read are different questions and must stay so."""
    install, _, seen = wire
    install([leg(185.0), leg(180.0)])
    apc._reprice_and_close_open()
    assert seen["kwargs"].get("apply_quality_gate") is False, (
        "the mark path must opt out of the entry-side quality gate explicitly"
    )


# ── half two: when it genuinely cannot be priced, say so ──────────────────────────────────────

def test_unpriceable_position_enters_an_explicit_paused_state(open_psx, wire, read_ledger):
    """Legs present but with no market: the position pauses, and the ledger records why."""
    install, lines, _ = wire
    install([dead_leg(185.0), dead_leg(180.0)] + junk(10))

    marked, closed = apc._reprice_and_close_open()

    assert (marked, closed) == (0, 0)
    row = read_ledger()[0]
    assert row["mark_status"] == ol.MARK_UNAVAILABLE
    assert ol.mark_is_stale(row)
    assert row["mark_unavailable_since"]
    assert "not quotable" in row["mark_unavailable_reason"]


def test_missing_strike_also_pauses_rather_than_silently_skipping(open_psx, wire, read_ledger):
    """The literal live symptom: the strike isn't in the chain at all."""
    install, lines, _ = wire
    install([leg(999.0), leg(998.0)])

    apc._reprice_and_close_open()

    row = read_ledger()[0]
    assert row["mark_status"] == ol.MARK_UNAVAILABLE, (
        "a strike that is not in the chain left the position indistinguishable from one that "
        "simply had not moved"
    )
    assert "strike not in chain" in row["mark_unavailable_reason"]


def test_the_skipped_close_evaluation_is_visible(open_psx, wire):
    """A skip nothing announces is the failure mode, not the fix.

    The close rules must NOT run against a stale mark — but until now they also did not
    announce that they were not running.
    """
    install, lines, _ = wire
    install([dead_leg(185.0), dead_leg(180.0)])
    apc._reprice_and_close_open()
    blob = "\n".join(lines)
    assert "MARK-UNAVAILABLE" in blob
    assert "Stop/target NOT evaluated" in blob


def test_a_stale_mark_never_reaches_the_close_rules(open_psx, wire, monkeypatch):
    """The stop must not fire on a price from last week."""
    install, _, _ = wire
    install([dead_leg(185.0), dead_leg(180.0)])
    called = []
    monkeypatch.setattr(apc, "_ravens_or_legacy_close",
                        lambda *a, **k: called.append(a) or False)
    apc._reprice_and_close_open()
    assert called == [], "close logic was handed an unrefreshed mark"


def test_going_dark_inside_the_dte_window_escalates(temp_ledger, wire, read_ledger):
    """The dangerous case, stated out loud.

    Target and stop can wait for a quote. The DTE close cannot: it fires on a calendar fact and
    it lives behind the same lookup, so an unpriceable position inside the window has nothing
    managing it at all. That has to be louder than a skipped mark, because no later cycle
    rescues it — expiration arrives on schedule.
    """
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=3)).isoformat()
    ol.open_paper_trade(ticker="PSX", short_strike=185.0, long_strike=180.0, expiration=soon,
                        entry_credit_per_share=1.00, dte=3, fill_model="natural", source="test")
    install, lines, _ = wire
    install([dead_leg(185.0, soon), dead_leg(180.0, soon)])

    apc._reprice_and_close_open()

    blob = "\n".join(lines)
    assert "UNMANAGED-AT-EXPIRY" in blob, (
        "a position that cannot be priced inside the DTE close window rode to expiry silently"
    )
    assert "needs a human" in blob


# ── the mark itself has to be believable ──────────────────────────────────────────────────────

def test_an_implausible_mark_is_refused_not_recorded(open_psx, wire, read_ledger):
    """Reading an ungated chain means occasionally reading a broken print.

    A vertical is worth between zero and its width. Acting on a number outside that is the same
    mistake as ignoring the position, pointed the other way — so it pauses instead.
    """
    install, _, _ = wire
    install([leg(185.0, bid=40.0, ask=50.0), leg(180.0, bid=0.10, ask=0.20)])

    marked, _ = apc._reprice_and_close_open()

    assert marked == 0
    row = read_ledger()[0]
    assert row["mark_status"] == ol.MARK_UNAVAILABLE
    assert "implausible mark" in row["mark_unavailable_reason"]


def test_a_good_mark_clears_a_previous_outage(open_psx, wire, read_ledger):
    """Recovery must be automatic, or the flag becomes noise everyone learns to ignore."""
    install, _, _ = wire

    install([dead_leg(185.0), dead_leg(180.0)])
    apc._reprice_and_close_open()
    assert read_ledger()[0]["mark_status"] == ol.MARK_UNAVAILABLE

    install([leg(185.0, bid=0.80, ask=0.90), leg(180.0, bid=0.30, ask=0.40)])
    apc._reprice_and_close_open()

    row = read_ledger()[0]
    assert row["mark_status"] == ol.MARK_LIVE
    assert row["mark_unavailable_since"] is None
    assert row["mark_skips_consecutive"] == 0


def test_consecutive_skips_count_up_but_the_clock_starts_once(open_psx, wire, read_ledger):
    """How long a position has been dark is a different fact from how many times we tried."""
    install, _, _ = wire
    install([dead_leg(185.0), dead_leg(180.0)])

    apc._reprice_and_close_open()
    first_since = read_ledger()[0]["mark_unavailable_since"]
    apc._reprice_and_close_open()
    apc._reprice_and_close_open()

    row = read_ledger()[0]
    assert row["mark_skips_consecutive"] == 3
    assert row["mark_unavailable_since"] == first_since, (
        "a position stuck for a week must not report that it went stale today"
    )


# ── the fix must not have touched selection ───────────────────────────────────────────────────

def test_the_entry_path_still_gets_the_gate(monkeypatch):
    """The cohort contract freezes what a candidate must clear. This changed none of it.

    get_options_chain's default is unchanged, so every selection caller sees exactly the chain
    it saw before; only the explicit apply_quality_gate=False caller sees more.
    """
    import inspect
    from data import fetcher
    sig = inspect.signature(fetcher.get_options_chain)
    assert sig.parameters["apply_quality_gate"].default is True
    assert sig.parameters["apply_quality_gate"].kind is inspect.Parameter.KEYWORD_ONLY


def test_quality_is_still_measured_on_the_ungated_read(monkeypatch):
    """The ungated view must not launder a bad chain into a clean quality reading.

    The ratio describes the CHAIN, not the caller's appetite for it, so an ungated read has to
    log the same number a gated one would — otherwise turning the gate off would quietly make
    the data-quality record report health it never had.
    """
    from data import fetcher
    recorded = []
    monkeypatch.setattr(fetcher, "_record_chain_quality",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(fetcher, "_parse_yfinance_options",
                        lambda *a, **k: [leg(100.0)] * 2 + junk(8))
    monkeypatch.setattr(fetcher, "get_price_data", lambda *a, **k: _fake_px())
    monkeypatch.setattr(config, "POLYGON_API_KEY", "", raising=False)
    fetcher._cache.clear()

    rows = fetcher.get_options_chain("ZZZ", 0, 200, apply_quality_gate=False)

    assert len(rows) == 10, "ungated callers get the raw chain"
    _, _, raw, usable, ratio = recorded[-1]
    assert (raw, usable) == (10, 2)
    assert ratio == 0.2, "the quality reading must describe the chain, not the caller"


def test_the_quality_reading_is_logged_once_per_chain_not_once_per_view(monkeypatch):
    """Splitting the cache by view must not double-count a ticker in the quality log.

    Before the split, a second call for the same chain hit the cache and recorded nothing. Two
    cache keys reintroduce the chance of two readings for one chain, and every aggregate built
    on data_quality_log.json would inherit the duplicate.
    """
    from data import fetcher
    recorded = []
    monkeypatch.setattr(fetcher, "_record_chain_quality", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(fetcher, "_parse_yfinance_options",
                        lambda *a, **k: [leg(100.0)] * 6 + junk(4))
    monkeypatch.setattr(fetcher, "get_price_data", lambda *a, **k: _fake_px())
    monkeypatch.setattr(config, "POLYGON_API_KEY", "", raising=False)
    fetcher.clear_cache()

    fetcher.get_options_chain("ZZZ", 0, 200)
    fetcher.get_options_chain("ZZZ", 0, 200, apply_quality_gate=False)

    assert len(recorded) == 1, "one chain, one reading"


def test_a_one_sided_quote_still_degrades_to_mid_as_it_always_did(open_psx, wire, read_ledger):
    """The regression this fix nearly introduced, pinned so it cannot come back.

    NEE's short leg quoted 0.36 bid / 0.00 ask on the live 08:46 cycle. The file already had a
    considered answer for a missing side — mark at the mid and log that the mark is optimistic —
    and the first version of the leg check demanded the short ask outright, pre-empting it.
    Pausing is for a position with NO market, not for one with half of one.

    Note the long leg here is deliberately tight. NEE's real long leg quoted 0.13/0.37, a 96%-of-
    mid spread that this check rejects on width — and the entry filter rejected it on the same
    80% rule, so NEE pausing was never the regression. Isolating the missing-ask case is the
    only way to test the missing-ask rule.
    """
    install, lines, _ = wire
    install([leg(185.0, bid=0.36, ask=0.0), leg(180.0, bid=0.13, ask=0.15)])

    marked, _ = apc._reprice_and_close_open()

    assert marked == 1, "a one-sided quote must degrade, not pause: " + " | ".join(lines)
    row = read_ledger()[0]
    assert row["mark_status"] == ol.MARK_LIVE
    assert any("marking at mid" in l and "optimistic" in l for l in lines), (
        "the degradation has to keep announcing that it happened"
    )


def test_the_marking_bar_is_liquidity_blind_by_design(open_psx, wire, read_ledger):
    """A leg with a real two-sided quote but no volume and no open interest must still mark.

    This is the whole distinction the fix rests on. fetcher._option_record_is_usable rejects it,
    correctly, when asking "is this liquid enough to sell into?" — the wrong question for a
    position already held.
    """
    install, _, _ = wire
    install([leg(185.0, bid=0.80, ask=0.90, volume=0, oi=0),
             leg(180.0, bid=0.30, ask=0.40, volume=0, oi=0)])

    marked, _ = apc._reprice_and_close_open()

    assert marked == 1
    assert read_ledger()[0]["current_mark"] == pytest.approx(0.60)


def test_an_absurdly_wide_quote_is_still_refused(open_psx, wire, read_ledger):
    """Relaxing the bar must not relax it into meaninglessness.

    XLE quoted 0.01 / 0.18 on the same cycle — a 179%-of-mid spread. There is no price in that,
    only a range, and marking a position off it would invent a number.
    """
    install, _, _ = wire
    install([leg(185.0, bid=0.01, ask=0.18), leg(180.0, bid=0.05, ask=0.12)])

    marked, _ = apc._reprice_and_close_open()

    assert marked == 0
    assert read_ledger()[0]["mark_status"] == ol.MARK_UNAVAILABLE


def test_the_marking_bar_is_a_strict_superset_of_the_entry_filter():
    """The invariant the whole fix rests on, proved by enumeration rather than asserted.

    This fix is only safe if it can never make marking RARER than it was. Before it, a position
    was marked when its legs survived fetcher._quality_filter_options; after it, when the legs
    clear _leg_quote_is_usable. That is an improvement only if the second set contains the first
    — and "I removed a condition, so it must be more permissive" is exactly the reasoning that
    has been wrong in this codebase before.

    Enumerate the quote space and check the implication directly: every leg the entry filter
    accepts must also be markable. A single counterexample means the fix trades a silent-skip
    bug for a silent-mark-less bug.
    """
    import itertools
    from data.fetcher import _option_record_is_usable

    prices = [0.0, 0.01, 0.05, 0.13, 0.36, 0.80, 1.00, 2.09, 5.0]
    activity = [(0, 0), (0, 100), (500, 1000)]

    violations, gained = [], 0
    for bid, ask in itertools.product(prices, prices):
        mid = round((bid + ask) / 2, 2) if (bid + ask) > 0 else 0
        for vol, oi in activity:
            l = {"bid": bid, "ask": ask, "mid": mid, "volume": vol, "open_interest": oi}
            old_ok, new_ok = _option_record_is_usable(l), apc._leg_quote_is_usable(l)
            if old_ok and not new_ok:
                violations.append(l)
            elif new_ok and not old_ok:
                gained += 1

    assert not violations, (
        "the marking bar rejects legs the entry filter accepted — this fix would make marking "
        f"rarer, not commoner: {violations[:3]}"
    )
    assert gained > 0, "if it accepts nothing new, it cannot have fixed anything"


def _fake_px():
    import pandas as pd
    return pd.DataFrame({"Close": [100.0, 101.0]})


# ── Marking asks a different question from selection ─────────────────────────────────────

def test_marking_bypasses_the_selection_strike_window():
    """A winning put spread drifts OUT of the 75-102%-of-spot fetch band.

    The band is a selection optimisation -- it stops a scan quoting strikes it would never
    sell. Applied to marking it becomes a hazard that scales with profit: the more the
    underlying rallies away from a short put, the closer that strike sits to the bottom of the
    band, until the fetch cannot see the position at all. An explicit strike list must bypass
    the band entirely.
    """
    from data import robinhood_mcp as rh
    import inspect
    src = inspect.getsource(rh.afetch_chain)
    assert "want_strikes" in src and "lo = hi = None" in src, (
        "explicit strikes must switch the percentage band off")


def test_the_mark_path_asks_for_only_the_contracts_it_needs(monkeypatch):
    """It knows every strike and expiry already; a 200-day chain to use two rows of it cost
    ~75 s across the open book."""
    import auto_paper_cycle as apc
    import inspect
    src = inspect.getsource(apc._reprice_and_close_open) if hasattr(apc, "_reprice_and_close_open") \
        else inspect.getsource(apc)
    assert "expirations=_exp" in src and "strikes=_strikes" in src, (
        "the mark path still pulls a broad chain")


def test_a_targeted_fetch_is_cached_separately_from_a_broad_one(monkeypatch):
    """A targeted fetch returns a deliberately PARTIAL chain. Serving it to a caller that
    asked for the whole window would silently truncate a scan."""
    from data import fetcher
    import inspect
    src = inspect.getsource(fetcher.get_options_chain)
    assert "_tgt" in src, "targeted and broad fetches share a cache key"
