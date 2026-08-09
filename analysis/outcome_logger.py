"""
analysis/outcome_logger.py — Gate 1 outcome logging.

Purpose (from the 2026-07-06 audit): before trusting any qualifier, capture ground truth —
the MODELED credit at scan time vs. the ACTUAL fill you get, and the realized outcome. Over
~30 trades this measures three things the audit flagged:

  1. Model-vs-market credit gap   (data quality — yfinance is delayed/unofficial)
  2. Hit rate vs. modeled p_profit (is the probability calibrated?)
  3. Realized edge vs. modeled edge (does the "edge" survive real fills?)

Storage: one JSON object per line in logs/vega_outcomes.jsonl (append-only, hand-editable).
Lifecycle of a record:  modeled  →  filled (you enter real credit)  →  closed (you enter exit).

This module is dependency-free (stdlib only) and never raises into the scan path.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTCOMES_FILE = BASE_DIR / "logs" / "vega_outcomes.jsonl"


def _trade_id(scan_ts: str, ticker: str, short_strike, long_strike, expiration) -> str:
    """Stable, human-readable id: TICKER-SHORT/LONG-EXP-SCANDATE."""
    date = (scan_ts or "")[:10]
    return f"{ticker}-{short_strike}/{long_strike}-{expiration}-{date}"


def _same_strike(a, b) -> bool:
    """Strikes arrive as float or str depending on the caller (form post vs candidate json)."""
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return a == b


def _read_all() -> List[Dict]:
    if not OUTCOMES_FILE.exists():
        return []
    rows = []
    for line in OUTCOMES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_all(rows: List[Dict]) -> None:
    OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write — serialize to a temp file then os.replace so an interrupted write can
    # never truncate the ledger (the failure mode that corrupted scan_log.json).
    tmp = OUTCOMES_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, OUTCOMES_FILE)


def _round_trip_cost_per_contract() -> float:
    """Robinhood round-trip commission for a vertical: 2 legs open + 2 legs close."""
    per_leg = float(getattr(_config, "COMMISSION_PER_CONTRACT_PER_LEG", 0.54)) if _config else 0.54
    legs = int(getattr(_config, "LEGS_PER_SPREAD", 2)) if _config else 2
    return round(per_leg * legs * 2, 2)


def open_paper_trade(ticker: str, short_strike, long_strike, expiration,
                     entry_credit_per_share: float, dte=None, delta=None,
                     iv_rank=None, implied_pop=None, true_pop=None, p_max_profit=None,
                     contracts: int = 1, fill_model: str = "natural",
                     natural_credit_per_share=None, mid_credit_per_share=None,
                     source: str = "manual", note: Optional[str] = None,
                     theta=None, allow_duplicate: bool = False,
                     edge_score=None, vrp=None, technical_score=None,
                     term_slope=None, skew_steepness=None, vix_at_entry=None,
                     atm_iv_at_entry=None, rv_at_entry=None,
                     expected_move_at_entry=None, pop_gap_at_entry=None,
                     btc_iv_gap_pp=None, btc_vrp_pp=None) -> str:
    """
    Open a PAPER position from a real candidate (or manual entry). Records the entry credit you
    would realistically collect; net P/L on close subtracts Robinhood round-trip commissions.
    Returns the trade id. Reuses the same ledger + set_close/report as real Gate-1 trades.
    """
    rows = _read_all()
    existing_ids = {r.get("id") for r in rows}
    ts = datetime.now().isoformat()
    tid = _trade_id(ts, ticker, short_strike, long_strike, expiration)

    # Re-opening a spread that is ALREADY open is a double-write, not a new position.
    # This used to fall straight through to the uniquifier below, which minted a -HHMMSS id and
    # let the duplicate live as an independent trade: it marked, stopped out and double-counted
    # its loss. That is exactly what happened to META 570/565 exp 2026-08-07, logged twice 24s
    # apart on 2026-07-13 and closed twice at -$256.16. Reject it unless the caller is explicitly
    # adding to an existing position.
    if not allow_duplicate:
        dup = next((r for r in rows
                    if r.get("status") == "open"
                    and r.get("ticker") == ticker
                    and r.get("expiration") == expiration
                    and _same_strike(r.get("short_strike"), short_strike)
                    and _same_strike(r.get("long_strike"), long_strike)), None)
        if dup:
            raise ValueError(
                f"{ticker} {short_strike}/{long_strike} {expiration} is already open as "
                f"{dup.get('id')} — refusing to log a duplicate. "
                f"Pass allow_duplicate=True to deliberately add to the position."
            )

    # Same-day re-entry AFTER the previous one closed is legitimate; keep those ids unique.
    if tid in existing_ids:
        tid = f"{tid}-{datetime.now().strftime('%H%M%S')}"

    width = None
    try:
        width = round(float(short_strike) - float(long_strike), 2)
    except Exception:
        pass
    credit = round(float(entry_credit_per_share), 2)
    if implied_pop is None and delta is not None:
        try:
            implied_pop = round(1 - abs(float(delta)), 3)
        except Exception:
            implied_pop = None

    rows.append({
        "id": tid,
        "status": "open",                 # open (paper) -> closed
        "mode": "paper",
        "source": source,
        "note": note,
        "logged_at": ts,
        "opened_at": ts,
        "scan_ts": ts,
        "session_type": "paper",
        "ticker": ticker,
        "strategy": "bull_put_spread",
        "short_strike": short_strike,
        "long_strike": long_strike,
        "expiration": expiration,
        "dte": dte,
        "spread_width": width,
        "contracts": int(contracts),
        # Paper entry: modeled == actual (you chose the fill), so the credit-gap is zero and
        # the calibration/P&L math flows through set_close unchanged.
        "modeled_credit_per_share": credit,
        "modeled_credit_usd": round(credit * 100, 2),
        "actual_fill_credit": credit,
        # FILL COHORT. "mid" = entered at the mid (every trade before 2026-08-02) — an
        # unachievable price that overstated collectable credit by ~75%. "natural" = entered at
        # bid/ask, what a real fill collects. Marks and closes MUST use the same basis the position
        # was entered on, or a mid-entry position exited at natural double-counts the pessimism and
        # is neither an honest record nor a fair benchmark. Kept, not deleted: the mid cohort is
        # still a valid record of what the selection logic picked — it just cannot be compared
        # against, or pooled with, natural-fill results.
        "fill_model": fill_model,
        # Both sides of the fill on record, not just the one booked. `actual_fill_credit` is what
        # the trade is scored on; these two make the gap measurable per trade so the 75.5% figure
        # from the 2026-08-02 audit can be tracked over time instead of re-derived from snapshots.
        # NULL on legacy rows — not recoverable after the fact.
        "natural_credit_per_share": natural_credit_per_share,
        "mid_credit_per_share": mid_credit_per_share,
        "estimated_round_trip_cost_per_contract": _round_trip_cost_per_contract(),
        "delta": delta,
        "short_theta": theta,
        "iv_rank": iv_rank,
        # true_pop = drift-removed (calibrated) POP from the engine edge calculator.
        # implied_pop = delta-derived market-implied POP (1 - abs(delta)).
        # modeled_pop stores true_pop when available; falls back to implied_pop so
        # the CLV tracker and calibration grader always have a usable POP field.
        "true_pop": true_pop,
        "modeled_pop": true_pop if true_pop is not None else implied_pop,
        "implied_pop": implied_pop,
        # Which probability modeled_pop actually came from. The auto-open path reads
        # output/candidates/*.json, whose schema has NO true_pop field (it is engine-only,
        # computed in main.py via edge_calculator.calculate_true_pop and absent from the
        # vega_candidates fast scan). So auto-paper trades grade against a delta-derived
        # proxy, not the calibrated signal. Record that explicitly rather than letting the
        # fallback stay invisible — calibration stats must be able to segregate the two.
        "pop_source": "true_pop" if true_pop is not None else "implied_pop",
        # P(price > short strike) — the apples-to-apples counterpart to implied_pop (1 - |delta|),
        # which is also measured at the short strike. true_pop is measured at BREAKEVEN, so
        # comparing it against implied_pop overstates edge. Edge scoring must use this field.
        "p_max_profit": p_max_profit,
        # ── Score components at entry ──
        # The calibration engine's whole job is correlating what the engine BELIEVED at entry
        # against what actually happened. None of these were recorded, so on 58 closed trades
        # not one score component could be tested — the feedback loop had no inputs. They are
        # NULL on every trade logged before 2026-08-05 and are not recoverable after the fact,
        # so calibration counts only trades that carry them.
        # Which close logic governed this trade. The 45 stop-outs in this ledger were killed
        # by a 1.5x credit stop marked natural-in/natural-out, which fired at t=0 on bid-ask
        # spread alone — a mechanism that no longer exists. Pooling those with trades closed
        # by thesis judgment would make the record neither an honest history nor a fair
        # benchmark, exactly as the fill_model cohorts already are. Absent = legacy.
        "close_logic": ("ravens_v1" if getattr(_config, "RAVENS_FRAMEWORK_ENABLED", False)
                        else "credit_stop"),
        "close_decision_basis": getattr(_config, "CLOSE_DECISION_MARK_BASIS", "natural"),
        "edge_score": edge_score,
        "vrp": vrp,
        "technical_score": technical_score,
        "term_slope": term_slope,
        "skew_steepness": skew_steepness,
        "vix_at_entry": vix_at_entry,
        # ── Raw entry state ──
        # The score components above are the engine's CONCLUSIONS. These four are the
        # measurements those conclusions were drawn from, and without them a calibration run
        # can only ask "was the score right?" — never "was the score wrong because the inputs
        # were wrong, or because the weighting was?". `vrp` already stores the IV-RV spread;
        # storing its two halves separately is what makes the spread decomposable after the
        # fact. NULL on every trade before 2026-08-08 and not recoverable.
        "atm_iv_at_entry": atm_iv_at_entry,
        "rv_at_entry": rv_at_entry,
        # 1 sigma over the holding period, in dollars: spot * iv * sqrt(dte/365).
        # Calendar days, matching analysis.horizon.expected_move — the same unit the strike
        # distance and the level cushions are already measured in, so they stay comparable.
        "expected_move_at_entry": expected_move_at_entry,
        # true_pop - implied_pop. THE model-edge claim: how much probability the engine thinks
        # it sees that the market's delta does not. Every trade asserts it and nothing recorded
        # it, so the one number VEGA most needs graded was the one number never written down.
        # Negative means the engine is LESS confident than the market — worth knowing too.
        "pop_gap_at_entry": pop_gap_at_entry,
        # ── Cross-venue volatility (BTC trackers only; None everywhere else) ──
        # DVOL minus this ETF's ATM IV, in vol points. The RAW measurement is what is stored,
        # never the label: BTC_IV_GAP_WIDE_PP is a reasoned guess and has graded nothing yet, so
        # persisting the band would freeze a guess into the record. Store the number, let the
        # calibration engine set the band once enough IBIT trades carrying it have closed.
        "btc_iv_gap_pp": btc_iv_gap_pp,
        "btc_vrp_pp": btc_vrp_pp,
        "max_loss_per_contract": (round((width - credit) * 100, 2) if (width and credit < width) else None),
        # Live mark (updated on each rescan while open) → unrealized P/L
        "current_mark": None,
        "unrealized_gross": None,
        "unrealized_net": None,
        "marked_at": None,
        # Ground truth (filled on close)
        "exit_price": None,
        "realized_gross_pl_per_contract": None,
        "realized_net_pl_per_contract": None,
        "realized_pl_per_contract": None,
        "outcome": None,
        "exit_reason": None,
        "filled_at": ts,
        "closed_at": None,
    })
    _write_all(rows)
    logger.info(f"[outcomes] Opened paper trade {tid}")
    return tid


LEGACY_CLOSE_LOGIC = "credit_stop_1.5x_natural"


def close_cohort(record: Dict) -> str:
    """Which close-logic regime governed a trade.

    Read through this rather than off the raw field: history is deliberately NOT rewritten,
    so trades predating the marker carry no field and are reported as the legacy cohort.
    Mutating closed records to add metadata would edit a trading history that should stay
    exactly as it was written.
    """
    return record.get("close_logic") or LEGACY_CLOSE_LOGIC


def _append_to_list_field(trade_id: str, field: str, entry: Dict) -> bool:
    """Append to a list field on an OPEN trade, creating it if absent."""
    rows = _read_all()
    for r in rows:
        if r.get("id") == trade_id and r.get("status") == "open":
            r.setdefault(field, []).append(entry)
            _write_all(rows)
            return True
    return False


def append_stress_snapshot(trade_id: str, snapshot: Dict) -> bool:
    """What a position looked like the moment it came under pressure.

    Muninn needs this and nothing has ever recorded it — which is why Memory starts blind and
    cannot be backfilled. A close record knows a trade was stopped; it does not know what the
    chart looked like when it happened. Appended, never overwritten: the first time a position
    is stressed is a different fact from the fifth.
    """
    return _append_to_list_field(trade_id, "stress_snapshots", snapshot)


def append_raven_alert(trade_id: str, alert: Dict) -> bool:
    """A HOLD_TENSION or MUNINN_BLIND divergence, kept for the cockpit and for audit."""
    return _append_to_list_field(trade_id, "raven_alerts", alert)


def set_mark(trade_id: str, current_mark_per_share: float) -> bool:
    """Update the live spread mark for an OPEN paper trade → unrealized P/L (per contract).
    unrealized gross = (entry_credit - current_mark) * 100; net subtracts round-trip fees."""
    rows = _read_all()
    for r in rows:
        if r.get("id") == trade_id and r.get("status") == "open":
            entry = r.get("actual_fill_credit")
            if entry is None:
                return False
            mark = round(float(current_mark_per_share), 2)
            gross = round((float(entry) - mark) * 100, 2)
            net = round(gross - float(r.get("estimated_round_trip_cost_per_contract") or 0.0), 2)
            r["current_mark"] = mark
            r["unrealized_gross"] = gross
            r["unrealized_net"] = net
            r["marked_at"] = datetime.now().isoformat()
            _write_all(rows)
            return True
    return False


def record_modeled_trades(scan_ts: str, session_type: str, qualified_trades: List[Dict]) -> int:
    """
    Append one 'modeled' record per qualified trade. Idempotent: a trade whose id already
    exists (same ticker/strikes/expiration/scan-date) is skipped, so re-running a scan the
    same day won't duplicate rows. Returns the number of NEW records written.
    """
    try:
        existing = _read_all()
        existing_ids = {r.get("id") for r in existing}
        new_rows: List[Dict] = []

        for t in qualified_trades:
            tid = _trade_id(
                scan_ts, t.get("ticker"), t.get("short_strike"),
                t.get("long_strike"), t.get("expiration"),
            )
            if tid in existing_ids:
                continue
            new_rows.append({
                "id": tid,
                "status": "modeled",                     # modeled -> filled -> closed
                "logged_at": datetime.utcnow().isoformat(),
                "scan_ts": scan_ts,
                "session_type": session_type,
                "ticker": t.get("ticker"),
                "strategy": t.get("strategy"),
                "short_strike": t.get("short_strike"),
                "long_strike": t.get("long_strike"),
                "expiration": t.get("expiration"),
                "dte": t.get("dte"),
                # Modeled expectations (what the engine believed at scan time)
                "modeled_credit_per_share": t.get("credit_per_share"),
                "modeled_credit_usd": t.get("credit_usd"),
                "modeled_net_credit_per_share": t.get("net_credit_per_share"),
                "modeled_net_credit_usd": t.get("net_credit_usd"),
                "estimated_entry_cost_per_contract": t.get("estimated_entry_cost_per_contract"),
                "estimated_exit_cost_per_contract": t.get("estimated_exit_cost_per_contract"),
                "estimated_round_trip_cost_per_contract": t.get("estimated_round_trip_cost_per_contract"),
                "spread_width": t.get("spread_width") or (
                    (t.get("short_strike") or 0) - (t.get("long_strike") or 0)
                ),
                "delta": t.get("delta"),
                "iv_rank": t.get("iv_rank"),
                "vrp": t.get("vrp"),
                "edge_score": t.get("edge_score"),
                "edge_points": t.get("edge_points"),
                "p_max_profit": t.get("p_max_profit"),
                # true_pop stored separately so CLV / calibration graders have the raw field.
                "true_pop": t.get("true_pop"),
                "modeled_pop": t.get("true_pop"),   # calibrated POP is the primary model signal
                "implied_pop": t.get("implied_pop"),
                # Ground truth (filled in later by you via log_outcome.py)
                "actual_fill_credit": None,     # real credit per share you collected
                "exit_price": None,             # spread mark per share when you closed
                "realized_gross_pl_per_contract": None,
                "realized_net_pl_per_contract": None,
                "realized_pl_per_contract": None,
                "outcome": None,                # win | loss | scratch
                "exit_reason": None,
                "filled_at": None,
                "closed_at": None,
            })

        if new_rows:
            _write_all(existing + new_rows)
            logger.info(f"[outcomes] Recorded {len(new_rows)} modeled trade(s) to {OUTCOMES_FILE.name}")
        return len(new_rows)
    except Exception as e:
        logger.warning(f"[outcomes] record_modeled_trades failed (non-blocking): {e}")
        return 0


def set_fill(trade_id: str, actual_fill_credit: float) -> bool:
    """Record the actual credit per share you collected. Returns True if the id was found."""
    rows = _read_all()
    for r in rows:
        if r.get("id") == trade_id:
            r["actual_fill_credit"] = round(float(actual_fill_credit), 2)
            r["status"] = "filled"
            r["filled_at"] = datetime.utcnow().isoformat()
            _write_all(rows)
            return True
    return False


def set_close(trade_id: str, exit_price: float, outcome: str,
              reason: Optional[str] = None) -> bool:
    """
    Close a trade. exit_price = spread mark per share when you exited (what you paid to close).
    realized gross P/L per contract = (actual_fill_credit - exit_price) * 100.
    realized net P/L per contract = gross P/L - estimated round-trip costs.
    Returns True if the id was found.
    """
    # MONEY PATH — failures here silently lose realized outcomes, which is the one thing the
    # Gate-1 ledger exists to capture. Log loudly and re-raise; never swallow.
    try:
        rows = _read_all()
    except Exception:
        logger.error("[CLOSE] failed to read ledger while closing %s", trade_id, exc_info=True)
        raise

    for r in rows:
        if r.get("id") == trade_id:
            fill = r.get("actual_fill_credit")
            if fill is None:
                fill = r.get("modeled_credit_per_share") or 0.0
                logger.warning(
                    "[CLOSE] %s has no actual_fill_credit; falling back to modeled credit %s. "
                    "Realized P&L for this trade is modelled, not achieved.", trade_id, fill)
            r["exit_price"] = round(float(exit_price), 2)
            gross_pl = round((float(fill) - float(exit_price)) * 100, 2)
            est_cost = float(r.get("estimated_round_trip_cost_per_contract") or 0.0)
            net_pl = round(gross_pl - est_cost, 2)
            r["realized_gross_pl_per_contract"] = gross_pl
            r["realized_net_pl_per_contract"] = net_pl
            # Backward-compatible field now points to net P/L.
            r["realized_pl_per_contract"] = net_pl
            r["outcome"] = (outcome or "").lower()
            r["exit_reason"] = reason
            r["status"] = "closed"
            r["closed_at"] = datetime.utcnow().isoformat()
            try:
                _write_all(rows)
            except Exception:
                logger.error("[CLOSE] failed to persist close for %s (exit=%s outcome=%s)",
                             trade_id, exit_price, outcome, exc_info=True)
                raise
            logger.info("[CLOSE] %s exit=%.2f outcome=%s reason=%s gross=%.2f net=%.2f",
                        trade_id, float(exit_price), r["outcome"], reason, gross_pl, net_pl)
            return True

    logger.warning("[CLOSE] trade id not found in ledger: %s — nothing closed", trade_id)
    return False


def load_records() -> List[Dict]:
    """Public read accessor (for reporting / analysis)."""
    return _read_all()
