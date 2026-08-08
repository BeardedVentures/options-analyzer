#!/usr/bin/env python3
"""
auto_paper_cycle.py

Automates one paper-trading cycle:
1) Analyze: run vega_candidates.py to refresh candidate snapshot (JSON only).
2) Select: auto-open top-qualified paper trades up to configurable limits.
3) Grade: reprice open positions and auto-close by target/stop/DTE rules.
4) Report: refresh paper report + dashboard outputs.

Run modes:
  (default)     scan -> auto-open (<=5/run, max 15 open) -> reprice all -> auto-close
  --mark-only   reprice all open positions -> auto-close ONLY (no scan, no new opens).
                Use for a dedicated end-of-day resolution run.

IMPORTANT — scheduling:
  The vega_app.py cockpit has a built-in market-hours-aware scheduler that fires
  this script automatically. That is the preferred driver. If you also have a
  Windows Task Scheduler entry pointing at this script, it will exit harmlessly
  during market-closed hours (weekends, overnights, holidays) thanks to the
  is_market_open() guard below — but you should disable or remove that Task
  Scheduler entry to avoid any overlap with the cockpit's own scheduler.
  Use the cockpit's INTRADAY_SCHEDULER_ENABLED / PAPER_CYCLE_MIN config keys
  to control cadence instead.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from analysis import outcome_logger as ol


BASE = Path(__file__).resolve().parent
LOGS_DIR = BASE / "logs"
LOCK_FILE = LOGS_DIR / "auto_paper_cycle.lock"

# ─────────────────────────────────────────────────────────────────────────────
# Market-hours guard
# ─────────────────────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    """Return True only during US equity options trading hours (9:30–16:00 ET, Mon–Fri).

    If VEGA_COCKPIT_SPAWNED=1 is set the cockpit already performed this check, so
    we trust it and return True immediately (avoids a double-check redundancy).
    """
    if os.getenv("VEGA_COCKPIT_SPAWNED", "").strip() in ("1", "true", "yes"):
        return True  # cockpit's _scheduler_loop already gated on market_status()
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        try:
            import pytz
            now = datetime.now(pytz.timezone("America/New_York"))
        except Exception:
            # Timezone library unavailable — default to CLOSED so we never fire
            # blindly during off-hours.
            return False
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= hm < 16 * 60


# ─────────────────────────────────────────────────────────────────────────────
# Old candidates file pruner — prevents output/candidates/ from growing unbounded
# ─────────────────────────────────────────────────────────────────────────────
_KEEP_CANDIDATE_FILES = int(os.getenv("VEGA_KEEP_CANDIDATES", "20"))  # keep N most-recent pairs


def _prune_candidates(keep: int = _KEEP_CANDIDATE_FILES) -> int:
    """Delete oldest candidates_*.json and candidates_*.html files, keeping the most recent `keep` of each."""
    removed = 0
    for ext in ("json", "html"):
        files = sorted(glob.glob(str(BASE / "output" / "candidates" / f"candidates_*.{ext}")))
        for old in files[:-keep]:
            try:
                os.remove(old)
                removed += 1
            except Exception:
                pass
    return removed


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}")


def _run(cmd: List[str]) -> int:
    _log(f"RUN: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(BASE), check=False)
        return int(proc.returncode)
    except Exception as exc:
        _log(f"ERROR running command: {exc}")
        return 1


def _latest_candidates() -> Tuple[Optional[Dict], Optional[Path]]:
    files = sorted(glob.glob(str(BASE / "output" / "candidates" / "candidates_*.json")))
    if not files:
        return None, None
    path = Path(files[-1])
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except Exception as exc:
        _log(f"Failed to parse candidates file {path.name}: {exc}")
        return None, path


def _trade_key(ticker: str, short_strike, long_strike, expiration: str) -> Tuple[str, float, float, str]:
    return (str(ticker).upper(), float(short_strike), float(long_strike), str(expiration))


def _current_dte(expiration) -> Optional[int]:
    """Days-to-expiration as of NOW (not the value stored at open, which never decreases)."""
    try:
        return (datetime.strptime(str(expiration), "%Y-%m-%d").date() - datetime.now().date()).days
    except Exception:
        return None


def _apply_close_rules(r: Dict, mark_price: float, dte, roundtrip_cost: float,
                       target_profit_pct: float, stop_mult: float) -> bool:
    """Close an open paper trade if it hit the 50% profit target, the 2x stop, or the DTE window.
    Shared by the normal cycle and the mark-only (end-of-day) run."""
    entry = r.get("actual_fill_credit")
    if not isinstance(entry, (int, float)):
        return False
    target_exit = float(entry) * (1.0 - target_profit_pct)
    stop_exit = float(entry) * stop_mult
    should_close = False
    outcome = "scratch"
    reason = "auto-monitor"
    if float(mark_price) <= target_exit:
        should_close, outcome, reason = True, "win", "auto-target-profit"
    elif float(mark_price) >= stop_exit:
        should_close, outcome, reason = True, "loss", "auto-stop-loss"
    elif isinstance(dte, (int, float)) and dte <= 7:
        net = ((float(entry) - float(mark_price)) * 100.0) - roundtrip_cost
        outcome = "win" if net > 1.0 else ("loss" if net < -1.0 else "scratch")
        should_close, reason = True, "auto-dte-window"
    if should_close and ol.set_close(r.get("id"), float(mark_price), outcome, reason):
        _log(f"AUTO-CLOSE {r.get('id')} | exit={float(mark_price):.2f} outcome={outcome} reason={reason}")
        return True
    return False


def _missing_gates(c: Dict) -> List[str]:
    """Gate keys REQUIRED_GATES demands that this candidate does not carry a result for.

    A missing key is a broken scanner contract, not a failing trade — it means the scan stopped
    emitting a gate the trade path believes is being enforced. That is the exact failure mode
    behind the IV-rank, POP and quote-spread leaks, so it must be loud.
    """
    gates = c.get("gates") or {}
    return [k for k in getattr(config, "REQUIRED_GATES", ()) if k not in gates]


def _entry_state(c: Dict, ctx: Dict) -> Dict:
    """The raw measurements behind the entry, pulled from the candidate and its row context.

    Every field degrades to None independently. A missing IV must not cost the trade its
    pop_gap, and a fast-scan candidate with no calibrated true_pop must record None rather
    than a zero that would later read as "the engine claimed no edge".
    """
    def _f(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    atm_iv = _f(ctx.get("atm_iv")) or _f(c.get("short_iv"))
    spot = _f(ctx.get("spot")) or _f(ctx.get("price"))
    dte = c.get("dte")

    em = None
    if atm_iv and spot and dte:
        try:
            from analysis.horizon import expected_move
            # Reuse rather than reimplement: horizon already defines the 1-sigma move as
            # spot*iv*sqrt(dte/365) over CALENDAR days, and the strike distances and level
            # cushions on the board are already expressed in that unit. A second formula here
            # — on 252 trading days, say — would overstate the move by ~20% and make the two
            # silently incomparable.
            em = expected_move(spot, atm_iv, int(dte))
            em = round(em, 4) if em is not None else None
        except Exception as e:                       # pragma: no cover - defensive
            _log(f"expected_move failed for {c.get('ticker')}: {e}")

    true_pop = _f(c.get("true_pop") or c.get("pop_true"))
    gap = c.get("pop_gap")
    if gap is None and true_pop is not None:
        gap = round(true_pop - (_f(c.get("pop_implied")) or 0.0), 4)

    return {
        "atm_iv_at_entry": round(atm_iv, 4) if atm_iv else None,
        "rv_at_entry": (lambda r: round(r, 4) if r else None)(_f(ctx.get("rv"))),
        "expected_move_at_entry": em,
        "pop_gap_at_entry": gap,
    }


def _candidate_passes_minimum(c: Dict, verbose: bool = False) -> bool:
    """Gate a candidate for auto-open. `verbose` logs the failing gates for picked candidates.

    Gate results are NOT logged for every enumerated candidate — a scan produces ~470 of them
    across 9 gates, and 4k log lines per cycle would bury the events that matter. Callers that
    care about a specific candidate pass verbose=True.
    """
    gates = c.get("gates") or {}
    # Enforce the full contract rather than a hand-maintained subset. The old local tuple omitted
    # `pop` (and had no way to know about quote_spread), which is how the POP floor went unenforced.
    required = tuple(getattr(config, "REQUIRED_GATES", ()))
    failed = [k for k in required if not gates.get(k, False)]
    if failed:
        if verbose:
            _log(f"[GATE] {c.get('ticker')} REJECT failed={failed}")
        return False
    credit_usd = c.get("credit_usd")
    if not isinstance(credit_usd, (int, float)) or credit_usd < float(getattr(config, "MIN_CREDIT_USD", 25)):
        if verbose:
            _log(f"[GATE] {c.get('ticker')} REJECT credit_usd={credit_usd} "
                 f"< MIN_CREDIT_USD={getattr(config, 'MIN_CREDIT_USD', 25)}")
        return False
    # POP floor. main.py:491 gates on the calibrated probability of profit, but the auto-open
    # path enforced NO pop floor at all — `pop` was computed as a gate annotation and then left
    # out of `required`. Same enforcement leak the IV-rank gate closed on 2026-07-25. Prefer
    # true_pop (drift-removed, attached by vega_candidates.attach_true_pop); fall back to
    # pop_implied for snapshots written before that wiring existed.
    min_pop = float(getattr(config, "MIN_PROBABILITY_OF_PROFIT", 0.72))
    pop = c.get("true_pop")
    if pop is None:
        pop = c.get("pop_implied")
    if not isinstance(pop, (int, float)) or float(pop) < min_pop:
        if verbose:
            _log(f"[GATE] {c.get('ticker')} REJECT pop={pop} < MIN_PROBABILITY_OF_PROFIT={min_pop} "
                 f"(source={'true_pop' if c.get('true_pop') is not None else 'pop_implied'})")
        return False
    return True


def _candidate_score(c: Dict) -> float:
    gates_passed = float(c.get("gates_passed") or 0)
    gates_total = float(c.get("gates_total") or 8)
    q = (gates_passed / gates_total) if gates_total else 0.0
    pop = float(c.get("pop_implied") or 0.0)
    roi = float(c.get("roi") or 0.0)
    delta = abs(float(c.get("short_delta") or 0.0))
    delta_penalty = abs(delta - float(getattr(config, "SHORT_STRIKE_TARGET_DELTA", 0.20)))
    return (q * 100.0) + (pop * 35.0) + (roi * 25.0) - (delta_penalty * 40.0)


def _pick_new_trades(cand_data: Dict, open_rows: List[Dict], max_open_total: int, max_new_per_run: int) -> List[Tuple[str, Dict, Dict]]:
    open_keys = set()
    open_tickers = set()
    for r in open_rows:
        try:
            open_keys.add(_trade_key(r.get("ticker"), r.get("short_strike"), r.get("long_strike"), r.get("expiration")))
        except Exception:
            pass
        if r.get("ticker"):
            open_tickers.add(str(r.get("ticker")).upper())

    slots = max(0, max_open_total - len(open_rows))
    if slots <= 0:
        return []

    budget = min(slots, max_new_per_run)
    ranked: List[Tuple[float, str, Dict, Dict]] = []
    # Rejection tally so a zero-pick cycle is explainable. On 2026-07-31 a scan returned 0
    # candidates across 50 tickers and the snapshot recorded no reason why.
    rej: Dict[str, int] = {}
    seen_cands = 0
    contract_broken = False

    def _rej(key: str) -> None:
        rej[key] = rej.get(key, 0) + 1

    for row in cand_data.get("rows", []):
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        if ticker in open_tickers:
            _rej("already_open_ticker")
            continue
        # IV-rank hard gate (audit fix 2026-07-25). MIN_IV_RANK is enforced in
        # the cockpit scan (main.py) but was NOT wired into the auto-open path,
        # so cheap premium could be selected here — e.g. TSLA @ IV-rank 9.8, the
        # only realized loss in the ledger. Never sell premium below the floor.
        _ivr = (row.get("ctx") or {}).get("iv_rank")
        if isinstance(_ivr, (int, float)) and _ivr < float(getattr(config, "MIN_IV_RANK", 45)):
            _rej("iv_rank_below_floor")
            continue
        for c in row.get("candidates", []):
            seen_cands += 1
            # A MISSING gate key is a broken scanner contract, not a failing trade — but
            # `gates.get(k, False)` renders them identical, so a stale snapshot would silently
            # yield zero trades instead of reporting the mismatch. Surface it once, loudly.
            if not contract_broken:
                _missing = _missing_gates(c)
                if _missing:
                    contract_broken = True
                    _log(
                        f"[GATE] CONTRACT MISMATCH — candidates are missing REQUIRED_GATES "
                        f"results: {_missing}. The snapshot ({cand_data.get('meta', {}).get('stamp', 'unknown')}) "
                        f"predates these gates, or vega_candidates no longer emits them. "
                        f"No trades will be opened until the scan and config.REQUIRED_GATES agree."
                    )
            try:
                key = _trade_key(ticker, c.get("short_strike"), c.get("long_strike"), c.get("expiration"))
            except Exception as exc:
                _log(f"[GATE] {ticker} REJECT malformed candidate (no trade key): {exc}")
                _rej("malformed_candidate")
                continue
            if key in open_keys:
                _rej("already_open_position")
                continue
            if not _candidate_passes_minimum(c):
                _rej("failed_gates")
                continue
            ranked.append((_candidate_score(c), ticker, row, c))

    ranked.sort(key=lambda x: x[0], reverse=True)

    _log(f"[GATE] scanned {seen_cands} candidate(s); {len(ranked)} eligible; rejections: "
         f"{dict(sorted(rej.items(), key=lambda kv: -kv[1])) or 'none'}")

    # When nothing survives, name the gates responsible rather than leaving a bare zero.
    if not ranked and seen_cands:
        tally: Dict[str, int] = {}
        for row in cand_data.get("rows", []):
            for c in row.get("candidates", []):
                for k in getattr(config, "REQUIRED_GATES", ()):
                    if not (c.get("gates") or {}).get(k, False):
                        tally[k] = tally.get(k, 0) + 1
        _log(f"[GATE] no eligible candidates — failures by gate: "
             f"{dict(sorted(tally.items(), key=lambda kv: -kv[1]))}")

    picked: List[Tuple[str, Dict, Dict]] = []
    seen_tickers = set()
    for _score, ticker, row, c in ranked:
        if ticker in seen_tickers:
            continue
        picked.append((ticker, row, c))
        seen_tickers.add(ticker)
        if len(picked) >= budget:
            break
    return picked


def _auto_open_from_candidates(cand_data: Dict, source_file: str) -> int:
    rows = ol.load_records()
    open_rows = [r for r in rows if r.get("status") == "open" and r.get("mode") == "paper"]

    max_open_total = int(os.getenv("VEGA_MAX_OPEN_TOTAL", "15"))
    max_new_per_run = int(os.getenv("VEGA_MAX_NEW_PER_RUN", "5"))

    picks = _pick_new_trades(cand_data, open_rows, max_open_total=max_open_total, max_new_per_run=max_new_per_run)
    if not picks:
        _log("No new auto-open candidates this cycle.")
        return 0

    # GATE CONTRACT CHECK. If the scanner stopped emitting a gate that REQUIRED_GATES claims is
    # enforced, every downstream `gates.get(k, False)` would quietly read False-or-missing and the
    # trade path would be running on a rule set nobody verified. Fail the whole auto-open loudly
    # rather than open trades under an unknown gate set — this is what makes leak #4 impossible
    # to ship silently.
    for _tk, _row, _c in picks:
        missing = _missing_gates(_c)
        if missing:
            _log(
                f"ABORT AUTO-OPEN — candidate {_tk} is missing REQUIRED_GATES results: {missing}. "
                f"The scanner (vega_candidates.build_candidates) and config.REQUIRED_GATES are out "
                f"of sync; no trades opened this cycle."
            )
            return 0

    opened = 0
    for ticker, row, c in picks:
        try:
            # FILL MODEL (audit fix 2026-08-02). Trades used to book `credit_per_share`, the MID.
            # You cannot fill a credit spread at the mid: you sell the short leg at its BID and buy
            # the long leg at its ASK. Across the 2026-07-31 snapshot the mid overstated the
            # achievable credit by 75.5% on average (mean $92.33 vs $21.85 natural), which made the
            # entire P&L record optimistically invalid. Book the natural credit instead.
            natural = c.get("natural_credit_per_share")
            if natural is None:
                _log(f"REJECTED_NO_NATURAL_CREDIT {ticker} — candidate has no bid/ask credit; skipping.")
                continue
            natural = float(natural)
            if natural <= 0:
                # ~25% of candidates price this way: at natural prices they are debit spreads,
                # not credit spreads. They were previously opened as if they paid the mid.
                _log(
                    f"REJECTED_NEGATIVE_NATURAL_CREDIT {ticker} "
                    f"{c['short_strike']:g}/{c['long_strike']:g} {c['expiration']} "
                    f"natural={natural:.2f} (mid={float(c.get('credit_per_share') or 0):.2f}) "
                    f"— unfillable as a credit spread; not opening."
                )
                continue

            # Raw entry state. The score components below are the engine's CONCLUSIONS; these
            # are the measurements they were drawn from. Without them a calibration run can
            # only ask whether a score was right, never whether it was wrong because the
            # inputs were wrong or because the weighting was. All of it is already sitting in
            # the row context — it was simply never persisted.
            _ctx = row.get("ctx") or {}
            _entry = _entry_state(c, _ctx)

            tid = ol.open_paper_trade(
                ticker=ticker,
                short_strike=c["short_strike"],
                long_strike=c["long_strike"],
                expiration=c["expiration"],
                entry_credit_per_share=natural,
                dte=c.get("dte"),
                delta=c.get("short_delta"),
                iv_rank=(row.get("ctx") or {}).get("iv_rank"),
                implied_pop=c.get("pop_implied"),
                # true_pop: drift-removed calibrated POP from the edge calculator.
                # Present when opening from the main engine; None from vega_candidates fast scan.
                true_pop=c.get("true_pop") or c.get("pop_true"),
                p_max_profit=c.get("p_max_profit"),
                contracts=1,
                fill_model="natural",
                natural_credit_per_share=natural,
                mid_credit_per_share=c.get("credit_per_share"),
                source="auto-paper",
                note=f"auto from {source_file}",
                theta=c.get("short_theta"),
                # What the engine BELIEVED at entry. Without these the calibration engine has
                # nothing to correlate outcomes against — it was the missing half of the
                # feedback loop. Absent from the vega_candidates fast-scan schema, so they
                # stay None on that path rather than being faked.
                edge_score=c.get("edge_score"),
                vrp=c.get("vrp") or (row.get("ctx") or {}).get("vrp"),
                technical_score=c.get("composite_score") or c.get("technical_score"),
                term_slope=c.get("term_slope"),
                skew_steepness=c.get("skew_steepness"),
                vix_at_entry=_ctx.get("vix"),
                atm_iv_at_entry=_entry["atm_iv_at_entry"],
                rv_at_entry=_entry["rv_at_entry"],
                expected_move_at_entry=_entry["expected_move_at_entry"],
                pop_gap_at_entry=_entry["pop_gap_at_entry"],
            )
            # Write down every falsifiable claim this trade carries. The engine has always
            # made these assertions and always discarded them, which is why nothing it
            # predicts has ever been marked.
            try:
                from analysis import predictions as _pred
                _claims = _pred.record_trade_predictions(
                    {**c, "ticker": ticker, "close_logic": "ravens_v1"}, tid)
                if _claims:
                    _log(f"PREDICTIONS {tid}: recorded "
                         f"{', '.join(x.split('::')[1] for x in _claims)}")
            except Exception as _e:
                _log(f"Prediction recording failed for {tid}: {_e}")
            _log(
                f"AUTO-OPEN {tid} | {ticker} {c['short_strike']:g}/{c['long_strike']:g} {c['expiration']} "
                f"credit={natural:.2f} (natural; mid was {float(c.get('credit_per_share') or 0):.2f})"
            )
            opened += 1
        except Exception as exc:
            _log(f"Failed to auto-open candidate {ticker}: {exc}")
    return opened


def _ravens_close_check(position: Dict, decision_mark, short_leg, long_leg, exp) -> Optional[Dict]:
    """Run Huginn, Muninn and Odin for one open position. Returns Odin's synthesis, or None
    when the framework is off or the data needed is unavailable. Never closes anything."""
    if not getattr(config, "RAVENS_FRAMEWORK_ENABLED", True):
        return None
    try:
        from data import fetcher
        from analysis import huginn as H, muninn as M, odin as O
        from analysis import outcome_logger as ol_

        df = fetcher.get_price_data(position.get("ticker"), period="6mo")
        if df is None or df.empty:
            return None
        data = {
            "current_price": float(df["Close"].iloc[-1]),
            "closes": [float(x) for x in df["Close"].tolist()],
            "highs": [float(x) for x in df["High"].tolist()],
            "lows": [float(x) for x in df["Low"].tolist()],
            "volumes": [float(x) for x in df["Volume"].tolist()] if "Volume" in df else [],
            "current_delta": (short_leg or {}).get("delta"),
            "mark": decision_mark,
            "dte_remaining": _current_dte(exp),
            "as_of": _now(),
            "news_sentiment": None,      # close-time news read is not wired yet
            "earnings_check": {},        # nor is a close-time earnings check
        }
        h = H.evaluate(position, data)
        m = M.compute_recovery_probability(position, h, ol_.load_records(), data)
        o = O.synthesize(h, m, position)
        o["_huginn"], o["_muninn"] = h, m
        # Record what this looked like under pressure so Memory can answer next time. The
        # observation cannot be reconstructed after the fact, which is exactly why Muninn is
        # blind today.
        if h.get("thesis_status") in ("UNDER_PRESSURE", "VIOLATED", "WOLF"):
            try:
                ol_.append_stress_snapshot(position.get("id"),
                                           M.record_stress_snapshot(position, h, data))
            except Exception as e:
                _log(f"Ravens: could not record stress snapshot for {position.get('id')}: {e}")
        return o
    except Exception as e:
        _log(f"Ravens check failed for {position.get('id')}: {e}")
        return None


def _ravens_or_legacy_close(r: Dict, mark, decision_mark, short_leg, long_leg, exp,
                            roundtrip_cost, target_profit_pct, stop_mult) -> bool:
    """Profit target and the DTE window are unchanged and still run on the legacy path. Only
    the close-for-loss decision moves to the ravens."""
    entry = r.get("actual_fill_credit")
    if isinstance(entry, (int, float)) and float(entry) > 0:
        # Profit target first — it is not in dispute and needs no raven.
        if float(decision_mark) <= float(entry) * (1.0 - target_profit_pct):
            return _apply_close_rules(r, mark, _current_dte(exp), roundtrip_cost,
                                      target_profit_pct, stop_mult)

    synthesis = _ravens_close_check(r, decision_mark, short_leg, long_leg, exp)
    if synthesis is None:
        # No raven read available — fall back to the legacy rules so a position is never left
        # completely unmanaged.
        return _apply_close_rules(r, mark, _current_dte(exp), roundtrip_cost,
                                  target_profit_pct, stop_mult)

    rec = synthesis["recommendation"]
    if rec in ("WOLF_CLOSE", "CLOSE"):
        outcome = "loss" if float(mark) > float(entry or 0) else "win"
        reason = "wolf-stop" if rec == "WOLF_CLOSE" else "raven-thesis-violated"
        if ol.set_close(r.get("id"), float(mark), outcome, reason):
            _log(f"RAVEN-CLOSE {r.get('id')} [{rec}] {synthesis['plain_english']}")
            return True
        return False

    if rec in ("HOLD_TENSION", "MUNINN_BLIND"):
        _log(f"RAVENS ALERT [{r.get('id')}] {rec}: {synthesis['plain_english']}")
        _record_raven_alert(r, synthesis)

    # DTE window still closes the position regardless of what the ravens think — an expiring
    # position is a mechanical fact, not a thesis question.
    dte = _current_dte(exp)
    if isinstance(dte, (int, float)) and dte <= 7:
        return _apply_close_rules(r, mark, dte, roundtrip_cost, target_profit_pct, stop_mult)
    return False


def _record_raven_alert(position: Dict, synthesis: Dict) -> None:
    """Persist the divergence so the cockpit can surface it and so it is auditable later."""
    try:
        from analysis import outcome_logger as ol_
        ol_.append_raven_alert(position.get("id"), {
            "at": _now(),
            "recommendation": synthesis.get("recommendation"),
            "confidence": synthesis.get("confidence"),
            "plain_english": synthesis.get("plain_english"),
            "huginn_status": synthesis.get("huginn_status"),
            "muninn_probability": synthesis.get("muninn_probability"),
        })
    except Exception as e:
        _log(f"Could not record raven alert for {position.get('id')}: {e}")


def _level_breach_alerts(ticker: str, positions: List[Dict]) -> int:
    """Warn when an open position's structural thesis has broken.

    Every order ticket says "exit if it breaks support on volume", and until 2026-08-05
    nothing anywhere watched for it — trade management was purely price/DTE based, so the
    instruction was never actionable after entry.

    ADVISORY ONLY. It logs; it does not close. An auto-close on a support break would cut
    winners that dip and recover, which is a strategy change rather than a gap fix, and it
    belongs to Josh rather than to a heuristic.
    """
    if not getattr(config, "LEVEL_MANAGEMENT_ALERTS", True):
        return 0
    try:
        from data import fetcher
        from analysis.levels import find_levels

        px_data = fetcher.get_price_data(ticker, period="1y")
        if px_data is None or px_data.empty:
            return 0
        spot = float(px_data["Close"].iloc[-1])
        levels = find_levels(px_data["High"].tolist(), px_data["Low"].tolist(),
                             px_data["Close"].tolist())
        alerts = 0
        for r in positions:
            try:
                ks = float(r.get("short_strike"))
            except (TypeError, ValueError):
                continue
            # A shield that mattered: a level ABOVE the short strike which spot has now lost.
            # Once price is under it, the short strike is the next thing in the way.
            breached = [l for l in levels.get("support_levels", [])
                        if l["price"] > ks and spot < l["price"]]
            if not breached:
                continue
            lost = max(breached, key=lambda l: l["strength"])
            alerts += 1
            _log(f"LEVEL-ALERT {r.get('id')} {ticker} spot={spot:.2f} lost support "
                 f"${lost['price']:.2f} ({lost['touches']}x, str={lost['strength']:.0f}) "
                 f"— short strike {ks:.2f} is now the next level down. Advisory only.")
        return alerts
    except Exception as exc:
        _log(f"Level alerts skipped for {ticker}: {exc}")
        return 0


def _reprice_and_close_open() -> Tuple[int, int]:
    """Reprice EVERY open paper position from a fresh, wide options chain (not just names that
    still happen to be top candidates), update its mark, and auto-close by target/stop/DTE.

    This is the fix that lets trades actually RESOLVE: the previous candidate-snapshot marking
    only touched a position if its exact strike/expiration reappeared as a fresh top candidate,
    so most open positions were never re-marked and never met a close rule.
    """
    from data import fetcher

    rows = ol.load_records()
    open_rows = [r for r in rows if r.get("status") == "open" and r.get("mode") == "paper"]
    if not open_rows:
        return 0, 0

    target_profit_pct = float(getattr(config, "TARGET_PROFIT_PCT", 0.50))
    stop_mult = float(getattr(config, "STOP_LOSS_MULTIPLIER", 2.0))
    roundtrip_cost = float(ol._round_trip_cost_per_contract())

    by_ticker: Dict[str, List[Dict]] = {}
    for r in open_rows:
        by_ticker.setdefault(str(r.get("ticker")), []).append(r)

    marked = 0
    closed = 0
    for ticker, positions in by_ticker.items():
        try:
            chain = fetcher.get_options_chain(ticker, 0, 200)  # wide window to find held strikes
        except Exception as exc:
            _log(f"Reprice: chain fetch failed for {ticker}: {exc}")
            continue
        _level_breach_alerts(ticker, positions)
        idx = {(round(float(o["strike"]), 2), o["expiration"]): o for o in chain}
        for r in positions:
            exp = r.get("expiration")
            try:
                s = idx.get((round(float(r["short_strike"]), 2), exp))
                l = idx.get((round(float(r["long_strike"]), 2), exp))
            except Exception:
                continue
            if not (s and l):
                # Log explicitly so silent skips are visible in run.log.
                # CRM was skipped without any log entry on 2026-07-29; this closes that gap.
                _log(
                    f"Reprice: strike not found in chain for {r.get('id')} "
                    f"(short={r.get('short_strike')} long={r.get('long_strike')} exp={exp}) "
                    f"— skipping mark this cycle (chain depth: {len(idx)} strikes)"
                )
                continue
            # EXIT MARK (audit fix 2026-08-02). Closing a bull put spread is a BUY-TO-CLOSE, so the
            # direction is the mirror of entry: you pay the short leg's ASK and receive the long
            # leg's BID. Entry uses (short_bid - long_ask); reusing that formula here would make
            # exits look cheaper than reality and would be optimistic in the same way the mid was.
            #
            # Mark on the SAME basis the position was entered on. Legacy positions were entered at
            # the mid; marking those at natural would charge them a spread they never collected,
            # inventing losses and corrupting them as a benchmark. Each cohort stays internally
            # consistent: mid-in/mid-out, natural-in/natural-out.
            if (r.get("fill_model") or "mid") == "mid":
                mark = round(float(s.get("mid") or 0) - float(l.get("mid") or 0), 2)
            else:
                s_ask, l_bid = s.get("ask"), l.get("bid")
                if s_ask and l_bid:
                    mark = round(float(s_ask) - float(l_bid), 2)
                else:
                    # Quote gap: degrade to mid rather than mark at zero, but say so — this mark
                    # is optimistic relative to the position's own natural basis.
                    mark = round(float(s.get("mid") or 0) - float(l.get("mid") or 0), 2)
                    _log(
                        f"Reprice: missing ask/bid for {r.get('id')} — marking at mid "
                        f"(short_ask={s_ask} long_bid={l_bid}); this mark is optimistic."
                    )
            if ol.set_mark(r.get("id"), mark):
                marked += 1

            # DECISION MARK. `mark` above is the natural (worst-side) price and stays the
            # basis for realised P&L — the record must remain honest about slippage. But it
            # is the wrong number to make a CLOSE decision on: entry natural is
            # short_bid-long_ask and exit natural is short_ask-long_bid, so a position pays
            # the full bid-ask on both legs twice before it can show a profit. Live GDX
            # 76/75 on 2026-08-06 quoted entry +$0.03 against exit +$0.49 — a 16.3x apparent
            # loss at t=0 with no price movement. That is why 40 of 45 stop-outs fired
            # inside 24 hours and 44 of 45 underlyings are now back above the strike.
            decision_mark = mark
            if getattr(config, "CLOSE_DECISION_MARK_BASIS", "mid") == "mid":
                s_mid, l_mid = s.get("mid"), l.get("mid")
                if s_mid is not None and l_mid is not None:
                    decision_mark = round(float(s_mid) - float(l_mid), 2)

            if _ravens_or_legacy_close(r, mark, decision_mark, s, l, exp,
                                       roundtrip_cost, target_profit_pct, stop_mult):
                closed += 1
    return marked, closed


def _resolve_predictions() -> Dict:
    """Mark every claim whose horizon has passed. This is the half of the learning loop that
    turns a recorded assertion into a graded one; without it the ledger only accumulates."""
    try:
        from analysis import predictions as pred
        from data import fetcher

        def lookup(ticker, start, end):
            df = fetcher.get_price_data(ticker, period="1y")
            if df is None or df.empty:
                return []
            out = []
            for idx, row in df.iterrows():
                d = idx.date() if hasattr(idx, "date") else idx
                if start <= d <= end:
                    out.append((d, float(row["High"]), float(row["Low"]), float(row["Close"])))
            return out

        stats = pred.resolve(lookup)
        if stats.get("checked"):
            _log(f"PREDICTIONS resolved={stats['resolved']} "
                 f"unresolvable={stats['unresolvable']} of {stats['checked']} due")
        return stats
    except Exception as e:
        _log(f"Prediction resolution failed: {e}")
        return {}


def _acquire_lock(max_age_seconds: Optional[int] = None) -> bool:
    # Stale-lock threshold: a crashed/killed run must not wedge the cycle. Default 30 min -
    # comfortably above a normal run, below the inter-run gap and the 25-min task kill limit.
    # Override with env VEGA_LOCK_STALE_MIN (minutes).
    if max_age_seconds is None:
        max_age_seconds = int(os.getenv("VEGA_LOCK_STALE_MIN", "30")) * 60
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < max_age_seconds:
                _log("Another cycle appears active; skipping this run.")
                return False
            LOCK_FILE.unlink(missing_ok=True)
            _log("Removed stale automation lock.")
        except Exception:
            return False
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="VEGA auto paper cycle")
    ap.add_argument(
        "--mark-only", action="store_true",
        help="Skip scanning/opening; only reprice open positions and auto-close by "
             "target/stop/DTE. Use for a dedicated end-of-day resolution run.",
    )
    ap.add_argument(
        "--force-market-open", action="store_true",
        help="Bypass the market-hours check (for testing / manual runs).",
    )
    args = ap.parse_args(argv)

    # ── Market-hours gate ─────────────────────────────────────────────────────
    # Exit immediately when the market is closed so scheduled (e.g. Windows Task
    # Scheduler) runs during evenings, overnights, and weekends are no-ops. The
    # cockpit's internal _scheduler_loop already guards on market_status(), so
    # when VEGA_COCKPIT_SPAWNED=1 this check is a fast-pass.
    if not args.force_market_open and not is_market_open():
        _log("Market closed — skipping cycle. (Pass --force-market-open to override.)")
        return 0
    # ─────────────────────────────────────────────────────────────────────────

    if not _acquire_lock():
        return 0

    try:
        # Mark-only: resolve existing positions without opening new ones near the close.
        if args.mark_only:
            _log("=== AUTO PAPER CYCLE (MARK-ONLY) START ===")
            marked, closed = _reprice_and_close_open()
            _resolve_predictions()
            _run([sys.executable, "paper_desk.py", "report"])
            _run([sys.executable, "paper_desk.py", "dashboard", "--no-open"])
            _log(f"Mark-only summary: marked={marked}, closed={closed}")
            _log("=== AUTO PAPER CYCLE (MARK-ONLY) END ===")
            return 0

        _log("=== AUTO PAPER CYCLE START ===")

        # --no-open  : suppress browser launch from subprocess
        # --no-html  : write only the JSON snapshot (no standalone HTML file) so
        #              output/candidates/ doesn't accumulate 100 KB HTML files on
        #              every scheduled run. The JSON is still written and read back
        #              below by _latest_candidates().
        rc = _run([sys.executable, "vega_candidates.py", "--no-open", "--no-html"])
        if rc != 0:
            _log("vega_candidates.py failed; skipping open/grade this cycle.")
            return rc

        cand_data, cand_path = _latest_candidates()
        if not cand_data or not cand_path:
            _log("No candidate snapshot found after scan.")
            return 1

        opened = _auto_open_from_candidates(cand_data, cand_path.name)
        marked, closed = _reprice_and_close_open()
        _resolve_predictions()

        _run([sys.executable, "paper_desk.py", "report"])
        _run([sys.executable, "paper_desk.py", "dashboard", "--no-open"])

        pruned = _prune_candidates()
        _log(
            f"Cycle summary: opened={opened}, marked={marked}, closed={closed}, "
            f"snapshot={cand_path.name}, pruned_old_files={pruned}"
        )
        _log("=== AUTO PAPER CYCLE END ===")
        return 0
    finally:
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
