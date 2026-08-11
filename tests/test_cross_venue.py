"""Cross-venue volatility: one shell, per-asset config, nothing shared.

An ETF's options and the underlying asset's own options price one risk in two venues. Where
the underlying publishes a vol index, the gap is information the ETF's thin IV history cannot
supply — IBIT has three days of its own IV and Bitcoin has a continuous 30-day index for free.

The generalisation from "BTC and nothing else" to "any declared asset" is where this gets
dangerous, and every test below guards one of the ways a shared implementation lies quietly:

  * a shared noise floor calls gold's ordinary basis a signal and hides GDX's real moves
  * a shared snapshot renders one asset's spot and vol under another asset's heading
  * a reference DERIVED from the ETF's own chain is a reconstruction check, never an edge
  * a stale reference still subtracts, and produces a confident number from a dead operand

Every value asserted here comes from ticker_profile.DECLARED. The point of the design is that
adding an asset is a config change; the point of these tests is that it stays one.
"""
import pytest

from analysis import cross_venue as cv
from analysis import ticker_profile as tp


CTX = {"BTC_DVOL": 35.9, "ETH_DVOL": 49.4,
       "GVZ": {"value": 27.9, "asof": "2026-08-10", "age_days": 1}}


# ── The declared schema ───────────────────────────────────────────────────────────────────────

def test_every_declared_asset_carries_a_complete_cross_venue_block():
    """A half-filled block is worse than none: the renderer would show a card with a label and
    no floor, and the floor is what decides whether the number means anything."""
    for tk in tp.cross_venue_tickers():
        c = tp.cross_venue(tk)
        assert c["ref_name"] and c["ref_signal"] and c["source"], tk
        assert c["noise_floor_pp"] is not None, f"{tk} has no noise floor"
        assert c["drivers"], f"{tk} has no driver list"


def test_noise_floors_are_not_shared_between_assets():
    """BTC's ordinary venue basis is ~1.5pp; GLD's entire gap lives inside half a point and GDX
    sits 6+ points above GVZ on miner beta alone. One shared threshold reports gold noise as
    signal and hides every real move in GDX behind an unreachable bar."""
    floors = {tk: tp.cross_venue(tk)["noise_floor_pp"] for tk in tp.cross_venue_tickers()}
    assert floors["GLD"] < floors["IBIT"] < floors["ETHA"] < floors["GDX"]
    assert len(set(floors.values())) > 1


def test_driver_lists_are_not_shared_between_assets():
    """BTC's drivers are meaningless for gold. Sharing them is how a card starts explaining the
    wrong asset in fluent, confident prose."""
    btc = set(tp.cross_venue("IBIT")["drivers"])
    gold = set(tp.cross_venue("GLD")["drivers"])
    rates = set(tp.cross_venue("TLT")["drivers"])
    assert not (btc & gold) and not (btc & rates) and not (gold & rates)


def test_an_undeclared_ticker_has_no_block_at_all():
    assert tp.cross_venue("AAPL") is None
    assert tp.cross_venue("SPY") is None


def test_the_master_switch_defaults_off():
    """A name nobody declared a reference for has no reference. Defaulting True would make every
    unlisted ticker try to render against a ctx key that does not exist."""
    assert tp.declared("AAPL")["has_cross_venue_signal"] is False


# ── Blocked assets are specified, not forgotten ───────────────────────────────────────────────

@pytest.mark.parametrize("tk", ["TLT", "SOLZ"])
def test_a_blocked_asset_still_declares_its_reference_and_says_why(tk):
    """The difference between "we never thought about TLT" and "TLT is fully specified and
    blocked on a licensed feed" is the whole reason the config keeps the block when the switch
    is off."""
    c = tp.cross_venue(tk)
    assert c["enabled"] is False
    assert c["ref_name"] and c["ref_signal"]
    assert c["blocked_reason"], f"{tk} is off with no reason recorded"


def test_a_blocked_asset_never_produces_a_gap_even_with_a_feed_present():
    """The switch is the authority. If a MOVE value somehow appeared in ctx, TLT must still not
    render a reading — the feed being reachable was never the question."""
    out = cv.evaluate("TLT", 0.12, {**CTX, "MOVE": 75.46})
    assert out["available"] is False
    assert out["gap_pp"] is None
    assert "MOVE" in out["note"]


# ── The gap itself ────────────────────────────────────────────────────────────────────────────

def test_the_gap_uses_this_assets_own_floor():
    """3.4pp is a signal for IBIT (floor 2.0) and noise for ETHA (floor 4.0). Same number,
    opposite readings, and only the per-asset floor can tell them apart."""
    ibit = cv.evaluate("IBIT", (35.9 - 3.4) / 100, CTX)
    etha = cv.evaluate("ETHA", (49.4 - 3.4) / 100, CTX)
    assert ibit["gap_pp"] == pytest.approx(3.4, abs=0.05)
    assert etha["gap_pp"] == pytest.approx(3.4, abs=0.05)
    assert ibit["reading"] == cv.ETF_CHEAP
    assert etha["reading"] == cv.ALIGNED


def test_iv_fractions_and_index_points_are_reconciled_once():
    """The ETF carries IV as a fraction and the reference in vol points. Mixing them produces a
    hundred-fold error that still looks like a plausible number."""
    out = cv.evaluate("IBIT", 0.3180, CTX)
    assert out["proxy_iv_pp"] == pytest.approx(31.8, abs=0.05)
    assert out["gap_pp"] == pytest.approx(4.1, abs=0.05)


def test_a_missing_reference_is_absence_not_a_neutral_reading():
    out = cv.evaluate("IBIT", 0.32, {})
    assert out["available"] is False and out["reading"] == cv.UNAVAILABLE


def test_a_missing_etf_iv_is_absence_too():
    out = cv.evaluate("IBIT", None, CTX)
    assert out["available"] is False


def test_the_reference_reading_carries_its_own_date_when_it_has_one():
    """So a card can say how old its number is instead of implying it is live."""
    out = cv.evaluate("GLD", 0.219, CTX)
    assert out["ref_asof"] == "2026-08-10"
    assert out["ref_age_days"] == 1


# ── Derived references are not edges ──────────────────────────────────────────────────────────

def test_a_self_derived_reference_is_a_quality_check_never_an_edge():
    """GVZ is computed from GLD's own chain. A gap is our reconstruction disagreeing with
    CBOE's on the same options — calling it an edge is claiming to arbitrage arithmetic."""
    out = cv.evaluate("GLD", 0.219, CTX)
    assert out["is_quality_check"] is True
    assert out["reading"] == cv.QUALITY
    assert "priced twice" in out["note"]


def test_a_reference_derived_from_a_DIFFERENT_asset_is_a_real_spread():
    """GVZ describes bullion; GDX is a levered equity claim on it. Keying on bool(derived_from)
    alone filed an eleven-point miner-beta spread as "the likelier number to be wrong is ours"."""
    out = cv.evaluate("GDX", 0.423, CTX)
    assert out["is_quality_check"] is False
    assert out["reading"] in (cv.ETF_RICH, cv.ETF_CHEAP)
    assert "a different asset" in out["note"]


def test_an_independent_reference_is_never_a_quality_check():
    assert cv.evaluate("IBIT", 0.318, CTX)["is_quality_check"] is False


# ── Reference plumbing ────────────────────────────────────────────────────────────────────────

def test_a_reference_reads_as_a_bare_float_or_a_dated_dict():
    """DVOL arrives bare and live; published indices arrive dated so staleness is checkable.
    Flattening the dict at populate time would throw away the only thing that makes the
    staleness guard possible."""
    assert cv.ref_value(35.9) == 35.9
    assert cv.ref_value({"value": 27.9, "asof": "2026-08-10"}) == 27.9
    assert cv.ref_value(None) is None
    assert cv.ref_value({"value": None}) is None
    assert cv.ref_value("not a number") is None


def test_the_clock_caveat_appears_only_where_it_can_bite():
    """DVOL is 24/7 and the ETF's options are not, so an after-hours gap is partly the clock.
    GVZ keeps equity hours and needs no such warning."""
    assert "continuously" in cv.evaluate("IBIT", 0.318, CTX)["note"]
    assert "continuously" not in cv.evaluate("GLD", 0.219, CTX)["note"]


# ── Staleness guard (data/vol_indices) ────────────────────────────────────────────────────────
# The reason this module exists rather than a two-line yfinance call. On 2026-08-11 Yahoo was
# still serving ^MOVE — HTTP 200, a plausible 75.46, last updated 2026-07-17. Nothing in the
# response says so. A gap computed from it would have compared today's TLT IV against July's
# MOVE and reported the difference as an edge.

from data import vol_indices as vi


@pytest.fixture(autouse=True)
def _clear_index_cache():
    vi._CACHE.clear()
    yield
    vi._CACHE.clear()


def _fake(monkeypatch, asof, value=27.9):
    monkeypatch.setattr(vi, "_fred", lambda s: {"value": value, "asof": asof, "source": "FRED"})
    monkeypatch.setattr(vi, "_yahoo", lambda s: None)


def test_a_current_reading_comes_back_dated(monkeypatch):
    from datetime import date
    _fake(monkeypatch, date.today().isoformat())
    got = vi.get_index("GVZ")
    assert got["value"] == 27.9 and got["age_days"] == 0 and got["stale"] is False


def test_a_stale_reading_is_absent_not_flagged(monkeypatch):
    """Callers are gap calculations. A subtraction will happily produce a confident number from
    a month-old operand, so the only safe shape is absence."""
    from datetime import date, timedelta
    _fake(monkeypatch, (date.today() - timedelta(days=25)).isoformat())
    assert vi.get_index("GVZ") is None


def test_the_staleness_guard_is_not_bypassed_by_the_cache(monkeypatch):
    """Caching the post-check result let a value admitted under one max_age be returned under a
    stricter one without re-examination — and the cached path is the one that runs in
    production."""
    from datetime import date, timedelta
    _fake(monkeypatch, (date.today() - timedelta(days=2)).isoformat())
    assert vi.get_index("GVZ", max_age_days=3) is not None
    assert vi.get_index("GVZ", max_age_days=1) is None


def test_an_undated_reading_is_discarded(monkeypatch):
    """A value whose age cannot be established is a value whose age is unknown, and unknown is
    not young."""
    _fake(monkeypatch, "")
    assert vi.get_index("GVZ") is None


def test_move_is_absent_from_the_registry():
    """FRED publishes no MOVE series and Yahoo's has been stale since 2026-07-17. A
    working-looking entry in a registry whose whole job is trustworthiness is worse than a gap."""
    assert "MOVE" not in vi.SOURCES
    assert vi.get_index("MOVE") is None


def test_populate_writes_each_signal_under_its_own_key(monkeypatch):
    from datetime import date
    _fake(monkeypatch, date.today().isoformat())
    ctx = {}
    vi.populate(ctx, ["GVZ", "MOVE"])
    assert ctx["GVZ"]["value"] == 27.9
    assert ctx["MOVE"] is None


# ── The multi-asset render (P4) ───────────────────────────────────────────────────────────────

import vega_app


def _blocktext(tk, ctx):
    import re, html as H
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", vega_app._asset_block(tk, ctx))))


def test_one_assets_snapshot_never_renders_under_another_assets_heading():
    """The bug this guards was live and invisible: defaulting the currency to BTC for every
    non-ETH asset put Bitcoin's spot, DVOL, realised vol and variance premium on GLD's card
    under gold's heading. Every value rendered was individually true."""
    ctx = {"GVZ": {"value": 27.9, "asof": "2026-08-10", "age_days": 1},
           "_snap_BTC": {"ok": True, "currency": "BTC", "spot": 63488.0, "dvol": 35.9,
                         "rv_30d": 28.4, "vrp_pp": 7.5}}
    txt = _blocktext("GLD", ctx)
    assert "63,488" not in txt and "BTC spot" not in txt
    assert "GVZ" in txt


def test_a_blocked_asset_renders_a_placeholder_carrying_its_reason():
    """A card with blank numbers claims the read was attempted and came back empty. The
    placeholder says it was never possible, and only one of those is true."""
    txt = _blocktext("TLT", {})
    assert "No native volatility reference in service" in txt
    assert "MOVE" in txt and "stale" in txt


def test_the_placeholder_names_the_reference_it_is_waiting_on():
    txt = _blocktext("SOLZ", {})
    assert "SOL DVOL" in txt and "Deribit" in txt


def test_an_undeclared_ticker_renders_nothing():
    assert vega_app._asset_block("AAPL", {}) == ""


def test_trades_come_before_the_research_within_each_block():
    """A page that only reads instruments is a readout. Burying the spreads under the vol study
    is how it became one the first time."""
    txt = _blocktext("IBIT", {"BTC_DVOL": 35.9})
    assert txt.index("Tradeable now") < txt.index("Cross-venue volatility")


def test_the_render_loop_covers_every_declared_asset():
    """Adding an asset must stay a config change. A hardcoded list in the view is how the
    second asset diverges from the first."""
    import inspect
    src = inspect.getsource(vega_app.view_bitcoin)
    assert "cross_venue_tickers" in src
    html = vega_app.view_bitcoin()
    for tk in tp.cross_venue_tickers():
        assert tk in html, f"{tk} is declared but never rendered"
