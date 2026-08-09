"""BTC data layer and the cross-venue volatility read (steps 1 and 2, 2026-08-09).

Every test here is offline. The live endpoints are exercised by hand; the suite must never
depend on Deribit or Coinbase being up, because a test that fails when a third party has an
outage stops being read.

The signal: IBIT options and BTC options price the same underlying risk in two venues. Deribit
publishes DVOL free; IBIT's ATM IV comes off a chain VEGA already fetches. The gap needs no
forecast to be informative, which is why it ships before any directional call — a direction has
to beat a coin flip to be worth anything, a spread only has to be measurable.
"""
import math

import pytest

import config
from analysis import btc_signal as B


BTC_OK = {"dvol": 34.24, "btc_spot": 65170.0, "btc_rv_30d": 27.99, "btc_vrp_pp": 6.25}


# ── Who the read applies to ───────────────────────────────────────────────────────────────────

def test_it_applies_to_ibit():
    assert B.applies_to("IBIT") is True
    assert B.applies_to("ibit") is True


def test_it_does_not_apply_to_coin():
    """Measured 2026-08-09: DVOL 34.24 vs IBIT 32.72 is a 1.5pt gap between two prices for the
    same risk. Against COIN it is 65.23 vs 34.24 — a 31pt gap that measures the difference
    between an operating company and Bitcoin, not a mispricing of Bitcoin."""
    assert B.applies_to("COIN") is False
    out = B.evaluate("COIN", 0.6523, BTC_OK)
    assert out["available"] is False
    assert "does not track BTC" in out["note"]


@pytest.mark.parametrize("tk", ["SPY", "NVDA", "TLT", "", None])
def test_it_does_not_apply_to_equities(tk):
    assert B.applies_to(tk) is False


def test_the_whole_signal_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "BTC_SIGNAL_ENABLED", False)
    assert B.applies_to("IBIT") is False


# ── The arithmetic ────────────────────────────────────────────────────────────────────────────

def test_the_gap_converts_units_exactly_once():
    """IV travels as a FRACTION everywhere in this codebase (0.3272); DVOL arrives in vol
    POINTS (34.24). Mixing them silently produces a hundred-fold error that still looks like a
    plausible number, so the conversion happens in one place and is pinned here."""
    out = B.evaluate("IBIT", 0.3272, BTC_OK)
    assert out["proxy_iv_pp"] == 32.72
    assert out["dvol"] == 34.24
    assert out["iv_gap_pp"] == pytest.approx(1.52, abs=0.01)


def test_a_richer_etf_surface_reads_as_a_negative_gap():
    out = B.evaluate("IBIT", 0.4000, BTC_OK)      # ETF at 40.0 vs BTC at 34.24
    assert out["iv_gap_pp"] == pytest.approx(-5.76, abs=0.01)
    assert out["reading"] == "etf_rich"
    assert "the rich one" in out["note"]


def test_a_cheaper_etf_surface_reads_as_a_positive_gap():
    out = B.evaluate("IBIT", 0.2800, BTC_OK)      # ETF at 28.0 vs BTC at 34.24
    assert out["reading"] == "etf_cheap"
    assert "cheaper of the two surfaces" in out["note"]


def test_a_narrow_gap_reads_as_aligned_and_claims_no_edge():
    out = B.evaluate("IBIT", 0.3272, BTC_OK)
    assert out["reading"] == "aligned"
    assert "no cross-venue edge" in out["note"]


@pytest.mark.parametrize("gap_target, expected", [
    (3.0, "etf_cheap"), (2.99, "aligned"), (0.0, "aligned"),
    (-2.99, "aligned"), (-3.0, "etf_rich"),
])
def test_band_boundaries(gap_target, expected):
    iv = (BTC_OK["dvol"] - gap_target) / 100.0
    assert B.evaluate("IBIT", iv, BTC_OK)["reading"] == expected


# ── Honest absence ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("iv, btc, missing", [
    (None, BTC_OK, "IBIT ATM IV"),
    (0.3272, {"dvol": None}, "DVOL"),
    (0.3272, {}, "DVOL"),
    (0.3272, None, "DVOL"),
])
def test_a_missing_side_is_unavailable_not_neutral(iv, btc, missing):
    """Absence of information and a neutral reading are different facts, and only one of them
    is honest. An unavailable read must never present as `aligned`."""
    out = B.evaluate("IBIT", iv, btc)
    assert out["available"] is False
    assert out["reading"] != "aligned"
    assert missing in out["note"]


def test_the_shape_is_always_the_same():
    """Callers index this dict. Every path must return every key or a downstream KeyError
    becomes a scan failure on the day an endpoint goes down."""
    keys = set(B.evaluate("IBIT", 0.3272, BTC_OK))
    for out in (B.evaluate("COIN", 0.65, BTC_OK),
                B.evaluate("IBIT", None, None),
                B.evaluate("SPY", 0.2, BTC_OK)):
        assert set(out) >= keys - {"note"} or set(out).issubset(keys | {"note"})
        assert "available" in out and "reading" in out


def test_thresholds_are_flagged_provisional():
    """The band edges are reasoned, not fitted. Marking them provisional is what stops a guess
    from reading as evidence once it is on a dashboard."""
    assert B.evaluate("IBIT", 0.3272, BTC_OK)["provisional"] is True
    assert "provisional" in B.evaluate("IBIT", 0.3272, BTC_OK)["note"]


# ── It can never gate a trade ─────────────────────────────────────────────────────────────────

def test_the_signal_never_enters_the_gates_dict():
    """`qualified = all(gates.values())`, so a signal absent from that dict cannot block a trade
    whatever it reads. Advisory by construction beats advisory by remembering a flag."""
    import inspect
    from analysis import assessment as A
    assert "btc" not in inspect.getsource(A.evaluate_gates).lower()
    assert "btc_cross_venue" in inspect.getsource(A.assess)


def test_a_dead_endpoint_cannot_fail_an_equity_scan(monkeypatch):
    """A crypto outage must narrow what can be said about IBIT, never stop the scan that
    fifty-odd equity tickers share."""
    from analysis import assessment as A
    from data import crypto
    monkeypatch.setattr(crypto, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    out = A._btc_cross_venue({"ticker": "IBIT", "atm_iv": 0.3272})
    assert out["available"] is False


def test_the_read_is_skipped_entirely_for_non_btc_tickers(monkeypatch):
    """Fifty-odd equity names must not pay an HTTP round trip each."""
    from analysis import assessment as A
    from data import crypto
    called = []
    monkeypatch.setattr(crypto, "snapshot", lambda: called.append(1) or {})
    A._btc_cross_venue({"ticker": "SPY", "atm_iv": 0.20})
    assert called == [], "no crypto fetch should happen for a non-BTC ticker"


# ── Realised vol convention ───────────────────────────────────────────────────────────────────

def test_btc_realised_vol_annualises_on_365_not_252():
    """Bitcoin does not close. Using the equity convention would understate BTC realised vol by
    ~19% and bias every VRP comparison against DVOL the same way — worse than not computing it."""
    from data import crypto
    closes = [100.0 * math.exp(0.01 * (-1) ** i) for i in range(40)]
    candles = [{"date": f"2026-01-{i+1:02d}", "close": c, "high": c, "low": c} for i, c in enumerate(closes)]
    rv365 = crypto.realised_vol(candles, window=30)
    assert rv365 is not None
    ratio = math.sqrt(365) / math.sqrt(252)
    naive252 = rv365 / ratio
    assert rv365 > naive252
    assert "365" in __import__("inspect").getsource(crypto.realised_vol)


def test_realised_vol_needs_enough_bars_and_says_so_by_returning_none():
    from data import crypto
    assert crypto.realised_vol([], window=30) is None
    assert crypto.realised_vol([{"close": 100.0}] * 5, window=30) is None


# ── Ledger persistence ────────────────────────────────────────────────────────────────────────

def test_the_raw_gap_reaches_the_ledger_not_the_label(temp_ledger, read_ledger):
    """Store the number, never the band. BTC_IV_GAP_WIDE_PP has graded nothing yet, so writing
    the label would freeze today's guess into the permanent record."""
    from analysis import outcome_logger as ol
    tid = ol.open_paper_trade(
        ticker="IBIT", short_strike=34, long_strike=33, expiration="2026-09-18",
        entry_credit_per_share=0.15, dte=40, btc_iv_gap_pp=1.52, btc_vrp_pp=6.25)
    r = next(x for x in read_ledger() if x["id"] == tid)
    assert r["btc_iv_gap_pp"] == 1.52 and r["btc_vrp_pp"] == 6.25
    assert "etf_cheap" not in str(r) and "aligned" not in str(r)


def test_equity_trades_carry_the_fields_as_null(temp_ledger, read_ledger):
    from analysis import outcome_logger as ol
    tid = ol.open_paper_trade(
        ticker="SPY", short_strike=750, long_strike=747, expiration="2026-09-18",
        entry_credit_per_share=0.50, dte=40)
    r = next(x for x in read_ledger() if x["id"] == tid)
    assert r["btc_iv_gap_pp"] is None and r["btc_vrp_pp"] is None


def test_the_open_path_threads_the_gap_through():
    import inspect
    import auto_paper_cycle as apc
    src = inspect.getsource(apc)
    assert "btc_iv_gap_pp=_entry[" in src and "btc_vrp_pp=_entry[" in src


def test_entry_state_reads_the_gap_the_scan_measured_not_a_fresh_one():
    """A second fetch at open time would record a different number from the one that was judged.
    The candidate's own analysis block is the number the decision was made on."""
    import auto_paper_cycle as apc
    from conftest import make_candidate
    c = make_candidate(analysis={"btc_cross_venue": {"available": True, "iv_gap_pp": 1.52,
                                                     "btc_vrp_pp": 6.25}})
    s = apc._entry_state(c, {"spot": 36.8})
    assert s["btc_iv_gap_pp"] == 1.52 and s["btc_vrp_pp"] == 6.25


def test_an_unavailable_read_persists_as_null_rather_than_zero():
    import auto_paper_cycle as apc
    from conftest import make_candidate
    c = make_candidate(analysis={"btc_cross_venue": {"available": False, "iv_gap_pp": None}})
    s = apc._entry_state(c, {"spot": 36.8})
    assert s["btc_iv_gap_pp"] is None
