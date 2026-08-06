#!/usr/bin/env python3
"""
calibration_engine.py — tests VEGA's scoring beliefs against VEGA's own results.

Every weight in config.py was hand-set from reasoning about how premium selling should work.
The reasoning is defensible; none of it has ever been checked against a closed trade. This
module closes that loop: it reads the outcome ledger, asks whether each score component
actually separated winners from losers, and PROPOSES adjustments where it did not.

It never edits config.py. Proposals are surfaced for human approval and applied by hand.
Scoring weights are the engine's beliefs about edge, and a bug here must never be able to
silently rewrite them.

Two things the original brief assumed that are not true of this codebase, and which change
what the module can honestly do:

  1. Outcomes live in logs/vega_outcomes.jsonl, not a Supabase `vega_trade_outcomes` table.
     The Supabase read and the FastAPI endpoint belong to the JARVIS repo, which is not this
     one. This reads the local ledger.

  2. The ledger records `outcome` ("win"/"loss"/"scratch"), `modeled_pop` and
     `max_loss_per_contract` — not `won`, `true_pop`, `max_loss_usd`. More importantly it did
     not record edge_score, vrp or technical_score AT ALL until 2026-08-05, so on the 58
     trades closed before then not one score component is testable. Components are tested
     only where the data exists, and what could not be tested is reported explicitly rather
     than silently skipped.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _cfg(name: str, default):
    return getattr(config, name, default)


# Components worth testing, with the config weight each currently carries.
_COMPONENTS = [
    ("edge_score", "EDGE_SCORE_WEIGHT", 100),
    ("vrp", "VRP_WEIGHT", 30),
    ("technical_score", "TECHNICAL_WEIGHT", 20),
    ("iv_rank", "IV_RANK_WEIGHT", 20),
    ("modeled_pop", "TRUE_POP_WEIGHT", 25),
]


def _won(t: Dict) -> Optional[bool]:
    """The ledger stores outcome as win/loss/scratch. A scratch is neither — counting it
    either way would bias the win rate, so it is excluded from discrimination tests."""
    o = (t.get("outcome") or "").strip().lower()
    if o == "win":
        return True
    if o == "loss":
        return False
    return None


def _closed(trades: Sequence[Dict]) -> List[Dict]:
    return [t for t in (trades or [])
            if (t.get("status") == "closed" or t.get("closed_at")) and _won(t) is not None]


def _test_component_predictiveness(trades: Sequence[Dict], component: str,
                                   current_weight: int) -> Optional[Dict]:
    """Split on the component into terciles and compare win rates.

    If the top third does not beat the bottom third by a meaningful margin, the component is
    not separating winners from losers and is probably carrying more weight than it earns.
    """
    min_n = int(_cfg("CALIBRATION_MIN_COMPONENT_N", 20))
    vals = [(float(t[component]), _won(t)) for t in trades
            if t.get(component) is not None]
    if len(vals) < min_n:
        return {"component": component, "testable": False, "n": len(vals),
                "reason": f"only {len(vals)} closed trades carry {component} "
                          f"({min_n} needed)"}

    vals.sort(key=lambda x: x[0])
    n3 = len(vals) // 3
    if n3 < 2:
        return {"component": component, "testable": False, "n": len(vals),
                "reason": "too few trades to form terciles"}
    terciles = [vals[:n3], vals[n3:2 * n3], vals[2 * n3:]]
    win_rates = [sum(1 for _, w in t if w) / len(t) for t in terciles]
    spread = max(win_rates) - min(win_rates)
    flat = float(_cfg("CALIBRATION_FLAT_SPREAD", 0.08))

    if spread >= flat:
        return None  # discriminating; no proposal

    proposed = max(5, current_weight - 5)
    return {
        "component": component,
        "testable": True,
        "current_weight": current_weight,
        "proposed_weight": proposed,
        "rationale": (
            f"{component} tercile win rates are "
            f"{[round(r * 100, 1) for r in win_rates]}% — a spread of only "
            f"{round(spread * 100, 1)}pp between the weakest and strongest third. "
            f"The component is not separating winners from losers, so the "
            f"{current_weight} points it carries are not earned. Suggest {proposed}."
        ),
        "supporting_data": {
            "tercile_win_rates": [round(r, 4) for r in win_rates],
            "spread_pp": round(spread * 100, 1),
            "n_trades": len(vals),
        },
        "confidence": "high" if len(vals) >= 40 else "medium",
    }


def _regime_splits(trades: Sequence[Dict]) -> Dict:
    """Win rate in high-vol versus low-vol tape. A wide divergence means one set of
    thresholds is being asked to serve two different markets."""
    cut = float(_cfg("CALIBRATION_REGIME_VIX", 20.0))
    buckets: Dict[str, List[Dict]] = {"high_vol": [], "low_vol": [], "unknown": []}
    for t in trades:
        v = t.get("vix_at_entry")
        key = "unknown" if v is None else ("high_vol" if float(v) > cut else "low_vol")
        buckets[key].append(t)

    out = {}
    for key, rows in buckets.items():
        if not rows:
            continue
        wins = [t for t in rows if _won(t)]
        scores = [float(t["edge_score"]) for t in rows if t.get("edge_score") is not None]
        out[key] = {
            "n": len(rows),
            "win_rate": round(len(wins) / len(rows) * 100, 1),
            "avg_edge_score": round(sum(scores) / len(scores), 1) if scores else None,
        }

    alert = None
    hi, lo = out.get("high_vol"), out.get("low_vol")
    gap_pp = float(_cfg("CALIBRATION_REGIME_ALERT_PP", 10.0))
    if hi and lo and hi["n"] >= 10 and lo["n"] >= 10:
        gap = abs(hi["win_rate"] - lo["win_rate"])
        if gap >= gap_pp:
            better = "high-vol" if hi["win_rate"] > lo["win_rate"] else "low-vol"
            alert = (f"Win rate differs by {gap:.1f}pp across regimes "
                     f"({better} is stronger). One set of thresholds may be serving two "
                     f"different markets.")
    return {"splits": out, "alert": alert}


def _exit_analysis(trades: Sequence[Dict]) -> Dict:
    """Why the losses happened — and whether the calibration gap is even a scoring problem.

    This exists because the naive reading of a large negative gap is "the model overpredicts,
    cut the weights", and on this ledger that would be exactly the wrong fix. Modelled POP is
    the probability of finishing profitable AT EXPIRATION. A position closed early at a stop
    never gets to find out whether that was right. If stop-outs dominate the losses, the gap
    is measuring the exit rule, not the selection logic, and no amount of reweighting the
    score will close it.
    """
    losses = [t for t in trades if _won(t) is False]
    if not losses:
        return {"loss_count": 0, "stop_out_share": None, "verdict": None}

    stops = [t for t in losses
             if "stop" in (t.get("exit_reason") or "").lower()]
    share = len(stops) / len(losses)
    reasons: Dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason") or "unknown"
        reasons[r] = reasons.get(r, 0) + 1

    verdict = None
    if share >= float(_cfg("CALIBRATION_STOP_DOMINANCE", 0.70)):
        verdict = (
            f"{len(stops)} of {len(losses)} losses ({share*100:.0f}%) are stop-outs, not "
            f"expiration losses. Modelled POP measures the chance of finishing profitable at "
            f"EXPIRATION, and these positions were closed before that question was answered "
            f"— so the calibration gap below is measuring the exit rule, not the selection "
            f"logic. Reweighting score components will not close it. Look at "
            f"STOP_LOSS_MULTIPLIER (currently "
            f"{_cfg('STOP_LOSS_MULTIPLIER', 2.0)}x credit) before touching any weight."
        )
    return {
        "loss_count": len(losses),
        "stop_out_count": len(stops),
        "stop_out_share": round(share, 3),
        "exit_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "verdict": verdict,
    }


def get_calibration_proposals(closed_trades: Sequence[Dict]) -> Dict:
    """Analyse closed outcomes against the components that predicted them.

    Returns sample size, the gap between realised win rate and modelled POP, per-component
    proposals, what could not be tested and why, and the regime split.
    """
    trades = _closed(closed_trades)
    n = len(trades)
    min_n = int(_cfg("CALIBRATION_MIN_TRADES", 30))

    wins = [t for t in trades if _won(t)]
    win_rate = (len(wins) / n) if n else 0.0
    pops = [float(t["modeled_pop"]) for t in trades if t.get("modeled_pop") is not None]
    modeled = (sum(pops) / len(pops)) if pops else 0.0

    result = {
        "sample_size": n,
        "sufficient": n >= min_n,
        "min_required": min_n,
        "overall_win_rate": round(win_rate * 100, 1),
        "modeled_pop_avg": round(modeled * 100, 1),
        # Positive means the engine is winning more than it predicted (under-confident);
        # negative means it is losing more than it predicted, which is the dangerous side.
        "calibration_gap_pts": round((win_rate - modeled) * 100, 1) if pops else None,
        "proposals": [],
        "untestable": [],
        "regime": _regime_splits(trades),
        "exit_analysis": _exit_analysis(trades),
    }
    if not result["sufficient"]:
        result["note"] = (f"{n} closed trades — {min_n} needed before proposing any weight "
                          f"change. Small samples produce confident nonsense.")
        return result

    for comp, weight_key, default_weight in _COMPONENTS:
        weight = int(_cfg(weight_key, default_weight))
        r = _test_component_predictiveness(trades, comp, weight)
        if r is None:
            continue
        if r.get("testable"):
            result["proposals"].append(r)
        else:
            result["untestable"].append(r)
    return result


def load_and_analyse() -> Dict:
    """Convenience entry point: read the local ledger and analyse it."""
    if not _cfg("CALIBRATION_ENGINE_ENABLED", True):
        return {"sample_size": 0, "sufficient": False, "proposals": [],
                "note": "calibration engine disabled in config"}
    try:
        from analysis import outcome_logger as ol
        return get_calibration_proposals(ol.load_records())
    except Exception as e:  # pragma: no cover - defensive
        return {"sample_size": 0, "sufficient": False, "proposals": [],
                "note": f"could not read the outcome ledger: {e}"}


if __name__ == "__main__":
    import json
    print(json.dumps(load_and_analyse(), indent=2, default=str))
