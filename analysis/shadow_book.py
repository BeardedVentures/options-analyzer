#!/usr/bin/env python3
"""shadow_book.py — grade every trade the board recommended, including the ones it never opened.

The outcome ledger answers "were the trades we TOOK any good?". It cannot answer "is the board
any good?", because a recommendation the desk declined leaves no outcome behind. That gap is
not small: between 2026-08-11 and 2026-08-17 the board produced eleven recommendations and the
desk opened none of them, so six trading days of the system's actual output were recorded and
then abandoned. 158 `modeled` rows sit in the ledger and not one has ever been graded.

This module closes that loop WITHOUT touching the book. Nothing here opens a position, marks
one, or writes to vega_outcomes.jsonl. It reads the modeled rows, resolves each against what
the underlying actually did, and writes its own ledger. `analysis_eligible` still excludes
modeled rows from the live cohort, and the four in-flight natural|natural trades keep their
key — a shadow grade is evidence about the BOARD, deliberately kept out of evidence about the
DESK.

    build()      resolve every modeled recommendation, cache to the shadow ledger
    report()     per-strategy: did the thesis hold, and what would it have paid?

WHAT A GRADE MEANS HERE
-----------------------
`held` — did the short strike survive to expiry untouched? Measured on the LOW for put-side
structures and the HIGH for call-side ones, because a spread does not care where price closed
on the day the strike was breached. An iron condor is graded on both sides at once: it holds
only if neither short strike was touched. This is the thesis the board is actually asserting.

`breached_to_date` is always available and always informational. `held` is None until the
contract has actually expired — an unexpired trade has no outcome, and inferring one from the
current mark is how a ledger starts flattering itself.

WHY MOST ROWS GET NO P/L
------------------------
Modeled rows written before 2026-08-18 recorded `modeled_credit_per_share` from the board's
`credit_per_share`, which is the MID on the bull-put path and the NATURAL on the call-side
path — the same field holding two different prices with nothing to distinguish them. Pricing
P/L off that would reproduce the exact defect that made the first 18 trades in the outcome
ledger unusable, so this module refuses: no fillable credit, no P/L, and the directional grade
is reported on its own. Rows written from 2026-08-18 carry `natural_credit_per_share` and are
priced. The two are never pooled — see `cohort()`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import durable_write  # noqa: E402

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
LEDGER = BASE_DIR / "logs" / "vega_shadow_book.jsonl"

# Below this a per-strategy rate is an anecdote. Mirrors counterfactuals.MIN_GATE_SAMPLE and
# muninn.MIN_STRATUM_SAMPLE in intent: the point of this module is telling a board that picks
# well from one that does not, and 3-of-4 cannot do that for either.
MIN_SAMPLE = int(getattr(config, "SHADOW_MIN_SAMPLE", 20))

PUT_SIDE = "put"
CALL_SIDE = "call"
BOTH_SIDES = "both"


# ── Structure ────────────────────────────────────────────────────────────────

def sides(record: Dict) -> str:
    """Which side(s) of the underlying this structure is short.

    Read off the strategy label rather than the strikes, because the labels arrive in two
    spellings ('bull_put_spread' from main.py, 'Bear Call Spread' from multi_strategy) and a
    comparison against either literal silently misses the other — the same trap _strategy_key
    exists to close in auto_paper_cycle.
    """
    k = str(record.get("strategy") or "").lower().replace(" ", "_")
    if "condor" in k:
        return BOTH_SIDES
    if "call" in k:
        return CALL_SIDE
    return PUT_SIDE


def short_strikes(record: Dict) -> Dict[str, Optional[float]]:
    """The short strike on each side, or None where the structure has none.

    Reads the `legs` map when present and falls back to the two flat strike columns, so rows
    written before legs existed still resolve. A condor without its four legs recorded returns
    Nones and is reported unresolvable rather than silently graded on half its structure.
    """
    legs = record.get("legs") or {}
    side = sides(record)
    if side == BOTH_SIDES:
        return {PUT_SIDE: _num(legs.get("put_short_strike")),
                CALL_SIDE: _num(legs.get("call_short_strike"))}
    strike = _num(legs.get("short_strike"))
    if strike is None:
        strike = _num(record.get("short_strike"))
    return {side: strike, (CALL_SIDE if side == PUT_SIDE else PUT_SIDE): None}


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def fillable_credit(record: Dict) -> Optional[float]:
    """The credit the desk could actually have collected, or None if unknowable.

    Deliberately does NOT fall back to `modeled_credit_per_share`. That field is the mid on one
    path and the natural on the other, and a P/L computed from a price no fill could achieve is
    worse than no P/L at all: it looks like a measurement.
    """
    return _num(record.get("natural_credit_per_share"))


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve(record: Dict, price_history) -> Dict:
    """What the underlying did between the recommendation and expiry.

    `price_history` is a DataFrame indexed by date with High/Low/Close, as
    fetcher.get_price_data returns. Touch is measured intraday on the extreme, not the close.
    """
    out = {
        "breached_to_date": None, "held": None, "breach_side": None,
        "close_at_expiry": None, "days_observed": 0, "expired": False,
        "max_high_since": None, "min_low_since": None, "unresolvable": None,
    }
    strikes = short_strikes(record)
    side = sides(record)
    if all(v is None for v in strikes.values()):
        out["unresolvable"] = "no short strike recorded"
        return out
    exp = str(record.get("expiration") or "")[:10]
    if not exp or exp == "None":
        out["unresolvable"] = "no expiration recorded"
        return out
    if price_history is None or getattr(price_history, "empty", True):
        out["unresolvable"] = "no price history"
        return out

    try:
        scan = str(record.get("scan_ts") or record.get("logged_at") or "")[:10]
        after = price_history[price_history.index.strftime("%Y-%m-%d") > scan]
        if after.empty:
            out["unresolvable"] = "no bars after the recommendation"
            return out

        out["days_observed"] = int(len(after))
        out["max_high_since"] = round(float(after["High"].max()), 4)
        out["min_low_since"] = round(float(after["Low"].min()), 4)

        # Only bars up to and including expiry can breach the trade. Bars after it belong to
        # a contract that no longer exists, and counting them would grade a position the desk
        # would already have been out of.
        lifetime = after[after.index.strftime("%Y-%m-%d") <= exp]
        breached_sides = []
        if strikes[PUT_SIDE] is not None and not lifetime.empty:
            if float(lifetime["Low"].min()) <= strikes[PUT_SIDE]:
                breached_sides.append(PUT_SIDE)
        if strikes[CALL_SIDE] is not None and not lifetime.empty:
            if float(lifetime["High"].max()) >= strikes[CALL_SIDE]:
                breached_sides.append(CALL_SIDE)
        out["breached_to_date"] = bool(breached_sides)
        out["breach_side"] = "+".join(breached_sides) or None

        expired_bars = after[after.index.strftime("%Y-%m-%d") >= exp]
        if not expired_bars.empty:
            out["expired"] = True
            out["close_at_expiry"] = round(float(expired_bars["Close"].iloc[0]), 4)
            # `held` is the thesis: the short strike was never touched over the trade's life.
            # A condor holds only if BOTH sides held — being right about one wing and wrong
            # about the other is a loss, not a half-win.
            out["held"] = not breached_sides
        # else: held stays None. The trade has not expired and has no outcome yet.
        if side == BOTH_SIDES and (strikes[PUT_SIDE] is None or strikes[CALL_SIDE] is None):
            out["unresolvable"] = "condor missing a wing"
    except Exception as e:                            # pragma: no cover - defensive
        logger.debug("[shadow] resolve failed for %s: %s", record.get("ticker"), e)
        out["unresolvable"] = f"resolve error: {e}"
    return out


def grade_pl(record: Dict, outcome: Dict) -> Dict:
    """Modeled P/L per contract at expiry, or a stated refusal.

    Two numbers, never blended:
      `pl_at_expiry`  — held: keep the credit. Breached: the loss the structure would have
                        taken at expiry, from the actual settlement price, capped at width.
      `pl_at_stop`    — the same trade under the desk's live stop rule, which exits at
                        STOP_LOSS_MULTIPLIER x credit rather than riding to settlement.

    The stop figure is a BOUND, not a measurement: it assumes the stop filled at exactly its
    trigger, which no real fill does. Reported so the two management regimes can be compared,
    and labelled so it is never mistaken for a realised number.
    """
    credit = fillable_credit(record)
    out = {"pl_at_expiry": None, "pl_at_stop": None, "priced": False,
           "unpriced_reason": None, "credit_basis": None}
    if credit is None:
        out["unpriced_reason"] = (
            "no natural credit recorded — modeled_credit_per_share is the mid on the "
            "bull-put path and the natural on the call-side path, so it cannot be priced")
        return out
    if outcome.get("held") is None:
        out["unpriced_reason"] = "not expired yet"
        return out

    width = _num(record.get("spread_width"))
    if not width or width <= 0:
        out["unpriced_reason"] = "no positive spread width recorded"
        return out

    out["credit_basis"] = "natural"
    if outcome["held"]:
        out["pl_at_expiry"] = round(credit * 100, 2)
        out["pl_at_stop"] = round(credit * 100, 2)
        out["priced"] = True
        return out

    close = outcome.get("close_at_expiry")
    strikes = short_strikes(record)
    if close is None:
        out["unpriced_reason"] = "breached but no settlement price"
        return out

    # Intrinsic value of the short leg at settlement, capped at the width of the spread — the
    # long leg is what makes the risk defined, so no loss can exceed width minus credit.
    intrinsic = 0.0
    if strikes[PUT_SIDE] is not None:
        intrinsic += max(0.0, strikes[PUT_SIDE] - close)
    if strikes[CALL_SIDE] is not None:
        intrinsic += max(0.0, close - strikes[CALL_SIDE])
    intrinsic = min(intrinsic, width)
    out["pl_at_expiry"] = round((credit - intrinsic) * 100, 2)

    stop_mult = float(getattr(config, "STOP_LOSS_MULTIPLIER", 1.5))
    out["pl_at_stop"] = round(-(credit * (stop_mult - 1.0)) * 100, 2)
    out["priced"] = True
    return out


def cohort(record: Dict, grade: Dict) -> str:
    """The comparability key for a shadow grade: `shadow | <strategy> | <credit basis>`.

    Kept separate from outcome_logger.cohort() on purpose. These are recommendations that were
    never filled, so they share no fill model with the live book and must never be pooled with
    it. The credit basis is in the key for the same reason it is in the live one: a directional
    grade and a priced grade are different measurements of different things.
    """
    strat = str(record.get("strategy") or "unknown").lower().replace(" ", "_")
    basis = grade.get("credit_basis") or "unpriced"
    return f"shadow|{strat}|{basis}"


# ── Ledger ───────────────────────────────────────────────────────────────────

def iter_recommendations(records: Optional[Sequence[Dict]] = None) -> Iterable[Dict]:
    """Every board recommendation, opened or not. `modeled` is the board's own record of it."""
    if records is None:
        from analysis import outcome_logger as ol
        records = ol.load_records()
    return (r for r in records if r.get("status") == "modeled")


def _record(rec: Dict, outcome: Dict, grade: Dict) -> Dict:
    return {
        "id": rec.get("id"),
        "ticker": rec.get("ticker"),
        "strategy": rec.get("strategy"),
        "scan_date": str(rec.get("scan_ts") or rec.get("logged_at") or "")[:10],
        "session_type": rec.get("session_type"),
        "expiration": rec.get("expiration"),
        "dte_at_scan": rec.get("dte"),
        "legs": rec.get("legs"),
        "spread_width": rec.get("spread_width"),
        "natural_credit_per_share": rec.get("natural_credit_per_share"),
        "modeled_credit_per_share": rec.get("modeled_credit_per_share"),
        "edge_score": rec.get("edge_score"),
        "iv_rank": rec.get("iv_rank"),
        "true_pop": rec.get("true_pop"),
        "implied_pop": rec.get("implied_pop"),
        **outcome,
        **grade,
        "cohort": cohort(rec, grade),
        "resolved_at": datetime.now().isoformat(),
    }


def build(records: Optional[Sequence[Dict]] = None,
          ledger: Optional[Path] = None,
          price_lookup=None) -> Dict:
    """Resolve every recommendation and rewrite the shadow ledger.

    Rewritten rather than appended: a grade is a pure function of the recommendation and the
    price history, so re-running must converge rather than accumulate. The source of truth is
    vega_outcomes.jsonl, which this never modifies.
    """
    path = Path(ledger) if ledger else LEDGER
    if price_lookup is None:
        from data import fetcher
        price_lookup = fetcher.get_price_data

    recs = list(iter_recommendations(records))
    history: Dict[str, object] = {}
    rows: List[Dict] = []
    stats = {"total": len(recs), "resolved": 0, "expired": 0, "priced": 0, "unresolvable": 0}

    for rec in recs:
        tk = str(rec.get("ticker") or "").upper()
        if tk and tk not in history:
            try:
                history[tk] = price_lookup(tk)
            except Exception as e:
                logger.debug("[shadow] price lookup failed for %s: %s", tk, e)
                history[tk] = None
        outcome = resolve(rec, history.get(tk))
        grade = grade_pl(rec, outcome)
        rows.append(_record(rec, outcome, grade))
        if outcome.get("unresolvable"):
            stats["unresolvable"] += 1
        else:
            stats["resolved"] += 1
        if outcome.get("expired"):
            stats["expired"] += 1
        if grade.get("priced"):
            stats["priced"] += 1

    durable_write.atomic_write_text(
        path, "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))
    logger.info("[shadow] graded %(resolved)s/%(total)s recommendations "
                "(%(expired)s expired, %(priced)s priced)", stats)
    return stats


def load(ledger: Optional[Path] = None) -> List[Dict]:
    path = Path(ledger) if ledger else LEDGER
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── Reporting ────────────────────────────────────────────────────────────────

def summarize(rows: Optional[Sequence[Dict]] = None) -> Dict:
    """Per-cohort: how often the thesis held, and what it would have paid.

    Every figure states its own n, and a cohort below MIN_SAMPLE reports `insufficient` rather
    than a rate. `hit_rate` counts only EXPIRED trades — including live ones would score a
    thesis that has not finished being tested.
    """
    rows = load() if rows is None else rows
    by: Dict[str, List[Dict]] = {}
    for r in rows:
        by.setdefault(r.get("cohort") or "unknown", []).append(r)

    out: Dict[str, Dict] = {}
    for key, group in sorted(by.items()):
        expired = [r for r in group if r.get("held") is not None]
        priced = [r for r in expired if r.get("priced")]
        held = sum(1 for r in expired if r.get("held"))
        pops = [r["true_pop"] for r in expired if isinstance(r.get("true_pop"), (int, float))]
        entry = {
            "recommended": len(group),
            "expired": len(expired),
            "pending": len(group) - len(expired),
            "unresolvable": sum(1 for r in group if r.get("unresolvable")),
            "held": held,
            "hit_rate": round(held / len(expired), 4) if expired else None,
            "avg_true_pop": round(sum(pops) / len(pops), 4) if pops else None,
            "priced": len(priced),
            "total_pl_at_expiry": round(sum(r["pl_at_expiry"] for r in priced), 2) if priced else None,
            "total_pl_at_stop": round(sum(r["pl_at_stop"] for r in priced), 2) if priced else None,
            "sufficient": len(expired) >= MIN_SAMPLE,
        }
        # The calibration read the whole exercise exists for: the board asserts a probability
        # on every trade, and this is the first thing that ever checks it against an outcome.
        if entry["hit_rate"] is not None and entry["avg_true_pop"] is not None:
            entry["calibration_gap_pp"] = round(
                (entry["hit_rate"] - entry["avg_true_pop"]) * 100, 1)
        out[key] = entry
    return out


def report(rows: Optional[Sequence[Dict]] = None) -> str:
    summary = summarize(rows)
    if not summary:
        return "Shadow book is empty — run build() after a scan has recorded modeled trades."
    lines = ["SHADOW BOOK — every trade the board recommended, graded", ""]
    for key, s in summary.items():
        lines.append(f"  {key}")
        lines.append(f"    recommended {s['recommended']}  expired {s['expired']}  "
                     f"pending {s['pending']}  unresolvable {s['unresolvable']}")
        if not s["expired"]:
            lines.append("    no expired trades yet — nothing to grade")
        elif not s["sufficient"]:
            lines.append(f"    held {s['held']}/{s['expired']} — insufficient "
                         f"(n<{MIN_SAMPLE}), reported as a count, not a rate")
        else:
            lines.append(f"    held {s['held']}/{s['expired']} = {s['hit_rate']:.1%}")
        if s.get("calibration_gap_pp") is not None:
            lines.append(f"    board claimed {s['avg_true_pop']:.1%} — "
                         f"calibration gap {s['calibration_gap_pp']:+.1f}pp")
        if s["priced"]:
            lines.append(f"    P/L on {s['priced']} priced: "
                         f"${s['total_pl_at_expiry']:+,.2f} at expiry, "
                         f"${s['total_pl_at_stop']:+,.2f} under the {getattr(config, 'STOP_LOSS_MULTIPLIER', 1.5)}x stop")
        else:
            lines.append("    P/L unpriced — no fillable credit recorded on these rows")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":                            # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = build()
    print(f"\ngraded {stats['resolved']}/{stats['total']} "
          f"({stats['expired']} expired, {stats['priced']} priced, "
          f"{stats['unresolvable']} unresolvable)\n")
    print(report())
