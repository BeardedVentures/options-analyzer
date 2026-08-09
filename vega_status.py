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


def data_quality():
    """How much of the chain the last scan actually got to read.

    Sits directly under health because it qualifies everything below it: a record and a
    calibration built on 40%-quotable chains are measurements of the survivors, and until this
    number is on the screen there is no way to tell that from a measurement of the market.
    """
    _hdr("2 · CHAIN DATA QUALITY")
    try:
        from data import data_quality_log as dq
    except Exception as e:
        print(f"  data quality log unavailable: {e}")
        return

    s = dq.latest_scan()
    if not s["count"]:
        print(f"  {_c('No readings yet.', D)} Populates on the next scan "
              f"(fetcher.get_options_chain writes one row per ticker).")
        return

    floor, worst = s["floor"], s["worst_ratio"]
    col = {"green": G, "amber": Y, "red": R}.get(dq.band(worst), D)
    age = _age(s["at"])
    hrs = age.total_seconds() / 3600 if age else None
    print(f"  last scan             {s['count']} tickers · "
          f"{_c(f'{hrs:.1f}h ago', G if hrs < 4 else Y) if hrs is not None else _c('age unknown', D)}")
    print(f"  worst chain           {_c(f'{worst:.0%}', col)} quotable  ({s['worst_ticker']})")
    print(f"  below the {floor:.0%} floor    "
          f"{_c(str(s['below_floor']), R if s['below_floor'] else G)} "
          f"{'ticker skipped' if s['below_floor'] == 1 else 'tickers skipped'}")
    if s["sources"]:
        print(f"  sources               "
              f"{', '.join(f'{k} {v}' for k, v in sorted(s['sources'].items()))}")
    if not getattr(config, "SKEW_SCORING_ENABLED", True):
        print(f"  {_c('skew scoring OFF', Y)} — re-enable once these ratios hold above "
              f"{getattr(config, 'CHAIN_QUALITY_GOOD_RATIO', 0.70):.0%}.")


def btc():
    """The BTC layer: what it reads now, and how its claims are grading.

    Deliberately shown next to the equity record rather than in its own tool. The whole point of
    routing BTC claims into VEGA's prediction ledger instead of a private forecast table is that
    the two become comparable — a separate screen would undo that on the display side.
    """
    if not getattr(config, "BTC_SIGNAL_ENABLED", True):
        return
    _hdr("3 · BITCOIN LAYER  (free data · advisory only)")
    try:
        from data import crypto
        s = crypto.snapshot()
    except Exception as e:
        print(f"  crypto layer unavailable: {e}")
        return

    if not s.get("ok"):
        print(f"  {_c('No BTC read this cycle.', Y)} Deribit or Coinbase did not answer; "
              f"treat as absence of information, not a neutral reading.")
    else:
        vrp = s.get("btc_vrp_pp")
        print(f"  BTC spot              ${s['btc_spot']:,.0f}")
        print(f"  implied (DVOL)        {s['dvol']:.2f}%   realised 30d {s['btc_rv_30d']:.2f}%")
        print(f"  BTC variance premium  {_c(f'{vrp:+.2f}pp', G if (vrp or 0) > 0 else Y)}"
              f"   (implied over realised)")

    try:
        from analysis import btc_signal
        from data import fetcher, technicals
        for tk in sorted(getattr(config, "BTC_PROXY_TICKERS", {"IBIT"})):
            ch = fetcher.get_options_chain(tk, config.MIN_DTE, config.MAX_DTE)
            px = fetcher.get_price_data(tk, period="5d")
            if not ch or px is None or px.empty:
                continue
            iv = technicals.estimate_atm_iv(ch, float(px["Close"].iloc[-1]))
            x = btc_signal.evaluate(tk, iv, s)
            if x.get("available"):
                gap = x["iv_gap_pp"]
                col = Y if abs(gap) >= getattr(config, "BTC_IV_GAP_WIDE_PP", 3.0) else D
                print(f"  {tk} IV vs BTC         {x['proxy_iv_pp']:.2f}% vs {x['dvol']:.2f}%"
                      f"   gap {_c(f'{gap:+.2f}pp', col)}  [{x['reading']}]")
    except Exception as e:
        print(f"  cross-venue read unavailable: {e}")

    try:
        from analysis import btc_forecast as bf
        from analysis import predictions as pred
        g = pred.grade(cohort=bf.COHORT)
        n = g["total_claims"]
        if not n:
            print(f"  forecast claims       {_c('none yet', D)} — the first records on the next cycle")
        else:
            d = (g["by_type"] or {}).get("direction")
            print(f"  forecast claims       {n} made · {g['resolved']} resolved · "
                  f"{g['open']} awaiting their 14-day horizon")
            if d:
                col = G if d["gradeable"] and (d["brier"] or 1) < 0.25 else (Y if d["gradeable"] else D)
                print(f"    direction           n={d['n']} hit {d['hit_rate']:.0f}%  "
                      f"brier {d['brier']}  {_c(d['verdict'][:70], col)}")
            else:
                print(f"    {_c('Nothing resolved yet — first grades in ~14 days.', D)}")
    except Exception as e:
        print(f"  forecast ledger unavailable: {e}")


def record():
    _hdr("4 · THE RECORD, BY COHORT")
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
    _hdr("5 · WHAT IT HAS LEARNED")
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
    data_quality()
    btc()
    record()
    learning()
    next_steps()
