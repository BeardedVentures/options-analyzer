#!/usr/bin/env python3
"""
muninn.py — Memory. Pattern-matches an open position under stress against every comparable
situation the ledger has ever recorded, and reports how often those recovered.

Muninn does not decide. It reports a base rate. Odin decides.

WHAT MEMORY ACTUALLY HAS, as of 2026-08-05
------------------------------------------
The brief's similarity function reads `dte_at_stress_check`, `price_to_strike_bucket` and
`support_status_at_stress` off historical records. None of those fields has ever been
written. Worse, they cannot be reconstructed: they describe the position AT THE MOMENT OF
STRESS, and nothing has ever snapshotted that moment. The close record knows the trade was
stopped; it does not know what the chart looked like when it happened.

So Muninn starts blind, by construction, and stays blind until stress snapshots accumulate.
That is the honest state and it is reported as such — `sufficient: False` with the reason.
The one thing that must never happen here is inventing a probability from thin data, because
the entire purpose of this raven is to be the sober counterweight to Huginn's present-moment
alarm. A fabricated base rate would be worse than silence: it would launder a guess into a
number that looks like evidence.

record_stress_snapshot() writes the missing observation, so the ledger can start earning the
memory it does not yet have.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _cfg(name, default):
    return getattr(config, name, default)


def price_to_strike_bucket(price: Optional[float], short_strike: Optional[float],
                           atr: Optional[float] = None) -> Optional[str]:
    """Where price sits relative to the short strike, in ATR terms when available."""
    if price is None or not short_strike:
        return None
    d = float(price) - float(short_strike)
    if atr:
        u = d / atr
        if u < -1:
            return "deep_below"
        if u < 0:
            return "below"
        if u < 1:
            return "near"
        return "clear"
    return "below" if d < 0 else "clear"


def vix_bucket(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    v = float(v)
    return "high" if v > 25 else ("mid" if v > 15 else "low")


def record_stress_snapshot(trade: Dict, huginn_out: Dict, data: Dict) -> Dict:
    """The observation Muninn will need later: what this position looked like the first time
    it came under pressure. Appended to the trade's `stress_snapshots`, not overwritten — the
    first time a position is stressed is a different fact from the fifth."""
    sup = (huginn_out.get("signal_readings") or {}).get("support", {})
    buf = (huginn_out.get("signal_readings") or {}).get("strike_buffer", {})
    return {
        "at": data.get("as_of"),
        "dte_remaining": data.get("dte_remaining"),
        "thesis_status": huginn_out.get("thesis_status"),
        "support_status_at_stress": sup.get("status"),
        "price_to_strike_bucket": price_to_strike_bucket(
            data.get("current_price"), trade.get("short_strike"), buf.get("atr")),
        "atr_buffer": buf.get("atr_buffer"),
        "vix_at_stress": data.get("vix"),
        "mark": data.get("mark"),
    }


def _first_stress(t: Dict) -> Dict:
    snaps = t.get("stress_snapshots") or []
    return snaps[0] if snaps else {}


def similarity(open_pos: Dict, hist: Dict, huginn_out: Dict, data: Dict) -> float:
    """0-1. Only dimensions present on BOTH sides contribute, and the score is normalised by
    the weight actually used — so a match on one dimension out of four does not masquerade as
    a strong match."""
    snap = _first_stress(hist)
    score = weights = 0.0

    dte_o, dte_h = data.get("dte_remaining"), snap.get("dte_remaining")
    if dte_o is not None and dte_h is not None:
        score += max(0.0, 1.0 - abs(dte_o - dte_h) / 15.0) * 0.30
        weights += 0.30

    buf = (huginn_out.get("signal_readings") or {}).get("strike_buffer", {})
    p_o = price_to_strike_bucket(data.get("current_price"), open_pos.get("short_strike"),
                                 buf.get("atr"))
    p_h = snap.get("price_to_strike_bucket")
    if p_o and p_h:
        score += (1.0 if p_o == p_h else 0.3) * 0.30
        weights += 0.30

    s_o = (huginn_out.get("signal_readings") or {}).get("support", {}).get("status")
    s_h = snap.get("support_status_at_stress")
    if s_o and s_h:
        score += (1.0 if s_o == s_h else 0.2) * 0.25
        weights += 0.25

    v_o = vix_bucket(data.get("vix") or open_pos.get("vix_at_entry"))
    v_h = vix_bucket(snap.get("vix_at_stress") or hist.get("vix_at_entry"))
    if v_o and v_h:
        score += (1.0 if v_o == v_h else 0.0) * 0.15
        weights += 0.15

    return (score / weights) if weights > 0 else 0.0


def compute_recovery_probability(open_pos: Dict, huginn_out: Dict,
                                 all_closed: Sequence[Dict], data: Dict) -> Dict:
    """How often did comparable stressed positions end up winning?

    Only natural-fill trades are compared. Mid-fill trades were entered at a price that could
    not be achieved and are a different population; pooling them would import a systematic
    bias into the base rate.
    """
    min_n = int(_cfg("MUNINN_MIN_COMPARABLE", 5))
    min_sim = float(_cfg("MUNINN_MIN_SIMILARITY", 0.45))

    pool = [t for t in (all_closed or [])
            if (t.get("status") == "closed" or t.get("closed_at"))
            and (t.get("fill_model") or "mid") == "natural"
            and t.get("stress_snapshots")
            and (t.get("outcome") or "").lower() in ("win", "loss")]

    if not pool:
        return {"sufficient": False, "recovery_probability": None,
                "confidence": "insufficient_data", "comparable_count": 0,
                "reason": ("No closed trade carries a stress snapshot yet. Memory cannot "
                           "speak to this situation — the observation it would need was "
                           "never recorded, and cannot be reconstructed after the fact.")}

    scored = [(similarity(open_pos, t, huginn_out, data), t) for t in pool]
    matches = [(s, t) for s, t in scored if s >= min_sim]
    matches.sort(key=lambda x: -x[0])

    if len(matches) < min_n:
        return {"sufficient": False, "recovery_probability": None,
                "confidence": "insufficient_data", "comparable_count": len(matches),
                "reason": (f"Only {len(matches)} comparable situations on record "
                           f"({min_n} needed). Memory is not yet deep enough here.")}

    top = matches[:max(min_n, min(len(matches), 25))]
    wins = sum(1 for _, t in top if (t.get("outcome") or "").lower() == "win")
    prob = wins / len(top)
    avg_sim = sum(s for s, _ in top) / len(top)
    conf = ("high" if len(top) >= 20 and avg_sim >= 0.7 else
            "medium" if len(top) >= 10 else "low")
    return {
        "sufficient": True,
        "recovery_probability": round(prob, 3),
        "confidence": conf,
        "comparable_count": len(top),
        "avg_similarity": round(avg_sim, 3),
        "reason": (f"{wins} of {len(top)} comparable stressed positions recovered "
                   f"(avg similarity {avg_sim:.2f})."),
    }
