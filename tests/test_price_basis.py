"""Price-basis discipline: which reads want raw prices and which want adjusted.

The rule, written down because the next technical field added will otherwise get it wrong the
same way: anything compared against a STRIKE or other fixed price level reads RAW; anything
measuring a RETURN, a range or a volatility reads ADJUSTED.
"""
import inspect


def test_levels_in_screen_ticker_read_raw_prices():
    """A support level is compared against a short strike by _shelter_ok, and a strike is a
    fixed number in raw price space. Measured 2026-09-02: swapping the source flipped 6
    FAIL->PASS and 3 PASS->FAIL across 544 spreads -- small, but wrong is wrong."""
    import main
    src = inspect.getsource(main.screen_ticker)
    assert "get_raw_price_data" in src, "levels must be detected off the raw series"
    i_raw = src.index("get_raw_price_data")
    i_lvl = src.index("find_levels(")
    assert i_raw < i_lvl, "the raw fetch must precede the level detection it feeds"


def test_level_alerts_read_raw_prices():
    import auto_paper_cycle as apc
    fn = [v for k, v in vars(apc).items()
          if callable(v) and "find_levels" in (inspect.getsource(v) if hasattr(v, "__code__") else "")]
    assert fn, "expected a level-alert function calling find_levels"
    for f in fn:
        assert "get_raw_price_data" in inspect.getsource(f), (
            f"{f.__name__} compares levels to strikes and must read raw prices")


def test_technicals_still_read_ADJUSTED_prices():
    """The other half of the rule, and the guard against over-applying the fix. Returns, RSI,
    ATR and realized vol are all differences -- a dividend drop is not a real move, and the
    adjusted series is the correct input. Swapping these to raw would be a new bug."""
    import inspect as _i
    from analysis import vol_forecast, direction_forecast
    for mod in (vol_forecast, direction_forecast):
        src = _i.getsource(mod)
        assert "get_raw_price_data" not in src, (
            f"{mod.__name__} measures returns/vol and must keep the ADJUSTED series")


def test_the_two_fetchers_are_actually_different():
    """Guards every test above from being a tautology: if get_raw_price_data were a thin alias
    for the adjusted one, all of these would pass and nothing would be fixed."""
    from data import fetcher
    raw_src = inspect.getsource(fetcher.get_raw_price_data)
    adj_src = inspect.getsource(fetcher.get_price_data)
    assert "auto_adjust=False" in raw_src
    assert "auto_adjust=True" in adj_src
    assert "rawprice_" in raw_src, "must not share the adjusted series' cache key"
