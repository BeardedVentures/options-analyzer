"""VRP measured against FORECAST realised vol, not trailing.

VEGA sells variance. The trade is paid against the realised vol of the NEXT ~35 days; the
engine was subtracting the realised vol of the LAST 35. Measured over 35,774 observations
across 20 names and 8 years, that gap is directional, not random:

    COMPRESSING (<0.85x)   trailing UNDERSTATES future vol by 5.5pp  -> VRP too POSITIVE
    EXPANDING   (>1.15x)   trailing OVERSTATES  future vol by 10.4pp -> VRP too NEGATIVE

So the engine sold into lulls right before vol picked up, and refused rich premium right after
a shock. Unconditionally the bias is -0.13pp — the two errors are large, opposite and cancel,
which is exactly why it survived. Held-out MAE 13.07 -> 12.29; state bias -7.25 -> +0.65 and
+13.11 -> +5.43.

This matters because `vrp_pct < 0` sets a disqualification_reason in edge_calculator. The
forecast is not decoration on a card — it decides what qualifies.
"""
import pytest

from analysis import vol_forecast as vf


def test_an_expanded_vol_forecast_comes_down():
    """The AMD case: vol spiked, trailing overstates, VRP reads far too negative."""
    fc = vf.forecast_rv(recent_pp=60.0, long_run_pp=40.0)
    assert fc["state"] == vf.EXPANDING
    assert fc["forecast_pp"] < 60.0
    assert fc["shift_pp"] < 0


def test_a_compressed_vol_forecast_goes_up():
    """The dangerous case: premium looks rich only because the stock has been quiet."""
    fc = vf.forecast_rv(recent_pp=12.0, long_run_pp=20.0)
    assert fc["state"] == vf.COMPRESSING
    assert fc["forecast_pp"] > 12.0
    assert fc["shift_pp"] > 0


def test_a_stable_name_is_barely_moved():
    fc = vf.forecast_rv(recent_pp=20.0, long_run_pp=20.0)
    assert fc["state"] == vf.STABLE
    assert abs(fc["shift_pp"]) < 0.01


def test_the_forecast_sits_between_recent_and_long_run():
    """Mean reversion, not extrapolation: it can never overshoot either anchor."""
    for recent, lr in ((60.0, 40.0), (12.0, 20.0), (33.0, 31.0)):
        f = vf.forecast_rv(recent, lr)["forecast_pp"]
        assert min(recent, lr) - 0.01 <= f <= max(recent, lr) + 0.01


def test_no_history_is_absence_not_a_silent_passthrough():
    """Falling back to the trailing number would reintroduce the exact bias being corrected."""
    assert vf.forecast_rv(None, 20.0) is None
    assert vf.forecast_rv(0, 20.0) is None


def test_a_missing_long_run_claims_nothing_beyond_what_is_observed():
    fc = vf.forecast_rv(25.0, None)
    assert fc["forecast_pp"] == pytest.approx(25.0)


def test_vrp_is_computed_against_the_forecast():
    fc = vf.forecast_rv(60.0, 40.0)
    assert vf.vrp_forecast(50.0, fc) == pytest.approx(50.0 - fc["forecast_pp"], abs=0.01)
    assert vf.vrp_forecast(50.0, None) is None


# ── Sector context ────────────────────────────────────────────────────────────────────────────

def test_a_cooling_sector_pulls_a_name_down():
    """Sector vol added real information out of sample (MAE 11.32 -> 10.94). It enters as a
    nudge, never as a level."""
    plain = vf.forecast_rv(30.0, 30.0)["forecast_pp"]
    cooling = vf.forecast_rv(30.0, 30.0, sector_recent_pp=40.0, sector_long_run_pp=20.0)
    assert cooling["forecast_pp"] < plain
    assert cooling["sector_adj"] < 1.0


def test_the_sector_nudge_stays_small():
    """The sector is context, not the subject. A violently reverting sector must not swamp the
    name's own measured vol."""
    a = vf.forecast_rv(30.0, 30.0)["forecast_pp"]
    b = vf.forecast_rv(30.0, 30.0, sector_recent_pp=80.0, sector_long_run_pp=20.0)["forecast_pp"]
    assert abs(b - a) / a < 0.25


def test_sector_proxies_come_from_the_existing_ticker_map():
    """One taxonomy, two consumers — a second sector map would drift from the position cap."""
    assert vf.sector_proxy_for("AAPL") == "XLK"
    assert vf.sector_proxy_for("JPM") == "XLF"
    assert vf.sector_proxy_for("NOT_A_TICKER") is None


def test_crypto_has_no_equity_sector_proxy():
    """Its vol is driven by its own market; the equity sector complex says nothing about it."""
    assert vf.sector_proxy_for("IBIT") is None


def test_a_sector_outage_costs_precision_not_the_signal():
    fc = vf.for_ticker("AAPL", 30.0, 25.0, fetch=lambda t: None)
    assert fc is not None and fc["forecast_pp"] > 0
    assert fc["sector_adj"] == 1.0


# ── The wiring that makes it matter ───────────────────────────────────────────────────────────

def test_negative_vrp_still_disqualifies_so_the_forecast_has_teeth():
    """`vrp_pct < 0` sets a disqualification_reason. That is why forecasting rather than
    trailing changes which trades qualify instead of only how they are described."""
    from analysis import edge_calculator as ec
    r = ec.calculate_edge_score(ticker="X", strategy="bull_put_spread", technical_score=60,
                                vrp_pct=-1.0, edge_points=3.0, news_sentiment="NEUTRAL",
                                earnings_days_away=99, fundamentals_score=50)
    assert r["disqualification_reason"]
    assert (r["component_breakdown"] or {}).get("vrp") == 0


def test_a_name_with_no_earnings_date_does_not_crash_the_score():
    """days_to_earnings is None for an ETF, and `None < int` raises — which would drop the
    ticker into the scan's error path rather than scoring it."""
    from analysis import edge_calculator as ec
    r = ec.calculate_edge_score(ticker="SPY", strategy="bull_put_spread", technical_score=60,
                                vrp_pct=5.0, edge_points=3.0, news_sentiment="NEUTRAL",
                                earnings_days_away=None, fundamentals_score=50)
    assert r["total_score"] > 0


def test_technicals_emits_both_numbers_so_the_change_is_auditable():
    """vrp_trailing_pp sits beside vrp on every row; turning the flag off restores the old
    behaviour exactly."""
    import inspect
    from data import technicals
    src = inspect.getsource(technicals.calculate_all)
    for field in ("vrp_trailing_pp", "vrp_shift_pp", "rv_forecast_pp", "vol_state"):
        assert field in src, f"{field} is not emitted"
    assert "VRP_USE_FORECAST" in src, "the change must be reversible by config"
