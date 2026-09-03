#!/usr/bin/env python3
"""liveness.py — does each measurement channel actually produce a measurement?

NOT a data-quality check. Nothing here looks at whether a value is plausible; every check asks
the cruder question that kept going unasked: does this channel produce ANY graded output at
all, and if not, has it been silent longer than its own design says it should be?

WHY THIS EXISTS. Five instruments were built to measure this system and, as of 2026-09-02,
five were reporting nothing:

  shadow book        178 rows, priced=False on ALL of them -- 158 permanently unpriceable
                     because modelled_credit_per_share is the mid on the bull-put path and the
                     natural on the call side, so it cannot be priced at all.
  caps_v1 cohort     13 rows, every one status=modeled. Zero executed trades. "0 of 30" is
                     literally zero.
  counterfactuals    2,726 rows, horizon_complete False on ALL of them, and structurally
                     unable to change: snapshot retention was 2.9 trading days against a
                     10-day horizon. Repaired 2026-09-02.
  predictions        1,241 of 2,739 resolved -- this one PRODUCES, but resolution is censored
                     and the resolved slice is not a random sample of the whole.
  decision ledger    does not exist yet.

Every one of those failed SILENTLY, and in three cases the code carried a comment anticipating
the failure as an edge case. value_of_information() would have reported "insufficient" on all
eleven gates forever, correctly, and said nothing about why.

The generalisable rule, and the reason this file is small on purpose: an instrument that has
never once produced an output is indistinguishable from one that works, until someone asks. A
channel that cannot answer "how many graded outputs have you produced" is not instrumented.

WHAT A CHECK MAY NOT DO. It may not say a channel is broken when the honest answer is that it
is STARVED. caps_v1 has zero rows because ENTRY_HOLD is on and the board has qualified almost
nothing since 2026-08-10 -- the channel is wired correctly and has nothing to record. Reporting
that as CRITICAL every cycle would train the operator to ignore the file, which is how the
signal dies a second time. Starvation is reported as STARVED, with its reason, and only becomes
CRITICAL when the reason stops being true.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LIVENESS_LOG = LOG_DIR / "vega_liveness.jsonl"

OK, STARVED, CRITICAL, NOT_BUILT = "ok", "starved", "critical", "not_built"


# Channels that are DESIGNED but not yet in production. Registered here rather than omitted,
# because an unregistered channel is exactly the thing this module exists to catch: a
# measurement everyone assumes is running because nobody can see that it is not.
#
# The rule for this list: an entry leaves it in the SAME COMMIT that creates the ledger it
# names. A name that sits here after its writer ships is a channel nothing is checking.
_no_production_ledgers = {
    "decision_ledger": (
        "Designed 2026-09-02, not built. Intended to record every WATCH/PASS/TAKE decision and "
        "its reason, so the board's refusals are gradeable rather than invisible. Until it "
        "exists, the system cannot tell a board that picks badly from one whose picks are "
        "correctly declined."
    ),
}


def _read_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _oldest_age_days(rows: List[Dict], date_fields) -> Optional[float]:
    """How long the channel has had material to work with, in calendar days."""
    stamps = []
    for r in rows:
        for f in date_fields:
            v = r.get(f)
            if v:
                stamps.append(str(v)[:10])
                break
    if not stamps:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(min(stamps))).days
    except Exception:
        return None


# ── The channels ──────────────────────────────────────────────────────────────

def _check_counterfactuals() -> Dict:
    rows = _read_jsonl(LOG_DIR / "vega_counterfactuals.jsonl")
    graded = sum(1 for r in rows if r.get("horizon_complete") and r.get("touched") is not None)
    horizon = int(getattr(config, "COUNTERFACTUAL_HORIZON_DAYS", 10))
    age = _oldest_age_days(rows, ("scan_date",))
    # Calendar-day allowance for a horizon counted in TRADING days, plus slack.
    due = horizon * 1.6 + 3
    if not rows:
        return {"status": STARVED, "graded": 0, "reason": "no counterfactual rows recorded yet"}
    if graded:
        return {"status": OK, "graded": graded, "rows": len(rows)}
    if age is not None and age < due:
        return {"status": STARVED, "graded": 0,
                "reason": f"oldest row is {age}d old; a {horizon}-trading-day horizon needs ~{due:.0f}d"}
    return {"status": CRITICAL, "graded": 0, "rows": len(rows),
            "reason": (f"{len(rows)} rows, oldest {age}d old, and NOT ONE has completed a "
                       f"{horizon}-day horizon. This is the 2026-09-02 failure: snapshot "
                       f"retention shorter than the horizon, so nothing could ever mature.")}


def _check_shadow_book() -> Dict:
    rows = _read_jsonl(LOG_DIR / "vega_shadow_book.jsonl")
    priced = sum(1 for r in rows if r.get("priced"))
    expired = sum(1 for r in rows if r.get("expired"))
    if not rows:
        return {"status": STARVED, "graded": 0, "reason": "no board recommendations recorded yet"}
    if priced:
        return {"status": OK, "graded": priced, "rows": len(rows)}
    if not expired:
        return {"status": STARVED, "graded": 0,
                "reason": f"{len(rows)} rows but none has expired yet"}
    return {"status": CRITICAL, "graded": 0, "rows": len(rows), "expired": expired,
            "reason": (f"{expired} of {len(rows)} rows have expired and NOT ONE is priced. The "
                       f"known cause is modelled_credit_per_share carrying the MID on the "
                       f"bull-put path and the NATURAL on the call side, so no P&L can be "
                       f"computed. Breach/hold is being recorded; grading is not.")}


def _check_caps_cohort() -> Dict:
    try:
        from analysis import outcome_logger as ol
        rows = ol.load_records()
        epoch = ol.current_entry_epoch()
        mine = [r for r in rows if ol.entry_epoch(r) == epoch]
        graded = sum(1 for r in mine
                     if r.get("status") == "closed" and ol.analysis_eligible(r))
        executed = sum(1 for r in mine if r.get("status") in ("open", "closed"))
    except Exception as exc:
        return {"status": CRITICAL, "graded": 0,
                "reason": f"cohort could not be read at all: {type(exc).__name__}: {exc}"}

    if graded:
        return {"status": OK, "graded": graded, "epoch": epoch, "executed": executed}
    # STARVED, not CRITICAL, while a deliberate hold explains the zero. The distinction is the
    # whole point: a stopped instrument and a stopped supply look identical from the count.
    hold = bool(getattr(config, "ENTRY_HOLD", False))
    if hold:
        return {"status": STARVED, "graded": 0, "epoch": epoch, "executed": executed,
                "reason": ("ENTRY_HOLD is ON, so no trade can open. This zero is a policy, not "
                           "a fault. It becomes CRITICAL the moment the hold is lifted and the "
                           "count stays at zero while the board is qualifying.")}
    return {"status": CRITICAL, "graded": 0, "epoch": epoch, "executed": executed,
            "reason": (f"cohort {epoch} has {executed} executed trade(s) and 0 analysis-eligible "
                       f"closed rows, with no ENTRY_HOLD to explain it.")}


def _check_predictions() -> Dict:
    rows = _read_jsonl(LOG_DIR / "vega_predictions.jsonl")
    resolved = sum(1 for r in rows if r.get("status") == "resolved")
    if not rows:
        return {"status": STARVED, "graded": 0, "reason": "no predictions recorded yet"}
    if not resolved:
        return {"status": CRITICAL, "graded": 0, "rows": len(rows),
                "reason": f"{len(rows)} predictions recorded and NOT ONE resolved."}
    # Produces. The censoring problem is real and is NOT a liveness question -- predictions
    # that resolve first are not a random sample of all predictions, so any accuracy figure on
    # the resolved slice is uninterpretable. Reported as context so the number is never quoted
    # bare, never as a failure.
    return {"status": OK, "graded": resolved, "rows": len(rows),
            "open": len(rows) - resolved,
            "note": ("resolution is CENSORED -- fast resolvers skew toward large adverse moves, "
                     "so the resolved-slice hit rate is not an accuracy estimate")}


CHANNELS: Dict[str, Callable[[], Dict]] = {
    "counterfactual_ledger": _check_counterfactuals,
    "shadow_book": _check_shadow_book,
    "caps_cohort": _check_caps_cohort,
    "prediction_ledger": _check_predictions,
}


def check_all() -> Dict[str, Dict]:
    """Every registered channel, plus the ones that do not exist yet."""
    out: Dict[str, Dict] = {}
    for name, fn in CHANNELS.items():
        try:
            out[name] = fn()
        except Exception as exc:            # a check must never take the cycle down
            out[name] = {"status": CRITICAL, "graded": 0,
                         "reason": f"the check itself failed: {type(exc).__name__}: {exc}"}
    for name, why in _no_production_ledgers.items():
        out[name] = {"status": NOT_BUILT, "graded": 0, "reason": why}
    return out


def report(results: Optional[Dict[str, Dict]] = None) -> List[str]:
    """One line per channel, worst first. Returns the lines rather than printing them."""
    results = results if results is not None else check_all()
    order = {CRITICAL: 0, NOT_BUILT: 1, STARVED: 2, OK: 3}
    lines = []
    for name, r in sorted(results.items(), key=lambda kv: (order.get(kv[1]["status"], 9), kv[0])):
        head = f"[{r['status'].upper():8}] {name:22} graded={r.get('graded', 0)}"
        reason = r.get("reason") or r.get("note")
        lines.append(f"{head}  {reason}" if reason else head)
    return lines


def record(results: Optional[Dict[str, Dict]] = None) -> Dict[str, Dict]:
    """Log the outcome and append it to the liveness journal. CRITICAL is logged as CRITICAL."""
    results = results if results is not None else check_all()
    for name, r in results.items():
        msg = f"[liveness] {name}: {r['status']} (graded={r.get('graded', 0)})"
        if r.get("reason"):
            msg += f" — {r['reason']}"
        if r["status"] == CRITICAL:
            logger.critical(msg)
        elif r["status"] in (STARVED, NOT_BUILT):
            logger.warning(msg)
        else:
            logger.info(msg)
    try:
        LIVENESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LIVENESS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                 "channels": results}, default=str) + "\n")
    except Exception as exc:                # pragma: no cover - journalling is best-effort
        logger.warning("[liveness] could not journal: %s", exc)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for line in report():
        print(line)
