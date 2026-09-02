#!/usr/bin/env python3
"""The crypto/equity boundary, enforced rather than remembered.

VEGA carries a BTC layer (btc_signal, btc_forecast, crypto_vol_forecast, data/crypto). The
2026-09-01 review asserted it "ran inside all seven scans", which was wrong, and the correction
is the reason this file exists -- a boundary nobody can measure is a boundary that drifts.

WHERE THE LINE ACTUALLY IS, measured on the 2026-09-01 cycles:

  * auto_paper_cycle runs main.py as a SUBPROCESS. _record_btc_forecast and
    _record_crypto_premium_view run afterwards, in the parent, once main.py has exited --
    a different process, so they consume none of the scan's rate-limit budget. Measured cost
    12s of a 27m40s cycle (0.7%): 15:01:26 -> 15:01:33 and 15:01:55 -> 15:02:00.
  * The ONE in-scan hook is assessment._btc_cross_venue, which makes a network call only for
    config.BTC_PROXY_TICKERS ({'IBIT'}); the other 55 names short-circuit before any I/O.
    IBIT is on the watchlist deliberately, for uncorrelated VRP, and DVOL-vs-IBIT-IV is a
    premium read -- it serves the options mission rather than competing with it.
  * Crypto claims go to logs/vega_predictions.jsonl. The 30-trade cohort clock lives in
    logs/vega_outcomes.jsonl and is not written by any crypto path.

The property that makes all of this safe is ONE line in assess(): `qualified` is
`all(gates.values())`, and no crypto reading is ever a member of `gates`. These tests hold that
line behaviourally, so it cannot be lost to a refactor that looks harmless.
"""
import pytest

import config


def _ctx(ticker="IBIT"):
    return {"ticker": ticker, "spot": 36.80, "levels": {}, "structure": {},
            "term_structure": {}, "skew": {}, "closes": [], "puts": []}


def _spread():
    return {"side": "put", "short_strike": 34.0, "long_strike": 33.0, "dte": 33,
            "natural_credit_usd": 40.0, "natural_credit_to_width": 0.23, "pop": 0.80,
            "short_delta": -0.20, "short_leg": {"bid": 0.45, "ask": 0.50, "delta": -0.20}}


# ── The load-bearing invariant ───────────────────────────────────────────────────────────

def test_no_crypto_reading_is_a_gate():
    """`qualified = all(gates.values())`. A key that is never in `gates` cannot block a trade
    however alarming it reads -- advisory BY CONSTRUCTION, not by remembering to set a flag."""
    from analysis.assessment import evaluate_gates
    gates = evaluate_gates(_spread(), _ctx())
    leaked = [k for k in gates
              if any(w in k.lower() for w in ("btc", "crypto", "dvol", "cross_venue"))]
    assert leaked == [], f"a crypto reading became a hard gate: {leaked}"


def test_the_gate_set_is_exactly_the_declared_contract():
    """Stops the leak from the other direction: a new gate key appearing at all is visible
    here, whatever it is named."""
    from analysis.assessment import evaluate_gates
    gates = evaluate_gates(_spread(), _ctx())
    assert set(gates) == set(config.REQUIRED_GATES), (
        "evaluate_gates no longer matches config.REQUIRED_GATES -- if that is intended, update "
        "the contract deliberately")


@pytest.mark.parametrize("reading", [
    # Shapes match btc_signal.evaluate(): an available reading always carries `note`, and
    # the not_applicable shape (the path 55 of 56 tickers take) carries neither note nor gap.
    {"available": True, "iv_gap_pp": 25.0, "btc_vrp_pp": 40.0, "reading": "screaming_rich",
     "note": "IBIT IV is 25pp under DVOL"},
    {"available": True, "iv_gap_pp": -25.0, "btc_vrp_pp": -40.0, "reading": "screaming_cheap",
     "note": "IBIT IV is 25pp over DVOL"},
    {"available": False, "reading": "not_applicable"},
])
def test_qualification_is_identical_whatever_crypto_says(monkeypatch, reading):
    """The behavioural version, and the one that would actually catch a regression: drive the
    crypto read to its extremes and confirm the verdict does not move. Inspecting key names
    would pass a refactor that folded the signal into an existing gate's value."""
    from analysis import assessment
    monkeypatch.setattr(assessment, "_btc_cross_venue", lambda ctx: dict(reading))
    out = assessment.assess(_spread(), _ctx(), strategy=assessment.BULL_PUT)
    baseline = assessment.evaluate_gates(_spread(), _ctx())
    assert out["qualified"] == all(baseline.values())
    assert out["failed_gates"] == [k for k, v in baseline.items() if not v]
    assert out["analysis"]["btc_cross_venue"] == reading   # recorded, never acted on


# ── The blast radius of the one in-scan hook ─────────────────────────────────────────────

def test_only_declared_proxies_can_reach_a_crypto_endpoint():
    """55 of 56 tickers must short-circuit before any I/O. If applies_to ever widened, the
    scan would start paying a network call per ticker for a signal only IBIT can use."""
    from analysis import btc_signal
    assert btc_signal.applies_to("IBIT") is True
    for t in ("AAPL", "SPY", "XLE", "GE", "COIN"):
        assert btc_signal.applies_to(t) is False, f"{t} would now hit a crypto endpoint"


def test_coin_is_still_excluded():
    """Measured 2026-08-09: DVOL vs IBIT was a 1.5pt gap, vs COIN 31pt. COIN is an operating
    company, so that gap measures the difference between the assets rather than a mispricing
    of one. Adding it would turn a cross-venue read into noise."""
    assert "COIN" not in config.BTC_PROXY_TICKERS
    assert config.BTC_PROXY_TICKERS == {"IBIT"}


def test_a_dead_crypto_endpoint_cannot_break_the_equity_scan(monkeypatch):
    """The failure mode that matters operationally. Deribit going down must narrow what can be
    said about IBIT, never stop the other 55 names."""
    from analysis import assessment

    def _boom(*a, **k):
        raise RuntimeError("deribit unreachable")

    monkeypatch.setattr("analysis.btc_signal.applies_to", _boom)
    out = assessment._btc_cross_venue(_ctx())
    assert out["available"] is False
    assert assessment.assess(_spread(), _ctx(),
                             strategy=assessment.BULL_PUT)["qualified"] in (True, False)


# ── Ledger separation ────────────────────────────────────────────────────────────────────

def test_crypto_claims_go_to_the_prediction_ledger_not_the_trade_ledger():
    """The 30-trade cohort clock lives in vega_outcomes.jsonl. Crypto must not be able to
    advance, split or contaminate it -- the two files are the boundary."""
    from analysis import predictions
    from analysis import outcome_logger
    assert predictions.PREDICTIONS_FILE.name == "vega_predictions.jsonl"
    assert outcome_logger.OUTCOMES_FILE.name == "vega_outcomes.jsonl"
    assert predictions.PREDICTIONS_FILE != outcome_logger.OUTCOMES_FILE


def test_btc_fields_on_a_trade_row_are_recordings_not_inputs():
    """open_paper_trade accepts btc_iv_gap_pp/btc_vrp_pp so an IBIT spread carries what was known
    when it opened. They must stay OPTIONAL -- a required crypto field would make the equity
    ledger depend on a crypto feed being up."""
    import inspect
    from analysis.outcome_logger import open_paper_trade
    params = inspect.signature(open_paper_trade).parameters
    for f in ("btc_iv_gap_pp", "btc_vrp_pp"):
        assert f in params, f"{f} vanished from the ledger contract"
        assert params[f].default is None, f"{f} became required"


def test_cohort_key_carries_no_crypto_dimension():
    """cohort() is fill_model|gate_basis|close_logic|entry_epoch|vendor_basis. A crypto term
    here would split the equity cohort on a signal that never selected the trade."""
    from analysis import outcome_logger
    key = outcome_logger.cohort({"fill_model": "natural", "chain_source": "robinhood"})
    assert "btc" not in key.lower() and "crypto" not in key.lower()
    assert len(key.split("|")) == 5


# ── The process boundary ─────────────────────────────────────────────────────────────────

def test_the_scan_runs_as_a_subprocess_before_any_crypto_phase():
    """This is WHY crypto costs the scan no request budget, and it is a property of the call
    order in auto_paper_cycle.main -- exactly the kind of thing a tidy-up reorders. main.py is
    spawned and has exited before _record_btc_forecast is reached."""
    import inspect, auto_paper_cycle
    src = inspect.getsource(auto_paper_cycle.main)
    i_scan = src.index('"main.py"')
    i_btc = src.index("_record_btc_forecast()", i_scan)
    i_prem = src.index("_record_crypto_premium_view()", i_scan)
    assert i_scan < i_btc and i_scan < i_prem, (
        "a crypto phase moved ahead of the scan subprocess; it would now compete with the "
        "options fetch for the same wall clock and rate-limit budget")
