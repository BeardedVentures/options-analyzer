#!/usr/bin/env python3
"""
lottery_scanner.py — SPECULATIVE single-call "home-run" scanner (defined, capped cost).

This is deliberately separate from the disciplined premium-selling engine (main.py). Buying a
single call is a low-probability, high-variance bet whose max loss is 100% of the premium. It is
NOT a positive-edge income trade. This tool only surfaces calls in *specific* conditions and always
shows the true (low) probability and the move required, so the user decides with eyes open.

Conditions surfaced (one must hold):
  * MOMENTUM  — RSI 55-70, price above SMA20 & SMA50, news not negative  -> slightly-OTM call
  * REVERSAL  — RSI < 38, price within ~3% of nearest support, news not blocking -> OTM bounce call

Output: logs/lottery_latest.json (read by the cockpit "Lottery" view).

    python lottery_scanner.py            # live scan (needs yfinance on the tower)
    python lottery_scanner.py --demo     # write a synthetic sample so you can see the view
    python lottery_scanner.py --budget 250   # cap premium/contract

Nothing here places orders or spends money.
"""
from __future__ import annotations
import argparse, json, sys, os
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "logs" / "lottery_latest.json"
sys.path.insert(0, str(BASE))


def _build_call(ticker, price, tech, news, chain_calls, budget, dte_lo=25, dte_hi=45):
    """Pick a ~0.30-delta call in the DTE window under budget; classify the setup."""
    rsi = tech.get("rsi"); sma20 = tech.get("sma20"); sma50 = tech.get("sma50")
    support = tech.get("nearest_support"); trend = tech.get("trend")
    sent = (news.get("sentiment") or "NEUTRAL").upper()
    setup = None
    if rsi is not None and sma20 and sma50 and 55 <= rsi <= 70 and price > sma20 and price > sma50 and sent not in ("NEGATIVE", "BLOCKING"):
        setup, conv = "Momentum breakout — above SMA20/50 with room to run", "HIGH"
    elif rsi is not None and support and rsi < 38 and abs(price - support) / price <= 0.03 and sent != "BLOCKING":
        setup, conv = "Oversold bounce at support", "MED"
    else:
        return None
    # choose call: nearest to ~0.32 delta within DTE + budget
    cands = [o for o in chain_calls if dte_lo <= (o.get("dte") or 0) <= dte_hi
             and (o.get("mid") or 0) * 100 <= budget and (o.get("delta") or 0) > 0]
    if not cands:
        return None
    pick = min(cands, key=lambda o: abs((o.get("delta") or 0) - 0.32))
    prem_ps = float(pick.get("mid") or 0); prem = round(prem_ps * 100, 0)
    strike = float(pick.get("strike")); be = strike + prem_ps
    be_move = (be - price) / price * 100
    tgt_mult = 3.0
    # underlying price where the option ~triples at expiration (intrinsic ≈ 3*premium above breakeven)
    tgt_price = strike + prem_ps * (1 + tgt_mult)
    return {
        "ticker": ticker, "current_price": round(price, 2), "strike": strike,
        "expiration": pick.get("expiration"), "dte": pick.get("dte"),
        "premium_per_share": round(prem_ps, 2), "premium_usd": prem, "max_loss_usd": prem,
        "delta": round(float(pick.get("delta") or 0), 2), "iv": pick.get("iv"),
        "breakeven": round(be, 2), "breakeven_move_pct": round(be_move, 1),
        "target_multiple": tgt_mult, "target_price": round(tgt_price, 2),
        "conviction": conv, "setup": setup, "catalyst": news.get("market_impact_summary"),
        "news_sentiment": sent, "rsi": rsi, "trend": trend, "nearest_support": support,
    }


def scan_live(budget):
    import config
    from data import fetcher, technicals, news as newsmod
    calls = []
    for w in getattr(config, "WATCHLIST", []):
        tk = w["ticker"]
        try:
            px = fetcher.get_price_data(tk, period="6mo")
            if px is None or px.empty:
                continue
            price = float(px["Close"].iloc[-1])
            tech = technicals.compute(tk, px) if hasattr(technicals, "compute") else {}
            nws = newsmod.get_sentiment(tk) if hasattr(newsmod, "get_sentiment") else {}
            chain = fetcher.get_call_options_chain(tk, 25, 45) if hasattr(fetcher, "get_call_options_chain") else []
            row = _build_call(tk, price, tech, nws, chain, budget)
            if row:
                calls.append(row)
        except Exception as e:
            print(f"  {tk}: skipped ({e})")
    return calls


def demo():
    return [
        {"ticker": "NVDA", "current_price": 128.0, "strike": 140.0, "expiration": "2026-08-21", "dte": 36,
         "premium_per_share": 2.1, "premium_usd": 210, "max_loss_usd": 210, "delta": 0.30, "iv": 0.52,
         "breakeven": 142.1, "breakeven_move_pct": 11.0, "target_multiple": 3.0, "target_price": 148.4,
         "conviction": "HIGH", "setup": "Momentum breakout — above SMA20/50 with room to run",
         "catalyst": "Positive news themes — sector strength", "news_sentiment": "POSITIVE",
         "rsi": 63, "trend": "up", "nearest_support": 120.0},
        {"ticker": "AMD", "current_price": 132.0, "strike": 130.0, "expiration": "2026-08-14", "dte": 29,
         "premium_per_share": 3.4, "premium_usd": 340, "max_loss_usd": 340, "delta": 0.36, "iv": 0.48,
         "breakeven": 133.4, "breakeven_move_pct": 1.1, "target_multiple": 3.0, "target_price": 143.6,
         "conviction": "MED", "setup": "Oversold bounce at support", "catalyst": "No significant news impact",
         "news_sentiment": "NEUTRAL", "rsi": 35, "trend": "down", "nearest_support": 131.0},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--budget", type=float, default=400.0)
    a = ap.parse_args()
    calls = demo() if a.demo else scan_live(a.budget)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"timestamp": datetime.now().isoformat(),
                               "budget_per_contract": a.budget,
                               "lottery_calls": calls}, indent=2), encoding="utf-8")
    print(f"wrote {len(calls)} lottery call(s) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
