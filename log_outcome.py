#!/usr/bin/env python3
"""
log_outcome.py — Gate 1 CLI for recording real fills and outcomes against modeled trades.

The scanner auto-records every qualified trade as a "modeled" row (see analysis/outcome_logger.py).
You use this tool to add ground truth as you actually trade, then read the calibration report.

Usage
-----
  python log_outcome.py list                       # open trades awaiting a fill or a close
  python log_outcome.py fill  <id> <credit>        # you got filled at <credit> per share
  python log_outcome.py close <id> <exit> <win|loss|scratch> [reason...]
  python log_outcome.py report                      # calibration: modeled vs actual

Examples
--------
  python log_outcome.py fill  "SPY-580.0/575.0-2026-08-15-2026-07-06" 0.78
  python log_outcome.py close "SPY-580.0/575.0-2026-08-15-2026-07-06" 0.39 win "50% target hit"

Notes
-----
  * <credit> and <exit> are PER SHARE (e.g. 0.78 means $78 per contract).
    * report shows both gross and net P/L per contract (net includes estimated round-trip costs).
  * Everything lives in logs/vega_outcomes.jsonl — plain JSON lines, safe to hand-edit.
"""

import sys
import statistics
from analysis import outcome_logger as ol


def _fmt(x, nd=2, dash="—"):
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) else dash


def cmd_list():
    rows = ol.load_records()
    openish = [r for r in rows if r.get("status") in ("modeled", "filled")]
    if not openish:
        print("No open trades. (All recorded trades are closed, or none logged yet.)")
        return
    print(f"{'STATUS':8} {'ID':46} {'MODEL$':>8} {'NET$':>8} {'FILL$':>7}")
    print("-" * 84)
    for r in openish:
        print(f"{r.get('status',''):8} {r.get('id',''):46} "
              f"{_fmt(r.get('modeled_credit_per_share')):>8} "
              f"{_fmt(r.get('modeled_net_credit_per_share')):>8} "
              f"{_fmt(r.get('actual_fill_credit')):>7}")


def cmd_fill(args):
    if len(args) < 2:
        print("usage: log_outcome.py fill <id> <actual_credit_per_share>"); return 1
    tid, credit = args[0], float(args[1])
    if ol.set_fill(tid, credit):
        print(f"OK — fill recorded for {tid}: {credit:.2f}/share")
    else:
        print(f"NOT FOUND: {tid}  (run 'list' to see valid ids)"); return 1


def cmd_close(args):
    if len(args) < 3:
        print("usage: log_outcome.py close <id> <exit_price> <win|loss|scratch> [reason...]"); return 1
    tid, exit_price, outcome = args[0], float(args[1]), args[2]
    reason = " ".join(args[3:]) if len(args) > 3 else None
    if ol.set_close(tid, exit_price, outcome, reason):
        print(f"OK — closed {tid}: exit {exit_price:.2f}, outcome {outcome}")
    else:
        print(f"NOT FOUND: {tid}  (run 'list' to see valid ids)"); return 1


def cmd_report():
    rows = ol.load_records()
    closed = [r for r in rows if r.get("status") == "closed"]
    filled = [r for r in rows if r.get("actual_fill_credit") is not None]
    print("=" * 60)
    print("VEGA GATE 1 — CALIBRATION REPORT")
    print("=" * 60)
    print(f"Records total : {len(rows)}")
    print(f"Filled        : {len(filled)}")
    print(f"Closed        : {len(closed)}   (target: 30 before trusting the engine)")

    # 1) Model-vs-market credit gap
    gaps = [
        (r["actual_fill_credit"] - r["modeled_credit_per_share"])
        for r in filled
        if r.get("modeled_credit_per_share") is not None
    ]
    if gaps:
        print("\n[1] Modeled vs actual credit (per share)")
        print(f"    avg gap (actual-modeled): {statistics.mean(gaps):+.3f}")
        print(f"    median gap             : {statistics.median(gaps):+.3f}")
        worse = sum(1 for g in gaps if g < 0)
        print(f"    filled below model     : {worse}/{len(gaps)} "
              f"({100*worse/len(gaps):.0f}%)  <- data-quality signal")

        modeled_net = [r.get("modeled_net_credit_per_share") for r in filled if r.get("modeled_net_credit_per_share") is not None]
        if modeled_net:
            net_gaps = [
                (r["actual_fill_credit"] - r.get("modeled_net_credit_per_share", 0))
                for r in filled
                if r.get("actual_fill_credit") is not None and r.get("modeled_net_credit_per_share") is not None
            ]
            if net_gaps:
                print(f"    avg gap vs modeled net : {statistics.mean(net_gaps):+.3f}")

    if not closed:
        print("\n(No closed trades yet — win rate / realized edge appear once you close some.)")
        return

    # 2) Hit rate vs modeled probability of profit
    wins = [r for r in closed if r.get("outcome") == "win"]
    hit_rate = len(wins) / len(closed)
    modeled_pops = [r["modeled_pop"] for r in closed if r.get("modeled_pop") is not None]
    print("\n[2] Probability calibration")
    print(f"    realized win rate : {hit_rate:.1%}  ({len(wins)}/{len(closed)})")
    if modeled_pops:
        print(f"    avg modeled POP   : {statistics.mean(modeled_pops):.1%}")
        print(f"    calibration gap   : {hit_rate - statistics.mean(modeled_pops):+.1%} "
              f"(negative = model too optimistic)")

    # 3) Realized edge / P&L
    gross_pls = [r["realized_gross_pl_per_contract"] for r in closed if r.get("realized_gross_pl_per_contract") is not None]
    net_pls = [r["realized_net_pl_per_contract"] for r in closed if r.get("realized_net_pl_per_contract") is not None]
    if gross_pls or net_pls:
        print("\n[3] Realized P/L per contract")
        if gross_pls:
            print(f"    gross total  : {sum(gross_pls):+.2f}")
            print(f"    gross average: {statistics.mean(gross_pls):+.2f}")
        if net_pls:
            print(f"    net total    : {sum(net_pls):+.2f}")
            print(f"    net average  : {statistics.mean(net_pls):+.2f}")
            print(f"    net win/loss avg: "
                  f"{statistics.mean([p for p in net_pls if p>0] or [0]):+.2f} / "
                  f"{statistics.mean([p for p in net_pls if p<=0] or [0]):+.2f}")
    print("=" * 60)


def main(argv):
    if not argv:
        print(__doc__); return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":   return cmd_list()
    if cmd == "fill":   return cmd_fill(rest)
    if cmd == "close":  return cmd_close(rest)
    if cmd == "report": return cmd_report()
    print(f"unknown command: {cmd}\n"); print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
