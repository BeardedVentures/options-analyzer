#!/usr/bin/env python3
"""contracts.py — validate records at the boundaries where they cross between components.

WHY THIS EXISTS

Two defect classes in this repo's history are the same defect wearing different clothes:

  FIELD-NAME MISMATCH   producer writes `vrp_pp`, consumer reads `vrp`. The largest component
                        of the edge score read zero on every trade the auto-trader opened, for
                        weeks, and nothing failed. Same shape: `pop_pct` vs `pop`,
                        `realized_vol` vs `rv_30d`.

  SILENT NONE           edge_score was None on 100% of real paper trades because true_pop
                        attached after the assessment that needed it. support_level_at_entry
                        was null on every managed position. Both were found by after-the-fact
                        audit, months later, not by anything failing.

Both are invisible because Python is happy to hand you None and `float(None or 0)` is happy to
turn it into zero. A zero that should have been a measurement is indistinguishable from a
measurement of zero, and the ledger keeps accepting rows either way.

WHAT THIS DOES ABOUT IT

Declares what each record MUST carry at the moment it crosses a boundary, and enforces it
there. Not everywhere — three places, chosen because they are where a bad record stops being
recoverable:

    OPEN     a trade written to the ledger. Wrong here and every downstream number is wrong
             forever; the row cannot be re-derived once the chain moves.
    CLOSE    the outcome. This is the measurement the whole project exists to collect.
    SELECT   a candidate the auto-trader is about to act on.

TWO DIFFERENT SEVERITIES, DELIBERATELY

Writes RAISE. A ledger row that cannot be graded is worse than no row — it looks like data.
Reads REJECT the record and carry on. A malformed candidate should not take down a scan of 56
names; it should be refused and counted, which is what a gate already does.

NOT A TYPE SYSTEM. This checks presence and numeric sanity at three seams. It is deliberately
small enough that nobody is tempted to route around it.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class ContractError(ValueError):
    """A record failed its contract at a boundary. Carries every failure, not just the first —
    fixing one field at a time across three round trips is how these get abandoned."""


# ── Specs ─────────────────────────────────────────────────────────────────────
# (field, kind) where kind is "num" (finite number), "num0" (finite, non-negative),
# "str" (non-empty string). Absence and None both fail; that is the entire point.

OPEN_TRADE: Tuple[Tuple[str, str], ...] = (
    ("ticker", "str"),
    ("short_strike", "num"),
    ("expiration", "str"),
    # The credit the desk actually books. Modelled vs achieved was the single largest
    # confounder in the ledger's history, so both must be present and distinguishable.
    ("actual_fill_credit", "num"),
    ("dte", "num0"),
    ("contracts", "num0"),
    # Cohort identity. A trade whose fill/gate basis is unknown cannot be placed in a cohort,
    # and a trade outside a cohort cannot inform a base rate — see outcome_logger.cohort.
    ("fill_basis", "str"),
    ("gate_basis", "str"),
)

CLOSE_TRADE: Tuple[Tuple[str, str], ...] = (
    ("exit_price", "num0"),
    ("outcome", "str"),
    ("status", "str"),
)

SELECT_CANDIDATE: Tuple[Tuple[str, str], ...] = (
    ("ticker", "str"),
    ("short_strike", "num"),
    ("long_strike", "num"),
    ("natural_credit_usd", "num"),
    # Sizing. Refusing a candidate whose risk cannot be computed is the 2026-08-16 fix; this
    # states it as a contract rather than leaving it as one gate's local decision.
    ("max_loss_usd", "num0"),
)


def _check(rec: Dict, field: str, kind: str) -> Optional[str]:
    if field not in rec:
        return f"{field}: missing"
    v = rec.get(field)
    if v is None:
        # The whole reason this module exists. None is not zero and it is not a default.
        return f"{field}: None (a value was expected, not an absence)"
    if kind == "str":
        if not isinstance(v, str) or not v.strip():
            return f"{field}: expected a non-empty string, got {v!r}"
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return f"{field}: expected a number, got {v!r}"
    if math.isnan(f) or math.isinf(f):
        # NaN survives every comparison it meets and renders as a plausible-looking value.
        return f"{field}: {v!r} is not finite"
    if kind == "num0" and f < 0:
        return f"{field}: expected >= 0, got {f}"
    return None


def validate(rec: Dict, spec: Sequence[Tuple[str, str]], where: str) -> List[str]:
    """Every failure in this record, as human sentences. Empty list means it passed."""
    return [p for p in (_check(rec or {}, f, k) for f, k in spec) if p]


def enforce(rec: Dict, spec: Sequence[Tuple[str, str]], where: str) -> Dict:
    """WRITE boundary. Raises on any failure.

    A ledger row that cannot be graded is worse than no row, because it looks like data and
    gets averaged into base rates by someone who was not there when it was written.
    """
    problems = validate(rec, spec, where)
    if problems:
        raise ContractError(f"{where} contract failed:\n  " + "\n  ".join(problems))
    return rec


def accept(rec: Dict, spec: Sequence[Tuple[str, str]], where: str) -> bool:
    """READ boundary. Returns False and logs; never raises.

    A malformed candidate must not take down a scan of 56 names. It is refused and counted,
    which is what a gate already does — the difference is that the refusal is now visible
    instead of the record flowing through with a silent zero in it.
    """
    problems = validate(rec, spec, where)
    if problems:
        logger.warning("[contract] %s rejected %s: %s", where,
                       (rec or {}).get("ticker", "?"), "; ".join(problems))
        return False
    return True
