#!/usr/bin/env python3
"""
environment.py — VEGA's "game environment" read + best-practice structure recommendation.

THE IDEA (Josh's sports analogy, market side):
    News shocks come in two kinds. An AA plane crash or a Wells-Fargo fraud suit is an
    "injury" — exogenous, no pre-tremor, unpredictable → quarantined by the CLV tracker's
    news_catalyst flag. But some danger is READABLE in advance: the options tape heats up
    before many surprises (IV accelerating, put-skew steepening, news velocity rising, the
    whole sector trading heavy). That's the "likely game environment" — and a disciplined
    premium-seller reads it and gets ahead of it.

WHAT THIS DOES:
    Given a qualified trade, score how HOT the environment is *for the risk side of that
    specific structure*, and recommend the professionally-sound response:

      • bull put risk = downside.  Bearish heat (down-trend, weak RSI, rich/steepening put
        skew, negative news) means you're selling the exact move that's building.
      • bear call risk = upside.   Bullish heat is the mirror danger.
      • iron condor risk = both.   Two-sided vol expansion threatens both wings.

    Best-practice response (documented options-selling conventions — tastytrade mechanics,
    directional-alignment, skew harvesting; this is an EDUCATIONAL heuristic, NOT advice):

      COOL/NEUTRAL → keep the structure, full size.
      WARM         → size down (~½) and push the short strike further OTM (lower delta).
      HOT + one-sided against you → FLIP to the opposite defined-risk structure on the side
                     the tape is protecting (bull put breaking down → bear call above
                     resistance), OR stand aside.
      HOT + two-sided vol → iron condor only if both breakevens clear the expected move,
                     else stand aside.

    Sizing is the primary risk control; direction alignment is second; strike distance third.

Pure-stdlib, never raises into a caller. v1 scores from point-in-time fields (trend, RSI,
IV rank, skew, news). MOMENTUM signals (IV *acceleration*, news *velocity*, sector heat)
need the historical snapshots on the tower — see enrich_with_momentum() stub.
"""

from __future__ import annotations
from typing import Dict, Optional


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except Exception:
        return None


# Threat weights (0-100 heat). Tuned conservatively; the CLV tracker grades these calls
# over time so the thresholds can be learned rather than guessed.
def heat_assessment(t: Dict) -> Dict:
    """t = a raw engine qualified-trade dict (scan_latest.json shape) OR a cockpit card.
       Returns {heat, band, threat_dir, drivers[], recommendation{...}}."""
    strat = (t.get("strategy") or t.get("strat_type") or "bull_put").lower()
    if "condor" in strat:
        risk_dir = "both"
    elif "bear" in strat and "call" in strat:
        risk_dir = "upside"
    else:
        risk_dir = "downside"

    trend = (t.get("trend") or "").upper()
    rsi = _f(t.get("rsi"))
    ivr = _f(t.get("iv_rank"))
    skew = _f(t.get("skew_vol_pts"))          # + = puts richer (downside fear); − = calls richer
    news = (t.get("news_sentiment") or "").upper()

    heat = 0.0
    drivers = []

    def add(pts, why):
        nonlocal heat
        heat += pts
        drivers.append(why)

    # ── Directional danger to the SHORT side of THIS structure ──
    if risk_dir == "downside":                # bull put — danger is a break DOWN
        if trend == "STRONG_DOWN": add(35, "price in a STRONG downtrend — selling puts fights the tape")
        elif trend == "DOWN":      add(20, "price trending down under the short put")
        if rsi is not None and rsi < 40: add(12, f"RSI {rsi:.0f} — downside momentum")
        if skew is not None and skew > 1.0: add(12, f"put skew rich (+{skew:.1f}) — downside being bid")
        if news in ("NEGATIVE", "BLOCKING"): add(20, f"news {news} on the name")
        threat_dir = "bearish"
    elif risk_dir == "upside":                # bear call — danger is a rip UP
        if trend == "STRONG_UP": add(35, "price in a STRONG uptrend — selling calls fights the tape")
        elif trend == "UP":      add(20, "price trending up into the short call")
        if rsi is not None and rsi > 60: add(12, f"RSI {rsi:.0f} — upside momentum")
        if skew is not None and skew < -1.0: add(12, f"call skew rich ({skew:.1f}) — upside being chased")
        if news == "POSITIVE": add(20, "positive news driving the name up")
        threat_dir = "bullish"
    else:                                     # iron condor — both wings
        if trend in ("STRONG_UP", "STRONG_DOWN"): add(25, f"strong {trend.lower()} trend threatens a wing")
        if rsi is not None and (rsi < 35 or rsi > 65): add(10, f"RSI {rsi:.0f} at an extreme")
        if news in ("NEGATIVE", "POSITIVE", "BLOCKING"): add(15, f"directional news ({news}) vs a neutral structure")
        threat_dir = "two-sided"

    # ── Vol regime: high IV is the edge, but very high IV can mean a catalyst is priced in.
    #    (True IV *acceleration* is a momentum signal — see enrich_with_momentum.) ──
    if ivr is not None and ivr >= 85: add(10, f"IV rank {ivr:.0f} — richly elevated, watch for a catalyst")

    heat = max(0.0, min(100.0, heat))
    band = ("hot" if heat >= 45 else "warm" if heat >= 20 else "cool")
    rec = _recommend(strat, risk_dir, threat_dir, band, heat, t)
    return {"heat": round(heat), "band": band, "threat_dir": threat_dir,
            "risk_dir": risk_dir, "drivers": drivers, "recommendation": rec}


def _recommend(strat, risk_dir, threat_dir, band, heat, t) -> Dict:
    """Best-practice response. Returns {action, size_mult, structure, strike_note, rationale}."""
    price = _f(t.get("current_price") or t.get("price"))
    # rough "above resistance" zone for a flip-to-bear-call suggestion
    res = None
    if price is not None:
        buf = 0.06 if (_f(t.get("iv_rank")) or 0) >= 60 else 0.04
        res = price * (1 + buf)

    if band == "cool":
        return {"action": "keep", "size_mult": 1.0, "structure": strat,
                "strike_note": "", "rationale": "Environment is calm — rich premium likely to decay. Full size."}
    if band == "warm":
        alt = None
        if risk_dir == "downside":
            alt = (f"BEAR CALL spread above resistance (~${res:.0f}+)" if res else "BEAR CALL spread above resistance")
        elif risk_dir == "upside":
            alt = "BULL PUT spread below support"
        return {"action": "trim", "size_mult": 0.5, "structure": strat,
                "strike_note": "push the short strike further OTM (short delta ~0.10–0.15)",
                "alt_structure": alt,
                "rationale": ("Some heat against the short side — halve size and widen the cushion. "
                              + (f"Or express the same premium edge on the protected side: {alt}." if alt else ""))}
    # HOT
    if risk_dir == "downside":   # bull put in bearish heat → flip to bear call
        flip = f"BEAR CALL spread above resistance (~${res:.0f}+)" if res else "BEAR CALL spread above resistance"
        return {"action": "flip_or_stand_aside", "size_mult": 0.0, "structure": "bear_call_spread",
                "strike_note": flip,
                "rationale": ("Heating bearishly — the bull put is short the exact move that's building. "
                              "Sell the side the tape protects: a defined-risk bear call above resistance keeps the "
                              "theta/IV edge but aligns with the down-move. If no clean resistance, stand aside.")}
    if risk_dir == "upside":     # bear call in bullish heat → flip to bull put
        return {"action": "flip_or_stand_aside", "size_mult": 0.0, "structure": "bull_put_spread",
                "strike_note": "BULL PUT spread below support",
                "rationale": ("Heating bullishly — the bear call fights an uptrend. Rotate to a defined-risk bull put "
                              "below support, or stand aside.")}
    # iron condor two-sided heat
    return {"action": "condor_or_stand_aside", "size_mult": 0.5, "structure": "iron_condor",
            "strike_note": "only if BOTH breakevens clear the expected move; else stand aside",
            "rationale": ("Two-sided vol is building — hold the condor only if both wings sit outside the expected "
                          "move; otherwise the premium isn't paying for the risk. Size down.")}


def enrich_with_momentum(t: Dict, iv_history: Optional[list] = None,
                         news_history: Optional[list] = None, sector_heat: Optional[float] = None) -> Dict:
    """TOWER HOOK (not wired yet): fold in the true leading signals that need time-series —
       IV-rank acceleration (data/iv_history), news velocity (scan_log across days), and
       sector heat (peers stressed). These sharpen the point-in-time read in heat_assessment.
       Returns extra heat points + drivers to add. Stubbed to 0 until wired on the tower."""
    extra = 0.0
    drivers = []
    # e.g. if iv_history and rising fast: extra += 15; drivers.append("IV rank accelerating")
    return {"extra_heat": extra, "drivers": drivers}


# quick self-test / CLI over a scan artifact
if __name__ == "__main__":
    import json, sys
    from pathlib import Path
    p = Path(__file__).resolve().parent / "logs" / "scan_latest.json"
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
    d = json.loads(p.read_text())
    for t in d.get("qualified_trades", []):
        a = heat_assessment(t)
        r = a["recommendation"]
        print(f"{t['ticker']:5} {t['strategy'][:10]:10} heat={a['heat']:3} {a['band']:4} "
              f"[{a['threat_dir']}] -> {r['action']:20} size×{r['size_mult']} "
              f"{r['structure'] if r['structure']!=t.get('strategy','').lower() else ''}")
        if a["band"] != "cool":
            for dv in a["drivers"]:
                print(f"        · {dv}")
            print(f"        => {r['rationale']}")
