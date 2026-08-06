#!/usr/bin/env python3
"""
odin.py — the synthesis. Listens to both ravens and returns a recommendation.

Recommends; never closes. auto_paper_cycle decides what to do with the recommendation, and
anything short of a clear close surfaces for human review rather than acting.

The tier that matters is the third. When Thought says the story is broken and Memory says
situations like this have recovered, the system must NOT quietly close — it must say out loud
that its two halves disagree. That divergence is the whole reason the framework exists.
"""
from __future__ import annotations

from typing import Dict

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _cfg(name, default):
    return getattr(config, name, default)


def synthesize(huginn_out: Dict, muninn_out: Dict, trade: Dict) -> Dict:
    """Returns {recommendation, confidence, plain_english, huginn_status,
    muninn_probability, raven_agreement}."""
    h = huginn_out.get("thesis_status", "UNKNOWN")
    prob = muninn_out.get("recovery_probability")
    suff = muninn_out.get("sufficient", False)
    thr = float(_cfg("ODIN_RECOVERY_THRESHOLD", 0.35))

    def out(rec, conf, txt, agree):
        return {"recommendation": rec, "confidence": conf, "plain_english": txt,
                "huginn_status": h, "muninn_probability": prob,
                "muninn_confidence": muninn_out.get("confidence"),
                "raven_agreement": agree}

    # Tier 1 — the wolves. Nothing overrides them.
    if h == "WOLF":
        return out("WOLF_CLOSE", "high",
                   huginn_out.get("reason") or "Hard-floor condition triggered.", True)

    # Thesis holding — theta does the work.
    if h in ("INTACT", "EXCEEDED"):
        return out("HOLD", "high",
                   f"Thesis {h.lower()}. {huginn_out.get('reason', '')}".strip(), True)

    if h == "VIOLATED":
        if not suff:
            # Memory silent on a broken thesis is the most dangerous cell in the matrix: the
            # only evidence available says close, and there is nothing to weigh against it.
            return out("MUNINN_BLIND", "low",
                       f"Thesis violated — {huginn_out.get('reason', '')} "
                       f"{muninn_out.get('reason', '')} No historical base rate to weigh "
                       f"against it; this is a human decision.".strip(), False)
        if prob is not None and prob < thr:
            return out("CLOSE", "high",
                       f"Thesis violated and memory agrees: only {prob*100:.0f}% of "
                       f"comparable situations recovered. {huginn_out.get('reason', '')}".strip(),
                       True)
        # Tier 3 — the divergence. This is the QQQ case.
        return out("HOLD_TENSION", "medium",
                   f"Thought says the structure is broken ({huginn_out.get('reason', '')}) but "
                   f"memory says {prob*100:.0f}% of situations like this recovered "
                   f"({muninn_out.get('comparable_count')} comparable). The ravens disagree — "
                   f"hold and watch, or close deliberately.".strip(), False)

    if h == "UNDER_PRESSURE":
        if not suff:
            return out("MUNINN_BLIND", "low",
                       f"Under pressure — {huginn_out.get('reason', '')} "
                       f"{muninn_out.get('reason', '')} Monitor manually.".strip(), False)
        return out("HOLD", "medium",
                   f"Under pressure but not violated. Memory shows {prob*100:.0f}% recovery in "
                   f"comparable situations. {huginn_out.get('reason', '')}".strip(), True)

    return out("HOLD", "low", "Signals unclear; no action taken.", False)
