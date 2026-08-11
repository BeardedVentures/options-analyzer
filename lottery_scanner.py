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

import strategies


def vol_edge(px_close, iv, window: int = 60):
    """The edge a BUYER of options has: realized vol above implied.

    This tab had no edge metric at all. It ranked nothing, scored nothing, and labelled every
    card HIGH conviction — 19 of 19 in the 2026-08-11 scan — so the badge carried no
    information and the ordering was the watchlist's.

    The premium-selling side of VEGA is built on one number: implied vol above realized means
    the options are expensive and worth writing. Buying a call is the SAME test with the sign
    flipped. If a stock delivers more movement than its options are priced for, the buyer is
    getting the move cheap; if it delivers less, the buyer is overpaying for it however good
    the chart looks.

    That number was already computable here and simply never computed. Measured on
    2026-08-11: AMD realized 61.7 against 53.5 implied, PLTR 64.8 against 47.7, COIN 76.5
    against 60.7 — the names where buying is actually favoured — while the tab was surfacing
    JPM, FCX, BA and GDX at IV rank 0, cheap by percentile and never checked against what
    those stocks do.

    Returns vol points (realized minus implied). Positive favours the buyer. None when either
    side is unavailable — absence, not a zero that would rank as neutral.
    """
    import math
    if iv is None or iv <= 0:
        return None
    closes = [float(c) for c in px_close if c and float(c) > 0]
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    rets = rets[-window:]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    rv = math.sqrt(var) * math.sqrt(252) * 100
    ivp = iv * 100 if iv < 3 else iv
    return round(rv - ivp, 1)


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
    # Choose the call nearest ~0.32 delta within DTE + budget, but only inside the spec's
    # delta band. Searching the whole chain and picking "nearest 0.32" silently degrades to
    # junk when budget binds: on a high-priced underlying the only calls under $400 are deep
    # OTM, so this surfaced 0.08-delta tickets labelled HIGH conviction.
    d_lo, d_hi = strategies.STRATEGY_SPECS["long_call_lottery"]["target_delta"]
    cands = [o for o in chain_calls if dte_lo <= (o.get("dte") or 0) <= dte_hi
             and (o.get("mid") or 0) * 100 <= budget
             and d_lo <= (o.get("delta") or 0) <= d_hi]
    if not cands:
        return None
    pick = min(cands, key=lambda o: abs((o.get("delta") or 0) - 0.32))
    prem_ps = float(pick.get("mid") or 0); prem = round(prem_ps * 100, 0)
    strike = float(pick.get("strike")); be = strike + prem_ps
    be_move = (be - price) / price * 100
    tgt_mult = 3.0
    # underlying price where the option ~triples at expiration (intrinsic ≈ 3*premium above breakeven)
    tgt_price = strike + prem_ps * (1 + tgt_mult)
    # Final gate through the single source of truth (delta band, IV-rank cap, budget, and news
    # validation), so a lottery row is held to the same published criteria as every other
    # strategy and the cockpit can show it the same criteria panel.
    ev = strategies.evaluate("long_call_lottery", {
        "dte": pick.get("dte"), "delta": pick.get("delta"), "iv_rank": tech.get("iv_rank"),
        "trend": trend, "premium_usd": prem, "sentiment": sent,
    })
    if not ev["qualified"]:
        return None

    # Per-ticker differentiation (spec §5.5): surface 2–3 CONCRETE technical signals
    # instead of the same template setup line on every card. Built from this ticker's
    # actual indicators so no two cards read identically.
    ivr = tech.get("iv_rank")
    signals = []
    if rsi is not None and "Momentum" in (setup or ""):
        signals.append(f"RSI {rsi:.0f} — momentum with room below overbought")
        if sma20 and sma50:
            signals.append(f"Above SMA20 ${sma20:.2f} & SMA50 ${sma50:.2f}")
    elif rsi is not None:
        signals.append(f"RSI {rsi:.0f} — oversold, turning at support")
        if support:
            signals.append(f"Holding support ${support:.2f} ({(price - support) / price * 100:+.1f}% away)")
    signals.append(f"Breakeven +{be_move:.1f}% to ${be:.2f}; ~{tgt_mult:.0f}x if it reaches ${tgt_price:.2f}")
    if ivr is not None:
        signals.append(f"IV-Rank {ivr:.0f}")
    signals = signals[:3]

    # Drop the generic "no significant news" filler — it reads as template noise.
    catalyst = news.get("market_impact_summary")
    if catalyst and "no significant" in catalyst.lower():
        catalyst = None

    return {
        "ticker": ticker, "current_price": round(price, 2), "strike": strike,
        "expiration": pick.get("expiration"), "dte": pick.get("dte"),
        "premium_per_share": round(prem_ps, 2), "premium_usd": prem, "max_loss_usd": prem,
        "delta": round(float(pick.get("delta") or 0), 2), "iv": pick.get("iv"),
        "iv_rank": tech.get("iv_rank"),
        "breakeven": round(be, 2), "breakeven_move_pct": round(be_move, 1),
        "target_multiple": tgt_mult, "target_price": round(tgt_price, 2),
        "realized_vol_30d": tech.get("realized_vol") or tech.get("realized_vol_30d"),
        "vol_edge_pp": tech.get("_vol_edge_pp"),
        "conviction": conv, "setup": setup, "catalyst": catalyst, "signals": signals,
        "news_sentiment": sent, "rsi": rsi, "trend": trend, "nearest_support": support,
        "criteria": ev["criteria"], "news_check": ev["news_check"],
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
            # These were hasattr-guarded against function names that do not exist
            # (technicals.compute, news.get_sentiment), so tech/news silently fell back to {}
            # forever. _build_call needs rsi/sma/support and returns None without them, so the
            # live scanner could never emit a single call. Call the real functions instead.
            chain = fetcher.get_call_options_chain(tk, 25, 45)
            _iv = technicals.estimate_atm_iv(chain, price)
            tech = technicals.calculate_all(px, tk, current_iv=_iv)
            # The buyer's edge, attached before the card is built so it can be shown and ranked.
            tech["_vol_edge_pp"] = vol_edge(list(px["Close"].values), _iv)
            nws = newsmod.get_ticker_sentiment(tk)
            row = _build_call(tk, price, tech, nws, chain, budget)
            if row:
                calls.append(row)
        except Exception as e:
            print(f"  {tk}: skipped ({e})")
    # Rank by the buyer's edge, richest first. Unranked before this: the order was the
    # watchlist's, so the top card was whichever ticker config happened to list first.
    # Cards with no measurable edge sort last rather than being dropped — the setup may still
    # be worth a look, it just cannot claim the options are cheap.
    calls.sort(key=lambda c: (c.get("vol_edge_pp") is not None, c.get("vol_edge_pp") or -999),
               reverse=True)
    return calls


def demo():
    return [
        {"ticker": "NVDA", "current_price": 128.0, "strike": 140.0, "expiration": "2026-08-21", "dte": 36,
         "premium_per_share": 2.1, "premium_usd": 210, "max_loss_usd": 210, "delta": 0.30, "iv": 0.52,
         "breakeven": 142.1, "breakeven_move_pct": 11.0, "target_multiple": 3.0, "target_price": 148.4,
         "conviction": "HIGH", "setup": "Momentum breakout — above SMA20/50 with room to run",
         "catalyst": "Positive news themes — sector strength", "news_sentiment": "POSITIVE",
         "signals": ["RSI 63 — momentum with room below overbought", "Above SMA20 $124.40 & SMA50 $119.80",
                     "Breakeven +11.0% to $142.10; ~3x if it reaches $148.40"],
         "rsi": 63, "trend": "up", "nearest_support": 120.0},
        {"ticker": "AMD", "current_price": 132.0, "strike": 130.0, "expiration": "2026-08-14", "dte": 29,
         "premium_per_share": 3.4, "premium_usd": 340, "max_loss_usd": 340, "delta": 0.36, "iv": 0.48,
         "breakeven": 133.4, "breakeven_move_pct": 1.1, "target_multiple": 3.0, "target_price": 143.6,
         "conviction": "MED", "setup": "Oversold bounce at support", "catalyst": None,
         "signals": ["RSI 35 — oversold, turning at support", "Holding support $131.00 (+0.8% away)",
                     "Breakeven +1.1% to $133.40; ~3x if it reaches $143.60"],
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
