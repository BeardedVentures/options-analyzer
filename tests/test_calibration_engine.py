"""Calibration engine — analysis/calibration_engine.py.

Every scoring weight in config.py was hand-set from reasoning about how premium selling
should work, and none had ever been checked against a closed trade. This module closes that
loop by testing whether each component actually separated winners from losers.

The most consequential test in this file is test_stop_dominated_losses_are_flagged. On the
real ledger the engine reports a -56.7pp calibration gap, and the obvious reading of that
number is "the model overpredicts, cut the weights". That would be the wrong fix: 45 of 46
losses were stop-outs, and modelled POP is a probability of finishing profitable AT
EXPIRATION — a stopped position never got to answer that question. An engine that reports the
gap without that context actively misleads.
"""
import pytest

import config
from analysis.calibration_engine import (
    _exit_analysis,
    _won,
    get_calibration_proposals,
)


def _trade(**over):
    t = {"status": "closed", "outcome": "win", "modeled_pop": 0.78,
         "iv_rank": 60.0, "edge_score": 75, "vrp": 5.0, "technical_score": 70,
         "exit_reason": "auto-target-profit", "vix_at_entry": 15.0}
    t.update(over)
    return t


def _many(n, **over):
    return [_trade(**over) for _ in range(n)]


# ── Outcome mapping ───────────────────────────────────────────────────────────────────────────

def test_outcome_maps_win_loss_and_excludes_scratch():
    """The ledger stores win/loss/scratch, not a `won` boolean. Counting a scratch either way
    would bias the win rate."""
    assert _won(_trade(outcome="win")) is True
    assert _won(_trade(outcome="loss")) is False
    assert _won(_trade(outcome="scratch")) is None
    assert _won({}) is None


def test_scratches_are_excluded_from_the_sample():
    trades = _many(20, outcome="win") + _many(20, outcome="loss") + _many(10, outcome="scratch")
    assert get_calibration_proposals(trades)["sample_size"] == 40


def test_open_positions_are_not_counted():
    trades = _many(35) + [_trade(status="open", outcome=None) for _ in range(5)]
    assert get_calibration_proposals(trades)["sample_size"] == 35


# ── Activation gate ───────────────────────────────────────────────────────────────────────────

def test_small_sample_proposes_nothing():
    """Small samples produce confident nonsense; the gate exists to prevent it."""
    r = get_calibration_proposals(_many(15))
    assert r["sufficient"] is False
    assert r["proposals"] == []
    assert "needed" in r["note"]


def test_gate_is_config_driven(monkeypatch):
    monkeypatch.setattr(config, "CALIBRATION_MIN_TRADES", 10, raising=False)
    assert get_calibration_proposals(_many(15))["sufficient"] is True


def test_empty_input_survives():
    r = get_calibration_proposals([])
    assert r["sample_size"] == 0 and r["sufficient"] is False


# ── Calibration gap ───────────────────────────────────────────────────────────────────────────

def test_calibration_gap_is_realised_minus_modelled():
    trades = ([_trade(outcome="win", modeled_pop=0.78)] * 25 +
              [_trade(outcome="loss", modeled_pop=0.78)] * 5)
    r = get_calibration_proposals(trades)
    assert r["overall_win_rate"] == pytest.approx(83.3, abs=0.2)
    assert r["modeled_pop_avg"] == pytest.approx(78.0, abs=0.2)
    assert r["calibration_gap_pts"] == pytest.approx(5.3, abs=0.2)


def test_negative_gap_when_the_engine_loses_more_than_it_predicted():
    trades = ([_trade(outcome="loss", modeled_pop=0.78)] * 25 +
              [_trade(outcome="win", modeled_pop=0.78)] * 5)
    assert get_calibration_proposals(trades)["calibration_gap_pts"] < 0


# ── Component predictiveness ──────────────────────────────────────────────────────────────────

def _flat_component(n=45, component="technical_score", per_tercile_wins=10):
    """n trades whose component spans a range but whose win rate is IDENTICAL in every
    tercile — the definition of a component that is not discriminating.

    Built explicitly rather than with a modulo pattern: `i % 10 < 7` looks flat but lands
    80/60/80% across terciles of 15, a 20pp spread that is genuinely predictive. The fixture
    has to be flat by construction or the test proves nothing.
    """
    size = n // 3
    trades = []
    for t_idx in range(3):
        for pos in range(size):
            score = t_idx * size + pos          # sorts into its own tercile
            trades.append(_trade(**{component: score,
                                    "outcome": "win" if pos < per_tercile_wins else "loss"}))
    return trades


def test_non_predictive_component_generates_a_proposal():
    """Win rate identical across terciles means the component is not earning its weight."""
    r = get_calibration_proposals(_flat_component())
    props = [p for p in r["proposals"] if p["component"] == "technical_score"]
    assert len(props) == 1
    assert props[0]["proposed_weight"] < props[0]["current_weight"]
    assert "not separating winners" in props[0]["rationale"]
    assert props[0]["supporting_data"]["spread_pp"] == pytest.approx(0.0, abs=0.1)


def test_predictive_component_generates_no_proposal():
    """High scores winning and low scores losing is the component doing its job."""
    trades = ([_trade(technical_score=90, outcome="win")] * 20 +
              [_trade(technical_score=10, outcome="loss")] * 20)
    r = get_calibration_proposals(trades)
    assert [p for p in r["proposals"] if p["component"] == "technical_score"] == []


def test_component_absent_from_the_ledger_is_reported_not_skipped():
    """The real blocker: edge_score / vrp / technical_score were never recorded at entry, so
    on 58 closed trades not one score component was testable. Silence would read as "all
    components are fine"."""
    trades = _many(40, edge_score=None, vrp=None, technical_score=None)
    r = get_calibration_proposals(trades)
    names = {u["component"] for u in r["untestable"]}
    assert {"edge_score", "vrp", "technical_score"} <= names
    for u in r["untestable"]:
        assert u["testable"] is False and "needed" in u["reason"]


def test_proposal_confidence_scales_with_sample_size():
    sc = [p for p in get_calibration_proposals(_flat_component(30, per_tercile_wins=7))
          ["proposals"] if p["component"] == "technical_score"]
    bc = [p for p in get_calibration_proposals(_flat_component(60, per_tercile_wins=14))
          ["proposals"] if p["component"] == "technical_score"]
    assert sc and bc, "fixture stopped producing proposals"
    assert sc[0]["confidence"] == "medium"
    assert bc[0]["confidence"] == "high"


def test_weights_are_never_proposed_below_a_floor(monkeypatch):
    monkeypatch.setattr(config, "TECHNICAL_WEIGHT", 6, raising=False)
    props = get_calibration_proposals(_flat_component())["proposals"]
    assert props, "fixture stopped producing proposals"
    for p in props:
        assert p["proposed_weight"] >= 5


# ── The finding that matters most ─────────────────────────────────────────────────────────────

def test_stop_dominated_losses_are_flagged():
    """On the real ledger: 45 of 46 losses were stop-outs and the gap read -56.7pp. The
    obvious reading — "the model overpredicts, cut the weights" — is the wrong fix. Modelled
    POP is the chance of finishing profitable AT EXPIRATION, and a stopped position never got
    to answer that."""
    trades = ([_trade(outcome="loss", exit_reason="auto-stop-loss")] * 45 +
              [_trade(outcome="win", exit_reason="auto-target-profit")] * 12)
    ea = get_calibration_proposals(trades)["exit_analysis"]
    assert ea["stop_out_share"] == pytest.approx(1.0, abs=0.05)
    assert ea["verdict"] is not None
    assert "exit rule" in ea["verdict"]
    assert "STOP_LOSS_MULTIPLIER" in ea["verdict"]


def test_expiration_losses_do_not_trigger_the_exit_warning():
    """When losses really are expiration losses, the gap IS about selection and the warning
    would be noise."""
    trades = ([_trade(outcome="loss", exit_reason="auto-dte-window")] * 20 +
              [_trade(outcome="win", exit_reason="auto-target-profit")] * 20)
    assert get_calibration_proposals(trades)["exit_analysis"]["verdict"] is None


def test_exit_analysis_survives_a_board_with_no_losses():
    ea = _exit_analysis(_many(10, outcome="win"))
    assert ea["loss_count"] == 0 and ea["verdict"] is None


# ── Regime split ──────────────────────────────────────────────────────────────────────────────

def test_regime_split_separates_high_and_low_vol():
    trades = _many(20, vix_at_entry=28.0) + _many(20, vix_at_entry=13.0)
    splits = get_calibration_proposals(trades)["regime"]["splits"]
    assert splits["high_vol"]["n"] == 20 and splits["low_vol"]["n"] == 20


def test_regime_alert_fires_on_a_wide_divergence():
    trades = (_many(15, vix_at_entry=28.0, outcome="win") +
              _many(15, vix_at_entry=13.0, outcome="loss"))
    assert get_calibration_proposals(trades)["regime"]["alert"] is not None


def test_no_regime_alert_without_enough_trades_per_bucket():
    trades = (_many(3, vix_at_entry=28.0, outcome="win") +
              _many(30, vix_at_entry=13.0, outcome="loss"))
    assert get_calibration_proposals(trades)["regime"]["alert"] is None


def test_missing_vix_is_bucketed_as_unknown_not_guessed():
    trades = _many(35, vix_at_entry=None)
    assert get_calibration_proposals(trades)["regime"]["splits"]["unknown"]["n"] == 35


# ── The non-negotiable ────────────────────────────────────────────────────────────────────────

def test_the_engine_never_writes_to_config():
    """Scoring weights are the engine's beliefs about edge. A bug in the calibration engine
    must never be able to rewrite them — proposals are applied by hand."""
    import inspect

    from analysis import calibration_engine
    src = inspect.getsource(calibration_engine)
    assert "open(" not in src.replace("# ", "")
    assert "setattr(config" not in src
    assert "config.py" not in src.split('"""')[2] if src.count('"""') > 2 else True
