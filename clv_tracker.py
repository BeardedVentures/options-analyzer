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

    Prefers an explicitly-logged `theta_expected_mark` (written by the grader at re-mark
    time, the most accurate). Falls back to a live proxy: entry credit minus |theta|·days,
    using whatever mark and timestamps the record carries.
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
        theta = _f(r.get("short_theta"))
        opened = _parse_ts(r.get("opened_at") or r.get("filled_at") or r.get("scan_ts"))
        marked = _parse_ts(r.get("marked_at") or r.get("closed_at") or r.get("opened_at"))
        if theta is None or not opened or not marked:
            return None
        days = max(0, (marked - opened).days)
        theta_exp = max(0.0, entry - abs(theta) * days)

    clv = theta_exp - mark
    adverse = mark > entry * (1 + ADVERSE_MOVE_FRAC)
    return {
        "id": r.get("id"), "ticker": r.get("ticker"), "strategy": r.get("strategy"),
        "status": r.get("status"), "entry": entry, "mark": mark,
        "theta_expected": theta_exp, "clv": clv, "days": days,
        "beat": clv > 0, "adverse": adverse,
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
def calibration_curve(rows: List[Dict], bins=((0, .7), (.7, .8), (.8, .9), (.9, 1.01))) -> List[Dict]:
    closed = [r for r in rows if r.get("status") == "closed" and r.get("outcome") in ("win", "loss")]
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

    curve = calibration_curve(rows)
    graded = [c for c in curve if c["n"]]
    cal_gap = (sum(c["gap"] * c["n"] for c in graded) / sum(c["n"] for c in graded)) if graded else None

    return {
        "counts": {"total": len(rows),
                   "modeled": sum(1 for r in rows if r.get("status") == "modeled"),
                   "open": sum(1 for r in rows if r.get("status") == "open"),
                   "closed": sum(1 for r in rows if r.get("status") == "closed")},
        "clv": clv_stats(recs),
        "clv_ex_catalyst": clv_stats(ex),
        "calibration_gap_pp": (cal_gap * 100) if cal_gap is not None else None,
        "calibration_curve": curve,
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
    print(f"Calibration gap    : {cg:+.0f}pp (realized − predicted POP)" if cg is not None
          else "Calibration gap    : — (need closed win/loss records)")
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
