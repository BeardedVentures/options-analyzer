"""The two engines must agree, and this test is the only thing that makes them.

There are two independent paths that produce tradeable candidates:

    main.py            -> screen_ticker()      -> the board the operator reads
    vega_candidates.py -> build_candidates()   -> the snapshot the AUTO-TRADER opens from

They have diverged five documented times. Every one was silent, and every one let the desk
trade on different numbers than the board displayed:

    the earnings gate      lost its last production caller and inverted to fail-open
    the credit basis       gated on mid while the desk filled at natural
    the ATM IV definition  two definitions, one poisoned by a single bad quote
    the VRP field name     vrp_pp vs vrp, so 30 of 100 edge points read zero on every trade
    the forecast VRP       landed on the engine path only (2026-08-13, by me)

The fifth is the reason this file exists. A sixth, arriving mid-cohort, does not just cause a
bad trade — it splits the 30-trade cohort that the whole validation effort is waiting on, and
it does so invisibly, because both paths keep producing plausible numbers.

These tests do not compare the two implementations line by line. They pin the CONTRACTS that
have actually broken: same field names, same units, same thresholds, same enforcement points.
A divergence in any of those is what "silent" looked like each time.
"""
import inspect

import pytest

import config
import auto_paper_cycle
import main
import vega_candidates
from analysis import assessment


# ── Field-name contracts ──────────────────────────────────────────────────────────────────────

def test_both_paths_emit_vrp_under_the_same_key():
    """The fourth divergence: vega_candidates emitted `vrp_pp` while everything downstream read
    `vrp`, so the largest component of the edge score was silently zero on every trade the
    auto-trader ever opened."""
    assert "vrp" in vega_candidates.VOL_CONTEXT_KEYS
    tech_src = inspect.getsource(__import__("data.technicals", fromlist=["x"]).calculate_all)
    assert '"vrp"' in tech_src


def test_the_vol_context_contract_is_complete_on_every_return_path():
    """A key present only on the happy path is a key callers cannot rely on — and the fast path
    is the one that trades."""
    empty = vega_candidates.vol_context("NOPE", [], 0.0)
    assert set(empty) == set(vega_candidates.VOL_CONTEXT_KEYS)


def test_both_paths_carry_the_forecast_vrp_fields():
    """The fifth divergence, 2026-08-13: the forecast correction landed in technicals (engine)
    while vega_candidates (auto-trader) still computed implied-minus-TRAILING. The board
    reasoned on the corrected number while the robot traded the biased one."""
    for field in ("rv_forecast_pp", "vol_state", "vrp_trailing_pp"):
        assert field in vega_candidates.VOL_CONTEXT_KEYS, f"fast path lost {field}"
    src = inspect.getsource(vega_candidates.vol_context)
    assert "VRP_USE_FORECAST" in src, "the fast path must honour the same switch as the engine"


# ── Threshold contracts: one definition, read by both ─────────────────────────────────────────

def test_neither_path_hardcodes_a_gate_threshold():
    """Every divergence began as a literal in one file that drifted from a constant in another.
    Thresholds live in config; both paths read them from there."""
    for mod in (vega_candidates, auto_paper_cycle):
        src = inspect.getsource(mod)
        assert "REQUIRED_GATES" in src or "config." in src


def test_the_auto_trader_enforces_the_whole_contract_not_a_subset():
    """The enforcement leak: a hand-maintained local tuple omitted `pop`, so the POP floor went
    unenforced on the path that opens trades. It must read REQUIRED_GATES itself."""
    src = inspect.getsource(auto_paper_cycle._candidate_passes_minimum)
    assert "REQUIRED_GATES" in src
    assert "getattr(config" in src


def test_every_required_gate_is_actually_emitted_by_the_builder():
    """A gate the contract requires but the scanner never emits fails CLOSED forever and
    silently empties the board — the failure mode that killed bear call and condor."""
    emitted = set(assessment.evaluate_gates.__doc__ or "")
    gates = assessment.evaluate_gates(
        {"dte": 35, "short_strike": 95.0, "long_strike": 90.0, "side": "put",
         "credit_per_share": 0.50, "width": 5.0, "natural_credit_usd": 50.0,
         "short_delta": -0.20},
        {"ticker": "TEST", "spot": 100.0, "iv_rank": 60.0, "has_earnings": False,
         "earnings_days": None})
    missing = [g for g in config.REQUIRED_GATES if g not in gates]
    assert not missing, f"REQUIRED_GATES names gates the builder never emits: {missing}"


# ── Unit contracts ────────────────────────────────────────────────────────────────────────────

def test_vrp_is_in_vol_points_on_both_paths():
    """IV arrives as a fraction and vol as points. Mixing them produces a hundred-fold error
    that still looks like a plausible number, and it has happened here before."""
    src = inspect.getsource(vega_candidates.vol_context)
    assert "* 100" in src, "fast path must convert IV to vol points like technicals does"


def test_credit_is_gated_on_the_natural_basis_everywhere():
    """The second divergence: gates read the MID credit while the desk filled at NATURAL. GDX
    82/81 opened twice for $9 and $7 against a $19 floor because mid said $31 and $29."""
    src = inspect.getsource(auto_paper_cycle._candidate_passes_minimum)
    assert "credit_usd" not in src or "natural" in src, \
        "the auto-open path must not re-check a MID credit floor"


# ── The cohort contract ───────────────────────────────────────────────────────────────────────

def test_the_cohort_contract_is_declared_and_frozen():
    """A cohort whose definition lives only in someone's head gets redefined by accident. These
    exist so a change to them is a visible diff rather than a silent split."""
    for k in ("COHORT_FROZEN_AT", "COHORT_STRATEGY_LABEL", "COHORT_TARGET_CLOSED_TRADES"):
        assert hasattr(config, k), f"{k} is not declared"
    assert config.COHORT_TARGET_CLOSED_TRADES >= 30


def test_close_logic_is_part_of_the_cohort_key():
    """The correction that changed the sequencing plan: a roll rule or a 21-DTE exit added
    mid-run splits the cohort exactly as surely as moving a gate, because close_logic is in the
    key. The frozen strategy label says this cohort holds to stop or expiry."""
    from analysis import outcome_logger as ol
    key = ol.cohort({"fill_model": "natural", "gate_basis": "natural",
                     "close_logic": "ravens_v1", "opened_at": "2026-08-10T09:38:00"})
    assert key.count("|") == 4, "cohort key must be fill|gate|close|entry_epoch|vendor"
    assert "hold_to_stop_or_expiry" in config.COHORT_STRATEGY_LABEL


def test_entry_rules_are_part_of_the_cohort_key():
    """Same argument as close_logic, applied to the caps added 2026-08-20.

    Those caps decide which trades are ALLOWED TO EXIST, so they select the population — a book
    that may not put five spreads on one expiration is not the same population as one that did.
    Before this was in the key, the first trade opened under the caps would have been counted
    alongside the four opened in a single minute on 2026-08-10, and the cohort would have read
    5 of 30 as though the rules had never changed.
    """
    from analysis import outcome_logger as ol
    base = {"fill_model": "natural", "gate_basis": "natural", "close_logic": "ravens_v1"}
    before = ol.cohort({**base, "opened_at": "2026-08-10T09:38:00"})
    after = ol.cohort({**base, "opened_at": "2026-08-25T09:31:00"})
    assert before != after, (
        "a trade opened under the entry caps must not share a cohort key with one opened "
        "before them")
    assert config.ENTRY_RULES_EPOCH >= config.COHORT_FROZEN_AT


def test_the_pop_gap_gate_is_hard_for_the_robot_and_declared():
    """Eleven gates and none tested the edge. Decision 2026-08-14: hard for the auto-trader,
    advisory for the desk. Frozen for the cohort — flipping it mid-run resplits."""
    assert config.POP_GAP_GATE_AUTO_TRADER is True
    src = inspect.getsource(auto_paper_cycle._candidate_passes_minimum)
    assert "pop_gap" in src, "the gate is declared in config but not enforced"


def test_a_negative_pop_gap_is_refused_by_the_auto_trader():
    gates = {k: True for k in config.REQUIRED_GATES}
    # max_loss_usd under the cap, so this exercises the pop_gap gate rather than the sizing
    # gate added the same week.
    base = {"ticker": "X", "gates": gates, "natural_credit_usd": 50.0, "short_delta": -0.20,
            "max_loss_usd": 80.0, "short_strike": 100.0, "long_strike": 95.0}
    ok = dict(base, true_pop=0.80, pop_implied=0.72)
    bad = dict(base, true_pop=0.66, pop_implied=0.79)
    assert auto_paper_cycle._candidate_passes_minimum(ok) is True
    assert auto_paper_cycle._candidate_passes_minimum(bad) is False


def test_the_chain_quality_floor_is_set_for_good_data_before_it_arrives():
    """The floor decides which tickers are eligible, so it is part of the cohort contract and
    cannot move once the count starts. 0.30 was calibrated to degraded yfinance chains."""
    assert config.CHAIN_QUALITY_MIN_RATIO >= 0.50


# ── Account size must not reach the board ─────────────────────────────────────────────────────

def test_the_board_is_not_gated_on_account_size():
    """VEGA is a discovery tool across all risk levels, not a $500 autotrader. MAX_SPREAD_WIDTH
    was derived from ACCOUNT_BALANCE (5 below $5k, 10 above) and build_candidates refuses any
    pair wider than it — so at $500 a 10-wide spread was never enumerated. Declared directly
    now; RISK_TIERS communicates affordability instead of the board hiding the trade."""
    src = inspect.getsource(config)
    i = src.index("MAX_SPREAD_WIDTH =")
    assert "ACCOUNT_BALANCE" not in src[i:i + 80], "spread width must not derive from balance"
    assert config.MAX_SPREAD_WIDTH >= 10


def test_the_dead_balance_branching_functions_are_gone():
    """Both read as live constraints while gating nothing (select_best_strategy) or gating the
    wrong thing invisibly (get_max_spread_width). One of them caused a false claim about the
    system's own behaviour on 2026-08-14."""
    from analysis import edge_calculator
    assert not hasattr(config, "get_max_spread_width")
    assert not hasattr(edge_calculator, "select_best_strategy")


def test_risk_tiers_carry_affordability_instead_of_the_gate():
    """The mechanism that replaces balance gating: every qualified trade is presented with
    contracts-per-tier so the output serves accounts of any size."""
    assert len(config.RISK_TIERS) >= 3
    assert all("max_risk" in t for t in config.RISK_TIERS)


# ── Regime copy must match the measured evidence ──────────────────────────────────────────────

def test_low_vol_does_not_suppress_or_tell_the_operator_to_stand_aside():
    """Measured 1990-2026 over 9,041 days: the VIX 13-16 band the engine called "too cheap to
    sell" earned +2.95 vol points, was positive 85.5% of the time, and carried a 95.9% win rate
    with a MILDER worst case than the 16-30 bands treated as normal. Every VIX bucket tested is
    positive-expectancy. Low vol is a thinner market, not a wrong one — and the old copy told
    the operator "do not chase setups", which the evidence does not support."""
    reg = main._compute_regime_context({"vix": {"current": 14.5}})
    assert reg["regime_flag"] == "LOW_VOL"
    assert reg["trade_suppressed"] is False, "low vol must not read as Stand Aside"
    note = reg["regime_note"].lower()
    assert "not a stand-aside" in note
    assert "do not chase" not in note


def test_the_low_vol_note_quantifies_the_cost_rather_than_asserting_it():
    """Fewer qualifiers is true and worth saying; 'no edge' was not. The note carries the
    measured difference so the operator can weigh it."""
    note = main._compute_regime_context({"vix": {"current": 14.5}})["regime_note"]
    assert "+2.9" in note and "+4.2" in note, "state the size of the gap, not just its sign"


def test_high_vix_still_reads_as_the_richer_regime():
    """VRP rises monotonically with VIX (+2.43 -> +6.61). Correcting the low-vol copy must not
    flatten that — a low tape genuinely pays less."""
    assert main._compute_regime_context({"vix": {"current": 25.0}})["regime_flag"] != "LOW_VOL"


# ── P0-2: position sizing must actually bind ──────────────────────────────────────────────────

def test_no_account_size_gate_exists_anywhere_in_the_trade_path():
    """VEGA is a SCREENER. It recommends and does not act, and it does not model an account.

    A MAX_RISK_PER_TRADE_USD cap was added 2026-08-16 on a real finding ($4,141 of open risk
    against a $500 balance) and removed 2026-08-17 as the wrong shape. The measurement argument
    is stronger than the scope one: this paper loop is the INSTRUMENT that validates the
    screener, so filtering it by account size makes the cohort test "the small spreads the
    screener liked" rather than what the screener liked — biasing the exact sample the 30-trade
    gate depends on, and excluding wide spreads from the record meant to grade them."""
    # Executable lines only — the comment block explaining WHY the cap was removed names both
    # constants, and that explanation is worth keeping. Asserting on raw source text would
    # force the reasoning to be deleted to satisfy the test.
    src = inspect.getsource(auto_paper_cycle._candidate_passes_minimum)
    code = " ".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "MAX_RISK_PER_TRADE_USD" not in code
    assert "ACCOUNT_BALANCE" not in code


def test_risk_sizing_is_presented_not_enforced():
    """RISK_TIERS shows each trade at several sizes so the operator chooses. Display, not gate."""
    assert len(config.RISK_TIERS) >= 3
    assert all("max_risk" in t for t in config.RISK_TIERS)


def test_spread_width_is_independent_of_account_size():
    """The one place account size genuinely reached the board, fixed 2026-08-14."""
    cfg = inspect.getsource(config)
    i = cfg.index("MAX_SPREAD_WIDTH =")
    assert "ACCOUNT_BALANCE" not in cfg[i:i + 80]
    assert config.MAX_SPREAD_WIDTH >= 10


def test_the_select_contract_still_requires_computable_risk():
    """Removing the CAP does not remove the requirement that risk be KNOWN. A candidate whose
    max loss cannot be computed still cannot be presented as a recommendation — the operator
    needs the number to size against, even though VEGA never sizes for them."""
    from analysis import contracts
    assert ("max_loss_usd", "num0") in contracts.SELECT_CANDIDATE


def test_vendor_is_part_of_the_cohort_key():
    """Added 2026-08-31, the same argument fill_model makes about a different price.

    The 2026-08-27 move to Robinhood was made BECAUSE yfinance's stale, wide quotes collapse
    natural credit (short bid - long ask) toward zero -- that is what invalidated the prior
    64-trade cohort. yfinance is still the live fallback for any ticker Robinhood cannot serve:
    it priced 8 of 56 tickers in a single scan on 2026-08-31, two of them in every scan of the
    day. Without this dimension those trades were indistinguishable in the key from
    broker-priced ones, and the contamination the cohort system exists to exclude walks back in
    one ticker at a time.
    """
    from analysis import outcome_logger as ol
    base = {"fill_model": "natural", "gate_basis": "natural",
            "close_logic": "ravens_v1", "opened_at": "2026-08-28T09:38:00"}
    rh = ol.cohort({**base, "chain_source": "robinhood"})
    yf = ol.cohort({**base, "chain_source": "yfinance"})
    assert rh != yf, "a yfinance-priced trade must not share a cohort with a broker-priced one"


def test_unstamped_records_say_so_rather_than_guessing():
    """Provenance is not derivable from the open date WHILE BOTH VENDORS ARE LIVE -- yfinance is
    the standing fallback for any ticker Robinhood cannot serve, so a row on/after
    ROBINHOOD_TIER1_DATE with no chain_source gets 'unrecorded'. Back-filling that would be
    inventing a measurement.

    Updated 2026-09-03: the same reasoning does NOT extend before that date, because Robinhood
    was not in the fetch chain at all -- see the pre_robinhood test below."""
    from analysis import outcome_logger as ol
    assert ol.entry_vendor_basis({"opened_at": "2026-09-01"}) == "unrecorded"
    assert ol.entry_vendor_basis({"opened_at": "2026-09-01", "chain_source": None}) == "unrecorded"
    assert ol.entry_vendor_basis({"opened_at": "2026-09-01",
                                  "chain_source": "robinhood"}) == "robinhood"
    assert ol.entry_vendor_basis({}) == "unrecorded", "no date at all cannot be derived either"
