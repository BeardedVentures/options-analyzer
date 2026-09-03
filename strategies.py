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
import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

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


# data/technicals.py classifies regime as STRONG_UP | UP | NEUTRAL | DOWN | STRONG_DOWN.
# The specs above speak in thesis buckets (up / flat / down), so the two vocabularies must be
# reconciled in exactly one place. Without this, "NEUTRAL" never equals "flat" and the iron
# condor (trend_ok={"flat"}) can never qualify under ANY market condition, while a STRONG_DOWN
# tape — the textbook bear-call setup — is rejected for not fitting a bearish thesis.
_TREND_BUCKETS = {
    "STRONG_UP": "up", "UP": "up",
    "NEUTRAL": "flat", "FLAT": "flat", "SIDEWAYS": "flat", "RANGE": "flat",
    "DOWN": "down", "STRONG_DOWN": "down",
}


def normalize_trend(trend: str) -> str:
    """Map a technicals regime label onto its thesis bucket. Unknown/absent → "" (the
    caller then skips the regime check rather than silently failing a valid trade)."""
    if not trend:
        return ""
    t = str(trend).strip().upper()
    return _TREND_BUCKETS.get(t, t.lower())


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


def _chk(label: str, ok: bool, detail: str = "", advisory: bool = False) -> Dict:
    """A criterion row. `advisory=True` marks a row that INFORMS but never disqualifies:
    it renders amber in the cockpit exactly like a hard failure, but is excluded from the
    `qualified` computation below. Callers (multi_strategy.py, lottery_scanner.py) return
    None on `not qualified`, so anything appended without this flag is a hard block."""
    row = {"label": label, "ok": bool(ok), "detail": detail}
    if advisory:
        row["advisory"] = True
    return row


def evaluate(strategy: str, ctx: Dict) -> Dict:
    """Run the fitted criteria + news validation for a candidate.
    ctx keys (best-effort): dte, short_delta, credit_to_width, iv_rank, trend, pop, sentiment,
        premium_usd (lottery), delta (lottery).
    Returns {qualified: bool, criteria: [ {label, ok, detail} ], news_check: {...}, spec_label}.
    A recommendation qualifies only if ALL hard criteria pass AND news is not blocking.
    """
    spec = STRATEGY_SPECS.get(strategy)
    if not spec:
        # LOUD, because this is a programming error wearing the costume of a normal rejection.
        #
        # A caller passing a key that is not in STRATEGY_SPECS gets qualified=False -- which is
        # indistinguishable from a candidate that genuinely failed its criteria. main.py wraps
        # this call in a try/except whose comment reads "Fail open: a bug in evaluate() must not
        # silently empty the board" -- but nothing RAISES here, so that handler never sees it and
        # the board empties anyway, by the one route the guard cannot watch.
        #
        # It is not reachable today: all five production callers pass hardcoded literals
        # (bull_put, bear_call, iron_condor, long_call_lottery x2). It becomes reachable the
        # moment a spec key is renamed without updating a caller -- and the symptom would be a
        # board that qualifies nothing, which this project has just spent a week learning is
        # extremely easy to mistake for a market condition.
        #
        # Still returns qualified=False rather than raising: failing CLOSED is the safe
        # direction, and raising would hit main.py's fail-open handler and fill the board with
        # ungated candidates instead. Loud and closed, not silent or open.
        # test_every_caller_uses_a_real_strategy_key catches the rename before it ships.
        logger.error("[strategies] UNKNOWN STRATEGY %r -- not in STRATEGY_SPECS (%s). Nothing "
                     "can qualify under this key; if a spec was renamed, a caller was missed.",
                     strategy, ", ".join(sorted(STRATEGY_SPECS)))
        return {"qualified": False, "criteria": [_chk("unknown strategy", False)], "news_check": {}, "spec_label": strategy}
    crit: List[Dict] = []

    dte = ctx.get("dte")
    if dte is not None:
        lo, hi = spec["dte"]
        crit.append(_chk(f"DTE {lo}-{hi}", lo <= dte <= hi, f"{dte}d"))

    raw_trend = ctx.get("trend")
    trend = normalize_trend(raw_trend)
    if trend:
        # Show the raw label so the cockpit reads the same word the technicals panel does.
        crit.append(_chk("Regime fits thesis", trend in spec["trend_ok"],
                         f"trend {str(raw_trend).lower()}"))

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

    # Entry timing — pattern phase (advisory). Populated by the caller via
    # analysis.entry_timing.assess_entry_timing(); absent means the module is disabled.
    timing = ctx.get("entry_timing") or {}
    if timing:
        readiness = timing.get("readiness", "NEUTRAL")
        phase = (timing.get("phase") or "").replace("_", " ").title()
        rsi_val = timing.get("rsi_at_signal")
        rsi_txt = f" (RSI {rsi_val:.0f})" if isinstance(rsi_val, (int, float)) else ""
        crit.append(_chk(
            f"Entry timing {timing.get('readiness_icon', '')}".strip(),
            timing.get("timing_gate_pass", True),
            f"{readiness} — {phase}{rsi_txt}",
            advisory=True,
        ))

    # Advisory rows never disqualify — see _chk().
    #
    # all() OVER AN EMPTY SEQUENCE IS TRUE, so if every criterion were advisory this would
    # qualify the trade having enforced NOTHING, and it would do it silently. Not reachable
    # today -- every strategy path emits at least one hard criterion, verified 2026-09-03 -- but
    # this file's whole contract is that a row added here is a hard block unless it opts out
    # with advisory=True, and one careless advisory=True on the last hard row would flip the
    # default from "blocks" to "passes everything". A guard that fails OPEN on an empty
    # collection is the same family as the zero-row cap in data_quality_log and the
    # `gate_raw > 0` skip in fetcher: a boundary where the check returns a passing value
    # instead of an error.
    hard = [c for c in crit if not c.get("advisory")]
    if not hard:
        logger.error("[strategies] %s produced NO hard criteria -- refusing to qualify. Every "
                     "check was advisory, which means nothing was enforced.", spec["label"])
        return {"qualified": False, "criteria": crit, "news_check": news,
                "spec_label": spec["label"]}
    qualified = all(c["ok"] for c in hard)
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
        # --- REAL technicals vocabulary (STRONG_UP|UP|NEUTRAL|DOWN|STRONG_DOWN) ---
        # These are the labels the live engine actually passes. Before normalize_trend(),
        # "NEUTRAL" != "flat" so the condor could never qualify and STRONG_* always failed.
        ("iron_condor", {"dte": 30, "short_delta": 0.14, "credit_to_width": 0.32, "iv_rank": 48, "trend": "NEUTRAL", "pop": 0.70, "sentiment": "NEUTRAL"}, True),
        ("bear_call", {"dte": 33, "short_delta": 0.20, "credit_to_width": 0.22, "iv_rank": 62, "trend": "STRONG_DOWN", "pop": 0.78, "sentiment": "NEGATIVE"}, True),
        ("bull_put", {"dte": 30, "short_delta": -0.23, "credit_to_width": 0.57, "iv_rank": 68, "trend": "STRONG_UP", "pop": 0.84, "sentiment": "NEUTRAL"}, True),
        # ...and the thesis must still be enforced, not merely satisfied by normalization.
        ("bear_call", {"dte": 33, "short_delta": 0.20, "credit_to_width": 0.22, "iv_rank": 62, "trend": "STRONG_UP", "pop": 0.78, "sentiment": "NEGATIVE"}, False),
        ("iron_condor", {"dte": 30, "short_delta": 0.14, "credit_to_width": 0.32, "iv_rank": 48, "trend": "STRONG_UP", "pop": 0.70, "sentiment": "NEUTRAL"}, False),
        # --- Entry timing is ADVISORY: a failing timing row must NOT disqualify. ---
        # multi_strategy.py / lottery_scanner.py return None on `not qualified`, so if this
        # ever regresses, every bear call and condor with mid-range RSI silently disappears.
        ("bull_put", {"dte": 30, "short_delta": -0.23, "credit_to_width": 0.57, "iv_rank": 68, "trend": "up", "pop": 0.84, "sentiment": "NEUTRAL",
                      "entry_timing": {"readiness": "EARLY", "phase": "EARLY_PULLBACK", "timing_gate_pass": False, "rsi_at_signal": 62.0}}, True),
        ("bear_call", {"dte": 33, "short_delta": 0.20, "credit_to_width": 0.22, "iv_rank": 62, "trend": "down", "pop": 0.78, "sentiment": "NEGATIVE",
                       "entry_timing": {"readiness": "CAUTION", "phase": "EARLY_BOUNCE", "timing_gate_pass": False, "rsi_at_signal": 38.0}}, True),
        # ...but a genuine hard failure still blocks even when timing is OPTIMAL.
        ("bull_put", {"dte": 30, "short_delta": -0.23, "credit_to_width": 0.57, "iv_rank": 68, "trend": "up", "pop": 0.84, "sentiment": "NEGATIVE",
                      "entry_timing": {"readiness": "OPTIMAL", "phase": "REVERSAL_SETUP", "timing_gate_pass": True, "rsi_at_signal": 44.0}}, False),
    ]
    assert normalize_trend("NEUTRAL") == "flat" and normalize_trend("STRONG_DOWN") == "down"
    assert normalize_trend("flat") == "flat" and normalize_trend(None) == ""
    ok = 0
    for strat, ctx, exp in tests:
        r = evaluate(strat, ctx)
        good = r["qualified"] == exp
        ok += good
        print(f"{'OK ' if good else '!! '}{strat:18} qualified={r['qualified']} (expected {exp}) news={r['news_check']['verdict']}")
    print(f"\n{ok}/{len(tests)} criteria tests passed")
