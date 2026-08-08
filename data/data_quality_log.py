#!/usr/bin/env python3
"""
data/data_quality_log.py — how much of the chain VEGA actually got to look at.

Every signal above this layer is a statement about an options chain, and until now nothing
recorded how much of that chain was real. The yfinance fallback routinely discards 30-45% of
records as stale or unquoted, and a skew read, a term-structure slope or an IV rank computed
over what survived is a read of the survivors — not of the market. The numbers looked exactly
as confident either way, which is the problem: an unmeasured input cannot be discounted.

This module measures it, per ticker per scan, and keeps the history so a later calibration
run can ask whether the bad predictions came from bad reasoning or from a thin chain.

Advisory by construction. Recording never raises and never blocks a scan; the FLOOR is
enforced by the caller (fetcher.get_options_chain) so this file has no opinion about trading.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_quality_log.json")

# Keep the file bounded. One scan writes ~56 rows (one per watchlist ticker) and the cycle runs
# several times a day, so an unbounded file reaches six figures of rows within a quarter and
# every cockpit render pays to parse it.
MAX_ROWS = 5000


def _now() -> str:
    return datetime.now().isoformat()


def _read_all() -> List[Dict]:
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        # A corrupt log must not take down a scan. Start a fresh list and say so.
        logger.warning("[data_quality] could not read %s (%s) — starting fresh", LOG_FILE, e)
        return []


def record(ticker: str, chain_source: str, raw_count: int, usable_count: int,
           scan_id: Optional[str] = None, note: Optional[str] = None) -> Dict:
    """Append one ticker's chain-quality reading and return it.

    `usable_count` is how many records pass the usability predicate, whether or not the source
    path actually filtered them. Measuring both sources the same way is the whole point: if the
    ratio were computed as len(after_filter)/len(before_filter) it would be pinned at 1.000 on
    the Polygon path — which does not filter — and the metric would be incapable of ever
    reporting a problem on the primary data source.
    """
    raw_count = int(raw_count or 0)
    usable_count = int(usable_count or 0)
    ratio = round(usable_count / raw_count, 4) if raw_count > 0 else 0.0
    row = {
        "timestamp": _now(),
        "scan_id": scan_id,
        "ticker": ticker,
        "chain_source": chain_source,
        "raw_count": raw_count,
        "usable_count": usable_count,
        "usable_ratio": ratio,
        "score": int(round(ratio * 100)),
    }
    if note:
        row["note"] = note
    try:
        rows = _read_all()
        rows.append(row)
        if len(rows) > MAX_ROWS:
            rows = rows[-MAX_ROWS:]
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        os.replace(tmp, LOG_FILE)          # atomic; a killed scan cannot truncate the log
    except Exception as e:                 # pragma: no cover - defensive
        logger.warning("[data_quality] could not record %s: %s", ticker, e)
    return row


def read_recent(limit: int = 200) -> List[Dict]:
    return _read_all()[-int(limit):]


def latest_scan(floor: Optional[float] = None) -> Dict:
    """Summarise the most recent scan for the status board and the cockpit tile.

    "Most recent scan" is every row sharing the newest scan_id when scans are tagged, and
    otherwise the last reading per ticker — so a run that predates scan tagging still
    summarises to something truthful rather than to nothing.
    """
    if floor is None:
        try:
            import config
            floor = float(getattr(config, "CHAIN_QUALITY_MIN_RATIO", 0.30))
        except Exception:
            floor = 0.30

    rows = _read_all()
    if not rows:
        return {"count": 0, "worst_ratio": None, "worst_ticker": None,
                "below_floor": 0, "floor": floor, "at": None, "sources": {}}

    newest_scan = rows[-1].get("scan_id")
    if newest_scan:
        scan = [r for r in rows if r.get("scan_id") == newest_scan]
    else:
        latest_per_ticker: Dict[str, Dict] = {}
        for r in rows:
            latest_per_ticker[r.get("ticker")] = r
        scan = list(latest_per_ticker.values())

    worst = min(scan, key=lambda r: r.get("usable_ratio", 0.0))
    sources: Dict[str, int] = {}
    for r in scan:
        src = r.get("chain_source") or "unknown"
        sources[src] = sources.get(src, 0) + 1
    return {
        "count": len(scan),
        "at": scan[-1].get("timestamp"),
        "scan_id": newest_scan,
        "worst_ratio": worst.get("usable_ratio"),
        "worst_ticker": worst.get("ticker"),
        "below_floor": sum(1 for r in scan if (r.get("usable_ratio") or 0) < floor),
        "floor": floor,
        "sources": sources,
    }


def band(ratio: Optional[float], floor: Optional[float] = None) -> str:
    """green / amber / red / unknown — one definition, so the status board and the cockpit
    tile can never disagree about what counts as a bad chain."""
    if ratio is None:
        return "unknown"
    if floor is None:
        try:
            import config
            floor = float(getattr(config, "CHAIN_QUALITY_MIN_RATIO", 0.30))
        except Exception:
            floor = 0.30
    good = 0.70
    try:
        import config
        good = float(getattr(config, "CHAIN_QUALITY_GOOD_RATIO", 0.70))
    except Exception:
        pass
    if ratio < floor:
        return "red"
    return "green" if ratio >= good else "amber"
