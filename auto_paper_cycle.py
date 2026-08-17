#!/usr/bin/env python3
"""
auto_paper_cycle.py

Automates one paper-trading cycle:
1) Analyze: run main.py to build the BOARD — the same list the operator reviews.
2) Select: auto-open from that board, so the learning history grades what was shown.
   (vega_candidates.py also runs, demoted to recording counterfactuals only.)
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
    # Only a stop carries a multiplier. This path is the LEGACY stop (STOP_LOSS_MULTIPLIER,
    # 1.5x credit) — it fires when the ravens cannot judge, which is the data-failure fallback.
    # The wolf floor (3.0x) fires from _ravens_or_legacy_close and stamps its own value.
    eff_mult = stop_mult if reason == "auto-stop-loss" else None
    if should_close and ol.set_close(r.get("id"), float(mark_price), outcome, reason,
                                     effective_stop_multiplier=eff_mult):
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


def _min_credit_floor(c: Dict) -> float:
    """The credit floor for this candidate, from the single definition in config.

    Reads `spot` off the candidate (stamped by build_candidates). A snapshot written before
    that field existed returns None and gets the flat floor — stricter, never looser, so an
    old file can never open a trade the current contract would refuse.
    """
    fn = getattr(config, "min_credit_usd_for", None)
    if callable(fn):
        return float(fn(c.get("spot")))
    return float(getattr(config, "MIN_CREDIT_USD", 25))


def _current_vix() -> Optional[float]:
    """Spot VIX, or None. Never raises — every caller treats it as advisory colour."""
    try:
        from data import fetcher
        v = (fetcher.get_vix() or {}).get("current")
        return float(v) if isinstance(v, (int, float)) else None
    except Exception:
        return None


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
    # Spot comes off the CANDIDATE. This read `ctx["spot"]` and `ctx["price"]`, and the row
    # context is vega_candidates.vol_context's output, which emits neither — the price lives on
    # the enclosing row and, since the credit floor started scaling with it, on the candidate
    # itself. So spot was None on every trade, and with it expected_move_at_entry, because the
    # move needs `atm_iv and spot and dte`. Found by tests/test_schema_contracts.py, which is
    # the sixth instance of this shape and the first one a test caught rather than a person.
    spot = _f(c.get("spot"))
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

    # Cross-venue vol, present only on BTC trackers. Read off the analysis block the assessment
    # already produced rather than re-fetching — the scan measured it at candidate time, and a
    # second read at open time would record a different number from the one that was judged.
    bx = (c.get("analysis") or {}).get("btc_cross_venue") or {}

    return {
        "atm_iv_at_entry": round(atm_iv, 4) if atm_iv else None,
        "rv_at_entry": (lambda r: round(r, 4) if r else None)(_f(ctx.get("rv"))),
        "expected_move_at_entry": em,
        "pop_gap_at_entry": gap,
        "btc_iv_gap_pp": bx.get("iv_gap_pp") if bx.get("available") else None,
        "btc_vrp_pp": bx.get("btc_vrp_pp") if bx.get("available") else None,
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

    # pop_gap: VEGA's own probability against the market's. Negative means the engine rates the
    # trade WORSE than the price assumes, and none of the eleven contract gates tests it —
    # observed live on IBIT at -12.6pp while passing 11/11. Decision 2026-08-14: HARD for the
    # robot, ADVISORY for the operator. The desk may knowingly take a trade its model dislikes;
    # an unattended process must not. Part of the frozen cohort contract — see config.
    # Position-size cap. MAX_RISK_PER_TRADE_USD has existed in config since the beginning and
    # was read by NOTHING except the tip-sheet renderer — a display value masquerading as a
    # risk control. Measured 2026-08-16: 13 open positions carrying $4,141 of defined risk
    # against a $500 account (8.3x), with 11 of 13 individually over the $100 cap. Every P&L
    # and win-rate figure in the ledger is confounded by uncontrolled sizing until this binds.
    #
    # HARD for the auto-trader, which trades the paper account and must respect its size.
    # NOT applied to the board: MAX_SPREAD_WIDTH was deliberately decoupled from account size
    # on 2026-08-14 so the cockpit surfaces opportunities at every risk level and RISK_TIERS
    # says what each costs. The operator may take a $900 spread knowingly; an unattended
    # process sizing to a $500 account may not. Same split as the pop_gap gate below.
    cap = float(getattr(config, "MAX_RISK_PER_TRADE_USD", 0) or 0)
    if cap > 0:
        ml = c.get("max_loss_usd")
        if ml is None:
            w = c.get("width")
            nc = c.get("natural_credit_usd")
            ml = (float(w) * 100.0 - float(nc)) if (w is not None and nc is not None) else None
        if ml is None:
            # No sizing information is not a pass. A spread whose max loss cannot be computed
            # cannot be shown to fit the account, and this gate exists precisely because
            # unmeasured risk accumulated silently for months.
            if verbose:
                _log(f"[GATE] {c.get('ticker')} REJECT max_loss_usd unknown — cannot size")
            return False
        if float(ml) > cap:
            if verbose:
                _log(f"[GATE] {c.get('ticker')} REJECT max_loss ${float(ml):,.0f} > cap ${cap:,.0f}")
            return False

    if getattr(config, "POP_GAP_GATE_AUTO_TRADER", True):
        gap = c.get("pop_gap")
        if gap is None:
            tp, ip = c.get("true_pop"), c.get("pop_implied")
            gap = (float(tp) - float(ip)) if (tp is not None and ip is not None) else None
        floor = float(getattr(config, "POP_GAP_MIN", 0.0))
        if gap is not None and gap < floor:
            if verbose:
                _log(f"[GATE] {c.get('ticker')} REJECT pop_gap={gap:+.4f} < {floor}")
            return False
    # The credit floor is enforced by the contract's `min_credit_usd` gate against
    # natural_credit_usd (assessment.evaluate_gates), which is the basis the desk actually
    # fills at. A second check used to sit here reading c["credit_usd"] — the MID value — as a
    # belt-and-braces floor. It could not leak past the stronger contract check, but it is the
    # exact shape of the bug that opened GDX 82/81 twice for $9 and $7 against a $19 floor
    # because the gate read mid and the fill was natural. Removed rather than left as a second,
    # weaker definition of a rule that already has one.
    #
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
    """Rank the candidates that already cleared the contract. Gates decide IF; this decides WHICH.

    Led by edge_score, which is the whole reason a spread was worth selecting: VRP, IV rank,
    technical quality and the drift-corrected POP, weighted in analysis.edge_calculator. This
    function used to ignore it entirely and lead on gate completeness — so when five candidates
    all passed, the one opened was the structurally tidiest rather than the richest. That was
    not a weighting choice; edge_score was None on all 79 real trades in the ledger because of
    two stacked bugs (true_pop attached after the assessment that needed it, and the result
    never copied onto the candidate). Both are fixed, so this term now carries a number.

    true_pop, not pop_implied. pop_implied is 1-|delta| — a restatement of the delta the
    delta_penalty term already prices. Ranking on it double-counted the same fact and discarded
    the drift-removed probability the engine exists to compute.

    Weights are a starting point and are NOT validated. They should move once the prediction
    ledger has graded claims and the calibration engine can say which components carried
    information — see the value-of-information work. Until then this is an ordering, not a score.
    """
    gates_passed = float(c.get("gates_passed") or 0)
    # len(REQUIRED_GATES), not 8. The literal predated three gates, so a candidate missing the
    # key scored gates_passed/8 — which exceeds 1.0 at 11 passing gates and let the completeness
    # term dominate everything else.
    default_total = float(len(getattr(config, "REQUIRED_GATES", ())) or 1)
    gates_total = float(c.get("gates_total") or default_total)
    q = (gates_passed / gates_total) if gates_total else 0.0

    edge = float(c.get("edge_score") or 0.0) / 100.0          # 0-100 → 0-1, same scale as the rest
    true_pop = float(c.get("true_pop") or c.get("pop_implied") or 0.0)
    roi = float(c.get("roi") or 0.0)
    delta = abs(float(c.get("short_delta") or 0.0))
    delta_penalty = abs(delta - float(getattr(config, "SHORT_STRIKE_TARGET_DELTA", 0.20)))

    return ((edge * 50.0) + (q * 30.0) + (true_pop * 20.0) + (roi * 10.0)
            - (delta_penalty * 40.0))


# ─────────────────────────────────────────────────────────────────────────────
# ORPHANED: the candidates-path selection subtree
# ─────────────────────────────────────────────────────────────────────────────
# _pick_new_trades, _candidate_score, _candidate_passes_minimum, _missing_gates,
# _min_credit_floor and _entry_state selected trades from the vega_candidates snapshot. The
# desk now opens from the board (_auto_open_from_board), so NOTHING BELOW IS REACHABLE from
# the cycle.
#
# They are still here, and still tested, and that combination is precisely what inverted the
# earnings gate: vega_candidates carried a correct fail-closed implementation with thirteen
# passing tests and no caller, while the live path did the opposite for weeks. A gate "fixed"
# in _candidate_passes_minimum today would change nothing and no test would say so.
#
# Left in place deliberately for one release rather than deleted in the same change that
# rewired the desk — the tests below encode four hard-won enforcement leaks and deserve to be
# repointed with fresh attention, not folded into a rewiring commit. test_orphaned_selection_
# subtree_is_unreachable holds the line until then.


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
        # FAILS CLOSED. This used to read `isinstance(_ivr, (int, float)) and _ivr < floor`,
        # so a ticker whose IV rank could not be computed at all skipped the floor entirely and
        # was allowed to trade — the richness gate failing OPEN on exactly the tickers we know
        # least about. It became reachable on 2026-08-09 when estimate_atm_iv started returning
        # 0.0 instead of a wrong whole-chain median: "no number" now flows where "wrong number"
        # used to. Unknown richness is a reason not to sell, the same way the earnings gate
        # fails closed by design.
        _ivr = (row.get("ctx") or {}).get("iv_rank")
        if not isinstance(_ivr, (int, float)):
            _rej("iv_rank_unknown")
            continue
        if _ivr < float(getattr(config, "MIN_IV_RANK", 45)):
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


# _auto_open_from_candidates lived here: it opened from the vega_candidates snapshot while the
# operator reviewed main.py's board. Two lists, one of them traded and the other one read.
# _auto_open_from_board replaces it — the desk opens what was shown. Deleted rather than kept
# "just in case": a second opener is a second answer to which trades are real, and this file
# has spent the week removing exactly that shape.


# Structures the paper desk can actually MANAGE, not merely open. A trade it cannot mark is a
# trade it cannot close or learn from, so opening one is worse than skipping it: the position
# looks managed and is not.
#
# Iron condors are absent deliberately. The ledger records short_strike and long_strike — two
# legs — and a condor has four. It cannot be represented, let alone marked, and inventing a
# representation here would put un-markable positions into the record the whole learning loop
# depends on.
MANAGEABLE_STRATEGIES = ("bull_put", "bear_call")


def _strategy_key(record: Dict) -> str:
    """Normalise the strategy label. main.py emits 'bull_put_spread' from one path and
    'Bear Call Spread' from another — same concept, two spellings, and a comparison against
    either literal silently misses the other."""
    return str(record.get("strategy") or "").strip().lower().replace(" ", "_")


def _is_call_side(record: Dict) -> bool:
    return "call" in _strategy_key(record) or "condor" in _strategy_key(record)


def is_manageable(record: Dict) -> bool:
    """Can the desk mark, manage and close this structure? If not, it must never open it."""
    k = _strategy_key(record)
    if not k:
        return True                      # legacy rows predate the field and are all bull puts
    return any(k.startswith(s) for s in MANAGEABLE_STRATEGIES)


def _earnings_check(position: Dict, exp) -> Dict:
    """Does an earnings print now fall inside this position's remaining life?

    Returns {} — "nothing known", which check_wolves reads as no wolf — rather than raising or
    guessing. A calendar lookup failing must not close a position, and must not stop the other
    ravens from evaluating; an absent answer here is strictly less dangerous than an absent
    answer at ENTRY, because the position is already on and being watched.

    Deliberately does NOT fail closed the way the entry gate does. Refusing to open on unknown
    earnings costs one cycle of opportunity; closing an open position on unknown earnings
    realises a loss on no evidence.
    """
    try:
        from data import fetcher, fundamentals
        # Keep None as None: days_until_earnings maps an unknown date to the 999 sentinel, and
        # "999 days away" and "no idea" must not read the same to a risk check.
        edt = fetcher.get_earnings_date(position.get("ticker"))
        d = fundamentals.days_until_earnings(edt) if edt is not None else None
        if d is None:
            return {}
        dte = _current_dte(exp)
        if dte is None:
            return {}
        return {"in_window": 0 <= int(d) <= int(dte), "days_until_earnings": int(d)}
    except Exception as e:
        _log(f"Earnings check unavailable for {position.get('id')}: {e}")
        return {}


BOARD_FILE = BASE / "logs" / "scan_latest.json"


def _auto_open_from_board() -> int:
    """Open exactly what the board recommended — the same list the operator reviewed.

    The board is the product; this desk exists to build an honest learning history OF that
    board. It used to open from the vega_candidates snapshot while the operator read main.py's
    engine artifact, so the trades being validated were not the trades being shown: different
    search, different strikes, different strategies. A history of trades nobody was recommended
    grades nothing.

    Three refusals, all deliberate:

      NOT LIVE. A board built on stale quotes carries a MODELLED credit, and booking a fill at
      a price that was never available is the exact defect that made the first 18 trades in
      this ledger look like a 72% win rate. No opens off a modelled board, ever.

      NOT MANAGEABLE. A structure the desk cannot mark is one it cannot close or learn from.
      See is_manageable.

      NOT GATED. The board enforces the shared contract before a trade qualifies, so anything
      here has already passed. Re-checked anyway: this is the last point where a leak between
      the engine and the desk could still open a trade nobody approved.
    """
    if not BOARD_FILE.exists():
        _log("No board artifact yet — run the engine (main.py) before the desk can open.")
        return 0
    try:
        board = json.loads(BOARD_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"Board unreadable: {exc}")
        return 0

    trades = board.get("qualified_trades") or []
    if not trades:
        _log("Board has no qualified trades this cycle; nothing to open.")
        return 0

    modelled = [t for t in trades if t.get("fill_basis") == "modelled"]
    if modelled:
        _log(f"REFUSING TO OPEN — the board's credits are MODELLED ({len(modelled)}/{len(trades)} "
             f"trades), which means it was built while quotes were stale. Booking a fill at a "
             f"price that was never available is how this ledger got its first 18 unusable "
             f"trades. Re-run the engine during market hours.")
        return 0

    rows = ol.load_records()
    open_rows = [r for r in rows if r.get("status") == "open" and r.get("mode") == "paper"]
    open_tickers = {str(r.get("ticker")).upper() for r in open_rows if r.get("ticker")}
    slots = max(0, int(os.getenv("VEGA_MAX_OPEN_TOTAL", "15")) - len(open_rows))
    budget = min(slots, int(os.getenv("VEGA_MAX_NEW_PER_RUN", "5")))
    if budget <= 0:
        _log("No slots free; nothing opened.")
        return 0

    # Highest edge first — the board's own ranking, so the desk validates the top of the list
    # the operator would have acted on rather than an order of its own.
    trades = sorted(trades, key=lambda t: (t.get("edge_score") or 0), reverse=True)

    opened = 0
    for t in trades:
        if opened >= budget:
            break
        tk = str(t.get("ticker") or "").upper()
        if not tk or tk in open_tickers:
            continue
        if not is_manageable(t):
            _log(f"SKIP {tk} {t.get('strategy')} — the desk cannot mark this structure, so it "
                 f"must not open it. See is_manageable.")
            continue
        gates = t.get("assessment_gates") or {}
        missing = [k for k in getattr(config, "REQUIRED_GATES", ()) if k not in gates]
        failed = [k for k, v in gates.items() if not v]
        if missing or failed:
            _log(f"SKIP {tk} — board trade is not fully gated (missing={missing} failed={failed}).")
            continue
        credit = t.get("natural_credit_per_share")
        if not isinstance(credit, (int, float)) or credit <= 0:
            _log(f"SKIP {tk} — no positive fillable credit on the board trade.")
            continue
        try:
            tid = ol.open_paper_trade(
                ticker=tk,
                short_strike=t.get("short_strike"), long_strike=t.get("long_strike"),
                expiration=t.get("expiration"),
                entry_credit_per_share=float(credit),
                dte=t.get("dte"), delta=t.get("delta"), iv_rank=t.get("iv_rank"),
                implied_pop=t.get("implied_pop"), true_pop=t.get("true_pop"),
                p_max_profit=t.get("p_max_profit"), contracts=1, fill_model="natural",
                natural_credit_per_share=float(credit),
                mid_credit_per_share=t.get("credit_per_share"),
                source="auto-board", note="auto from scan_latest.json",
                edge_score=t.get("edge_score"), vrp=t.get("vrp"),
                technical_score=t.get("composite_score") or t.get("technical_score"),
                term_slope=t.get("term_slope"), skew_steepness=t.get("skew_steepness"),
                vix_at_entry=((board.get("market_context") or {}).get("vix") or {}).get("current"),
                support_level_at_entry=t.get("nearest_support"),
                # The raw measurements behind the entry. These were threaded by the old
                # candidates opener; dropping them here would silently undo the instrumentation
                # the calibration engine depends on — the board is the desk's only source now.
                atm_iv_at_entry=t.get("atm_iv"),
                rv_at_entry=t.get("rv_30d_decimal"),
                expected_move_at_entry=(t.get("horizon") or {}).get("expected_move"),
                pop_gap_at_entry=t.get("pop_gap"),
                btc_iv_gap_pp=(t.get("btc_cross_venue") or {}).get("iv_gap_pp"),
                btc_vrp_pp=(t.get("btc_cross_venue") or {}).get("btc_vrp_pp"),
                strategy=_strategy_key(t) or "bull_put_spread",
            )
            try:
                from analysis import predictions as _pred
                _pred.record_trade_predictions({**t, "ticker": tk, "close_logic": "ravens_v1"}, tid)
            except Exception as e:
                _log(f"Prediction recording failed for {tid}: {e}")
            _log(f"AUTO-OPEN {tid} | {tk} {t.get('strategy')} "
                 f"{t.get('short_strike')}/{t.get('long_strike')} {t.get('expiration')} "
                 f"credit={float(credit):.2f} (natural) edge={t.get('edge_score')}")
            open_tickers.add(tk)
            opened += 1
        except Exception as exc:
            _log(f"Failed to open board trade {tk}: {exc}")
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
            # BLOCKING news at close time is not wired. Left explicitly off rather than as a
            # silently-empty read: check_wolves tests for it, so an unfireable wolf that looks
            # wired is worse than one that is visibly disabled.
            "news_sentiment": None,
            # An earnings date that has moved INTO the remaining window is a structural change
            # to the risk being held, and it is the wolf the entry gate cannot cover — a print
            # can be scheduled after the position is open. This read used to be a hardcoded {},
            # so the condition could never fire: earnings risk was unmanaged at BOTH ends,
            # since the entry gate was simultaneously passing unknown dates open.
            "earnings_check": _earnings_check(position, exp),
            # record_stress_snapshot has always written a "vix_at_stress" field and this dict
            # has never carried a vix, so that field is None on every snapshot in the ledger.
            # Memory's whole job is telling one stressed position from another, and the market
            # regime it happened in is the coarsest distinction there is. Advisory: a VIX read
            # failing must not stop the ravens from evaluating.
            "vix": _current_vix(),
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
        # The ravens' hard floor, a different rule from the legacy 1.5x — recorded so the two
        # cohorts stay separable. A thesis violation is not a stop and carries no multiplier.
        wolf_mult = (float(getattr(config, "WOLF_STOP_MULTIPLIER", 3.0))
                     if rec == "WOLF_CLOSE" else None)
        if ol.set_close(r.get("id"), float(mark), outcome, reason,
                        effective_stop_multiplier=wolf_mult):
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
        # BOTH sides of the chain. get_options_chain returns PUTS ONLY, so a bear call's
        # strikes were never found in the index below: the position would be opened and then
        # skipped on every mark forever — never re-marked, never closed, never learned from.
        # That is worse than refusing to open it, because it looks like it is being managed.
        try:
            chain = list(fetcher.get_options_chain(ticker, 0, 200))  # wide window
        except Exception as exc:
            _log(f"Reprice: chain fetch failed for {ticker}: {exc}")
            continue
        if any(_is_call_side(r) for r in positions):
            try:
                chain += list(fetcher.get_call_options_chain(ticker, 0, 200))
            except Exception as exc:
                _log(f"Reprice: call chain fetch failed for {ticker}: {exc}")
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


def _record_btc_forecast() -> Optional[str]:
    """Write today's BTC directional claim into the shared prediction ledger.

    Runs on BOTH cycle paths, including mark-only, because the forecast is about the asset and
    not about the book — a day with no new positions is still a day the claim should be on
    record. Advisory in the strongest sense: it opens nothing, closes nothing, and touches no
    equity position. A crypto endpoint being down costs one sample, never a cycle.
    """
    if not getattr(config, "BTC_FORECAST_ENABLED", True):
        return None
    try:
        from analysis import btc_forecast as bf
        fc = bf.forecast()
        pid = bf.record_daily(fc)
        if pid:
            _log(f"BTC FORECAST {pid} | {fc['expected'].upper()} "
                 f"p={fc['probability']:.2f} over {fc['horizon_days']}d "
                 f"(flat band ±{fc['flat_band_pct']:.1f}%)")
        else:
            _log(f"BTC FORECAST abstained: {fc.get('reason', 'no reason given')}")
        return pid
    except Exception as e:
        _log(f"BTC forecast failed: {e}")
        return None


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
                 f"deferred={stats.get('deferred', 0)} "
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
            _record_btc_forecast()
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
        # THE BOARD IS THE PRODUCT AND THIS DESK VALIDATES IT. The engine builds the list the
        # operator reviews; the desk opens from that same list. It used to open from the
        # vega_candidates snapshot while the operator read main.py's artifact — different
        # search, different strikes, different strategies — so the learning history graded
        # trades nobody was ever shown.
        rc = _run([sys.executable, "main.py"])
        if rc != 0:
            _log("main.py failed; skipping open/grade this cycle.")
            return rc

        # The fast scan still runs, DEMOTED to what it is uniquely good at: enumerating widely
        # and recording full gate results for every candidate, including the ones that failed.
        # That is the only source analysis/counterfactuals.py can price a gate from — main.py's
        # own enumeration records drop reasons but not gate results. It no longer opens
        # anything, so it is a measuring instrument rather than a second opinion.
        if _run([sys.executable, "vega_candidates.py", "--no-open", "--no-html"]) != 0:
            _log("vega_candidates.py failed — counterfactual record will have a gap this cycle.")

        opened = _auto_open_from_board()
        marked, closed = _reprice_and_close_open()
        _record_btc_forecast()
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
