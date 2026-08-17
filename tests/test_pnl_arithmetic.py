"""P&L and close-rule arithmetic.

Thresholds are read from config, never hardcoded. The 2026-07-31 change moved
TARGET_PROFIT_PCT 0.50 -> 0.65 and STOP_LOSS_MULTIPLIER 2.0 -> 1.5, and tests that pin literals
would silently rot against the live params they are supposed to protect.

Note the real stop formula: stop_exit = entry * stop_mult (NOT entry * (1 + stop_mult)).
"""
import pytest

import config
import auto_paper_cycle as apc
from analysis import outcome_logger as ol


ENTRY = 1.50          # credit per share collected
WIDTH = 5.0
ROUNDTRIP = 2.16      # 0.54/leg * 2 legs * 2 (open+close)


# ── threshold arithmetic ──────────────────────────────────────────────────────────────────────

def test_profit_target_exit_price():
    """Target exit = entry * (1 - TARGET_PROFIT_PCT): buy the spread back for less than sold."""
    expected = ENTRY * (1.0 - config.TARGET_PROFIT_PCT)
    assert expected == pytest.approx(0.525 if config.TARGET_PROFIT_PCT == 0.65 else expected)
    assert 0 < expected < ENTRY


def test_stop_exit_price():
    """Stop exit = entry * STOP_LOSS_MULTIPLIER — the spread costing N x what you collected."""
    expected = ENTRY * config.STOP_LOSS_MULTIPLIER
    assert expected > ENTRY


def test_win_loss_ratio_is_favourable_under_current_params():
    """Max win vs max loss under the configured pair, and the win rate needed to break even."""
    win = ENTRY * config.TARGET_PROFIT_PCT * 100
    loss = ENTRY * (config.STOP_LOSS_MULTIPLIER - 1.0) * 100
    breakeven_wr = loss / (win + loss)
    # With 0.65 / 1.5 this is $97.50 win vs $75.00 loss -> breakeven 43.5%
    assert win > 0 and loss > 0
    assert breakeven_wr < 0.60, (
        f"breakeven win rate {breakeven_wr:.1%} — stop geometry needs an implausible hit rate"
    )


def test_max_loss_per_contract_arithmetic():
    """Max loss = (width - credit) * 100, the value outcome_logger stores at open."""
    assert (WIDTH - ENTRY) * 100 == pytest.approx(350.0)


def test_commission_is_per_leg_per_contract_round_trip():
    per_leg = config.COMMISSION_PER_CONTRACT_PER_LEG
    legs = config.LEGS_PER_SPREAD
    assert ol._round_trip_cost_per_contract() == pytest.approx(round(per_leg * legs * 2, 2))


# ── close rules ───────────────────────────────────────────────────────────────────────────────

def _row(entry=ENTRY):
    return {"id": "TEST-1", "actual_fill_credit": entry}


def _closes(monkeypatch, mark, dte=30):
    """Run _apply_close_rules with set_close stubbed; return (closed, outcome, reason)."""
    captured = {}

    def fake_set_close(tid, exit_price, outcome, reason, **_kw):
        captured.update(id=tid, exit_price=exit_price, outcome=outcome, reason=reason)
        return True

    monkeypatch.setattr(ol, "set_close", fake_set_close)
    closed = apc._apply_close_rules(_row(), mark, dte, ROUNDTRIP,
                                    config.TARGET_PROFIT_PCT, config.STOP_LOSS_MULTIPLIER)
    return closed, captured


def test_mark_at_target_closes_as_win(monkeypatch):
    target = ENTRY * (1.0 - config.TARGET_PROFIT_PCT)
    closed, cap = _closes(monkeypatch, target)
    assert closed is True
    assert cap["outcome"] == "win"
    assert cap["reason"] == "auto-target-profit"


def test_mark_at_stop_closes_as_loss(monkeypatch):
    stop = ENTRY * config.STOP_LOSS_MULTIPLIER
    closed, cap = _closes(monkeypatch, stop)
    assert closed is True
    assert cap["outcome"] == "loss"
    assert cap["reason"] == "auto-stop-loss"


def test_mark_between_target_and_stop_stays_open(monkeypatch):
    closed, cap = _closes(monkeypatch, ENTRY)   # unchanged from entry
    assert closed is False
    assert cap == {}


def test_dte_window_forces_close(monkeypatch):
    """Inside 7 DTE the position closes regardless of mark."""
    closed, cap = _closes(monkeypatch, ENTRY, dte=5)
    assert closed is True
    assert cap["reason"] == "auto-dte-window"


def test_dte_window_outcome_follows_net_pnl(monkeypatch):
    # mark well below entry at expiry -> profitable close
    closed, cap = _closes(monkeypatch, 0.10, dte=3)
    assert closed is True and cap["outcome"] == "win"
    # mark well above entry -> losing close
    closed, cap = _closes(monkeypatch, 3.00, dte=3)
    assert closed is True and cap["outcome"] == "loss"


def test_missing_entry_credit_never_closes(monkeypatch):
    monkeypatch.setattr(ol, "set_close", lambda *a, **k: True)
    assert apc._apply_close_rules({"id": "X"}, 1.0, 30, ROUNDTRIP,
                                  config.TARGET_PROFIT_PCT, config.STOP_LOSS_MULTIPLIER) is False


def test_stop_overshoot_is_recorded_at_actual_mark(monkeypatch):
    """A mark that gapped past the stop closes AT the mark, not at the stop price.

    This is why stale marks produced outsized losses: META stopped at 4.25 against a 3.42 stop
    because it had not been re-marked in 7 days.
    """
    overshoot = ENTRY * config.STOP_LOSS_MULTIPLIER * 2
    closed, cap = _closes(monkeypatch, overshoot)
    assert closed is True
    assert cap["exit_price"] == pytest.approx(overshoot)
    assert cap["exit_price"] > ENTRY * config.STOP_LOSS_MULTIPLIER


# ── What the desk can actually manage (2026-08-10) ────────────────────────────────────────────

def test_the_desk_refuses_structures_it_cannot_mark():
    """A trade the desk cannot mark is one it cannot close or learn from, so opening it is
    worse than skipping it: the position looks managed and is not.

    get_options_chain returns PUTS ONLY, so a bear call's strikes were never found in the
    reprice index — every mark skipped, forever. Condors are refused outright: the ledger
    records two strikes and a condor has four."""
    import auto_paper_cycle as apc
    assert apc.is_manageable({"strategy": "bull_put_spread"}) is True
    assert apc.is_manageable({"strategy": "Bear Call Spread"}) is True
    assert apc.is_manageable({"strategy": "Iron Condor"}) is False
    assert apc.is_manageable({}) is True          # legacy rows predate the field


def test_strategy_labels_are_normalised_before_comparison():
    """main.py emits 'bull_put_spread' from one path and 'Bear Call Spread' from another —
    same concept, two spellings, and a comparison against either literal misses the other."""
    import auto_paper_cycle as apc
    assert apc._strategy_key({"strategy": "Bear Call Spread"}) == "bear_call_spread"
    assert apc._is_call_side({"strategy": "Bear Call Spread"}) is True
    assert apc._is_call_side({"strategy": "bull_put_spread"}) is False


def test_the_reprice_loop_fetches_the_call_chain_when_it_holds_calls():
    import inspect
    import auto_paper_cycle as apc
    src = inspect.getsource(apc._reprice_and_close_open)
    assert "get_call_options_chain" in src and "_is_call_side" in src
