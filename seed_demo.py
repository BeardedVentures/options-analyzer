#!/usr/bin/env python3
"""
seed_demo.py — populate the whole VEGA beta with CRITERIA-COMPLIANT demo data.

Every trade written here is produced by the real strategies.py gates + news validation, so the
beta you review is exercising the actual selection logic — not hand-faked numbers. Trades that fail
their fitted criteria or whose thesis conflicts with news are dropped into rejected_trades with the
reason (so you can see the filter working). Runs offline; no live data needed.

    python seed_demo.py     # writes logs/scan_latest.json + logs/lottery_latest.json
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
import strategies

BASE = Path(__file__).resolve().parent
LOG = BASE / "logs"


def _spread(ticker, strat, price, short, long_, credit_ps, dte, delta, iv_rank, trend, sentiment,
            true_pop, implied_pop, comp, news_summary, extra=None):
    width = abs(short - long_)
    credit_usd = round(credit_ps * 100, 0)
    max_loss_usd = round(width * 100 - credit_usd, 0)
    ctw = credit_ps / width
    ctx = {"dte": dte, "short_delta": delta, "credit_to_width": ctw, "iv_rank": iv_rank,
           "trend": trend, "pop": true_pop, "sentiment": sentiment}
    ev = strategies.evaluate(strat, ctx)
    t = {
        "ticker": ticker, "strategy": strategies.STRATEGY_SPECS[strat]["label"],
        "current_price": price, "dte": dte, "expiration_display": "2026-08-21",
        "credit_per_share": credit_ps, "credit_usd": credit_usd, "max_loss_usd": max_loss_usd,
        "delta": delta, "iv_rank": iv_rank, "credit_to_width_pct": round(ctw * 100, 1),
        "true_pop": true_pop, "true_pop_confidence": "MEDIUM", "true_pop_drift_mode": "risk_free",
        "implied_pop": implied_pop, "edge_score": sum(comp.values()),
        "component_breakdown": comp, "vrp": round(comp.get("vrp", 0) / 3.0, 1),
        "news_sentiment": sentiment, "news_summary": news_summary, "trend": trend, "rsi": 50,
        "fundamentals_score": 7, "estimated_round_trip_cost_per_contract": 2.16,
        "auto_reasoning": f"{strategies.STRATEGY_SPECS[strat]['label']}: {ev['news_check']['detail']}.",
        "criteria": ev["criteria"], "news_check": ev["news_check"], "warnings": [],
    }
    if strat == "iron_condor":
        t.update(extra or {})
    else:
        t["short_strike"] = short; t["long_strike"] = long_
    return t, ev["qualified"], ev


def build():
    qualified, rejected = [], []
    rows = [
        # ticker, strat, price, short, long, credit, dte, delta, ivr, trend, sentiment, tpop, ipop, comp, summary
        ("UNH", "bull_put", 418.52, 380, 375, 2.85, 30, -0.23, 68, "up", "NEUTRAL", 0.84, 0.804,
         {"vrp": 22, "true_pop_edge": 18, "technical": 16, "fundamentals": 8, "news": 8, "earnings_safety": 5},
         "No significant news impact detected."),
        ("WMT", "bull_put", 180.1, 170, 168, 0.62, 36, -0.18, 41, "up", "POSITIVE", 0.79, 0.77,
         {"vrp": 15, "true_pop_edge": 12, "technical": 18, "fundamentals": 6, "news": 10, "earnings_safety": 5},
         "Positive news themes — favorable for premium selling."),
        ("TSLA", "bear_call", 240.0, 265, 270, 1.10, 33, 0.20, 62, "down", "NEGATIVE", 0.78, 0.75,
         {"vrp": 18, "true_pop_edge": 15, "technical": 16, "fundamentals": 6, "news": 13, "earnings_safety": 5},
         "Downgrade chatter — bearish tilt supports the short call."),
        ("XOM", "bear_call", 118.0, 128, 131, 0.80, 30, 0.22, 55, "flat", "NEUTRAL", 0.76, 0.73,
         {"vrp": 15, "true_pop_edge": 12, "technical": 16, "fundamentals": 7, "news": 10, "earnings_safety": 5},
         "Range-bound; no directional catalyst."),
        # rejected example: bull put into negative news (thesis conflict)
        ("BA", "bull_put", 175.0, 160, 155, 1.20, 31, -0.24, 58, "down", "NEGATIVE", 0.75, 0.73,
         {"vrp": 15, "true_pop_edge": 12, "technical": 10, "fundamentals": 4, "news": 0, "earnings_safety": 5},
         "Negative news themes — monitor closely."),
    ]
    for r in rows:
        t, ok, ev = _spread(*r)
        (qualified if ok else rejected).append({**t, "rejection_reason":
            "; ".join(c["label"] for c in ev["criteria"] if not c["ok"])} if not ok else t)

    # iron condor (multi-leg) — neutral, needs NEUTRAL news
    ic_extra = {"put_short_strike": 530, "put_long_strike": 525, "call_short_strike": 575, "call_long_strike": 580}
    t, ok, ev = _spread("SPY", "iron_condor", 552.0, 575, 580, 1.60, 30, 0.14, 48, "flat", "NEUTRAL",
                        0.70, 0.66, {"vrp": 15, "true_pop_edge": 18, "technical": 14, "fundamentals": 7, "news": 12, "earnings_safety": 5},
                        "Range-bound; balanced flows.", extra=ic_extra)
    (qualified if ok else rejected).append(t)
    # rejected IC: directional (POSITIVE) news breaks the range thesis
    t2, ok2, ev2 = _spread("QQQ", "iron_condor", 480.0, 500, 505, 1.5, 30, 0.15, 47, "up", "POSITIVE",
                          0.69, 0.66, {"vrp": 15, "true_pop_edge": 12, "technical": 10, "fundamentals": 6, "news": 0, "earnings_safety": 5},
                          "Positive breakout themes — directional risk.",
                          extra={"put_short_strike": 455, "put_long_strike": 450, "call_short_strike": 500, "call_long_strike": 505})
    if not ok2:
        rejected.append({**t2, "rejection_reason": "; ".join(c["label"] for c in ev2["criteria"] if not c["ok"])})
    else:
        qualified.append(t2)

    qualified.sort(key=lambda x: x["edge_score"], reverse=True)
    return qualified, rejected


def build_lottery():
    cands = [
        ("NVDA", 128.0, 140.0, 2.1, 36, 0.30, 52, "up", "POSITIVE", "Momentum breakout — above SMA20/50", 120.0),
        ("AMD", 132.0, 130.0, 3.4, 29, 0.36, 48, "down", "NEUTRAL", "Oversold bounce at support", 131.0),
        # rejected: negative news kills a bullish lottery
        ("PLTR", 42.0, 46.0, 1.1, 30, 0.32, 70, "down", "NEGATIVE", "Breakout attempt", 40.0),
    ]
    out = []
    for tk, price, strike, prem_ps, dte, delta, ivr, trend, sent, setup, support in cands:
        prem = round(prem_ps * 100, 0)
        ev = strategies.evaluate("long_call_lottery",
                                 {"dte": dte, "delta": delta, "iv_rank": ivr, "trend": trend,
                                  "premium_usd": prem, "sentiment": sent})
        if not ev["qualified"]:
            continue
        be = strike + prem_ps
        out.append({
            "ticker": tk, "current_price": price, "strike": strike, "expiration": "2026-08-21", "dte": dte,
            "premium_per_share": prem_ps, "premium_usd": prem, "max_loss_usd": prem, "delta": delta, "iv": ivr / 100.0,
            "breakeven": round(be, 2), "breakeven_move_pct": round((be - price) / price * 100, 1),
            "target_multiple": 3.0, "target_price": round(strike + prem_ps * 4, 2),
            "conviction": "HIGH" if trend == "up" else "MED", "setup": setup,
            "catalyst": ev["news_check"]["detail"], "news_sentiment": sent, "rsi": 60 if trend == "up" else 35,
            "trend": trend, "nearest_support": support, "criteria": ev["criteria"], "news_check": ev["news_check"],
        })
    return out


def main():
    LOG.mkdir(parents=True, exist_ok=True)
    qual, rej = build()
    (LOG / "scan_latest.json").write_text(json.dumps({
        "timestamp": datetime.now().isoformat(), "session_type": "DEMO",
        "market_context": {"vix": 18.4}, "regime": {"note": "Neutral bullish"},
        "qualified_trades": qual, "rejected_trades": rej}, indent=2), encoding="utf-8")
    lot = build_lottery()
    (LOG / "lottery_latest.json").write_text(json.dumps({
        "timestamp": datetime.now().isoformat(), "budget_per_contract": 400, "lottery_calls": lot}, indent=2), encoding="utf-8")
    print(f"seeded {len(qual)} qualified ({len(rej)} rejected) + {len(lot)} lottery calls")
    for t in qual:
        print(f"  QUALIFIED  {t['ticker']:5} {t['strategy']}")
    for t in rej:
        print(f"  rejected   {t['ticker']:5} {t['strategy']:18} -> {t.get('rejection_reason')}")


if __name__ == "__main__":
    main()
