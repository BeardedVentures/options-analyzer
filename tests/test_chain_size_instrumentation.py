"""Chain SIZE is recorded separately from chain QUALITY, and gates nothing.

WHY. CHAIN_QUALITY_MIN_RATIO is a ratio, and _parse_robinhood_options drops any contract with
no market at all before it appends. A dropped quote batch therefore shortens the numerator and
the denominator together and the ratio does not move -- _option_record_is_quotable's own
docstring says so: "a dropped quote batch therefore lowers raw_count; it cannot lower this
ratio."

Measured over data_quality_log on 2026-09-03: SMH 111 contracts -> 2 (ratio 1.000), META 93 ->
2 (1.000), NEE 39 -> 2 (1.000), GS 162 -> 45 (1.000). Nineteen of fifty-six tickers collapsed
by half or more at least once and the health metric certified every one of them as perfect.

These tests pin two things: the numbers are recorded, and recording them changed no decision.
"""
import pytest

from data import fetcher


def _rec(bid=1.0, ask=1.1, mid=1.05, **kw):
    r = {"bid": bid, "ask": ask, "mid": mid, "strike": 100.0, "delta": -0.20,
         "volume": 10, "open_interest": 100}
    r.update(kw)
    return r


def test_size_fields_are_written_onto_every_record():
    recs = [_rec(), _rec(strike=95.0)]
    fetcher._stamp_chain_size(recs, raw_count=110, band_raw=30, band_ratio=1.0)
    for r in recs:
        assert r["chain_raw_count"] == 110
        assert r["chain_band_raw"] == 30
        assert r["chain_band_ratio"] == 1.0


def test_the_collapse_the_ratio_cannot_see_is_flagged():
    """The SMH case: 111 contracts became 2, and the quality ratio read 1.000. Perfect quality
    on a chain that had almost entirely disappeared."""
    recs = [_rec()]
    fetcher._stamp_chain_size(recs, raw_count=2, band_raw=2, band_ratio=1.0)
    assert recs[0]["chain_band_ratio"] == 1.0, "the ratio still says perfect -- that is the bug"
    assert recs[0]["chain_size_below_floor"] is True, "size must catch what the ratio cannot"


def test_a_full_chain_is_not_flagged():
    """Guards the test above from being a flag that is simply always on."""
    recs = [_rec()]
    fetcher._stamp_chain_size(recs, raw_count=110, band_raw=30, band_ratio=1.0)
    assert recs[0]["chain_size_below_floor"] is False


def test_a_thin_chain_and_a_collapsed_one_are_distinguishable_only_by_size():
    """Both read ratio 1.0. Only the size fields separate them, which is the entire point."""
    healthy, collapsed = [_rec()], [_rec()]
    fetcher._stamp_chain_size(healthy, raw_count=110, band_raw=30, band_ratio=1.0)
    fetcher._stamp_chain_size(collapsed, raw_count=2, band_raw=2, band_ratio=1.0)
    assert healthy[0]["chain_band_ratio"] == collapsed[0]["chain_band_ratio"]
    assert healthy[0]["chain_size_below_floor"] != collapsed[0]["chain_size_below_floor"]


def test_the_flag_reuses_the_existing_observational_constant_not_a_new_gate():
    """A brand-new threshold would read as a trading gate the moment somebody found it. This
    reuses CHAIN_QUALITY_MIN_BAND_CONTRACTS, which already means 'below this the band ratio is
    noise', and records the raw counts so any floor can be applied to old rows later."""
    import config
    recs = [_rec()]
    fetcher._stamp_chain_size(recs, raw_count=5, band_raw=5, band_ratio=1.0)
    assert recs[0]["chain_size_obs_floor"] == getattr(config, "CHAIN_QUALITY_MIN_BAND_CONTRACTS", 8)


def test_stamping_size_REJECTS_NOTHING():
    """The selection-affecting half is deliberately not here. Adding an absolute floor changes
    which spreads qualify, mid-drought, on a system whose cohort contract says entry rules
    define the population -- an operator decision. Recording is not."""
    import inspect
    src = inspect.getsource(fetcher._stamp_chain_size)
    assert "return []" not in src
    assert "_chain_gate_skipped" not in src
    for kw in ("skip", "reject", "raise"):
        assert kw not in src.lower().split('"""')[-1], f"{kw} appears in the executable body"


def test_an_empty_chain_is_left_alone():
    recs = []
    fetcher._stamp_chain_size(recs, raw_count=0, band_raw=0, band_ratio=0.0)
    assert recs == []


def test_zero_band_does_not_flag():
    """band_raw == 0 means the band was not measurable, not that it was tiny. Flagging it would
    conflate 'no reading' with 'a bad reading' -- the same conflation that made the yfinance
    chain_source label wrong for AMT and PLD."""
    recs = [_rec()]
    fetcher._stamp_chain_size(recs, raw_count=0, band_raw=0, band_ratio=0.0)
    assert recs[0]["chain_size_below_floor"] is False
