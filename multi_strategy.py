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


def _structure(price_data, tech: Dict) -> Dict:
    """Chart-shape read for the timing advisory. {} on any failure — a bad structure read
    degrades the chip to momentum-only, it must never break a scan."""
    if not getattr(config, "STRUCTURE_ENABLED", True) or price_data is None:
        return {}
    try:
        from analysis.structure import detect_structure
        if price_data.empty:
            return {}
        d = price_data.tail(int(getattr(config, "STRUCTURE_LOOKBACK_DAYS", 180)))
        return detect_structure(
            highs=d["High"].tolist(), lows=d["Low"].tolist(), closes=d["Close"].tolist(),
            volumes=d["Volume"].tolist() if "Volume" in d else None,
            # Rich levels (clustered, strength-ranked) so "at support" agrees with
            # nearest_support rather than answering from the raw price list.
            supports=(tech or {}).get("support_levels") or (tech or {}).get("supports"),
            resistances=(tech or {}).get("resistance_levels") or (tech or {}).get("resistances"))
    except Exception as e:
        logger.warning("[structure] read failed: %s", e)
        return {}


def _vol_surface(ticker: str, price: float, calls, puts, strategy_key: str) -> Dict:
    """Term structure + skew for the call-side strategies.

    main.screen_ticker read the surface for bull puts only, so bear calls and condors were
    scored with NO term-structure component while bull puts took the full +5/-8 adjustment.
    On the 2026-08-05 20:52 board that put ADBE at a -5 penalty and WMT at none purely
    because WMT was never measured — two strategies ranked against each other on different
    bases. {} on any failure; the read is advisory.
    """
    out: Dict = {}
    if not getattr(config, "TERM_STRUCTURE_ENABLED", True):
        return out
    try:
        from data import fetcher
        from analysis.vol_surface import get_term_structure, get_skew_depth
        by_exp = fetcher.get_chain_by_expiry(ticker)
        if by_exp:
            out["term"] = get_term_structure(by_exp, price)
        if getattr(config, "SKEW_SCORING_ENABLED", True) and calls and puts:
            out["skew"] = get_skew_depth(puts, calls, price, strategy=strategy_key)
    except Exception as e:
        logger.warning("[vol_surface] read failed for %s: %s", ticker, e)
    return out


def _timing(strategy_key: str, tech: Dict, price: float, structure: Optional[Dict] = None) -> Dict:
    """Advisory pattern-phase assessment. Returns {} when disabled or on any failure, which
    strategies.evaluate() treats as "no timing row" — it must never break a scan."""
    if not getattr(config, "ENTRY_TIMING_ENABLED", True):
        return {}
    try:
        from analysis.entry_timing import assess_entry_timing
        return assess_entry_timing(strategy=strategy_key, tech=tech, current_price=price,
                                   structure=structure)
    except Exception as e:
        logger.warning("[timing] entry_timing assessment failed for %s: %s", strategy_key, e)
        return {}


def _tradeable(o: Dict) -> bool:
    return (o.get("mid", 0) or 0) > 0 and ((o.get("volume", 0) or 0) >= 1 or (o.get("open_interest", 0) or 0) >= 10)


def _pick_long(chain: List[Dict], short: Dict, direction: str) -> Optional[Dict]:
    """The long leg that maximises the FILLABLE credit-to-width, not the nearest strike.

    This returned the closest strike, which is the narrowest spread and therefore usually the
    worst credit-to-width available — and on an illiquid wing the long leg's own bid-ask can
    swallow more than the extra width earns. Same rule the bull-put sweep uses.
    """
    ks = short["strike"]; exp = short.get("expiration")
    if direction == "call":   # long strike ABOVE short (bear call)
        cands = [o for o in chain if o.get("expiration") == exp and 0 < (o["strike"] - ks) <= MAXW and _tradeable(o)]
    else:                     # long BELOW short (bull put)
        cands = [o for o in chain if o.get("expiration") == exp and 0 < (ks - o["strike"]) <= MAXW and _tradeable(o)]
    if not cands:
        return None
    from analysis.assessment import fill_basis
    def _ctw(o):
        w = abs(o["strike"] - ks)
        return fill_basis(short, o, w)["natural_credit_to_width"] if w else -1.0
    return max(cands, key=_ctw)


def _fill_ctw(short: Dict, long_: Dict, width: float) -> float:
    """Fillable credit-to-width for one wing — the number the sweep ranks and gates on."""
    from analysis.assessment import fill_basis
    return fill_basis(short, long_, width)["natural_credit_to_width"] if width else -1.0


def _best_wing(chain: List[Dict], lo: float, hi: float, side: str,
               levels: Optional[List[Dict]] = None):
    """The richest FILLABLE wing in the delta band, preferring one sheltered by a real level.

    Replaces _pick_short, which chose ONE strike by nearness to a delta target before anything
    had priced the spread. That is the same search-time preference the bull-put path gave up:
    it pre-empts the gates and the edge score, and on the call side it could hand back a strike
    whose spread is a debit once the bid-ask is crossed.

    Now every strike in the band is paired and priced, and the winner is the best fillable
    credit-to-width. The structural preference survives as a TIE-BREAK inside a tolerance band,
    exactly as select_bull_put_pair does it: selling under a level the market has defended more
    than once is real edge, and it should never cost meaningful credit or empty the board on
    names where no level happens to sit in the right place.

    `side` is REQUIRED and structural: it decides whether the long leg sits above the short
    (bear call) or below it (bull put). It used to default to "", which sent _pick_long hunting
    in the wrong direction and returned no wing at all — a silent empty board rather than an
    error.

    Returns (short, long, width) or (None, None, 0).
    """
    viable = []
    for sh in [o for o in chain if _tradeable(o) and lo <= abs(o.get("delta") or 0) <= hi]:
        lg = _pick_long(chain, sh, side)
        if not lg:
            continue
        w = abs(lg["strike"] - sh["strike"])
        ctw = _fill_ctw(sh, lg, w)
        if w > 0 and ctw > 0:
            viable.append((ctw, sh, lg, w))
    if not viable:
        return None, None, 0.0
    viable.sort(key=lambda x: -x[0])
    best_ctw = viable[0][0]

    if getattr(config, "LEVEL_AWARE_STRIKES", True) and levels and side:
        try:
            from analysis.levels import strike_cushion
            tol = float(getattr(config, "LEVEL_STRIKE_ROC_TOLERANCE", 0.10))
            min_buf = float(getattr(config, "LEVEL_MIN_BUFFER_PCT", 0.005))
            near = [v for v in viable if v[0] >= best_ctw * (1.0 - tol)]
            sheltered = [
                (strike_cushion(v[1].get("strike"), levels, side, min_buffer_pct=min_buf), v)
                for v in near]
            sheltered = [(c, v) for c, v in sheltered if c]
            if sheltered:
                c, v = max(sheltered, key=lambda cv: cv[0]["strength"])
                return v[1], v[2], v[3]
        except Exception as e:
            logger.warning("[levels] wing shelter preference skipped: %s", e)

    _, sh, lg, w = viable[0]
    return sh, lg, w


def _pop_below(dist_pct: float, dte: int, prices) -> Dict:
    """P(price ends BELOW a level dist_pct ABOVE spot) — measured directly, never mirrored."""
    return edge_calculator.calculate_pop_between(dte, prices, upper_move_pct=dist_pct)


def _pop_between(down_pct: float, up_pct: float, dte: int, prices) -> Dict:
    """P(price ends inside the band). down_pct/up_pct are POSITIVE distances from spot."""
    return edge_calculator.calculate_pop_between(dte, prices, lower_move_pct=-down_pct, upper_move_pct=up_pct)


def _edge_score(ticker, strategy, tech, vrp_pct, true_pop, implied_pop, sentiment, earnings_days,
                vol_surface=None):
    try:
        ep = edge_calculator.calculate_edge_points(true_pop, implied_pop).get("edge_points", 0)
    except Exception:
        ep = (true_pop - implied_pop) * 100 if (true_pop and implied_pop) else 0
    try:
        es = edge_calculator.calculate_edge_score(
            # data/technicals.py emits `composite_score`; this read `technical_score`, which
            # does not exist, so it silently fell back to the hardcoded 50 for every bear
            # call and iron condor since the beta build. Chart quality — trend, RSI, MAs and
            # the support-shelter check — never reached call-side edge scores at all.
            ticker=ticker, strategy=strategy,
            technical_score=tech.get("composite_score", tech.get("technical_score", 50)) or 50,
            vrp_pct=vrp_pct or 0, edge_points=ep, news_sentiment=(sentiment or "NEUTRAL"),
            earnings_days_away=earnings_days if earnings_days is not None else 99,
            fundamentals_score=tech.get("fundamentals_score"),
            # compute_skew_score has had per-strategy handling for bear_call (calls-rich is
            # favorable) and iron_condor (either wing) since the beta build, but this path never
            # passed the raw value — so those trades silently scored 0 on a 15-point component.
            skew_raw=tech.get("skew_vol_pts"),
            # Same term-structure basis the bull-put path uses, so strategies are
            # ranked against each other on equal footing.
            term_slope=((vol_surface or {}).get("term") or {}).get("slope"),
            event_expiry_flag=bool(((vol_surface or {}).get("term") or {}).get("event_expiry")))
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
        "nearest_resistance": tech.get("nearest_resistance"),
        # Same level payload the bull-put path emits, so the cockpit's Key levels panel is
        # not silently blank on every call-side trade.
        "support_levels": tech.get("support_levels", []),
        "resistance_levels": tech.get("resistance_levels", []),
        "news_sentiment": sentiment, "news_summary": tech.get("news_summary"),
        "fundamentals_score": tech.get("fundamentals_score"),
        # Emit the key unconditionally (None when unavailable) so every strategy's rows have the
        # same shape — the cockpit and any "is the field present?" check treat an absent key and
        # a null value very differently.
        "skew_vol_pts": tech.get("skew_vol_pts"),
        "true_pop_drift_mode": "risk_free", "estimated_round_trip_cost_per_contract":
            float(getattr(config, "COMMISSION_PER_CONTRACT_PER_LEG", 0.65)) * 4 + 4.0,
        "needs_validation": True, "warnings": [],
    }


def _surface_fields(vs: Optional[Dict]) -> Dict:
    """Same surface keys the bull-put path emits, so the cockpit and any consumer
    see one shape across every strategy."""
    term = (vs or {}).get("term") or {}
    skew = (vs or {}).get("skew") or {}
    return {
        "term_structure": term, "term_slope": term.get("slope", "unknown"),
        "term_spread_pts": term.get("term_spread_pts"),
        "term_confidence": term.get("confidence"),
        "event_expiry_date": term.get("event_expiry"),
        "event_expiry_flag": bool(term.get("event_expiry")),
        "skew_depth": skew, "skew_steepness": skew.get("skew_steepness", "unknown"),
        "skew_20d": skew.get("skew_20d"), "skew_40d": skew.get("skew_40d"),
    }


def build_bear_call(ticker, price, calls, prices_hist, tech, sentiment, earnings_days=None,
                    structure=None, vol_surface=None) -> Optional[Dict]:
    # tech carries support_levels / resistance_levels from analysis/levels.py.
    short, long_, width = _best_wing(calls, 0.16, 0.30, "call",
                                     (tech or {}).get("resistance_levels"))
    if not short or not long_:
        return None
    # THE FILLABLE CREDIT. This used to be short.mid - long.mid, and a credit spread cannot be
    # filled at the mid: you sell the short at its BID and buy the long at its ASK. The bull-put
    # path was corrected on 2026-08-07 after GDX opened twice for $9 and $7 of real credit
    # against a $19 floor; bear calls and condors were never touched, so they carried the same
    # defect and then became the whole board once the put side started pricing honestly.
    # One definition, in analysis.assessment.fill_basis, which also falls back to a modelled
    # fill when quotes are stale — after the close the book is unmaintained and the natural
    # credit stops meaning anything.
    from analysis.assessment import fill_basis as _fill
    _fb = _fill(short, long_, width)
    mid_ps = round((short.get("mid", 0) or 0) - (long_.get("mid", 0) or 0), 2)
    credit_ps = _fb["natural_credit_per_share"]
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
    es, comp = _edge_score(ticker, "bear_call_spread", tech, tech.get("vrp"), p_max_profit,
                           implied, sentiment, earnings_days, vol_surface)
    entry_timing = _timing("bear_call", tech, price, structure)
    ctx = {"dte": dte, "short_delta": short.get("delta"),
           "credit_to_width": credit_ps / width, "iv_rank": tech.get("iv_rank"),
           "trend": tech.get("trend"), "pop": true_pop, "sentiment": sentiment,
           "entry_timing": entry_timing}
    ev = strategies.evaluate("bear_call", ctx)
    if not ev["qualified"]:
        return None
    t = _base(ticker, "bear_call", price, tech, sentiment, dte, short.get("last_trade_date") or short.get("expiration"))
    t.update({
        "short_strike": short["strike"], "long_strike": long_["strike"], "credit_per_share": credit_ps,
        "credit_usd": credit_usd, "max_loss_usd": max_loss, "delta": short.get("delta"),
        # credit_per_share above IS the natural (fillable) credit. The mid is kept beside it
        # because the difference between them is the execution cost, and a board that shows
        # only one of the two is either flattering the trade or hiding what it costs to get in.
        "natural_credit_per_share": credit_ps, "natural_credit_usd": credit_usd,
        "natural_credit_to_width": round(credit_ps / width, 4) if width else 0,
        "mid_credit_per_share": mid_ps, "mid_credit_usd": round(mid_ps * 100, 0),
        "fill_basis": _fb["fill_basis"], "quotes_live": _fb["quotes_live"],
        "short_bid": short.get("bid"), "short_ask": short.get("ask"),
        "long_bid": long_.get("bid"), "long_ask": long_.get("ask"), "width": width,
        "credit_to_width_pct": round(credit_ps / width * 100, 1), "true_pop": true_pop,
        "p_max_profit": p_max_profit, "breakeven": round(be, 2),
        "true_pop_confidence": pr_res.get("confidence", "LOW"),
        "true_pop_windows": pr_res.get("independent_windows"),
        "implied_pop": round(implied, 3), "edge_score": es,
        "component_breakdown": comp, "skew_score": (comp or {}).get("skew", 0),
        **_surface_fields(vol_surface),
        "auto_reasoning": f"Bear call: {ev['news_check']['detail']}.",
        "criteria": ev["criteria"], "news_check": ev["news_check"],
        "entry_timing": entry_timing,
    })
    return t


def build_iron_condor(ticker, price, calls, puts, prices_hist, tech, sentiment, earnings_days=None,
                      structure=None, vol_surface=None) -> Optional[Dict]:
    _res = (tech or {}).get("resistance_levels")
    _sup = (tech or {}).get("support_levels")
    # BOTH wings swept and priced. A condor crosses four bid-ask spreads, so a wing chosen on
    # delta alone can quietly turn the whole structure into a debit.
    cs, cl, wcall_ = _best_wing(calls, 0.12, 0.22, "call", _res)
    ps, pl, wput_ = _best_wing(puts, 0.12, 0.22, "put", _sup)
    if not cs or not ps or not cl or not pl:
        return None
    wcall = abs(cl["strike"] - cs["strike"]); wput = abs(ps["strike"] - pl["strike"])
    width = max(wcall, wput)
    # A condor is two verticals, so it crosses FOUR bid-ask spreads — roughly double a
    # vertical's execution cost, and the structure where pricing on mids flatters the credit
    # most. Each wing is priced the way it fills: sell the short at its bid, buy the long at
    # its ask.
    from analysis.assessment import fill_basis as _fill
    _fbc, _fbp = _fill(cs, cl, wcall), _fill(ps, pl, wput)
    _fb = _fbc
    mid_ps = round((cs["mid"] - cl["mid"]) + (ps["mid"] - pl["mid"]), 2)
    credit_ps = round(_fbc["natural_credit_per_share"]
                      + _fbp["natural_credit_per_share"], 2)
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
    es, comp = _edge_score(ticker, "iron_condor", tech, tech.get("vrp"), p_max_profit,
                           implied, sentiment, earnings_days, vol_surface)
    entry_timing = _timing("iron_condor", tech, price, structure)
    ctx = {"dte": dte, "short_delta": cs.get("delta"), "credit_to_width": credit_ps / width,
           "iv_rank": tech.get("iv_rank"), "trend": tech.get("trend"), "pop": true_pop,
           "sentiment": sentiment, "entry_timing": entry_timing}
    ev = strategies.evaluate("iron_condor", ctx)
    if not ev["qualified"]:
        return None
    t = _base(ticker, "iron_condor", price, tech, sentiment, dte, cs.get("last_trade_date") or cs.get("expiration"))
    t.update({
        "put_short_strike": ps["strike"], "put_long_strike": pl["strike"],
        "call_short_strike": cs["strike"], "call_long_strike": cl["strike"],
        "credit_per_share": credit_ps, "credit_usd": credit_usd, "max_loss_usd": max_loss,
        "natural_credit_per_share": credit_ps, "natural_credit_usd": credit_usd,
        "natural_credit_to_width": round(credit_ps / width, 4) if width else 0,
        "mid_credit_per_share": mid_ps, "mid_credit_usd": round(mid_ps * 100, 0),
        "fill_basis": _fb["fill_basis"], "quotes_live": _fb["quotes_live"],
        "call_short_bid": cs.get("bid"), "call_long_ask": cl.get("ask"),
        "put_short_bid": ps.get("bid"), "put_long_ask": pl.get("ask"), "width": width,
        "delta": round((abs(cs.get("delta") or 0) - abs(ps.get("delta") or 0)), 3),
        "credit_to_width_pct": round(credit_ps / width * 100, 1),
        "true_pop": true_pop, "p_max_profit": p_max_profit,
        "breakeven_lower": round(lower_be, 2), "breakeven_upper": round(upper_be, 2),
        "true_pop_confidence": pr_res.get("confidence", "LOW"),
        "true_pop_windows": pr_res.get("independent_windows"),
        "implied_pop": round(implied, 3), "edge_score": es,
        "component_breakdown": comp, "skew_score": (comp or {}).get("skew", 0),
        **_surface_fields(vol_surface),
        "auto_reasoning": f"Iron condor: {ev['news_check']['detail']}.",
        "criteria": ev["criteria"], "news_check": ev["news_check"],
        "entry_timing": entry_timing,
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
        if tech.get("skew_vol_pts") is None and getattr(config, "SKEW_SCORING_ENABLED", True):
            # Same source and config gate as the bull-put path in main.screen_ticker.
            try:
                tech["skew_vol_pts"] = (fetcher.get_options_skew(
                    ticker, config.MIN_DTE, config.MAX_DTE) or {}).get("skew_vol_pts")
            except Exception as e:
                logger.debug(f"[multi_strategy] {ticker}: skew check skipped: {e}")
        sentiment = (sentiment_map.get(ticker, {}) or {}).get("sentiment", "NEUTRAL")
        # Earnings. This was hardcoded to None, which _edge_score turned into
        # earnings_days_away=99 — so every bear call and iron condor was scored as though
        # earnings were three months away, took the full earnings_safety bonus, and could
        # never trip the blackout regardless of the real date. The auto-open path is
        # protected by vega_candidates._earnings_clear, but anything surfaced on the board
        # and logged by hand had no gate at all.
        earnings_days = None
        try:
            from data import fundamentals as _fund
            earnings_days = _fund.days_until_earnings(fetcher.get_earnings_date(ticker))
        except Exception as e:
            logger.debug(f"[multi_strategy] {ticker}: earnings lookup failed: {e}")
        structure = _structure(price_data, tech)
        vsurf = _vol_surface(ticker, price, calls, puts, "bear_call_spread")
        if want_bc and calls:
            t = build_bear_call(ticker, price, calls, prices_hist, tech, sentiment, earnings_days,
                                structure, vsurf)
            if t:
                out.append(t)
        if want_ic and calls and puts:
            t = build_iron_condor(ticker, price, calls, puts, prices_hist, tech, sentiment,
                                  earnings_days, structure, vsurf)
            if t:
                out.append(t)
    except Exception as e:
        logger.warning(f"[multi_strategy] {ticker}: {e}")
    return out
