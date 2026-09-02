#!/usr/bin/env python3
"""Two blind spots found on 2026-09-01, both of the same shape: a fact the system HAD and
did not carry forward to the place that needed it.

1. Iron condors reached the ledger with no chain_source, so outcome_logger.vendor_basis
   rendered them 'unrecorded' -- while a bull put built from the IDENTICAL Robinhood fetch
   in the same scan rendered 'robinhood'. Two cohort keys for one chain.

2. robinhood_mcp._is_retryable_error defaults an unknown error body to PERMANENT (correct --
   it refuses to burn four attempts on a request that can never succeed) but said so to
   nobody. If Robinhood rewords its rate-limit response, every marker stops matching, retries
   collapse to a single attempt, and the only symptom is a coverage regression with no stated
   cause. The 2026-09-01 scans made that concrete: seven scans, zero rate limits, so the
   matcher is load-bearing code that production has never once exercised.
"""
import pytest


# ── 1. Vendor provenance ─────────────────────────────────────────────────────────────────

def test_a_single_vendor_collapses_to_its_own_name():
    from multi_strategy import _vendor_of
    assert _vendor_of({"chain_source": "robinhood"}) == "robinhood"
    assert _vendor_of({"chain_source": "robinhood"},
                      {"chain_source": "robinhood"}) == "robinhood"


def test_two_vendors_get_a_label_that_cannot_be_pooled_with_either():
    """The condor's wings come from two INDEPENDENT fetches -- get_call_options_chain and
    get_options_chain each choose their own source and each falls back to yfinance alone. If
    this returned one wing's label, a half-yfinance trade would file under 'robinhood' and
    walk straight past the dimension vendor_basis exists to enforce."""
    from multi_strategy import _vendor_of
    label = _vendor_of({"chain_source": "robinhood"}, {"chain_source": "yfinance"})
    assert label == "mixed:robinhood+yfinance"
    assert label not in ("robinhood", "yfinance")


def test_order_does_not_change_the_label():
    """Otherwise the same mixed trade keys two ways depending on which wing was passed first,
    which splits a cohort on argument order."""
    from multi_strategy import _vendor_of
    a = _vendor_of({"chain_source": "yfinance"}, {"chain_source": "robinhood"})
    b = _vendor_of({"chain_source": "robinhood"}, {"chain_source": "yfinance"})
    assert a == b


def test_a_missing_source_stays_missing():
    """None becomes 'unrecorded' in vendor_basis, which is the honest answer. Guessing
    'robinhood' here would fabricate a measurement -- the same error the ledger's backfill
    rules exist to prevent."""
    from multi_strategy import _vendor_of
    assert _vendor_of({}, {}) is None
    assert _vendor_of(None) is None
    assert _vendor_of({"chain_source": None}, {"chain_source": "robinhood"}) == "robinhood"


@pytest.mark.parametrize("fn", ["build_bear_call", "build_iron_condor"])
def test_every_call_side_generator_stamps_the_vendor(fn):
    """The test that would have caught this. build_bear_call stamped chain_source and
    build_iron_condor did not, and nothing compared them -- the gap was one missing key in a
    40-key dict, in a path that only produces rows when a condor qualifies."""
    import inspect, multi_strategy
    src = inspect.getsource(getattr(multi_strategy, fn))
    assert "chain_source" in src, f"{fn} emits a board row with no vendor provenance"


def test_the_put_path_stamps_it_too():
    """main.screen_ticker is where the ledger's bull puts come from."""
    import inspect, main
    assert "chain_source" in inspect.getsource(main.screen_ticker)


# ── 2. Unrecognised error bodies ─────────────────────────────────────────────────────────

def test_an_unknown_error_body_is_counted():
    from data import robinhood_mcp as rm
    rm.reset_unrecognized_errors()
    rm._note_unrecognized("QUOTA_EXCEEDED: slow down")
    assert sum(rm.unrecognized_errors().values()) == 1


def test_a_recognised_rate_limit_is_not_counted_as_unknown():
    """The counter must be able to read ZERO while the system is working. A counter that
    ticks on every error says nothing about whether the matcher still matches."""
    from data import robinhood_mcp as rm
    assert rm._is_retryable_error("RATE_LIMITED: too many requests") is True
    assert rm._is_retryable_error("Error: 429 Too Many Requests") is True
    rm.reset_unrecognized_errors()
    assert rm.unrecognized_errors() == {}


def test_the_matcher_can_actually_fail_to_match():
    """Guards against the counter being vacuously empty: prove an unrecognised body exists."""
    from data import robinhood_mcp as rm
    assert rm._is_retryable_error("Request entity too large") is False
    assert rm._is_retryable_error("CAPACITY_SHED: retry shortly") is False   # a plausible reword


def test_repeats_of_one_failure_collapse_to_one_signature():
    """56 tickers hitting one server-side change should read as one line with a count, not 56
    near-identical lines nobody scrolls through."""
    from data import robinhood_mcp as rm
    rm.reset_unrecognized_errors()
    for _ in range(5):
        rm._note_unrecognized("CAPACITY_SHED: retry shortly")
    errs = rm.unrecognized_errors()
    assert len(errs) == 1 and list(errs.values()) == [5]


def test_an_empty_body_still_gets_a_signature():
    from data import robinhood_mcp as rm
    rm.reset_unrecognized_errors()
    rm._note_unrecognized("")
    assert list(rm.unrecognized_errors()) == ["<empty response>"]


def test_chain_coverage_surfaces_the_counter():
    """Counted-but-unreported is the failure mode this whole item is about."""
    from data import fetcher, robinhood_mcp as rm
    fetcher.clear_cache()
    assert fetcher.chain_coverage()["unrecognized_errors"] == {}
    rm._note_unrecognized("CAPACITY_SHED: retry shortly")
    assert fetcher.chain_coverage()["unrecognized_errors"] == {"CAPACITY_SHED: retry shortly": 1}
    fetcher.clear_cache()
    assert fetcher.chain_coverage()["unrecognized_errors"] == {}, "not reset per scan"


def test_the_scan_reports_it_out_loud():
    """scan_latest.json is not read by a human between scans. A new signature has to reach
    run.log or it is invisible until someone goes looking for it."""
    import inspect, main
    src = inspect.getsource(main.run_scan)
    assert "unrecognized_errors" in src and "UNRECOGNIZED_ERROR_BODY" in src
