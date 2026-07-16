#!/usr/bin/env python3
"""
strategies.py — per-strategy selection criteria + news validation for VEGA.

Single source of truth for WHAT makes each strategy a valid recommendation. Each strategy has a
"thesis" (the directional/vol view it expresses) and a set of fitted gates. Crucially, every
recommendation is validated against news sentiment BEFORE it is surfaced: a thesis that conflicts
with the tape (e.g. selling puts into blocking/negative news, or selling calls into a bullish pop)
is blocked or flagged. This module is pure (no IO) so it is fully unit-testable and is used by both
the live engine (main.py, on the tower) and the demo seeder (seed_demo.py).

Only DEFINED-RISK income structures + a clearly-separated speculative long call. No undefined risk.
"""
from __future__ import annotations
from typing import Dict, List, Tuple

# News sentiment vocabulary (matches data/news.py): POSITIVE, NEUTRAL, NEGATIVE, BLOCKING
STRATEGY_SPECS: Dict[str, Dict] = {
    "bull_put": {
        "label": "Bull Put Spread", "thesis": "bullish / neutral (price stays above the short put)",
        "dte": (25, 45), "short_delta": (0.16, 0.30), "min_credit_to_width": 0.20,
        "iv_rank_min": 35, "trend_ok": {"up", "flat"}, "min_pop": 0.70,
        # selling downside premium: negative/blocking news is the enemy; a bullish tape is ideal
        "news_block": {"BLOCKING", "NEGATIVE"}, "news_ideal": {"POSITIVE", "NEUTRAL"},
    },
    "bear_call": {
        "label": "Bear Call Spread", "thesis": "bearish / neutral (price stays below the short call)",
        "dte": (25, 45), "short_delta": (0.16, 0.30), "min_credit_to_width": 0.20,
        "iv_rank_min": 35, "trend_ok": {"down", "flat"}, "min_pop": 0.70,
        # selling upside premium: a bullish pop / blocking news is the enemy
        "news_block": {"BLOCKING", "POSITIVE"}, "news_ideal": {"NEGATIVE", "NEUTRAL"},
    },
    "iron_condor": {
        "label": "Iron Condor", "thesis": "neutral / range-bound (price stays between the shorts)",
        "dte": (25, 45), "short_delta": (0.12, 0.22), "min_credit_to_width": 0.22,
        "iv_rank_min": 45, "trend_ok": {"flat"}, "min_pop": 0.65,
        # any strong directional catalyst breaks a range trade
        "news_block": {"BLOCKING", "NEGATIVE", "POSITIVE"}, "news_ideal": {"NEUTRAL"},
    },
    "long_call_lottery": {
        "label": "Long Call (lottery)", "thesis": "speculative bullish momentum / reversal bounce",
        "dte": (20, 45), "target_delta": (0.25, 0.42), "iv_rank_max": 85,
        "trend_ok": {"up", "flat", "down"}, "max_budget": 400,
        # a bullish swing needs the tape not fighting it
        "news_block": {"BLOCKING", "NEGATIVE"}, "news_ideal": {"POSITIVE", "NEUTRAL"},
    },
}


def validate_news(strategy: str, sentiment: str) -> Dict:
    """Validate the strategy's thesis against news sentiment. Returns
    {ok, verdict, detail}. ok=False means the recommendation should be blocked."""
    spec = STRATEGY_SPECS.get(strategy, {})
    s = (sentiment or "NEUTRAL").upper()
    block = spec.get("news_block", set())
    ideal = spec.get("news_ideal", set())
    if s in block:
        return {"ok": False, "verdict": "BLOCKED",
                "detail": f"News '{s}' conflicts with a {spec.get('thesis','')} thesis"}
    if s in ideal:
        return {"ok": True, "verdict": "CONFIRMS",
                "detail": f"News '{s}' supports the thesis"}
    return {"ok": True, "verdict": "NEUTRAL", "detail": f"News '{s}' — no conflict"}


def _chk(label: str, ok: bool, detail: str = "") -> Dict:
    return {"label": label, "ok": bool(ok), "detail": detail}


def evaluate(strategy: str, ctx: Dict) -> Dict:
    """Run the fitted criteria + news validation for a candidate.
    ctx keys (best-effort): dte, short_delta, credit_to_width, iv_rank, trend, pop, sentiment,
        premium_usd (lottery), delta (lottery).
    Returns {qualified: bool, criteria: [ {label, ok, detail} ], news_check: {...}, spec_label}.
    A recommendation qualifies only if ALL hard criteria pass AND news is not blocking.
    """
    spec = STRATEGY_SPECS.get(strategy)
    if not spec:
        return {"qualified": False, "criteria": [_chk("unknown strategy", False)], "news_check": {}, "spec_label": strategy}
    crit: List[Dict] = []

    dte = ctx.get("dte")
    if dte is not None:
        lo, hi = spec["dte"]
        crit.append(_chk(f"DTE {lo}-{hi}", lo <= dte <= hi, f"{dte}d"))

    trend = (ctx.get("trend") or "").lower()
    if trend:
        crit.append(_chk("Regime fits thesis", trend in spec["trend_ok"], f"trend {trend}"))

    iv = ctx.get("iv_rank")
    if iv is not None:
        if "iv_rank_min" in spec:
            crit.append(_chk(f"IV rank ≥ {spec['iv_rank_min']}", iv >= spec["iv_rank_min"], f"IV rank {iv:.0f}"))
        if "iv_rank_max" in spec:
            crit.append(_chk(f"IV rank ≤ {spec['iv_rank_max']}", iv <= spec["iv_rank_max"], f"IV rank {iv:.0f}"))

    if strategy == "long_call_lottery":
        d = ctx.get("delta")
        if d is not None:
            lo, hi = spec["target_delta"]
            crit.append(_chk(f"Delta {lo}-{hi}", lo <= abs(d) <= hi, f"Δ {d:.2f}"))
        prem = ctx.get("premium_usd")
        if prem is not None:
            crit.append(_chk(f"Cost ≤ ${spec['max_budget']}", prem <= spec["max_budget"], f"${prem:.0f}"))
    else:
        sd = ctx.get("short_delta")
        if sd is not None:
            lo, hi = spec["short_delta"]
            crit.append(_chk(f"Short delta {lo}-{hi}", lo <= abs(sd) <= hi, f"Δ {abs(sd):.2f}"))
        ctw = ctx.get("credit_to_width")
        if ctw is not None:
            crit.append(_chk(f"Credit/width ≥ {spec['min_credit_to_width']:.0%}",
                             ctw >= spec["min_credit_to_width"], f"{ctw:.0%}"))
        pop = ctx.get("pop")
        if pop is not None:
            crit.append(_chk(f"POP ≥ {spec['min_pop']:.0%}", pop >= spec["min_pop"], f"{pop:.0%}"))

    news = validate_news(strategy, ctx.get("sentiment"))
    crit.append(_chk("News validates thesis", news["ok"], news["detail"]))

    qualified = all(c["ok"] for c in crit)
    return {"qualified": qualified, "criteria": crit, "news_check": news, "spec_label": spec["label"]}


if __name__ == "__main__":
    # quick self-test
    tests = [
        ("bull_put", {"dte": 30, "short_delta": -0.23, "credit_to_width": 0.57, "iv_rank": 68, "trend": "up", "pop": 0.84, "sentiment": "NEUTRAL"}, True),
        ("bull_put", {"dte": 30, "short_delta": -0.23, "credit_to_width": 0.57, "iv_rank": 68, "trend": "up", "pop": 0.84, "sentiment": "NEGATIVE"}, False),  # news blocks
        ("bear_call", {"dte": 33, "short_delta": 0.20, "credit_to_width": 0.22, "iv_rank": 62, "trend": "down", "pop": 0.78, "sentiment": "NEGATIVE"}, True),
        ("bear_call", {"dte": 33, "short_delta": 0.20, "credit_to_width": 0.22, "iv_rank": 62, "trend": "up", "pop": 0.78, "sentiment": "POSITIVE"}, False),  # trend+news block
        ("iron_condor", {"dte": 30, "short_delta": 0.14, "credit_to_width": 0.32, "iv_rank": 48, "trend": "flat", "pop": 0.70, "sentiment": "NEUTRAL"}, True),
        ("iron_condor", {"dte": 30, "short_delta": 0.14, "credit_to_width": 0.32, "iv_rank": 48, "trend": "flat", "pop": 0.70, "sentiment": "POSITIVE"}, False),  # directional news
        ("long_call_lottery", {"dte": 36, "delta": 0.30, "iv_rank": 52, "trend": "up", "premium_usd": 210, "sentiment": "POSITIVE"}, True),
        ("long_call_lottery", {"dte": 36, "delta": 0.30, "iv_rank": 52, "trend": "up", "premium_usd": 210, "sentiment": "BLOCKING"}, False),
    ]
    ok = 0
    for strat, ctx, exp in tests:
        r = evaluate(strat, ctx)
        good = r["qualified"] == exp
        ok += good
        print(f"{'OK ' if good else '!! '}{strat:18} qualified={r['qualified']} (expected {exp}) news={r['news_check']['verdict']}")
    print(f"\n{ok}/{len(tests)} criteria tests passed")
