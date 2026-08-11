#!/usr/bin/env python3
"""
clv_tracker.py — VEGA's ever-learning scorecard (Closing-Line-Value style).

WHY (the sports-betting analogy):
    In sports betting you grade a bet against where the line *closed*, not just whether
    it won. Beating the closing line is a leading, low-variance signal that you had real
    edge — it shows up before the game is even played. A single win or loss is noise;
    persistent closing-line value is the edge.

    VEGA's equivalent: when you SELL a credit spread, its price shrinks from two separate
    forces — (1) theta (pure time decay, which happens even with zero edge) and (2) the
    underlying moving your way / IV falling (that's your edge showing up). Theta gives a
    "no-edge baseline": what the spread should be worth today from the clock alone.

        CLV_per_share = theta_expected_mark  −  actual_mark
        (+ = the spread is cheaper than time-decay alone can explain = the market moved
           toward your thesis early = you "beat the line". Leading signal of real edge.)

    The NEWS-CATALYST flag is the "injury" analog: if a position moves hard against you AND
    a material story hit that ticker in the same window, we tag it so those trades can be
    separated out — an exogenous shock shouldn't be charged against the model's calibration,
    just as you'd discard a bet lost to a freak injury when grading your handicapping.

This module is dependency-free (stdlib only) and NEVER raises into a caller — every public
function returns a plain dict/list and swallows bad records. It only READS the ledger
(logs/vega_outcomes.jsonl); the logging hooks live in analysis/outcome_logger.py.

CLI:
    python clv_tracker.py                # human-readable scorecard
    python clv_tracker.py --json         # machine-readable summary (for the cockpit / API)
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
OUTCOMES_FILE = BASE_DIR / "logs" / "vega_outcomes.jsonl"

# A position counts as an adverse move (candidate for a news catalyst) when its mark has
# risen this fraction above the entry credit — i.e. it's moved meaningfully against you.
ADVERSE_MOVE_FRAC = 0.25


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────
def _parse_ts(t) -> Optional[datetime]:
    if not t:
        return None
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(str(t)[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def load_ledger(path: Optional[Path] = None) -> List[Dict]:
    """Read every outcome record. Bad lines are skipped, never fatal."""
    p = Path(path) if path else OUTCOMES_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLV — mark vs theta baseline
# ─────────────────────────────────────────────────────────────────────────────
def clv_for_record(r: Dict) -> Optional[Dict]:
    """CLV for one open/closed record. Returns None if it lacks the fields to score.

    Prefers an explicitly-logged `theta_expected_mark`. Otherwise builds the no-edge baseline
    from TIME REMAINING, not from the short leg's theta.

    The old proxy was `entry - |short_theta| * days`, and it made the whole metric
    unwinnable. `short_theta` is the SHORT LEG's decay; a credit spread's net decay is the
    short leg's minus the long leg's, several times smaller, because the long leg is decaying
    in your favour at the same time. Charging the position the short leg's full theta
    overstated decay by roughly 3-10x: measured on the live ledger, META's short-leg theta
    alone would have consumed its entire $1.71 credit in 3.5 days against a 16-day hold. The
    result then clamped at zero, so 16% of records carried a baseline of exactly 0.00 — and
    once the baseline is zero, CLV = -mark, which is negative for every positive mark. A 9.3%
    beat rate over 75 records was measuring the formula, not the trades.

    The replacement is the null model the docstring at the top of this file already describes:
    what the spread is worth from the clock alone. With no price move and no vol change, a
    credit spread bleeds toward zero over its life, so the zero-edge mark is the entry credit
    scaled by the fraction of the original term still outstanding:

        theta_expected = entry * (dte_remaining / dte_at_entry)

    Assumption-light, uses only fields every record already carries, and — the property that
    matters — it can come out either way. It is an approximation: real decay is convex and
    accelerates near expiry, so this baseline is slightly generous early in a hold and
    slightly harsh late. That is a known bias in a known direction, which is a different thing
    from a number that cannot be positive.
    """
    entry = _f(r.get("modeled_credit_per_share"))
    if entry is None:
        entry = _f(r.get("actual_fill_credit"))
    # actual current price of the spread (per share); for closed use exit_price
    mark = _f(r.get("current_mark"))
    if mark is None and r.get("status") == "closed":
        mark = _f(r.get("exit_price"))
    if entry is None or mark is None:
        return None

    theta_exp = _f(r.get("theta_expected_mark"))
    days = None
    if theta_exp is None:
        dte0 = _f(r.get("dte"))
        opened = _parse_ts(r.get("opened_at") or r.get("filled_at") or r.get("scan_ts"))
        marked = _parse_ts(r.get("marked_at") or r.get("closed_at") or r.get("opened_at"))
        if not dte0 or dte0 <= 0 or not opened or not marked:
            # No term to scale against. Excluded from the scorecard rather than scored on a
            # baseline that would have to be invented — an ungradeable trade must not become
            # a graded one just because the grader had a fallback.
            return None
        days = max(0, (marked - opened).days)
        remaining = max(0.0, dte0 - days)
        theta_exp = entry * (remaining / dte0)

    clv = theta_exp - mark
    # A tie at zero is the maximum win, not a miss. At expiry the baseline is 0 by
    # construction, so a spread that expired worthless — the whole credit captured — scored
    # clv == 0 and failed a strict `> 0` test. Nothing beats capturing everything available.
    beat = clv > 0 or (mark <= 0.0 and entry > 0)
    adverse = mark > entry * (1 + ADVERSE_MOVE_FRAC)
    return {
        "id": r.get("id"), "ticker": r.get("ticker"), "strategy": r.get("strategy"),
        "status": r.get("status"), "entry": entry, "mark": mark,
        "theta_expected": theta_exp, "clv": clv, "days": days,
        "beat": beat, "adverse": adverse,
        "news_verdict": ((r.get("news_check") or {}).get("verdict")
                         if isinstance(r.get("news_check"), dict) else r.get("news_verdict")),
        "news_catalyst": bool(r.get("news_catalyst")),
        "catalyst_headline": r.get("catalyst_headline"),
    }


def clv_records(rows: List[Dict]) -> List[Dict]:
    out = []
    for r in rows:
        if r.get("status") in ("open", "closed"):
            c = clv_for_record(r)
            if c:
                out.append(c)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Calibration — predicted POP vs realized hit-rate (reliability curve)
# ─────────────────────────────────────────────────────────────────────────────
def _cohort_of(r: Dict) -> str:
    """The comparability key for one record, via outcome_logger.

    Read through that module rather than off a raw field: it derives the key from
    fill_model | gate_basis | close_logic, and history is deliberately not rewritten so older
    trades carry no field of their own.
    """
    try:
        from analysis import outcome_logger as ol
        return ol.cohort(r)
    except Exception as e:                               # pragma: no cover - defensive
        # Distinct sentinel per record is wrong (it would fragment), but a silent single
        # bucket is worse: it re-pools every regime and suppresses the multi-cohort warning
        # while looking exactly like a clean ledger. Name the failure so it reads as one.
        logging.getLogger(__name__).warning("[clv] cohort lookup failed: %s", e)
        return "cohort-unavailable"


def calibration_curve(rows: List[Dict], bins=((0, .7), (.7, .8), (.8, .9), (.9, 1.01)),
                      cohort: Optional[str] = None) -> List[Dict]:
    """Predicted vs realized win rate by POP bucket, for ONE cohort at a time.

    `cohort=None` pools everything, which is what this did unconditionally and what makes the
    pooled number meaningless on the current ledger: mid-fill trades won 13 of 18 and
    natural-fill trades won 0 of 46, so pooling them reports a 56.8pp calibration miss that
    describes the fill model rather than the POP model. vega_status has refused to pool these
    since the cohorts were defined — "Cohorts are not comparable" — and this function was the
    one place still doing it, feeding the number straight onto the cockpit's Track Record tab.
    """
    closed = [r for r in rows if r.get("status") == "closed" and r.get("outcome") in ("win", "loss")]
    if cohort is not None:
        closed = [r for r in closed if _cohort_of(r) == cohort]
    curve = []
    for lo, hi in bins:
        b = [r for r in closed if lo <= (_f(r.get("modeled_pop")) or -1) < hi]
        n = len(b)
        wins = sum(1 for r in b if r.get("outcome") == "win")
        pred = (sum(_f(r.get("modeled_pop")) or 0 for r in b) / n) if n else None
        real = (wins / n) if n else None
        curve.append({"lo": lo, "hi": hi, "n": n, "predicted": pred, "realized": real,
                      "gap": (real - pred) if (pred is not None and real is not None) else None})
    return curve


# ─────────────────────────────────────────────────────────────────────────────
# Edge retention — modeled edge vs realized net P/L
# ─────────────────────────────────────────────────────────────────────────────
def edge_retention(rows: List[Dict]) -> Dict:
    closed = [r for r in rows if r.get("status") == "closed"]
    net = [_f(r.get("realized_net_pl_per_contract")) for r in closed]
    net = [x for x in net if x is not None]
    modeled_edge = [_f(r.get("edge_points")) for r in closed]
    modeled_edge = [x for x in modeled_edge if x is not None]
    return {
        "n_closed": len(closed),
        "avg_realized_net_pl": (sum(net) / len(net)) if net else None,
        "avg_modeled_edge_pp": (sum(modeled_edge) / len(modeled_edge)) if modeled_edge else None,
        "total_realized_net_pl": sum(net) if net else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# News split — does VEGA's news verdict earn its keep? + catalyst quarantine
# ─────────────────────────────────────────────────────────────────────────────
def news_split(clv_recs: List[Dict]) -> Dict:
    def agg(recs):
        n = len(recs)
        if not n:
            return {"n": 0, "beat_rate": None, "avg_clv": None}
        beat = sum(1 for r in recs if r["beat"])
        return {"n": n, "beat_rate": beat / n, "avg_clv": sum(r["clv"] for r in recs) / n}
    confirms = [r for r in clv_recs if (r.get("news_verdict") or "").upper() == "CONFIRMS"]
    other = [r for r in clv_recs if (r.get("news_verdict") or "").upper() != "CONFIRMS"]
    catalysts = [r for r in clv_recs if r.get("news_catalyst")]
    return {"confirms": agg(confirms), "other": agg(other),
            "catalyst_flagged": [{"ticker": r["ticker"], "clv": r["clv"],
                                  "headline": r.get("catalyst_headline")} for r in catalysts]}


# ─────────────────────────────────────────────────────────────────────────────
# Summary — the single object the cockpit / API consumes
# ─────────────────────────────────────────────────────────────────────────────
def freshness(rows: List[Dict]) -> Dict:
    """Are the open-position marks current? The CLV/unrealized figures are only as fresh as
       the last re-mark. If auto_paper_cycle stops re-marking, everything here silently freezes —
       so surface the staleness loudly."""
    opens = [r for r in rows if r.get("status") == "open"]
    marks = [_parse_ts(r.get("marked_at")) for r in opens]
    marks = [m for m in marks if m]
    if not marks:
        return {"n_open": len(opens), "last_mark": None, "days_stale": None, "stale": bool(opens)}
    newest = max(marks)
    # naive vs tz-aware safety
    try:
        from datetime import datetime as _dt
        now = _dt.now(newest.tzinfo) if newest.tzinfo else _dt.now()
        days = (now - newest).days
    except Exception:
        days = None
    return {"n_open": len(opens), "last_mark": newest.strftime("%Y-%m-%d %H:%M"),
            "days_stale": days, "stale": (days is not None and days >= 2)}


def summary(path: Optional[Path] = None) -> Dict:
    rows = load_ledger(path)
    recs = clv_records(rows)
    fresh = freshness(rows)
    # raw vs ex-catalyst (quarantine exogenous news shocks, like discarding an injury loss)
    ex = [r for r in recs if not r.get("news_catalyst")]

    def clv_stats(rs):
        if not rs:
            return {"n": 0, "beat_rate": None, "avg_clv": None}
        beat = sum(1 for r in rs if r["beat"])
        return {"n": len(rs), "beat_rate": beat / len(rs),
                "avg_clv": sum(r["clv"] for r in rs) / len(rs)}

    # Calibration, per cohort. The headline number reports the LARGEST cohort alone and says
    # which one it is, because a single figure spanning incompatible selection and close rules
    # is not a model verdict — it is an average of two different systems.
    closed_rows = [r for r in rows if r.get("status") == "closed"
                   and r.get("outcome") in ("win", "loss")]
    cohorts: Dict[str, int] = {}
    for r in closed_rows:
        k = _cohort_of(r)
        cohorts[k] = cohorts.get(k, 0) + 1

    def _gap(cur):
        g = [c for c in cur if c["n"]]
        return (sum(c["gap"] * c["n"] for c in g) / sum(c["n"] for c in g)) if g else None

    by_cohort = []
    for name, n in sorted(cohorts.items(), key=lambda kv: -kv[1]):
        cur = calibration_curve(rows, cohort=name)
        by_cohort.append({"cohort": name, "n": n,
                          "gap_pp": (lambda g: g * 100 if g is not None else None)(_gap(cur)),
                          "curve": cur})

    # Prefer a cohort the system considers analysable. Picking purely by size drew the
    # cockpit's calibration verdict from 41 records that analysis_eligible() rejects as
    # broken-thermometer readings — selected on a mid basis the desk could not execute.
    try:
        from analysis import outcome_logger as _ol
        _eligible = {c["cohort"] for c in by_cohort
                     if any(_ol.analysis_eligible(r) for r in closed_rows
                            if _cohort_of(r) == c["cohort"])}
    except Exception:                                    # pragma: no cover - defensive
        _eligible = set()
    _ranked = ([c for c in by_cohort if c["cohort"] in _eligible]
               or by_cohort)
    lead = _ranked[0] if _ranked else None
    eligible_n = sum(c["n"] for c in by_cohort if c["cohort"] in _eligible)
    curve = lead["curve"] if lead else calibration_curve(rows)
    cal_gap = (lead["gap_pp"] / 100) if (lead and lead["gap_pp"] is not None) else None

    return {
        "counts": {"total": len(rows),
                   "modeled": sum(1 for r in rows if r.get("status") == "modeled"),
                   "open": sum(1 for r in rows if r.get("status") == "open"),
                   "closed": sum(1 for r in rows if r.get("status") == "closed")},
        "clv": clv_stats(recs),
        "clv_ex_catalyst": clv_stats(ex),
        "calibration_gap_pp": (cal_gap * 100) if cal_gap is not None else None,
        "calibration_curve": curve,
        # Which cohort the headline gap describes, and whether anything was left out of it.
        # Without these two the number reads as "the model", when it is one regime of several.
        "calibration_cohort": (lead or {}).get("cohort"),
        "calibration_cohort_n": (lead or {}).get("n"),
        "calibration_cohorts_present": len(cohorts),
        # How much of the ledger is analysable at all. Zero here means every calibration
        # number on the page is drawn from trades the system itself calls unusable.
        "calibration_eligible_n": eligible_n,
        "calibration_lead_eligible": bool(lead and lead["cohort"] in _eligible),
        "calibration_by_cohort": by_cohort,
        "edge_retention": edge_retention(rows),
        "freshness": fresh,
        "news": news_split(recs),
        "records": sorted(recs, key=lambda r: r["clv"]),  # worst→best; worst are catalyst suspects
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _print_human(s: Dict) -> None:
    c = s["counts"]
    print(f"VEGA CLV scorecard — {c['total']} records "
          f"({c['modeled']} modeled / {c['open']} open / {c['closed']} closed)\n")
    clv = s["clv"]
    if clv["n"]:
        print(f"CLV (mark vs theta): {clv['n']} scored — "
              f"{clv['beat_rate']*100:.0f}% beat the line, avg CLV ${clv['avg_clv']:+.3f}/share")
        ex = s["clv_ex_catalyst"]
        if ex["n"] != clv["n"]:
            print(f"   ex-catalyst      : {ex['n']} scored — "
                  f"{ex['beat_rate']*100:.0f}% beat, avg ${ex['avg_clv']:+.3f}/share "
                  f"(news shocks quarantined)")
    else:
        print("CLV: no scorable positions yet (need current_mark + theta on open records).")
    cg = s["calibration_gap_pp"]
    # Name the cohort. This figure moved from pooled to cohort-scoped, and an unlabelled
    # number that silently changed meaning is worse than the pooled one it replaced.
    if cg is None:
        print("Calibration gap    : — (need closed win/loss records)")
    else:
        _ch = s.get("calibration_cohort") or "?"
        _n = s.get("calibration_cohort_n") or 0
        _tot = s.get("counts", {}).get("closed") or 0
        print(f"Calibration gap    : {cg:+.0f}pp (realized − predicted POP)")
        print(f"  cohort           : {_ch}  n={_n} of {_tot} closed")
        if (s.get("calibration_cohorts_present") or 1) > 1:
            print(f"  NOT POOLED       : {s['calibration_cohorts_present']} cohorts in the ledger; "
                  f"{_tot - _n} closed trades are excluded from this figure.")
        if not s.get("calibration_lead_eligible"):
            print("  WARNING          : no cohort passes analysis_eligible — every trade here was "
                  "selected or filled on a basis the desk could not execute.")
    er = s["edge_retention"]
    if er["avg_realized_net_pl"] is not None:
        print(f"Edge retention     : avg realized net P/L ${er['avg_realized_net_pl']:+.2f}/ct "
              f"over {er['n_closed']} closed; total ${er['total_realized_net_pl']:+.2f}")
    nw = s["news"]
    cf, ot = nw["confirms"], nw["other"]
    if cf["n"] or ot["n"]:
        def fmt(a): return (f"{a['beat_rate']*100:.0f}% beat / ${a['avg_clv']:+.3f}"
                            if a["n"] else "—")
        print(f"News CONFIRMS  n={cf['n']}: {fmt(cf)}   |   other n={ot['n']}: {fmt(ot)}")
    if nw["catalyst_flagged"]:
        print("Catalyst-flagged (news shocks):")
        for x in nw["catalyst_flagged"]:
            print(f"   {x['ticker']:5} CLV ${x['clv']:+.2f}  {x.get('headline') or ''}")


def main():
    s = summary()
    if "--json" in sys.argv:
        print(json.dumps(s, indent=2, default=str))
    else:
        _print_human(s)


if __name__ == "__main__":
    main()
