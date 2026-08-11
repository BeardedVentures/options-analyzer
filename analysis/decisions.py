#!/usr/bin/env python3
"""decisions.py — what the operator did with what VEGA recommended.

The paper ledger records the trades that were TAKEN. That is a censored sample: it can say
how VEGA's accepted recommendations performed, and it can say nothing at all about the ones
waved through, which is the half of the record that would show whether the operator's
overrides add information or destroy it.

Two decisions are recorded here:

  WATCH   — interesting, not committed. The counterfactual that trains judgement: a watched
            setup that later works is a lesson, and one that blows up is a cheaper one.
  REJECT  — VEGA recommended this and the operator consciously declined. This is negative
            signal data. Over enough rows it answers a question nothing else can: when the
            operator disagrees with the engine, who is right?

The entry state is the whole point. A row saying "rejected WMT on the 11th" cannot be graded
against anything — by the time anyone looks, the chain has moved and the setup it describes
no longer exists. So every decision snapshots the same fields the paper ledger snapshots at
open (strikes, expiry, credit, delta, true_pop, pop_implied, pop_gap, edge_score), because a
decision that cannot be scored later is a diary entry, not data. This is the same failure
`pop_gap_at_entry` exists to prevent in outcome_logger, and the reason the 2026-08-10 snapshot
carries no `earnings_source`: instrumentation added after the fact cannot be backfilled.

Append-only JSONL. Never rewritten in place — see the ledger dedup incident that reverted a
day of closes while the line count stayed plausible.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

BASE_DIR = Path(__file__).resolve().parent.parent
LEDGER = BASE_DIR / "logs" / "vega_decisions.jsonl"

WATCH = "watch"
REJECT = "reject"
VALID = (WATCH, REJECT)

# The entry state a decision must carry to be gradeable later. Anything not in this list is
# recoverable from a later scan; everything in it is not.
_SNAPSHOT_FIELDS = (
    "strategy", "short_strike", "long_strike", "expiration", "dte",
    "credit_per_share", "credit_usd", "max_loss_usd", "delta",
    "true_pop", "pop_implied", "pop_gap", "edge_score", "iv_rank", "vrp",
    "spot", "roi",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v):
    """floats stay floats, blanks become None. Form posts arrive as strings, and '' parsed
    with float() raises — which would take the whole POST down over a missing optional."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def record(decision: str, ticker: str, snapshot: Optional[Dict] = None,
           note: str = "", source: str = "board",
           ledger: Optional[Path] = None) -> Dict:
    """Append one decision. Returns the row written.

    Raises ValueError on an unknown decision rather than storing it: a ledger with three
    spellings of "reject" in it cannot be grouped, and the failure would not surface until
    someone tried to analyse it months later.
    """
    if decision not in VALID:
        raise ValueError(f"decision must be one of {VALID}, got {decision!r}")
    # Capped, not just stripped: this arrives from an HTML form field, and an append-only
    # ledger is exactly the wrong place to discover that an unbounded string reached it.
    ticker = (ticker or "").strip().upper()[:16]
    if not ticker:
        raise ValueError("ticker is required")

    snap = snapshot or {}
    row: Dict = {
        "ts": _now(),
        "decision": decision,
        "ticker": ticker,
        "source": source,
        "note": (note or "")[:500],
    }
    for k in _SNAPSHOT_FIELDS:
        row[k] = _num(snap.get(k)) if k not in ("strategy", "expiration") else (snap.get(k) or None)
    # Derived here rather than trusted from the caller: the UI can post a stale or absent gap
    # and the one number the reject ledger exists to grade would be the one missing.
    if row.get("pop_gap") is None and row.get("true_pop") is not None \
            and row.get("pop_implied") is not None:
        row["pop_gap"] = round(row["true_pop"] - row["pop_implied"], 4)

    path = Path(ledger) if ledger else LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load(ledger: Optional[Path] = None) -> List[Dict]:
    """Every decision, oldest first. A corrupt line is skipped, not fatal — a half-written
    row from a killed process must not make the whole history unreadable."""
    path = Path(ledger) if ledger else LEDGER
    if not path.exists():
        return []
    out: List[Dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def watching(ledger: Optional[Path] = None) -> List[Dict]:
    """Currently-watched setups, most recent first, one per ticker+structure.

    A setup watched three times is one item on a watchlist, not three, but every touch stays
    in the ledger — deduplication is a read-time concern precisely so the raw record keeps
    the repeat, which is itself a signal about conviction.
    """
    seen = set()
    out = []
    for r in reversed(load(ledger)):
        if r.get("decision") != WATCH:
            continue
        key = (r.get("ticker"), r.get("short_strike"), r.get("long_strike"), r.get("expiration"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def summary(records: Optional[Sequence[Dict]] = None,
            ledger: Optional[Path] = None) -> Dict:
    """Counts, and the mean edge score on each side of the operator's judgement.

    The comparison is the point. If rejected setups carry a systematically lower edge score
    than taken ones, the operator is filtering in the same direction as the engine and adding
    little; if they are indistinguishable, the overrides are noise; if rejects score HIGHER,
    something is wrong with either the score or the operator, and that is worth knowing early.

    Deliberately returns counts alongside the means: a mean over three rows is not a finding,
    and a summary that hides its own sample size invites one to be read as such.
    """
    rows = list(records) if records is not None else load(ledger)

    def _mean(vals, places=1):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), places) if vals else None

    watched = [r for r in rows if r.get("decision") == WATCH]
    rejected = [r for r in rows if r.get("decision") == REJECT]
    # pop_gap is a 0-1 fraction and edge_score is 0-100; sharing one rounding rounded every
    # realistic gap (0.02-0.12) straight to 0.0 and silently deleted the single number this
    # ledger exists to produce. Reported in percentage points, named so, so the unit travels
    # with the value and the next reader cannot re-scale it by accident.
    gap_pp = _mean((r.get("pop_gap") or 0) * 100 for r in rejected
                   if isinstance(r.get("pop_gap"), (int, float)))
    return {
        "total": len(rows),
        "watch_count": len(watched),
        "reject_count": len(rejected),
        "watch_mean_edge": _mean(r.get("edge_score") for r in watched),
        "reject_mean_edge": _mean(r.get("edge_score") for r in rejected),
        "reject_mean_pop_gap_pp": gap_pp,
    }
