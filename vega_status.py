#!/usr/bin/env python3
"""
vega_status.py — one command that answers "is this thing working, and what has it learned?"

    python vega_status.py

Written for the person running the system rather than the person building it. Everything here
is read-only: it opens no positions, closes nothing, and writes nothing.

The three questions it exists to answer, in order:
  1. Is the machinery alive — is the cycle running, is the data fresh, are positions marked?
  2. What is the record — separated by cohort, because the pre-2026-08-06 trades were closed
     by a stop that fired at t=0 on bid-ask spread and cannot be pooled with anything.
  3. What has it learned — prediction calibration and what memory can and cannot yet say.
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import config
from analysis import outcome_logger as ol


G, R, Y, D, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[1m", "\033[0m"


def _c(s, col):
    return f"{col}{s}{X}"


def _hdr(t):
    print(f"\n{B}{t}{X}\n" + "─" * 74)


def _age(ts):
    try:
        return datetime.now() - datetime.fromisoformat(str(ts))
    except Exception:
        return None


def health():
    _hdr("1 · MACHINERY")
    cands = sorted(glob.glob(str(BASE / "output" / "candidates" / "candidates_*.json")))
    if cands:
        age = _age(datetime.fromtimestamp(Path(cands[-1]).stat().st_mtime).isoformat())
        h = age.total_seconds() / 3600 if age else 99
        print(f"  last candidate scan   {Path(cands[-1]).name}  "
              f"{_c(f'{h:.1f}h ago', G if h < 4 else (Y if h < 24 else R))}")
    else:
        print(f"  last candidate scan   {_c('NONE — the cycle has never produced candidates', R)}")

    scan = BASE / "logs" / "scan_latest.json"
    if scan.exists():
        try:
            d = json.loads(scan.read_text(encoding="utf-8"))
            age = _age(d.get("timestamp"))
            h = age.total_seconds() / 3600 if age else 99
            print(f"  last engine scan      {len(d.get('qualified_trades') or [])} qualified  "
                  f"{_c(f'{h:.1f}h ago', G if h < 24 else Y)}")
        except Exception:
            print(f"  last engine scan      {_c('unreadable', R)}")

    rows = ol.load_records()
    open_ = [r for r in rows if r.get("status") == "open"]
    stale = [r for r in open_ if (_age(r.get("marked_at")) or timedelta(days=9)) > timedelta(hours=8)]
    print(f"  open positions        {len(open_)}"
          + (f"   {_c(str(len(stale)) + ' not marked in 8h', Y)}" if stale else f"   {_c('all marked recently', G)}"))

    flags = [("Ravens close logic", "RAVENS_FRAMEWORK_ENABLED"),
             ("Prediction ledger", "PREDICTION_LEDGER_ENABLED"),
             ("Horizon calibration", "HORIZON_CALIBRATION_ENABLED"),
             ("Vol surface", "TERM_STRUCTURE_ENABLED")]
    on = [n for n, k in flags if getattr(config, k, False)]
    off = [n for n, k in flags if not getattr(config, k, False)]
    print(f"  subsystems on         {', '.join(on) if on else 'none'}")
    if off:
        print(f"  subsystems OFF        {_c(', '.join(off), Y)}")
    print(f"  close decision basis  {getattr(config, 'CLOSE_DECISION_MARK_BASIS', 'natural')}"
          f"   (hard floor {getattr(config, 'WOLF_STOP_MULTIPLIER', '?')}x credit)")


def record():
    _hdr("2 · THE RECORD, BY COHORT")
    rows = ol.load_records()
    closed = [r for r in rows if r.get("status") == "closed"]
    if not closed:
        print("  no closed trades yet")
        return
    groups = {}
    for r in closed:
        groups.setdefault(ol.close_cohort(r), []).append(r)
    for cohort, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        wins = [r for r in rs if (r.get("outcome") or "").lower() == "win"]
        pl = [r.get("realized_net_pl_per_contract") or 0 for r in rs]
        stops = [r for r in rs if "stop" in (r.get("exit_reason") or "")]
        wr = len(wins) / len(rs) * 100
        print(f"  {cohort}")
        print(f"      n={len(rs)}  win rate {_c(f'{wr:.0f}%', G if wr >= 60 else R)}  "
              f"net ${sum(pl):+,.0f}  stop-outs {len(stops)}/{len(rs)}")
    if len(groups) > 1:
        print(f"\n  {_c('Cohorts are not comparable.', Y)} The legacy cohort was closed by a 1.5x")
        print("  credit stop marked natural-in/natural-out, which fired at t=0 on bid-ask")
        print("  spread alone. Grade the ravens cohort on its own.")


def learning():
    _hdr("3 · WHAT IT HAS LEARNED")
    try:
        from analysis import predictions as pred
        g = pred.grade()
        print(f"  predictions   {g['total_claims']} made · {g['resolved']} resolved · "
              f"{g['open']} awaiting their horizon")
        if not g["by_type"]:
            print(f"  {_c('Nothing resolved yet. Claims resolve at expiry — first grades in ~30 days.', D)}")
        for t, v in sorted(g["by_type"].items(), key=lambda kv: -kv[1]["n"]):
            col = G if v["gradeable"] and (v["brier"] or 1) < 0.25 else (Y if v["gradeable"] else D)
            print(f"    {t:18} n={v['n']:<3} hit {v['hit_rate']:>5.1f}%  "
                  f"brier {str(v['brier'] or '—'):>6}  {_c(v['verdict'][:64], col)}")
    except Exception as e:
        print(f"  prediction ledger unavailable: {e}")

    try:
        from analysis.calibration_engine import load_and_analyse
        c = load_and_analyse()
        print(f"\n  calibration   {c['sample_size']} closed · win {c.get('overall_win_rate')}% "
              f"vs modelled {c.get('modeled_pop_avg')}% · gap {c.get('calibration_gap_pts')}pp")
        ea = c.get("exit_analysis") or {}
        if ea.get("verdict"):
            print(f"    {_c('!', Y)} {ea['verdict'][:150]}")
        for u in (c.get("untestable") or [])[:3]:
            print(f"    {_c('·', D)} {u['component']}: {u['reason']}")
    except Exception as e:
        print(f"  calibration unavailable: {e}")

    rows = ol.load_records()
    snaps = sum(len(r.get("stress_snapshots") or []) for r in rows)
    need = getattr(config, "MUNINN_MIN_COMPARABLE", 5)
    print(f"\n  memory        {snaps} stress snapshots recorded "
          f"({need} comparable needed before Muninn can speak)")
    alerts = [(r.get("id"), a) for r in rows for a in (r.get("raven_alerts") or [])]
    if alerts:
        print(f"\n  {_c('RAVEN ALERTS NEEDING YOUR DECISION:', Y)}")
        for tid, a in alerts[-5:]:
            print(f"    {a.get('recommendation')} · {tid}")
            print(f"      {a.get('plain_english', '')[:110]}")


def next_steps():
    _hdr("WHAT TO WATCH")
    rows = ol.load_records()
    closed_new = [r for r in rows if r.get("status") == "closed"
                  and ol.close_cohort(r) != ol.LEGACY_CLOSE_LOGIC]
    print(f"  · {len(closed_new)}/30 trades closed under the ravens — the sample that will")
    print(f"    actually grade this system. Nothing before it counts.")
    try:
        from analysis import predictions as pred
        g = pred.grade()
        m = getattr(config, "PREDICTION_MIN_FOR_GRADE", 10)
        worst = [(t, v) for t, v in g["by_type"].items() if v["gradeable"]]
        print(f"  · {g['resolved']} predictions resolved; {m} of a type needed before it grades.")
        if worst:
            t, v = max(worst, key=lambda kv: kv[1]["brier"] or 0)
            print(f"  · weakest claim type so far: {t} ({v['verdict'][:60]})")
    except Exception:
        pass
    print("  · run this after each session; it writes nothing and is safe any time.\n")


if __name__ == "__main__":
    print(f"\n{B}VEGA STATUS{X}  ·  {datetime.now():%Y-%m-%d %H:%M}")
    health()
    record()
    learning()
    next_steps()
