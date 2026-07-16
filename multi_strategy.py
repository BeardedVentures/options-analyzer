#!/usr/bin/env python3
"""
multi_strategy.py — LIVE generators for defined-risk CALL-side strategies (bear call, iron condor).

Additive to main.py's proven bull-put engine (that path is untouched). Reuses the real
edge_calculator true-POP / edge math, technicals, and strategies.py criteria + news validation.
Every trade it emits carries `needs_validation: True` (NEW live calls path — spot-check vs broker
on first run) plus the `criteria` + `news_check` from strategies.evaluate.

Pure-ish: the build_* functions accept injected chains so they can be unit-tested offline; scan_extra()
does the live fetch on the tower.  No undefined risk.
"""
from __future__ import annotations
from typing import Dict, List, Optional
import logging

import config
from analysis import edge_calculator
import strategies

logger = logging.getLogger(__name__)
MAXW = float(getattr(config, "MAX_SPREAD_WIDTH", 5))


def _tradeable(o: Dict) -> bool:
    return (o.get("mid", 0) or 0) > 0 and ((o.get("volume", 0) or 0) >= 1 or (o.get("open_interest", 0) or 0) >= 10)


def _pick_short(chain: List[Dict], target_delta: float, lo: float, hi: float) -> Optional[Dict]:
    cands = [o for o in chain if _tradeable(o) and lo <= abs(o.get("delta") or 0) <= hi]
    if not cands:
        return None
    return min(cands, key=lambda o: abs(abs(o.get("delta") or 0) - target_delta))


def _pick_long(chain: List[Dict], short: Dict, direction: str) -> Optional[Dict]:
    ks = short["strike"]; exp = short.get("expiration")
    if direction == "call":   # long strike ABOVE short (bear call)
        cands = [o for o in chain if o.get("expiration") == exp and 0 < (o["strike"] - ks) <= MAXW and _tradeable(o)]
        return min(cands, key=lambda o: o["strike"] - ks) if cands else None
    cands = [o for o in chain if o.get("expiration") == exp and 0 < (ks - o["strike"]) <= MAXW and _tradeable(o)]  # long BELOW short (bull put)
    return min(cands, key=lambda o: ks - o["strike"]) if cands else None


def _pop_below(dist_pct: float, dte: int, prices) -> Dict:
    """P(price ends BELOW a level dist_pct ABOVE spot) — measured directly, never mirrored."""
    return edge_calculator.calculate_pop_between(dte, prices, upper_move_pct=dist_pct)


def _pop_between(down_pct: float, up_pct: float, dte: int, prices) -> Dict:
    """P(price ends inside the band). down_pct/up_pct are POSITIVE distances from spot."""
    return edge_calculator.calculate_pop_between(dte, prices, lower_move_pct=-down_pct, upper_move_pct=up_pct)


def _edge_score(ticker, strategy, tech, vrp_pct, true_pop, implied_pop, sentiment, earnings_days):
    try:
        ep = edge_calculator.calculate_edge_points(true_pop, implied_pop).get("edge_points", 0)
    except Exception:
        ep = (true_pop - implied_pop) * 100 if (true_pop and implied_pop) else 0
    try:
        es = edge_calculator.calculate_edge_score(
            ticker=ticker, strategy=strategy, technical_score=tech.get("technical_score", 50) or 50,
            vrp_pct=vrp_pct or 0, edge_points=ep, news_sentiment=(sentiment or "NEUTRAL"),
            earnings_days_away=earnings_days if earnings_days is not None else 99,
            fundamentals_score=tech.get("fundamentals_score"))
        return es.get("total_score", 0), es.get("component_breakdown", {})
    except Exception as e:
        logger.debug(f"edge_score fallback: {e}")
        return int(max(0, min(100, 50 + ep))), {}


def _base(ticker, strategy_key, price, tech, sentiment, dte, exp):
    return {
        "ticker": ticker, "strategy": strategies.STRATEGY_SPECS[strategy_key]["label"],
        "current_price": round(price, 2), "dte": dte, "expiration_display": exp,
        "iv_rank": tech.get("iv_rank"), "vrp": tech.get("vrp"), "trend": tech.get("trend"),
        "rsi": tech.get("rsi"), "nearest_support": tech.get("nearest_support"),
        "news_sentiment": sentiment, "news_summary": tech.get("news_summary"),
        "fundamentals_score": tech.get("fundamentals_score"),
        "true_pop_drift_mode": "risk_free", "estimated_round_trip_cost_per_contract":
            float(getattr(config, "COMMISSION_PER_CONTRACT_PER_LEG", 0.65)) * 4 + 4.0,
        "needs_validation": True, "warnings": [],
    }


def build_bear_call(ticker, price, calls, prices_hist, tech, sentiment, earnings_days=None) -> Optional[Dict]:
    short = _pick_short(calls, 0.22, 0.16, 0.30)
    if not short:
        return None
    long_ = _pick_long(calls, short, "call")
    if not long_:
        return None
    credit_ps = round((short.get("mid", 0) or 0) - (long_.get("mid", 0) or 0), 2)
    width = abs(long_["strike"] - short["strike"])
    if credit_ps <= 0 or width <= 0:
        return None
    credit_usd = round(credit_ps * 100, 0); max_loss = round(width * 100 - credit_usd, 0)
    be = short["strike"] + credit_ps
    dte = short.get("dte") or 0
    if not price:
        return None
    otm_dist = (short["strike"] - price) / price   # short strike sits ABOVE spot
    be_dist = (be - price) / price
    # Mirror the proven bull-put convention: P(max profit) at the short strike for the edge
    # comparison (apples-to-apples with delta-implied P(OTM)), P(profit) at breakeven to gate.
    mp_res = _pop_below(otm_dist, dte, prices_hist)
    pr_res = _pop_below(be_dist, dte, prices_hist)
    p_max_profit = mp_res.get("true_pop")
    true_pop = pr_res.get("true_pop")
    implied = 1 - abs(short.get("delta") or 0)
    es, comp = _edge_score(ticker, "bear_call_spread", tech, tech.get("vrp"), p_max_profit, implied, sentiment, earnings_days)
    ctx = {"dte": dte, "short_delta": short.get("delta"),
           "credit_to_width": credit_ps / width, "iv_rank": tech.get("iv_rank"),
           "trend": tech.get("trend"), "pop": true_pop, "sentiment": sentiment}
    ev = strategies.evaluate("bear_call", ctx)
    if not ev["qualified"]:
        return None
    t = _base(ticker, "bear_call", price, tech, sentiment, dte, short.get("last_trade_date") or short.get("expiration"))
    t.update({
        "short_strike": short["strike"], "long_strike": long_["strike"], "credit_per_share": credit_ps,
        "credit_usd": credit_usd, "max_loss_usd": max_loss, "delta": short.get("delta"),
        "credit_to_width_pct": round(credit_ps / width * 100, 1), "true_pop": true_pop,
        "p_max_profit": p_max_profit, "breakeven": round(be, 2),
        "true_pop_confidence": pr_res.get("confidence", "LOW"),
        "true_pop_windows": pr_res.get("independent_windows"),
        "implied_pop": round(implied, 3), "edge_score": es,
        "component_breakdown": comp, "auto_reasoning": f"Bear call: {ev['news_check']['detail']}.",
        "criteria": ev["criteria"], "news_check": ev["news_check"],
    })
    return t


def build_iron_condor(ticker, price, calls, puts, prices_hist, tech, sentiment, earnings_days=None) -> Optional[Dict]:
    cs = _pick_short(calls, 0.16, 0.12, 0.22); ps = _pick_short(puts, 0.16, 0.12, 0.22)
    if not cs or not ps:
        return None
    cl = _pick_long(calls, cs, "call"); pl = _pick_long(puts, ps, "put")
    if not cl or not pl:
        return None
    credit_ps = round((cs["mid"] - cl["mid"]) + (ps["mid"] - pl["mid"]), 2)
    wcall = abs(cl["strike"] - cs["strike"]); wput = abs(ps["strike"] - pl["strike"])
    width = max(wcall, wput)
    if credit_ps <= 0 or width <= 0:
        return None
    credit_usd = round(credit_ps * 100, 0); max_loss = round(width * 100 - credit_usd, 0)
    dte = cs.get("dte") or 0
    if not price:
        return None
    # P(max profit): price finishes between the two SHORT strikes — directly comparable to the
    # delta-implied 1-|Δc|-|Δp|. P(profit): between the two breakevens (credit widens the band).
    mp_res = _pop_between((price - ps["strike"]) / price, (cs["strike"] - price) / price, dte, prices_hist)
    lower_be = ps["strike"] - credit_ps
    upper_be = cs["strike"] + credit_ps
    pr_res = _pop_between((price - lower_be) / price, (upper_be - price) / price, dte, prices_hist)
    p_max_profit = mp_res.get("true_pop")
    true_pop = pr_res.get("true_pop")
    implied = 1 - abs(cs.get("delta") or 0) - abs(ps.get("delta") or 0)
    es, comp = _edge_score(ticker, "iron_condor", tech, tech.get("vrp"), p_max_profit, implied, sentiment, earnings_days)
    ctx = {"dte": dte, "short_delta": cs.get("delta"), "credit_to_width": credit_ps / width,
           "iv_rank": tech.get("iv_rank"), "trend": tech.get("trend"), "pop": true_pop, "sentiment": sentiment}
    ev = strategies.evaluate("iron_condor", ctx)
    if not ev["qualified"]:
        return None
    t = _base(ticker, "iron_condor", price, tech, sentiment, dte, cs.get("last_trade_date") or cs.get("expiration"))
    t.update({
        "put_short_strike": ps["strike"], "put_long_strike": pl["strike"],
        "call_short_strike": cs["strike"], "call_long_strike": cl["strike"],
        "credit_per_share": credit_ps, "credit_usd": credit_usd, "max_loss_usd": max_loss,
        "delta": round((abs(cs.get("delta") or 0) - abs(ps.get("delta") or 0)), 3),
        "credit_to_width_pct": round(credit_ps / width * 100, 1),
        "true_pop": true_pop, "p_max_profit": p_max_profit,
        "breakeven_lower": round(lower_be, 2), "breakeven_upper": round(upper_be, 2),
        "true_pop_confidence": pr_res.get("confidence", "LOW"),
        "true_pop_windows": pr_res.get("independent_windows"),
        "implied_pop": round(implied, 3), "edge_score": es,
        "component_breakdown": comp, "auto_reasoning": f"Iron condor: {ev['news_check']['detail']}.",
        "criteria": ev["criteria"], "news_check": ev["news_check"],
    })
    return t


def scan_extra(ticker: str, sentiment_map: Dict, price_data=None, calls=None, puts=None, tech=None) -> List[Dict]:
    """Live-scan the enabled call-side strategies for one ticker. Returns qualified trade dicts."""
    out: List[Dict] = []
    enabled = getattr(config, "ENABLED_STRATEGIES", [])
    want_bc = "bear_call_spread" in enabled
    want_ic = "iron_condor" in enabled
    if not (want_bc or want_ic):
        return out
    try:
        from data import fetcher, technicals
        if price_data is None:
            price_data = fetcher.get_price_data(ticker)
        if price_data is None or price_data.empty:
            return out
        price = float(price_data["Close"].iloc[-1])
        prices_hist = price_data["Close"]
        if calls is None:
            calls = fetcher.get_call_options_chain(ticker, config.MIN_DTE, config.MAX_DTE)
        if want_ic and puts is None:
            puts = fetcher.get_options_chain(ticker, config.MIN_DTE, config.MAX_DTE)
        if tech is None:
            try:
                # current_iv is REQUIRED: it defaults to 0.0, which yields iv_rank 0 and makes
                # every iv_rank_min gate (bear call 35, condor 45) unpassable — the generators
                # would silently never emit a trade. Rank off the same chain we price from.
                iv_chain = list(calls or []) + list(puts or [])
                current_iv = technicals.estimate_atm_iv(iv_chain, price)
                if not current_iv:
                    logger.warning(f"[multi_strategy] {ticker}: no ATM IV in chain — iv_rank gates will reject")
                tech = technicals.calculate_all(price_data, ticker, current_iv=current_iv)
            except Exception as e:
                logger.warning(f"[multi_strategy] {ticker}: technicals failed: {e}")
                tech = {}
        sentiment = (sentiment_map.get(ticker, {}) or {}).get("sentiment", "NEUTRAL")
        earnings_days = None
        if want_bc and calls:
            t = build_bear_call(ticker, price, calls, prices_hist, tech, sentiment, earnings_days)
            if t:
                out.append(t)
        if want_ic and calls and puts:
            t = build_iron_condor(ticker, price, calls, puts, prices_hist, tech, sentiment, earnings_days)
            if t:
                out.append(t)
    except Exception as e:
        logger.warning(f"[multi_strategy] {ticker}: {e}")
    return out
