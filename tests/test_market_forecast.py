"""The standing market regime call, and the three ways a screen like it lies.

This module exists because the cockpit was a SNAPSHOT pretending to be a board. It rendered
whatever was on disk when its window opened, nothing told it the board had moved, and the only
way to see current numbers was to close the engine and relaunch it. The fix has three parts and
each part has a failure mode that looks exactly like success from the outside:

  1. A page that reloads on a TIMER refreshes a weekend board that cannot have changed and
     interrupts a read for nothing. The reload must be driven by the artifact's mtime.
  2. A "live" forecast served out of data/fetcher's cache is not live. That cache has no expiry
     -- it is sized for a two-minute scan process -- so in a long-lived server the first frame
     fetched is the frame shown for as long as the window stays open. That is the ORIGINAL BUG
     one layer down, and it would have been invisible: the page would have looked perfect.
  3. A crypto claim annualised on 252 sessions carries a flat band 20% too narrow, so NEUTRAL
     becomes 20% harder to hit on exactly the assets that move most -- and the resulting hit
     rate is wrong in a direction nobody would think to check.
"""
from datetime import date

import math

import pytest

import config
from analysis import market_forecast as mf
from analysis import predictions as P


def _series(n=420, drift=0.0, vol=0.011, px0=100.0):
    """A deterministic geometric walk. No randomness: a probability test that flakes is noise."""
    out, px = [], px0
    for i in range(n):
        # A fixed oscillation supplies the variance; drift supplies the direction.
        px *= math.exp(drift + vol * math.sin(i * 0.7) * 0.9)
        out.append(px)
    return out


def _asset(klass="equity", ticker="SPY"):
    return {"ticker": ticker, "name": ticker, "klass": klass, "group": "g"}


def _spec(key="1m"):
    return next(h for h in mf.HORIZONS if h[0] == key)


# ── The band ────────────────────────────────────────────────────────────────
def test_flat_band_makes_the_three_outcomes_equally_likely_under_zero_drift():
    """The whole page is read against this. If the three do not start tied, every printed
    percentage means something other than what the operator will take it to mean."""
    f = mf.forecast(_asset(), _series(), _spec("1m"), today=date(2026, 9, 8), apply_tilt=False)
    assert f["expected"] != "none"
    assert f["p_up"] == pytest.approx(1 / 3, abs=0.005)
    assert f["p_down"] == pytest.approx(1 / 3, abs=0.005)
    assert f["p_flat"] == pytest.approx(1 / 3, abs=0.005)


def test_the_three_probabilities_sum_to_one():
    """Three separately rounded numbers sum to 0.9999, and a distribution that does not sum to
    one resurfaces later as a Brier score nobody can reconcile."""
    for key in mf.HORIZON_KEYS:
        f = mf.forecast(_asset(), _series(drift=0.0004), _spec(key), today=date(2026, 9, 8))
        assert f["p_up"] + f["p_down"] + f["p_flat"] == pytest.approx(1.0, abs=1e-9)


def test_crypto_band_is_wider_than_the_equity_band_for_the_same_series():
    """SAME prices, same horizon, different calendar. Crypto compounds over 365 days and
    equities over 252, so a week is 7 of one and 5 of the other; annualising crypto on 252
    understates its horizon sigma by sqrt(365/252) and silently makes NEUTRAL harder to hit."""
    px = _series()
    eq = mf.forecast(_asset("equity"), px, _spec("1w"), today=date(2026, 9, 8))
    cr = mf.forecast(_asset("crypto", "BTC-USD"), px, _spec("1w"), today=date(2026, 9, 8))
    assert cr["flat_band_pct"] > eq["flat_band_pct"]
    assert cr["horizon_periods"] == 7 and eq["horizon_periods"] == 5


def test_vol_window_scales_with_the_horizon():
    """A six-month band set from twenty sessions of vol is a reading of last month wearing a
    half-year label. The window grows with the horizon and is capped so the data requirement
    stays finite."""
    px = _series()
    day = mf.forecast(_asset(), px, _spec("24h"), today=date(2026, 9, 8))
    half = mf.forecast(_asset(), px, _spec("6m"), today=date(2026, 9, 8))
    assert day["vol_window"] == mf.VOL_WINDOW_MIN
    assert half["vol_window"] == mf.VOL_WINDOW_MAX > day["vol_window"]


# ── The tilt ────────────────────────────────────────────────────────────────
def test_the_lean_grows_with_the_horizon_because_drift_accumulates_linearly():
    """Drift accumulates in t and sigma in sqrt(t), so the SAME edge is worth mu = IR*sqrt(t).
    A constant sigma tilt across horizons -- the obvious implementation -- implies a drift at
    one day that annualises past 100%, and the error is largest where the signal is weakest."""
    px = _series(drift=0.0009)
    leans = []
    for key in ("24h", "1w", "1m", "6m"):
        f = mf.forecast(_asset(), px, _spec(key), today=date(2026, 9, 8))
        leans.append(abs(f["mu_sigmas"]))
    assert leans == sorted(leans)
    assert leans[0] < leans[-1]


def test_the_baseline_twin_is_pure_climatology():
    """It must carry the identical band and horizon with the mean pinned at zero, or grading the
    signal against it charges the signal for something other than its own existence."""
    px = _series(drift=0.0009)
    live = mf.forecast(_asset(), px, _spec("1m"), today=date(2026, 9, 8), apply_tilt=True)
    base = mf.forecast(_asset(), px, _spec("1m"), today=date(2026, 9, 8), apply_tilt=False)
    assert base["mu_sigmas"] == 0.0
    assert base["flat_band_pct"] == live["flat_band_pct"]
    assert base["horizon_periods"] == live["horizon_periods"]
    assert live["mu_sigmas"] != 0.0


def test_abstains_rather_than_guessing_on_thin_history():
    """A forecaster that cannot decline has to guess, and a guess written into the ledger
    poisons the record it was built to grade."""
    f = mf.forecast(_asset(), _series(n=20), _spec("1w"), today=date(2026, 9, 8))
    assert f["expected"] == "none" and f["call"] == "NO CALL"
    assert f["probability"] is None


# ── The claim ───────────────────────────────────────────────────────────────
def test_claim_types_are_scored_by_the_existing_direction_scorer():
    """predictions matches direction claims BY PREFIX. A type that misses the prefix falls
    through to "no scorer for claim type" and marks its whole population unresolvable while the
    ledger keeps reporting a healthy row count -- a channel that is broken and looks fine."""
    for ct in mf.CLAIM_TYPES:
        assert P.is_direction_claim(ct), ct
        assert P.is_direction_claim(f"{ct}_baseline"), ct


def test_claim_types_do_not_collide_with_the_watchlist_sweep():
    """grade() buckets by claim_type. Sharing a bucket with direction_forecast's 56-ticker sweep
    would average two populations with different base rates and different bands."""
    from analysis import direction_forecast as dfc
    sweep = {h[0] for h in dfc.HORIZONS}
    assert not (set(mf.CLAIM_TYPES) & sweep)


def test_record_daily_writes_one_live_claim_and_one_twin_per_asset_and_horizon(monkeypatch):
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])
    px = _series(drift=0.0009)
    stats = mf.record_daily(today=date(2026, 9, 8), price_lookup=lambda tk: px, force=True)
    assert stats["recorded"] == len(mf.HORIZONS)

    rows = P.load()
    live = [r for r in rows if not (r["context"] or {}).get("baseline")]
    twin = [r for r in rows if (r["context"] or {}).get("baseline")]
    assert len(live) == len(twin) == len(mf.HORIZONS)
    for r in live:
        ctx = r["context"]
        assert ctx["cohort"] == mf.COHORT and ctx["close_logic"] == mf.COHORT
        # Everything the direction scorer reads must be present, or the claim resolves to
        # "missing direction context" months after it was too late to fix.
        assert ctx["expected"] in ("up", "down", "flat")
        assert ctx["price_at_claim"] and ctx["flat_band_pct"] and ctx["score_on"]
        assert r["resolves_on"] > ctx["score_on"]


def test_record_daily_is_idempotent_for_the_day(monkeypatch):
    """The scheduler stamps the day BEFORE running the job, a retry is normal, and two cockpits
    may be open. Re-recording must be a no-op or a day's opinion gets counted many times and
    the hit rate starts describing how often the job ran."""
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])
    px = _series(drift=0.0009)
    mf.record_daily(today=date(2026, 9, 8), price_lookup=lambda tk: px, force=True)
    before = len(P.load())
    mf.record_daily(today=date(2026, 9, 8), price_lookup=lambda tk: px, force=True)
    assert len(P.load()) == before


def test_record_daily_never_raises_on_a_dead_price_feed(monkeypatch):
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])

    def boom(_tk):
        raise RuntimeError("vendor down")

    stats = mf.record_daily(today=date(2026, 9, 8), price_lookup=boom, force=True)
    assert stats["failed"] == 1 and stats["recorded"] == 0


def test_the_hour_gate_stops_a_morning_anchor(monkeypatch):
    """The claim is anchored to price_at_claim and settles on a later close. Anchoring it at
    09:35 hands the 24-hour call several hours of the move it is supposed to be predicting."""
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])
    monkeypatch.setattr(config, "MARKET_FORECAST_AFTER_HOUR", 99, raising=False)
    monkeypatch.delenv("VEGA_MARKET_FORECAST_AFTER_HOUR", raising=False)
    stats = mf.record_daily(today=date(2026, 9, 8), price_lookup=lambda tk: _series())
    assert stats["skipped_hour"] and stats["recorded"] == 0


# ── The live board ──────────────────────────────────────────────────────────
def test_live_board_writes_nothing(monkeypatch):
    """The grid recomputes on every page load. If it also recorded, a day's claim would be
    written once per refresh at drifting anchors and the ledger would grade the refresh rate."""
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])
    before = len(P.load())
    mf.live_board(today=date(2026, 9, 8), price_lookup=lambda tk: _series())
    assert len(P.load()) == before


def test_live_board_survives_one_dead_asset(monkeypatch):
    """A crypto endpoint going down must narrow the page, never blank it."""
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY"),
                                               _asset("crypto", "BTC-USD")])

    def half(tk):
        if tk == "BTC-USD":
            raise RuntimeError("venue down")
        return _series()

    b = mf.live_board(today=date(2026, 9, 8), price_lookup=half)
    assert len(b["rows"]) == 2
    assert b["rows"][0]["cells"]["1m"]["expected"] != "none"
    assert b["rows"][1]["cells"]["1m"]["expected"] == "none"
    assert b["errors"]


def test_price_cache_expires(monkeypatch):
    """THE FAILURE THAT WOULD HAVE LOOKED LIKE SUCCESS. data/fetcher._cache has no expiry, so a
    long-lived cockpit serving the board straight from it would show the first frame it ever
    fetched, forever, under a LIVE heading."""
    calls = {"n": 0}

    class _FakeFrame:
        empty = False

        def __init__(self, closes):
            self._c = closes

        def __getitem__(self, _k):
            return type("S", (), {"tolist": lambda _s: self._c})()

    def fake_get(ticker, period="2y"):
        calls["n"] += 1
        return _FakeFrame(_series())

    import sys
    import types
    import data as data_pkg
    fake_fetcher = types.SimpleNamespace(get_price_data=fake_get, _cache={})
    # BOTH bindings, and the attribute is the one that actually matters. `from data import
    # fetcher` resolves the ATTRIBUTE on the package first and only falls back to sys.modules
    # when it is absent, so patching sys.modules alone works in a bare interpreter and silently
    # stops working the moment anything else has already imported data.fetcher -- which the
    # conftest does. The test then reaches the real vendor, the fake counter stays at zero, and
    # a passing TTL looks identical to a TTL that was never exercised.
    monkeypatch.setitem(sys.modules, "data.fetcher", fake_fetcher)
    monkeypatch.setattr(data_pkg, "fetcher", fake_fetcher, raising=False)
    monkeypatch.setattr(config, "MARKET_FORECAST_CACHE_MIN", 10, raising=False)
    mf._price_cache.clear()

    mf._default_lookup("SPY")
    mf._default_lookup("SPY")
    assert calls["n"] == 1, "the TTL should hold within the window"

    # Age the entry past the TTL and it must go back to the vendor.
    stamp, closes = mf._price_cache["SPY"]
    mf._price_cache["SPY"] = (stamp - 3600, closes)
    mf._default_lookup("SPY")
    assert calls["n"] == 2


# ── The graded record ───────────────────────────────────────────────────────
def test_track_record_reports_every_horizon_even_with_an_empty_ledger():
    """A page that hides a horizon until it has data lets a channel that never wrote anything
    look identical to one that is merely young."""
    t = mf.track_record(rows=[])
    assert [h["horizon"] for h in t["horizons"]] == list(mf.HORIZON_KEYS)
    assert all(h["signal"] is None and not h["gradeable"] for h in t["horizons"])


def test_track_record_grades_the_signal_against_its_own_twin(monkeypatch):
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])
    mf.record_daily(today=date(2026, 9, 8), price_lookup=lambda tk: _series(drift=0.0009),
                    force=True)
    rows = P.load()
    for r in rows:                      # settle them all as correct, which is enough to grade
        r["status"], r["correct"] = "resolved", True
    t = mf.track_record(rows=rows)
    m = {h["horizon"]: h for h in t["horizons"]}["1m"]
    assert m["signal"] and m["baseline"], "both the claim and its control must be graded"
    assert m["skill_vs_baseline"] is not None


def test_recent_hides_the_baseline_twins(monkeypatch):
    """The twin is a control, not a forecast. Listing it beside the live claims would show the
    operator two calls a day per horizon and invite them to read the control as an opinion."""
    monkeypatch.setattr(mf, "assets", lambda: [_asset("equity", "SPY")])
    mf.record_daily(today=date(2026, 9, 8), price_lookup=lambda tk: _series(drift=0.0009),
                    force=True)
    assert all(not (r["context"] or {}).get("baseline") for r in mf.recent(50))
    assert len(mf.recent(50)) == len(mf.HORIZONS)


# ── The recorder has to run when nobody is watching ─────────────────────────
def test_the_daily_claim_is_driven_by_the_cycle_not_only_by_the_cockpit():
    """A ledger written only while the dashboard is open records when the desk was staffed.

    The cockpit's scheduler fires the daily claim, which is fine as far as it goes -- but the
    cockpit is a window the operator opens when they feel like looking at something. If that
    were the ONLY driver, every horizon's hit rate would be conditioned on the days Josh
    happened to launch the engine, with nothing on the page hinting at it. The Windows task
    runs the paper cycle whether anyone is at the machine or not, so the claim is wired there
    too; predictions.record is idempotent per (asset, horizon, day), so both firing is a no-op.

    Asserted against the BAND sweep's call sites rather than a hardcoded count: the invariant
    is "wired into every cycle path the other measurement channels are", which survives someone
    adding a third path, and a bare `== 2` would not.
    """
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "auto_paper_cycle.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    called = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called[node.func.id] = called.get(node.func.id, 0) + 1
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    assert "_record_market_forecast" in defined, (
        "the paper cycle has no market-forecast recorder, so the regime claim is written only "
        "while the cockpit window is open")
    assert called.get("_record_market_forecast", 0) == called.get("_record_band_forecasts", 0), (
        "the market forecast is wired into fewer cycle paths than the band sweep — one of the "
        "paths (full cycle / mark-only) will silently skip the daily claim")
