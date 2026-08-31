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
    """Age of a timestamp, whether or not it carries a UTC offset.

    scan_latest.json is written with an Eastern offset while every other artefact read here is
    naive local time. Subtracting an aware datetime from a naive datetime.now() raises
    TypeError, which this caught and turned into None — and the engine-scan caller rendered
    None as the literal sentinel 99. So the board reported "last engine scan 99.0h ago" on
    every single run, in amber, while the scan was in fact minutes old, and the number was
    indistinguishable from a genuine four-day-old board. Normalise to local wall time instead.
    """
    try:
        t = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return datetime.now() - t


def _age_hours(ts):
    """Hours since `ts`, or None if it cannot be read. Never a sentinel: a magic number here
    is a number the reader will believe."""
    age = _age(ts)
    return age.total_seconds() / 3600 if age is not None else None


def _freshness(ts, good=4.0, warn=24.0):
    """Render an age the same way everywhere, and say so plainly when it is unknown."""
    h = _age_hours(ts)
    if h is None:
        return _c("age unreadable", R)
    return _c(f"{h:.1f}h ago", G if h < good else (Y if h < warn else R))


def health():
    _hdr("1 · MACHINERY")
    cands = sorted(glob.glob(str(BASE / "output" / "candidates" / "candidates_*.json")))
    if cands:
        print(f"  last candidate scan   {Path(cands[-1]).name}  "
              f"{_freshness(datetime.fromtimestamp(Path(cands[-1]).stat().st_mtime).isoformat())}")
    else:
        print(f"  last candidate scan   {_c('NONE — the cycle has never produced candidates', R)}")

    scan = BASE / "logs" / "scan_latest.json"
    if scan.exists():
        try:
            d = json.loads(scan.read_text(encoding="utf-8"))
            print(f"  last engine scan      {len(d.get('qualified_trades') or [])} qualified  "
                  f"{_freshness(d.get('timestamp'))}")
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
    skipped = s.get("below_floor_tickers") or []
    print(f"  below the {floor:.0%} floor    "
          f"{_c(str(s['below_floor']), R if s['below_floor'] else G)} "
          f"{'ticker skipped' if s['below_floor'] == 1 else 'tickers skipped'}"
          # Name them. A bare count invites the reading that the board simply found nothing
          # today, when the truth is that these names were never looked at.
          + (f"  {_c(', '.join(skipped[:10]) + ('...' if len(skipped) > 10 else ''), D)}"
             if skipped else ""))
    if s["sources"]:
        print(f"  sources               "
              f"{', '.join(f'{k} {v}' for k, v in sorted(s['sources'].items()))}")
    if not getattr(config, "SKEW_SCORING_ENABLED", True):
        print(f"  {_c('skew scoring OFF', Y)} — re-enable once these ratios hold above "
              f"{getattr(config, 'CHAIN_QUALITY_GOOD_RATIO', 0.70):.0%}.")


def iv_readiness():
    """Can the system tell a rich day from a cheap one, per ticker?

    IV rank is the standard answer to that, and it is only as good as the history behind it.
    Until 2026-08-09 two functions wrote that history with different definitions of ATM IV, and
    10% of every stored observation is a bad quote. This section says out loud which tickers can
    currently support a richness judgement and which cannot.
    """
    _hdr("3 · CAN IT TELL RICH FROM CHEAP?  (per-ticker IV readiness)")
    try:
        from analysis import ticker_profile as tp
        from data import fetcher
        import glob as _glob
        from pathlib import Path as _P
    except Exception as e:
        print(f"  profiles unavailable: {e}")
        return

    files = sorted(_glob.glob(str(BASE / config.IV_HISTORY_DIR / "*.json")))
    if not files:
        print(f"  {_c('No IV history at all.', Y)} Nothing can be ranked yet.")
        return

    rows, ready = [], 0
    for f in files:
        tk = _P(f).stem
        try:
            px = fetcher.get_price_data(tk, period="6mo")
            close = px["Close"] if px is not None and not px.empty else None
            l = tp.learned(tk, close)
        except Exception:
            continue
        rows.append((tk, l))
        if l["sufficient"]:
            ready += 1

    total = len(rows)
    min_n = int(getattr(config, "PROFILE_MIN_OBSERVATIONS", 20))
    col = G if ready > total * 0.5 else (Y if ready else R)
    print(f"  tickers ranked on real history   {_c(f'{ready}/{total}', col)} "
          f"(need {min_n} clean observations)")
    dropped = sum(l["iv_observations_dropped"] for _, l in rows)
    if dropped:
        print(f"  bad quotes excluded              {_c(str(dropped), Y)} stored observations "
              f"were implausible vs the ticker's own realised vol")
    thin = sorted((r for r in rows if not r[1]["sufficient"]), key=lambda r: r[1]["iv_observations"])
    if thin:
        names = ", ".join(f"{tk}({l['iv_observations']})" for tk, l in thin[:8])
        print(f"  thinnest histories               {_c(names, D)}")
    if ready == 0:
        print(f"  {_c('No ticker can support an IV-rank judgement yet.', Y)} Richness reads are "
              f"approximations until histories accrue — roughly {min_n} more trading days.")


def btc():
    """The BTC layer: what it reads now, and how its claims are grading.

    Deliberately shown next to the equity record rather than in its own tool. The whole point of
    routing BTC claims into VEGA's prediction ledger instead of a private forecast table is that
    the two become comparable — a separate screen would undo that on the display side.
    """
    if not getattr(config, "BTC_SIGNAL_ENABLED", True):
        return
    _hdr("4 · BITCOIN LAYER  (free data · advisory only)")
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


def crypto_premium():
    """The IBIT premium view: is selling paid for the risk right now?

    Advisory. It gates nothing -- the cohort contract says a criterion added mid-cohort splits
    the sample as surely as a rule change, and the ravens cohort is at 12 of 30. This earns its
    way into selection by being right on the ledger first.
    """
    _hdr("4b - IBIT PREMIUM VIEW  (forecast realised vs implied)")
    try:
        import pandas as pd
        import yfinance as yf
        from analysis import crypto_vol_forecast as cvf
        from data import fetcher as _f

        btc = yf.Ticker("BTC-USD").history(start="2017-01-01", auto_adjust=False)["Close"]
        ibit = yf.Ticker("IBIT").history(start="2024-01-01", auto_adjust=False)["Close"]
        for _s in (btc, ibit):
            _s.index = pd.to_datetime(_s.index).tz_localize(None)
        j = pd.DataFrame({"b": btc, "i": ibit}).dropna()

        iv = None
        try:
            spot = float(_f.get_price_data("IBIT")["Close"].iloc[-1])
            chain = [c for c in _f.get_options_chain("IBIT") if c.get("iv")]
            if chain:
                iv = sorted(chain, key=lambda c: abs(c["strike"] - spot))[0]["iv"]
        except Exception:
            pass

        v = cvf.premium_view(iv, list(btc.values),
                             ibit_closes=list(j.i.values) if len(j) > 200 else None,
                             paired_btc_closes=list(j.b.values) if len(j) > 200 else None)
        f = v.get("forecast") or {}
        print(f"  BTC forward {f.get('horizon_days', 30)}d vol   "
              f"{('%.1f%%' % (100 * f['forecast_rv'])) if f.get('forecast_rv') else '—'}"
              f"   {_c(f.get('method', '—'), D)} · trailing "
              f"{('%.1f%%' % (100 * f['trailing_rv'])) if f.get('trailing_rv') else '—'}")
        if v.get("ibit_rv_forecast"):
            print(f"  mapped to IBIT        {100 * v['ibit_rv_forecast']:.1f}%   "
                  f"{_c((v.get('ibit_map') or {}).get('method', ''), D)}")
        print(f"  IBIT implied (live)   "
              f"{('%.1f%%' % (100 * iv)) if iv else _c('unavailable', D)}")
        if v.get("available"):
            col = {"SELL_PREMIUM": G, "THIN": Y, "STAND_ASIDE": R}.get(v["verdict"], D)
            print(f"  expected VRP          {_c('%+.1f vol pts' % v['expected_vrp_pp'], col)}"
                  f"   P(realised<implied) {v.get('prob_realised_under_implied')}")
            print(f"  verdict               {_c(v['verdict'], col)}")
        print(f"  {_c(v.get('note', ''), D)}")
        print(f"  {_c('advisory — records a graded claim, gates nothing', D)}")
    except Exception as exc:
        print(f"  {_c('unavailable: %s' % str(exc)[:70], D)}")
    print()


def record():
    _hdr("5 · THE RECORD, BY COHORT")
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
    _hdr("6 · WHAT IT HAS LEARNED")
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
    # One alert is appended per position PER CYCLE, so a position that has been flagged all
    # week contributes dozens of identical rows. Showing the raw tail meant the same ARKK
    # position printed five times and the nine other positions needing a decision printed
    # none. Collapse to the latest alert per (position, recommendation).
    alerts = [(r.get("id"), a) for r in rows for a in (r.get("raven_alerts") or [])]
    latest = {}
    for tid, a in alerts:
        key = (tid, a.get("recommendation"))
        prev = latest.get(key)
        if prev is None or str(a.get("at") or "") >= str(prev[0].get("at") or ""):
            latest[key] = (a, latest.get(key, (None, 0))[1] + 1)
        else:
            latest[key] = (prev[0], prev[1] + 1)
    if latest:
        standing = sorted(latest.items(), key=lambda kv: str(kv[1][0].get("at") or ""),
                          reverse=True)
        print(f"\n  {_c('RAVEN ALERTS NEEDING YOUR DECISION:', Y)}"
              f"  {_c(f'{len(standing)} standing, {len(alerts)} raw', D)}")
        for (tid, rec), (a, n) in standing[:5]:
            since = str(a.get("at") or "")[:16].replace("T", " ")
            repeat = _c(f"  x{n} since {since}", D) if n > 1 else ""
            print(f"    {rec} · {tid}{repeat}")
            print(f"      {a.get('plain_english', '')[:110]}")
        if len(standing) > 5:
            print(f"    {_c(f'... and {len(standing) - 5} more', D)}")


def why_empty():
    """Where the board lost every candidate, read off the scan's own diagnostics.

    The engine already records this -- select_bull_put_pair counts each reason it discards a
    leg, and each enumerated pair carries the floor that killed it -- and it has been writing
    it into scan_latest.json all along. Nothing read it back. Diagnosing an eighteen-day entry
    drought on 2026-08-25 meant re-running pair selection by hand against live chains to
    recover numbers that were already sitting on disk.

    An empty board is the system's most common output and its least self-explanatory. It looks
    identical whether the gates are working correctly in a low-premium regime or a data feed
    has quietly stopped returning quotes, and the difference is the whole question.
    """
    _hdr("7 - WHY THE BOARD IS EMPTY")
    scan = BASE / "logs" / "scan_latest.json"
    if not scan.exists():
        print("  " + _c("no scan on disk yet", D))
        return
    try:
        d = json.loads(scan.read_text(encoding="utf-8"))
    except Exception as e:
        print("  " + _c("scan unreadable: %s" % e, R))
        return

    qualified = d.get("qualified_trades") or []
    rejected = d.get("rejected_trades") or []
    if qualified:
        print("  " + _c("%d qualified" % len(qualified), G) + " - the board is not empty.")

    by_category, legs, pairs = {}, {}, {}
    enumerated = 0
    for t in rejected:
        cat = t.get("category") or "UNKNOWN"
        by_category[cat] = by_category.get(cat, 0) + 1
        pd = t.get("pair_selection_diagnostics") or {}
        for reason, n in (pd.get("top_reasons") or []):
            legs[reason] = legs.get(reason, 0) + n
        for e in (pd.get("enumerated") or []):
            enumerated += 1
            if e.get("dropped_for"):
                pairs[e["dropped_for"]] = pairs.get(e["dropped_for"], 0) + 1

    print("  %d tickers rejected, by stage:" % len(rejected))
    for cat, n in sorted(by_category.items(), key=lambda kv: -kv[1])[:6]:
        print("      %4d  %s" % (n, cat))

    if legs:
        print("\n  Legs discarded before any spread could form:")
        for reason, n in sorted(legs.items(), key=lambda kv: -kv[1])[:6]:
            print("      %4d  %s" % (n, reason))

    if enumerated:
        survived = enumerated - sum(pairs.values())
        col = G if survived else R
        print("\n  %d spreads did form; %s"
              % (enumerated, _c("%d survived the credit floors" % survived, col)))
        for reason, n in sorted(pairs.items(), key=lambda kv: -kv[1])[:6]:
            print("      %4d  %s" % (n, reason))
        if not survived:
            # The distinction that matters. Legs dying is usually thin data; every FORMED
            # spread dying on a credit floor is a statement about premium, and no amount of
            # data repair changes it.
            print("\n  " + _c("Every spread that formed died on a credit floor.", Y))
            print("  That is a premium problem, not a data problem - the chains were good")
            print("  enough to price a spread, and the spread was not worth selling here.")
    elif legs:
        print("\n  " + _c("No spread ever formed.", Y))
        print("  The legs are filtered out before pairing, which points at the liquidity")
        print("  and quote floors or the strike band, not at premium.")
    print()


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
    iv_readiness()
    btc()
    crypto_premium()
    record()
    learning()
    why_empty()
    next_steps()
