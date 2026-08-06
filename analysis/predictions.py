#!/usr/bin/env python3
"""
predictions.py — the ledger that lets VEGA grade its own forecasting, not just its P&L.

VEGA already records what HAPPENED (win, loss, dollars). It has never recorded what it
CLAIMED, so nothing it asserts has ever been marked. The engine says "the short strike is
0.52 sigma away — genuinely in play", "EARLY: premium improves below RSI 50", "event spike at
the Sep 11 expiry", "3-touch support at $240 will hold". Every one of those is falsifiable
within a known window, and every one of them evaporated the moment it was printed.

That gap is why the calibration engine could only compare realised win rate against modelled
POP — the single prediction the system happened to store — and why that comparison turned out
to be measuring the exit rule instead of the model.

This module fixes the shape of the problem rather than any one claim:

    record(...)   a falsifiable claim, with the horizon by which it must resolve
    resolve(...)  score matured claims against what the market actually did
    grade(...)    per claim TYPE: hit rate, Brier score, and the bias direction

Brier score is used rather than raw accuracy because these claims carry confidence. A
predictor that says 60% and is right 60% of the time is well calibrated; one that says 95%
and is right 60% of the time is overconfident and more dangerous, and accuracy alone cannot
tell those apart.

Storage is a JSONL ledger beside the outcomes ledger, keyed by trade id.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

BASE_DIR = Path(__file__).resolve().parent.parent
PREDICTIONS_FILE = BASE_DIR / "logs" / "vega_predictions.jsonl"

# Claim types. Each is a thing the engine asserts today with no way of being wrong.
STRIKE_HOLDS = "strike_holds"        # price finishes on the safe side of the short strike
STRIKE_UNTOUCHED = "strike_untouched"  # price never trades through the short strike
LEVEL_HOLDS = "level_holds"          # a named support/resistance is not breached on a close
TIMING_IMPROVES = "timing_improves"  # EARLY: waiting would have paid better premium
EVENT_REALISED = "event_realised"    # term-structure event spike showed up as a real move
DIRECTION = "direction"              # the regime call was right about which way price went


def _cfg(name, default):
    return getattr(_config, name, default) if _config else default


def _read() -> List[Dict]:
    if not PREDICTIONS_FILE.exists():
        return []
    out = []
    for line in PREDICTIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _write(rows: Sequence[Dict]) -> None:
    PREDICTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREDICTIONS_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, PREDICTIONS_FILE)


def load() -> List[Dict]:
    return _read()


def record(trade_id: str, ticker: str, claim_type: str, claim: str,
           probability: Optional[float], resolves_on: str,
           context: Optional[Dict] = None) -> Optional[str]:
    """Log one falsifiable claim.

    `probability` is what the engine believed at the time (0-1) — required for Brier scoring
    and the reason accuracy alone is not enough. `resolves_on` is the date the claim can first
    be marked; a claim with no resolution date is an opinion, not a prediction, and is
    rejected.
    """
    if not _cfg("PREDICTION_LEDGER_ENABLED", True):
        return None
    if not (trade_id and claim_type and resolves_on):
        return None
    pid = f"{trade_id}::{claim_type}"
    rows = _read()
    if any(r.get("id") == pid for r in rows):
        return pid          # one claim of each type per trade; re-recording is a no-op
    rows.append({
        "id": pid, "trade_id": trade_id, "ticker": ticker,
        "claim_type": claim_type, "claim": claim,
        "probability": round(float(probability), 4) if probability is not None else None,
        "made_at": datetime.now().isoformat(),
        "resolves_on": str(resolves_on)[:10],
        "context": context or {},
        "status": "open", "correct": None, "resolved_at": None, "resolution_note": None,
    })
    _write(rows)
    return pid


def _mark(row: Dict, correct: Optional[bool], note: str) -> None:
    row["status"] = "resolved" if correct is not None else "unresolvable"
    row["correct"] = correct
    row["resolved_at"] = datetime.now().isoformat()
    row["resolution_note"] = note


def resolve(price_lookup, today: Optional[date] = None) -> Dict:
    """Score every claim whose resolution date has passed.

    `price_lookup(ticker, start, end)` must return a list of (date, high, low, close) covering
    the claim window; the caller owns data fetching so this module stays pure and testable.
    """
    today = today or date.today()
    rows = _read()
    stats = {"checked": 0, "resolved": 0, "unresolvable": 0}
    changed = False

    for r in rows:
        if r.get("status") != "open":
            continue
        try:
            due = date.fromisoformat(r["resolves_on"])
        except Exception:
            _mark(r, None, "unparseable resolution date")
            changed = True
            continue
        if due > today:
            continue
        stats["checked"] += 1
        try:
            made = datetime.fromisoformat(r["made_at"]).date()
            bars = price_lookup(r["ticker"], made, due)
        except Exception as e:
            _mark(r, None, f"price history unavailable: {e}")
            stats["unresolvable"] += 1
            changed = True
            continue
        if not bars:
            _mark(r, None, "no price history in the claim window")
            stats["unresolvable"] += 1
            changed = True
            continue

        correct, note = _score(r, bars)
        _mark(r, correct, note)
        stats["resolved" if correct is not None else "unresolvable"] += 1
        changed = True

    if changed:
        _write(rows)
    return stats


def _score(r: Dict, bars: Sequence) -> tuple:
    """Mark one claim. bars = [(date, high, low, close), ...]."""
    ctx = r.get("context") or {}
    ct = r.get("claim_type")
    closes = [b[3] for b in bars]
    lows = [b[2] for b in bars]
    highs = [b[1] for b in bars]
    final = closes[-1]

    if ct == STRIKE_HOLDS:
        k = ctx.get("short_strike")
        if k is None:
            return None, "no strike in context"
        is_call = "call" in (ctx.get("strategy") or "").lower()
        ok = final < float(k) if is_call else final > float(k)
        return ok, (f"settled {final:.2f} vs strike {float(k):.2f} "
                    f"({'call' if is_call else 'put'} side)")

    if ct == STRIKE_UNTOUCHED:
        k = ctx.get("short_strike")
        if k is None:
            return None, "no strike in context"
        is_call = "call" in (ctx.get("strategy") or "").lower()
        touched = (max(highs) >= float(k)) if is_call else (min(lows) <= float(k))
        return (not touched), (f"extreme {'high ' + format(max(highs), '.2f') if is_call else 'low ' + format(min(lows), '.2f')} "
                               f"vs strike {float(k):.2f}")

    if ct == LEVEL_HOLDS:
        lvl, side = ctx.get("level"), ctx.get("side", "support")
        if lvl is None:
            return None, "no level in context"
        broke = any(c < float(lvl) for c in closes) if side == "support" \
            else any(c > float(lvl) for c in closes)
        return (not broke), (f"{side} {float(lvl):.2f}: "
                             f"{'breached on a close' if broke else 'held every close'}")

    if ct == EVENT_REALISED:
        # The surface said a dated catalyst sat in this window. Did realised movement in the
        # days around it actually exceed the ordinary daily range?
        thr = float(ctx.get("move_threshold_pct") or 4.0)
        moves = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        if not moves:
            return None, "not enough bars to measure a move"
        biggest = max(moves)
        return (biggest >= thr), f"largest single-day move {biggest:.1f}% vs {thr:.1f}% threshold"

    if ct == TIMING_IMPROVES:
        # EARLY claimed premium would be richer later. Proxy: did the underlying move toward
        # the short side, which is what would have raised the credit on the same strike?
        k = ctx.get("short_strike")
        entry_px = ctx.get("price_at_claim")
        if k is None or entry_px is None:
            return None, "missing strike or entry price"
        is_call = "call" in (ctx.get("strategy") or "").lower()
        best = max(closes) if is_call else min(closes)
        moved = (best > float(entry_px)) if is_call else (best < float(entry_px))
        pct = abs(best / float(entry_px) - 1) * 100
        return moved and pct >= 1.0, (f"price reached {best:.2f} from {float(entry_px):.2f} "
                                      f"({pct:.1f}% toward the short side)")

    if ct == DIRECTION:
        entry_px = ctx.get("price_at_claim")
        expect = (ctx.get("expected") or "").lower()
        if entry_px is None or expect not in ("up", "down", "flat"):
            return None, "missing direction context"
        chg = (final / float(entry_px) - 1) * 100
        got = "up" if chg > 1 else ("down" if chg < -1 else "flat")
        return got == expect, f"expected {expect}, price went {got} ({chg:+.1f}%)"

    return None, f"no scorer for claim type {ct}"


def grade(rows: Optional[Sequence[Dict]] = None, cohort: Optional[str] = None) -> Dict:
    """Per claim type: how often it was right, and whether its confidence was earned.

    Brier score is mean((probability - outcome)^2): 0 is perfect, 0.25 is what you get by
    always saying 50%, and above that the claim is worse than a coin flip dressed in a number.
    `bias` is mean(probability) - hit_rate: positive means systematically overconfident.
    """
    rows = list(rows if rows is not None else _read())
    if cohort:
        rows = [r for r in rows if (r.get("context") or {}).get("close_logic") == cohort]
    resolved = [r for r in rows if r.get("status") == "resolved" and r.get("correct") is not None]

    by_type: Dict[str, Dict] = {}
    for r in resolved:
        t = r["claim_type"]
        b = by_type.setdefault(t, {"n": 0, "hits": 0, "probs": [], "briers": []})
        b["n"] += 1
        b["hits"] += 1 if r["correct"] else 0
        p = r.get("probability")
        if p is not None:
            b["probs"].append(p)
            b["briers"].append((p - (1.0 if r["correct"] else 0.0)) ** 2)

    out = {}
    min_n = int(_cfg("PREDICTION_MIN_FOR_GRADE", 10))
    for t, b in by_type.items():
        hit = b["hits"] / b["n"]
        brier = (sum(b["briers"]) / len(b["briers"])) if b["briers"] else None
        avg_p = (sum(b["probs"]) / len(b["probs"])) if b["probs"] else None
        out[t] = {
            "n": b["n"],
            "hit_rate": round(hit * 100, 1),
            "avg_confidence": round(avg_p * 100, 1) if avg_p is not None else None,
            "brier": round(brier, 4) if brier is not None else None,
            "bias_pp": round((avg_p - hit) * 100, 1) if avg_p is not None else None,
            "gradeable": b["n"] >= min_n,
            "verdict": _verdict(b["n"], min_n, hit, avg_p, brier),
        }
    return {
        "total_claims": len(rows),
        "open": sum(1 for r in rows if r.get("status") == "open"),
        "resolved": len(resolved),
        "unresolvable": sum(1 for r in rows if r.get("status") == "unresolvable"),
        "by_type": out,
    }


def _verdict(n: int, min_n: int, hit: float, avg_p: Optional[float],
             brier: Optional[float]) -> str:
    if n < min_n:
        return f"only {n} resolved — not gradeable yet ({min_n} needed)"
    if avg_p is None:
        return f"{hit*100:.0f}% correct over {n}, but no confidence was recorded to calibrate"
    bias = (avg_p - hit) * 100
    if brier is not None and brier > 0.25:
        return (f"{hit*100:.0f}% correct over {n} with a Brier of {brier:.2f} — worse than "
                f"always guessing 50%. This claim type is not adding information.")
    if bias > 10:
        return (f"{hit*100:.0f}% correct but claiming {avg_p*100:.0f}% — overconfident by "
                f"{bias:.0f}pp. The direction is useful; the certainty is not earned.")
    if bias < -10:
        return (f"{hit*100:.0f}% correct while only claiming {avg_p*100:.0f}% — "
                f"underconfident by {abs(bias):.0f}pp. This signal deserves more weight.")
    return f"{hit*100:.0f}% correct over {n}, well calibrated (Brier {brier:.2f})."


def record_trade_predictions(trade: Dict, trade_id: str) -> List[str]:
    """Extract every falsifiable claim a scored trade already contains.

    This is the join between the engine and the ledger: the engine has been making all of
    these assertions and discarding them. Nothing new is being predicted here — it is being
    written down.
    """
    made: List[str] = []
    exp = trade.get("expiration") or trade.get("exp")
    if not (exp and trade_id):
        return made
    tk = trade.get("ticker")
    ks = trade.get("short_strike") or trade.get("short")
    strategy = trade.get("strategy") or trade.get("strat_type") or "bull_put_spread"
    spot = trade.get("current_price") or trade.get("price")
    cohort = trade.get("close_logic")

    def ctx(**kw):
        d = {"short_strike": ks, "strategy": strategy, "close_logic": cohort}
        d.update(kw)
        return d

    # 1. The core claim of every credit spread: POP.
    pop = trade.get("true_pop")
    if pop is not None and ks:
        pid = record(trade_id, tk, STRIKE_HOLDS,
                     f"{tk} finishes on the safe side of {ks:g} at expiry",
                     pop, exp, ctx())
        if pid:
            made.append(pid)

    # 2. Stronger and rarer: never touched at all. p_max_profit is measured at the strike.
    pmax = trade.get("p_max_profit")
    if pmax is not None and ks:
        pid = record(trade_id, tk, STRIKE_UNTOUCHED,
                     f"{tk} never trades through {ks:g} before expiry", pmax, exp, ctx())
        if pid:
            made.append(pid)

    # 3. The structural claim — the level the strike was placed under.
    lvls = trade.get("support_levels") or []
    if lvls and ks:
        best = max(lvls, key=lambda l: l.get("strength") or 0)
        if best.get("price") and float(best["price"]) > float(ks):
            conf = min(0.95, 0.5 + (best.get("strength") or 0) / 200.0)
            pid = record(trade_id, tk, LEVEL_HOLDS,
                         f"support {best['price']:g} ({best.get('touches')} touches) holds "
                         f"every close through expiry",
                         conf, exp, ctx(level=best["price"], side="support"))
            if pid:
                made.append(pid)

    # 4. The timing claim. EARLY explicitly asserts waiting would pay better.
    et = trade.get("entry_timing") or {}
    if et and not et.get("timing_gate_pass", True) and spot:
        horizon = (date.today() + timedelta(days=int(_cfg("PREDICTION_TIMING_HORIZON_DAYS", 14))))
        pid = record(trade_id, tk, TIMING_IMPROVES,
                     f"{et.get('readiness')}: premium would be richer if entry were deferred",
                     0.6, horizon.isoformat(), ctx(price_at_claim=spot))
        if pid:
            made.append(pid)

    # 5. The volatility-surface claim — a dated catalyst inside this window.
    if trade.get("event_expiry_flag") and trade.get("event_expiry_date"):
        pid = record(trade_id, tk, EVENT_REALISED,
                     f"a dated catalyst sits in the window ending {trade['event_expiry_date']}",
                     0.65, trade["event_expiry_date"], ctx(move_threshold_pct=4.0))
        if pid:
            made.append(pid)

    return made
