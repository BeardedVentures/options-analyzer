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

import contextlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
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
            text = f.read()
    except FileNotFoundError:
        return []
    except Exception as e:                     # pragma: no cover - defensive
        logger.warning("[data_quality] could not open %s (%s) — reading as empty", LOG_FILE, e)
        return []

    try:
        rows = json.loads(text)
        return rows if isinstance(rows, list) else []
    except Exception as e:
        return _salvage(text, e)


def _salvage(text: str, err: Exception) -> List[Dict]:
    """Recover what a corrupt log still holds, and never silently discard the rest.

    Returning [] here — which this did until 2026-08-25 — is not a neutral fallback. The
    caller appends its new rows to whatever comes back and writes the result, so an unreadable
    file was rewritten as a handful of rows and the whole history went with it. That fired at
    least five times between 2026-08-13 and 2026-08-25; the last one took 886 KB of per-ticker
    chain readings down to 10 KB, unrecoverably, because this file is gitignored and nothing
    backs it up.

    The corruption is always the same shape — "Extra data": a complete JSON array with the tail
    of a longer document stuck to it, produced by two processes writing the same fixed temp
    path. The leading array is therefore intact and raw_decode gets it back. Anything that
    cannot be decoded is moved aside instead of overwritten, so a later run can still inspect
    it rather than find it gone.
    """
    try:
        rows, end = json.JSONDecoder().raw_decode(text.lstrip())
        if isinstance(rows, list):
            logger.warning("[data_quality] %s was corrupt (%s) — salvaged %d rows and dropped "
                           "%d trailing bytes", LOG_FILE, err, len(rows), max(0, len(text) - end))
            return rows
    except Exception:
        pass

    quarantine = "%s.corrupt-%s" % (LOG_FILE, datetime.now().strftime("%Y%m%dT%H%M%S"))
    try:
        os.replace(LOG_FILE, quarantine)
        logger.error("[data_quality] %s is unreadable (%s) and could not be salvaged — moved to "
                     "%s and starting fresh", LOG_FILE, err, quarantine)
    except Exception as e:                     # pragma: no cover - defensive
        logger.error("[data_quality] %s is unreadable (%s), could not be salvaged, and could not "
                     "be quarantined (%s) — starting fresh", LOG_FILE, err, e)
    return []


LOCK_STALE_SECONDS = 30


@contextlib.contextmanager
def _exclusive(timeout: float = 10.0):
    """Serialise the read-modify-write across processes.

    Per-process temp paths stopped the CORRUPTION but not the LOSSES. On Windows os.replace
    fails outright while another process holds the destination open, even for reading, so
    under real contention a retry loop still dropped readings: a five-writer stress run on
    2026-08-25 lost roughly a fifth of them to "[WinError 5] Access is denied" after five
    retries each. Read-modify-write cannot be made safe by retrying, only by admitting one
    writer at a time.

    O_CREAT|O_EXCL is the portable mutex. A crashed holder must not wedge instrumentation
    forever, so a lock older than LOCK_STALE_SECONDS is broken rather than waited on.
    """
    lock = "%s.lock" % LOG_FILE
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > LOCK_STALE_SECONDS:
                    os.unlink(lock)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError("chain-quality log locked for over %.0fs" % timeout)
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:                              # pragma: no cover - defensive
            pass
        try:
            os.unlink(lock)
        except OSError:                              # pragma: no cover - defensive
            pass


def _replace_with_retry(src: str, dst: str, attempts: int = 5) -> None:
    """os.replace, retried — on Windows it fails outright if any other process holds the
    destination open, even for reading. A cockpit render during a scan is enough, and the log
    carried a steady trickle of dropped rows ("[WinError 32] ... used by another process")
    from exactly that. Retrying costs a few hundred milliseconds in the rare collision and
    turns a lost reading into a recorded one.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


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
        with _exclusive():
            _append_locked(row)
    except Exception as e:                 # pragma: no cover - defensive
        logger.warning("[data_quality] could not record %s: %s", ticker, e)
    return row


def _append_locked(row: Dict) -> None:
    """The read-modify-write itself. The caller MUST hold _exclusive()."""
    rows = _read_all()
    rows.append(row)
    if len(rows) > MAX_ROWS:
        rows = rows[-MAX_ROWS:]
    # The temp path is unique per process as well as lock-guarded. Belt and braces on purpose:
    # a shared ".tmp" is what corrupted this log five times between 2026-08-13 and 2026-08-25,
    # and a lock only helps the writers that take it. Anything that ever writes here without
    # one still cannot clobber another process's temp file.
    tmp = "%s.%d.tmp" % (LOG_FILE, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        _replace_with_retry(tmp, LOG_FILE)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:                          # pragma: no cover - defensive
                pass


def read_recent(limit: int = 200) -> List[Dict]:
    return _read_all()[-int(limit):]


def latest_scan(floor: Optional[float] = None, window_minutes: Optional[float] = None) -> Dict:
    """Summarise the most recent CYCLE for the status board and the cockpit tile.

    "Most recent scan" used to mean every row sharing the newest scan_id. But scan_id is fixed
    per PROCESS (fetcher._SCAN_ID), and one cycle runs several: the 56-ticker engine scan, then
    vega_candidates, then a mark loop that only touches the ~13 tickers already holding open
    positions. The mark loop writes last, so the tile described the mark loop — which reads
    healthy by construction, since those chains were quotable enough to open on. On 2026-08-25
    it showed "below the 50% floor: 0 tickers skipped" in green while that same cycle had
    skipped ten tickers outright. The number was true of the batch and false of the cycle, and
    the tile is read as a statement about the cycle.

    So the unit here is a time window, not a batch: every reading within `window_minutes` of the
    newest one, which is the last time VEGA looked at the market. Within it a ticker is
    represented by its WORST reading, because the question the floor asks is whether that
    ticker was skipped, not what it averaged.
    """
    if floor is None:
        try:
            import config
            floor = float(getattr(config, "CHAIN_QUALITY_MIN_RATIO", 0.30))
        except Exception:
            floor = 0.30
    if window_minutes is None:
        try:
            import config
            window_minutes = float(getattr(config, "CHAIN_QUALITY_SCAN_WINDOW_MIN", 30))
        except Exception:
            window_minutes = 30.0

    rows = _read_all()
    empty = {"count": 0, "worst_ratio": None, "worst_ticker": None, "below_floor": 0,
             "floor": floor, "at": None, "sources": {}, "scan_id": None, "scan_ids": []}
    if not rows:
        return empty

    def _ts(r):
        try:
            return datetime.fromisoformat(str(r.get("timestamp")))
        except (TypeError, ValueError):
            return None

    stamped = [(r, _ts(r)) for r in rows]
    newest = max((t for _, t in stamped if t is not None), default=None)
    if newest is None:
        # Nothing carries a readable timestamp; fall back to the old batch semantics rather
        # than reporting nothing.
        window = [r for r in rows if r.get("scan_id") == rows[-1].get("scan_id")]
    else:
        cutoff = newest - timedelta(minutes=window_minutes)
        window = [r for r, t in stamped if t is not None and t >= cutoff]
    if not window:
        return empty

    worst_per_ticker: Dict[str, Dict] = {}
    for r in window:
        tk = r.get("ticker")
        cur = worst_per_ticker.get(tk)
        if cur is None or (r.get("usable_ratio") or 0.0) < (cur.get("usable_ratio") or 0.0):
            worst_per_ticker[tk] = r
    scan = list(worst_per_ticker.values())

    worst = min(scan, key=lambda r: r.get("usable_ratio") or 0.0)
    sources: Dict[str, int] = {}
    for r in scan:
        src = r.get("chain_source") or "unknown"
        sources[src] = sources.get(src, 0) + 1
    scan_ids = sorted({r.get("scan_id") for r in window if r.get("scan_id")})
    return {
        "count": len(scan),
        "at": max(r.get("timestamp") for r in window),
        "scan_id": scan_ids[-1] if scan_ids else None,
        "scan_ids": scan_ids,
        "window_minutes": window_minutes,
        "worst_ratio": worst.get("usable_ratio"),
        "worst_ticker": worst.get("ticker"),
        "below_floor": sum(1 for r in scan if (r.get("usable_ratio") or 0) < floor),
        "below_floor_tickers": sorted(r.get("ticker") for r in scan
                                      if (r.get("usable_ratio") or 0) < floor),
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
