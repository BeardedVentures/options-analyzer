#!/usr/bin/env python3
"""counterfactuals.py — what happened to the trades VEGA refused to take.

The trade ledger can only ever answer whether the PICKS were good. It cannot answer whether
the GATES are good, because a rejected candidate leaves no record of what it would have done.
Eleven gates decide every entry and not one of them has ever been measured against an outcome.

Nothing new is recorded to make this work. Every scan already writes output/candidates/*.json
with each candidate's full gate results, and 2,590 of them are sitting on disk — 562 of which
failed EXACTLY ONE gate, which is the sample that can price a single gate's contribution. This
module resolves those against what the underlying actually did and reports, per gate, whether
the candidates it blocked went on to behave worse than the ones that passed.

    build()                 resolve every snapshot candidate, cache to the ledger
    value_of_information()  per-gate: did blocking this actually avoid anything?

WHAT "OUTCOME" MEANS HERE
-------------------------
`touched` — did the underlying trade at or through the short strike after the scan? For a
credit spread that is the event that matters: it is what drives a delta breach, a stop-out and
a loss, and unlike expiry it is answerable the day after a scan rather than 40 days later.

`held_at_expiry` is also computed, and is None until the contract actually expires. It is the
cleaner measure and the slower one. Both are reported; neither is inferred from the other.

WHAT THIS CANNOT TELL YOU YET
-----------------------------
Snapshots keep the top 3 candidates per ticker by natural credit-to-width, not every spread
enumerated. So this is a defined but BIASED sample: it can compare gates against each other
within that band, and it cannot speak for the spreads that never made the top 3.

Observations are also not independent — the same spread reappears across consecutive scans.
dedup_key collapses them to first-observation, which is the honest unit, and every report
states its n. A gate whose sample is below MIN_GATE_SAMPLE reports "insufficient" rather than a
number, for the same reason muninn refuses to publish a 2-of-3 recovery rate.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = BASE_DIR / "output" / "candidates"
LEDGER = BASE_DIR / "logs" / "vega_counterfactuals.jsonl"

# Below this, a per-gate rate is a story rather than a measurement. Mirrors
# muninn.MIN_STRATUM_SAMPLE in intent: the whole value of this module is telling a gate that
# earns its place from one that does not, and a 3-of-4 "touch rate" cannot do that.
MIN_GATE_SAMPLE = 20

# Every spread is judged over the SAME number of trading days after its scan, and one that has
# not lived that long yet is excluded rather than counted as untouched.
#
# Without this the module produces its own worst failure. The first real run resolved 639
# spreads and reported 0% touched on every gate — which reads as "none of the eleven gates
# avoids anything" and actually meant the median spread had been observed for TWO DAYS. A
# 39-DTE spread at 0.20 delta is not expected to be touched in two days; nothing had had time
# to happen. Worse, the window length is confounded with the scan date, so a gate whose blocked
# candidates happened to come from the oldest scan would look worse than one whose came from
# today purely through exposure.
#
# A fixed horizon makes every observation comparable and makes "too early" say so out loud.
HORIZON_DAYS = int(getattr(config, "COUNTERFACTUAL_HORIZON_DAYS", 10))


# ── Reading what the scans already wrote ──────────────────────────────────────────
def _scan_date(path: Path) -> str:
    """The scan's date, from the filename: candidates_YYYY-MM-DD_HHMM.json."""
    stem = path.stem.replace("candidates_", "")
    return stem.split("_")[0]


def dedup_key(c: Dict) -> str:
    """One spread, however many scans saw it.

    A 39-DTE spread reappears in every scan until it drifts out of the delta band, so counting
    each sighting would weight a long-lived candidate 20x against a one-day one and make every
    sample size a fiction.
    """
    return (f"{c.get('ticker')}-{c.get('short_strike')}/{c.get('long_strike')}"
            f"-{c.get('expiration')}")


def iter_snapshot_candidates(snapshot_dir: Optional[Path] = None) -> Iterator[Dict]:
    """Every candidate every scan recorded, oldest first, annotated with its scan date."""
    d = Path(snapshot_dir or SNAPSHOT_DIR)
    if not d.exists():
        return
    for path in sorted(d.glob("candidates_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:                       # pragma: no cover - defensive
            logger.debug("[counterfactuals] unreadable snapshot %s: %s", path.name, e)
            continue
        scan_date = _scan_date(path)
        for row in data.get("rows") or []:
            for c in row.get("candidates") or []:
                if not (c.get("ticker") and c.get("short_strike") and c.get("expiration")):
                    continue
                yield {**c, "scan_date": scan_date, "snapshot": path.name,
                       "row_price": row.get("price"),
                       "iv_rank": (row.get("ctx") or {}).get("iv_rank")}


def first_sightings(candidates: Sequence[Dict]) -> List[Dict]:
    """One record per spread, from the scan that saw it first.

    First sighting, not last: the question is what a decision made AT THAT MOMENT would have
    led to, and a later sighting has already had part of the outcome happen to it.
    """
    seen: Dict[str, Dict] = {}
    for c in sorted(candidates, key=lambda x: (x.get("scan_date") or "", x.get("snapshot") or "")):
        seen.setdefault(dedup_key(c), c)
    return list(seen.values())


# ── Resolution ────────────────────────────────────────────────────────────────────
def resolve(candidate: Dict, price_history, horizon_days: Optional[int] = None) -> Dict:
    """What the underlying did after the scan. Returns the outcome fields only.

    `price_history` is a DataFrame indexed by date with Low/Close, as fetcher.get_price_data
    returns. Touch is measured on the LOW, not the close: a spread does not care that price
    recovered by 4pm, and the delta breach that closes it fires intraday.

    `touched` is the comparable measure — was the strike touched within HORIZON_DAYS trading
    days of the scan — and is None until the spread has actually lived that long.
    `touched_to_date` is the informational one and is always available. Only the first is fed
    to value_of_information, because only the first gives every spread the same exposure.
    """
    horizon = int(horizon_days if horizon_days is not None else HORIZON_DAYS)
    out = {"touched": None, "touched_to_date": None, "min_low_since": None,
           "days_observed": 0, "horizon_days": horizon, "horizon_complete": False,
           "held_at_expiry": None, "close_at_expiry": None}
    if price_history is None or getattr(price_history, "empty", True):
        return out
    try:
        short = float(candidate["short_strike"])
        scan = str(candidate.get("scan_date"))
        exp = str(candidate.get("expiration"))[:10]

        after = price_history[price_history.index.strftime("%Y-%m-%d") > scan]
        if after.empty:
            return out
        out["days_observed"] = int(len(after))
        out["min_low_since"] = round(float(after["Low"].min()), 4)
        out["touched_to_date"] = bool(float(after["Low"].min()) <= short)

        # The comparable read: the same number of days for every spread, or nothing.
        out["horizon_complete"] = out["days_observed"] >= horizon
        if out["horizon_complete"]:
            window = after.iloc[:horizon]
            out["touched"] = bool(float(window["Low"].min()) <= short)

        expired = after[after.index.strftime("%Y-%m-%d") >= exp]
        if not expired.empty:
            close = float(expired["Close"].iloc[0])
            out["close_at_expiry"] = round(close, 4)
            out["held_at_expiry"] = bool(close > short)
    except Exception as e:                           # pragma: no cover - defensive
        logger.debug("[counterfactuals] resolve failed for %s: %s", candidate.get("ticker"), e)
    return out


def _record(candidate: Dict, outcome: Dict) -> Dict:
    gates = candidate.get("gates") or {}
    failed = sorted(k for k, v in gates.items() if not v)
    return {
        "key": dedup_key(candidate),
        "ticker": candidate.get("ticker"),
        "scan_date": candidate.get("scan_date"),
        "snapshot": candidate.get("snapshot"),
        "expiration": candidate.get("expiration"),
        "dte_at_scan": candidate.get("dte"),
        "short_strike": candidate.get("short_strike"),
        "long_strike": candidate.get("long_strike"),
        "spot_at_scan": candidate.get("spot") or candidate.get("row_price"),
        "natural_credit_per_share": candidate.get("natural_credit_per_share"),
        "natural_credit_to_width": candidate.get("natural_credit_to_width"),
        "short_delta": candidate.get("short_delta"),
        "true_pop": candidate.get("true_pop"),
        "pop_implied": candidate.get("pop_implied"),
        "edge_score": candidate.get("edge_score"),
        "iv_rank": candidate.get("iv_rank"),
        "gates": gates,
        "failed_gates": failed,
        "qualified": bool(gates) and not failed,
        # The single-gate sample. A candidate failing three gates says nothing about any one of
        # them; a candidate failing exactly one is the only clean read on that gate's value.
        "sole_failed_gate": failed[0] if len(failed) == 1 else None,
        **outcome,
        "resolved_at": datetime.now().isoformat(),
    }


def build(snapshot_dir: Optional[Path] = None, ledger: Optional[Path] = None,
          fetch=None) -> int:
    """Re-resolve every candidate the snapshots still hold, MERGED over what is already here.

    Two properties are in tension and both matter.

    An unexpired candidate's outcome legitimately CHANGES as time passes — untouched today,
    touched next week — so its row has to be rewritten rather than appended, or the ledger
    becomes a pile of superseded rows.

    But this ledger outlives its own source. output/candidates/ is gitignored under a comment
    describing it as a regenerated artifact directory, and it is not: a past scan cannot be
    re-run, so a pruned snapshot is a permanently lost observation. A wholesale rewrite would
    therefore quietly delete the counterfactual history the moment anyone cleaned that folder.

    So: rows whose snapshot is still present are re-resolved, and rows whose snapshot has gone
    are KEPT as last resolved and marked `source_snapshot_missing`. That makes this file, not
    the snapshot directory, the durable record — which is what it has to be, because it is
    ~200x smaller and is the thing worth backing up.
    """
    path = Path(ledger or LEDGER)
    if fetch is None:                                # pragma: no cover - network
        from data import fetcher
        fetch = lambda tk: fetcher.get_price_data(tk, period="6mo")

    existing = {r.get("key"): r for r in load(path)}

    cands = first_sightings(list(iter_snapshot_candidates(snapshot_dir)))
    history: Dict[str, object] = {}
    fresh: Dict[str, Dict] = {}
    for c in cands:
        tk = c["ticker"]
        if tk not in history:
            try:
                history[tk] = fetch(tk)
            except Exception as e:                   # pragma: no cover - network
                logger.debug("[counterfactuals] no history for %s: %s", tk, e)
                history[tk] = None
        fresh[dedup_key(c)] = _record(c, resolve(c, history[tk]))

    orphaned = 0
    for key, row in existing.items():
        if key not in fresh:
            row["source_snapshot_missing"] = True
            fresh[key] = row
            orphaned += 1
    if orphaned:
        logger.warning(
            "[counterfactuals] %d spread(s) kept from the ledger with no surviving snapshot. "
            "A scan cannot be re-run, so these observations exist nowhere else.", orphaned)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in fresh.values()),
                   encoding="utf-8")
    tmp.replace(path)
    return len(fresh)


def load(ledger: Optional[Path] = None) -> List[Dict]:
    path = Path(ledger or LEDGER)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── The question this module exists to answer ─────────────────────────────────────
def _touch_rate(records: Sequence[Dict]) -> Optional[float]:
    obs = [r for r in records if r.get("touched") is not None]
    return (sum(1 for r in obs if r["touched"]) / len(obs)) if obs else None


def value_of_information(records: Optional[Sequence[Dict]] = None) -> Dict:
    """Per gate: did the candidates it blocked actually behave worse than the ones that passed?

    Compares candidates whose ONLY failure was gate G against candidates that passed every
    gate. A gate earning its place blocks spreads that go on to be touched more often than the
    ones it let through. A gate whose blocked candidates do just as well is costing
    opportunity and buying nothing — it belongs in the ranking function, not the contract.

    `lift` is (blocked touch rate − qualified touch rate) in percentage points. Positive means
    the gate is doing work. Negative means it is rejecting better-than-average spreads.

    This does NOT prove causation and is not a licence to delete a gate on one reading. It is
    the first evidence any of the eleven has ever had, and a negative lift is a reason to
    investigate, not to act.
    """
    all_recs = list(records if records is not None else load())
    # Only spreads that have lived the full horizon. Everything else is still maturing, and
    # counting it as untouched is how "we looked too early" turns into "the gate is useless".
    recs = [r for r in all_recs if r.get("horizon_complete")]
    maturing = len(all_recs) - len(recs)

    qualified = [r for r in recs if r.get("qualified")]
    base = _touch_rate(qualified)

    gates: Dict[str, Dict] = {}
    for gate in getattr(config, "REQUIRED_GATES", ()):
        blocked = [r for r in recs if r.get("sole_failed_gate") == gate]
        rate = _touch_rate(blocked)
        entry: Dict = {"n_blocked": len(blocked), "touch_rate": rate}
        if len(blocked) < MIN_GATE_SAMPLE or rate is None or base is None:
            entry["verdict"] = "insufficient"
            entry["reason"] = (f"{len(blocked)} candidates failed only this gate and have "
                               f"lived {HORIZON_DAYS} trading days ({MIN_GATE_SAMPLE} needed).")
        else:
            entry["lift_pp"] = round((rate - base) * 100, 1)
            entry["verdict"] = "earns_its_place" if rate > base else "no_measured_value"
        gates[gate] = entry

    return {
        "n_records": len(recs),
        "n_maturing": maturing,
        "n_total": len(all_recs),
        "horizon_days": HORIZON_DAYS,
        "n_qualified": len(qualified),
        "qualified_touch_rate": base,
        "min_sample": MIN_GATE_SAMPLE,
        "gates": gates,
        "caveats": [
            "Snapshots keep the top 3 candidates per ticker by natural credit-to-width, so "
            "this measures gates within that band and cannot speak for spreads that never "
            "made the top 3.",
            "Touch is not loss. A touched spread may still expire worthless; an untouched one "
            "was never at risk. Touch is the leading indicator, not the outcome.",
            "held_at_expiry is the cleaner measure and stays None until contracts expire.",
        ],
    }


def report(records: Optional[Sequence[Dict]] = None) -> str:
    """The above, as something a person can read."""
    v = value_of_information(records)
    base = v["qualified_touch_rate"]
    lines = [
        "VEGA — value of information, per gate",
        f"  horizon: {v['horizon_days']} trading days after the scan, identical for every spread",
        f"  {v['n_total']} spreads on record · {v['n_records']} have lived the full horizon · "
        f"{v['n_maturing']} still maturing",
        f"  {v['n_qualified']} of the measurable ones passed every gate",
        (f"  baseline: {base*100:.0f}% of qualified spreads were touched"
         if base is not None else
         "  baseline: NOT YET MEASURABLE — no qualified spread has lived the full horizon."),
        "",
        f"  {'gate':<26}{'n':>5}{'touched':>10}{'lift':>9}   verdict",
    ]
    for gate, d in sorted(v["gates"].items(),
                          key=lambda kv: -(kv[1].get("lift_pp") or -999)):
        rate = f"{d['touch_rate']*100:.0f}%" if d.get("touch_rate") is not None else "—"
        lift = f"{d['lift_pp']:+.0f}pp" if d.get("lift_pp") is not None else "—"
        lines.append(f"  {gate:<26}{d['n_blocked']:>5}{rate:>10}{lift:>9}   {d['verdict']}")
    lines += ["", "  Caveats:"] + [f"   - {c}" for c in v["caveats"]]
    return "\n".join(lines)


if __name__ == "__main__":                           # pragma: no cover - CLI
    logging.basicConfig(level=logging.WARNING)
    n = build()
    print(f"\nResolved {n} spreads into {LEDGER.name}\n")
    print(report())
