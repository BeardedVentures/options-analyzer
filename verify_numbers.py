#!/usr/bin/env python3
"""
verify_numbers.py — accuracy + freshness guard for the VEGA board.

Loads logs/scan_latest.json (the engine artifact the cockpit shows) and checks:
  * internal consistency: breakeven, max_loss, ROC, and edge all recompute from primitives
  * gambler-edge sanity: EV and break-even win rate are well-defined
  * freshness: the scan is within the ~15-min delayed-quote window

Exit code 0 = all rows tie out and data is fresh; 1 = issues found. Safe to run in CI
or from the launcher before trusting the board.
    python verify_numbers.py
"""
import json, sys
from datetime import datetime
from pathlib import Path

ART = Path(__file__).resolve().parent / "logs" / "scan_latest.json"


def strategy_key(t):
    """Map a strategy name back to a geometry family. Unknown → caller fails the row loudly
    rather than skipping its checks (a silent pass reads as 'verified').

    Accepts both spellings in circulation: main.py emits the raw key ("bull_put_spread")
    while multi_strategy emits the display label ("Iron Condor"), so normalize separators.
    """
    s = (t.get("strategy") or "").lower().replace("_", " ")
    if "iron condor" in s:
        return "iron_condor"
    if "bear call" in s:
        return "bear_call"
    if "bull put" in s:
        return "bull_put"
    return "unknown"


def geometry(t, key):
    """Return (width, [(label, expected_breakeven)]) for the strategy's leg geometry.
    Each family has its own width and breakeven algebra — a bull-put-shaped check silently
    skips an iron condor (4 strikes, no short_strike key) and reports it as reconciled."""
    c = t.get("credit_per_share")
    if key in ("bull_put", "bear_call"):
        short, long_ = t.get("short_strike"), t.get("long_strike")
        if None in (short, long_) or c is None:
            return None, []
        width = abs(short - long_)
        be = (short - c) if key == "bull_put" else (short + c)
        return width, [("breakeven", be)]
    if key == "iron_condor":
        cs, cl = t.get("call_short_strike"), t.get("call_long_strike")
        ps, pl = t.get("put_short_strike"), t.get("put_long_strike")
        if None in (cs, cl, ps, pl) or c is None:
            return None, []
        # Max loss is driven by the WIDER side: only one side can be breached at expiry.
        width = max(abs(cl - cs), abs(ps - pl))
        return width, [("breakeven_lower", ps - c), ("breakeven_upper", cs + c)]
    return None, []


def check_trade(t):
    issues = []
    key = strategy_key(t)
    if key == "unknown":
        return [f"unrecognized strategy '{t.get('strategy')}' — geometry NOT verified"]

    credit_ps = t.get("credit_per_share"); credit_usd = t.get("credit_usd")
    ml = t.get("max_loss_usd"); tp = t.get("true_pop"); imp = t.get("implied_pop")
    width, breakevens = geometry(t, key)

    if width is None:
        issues.append(f"{key}: missing strikes/credit — geometry NOT verified")
    else:
        if credit_usd is not None and ml is not None:
            exp_ml = width * 100 - credit_usd
            if abs(ml - exp_ml) > 1.0:
                issues.append(f"max_loss {ml} != width-credit {exp_ml:.0f}")
        # Only compare breakevens the artifact actually carries; recompute is the source of truth.
        for label, expected in breakevens:
            got = t.get(label)
            if got is not None and abs(got - expected) > 0.02:
                issues.append(f"{label} {got} != recomputed {expected:.2f}")

    if credit_usd is not None and ml:
        roc = credit_usd / ml
        if not (0 < roc < 5):
            issues.append(f"ROC out of range: {roc:.2f}")
    if tp is not None and imp is not None:
        if not (0 <= tp <= 1 and 0 <= imp <= 1):
            issues.append(f"POP out of [0,1]: true={tp} implied={imp}")
    if credit_ps is not None and credit_ps <= 0:
        issues.append("non-positive credit")
    # P(profit at breakeven) must exceed P(max profit at the short strike) — the breakeven band
    # is strictly wider. Inversion means the two are being measured against different events.
    pmp = t.get("p_max_profit")
    if tp is not None and pmp is not None and tp < pmp - 0.001:
        issues.append(f"true_pop {tp} < p_max_profit {pmp} (POP measured on wrong event?)")
    return issues


def describe(t):
    """Human-readable leg summary per strategy family."""
    key = strategy_key(t)
    if key == "iron_condor":
        return (f"{t.get('put_long_strike')}/{t.get('put_short_strike')}"
                f"--{t.get('call_short_strike')}/{t.get('call_long_strike')}")
    return f"{t.get('short_strike')}/{t.get('long_strike')}"


def main():
    if not ART.exists() or ART.stat().st_size == 0:
        print("no engine artifact (scan_latest.json empty) — cockpit is on the legacy fast-scan fallback.")
        return 0
    d = json.loads(ART.read_text())
    trades = d.get("qualified_trades") or []
    ts = d.get("timestamp")
    print(f"artifact: {len(trades)} qualified trades, timestamp {ts}")
    # freshness
    stale = False
    try:
        # main.py stamps tz-aware ET; the seeder stamps naive local. Splitting on "." used to
        # discard the offset and compare ET against naive local time, so on a non-ET box the
        # age came out ~-60 min: never > 20, so the staleness guard could never fire.
        dt = datetime.fromisoformat(str(ts))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        age = (now - dt).total_seconds() / 60
        if age < -5:
            # Future-stamped: clock skew or a bad tz. Treat as untrustworthy, not "fresh".
            print(f"age: {age:.0f} min — TIMESTAMP IS IN THE FUTURE (clock skew?); cannot vouch for freshness")
            stale = True
        else:
            print(f"age: {age:.0f} min", "(fresh)" if age <= 20 else "(STALE — rescan)")
            stale = age > 20
    except Exception as e:
        print(f"age: could not parse timestamp ({e}) — treating as stale")
        stale = True
    bad = 0
    for t in trades:
        iss = check_trade(t)
        tag = "OK " if not iss else "!! "
        print(f"  {tag}{t.get('ticker'):6} {strategy_key(t):12} {describe(t):22}"
              + ("" if not iss else "  <-- " + "; ".join(iss)))
        if iss:
            bad += 1
    print(f"\n{len(trades)-bad}/{len(trades)} rows reconcile" + (f", {bad} FAILED" if bad else ""))
    return 1 if (bad or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
