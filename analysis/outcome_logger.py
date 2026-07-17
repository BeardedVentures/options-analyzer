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
                     iv_rank=None, implied_pop=None, contracts: int = 1,
                     source: str = "manual", note: Optional[str] = None,
                     theta=None) -> str:
    """
    Open a PAPER position from a real candidate (or manual entry). Records the entry credit you
    would realistically collect; net P/L on close subtracts Robinhood round-trip commissions.
    Returns the trade id. Reuses the same ledger + set_close/report as real Gate-1 trades.
    """
    rows = _read_all()
    existing_ids = {r.get("id") for r in rows}
    ts = datetime.now().isoformat()
    tid = _trade_id(ts, ticker, short_strike, long_strike, expiration)
    if tid in existing_ids:  # ensure uniqueness for repeat same-day paper entries
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
        "estimated_round_trip_cost_per_contract": _round_trip_cost_per_contract(),
        "delta": delta,
        "short_theta": theta,
        "iv_rank": iv_rank,
        "modeled_pop": implied_pop,
        "implied_pop": implied_pop,
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
                "modeled_pop": t.get("true_pop"),
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
    rows = _read_all()
    for r in rows:
        if r.get("id") == trade_id:
            fill = r.get("actual_fill_credit")
            if fill is None:
                fill = r.get("modeled_credit_per_share") or 0.0
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
            _write_all(rows)
            return True
    return False


def load_records() -> List[Dict]:
    """Public read accessor (for reporting / analysis)."""
    return _read_all()
