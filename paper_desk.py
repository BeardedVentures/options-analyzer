#!/usr/bin/env python3
"""
paper_desk.py — VEGA Paper Desk: log paper trades, track results, see if the system works.

This is the free, credit-free validation cockpit built on top of what's already here. The loop:

    1. python vega_candidates.py            # see REAL spreads on offer today (delayed, free)
    2. python paper_desk.py open ...         # log the one you'd take, as a PAPER position
    3. ...days later...
    4. python paper_desk.py close <id> <exit_debit> win|loss|scratch "reason"
    5. python paper_desk.py report           # net-of-fee stats, win rate, expectancy, calibration
    6. python paper_desk.py dashboard        # one-page HTML view of open + closed + stats

Everything is paper until you say otherwise: no real orders, no paid data, no LLM credits
(DISABLE_AI is on). Net P/L subtracts Robinhood's real round-trip commissions so the results
are honest. Prove the edge here before spending a dollar on data or risking the account.

Trades live in logs/vega_outcomes.jsonl (shared with the Gate-1 ledger; hand-editable JSON lines).

COMMANDS
--------
  open   --from-latest TICKER [--rank N] [--contracts N]      # from newest vega_candidates run
  open   --ticker T --short S --long L --exp YYYY-MM-DD --credit C [--dte D] [--delta X] [--contracts N]
  list                                                        # open paper positions
  close  <id> <exit_debit_per_share> <win|loss|scratch> [reason...]
  report                                                      # net-of-fee stats + calibration
  dashboard [--no-open]                                       # render + open the HTML cockpit
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from analysis import outcome_logger as ol

BASE = Path(__file__).resolve().parent
CAND_DIR = BASE / "output" / "candidates"
DASH_DIR = BASE / "output" / "paper_desk"


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _fmt(x, nd=2, dash="—"):
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) else dash


def _latest_candidates():
    files = sorted(glob.glob(str(CAND_DIR / "candidates_*.json")))
    if not files:
        return None, None
    path = files[-1]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data, path
    except Exception:
        return None, path


def compute_stats(rows):
    closed = [r for r in rows if r.get("status") == "closed"]
    open_ = [r for r in rows if r.get("status") == "open"]
    s = {
        "n_open": len(open_), "n_closed": len(closed),
        "wins": 0, "losses": 0, "scratch": 0, "win_rate": None,
        "net_total": 0.0, "gross_total": 0.0, "credit_collected": 0.0,
        "expectancy_per_contract": None, "avg_win": None, "avg_loss": None,
        "profit_factor": None, "avg_implied_pop": None, "calibration_gap": None,
        "open_risk": 0.0,
    }
    for r in open_:
        c = int(r.get("contracts") or 1)
        ml = r.get("max_loss_per_contract")
        if isinstance(ml, (int, float)):
            s["open_risk"] += ml * c
    if not closed:
        return s, closed, open_

    net_per_c, wins_pl, losses_pl, pops = [], [], [], []
    for r in closed:
        c = int(r.get("contracts") or 1)
        net = r.get("realized_net_pl_per_contract")
        gross = r.get("realized_gross_pl_per_contract")
        if isinstance(net, (int, float)):
            s["net_total"] += net * c
            net_per_c.append(net)
        if isinstance(gross, (int, float)):
            s["gross_total"] += gross * c
        oc = (r.get("outcome") or "").lower()
        if oc == "win":
            s["wins"] += 1
            if isinstance(net, (int, float)): wins_pl.append(net)
        elif oc == "loss":
            s["losses"] += 1
            if isinstance(net, (int, float)): losses_pl.append(net)
        else:
            s["scratch"] += 1
        if isinstance(r.get("implied_pop"), (int, float)):
            pops.append(r["implied_pop"])
        cr = r.get("modeled_credit_per_share")
        if isinstance(cr, (int, float)):
            s["credit_collected"] += cr * 100 * c

    s["win_rate"] = (s["wins"] / s["n_closed"]) if s["n_closed"] else None
    if net_per_c:
        s["expectancy_per_contract"] = statistics.mean(net_per_c)
    if wins_pl:
        s["avg_win"] = statistics.mean(wins_pl)
    if losses_pl:
        s["avg_loss"] = statistics.mean(losses_pl)
    win_sum = sum(p for p in net_per_c if p > 0)
    loss_sum = abs(sum(p for p in net_per_c if p < 0))
    s["profit_factor"] = (win_sum / loss_sum) if loss_sum > 0 else (float("inf") if win_sum > 0 else None)
    if pops:
        s["avg_implied_pop"] = statistics.mean(pops)
        if s["win_rate"] is not None:
            s["calibration_gap"] = s["win_rate"] - s["avg_implied_pop"]
    return s, closed, open_


# ─────────────────────────────────────────────────────────────────────────────
# commands
# ─────────────────────────────────────────────────────────────────────────────
def cmd_open(a):
    if a.from_latest:
        data, path = _latest_candidates()
        if not data:
            print("No candidates file found. Run: python vega_candidates.py"); return 1
        tk = a.from_latest.upper()
        row = next((r for r in data.get("rows", []) if r.get("ticker") == tk), None)
        if not row or not row.get("candidates"):
            print(f"No candidates for {tk} in {os.path.basename(path)}."); return 1
        cands = row["candidates"]
        if a.rank < 1 or a.rank > len(cands):
            print(f"{tk} has {len(cands)} candidate(s); --rank must be 1..{len(cands)}."); return 1
        c = cands[a.rank - 1]
        try:
            tid = ol.open_paper_trade(
                ticker=tk, short_strike=c["short_strike"], long_strike=c["long_strike"],
                expiration=c["expiration"], entry_credit_per_share=c["credit_per_share"],
                dte=c.get("dte"), delta=c.get("short_delta"), iv_rank=(row.get("ctx") or {}).get("iv_rank"),
                implied_pop=c.get("pop_implied"), contracts=a.contracts, source="candidate",
                note=f"from {os.path.basename(path)} rank {a.rank}",
            )
        except ValueError as e:
            print(f"Not opened: {e}"); return 1
        print(f"OK — opened paper {tid}\n   {tk} {c['short_strike']:g}/{c['long_strike']:g} {c['expiration']} "
              f"({c.get('dte')}d)  credit ${c['credit_per_share']:.2f}/sh x{a.contracts}  "
              f"maxloss ${c.get('max_loss_usd', 0):.0f}/ct")
        return 0
    # manual
    req = [a.ticker, a.short, a.long, a.exp, a.credit]
    if any(v is None for v in req):
        print("manual open needs --ticker --short --long --exp --credit"); return 1
    try:
        tid = ol.open_paper_trade(
            ticker=a.ticker.upper(), short_strike=a.short, long_strike=a.long, expiration=a.exp,
            entry_credit_per_share=a.credit, dte=a.dte, delta=a.delta, contracts=a.contracts, source="manual",
        )
    except ValueError as e:
        print(f"Not opened: {e}"); return 1
    print(f"OK — opened paper {tid}")
    return 0


def cmd_list():
    rows = ol.load_records()
    open_ = [r for r in rows if r.get("status") == "open"]
    if not open_:
        print("No open paper positions. Open one:  python paper_desk.py open --from-latest SPY")
        return 0
    print(f"{'ID':44} {'CR/sh':>6} {'x':>3} {'MAXLOSS':>8} {'DTE':>4}")
    print("-" * 70)
    for r in open_:
        print(f"{r.get('id',''):44} {_fmt(r.get('actual_fill_credit')):>6} "
              f"{int(r.get('contracts') or 1):>3} {_fmt(r.get('max_loss_per_contract'),0):>8} {str(r.get('dte') or '—'):>4}")
    return 0


def cmd_close(a):
    reason = " ".join(a.reason) if a.reason else None
    if ol.set_close(a.id, a.exit_debit, a.outcome, reason):
        print(f"OK — closed {a.id}: exit {a.exit_debit:.2f}, {a.outcome}")
        return 0
    print(f"NOT FOUND: {a.id}  (run 'list')"); return 1


def _print_report(rows):
    s, closed, open_ = compute_stats(rows)
    print("=" * 62)
    print("VEGA PAPER DESK — RESULTS")
    print("=" * 62)
    print(f"Open positions : {s['n_open']}   (capital at risk ${s['open_risk']:.0f})")
    print(f"Closed trades  : {s['n_closed']}   (target: 30 before trusting the edge)")
    if not closed:
        print("\nNo closed trades yet — stats appear once you close some.")
        print("=" * 62); return
    print(f"\nWin rate       : {s['win_rate']:.1%}  ({s['wins']}W / {s['losses']}L / {s['scratch']}S)")
    if s["avg_implied_pop"] is not None:
        print(f"Avg implied POP: {s['avg_implied_pop']:.1%}")
        print(f"Calibration    : {s['calibration_gap']:+.1%}  (negative = you're losing more than the model implied)")
    print(f"\nNet P/L total  : ${s['net_total']:+.2f}   (after Robinhood round-trip fees)")
    print(f"Gross P/L      : ${s['gross_total']:+.2f}")
    if s["expectancy_per_contract"] is not None:
        print(f"Expectancy     : ${s['expectancy_per_contract']:+.2f} net per contract per trade")
    if s["avg_win"] is not None or s["avg_loss"] is not None:
        print(f"Avg win / loss : ${_fmt(s['avg_win'])} / ${_fmt(s['avg_loss'])} per contract")
    if s["profit_factor"] is not None:
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        print(f"Profit factor  : {pf}  (>1 = net winning)")
    _print_fill_cohorts(closed)
    print("\ntastytrade benchmark: a ~20Δ short defined-risk spread managed at 50% historically")
    print("wins in the ~75-85% range. Beat your avg implied POP net-of-fees and you have an edge.")


def _print_fill_cohorts(closed):
    """Split realized results by fill basis.

    Pooling them is misleading: 'mid' trades were booked at a price no one can fill (the mid
    overstated collectable credit by ~75%), so their P&L is optimistic and their win rate is
    inflated. They stay in the ledger as a record of what the selection logic chose, but only the
    'natural' cohort estimates what live trading would produce.
    """
    groups = {}
    for r in closed:
        groups.setdefault(r.get("fill_model") or "mid", []).append(r)
    if len(groups) < 2 and "mid" not in groups:
        return

    print("\n" + "-" * 62)
    print("BY FILL MODEL  (only 'natural' predicts live results)")
    print("-" * 62)
    labels = {"mid": "mid      (legacy — unachievable entry price)",
              "natural": "natural  (bid/ask — live-comparable)"}
    for key in ("natural", "mid"):
        rs = groups.get(key)
        if not rs:
            continue
        pl = [r.get("realized_net_pl_per_contract") for r in rs
              if r.get("realized_net_pl_per_contract") is not None]
        if not pl:
            continue
        wins = [p for p in pl if p > 0]
        print(f"{labels.get(key, key):48}")
        print(f"   n={len(pl):<3} win rate {len(wins)/len(pl):6.1%}   net ${sum(pl):+9.2f}   "
              f"avg ${sum(pl)/len(pl):+7.2f}/trade")
    if "natural" not in groups:
        print("   natural  — no closed trades yet; live-comparable track record starts at n=0.")
    print("=" * 62)


def cmd_report():
    _print_report(ol.load_records())
    return 0


def _dash_html(rows):
    s, closed, open_ = compute_stats(rows)
    data, cpath = _latest_candidates()
    gold = getattr(config, "ROBINHOOD_GOLD", False)
    rt = ol._round_trip_cost_per_contract()

    def stat_card(label, value, sub=""):
        return f'<div class="card"><div class="lab">{label}</div><div class="val">{value}</div><div class="sub">{sub}</div></div>'

    # summary cards
    wr = f'{s["win_rate"]*100:.0f}%' if s["win_rate"] is not None else "—"
    net = f'${s["net_total"]:+.0f}' if s["n_closed"] else "—"
    exp = f'${s["expectancy_per_contract"]:+.2f}' if s["expectancy_per_contract"] is not None else "—"
    pf = ("∞" if s["profit_factor"] == float("inf") else f'{s["profit_factor"]:.2f}') if s["profit_factor"] is not None else "—"
    calib = f'{s["calibration_gap"]*100:+.0f}pp' if s["calibration_gap"] is not None else "—"
    cards = "".join([
        stat_card("Closed", f'{s["n_closed"]}', f'of 30 target'),
        stat_card("Win rate", wr, f'{s["wins"]}W/{s["losses"]}L/{s["scratch"]}S'),
        stat_card("Net P/L", net, "after RH fees"),
        stat_card("Expectancy", exp, "net/contract/trade"),
        stat_card("Profit factor", pf, "&gt;1 = winning"),
        stat_card("Calibration", calib, "real − implied POP"),
        stat_card("Open risk", f'${s["open_risk"]:.0f}', f'{s["n_open"]} open'),
    ])

    # open positions
    if open_:
        orows = "".join(
            f'<tr><td>{r.get("ticker","")}</td><td>{r.get("short_strike")}/{r.get("long_strike")}</td>'
            f'<td>{r.get("expiration","")}<div class=dim>{r.get("dte") or "—"}d</div></td>'
            f'<td>${_fmt(r.get("actual_fill_credit"))}</td><td>{int(r.get("contracts") or 1)}</td>'
            f'<td>${_fmt(r.get("max_loss_per_contract"),0)}</td>'
            f'<td class=mono title="{r.get("id","")}">{r.get("id","")[:38]}…</td></tr>'
            for r in open_
        )
        open_tbl = (f'<table><thead><tr><th>Ticker</th><th>Short/Long</th><th>Exp</th><th>Credit/sh</th>'
                    f'<th>Ct</th><th>Max loss</th><th>ID (for close)</th></tr></thead><tbody>{orows}</tbody></table>')
    else:
        open_tbl = '<div class="empty">No open paper positions. Run <code>vega_candidates.py</code> then <code>paper_desk.py open</code>.</div>'

    # closed trades
    if closed:
        crows = ""
        for r in sorted(closed, key=lambda x: x.get("closed_at") or "", reverse=True):
            net = r.get("realized_net_pl_per_contract")
            cls = "win" if (r.get("outcome") == "win") else ("loss" if r.get("outcome") == "loss" else "")
            crows += (f'<tr class="{cls}"><td>{r.get("ticker","")}</td><td>{r.get("short_strike")}/{r.get("long_strike")}</td>'
                      f'<td>{r.get("expiration","")}</td><td>${_fmt(r.get("actual_fill_credit"))}</td>'
                      f'<td>${_fmt(r.get("exit_price"))}</td><td>{(r.get("outcome") or "").upper()}</td>'
                      f'<td>${_fmt(net,0)}</td><td class=dim>{r.get("exit_reason") or ""}</td></tr>')
        closed_tbl = (f'<table><thead><tr><th>Ticker</th><th>Short/Long</th><th>Exp</th><th>Credit</th>'
                      f'<th>Exit</th><th>Result</th><th>Net/ct</th><th>Reason</th></tr></thead><tbody>{crows}</tbody></table>')
    else:
        closed_tbl = '<div class="empty">No closed trades yet.</div>'

    # latest candidates snapshot
    cand_html = ""
    if data:
        crows = ""
        for row in data.get("rows", []):
            best = (row.get("candidates") or [None])[0]
            if not best:
                continue
            g = best.get("gates", {})
            passed = best.get("gates_passed", 0); total = best.get("gates_total", 0)
            crows += (f'<tr><td>{row["ticker"]}</td><td>${row["price"]:.2f}</td>'
                      f'<td>{best["short_strike"]:g}/{best["long_strike"]:g}</td><td>{best["expiration"]} ({best.get("dte")}d)</td>'
                      f'<td>${best["credit_usd"]:.0f}</td><td>{best["credit_to_width"]*100:.0f}%</td>'
                      f'<td>{best["short_delta"]:.2f}</td><td>{passed}/{total}</td></tr>')
        if crows:
            cand_html = (f'<h2>Latest candidates <span class=dim>({os.path.basename(cpath)})</span></h2>'
                         f'<table><thead><tr><th>Ticker</th><th>Price</th><th>Short/Long</th><th>Exp</th>'
                         f'<th>Credit</th><th>Cr/W</th><th>Δ</th><th>Gates</th></tr></thead><tbody>{crows}</tbody></table>')

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>VEGA Paper Desk</title>
<style>
 body{{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1a1a1a;background:#fafafa}}
 h1{{font-size:22px;margin:0 0 2px}} h2{{font-size:15px;margin:22px 0 8px}}
 .sub{{color:#666;margin-bottom:16px;font-size:12px}}
 .cards{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px}}
 .card{{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:10px 14px;min-width:104px}}
 .card .lab{{font-size:11px;color:#777;text-transform:uppercase;letter-spacing:.03em}}
 .card .val{{font-size:22px;font-weight:700;margin:2px 0}}
 .card .sub{{font-size:10px;color:#999}}
 table{{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden}}
 th{{text-align:left;color:#555;font-weight:600;border-bottom:2px solid #eee;padding:7px 9px;font-size:12px}}
 td{{padding:7px 9px;border-bottom:1px solid #f2f2f2}}
 tr.win{{background:#f0fbf3}} tr.loss{{background:#fdf1f1}}
 .dim{{color:#999;font-size:11px}} .mono{{font-family:ui-monospace,monospace;font-size:11px}}
 .empty{{color:#999;font-style:italic;padding:10px;background:#fff;border:1px dashed #ddd;border-radius:8px}}
 code{{background:#eee;padding:1px 5px;border-radius:4px;font-size:12px}}
 .foot{{color:#888;font-size:11px;margin-top:20px;line-height:1.6}}
</style></head><body>
<h1>VEGA Paper Desk</h1>
<div class="sub">{stamp} · PAPER mode · AI disabled (no credits) · Robinhood{' Gold' if gold else ''} fees ≈ ${rt:.2f} round-trip/contract</div>
<div class="cards">{cards}</div>
<h2>Open positions</h2>{open_tbl}
<h2>Closed trades</h2>{closed_tbl}
{cand_html}
<div class="foot">
 Net P/L is after Robinhood round-trip commissions ({config.LEGS_PER_SPREAD} legs × 2 × ${config.COMMISSION_PER_CONTRACT_PER_LEG:.2f}).
 Prove a positive net expectancy and a win rate at/above your implied POP across ~30 closes before spending on
 paid data or risking real capital. Educational tool — not financial advice; every order is your own.
</div>
</body></html>"""


def cmd_dashboard(a):
    rows = ol.load_records()
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    out = DASH_DIR / f"paper_desk_{datetime.now().strftime('%Y-%m-%d_%H%M')}.html"
    latest = DASH_DIR / "paper_desk_latest.html"
    html = _dash_html(rows)
    out.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")
    print(f"Dashboard: {out}")
    if not a.no_open:
        try:
            webbrowser.open(latest.as_uri())
        except Exception:
            pass
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="VEGA Paper Desk — log paper trades and track results")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="open a paper position")
    o.add_argument("--from-latest", type=str, help="ticker; pull from newest vega_candidates run")
    o.add_argument("--rank", type=int, default=1, help="which candidate for that ticker (1=best)")
    o.add_argument("--ticker", type=str); o.add_argument("--short", type=float); o.add_argument("--long", type=float)
    o.add_argument("--exp", type=str); o.add_argument("--credit", type=float)
    o.add_argument("--dte", type=int); o.add_argument("--delta", type=float)
    o.add_argument("--contracts", type=int, default=1)

    sub.add_parser("list", help="show open paper positions")

    c = sub.add_parser("close", help="close a paper position")
    c.add_argument("id"); c.add_argument("exit_debit", type=float)
    c.add_argument("outcome", choices=["win", "loss", "scratch"]); c.add_argument("reason", nargs="*")

    sub.add_parser("report", help="print net-of-fee results")

    d = sub.add_parser("dashboard", help="render + open the HTML cockpit")
    d.add_argument("--no-open", action="store_true")
    return p


def main(argv):
    a = build_parser().parse_args(argv)
    if a.cmd == "open":      return cmd_open(a)
    if a.cmd == "list":      return cmd_list()
    if a.cmd == "close":     return cmd_close(a)
    if a.cmd == "report":    return cmd_report()
    if a.cmd == "dashboard": return cmd_dashboard(a)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
