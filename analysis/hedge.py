#!/usr/bin/env python3
"""hedge.py — the directional exposure a premium trade carries, and what to do about it.

WHY THIS EXISTS, AND WHY IT IS NOT A COPY OF WHAT A DESK DOES

A professional vol desk sells premium and delta-hedges continuously with shares. That is the
entire trick: hedging strips out direction so what remains is the variance risk premium, which
is the edge they actually believe in. It is why their per-trade edge is small and their results
are repeatable.

Retail cannot do that, and pretending otherwise would be the most expensive kind of advice.
Hedging is continuous — the delta moves as the stock moves, so the hedge has to be rebalanced,
and each rebalance costs spread and commission. A one-contract bull put at 0.20 delta carries
roughly 15-20 shares of equivalent exposure; on a $115 stock that is ~$2,000 of capital or
borrow to neutralise a position whose entire max loss is $68. The hedge costs more than the
risk it removes.

So this module does three honest things instead of one dishonest one:

1. STATES the exposure. A credit spread is not direction-neutral and the board never said how
   much direction it was carrying. "Short put, 0.20 delta" means nothing to most readers;
   "this behaves like owning 18 shares" means something to everyone.

2. AGGREGATES it across the book. This is the number that actually matters and the one no
   screen showed. Eleven bull puts are not eleven independent bets — they are one large long
   position in the market, and the correlation shows up exactly when it hurts. A desk watches
   book delta continuously; a retail trader can watch it once a day, and that is most of the
   benefit for none of the cost.

3. SUGGESTS the structure, not the share trade. The affordable retail hedge is not shares, it
   is a second option position: pairing a bull put with a bear call on the same underlying
   makes an iron condor, which is delta-flatter and collects premium on both sides rather than
   bleeding it to a share hedge. VEGA already scans bear calls.

NOTHING HERE PLACES AN ORDER. It reports exposure and names the structure that would reduce
it.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

SHARES_PER_CONTRACT = 100

# Long leg delta as a fraction of the short leg's, when the long leg's own delta is not
# recorded. The long strike sits further OTM so its delta is strictly smaller; across the
# 1-5 point widths this system trades it typically runs 50-70% of the short's. 0.60 is the
# middle of that, and the result is labelled an estimate wherever it is shown — the exact
# figure needs long_delta stored at build time, which is a scan change and not a display one.
_LONG_LEG_DELTA_FRACTION = 0.60

# Book delta beyond which the concentration is worth naming, as a fraction of account equity
# expressed in share-equivalent dollars.
BOOK_DELTA_WARN_PCT = float(getattr(config, "BOOK_DELTA_WARN_PCT", 0.25))


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def position_delta(short_delta, contracts: int = 1, long_delta=None,
                   strategy: str = "bull_put") -> Optional[Dict]:
    """Share-equivalent directional exposure of one spread position.

    A short put has POSITIVE delta (it profits as the stock rises); a short call has negative.
    The long leg offsets part of it. Returns share-equivalents because "18 shares" is a unit
    every reader already owns an intuition for, and "0.18 delta" is not.
    """
    sd = _f(short_delta)
    if sd is None or not contracts:
        return None
    sd = abs(sd)
    ld = _f(long_delta)
    estimated = ld is None
    if ld is None:
        ld = sd * _LONG_LEG_DELTA_FRACTION
    else:
        ld = abs(ld)
    net = sd - ld                                   # always positive magnitude
    strat = (strategy or "bull_put").lower()
    if "condor" in strat:
        # Two offsetting wings. The residual is the difference between them, which is small by
        # construction and cannot be signed from the put side alone.
        sign = 0.0
    elif "bear_call" in strat or "bear call" in strat:
        sign = -1.0                                 # hurt by a rally
    else:
        sign = 1.0                                  # bull put: hurt by a fall
    shares = sign * net * SHARES_PER_CONTRACT * int(contracts)
    return {
        "net_delta_per_contract": round(sign * net, 4),
        "share_equivalent": round(shares, 1),
        "contracts": int(contracts),
        "direction": ("long" if shares > 0 else "short" if shares < 0 else "neutral"),
        "long_leg_estimated": estimated,
    }


def hedge_suggestion(candidate: Dict, spot=None, account_equity=None) -> Optional[Dict]:
    """What the exposure is, what a share hedge would cost, and the cheaper structure.

    The share-hedge cost is computed and shown precisely so it can be REJECTED on the numbers
    rather than on a rule of thumb. On most retail-sized spreads it is plainly uneconomic, and
    seeing that once teaches more than being told.
    """
    sd = candidate.get("short_delta")
    if sd is None:
        sd = candidate.get("delta")
    pos = position_delta(sd, candidate.get("contracts") or 1,
                         candidate.get("long_delta"),
                         candidate.get("strat_type") or candidate.get("strategy") or "bull_put")
    if not pos:
        return None
    px = _f(spot) if spot is not None else _f(candidate.get("price") or candidate.get("spot"))
    max_loss = _f(candidate.get("max_loss_usd"))
    shares = pos["share_equivalent"]
    notional = abs(shares) * px if px else None

    out = dict(pos)
    out.update({
        "spot": round(px, 2) if px else None,
        "hedge_shares": round(-shares, 1),          # the offsetting share position
        "hedge_notional_usd": round(notional, 2) if notional else None,
        "max_loss_usd": max_loss,
    })
    # Is a share hedge proportionate? Compare the capital it ties up against the risk it
    # removes. Anything over 1x is spending more than the position can lose.
    if notional and max_loss and max_loss > 0:
        ratio = notional / max_loss
        out["hedge_cost_ratio"] = round(ratio, 2)
        out["share_hedge_worth_it"] = ratio <= 1.0
    else:
        out["hedge_cost_ratio"] = None
        out["share_hedge_worth_it"] = None

    if pos["direction"] == "neutral":
        out["suggestion"] = ("Already close to delta-neutral — both wings offset, which is the "
                             "structural version of a hedge.")
    elif out.get("share_hedge_worth_it") is False:
        opp = "bear call" if shares > 0 else "bull put"
        out["suggestion"] = (
            f"Behaves like {'owning' if shares > 0 else 'being short'} "
            f"{abs(shares):.0f} shares. A share hedge would tie up "
            f"${notional:,.0f} against ${max_loss:,.0f} of max loss "
            f"({out['hedge_cost_ratio']:.1f}x) — not worth it at this size. The affordable "
            f"hedge is structural: a {opp} on the same name turns this into an iron condor, "
            f"which is delta-flatter and collects premium on both sides instead of paying to "
            f"remove direction.")
    else:
        out["suggestion"] = (
            f"Behaves like {'owning' if shares > 0 else 'being short'} {abs(shares):.0f} "
            f"shares. {'Short' if shares > 0 else 'Buy'} {abs(out['hedge_shares']):.0f} shares "
            f"to flatten it (${notional:,.0f}), or pair it with the opposite spread for a "
            f"structural hedge that costs nothing to carry.")
    return out


def book_delta(open_positions: Sequence[Dict], marks: Optional[Dict] = None) -> Dict:
    """Aggregate share-equivalent exposure across every open position.

    THE number this system never showed. Eleven bull puts are not eleven independent bets —
    they are one large long position, and the correlation between them arrives precisely when
    it hurts. Grouped by ticker as well as totalled, because concentration in one name and
    concentration across the book are different problems with different fixes.
    """
    per_ticker: Dict[str, float] = {}
    total = 0.0
    counted = 0
    for r in open_positions or ():
        pos = position_delta(r.get("delta") or r.get("short_delta"),
                             r.get("contracts") or 1,
                             r.get("long_delta"),
                             r.get("strategy") or r.get("strat_type") or "bull_put")
        if not pos:
            continue
        tk = (r.get("ticker") or "?").upper()
        sh = pos["share_equivalent"]
        per_ticker[tk] = round(per_ticker.get(tk, 0.0) + sh, 1)
        total += sh
        counted += 1
    notional = None
    if marks:
        notional = sum(abs(v) * float(marks.get(k) or 0) for k, v in per_ticker.items())
    return {
        "positions": counted,
        "share_equivalent": round(total, 1),
        "by_ticker": dict(sorted(per_ticker.items(), key=lambda kv: -abs(kv[1]))),
        "notional_usd": round(notional, 2) if notional else None,
        "direction": ("long" if total > 0 else "short" if total < 0 else "neutral"),
    }
