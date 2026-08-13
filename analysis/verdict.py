#!/usr/bin/env python3
"""verdict.py — the trade in words a person without a finance background can act on.

Everything VEGA computes is what premium sellers actually use: variance risk premium, the
probability gap against the market's own pricing, where the strike sits against defended
levels, and the directional exposure the position carries. None of that is dumbed down here.

What changes is that it gets SAID. The board could tell you "VRP +8.6pp, VEGA POP 82%, edge
score 74, delta 0.20" — every number correct, and none of it answering the question the reader
actually has, which is "is this good, and why". A realtor reading that page has no way in.

THE RULE THIS MODULE FOLLOWS: every sentence is generated from a number already computed
elsewhere. Nothing is asserted that the engine did not measure, and no separate narrative
logic can drift from the scores — if the grade and the sentences ever disagree it is because
the inputs disagree, which is a bug worth seeing rather than smoothing over.

The four things a reader needs, in the order they need them:
    1. Is this good?              one word and a 0-10, on the same scale as the KPI cards
    2. What am I actually doing?  the bet, in dollars and a date
    3. Why is it good?            the two or three reasons that carry the grade
    4. What could go wrong?       the risk, stated as plainly as the reward
"""
from __future__ import annotations

from typing import Dict, List, Optional

STRONG, GOOD, FAIR, WEAK, AVOID = "Strong", "Good", "Fair", "Weak", "Avoid"


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def grade(edge_score, vrp=None, pop_gap_pp=None) -> Dict:
    """A 0-10 and a word, on the same scale the KPI legend already teaches.

    Led by edge_score because that is the engine's own composite. The two overrides exist
    because a reader must never be shown an encouraging word over a trade the engine's own
    disqualifiers reject: negative VRP means the options are cheap relative to what the stock
    delivers, and a negative POP gap means VEGA rates it worse than the market does. Either
    caps the grade regardless of what the composite says.
    """
    es = _f(edge_score) or 0.0
    score = max(0.0, min(10.0, es / 10.0))
    v, g = _f(vrp), _f(pop_gap_pp)
    capped = None
    if v is not None and v < 0:
        score, capped = min(score, 3.5), "negative_vrp"
    if g is not None and g < 0:
        score, capped = min(score, 4.5), capped or "negative_pop_gap"
    word = (STRONG if score >= 8 else GOOD if score >= 7 else
            FAIR if score >= 5 else WEAK if score >= 3.5 else AVOID)
    return {"score": round(score, 1), "word": word, "capped_by": capped}


def the_bet(c: Dict) -> Optional[str]:
    """What the operator is actually doing, in dollars and a date. No jargon.

    A credit spread described as "short the 104 put, long the 103" is precise and useless to
    most readers. The same trade described as "you are paid $151 now to bet WMT stays above
    $104 until 18 Sept" is equally precise and immediately checkable against reality.
    """
    tk = c.get("ticker")
    credit = _f(c.get("credit_usd"))
    ml = _f(c.get("max_loss_usd"))
    dte = c.get("dte")
    exp = c.get("exp") or c.get("expiration")
    strat = (c.get("strat_type") or c.get("strategy") or "bull_put").lower()
    if not tk or credit is None:
        return None
    when = f"until {exp}" if exp else (f"for {dte} days" if dte else "until expiry")
    risk = f" The most you can lose is ${ml:,.0f}." if ml else ""
    if "condor" in strat:
        lo, hi = c.get("put_short"), c.get("call_short")
        if lo and hi:
            return (f"You are paid ${credit:,.0f} now to bet {tk} stays between "
                    f"${_f(lo):,.2f} and ${_f(hi):,.2f} {when}.{risk}")
        return f"You are paid ${credit:,.0f} now to bet {tk} stays in a range {when}.{risk}"
    if "bear_call" in strat or "bear call" in strat:
        k = _f(c.get("call_short") or c.get("short"))
        return (f"You are paid ${credit:,.0f} now to bet {tk} stays BELOW "
                f"${k:,.2f} {when}.{risk}" if k else None)
    k = _f(c.get("short") or c.get("put_short"))
    if k is None:
        return None
    return (f"You are paid ${credit:,.0f} now to bet {tk} stays ABOVE ${k:,.2f} {when}.{risk}")


def reasons(c: Dict, limit: int = 3) -> List[str]:
    """Why the grade is what it is, strongest first, in plain sentences.

    Each is generated from a measured field. Ordered by how much it should move a decision
    rather than by how easy it is to phrase.
    """
    out: List[str] = []
    vrp = _f(c.get("vrp"))
    gap = _f(c.get("edge_pp"))
    tp = _f(c.get("true_pop"))
    ip = _f(c.get("implied_pop"))
    roi = _f(c.get("roi"))

    if vrp is not None and vrp > 0:
        out.append(f"You are being overpaid: the options are priced for a bigger move than "
                   f"{c.get('ticker')} has actually been making, by {vrp:.1f} points.")
    if gap is not None and gap > 0 and tp is not None:
        out.append(f"VEGA gives this {tp*100:.0f}% odds of working; the market is pricing "
                   f"{(ip or 0)*100:.0f}%. That {gap:.1f}-point gap is the whole edge.")
    sh = c.get("_shelter")
    if sh:
        out.append(f"The price it has to hold, ${_f(sh):,.2f}, is a level buyers have "
                   f"defended before — not an arbitrary number.")
    if roi is not None and roi > 0:
        out.append(f"You collect {roi*100:.0f}% of what you put at risk.")
    return out[:limit]


def watch_outs(c: Dict, limit: int = 3) -> List[str]:
    """The risk, stated as plainly as the reward — and never omitted when the grade is good.

    A page that explains the upside in plain English and leaves the downside in jargon is not
    a neutral page. Whatever effort went into making "you are paid $151" legible has to go
    into "you can lose $349" as well.
    """
    out: List[str] = []
    ml = _f(c.get("max_loss_usd"))
    credit = _f(c.get("credit_usd"))
    vrp = _f(c.get("vrp"))
    gap = _f(c.get("edge_pp"))
    be = _f(c.get("breakeven"))
    tk = c.get("ticker")

    if vrp is not None and vrp < 0:
        out.append(f"The options are CHEAP for how much {tk} has been moving. Selling premium "
                   f"here is the wrong side of that — this is the main reason to pass.")
    if gap is not None and gap < 0:
        out.append(f"VEGA rates this {abs(gap):.1f} points WORSE than the market does. No gate "
                   f"blocks that, so it is on you to weigh it.")
    if ml and credit and ml > 0:
        out.append(f"You risk ${ml:,.0f} to make ${credit:,.0f} — about "
                   f"{ml/credit:.0f} to 1 against you. It has to work far more often than it "
                   f"fails just to break even.")
    if be:
        out.append(f"Below ${be:,.2f} you start losing money.")
    if c.get("already_in_position"):
        out.append(f"You already hold {tk}. This doubles down rather than spreading risk.")
    return out[:limit]


def summarise(c: Dict) -> Dict:
    """The whole verdict for one trade."""
    g = grade(c.get("edge_score") or c.get("priority"), c.get("vrp"), c.get("edge_pp"))
    return {
        "grade": g,
        "bet": the_bet(c),
        "reasons": reasons(c),
        "watch_outs": watch_outs(c),
    }
