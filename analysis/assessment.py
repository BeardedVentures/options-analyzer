#!/usr/bin/env python3
"""
assessment.py — the single definition of "is this a good trade, and why".

VEGA grew two engines. main.screen_ticker composed levels, horizon, structure, entry timing,
the volatility surface and the edge score into a rich judgement, and wrote it to the cockpit.
vega_candidates applied numeric gates and wrote the file the auto-trader opens from. The
result was a system that showed the operator one standard and traded on another: on
2026-08-07 the board displayed expected-move calibration while the robot opened GDX twice
into open air on delta alone.

Two engines also meant the hard gates were implemented twice against the same config
constants — the precise shape behind four enforcement leaks (IV rank, POP floor, quote
spread, and the mid/natural credit basis).

This module is the one place that answers the question. Both paths call it, so a rule added
here is enforced everywhere by construction rather than by discipline.

    load_context(ticker, ...)   fetch everything a ticker needs ONCE
    evaluate_gates(spread, ctx) the hard contract — REQUIRED_GATES, one implementation
    assess(spread, ctx)         gates + analysis + score + plain-English narrative

Design constraints, in priority order:
  1. One definition. No rule may exist in two places.
  2. One fetch per ticker. Spreads on the same underlying share market context; the previous
     arrangement re-fetched price history and chains per path, per scan.
  3. Degrade, never break. Any analysis layer that fails leaves its section empty and the
     trade is still gated on what IS known — advisory signals must not be able to take the
     scan down.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# Strategy identity is carried on the assessment so downstream code never has to infer it
# from strike geometry, which is how a bear call ends up scored as a bull put.
BULL_PUT = "bull_put_spread"
BEAR_CALL = "bear_call_spread"
IRON_CONDOR = "iron_condor"


def _cfg(name, default):
    return getattr(config, name, default)


# ── Market context: fetched once per ticker ─────────────────────────────────────
def load_context(ticker: str, price_data=None, puts=None, calls=None,
                 tech: Optional[Dict] = None, sentiment: Optional[str] = None,
                 earnings_days: Optional[int] = None,
                 with_surface: bool = False,
                 has_earnings: Optional[bool] = None) -> Dict:
    """Everything an assessment needs for one underlying, resolved once.

    TWO PHASES, deliberately. This loads only what the GATES need — price history for levels,
    and the chain already in hand. The volatility surface is left out because it costs two
    further chain fetches per ticker (a wide 5-120 DTE pull plus the call side) and gates
    nothing: it is advisory. Loading it for all 56 watchlist tickers took the scan from two
    API calls per ticker to five and pushed a ~3 minute scan past four, to analyse candidates
    that were about to be rejected anyway.

    Call enrich_surface() on the survivors instead — roughly nine tickers rather than
    fifty-six. Analysis follows selection; it does not precede it.

    Every field is optional and independently recoverable. A missing layer narrows what can
    be judged; it never prevents a judgement.
    """
    from data import fetcher

    ctx: Dict = {"ticker": ticker, "errors": []}

    def _try(label, fn):
        try:
            return fn()
        except Exception as e:                       # pragma: no cover - defensive
            ctx["errors"].append(f"{label}: {e}")
            logger.debug("[assessment] %s failed for %s: %s", label, ticker, e)
            return None

    if price_data is None:
        price_data = _try("price_data", lambda: fetcher.get_price_data(ticker, period="1y"))
    ctx["price_data"] = price_data
    if price_data is not None and not getattr(price_data, "empty", True):
        ctx["spot"] = float(price_data["Close"].iloc[-1])
        ctx["highs"] = [float(x) for x in price_data["High"].tolist()]
        ctx["lows"] = [float(x) for x in price_data["Low"].tolist()]
        ctx["closes"] = [float(x) for x in price_data["Close"].tolist()]
        ctx["volumes"] = ([float(x) for x in price_data["Volume"].tolist()]
                          if "Volume" in price_data else [])
    else:
        ctx["spot"] = None

    ctx["puts"] = puts
    ctx["calls"] = calls
    ctx["tech"] = tech or {}
    ctx["sentiment"] = sentiment
    ctx["earnings_days"] = earnings_days
    # Does this underlying report at all? DECLARED knowledge, not observation — an ETF's
    # absence of earnings is a fact about the world, and ticker_profile.declared already
    # returns None (not False) when it does not know, so "unknown" survives the trip.
    # The earnings gate fails closed on an unknown DATE; without this it would also fail
    # closed on every index ETF, which have no date to know.
    if has_earnings is None:
        try:
            from analysis.ticker_profile import declared
            has_earnings = declared(ticker).get("has_earnings")
        except Exception as e:                       # pragma: no cover - defensive
            logger.debug("[assessment] declared() failed for %s: %s", ticker, e)
    ctx["has_earnings"] = has_earnings
    # Instrumentation for the earnings-gap audit: a passing gate must be distinguishable
    # between "confirmed clear" and "we never found out". Without this the audit cannot be
    # run even retroactively — the 2026-08-10 snapshot carries no such record for any of its
    # 160 candidates, so a True result there is uninterpretable.
    ctx["earnings_source"] = ("declared_none" if has_earnings is False
                              else "lookup" if earnings_days is not None
                              else "unknown")

    if ctx.get("closes"):
        ctx["levels"] = _try("levels", lambda: __import__(
            "analysis.levels", fromlist=["find_levels"]).find_levels(
            ctx["highs"], ctx["lows"], ctx["closes"])) or {}
        ctx["structure"] = _try("structure", lambda: __import__(
            "analysis.structure", fromlist=["detect_structure"]).detect_structure(
            ctx["highs"], ctx["lows"], ctx["closes"], ctx["volumes"],
            supports=(ctx.get("levels") or {}).get("support_levels"),
            resistances=(ctx.get("levels") or {}).get("resistance_levels"))) or {}
    else:
        ctx["levels"], ctx["structure"] = {}, {}

    if with_surface and _cfg("TERM_STRUCTURE_ENABLED", True):
        by_exp = _try("chain_by_expiry", lambda: fetcher.get_chain_by_expiry(ticker))
        if by_exp and ctx.get("spot"):
            ctx["term_structure"] = _try("term_structure", lambda: __import__(
                "analysis.vol_surface", fromlist=["get_term_structure"]).get_term_structure(
                by_exp, ctx["spot"])) or {}
        if puts and calls and ctx.get("spot"):
            ctx["skew"] = _try("skew", lambda: __import__(
                "analysis.vol_surface", fromlist=["get_skew_depth"]).get_skew_depth(
                puts, calls, ctx["spot"], BULL_PUT)) or {}
    ctx.setdefault("term_structure", {})
    ctx.setdefault("skew", {})
    return ctx


def enrich_surface(ctx: Dict, calls=None) -> Dict:
    """Add the volatility surface to a context that already passed the gates.

    Separated from load_context so the expensive reads happen once per SURVIVING ticker
    rather than once per scanned ticker. Idempotent and safe to call repeatedly.
    """
    if ctx.get("_surface_loaded") or not _cfg("TERM_STRUCTURE_ENABLED", True):
        return ctx
    from data import fetcher
    ticker, spot = ctx.get("ticker"), ctx.get("spot")
    if not (ticker and spot):
        return ctx
    try:
        by_exp = fetcher.get_chain_by_expiry(ticker)
        if by_exp:
            from analysis.vol_surface import get_term_structure
            ctx["term_structure"] = get_term_structure(by_exp, spot) or {}
    except Exception as e:
        ctx.setdefault("errors", []).append(f"term_structure: {e}")
    if _cfg("SKEW_SCORING_ENABLED", True):
        try:
            calls = calls or ctx.get("calls") or fetcher.get_call_options_chain(
                ticker, _cfg("MIN_DTE", 25), _cfg("MAX_DTE", 45))
            if calls and ctx.get("puts"):
                from analysis.vol_surface import get_skew_depth
                ctx["calls"] = calls
                ctx["skew"] = get_skew_depth(ctx["puts"], calls, spot, BULL_PUT) or {}
        except Exception as e:
            ctx.setdefault("errors", []).append(f"skew: {e}")
    ctx["_surface_loaded"] = True
    return ctx


# ── The hard contract ───────────────────────────────────────────────────────────
def _otm_buffer_ok(ticker: str, spot: float, short_strike: float, side: str) -> bool:
    """Minimum distance between spot and the short strike, per the SPY-vs-stock rule."""
    if not spot or not short_strike:
        return False
    if ticker.upper() in _cfg("SPY_BUFFER_TICKERS", set()):
        gap = (spot - short_strike) if side == "put" else (short_strike - spot)
        return gap >= float(_cfg("MIN_STRIKE_BUFFER_SPY", 10.0))
    pct = ((spot - short_strike) / spot) if side == "put" else ((short_strike - spot) / spot)
    return pct >= float(_cfg("MIN_STRIKE_BUFFER_STOCK", 0.05))


def _ticker_profile(ctx: Dict) -> Dict:
    """Per-underlying character. Never raises — a missing profile narrows the narrative, it
    does not stop an assessment."""
    try:
        from analysis import ticker_profile
        pd_ = ctx.get("price_data")
        close = pd_["Close"] if pd_ is not None and not getattr(pd_, "empty", True) else None
        return ticker_profile.profile(ctx.get("ticker") or "", close)
    except Exception as e:
        logger.debug("[assessment] ticker profile failed: %s", e)
        return {}


def _btc_cross_venue(ctx: Dict) -> Dict:
    """DVOL-versus-ETF-IV for a BTC tracker. Costs one cached HTTP read, and only for the
    handful of tickers in BTC_PROXY_TICKERS — the other fifty-odd names never touch it.

    Fails to an `available: False` dict rather than raising: a crypto endpoint being down must
    narrow what can be said about IBIT, never stop the equity scan that shares this function.
    """
    ticker = ctx.get("ticker") or ""
    try:
        from analysis import btc_signal
        if not btc_signal.applies_to(ticker):
            return {"available": False, "reading": "not_applicable"}

        iv = ctx.get("atm_iv")
        if iv is None and ctx.get("puts") and ctx.get("spot"):
            from data import technicals
            iv = technicals.estimate_atm_iv(ctx["puts"], ctx["spot"])
            ctx["atm_iv"] = iv

        from data import crypto
        return btc_signal.evaluate(ticker, iv, crypto.snapshot())
    except Exception as e:
        logger.debug("[assessment] btc cross-venue failed for %s: %s", ticker, e)
        return {"available": False, "reading": "unavailable", "note": str(e)[:120]}


def natural_credit(short_leg: Dict, long_leg: Dict, width: float) -> Dict:
    """The credit a fill actually pays: sell the short leg at its BID, buy the long at its ASK.

    THE one definition. A credit spread cannot be filled at the mid — you cross the spread on
    both legs — and the gap is not small: across the 2026-07-31 snapshot the mid overstated the
    achievable credit by 75.5% on average, $92.33 against $21.85. Every trade in the ledger
    booked at a mid is priced at a number that was never available.

    The damage is not only in the P&L. `min_credit_usd` and `credit_to_width` are the two gates
    that decide whether a spread pays enough to be worth its risk, so gating them on the mid
    passes spreads that are dead at inception. GDX 82/81 opened twice on 2026-08-07 for $9 and
    $7 of real credit against a $19 floor, because the mid said $31 and $29, and both were
    stopped out inside the same cycle.

    Returns zeros — never None — when a leg is unquotable, so the caller's gates see a credit
    that cannot clear a floor rather than a missing key that might default to passing.
    """
    def _f(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    per_share = round(_f(short_leg.get("bid")) - _f(long_leg.get("ask")), 4)
    w = _f(width)
    return {
        "natural_credit_per_share": per_share,
        "natural_credit_usd": round(per_share * 100, 2),
        "natural_credit_to_width": round(per_share / w, 4) if w > 0 else 0.0,
    }


def quotes_are_live(now=None) -> bool:
    """Are option quotes currently being made, or are we reading yesterday's last prints?

    US equity options trade 09:30-16:00 ET. Outside that the book is not being maintained and
    the bid-ask blows out: measured on GOOG 335/330 on 2026-08-10, the short leg's spread went
    from $0.25 at 14:47 to $0.90 at 18:03 and the long's from $0.20 to $0.60. The fillable
    credit on that one spread fell from $100 to $30 with no change in the underlying.

    That matters because the gates are now enforced on the fillable credit. A scan run after
    the close would reject every candidate on the watchlist and report an empty board as
    though the market were paying nothing.
    """
    try:
        from zoneinfo import ZoneInfo
        now = now or datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        try:
            import pytz
            now = now or datetime.now(pytz.timezone("America/New_York"))
        except Exception:
            return False        # cannot tell the time zone: assume stale, never assume fresh
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm < (16 * 60)


def fill_basis(short_leg: Dict, long_leg: Dict, width: float,
               live: Optional[bool] = None) -> Dict:
    """The credit to gate and rank on, and an honest label for where it came from.

    LIVE quotes -> the natural credit, which is what a fill actually pays.

    STALE quotes -> a MODELLED credit, because the natural computed from an unmaintained book
    is not a conservative estimate, it is a meaningless one. The model is the mid haircut by
    MODELLED_FILL_RATIO, and that ratio is measured rather than assumed: across the 158
    positively-priced candidates in the 2026-08-10 14:47 intraday scan, the natural credit ran
    a median 78% of the mid.

    The modelled number is never presented as fillable. Callers carry `fill_basis` through to
    the board so an after-hours read is marked provisional instead of quietly passing for a
    live one — the whole failure this exists to prevent is a price that looks executable and
    is not.

    Re-measure the ratio as the ledger grows; a single global haircut is a textbook constant of
    exactly the kind this codebase keeps finding, and it is only defensible while it is
    explicitly provisional and explicitly derived from data.
    """
    if live is None:
        live = quotes_are_live()
    nat = natural_credit(short_leg, long_leg, width)

    def _f(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    mid_ps = round(_f(short_leg.get("mid")) - _f(long_leg.get("mid")), 4)
    w = _f(width)
    if live:
        out = dict(nat)
        out.update({"fill_basis": "natural", "quotes_live": True})
    else:
        ratio = float(_cfg("MODELLED_FILL_RATIO", 0.78))
        ps = round(mid_ps * ratio, 4)
        out = {
            "natural_credit_per_share": ps,
            "natural_credit_usd": round(ps * 100, 2),
            "natural_credit_to_width": round(ps / w, 4) if w > 0 else 0.0,
            "fill_basis": "modelled", "quotes_live": False,
        }
    # Both raw measurements travel with the decision either way, so a later audit can ask
    # whether a call was wrong because the model was wrong or because the inputs were.
    out["observed_natural_per_share"] = nat["natural_credit_per_share"]
    out["mid_credit_per_share"] = mid_ps
    return out


def _min_credit_floor(spot) -> float:
    """The price-scaled credit floor. Delegates to config so the rule has one definition —
    see config.min_credit_usd_for. Falls back to the flat floor if config predates it."""
    fn = getattr(config, "min_credit_usd_for", None)
    if callable(fn):
        return fn(spot)
    return float(_cfg("MIN_CREDIT_USD", 25))


def _liquidity_ok(leg: Dict) -> bool:
    return ((leg.get("volume") or 0) >= _cfg("MIN_OPTION_VOLUME", 100)
            or (leg.get("open_interest") or 0) >= _cfg("MIN_OPTION_OPEN_INTEREST", 500))


def _quote_spread_ok(leg: Dict) -> bool:
    bid, ask, mid = (leg.get("bid") or 0), (leg.get("ask") or 0), (leg.get("mid") or 0)
    if bid <= 0 or ask <= 0 or mid <= 0 or ask < bid:
        return False
    return ((ask - bid) / mid) <= float(_cfg("MAX_QUOTE_SPREAD_PCT", 0.35))


def leg_quote(spread: Dict, side: str) -> Dict:
    """One leg's quote, however the caller happens to carry it. `side` is "short" or "long".

    Two shapes are both real. build_candidates FLATTENS the quotes onto the candidate
    (short_bid/short_ask/long_bid/…) and pops `short_leg` before serialising, because a leg
    object is not JSON-safe — so a candidate read back from a snapshot has only the flat form.
    The engine path passes nested leg dicts. Normalising here means the rule is enforced
    identically regardless of which shape reached it, instead of quietly passing on the shape
    it does not recognise.

    The flat keys win when present, because they are what build_candidates writes last and what
    survives to disk; the nested dict is the fallback for callers that only have it.
    """
    bid, ask = spread.get(f"{side}_bid"), spread.get(f"{side}_ask")
    if bid is not None or ask is not None:
        mid = spread.get(f"{side}_mid")
        if mid is None and bid is not None and ask is not None:
            mid = (float(bid) + float(ask)) / 2
        return {"bid": bid, "ask": ask, "mid": mid}
    leg = spread.get(f"{side}_leg")
    return leg if isinstance(leg, dict) else {}


def _spread_quote_ok(spread: Dict) -> bool:
    """BOTH legs must quote inside MAX_QUOTE_SPREAD_PCT — checked per leg, not on the net.

    This rule existed twice, disagreeing: vega_candidates checked both legs, the shared
    contract checked only the short one, and the shared one is what gates the trade. The long
    leg is the one being BOUGHT, so a wide market on it comes straight out of the credit
    actually received and out of the exit. Two answers to one question is the shape behind the
    four enforcement leaks this module exists to close, so the stricter definition wins and the
    duplicate is deleted.

    An absent long leg is "not applicable" and passes; a long leg that is PRESENT but
    unquotable fails. Those are different facts and collapsing them is how a gate starts
    reporting safe because it cannot say unsafe.
    """
    if not _quote_spread_ok(leg_quote(spread, "short") or spread.get("short_leg") or {}):
        return False
    long_leg = leg_quote(spread, "long")
    if not long_leg:
        return True
    return _quote_spread_ok(long_leg)


def _shelter_ok(short_strike, levels: Dict, side: str) -> bool:
    """Is a level the market has defended standing between spot and the short strike?

    Fails OPEN when levels are unavailable. The earnings gate fails closed by design; this
    one cannot, because a level read depends on price history that is routinely thin and a
    data gap must not empty the board.
    """
    if not _cfg("SUPPORT_SHELTER_GATE_ENABLED", True):
        return True
    pool = ((levels or {}).get("support_levels") if side == "put"
            else (levels or {}).get("resistance_levels"))
    if not pool:
        return True
    try:
        from analysis.levels import strike_cushion
        return strike_cushion(float(short_strike), pool, side,
                              min_buffer_pct=float(_cfg("LEVEL_MIN_BUFFER_PCT", 0.005))) is not None
    except Exception:
        return True


def evaluate_gates(spread: Dict, ctx: Dict) -> Dict:
    """The REQUIRED_GATES contract, implemented exactly once.

    `spread` must carry: short_strike, dte, natural_credit_usd, natural_credit_to_width,
    pop, short_delta, short_leg, side. Returns {gate_key: bool} covering every key in
    config.REQUIRED_GATES, so a candidate can never reach the trade path missing one.
    """
    side = spread.get("side", "put")
    short_leg = spread.get("short_leg") or {}
    spot = ctx.get("spot")
    g = {
        "delta_cap": abs(float(spread.get("short_delta") or 0)) <= _cfg("SHORT_STRIKE_MAX_DELTA", 0.30),
        "otm_buffer": _otm_buffer_ok(ctx.get("ticker", ""), spot, spread.get("short_strike"), side),
        "credit_to_width": (spread.get("natural_credit_to_width") or 0) >= _cfg("MIN_CREDIT_TO_WIDTH_PCT", 0.15),
        "min_credit_usd": (spread.get("natural_credit_usd") or 0) >= _min_credit_floor(spot),
        "liquidity": _liquidity_ok(short_leg),
        "pop": (spread.get("pop") or 0) >= _cfg("MIN_PROBABILITY_OF_PROFIT", 0.72),
        "dte_window": _cfg("MIN_DTE", 25) <= (spread.get("dte") or 0) <= _cfg("MAX_DTE", 45),
        "quote_spread": _spread_quote_ok(spread),
        "natural_credit_positive": (spread.get("natural_credit_per_share") or 0) > 0,
        "earnings_clear": _earnings_clear(spread, ctx),
        "support_shelter": _shelter_ok(spread.get("short_strike"), ctx.get("levels") or {}, side),
    }
    # Fail loudly if the contract and this implementation drift apart. A gate silently
    # missing from the dict is how three enforcement leaks reached production.
    missing = [k for k in _cfg("REQUIRED_GATES", ()) if k not in g]
    if missing:
        raise AssertionError(f"evaluate_gates does not emit required gates: {missing}")
    return g


def _earnings_clear(spread: Dict, ctx: Dict) -> bool:
    """No earnings on or before expiry. Stricter than a blackout window: a spread held
    THROUGH a print is an event bet regardless of how far away the print was at entry.

    FAILS CLOSED on unknown, and the distinction that makes that affordable is
    `has_earnings`: a name DECLARED to have no earnings (SPY, QQQ, TLT, GLD, IBIT) passes
    without a lookup, so failing closed costs only the names that genuinely report.

    This used to `return True` when the date was unknown, with the note "the caller's own
    gate decides" — referring to vega_candidates.attach_earnings_gate, which was correct,
    ETF-aware, fail-closed, and covered by thirteen tests. When the gates consolidated here
    that function lost its last production caller and the live path silently inverted: the
    only surviving implementation passed a name it knew nothing about straight into a print.
    A green test suite over an unwired function is the same failure as a metric that cannot
    come out non-zero, which is why the reachability test in test_candidate_gates.py now
    asserts that every gate's implementation is one the live scan actually reaches.
    """
    if not _cfg("EARNINGS_GATE_ENABLED", True):
        return True
    # Declared to have no earnings at all — nothing to look up, nothing to fear.
    if ctx.get("has_earnings") is False:
        return True
    d = ctx.get("earnings_days")
    if d is None:
        # Unknown is not the same as clear. A missing date is a data gap, and skipping a
        # tradeable name for one cycle costs far less than selling premium into a print.
        return False
    dte = spread.get("dte")
    return not (dte is not None and 0 <= d <= dte)


# ── The full judgement ──────────────────────────────────────────────────────────
def assess(spread: Dict, ctx: Dict, strategy: str = BULL_PUT) -> Dict:
    """Gates, analysis and narrative for one spread.

    The analysis block is where the two engines diverged: it was computed for the cockpit and
    never for the trades. It is attached here so both paths carry it.
    """
    gates = evaluate_gates(spread, ctx)
    out: Dict = {
        "ticker": ctx.get("ticker"),
        "strategy": strategy,
        "gates": gates,
        "gates_passed": sum(1 for v in gates.values() if v),
        "gates_total": len(gates),
        "qualified": all(gates.values()),
        "failed_gates": [k for k, v in gates.items() if not v],
        "analysis": {},
        "narrative": "",
    }

    a = out["analysis"]
    levels = ctx.get("levels") or {}
    a["support_levels"] = (levels.get("support_levels") or [])[:3]
    a["resistance_levels"] = (levels.get("resistance_levels") or [])[:3]
    a["structure"] = ctx.get("structure") or {}
    a["term_structure"] = ctx.get("term_structure") or {}
    a["skew"] = ctx.get("skew") or {}

    # Cross-venue volatility, for BTC-tracking underlyings only. Lives HERE — in the shared
    # assessment — rather than in strategies.evaluate(), which only main.py calls: putting it
    # there would have left the fast scan and the auto-trader blind to it and re-opened the
    # two-engine divergence this module exists to close.
    #
    # It is deliberately not a gate. `qualified = all(gates.values())`, so a signal that never
    # enters the gates dict cannot block a trade whatever it reads — advisory by construction
    # rather than by remembering to set a flag.
    a["btc_cross_venue"] = _btc_cross_venue(ctx)

    # What this system knows about THIS underlying, as opposed to underlyings. The gates above
    # are textbook rules applied identically to 56 names; this is the place that says where a
    # textbook read of this particular ticker would be wrong. Advisory — see ticker_profile.
    a["profile"] = _ticker_profile(ctx)

    side = spread.get("side", "put")
    try:
        from analysis.horizon import calibrate
        a["horizon"] = calibrate(
            strategy=strategy, spot=ctx.get("spot"),
            iv=(spread.get("short_leg") or {}).get("iv") or spread.get("short_iv"),
            dte=spread.get("dte"), short_strike=spread.get("short_strike"),
            support_levels=levels.get("support_levels"),
            resistance_levels=levels.get("resistance_levels"),
            structure=ctx.get("structure"))
    except Exception as e:
        a["horizon"] = {}
        logger.debug("[assessment] horizon failed: %s", e)

    try:
        from analysis.entry_timing import assess_entry_timing
        tech = dict(ctx.get("tech") or {})
        tech.setdefault("nearest_support", levels.get("nearest_support"))
        tech.setdefault("nearest_resistance", levels.get("nearest_resistance"))
        a["entry_timing"] = assess_entry_timing(
            strategy=strategy, tech=tech, current_price=ctx.get("spot"),
            structure=ctx.get("structure"))
    except Exception as e:
        a["entry_timing"] = {}
        logger.debug("[assessment] entry_timing failed: %s", e)

    try:
        from analysis.levels import strike_cushion
        pool = (levels.get("support_levels") if side == "put"
                else levels.get("resistance_levels"))
        a["shelter"] = strike_cushion(
            float(spread["short_strike"]), pool or [], side,
            min_buffer_pct=float(_cfg("LEVEL_MIN_BUFFER_PCT", 0.005))) or {}
    except Exception:
        a["shelter"] = {}

    # ── Edge score ──
    # Ranking lived only in main.py, so the scanner sorted on a local heuristic while the
    # board sorted on the real score — two orderings of the same opportunity set. Computed
    # here when the probability inputs are present; skipped, not faked, when they are not.
    out["edge_score"] = None
    out["edge_components"] = {}
    try:
        true_pop = spread.get("true_pop")
        implied = spread.get("implied_pop")
        if implied is None and spread.get("short_delta") is not None:
            implied = 1 - abs(float(spread["short_delta"]))
        if true_pop is not None and implied is not None:
            from analysis import edge_calculator as ec
            ep = ec.calculate_edge_points(true_pop, implied).get("edge_points", 0)
            ts = ctx.get("term_structure") or {}
            _tech = ctx.get("tech") or {}
            # `tech` is filled by EITHER data.technicals.calculate_all (engine path) or
            # vega_candidates.vol_context (fast scan). They now share field names where they
            # describe the same quantity; `vrp_pp` is the fast scan's old spelling and is read
            # only so snapshots already on disk still score. Both are volatility POINTS, the
            # unit calculate_edge_score's thresholds (>=9, >=7, >=5) are written against.
            #
            # This read knew only "vrp" while the fast scan emitted only "vrp_pp", so the
            # largest component of the score — 30 of 100 — was zero on every auto-opened trade.
            es = ec.calculate_edge_score(
                ticker=ctx.get("ticker"), strategy=strategy,
                technical_score=_tech.get("composite_score", 50) or 50,
                vrp_pct=_tech.get("vrp") or _tech.get("vrp_pp") or 0,
                edge_points=ep,
                news_sentiment=(ctx.get("sentiment") or "NEUTRAL"),
                earnings_days_away=(ctx.get("earnings_days")
                                    if ctx.get("earnings_days") is not None else 99),
                fundamentals_score=(ctx.get("tech") or {}).get("fundamentals_score"),
                skew_raw=(ctx.get("tech") or {}).get("skew_vol_pts"),
                term_slope=ts.get("slope"),
                event_expiry_flag=bool(ts.get("event_expiry")))
            out["edge_score"] = es.get("total_score")
            out["edge_components"] = es.get("component_breakdown") or {}
            out["edge_points"] = ep
    except Exception as e:
        logger.debug("[assessment] edge score failed: %s", e)

    out["narrative"] = _narrate(out, spread, ctx)
    return out


def _narrate(out: Dict, spread: Dict, ctx: Dict) -> str:
    """One paragraph a person can act on. The same sentence the cockpit shows and the
    auto-trader logs, so a decision can be audited against the reasoning that produced it."""
    a = out["analysis"]
    bits: List[str] = []

    if out["qualified"]:
        bits.append(f"Passes all {out['gates_total']} gates.")
    else:
        bits.append(f"Blocked by {', '.join(out['failed_gates'])}.")

    sh = a.get("shelter") or {}
    if sh:
        bits.append(f"Short strike sits under {sh['level']:,.2f} "
                    f"({sh['touches']} touches, {sh['buffer_pct']*100:.1f}% buffer).")
    else:
        bits.append("No defended level stands above the short strike.")

    hz = a.get("horizon") or {}
    if hz.get("expected_move"):
        reach = hz.get("strike_reach", "unknown").replace("_", " ")
        bits.append(f"Over {hz.get('dte')} days the market prices ±${hz['expected_move']:,.2f}; "
                    f"the strike is {reach}.")

    bx = a.get("btc_cross_venue") or {}
    if bx.get("available"):
        bits.append(bx["note"])

    # The cautions are the part of the profile that changes a decision: each one names a place
    # where the generic rule and this specific asset disagree.
    for c in ((a.get("profile") or {}).get("cautions") or [])[:2]:
        bits.append(c)

    et = a.get("entry_timing") or {}
    if et.get("readiness"):
        bits.append(f"Timing {et['readiness'].lower()}"
                    + (f" — {et.get('headline','').lower()}" if et.get("headline") else "") + ".")

    ts = a.get("term_structure") or {}
    if ts.get("slope") and ts["slope"] != "unknown":
        if ts.get("event_expiry"):
            bits.append(f"A dated catalyst sits in the window ending {ts['event_expiry']}.")
        else:
            bits.append(f"IV term structure is {ts['slope']}.")
    return " ".join(bits)
