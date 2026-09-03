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

# The legacy whole-file JSON array. Retained only so an existing file can be migrated once.
LEGACY_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data_quality_log.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_quality_log.jsonl")

# ── RETENTION, sized against the longest LOOKBACK that might query this, not against disk ────
#
# The old sizing was MAX_ROWS = 5000 with the reasoning "one scan writes ~56 rows and the cycle
# runs several times a day", implying ~400/day and a file reaching six figures "within a
# quarter". Measured 2026-09-03: 1,391 rows/TRADING DAY -- 3.5x that -- because a cycle spawns
# several processes and each does its own put-chain, call-chain and mark-loop fetches. So the
# window was really 3.6 TRADING DAYS, and the file already covered only 08-31..09-03.
#
# That cost a real evidence path. outcome_logger.entry_vendor_basis pointed at this file as the
# way to recover provenance for already-opened trades "from 2026-08-27 onward"; by the time
# anyone needed it, 08-27..08-30 had been eaten. It cost nothing that time only because the
# whole open book predates 08-27 -- which will not hold for the next finding.
#
# Provenance is the class of record whose value is RETROSPECTIVE: you do not know which join
# you will need until something is found wrong weeks later. So the window is sized against a
# trade's full life plus the lag before anyone analyses it -- MAX_DTE is 45 days -- rather than
# against how big the file gets. This is the third rolling-window instrument here to eat its own
# evidence (_KEEP_CANDIDATE_FILES at 20 against a 10-day horizon, snapshot pruning driving
# first-sighting drift, now this), and the general rule is the one being applied here.
MAX_AGE_DAYS = 120

# Pathological-volume backstop ONLY. Age is the real policy; this stops a runaway writer from
# filling the disk between compactions. ~120 days at the measured rate is ~167k rows, so this
# is roughly 2.4x headroom and should never bind in normal operation.
MAX_ROWS = 400_000

# How many trailing lines the bounded readers pull. Every live consumer -- latest_scan for the
# cockpit tile and the status board, read_recent for ticker_profile -- wants the TAIL, so they
# never pay for the retention window. One cycle writes a few hundred rows, so this covers many
# cycles' worth of any time-window question.
TAIL_LINES = 5000


def _now() -> str:
    return datetime.now().isoformat()


def _parse_lines(lines) -> List[Dict]:
    """JSONL rows, skipping unreadable lines rather than losing the file to one of them.

    The whole reason for the format change: a corrupt line in an array file poisons every row
    after it, which is how this log was destroyed five times between 2026-08-13 and 2026-08-25.
    One bad line in JSONL costs one reading.
    """
    out, bad = [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except Exception:
            bad += 1
    if bad:
        logger.warning("[data_quality] skipped %d unreadable line(s); the rest were kept", bad)
    return out


def _migrate_legacy_if_needed() -> None:
    """Convert the old whole-file JSON array to JSONL, once. Never destroys the original."""
    if os.path.exists(LOG_FILE) or not os.path.exists(LEGACY_LOG_FILE):
        return
    try:
        with open(LEGACY_LOG_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            rows = json.loads(text)
            if not isinstance(rows, list):
                rows = []
        except Exception as e:
            rows = _salvage(text, e)
        tmp = "%s.%d.tmp" % (LOG_FILE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        _replace_with_retry(tmp, LOG_FILE)
        # The legacy file is RENAMED, not deleted: it is the only copy of these readings and a
        # failed migration must be recoverable by hand.
        os.replace(LEGACY_LOG_FILE, LEGACY_LOG_FILE + ".migrated")
        logger.info("[data_quality] migrated %d row(s) from the legacy array file to JSONL",
                    len(rows))
    except Exception as e:                     # pragma: no cover - defensive
        logger.warning("[data_quality] legacy migration failed (%s); starting a fresh log", e)


def _read_tail(limit: int) -> List[Dict]:
    """The last `limit` rows, without reading the whole file.

    Every live consumer wants the tail, so this is what keeps a 120-day window free for readers.
    Seeks backward in chunks until enough newlines have been seen.
    """
    _migrate_legacy_if_needed()
    limit = max(1, int(limit))
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        return []
    if not size:
        return []
    chunk, data, pos = 65536, b"", size
    try:
        with open(LOG_FILE, "rb") as f:
            while pos > 0 and data.count(b"\n") <= limit:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except Exception as e:                     # pragma: no cover - defensive
        logger.warning("[data_quality] tail read failed (%s)", e)
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return _parse_lines(lines[-limit:])


def _unreadable_lines() -> List[str]:
    """The raw lines _parse_lines would skip. Used by compaction to preserve them first."""
    out = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if not t:
                    continue
                try:
                    if not isinstance(json.loads(t), dict):
                        out.append(t)
                except Exception:
                    out.append(t)
    except FileNotFoundError:
        return []
    except Exception:                          # pragma: no cover - defensive
        return []
    return out


def _read_all() -> List[Dict]:
    """EVERY row. Used by compaction and by callers that genuinely need the history.

    Live paths should use _read_tail: at a 120-day window this file is large by design, and
    reading it wholesale on a cockpit render is the cost the old MAX_ROWS was protecting
    against. The protection now comes from bounded readers rather than from throwing evidence
    away.
    """
    _migrate_legacy_if_needed()
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return _parse_lines(f)
    except FileNotFoundError:
        return []
    except Exception as e:                     # pragma: no cover - defensive
        logger.warning("[data_quality] could not open %s (%s) — reading as empty", LOG_FILE, e)
        return []


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
    """Append ONE line. The caller MUST still hold _exclusive().

    This was a full read-modify-write: read every row, append, truncate, json.dump the entire
    list with indent=2 to a temp file, atomically replace -- per row written. At the measured
    1,391 rows/trading day across several concurrent cycle processes, that serialised the whole
    cycle behind a lock held for the duration of a whole-file rewrite, and it grew with the
    file. It is why the retention window could not simply be raised: a 120-day window under the
    old writer would have meant rewriting ~40MB on every single append.

    THE LOCK STAYS. Concurrent appends from multiple processes are not reliably atomic on
    Windows even for small writes, and this log has already been destroyed five times by
    unserialised writers.
    """
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def compact(max_age_days: Optional[int] = None, max_rows: Optional[int] = None) -> Dict:
    """Drop readings older than the retention window. Rewrites the file, so run it OFF the hot path.

    Called from the end-of-day --mark-only run, deliberately not intraday: a whole-file rewrite
    inside a cycle that has ~28 minutes of headroom against PT45M is exactly the kind of thing
    that lands inside the budget once and then gets blamed on something else.

    Age is the policy; max_rows is a pathological-volume backstop applied after it.
    """
    max_age_days = int(MAX_AGE_DAYS if max_age_days is None else max_age_days)
    max_rows = int(MAX_ROWS if max_rows is None else max_rows)
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    try:
        with _exclusive():
            # QUARANTINE BEFORE REWRITING. _parse_lines skips a line it cannot read, which is
            # right for a READ -- one bad line costs one reading instead of the file. But
            # compaction is the one place that REWRITES, so a skipped line would be silently
            # destroyed here rather than merely ignored. The array format quarantined
            # unreadable bytes to a file (see _salvage); losing that guarantee in the format
            # change would be trading a loud failure for a quiet one, which is the exact defect
            # this whole session has been chasing.
            damaged = _unreadable_lines()
            if damaged:
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
                keep = "%s.damaged-%s" % (LOG_FILE, stamp)
                try:
                    with open(keep, "w", encoding="utf-8") as f:
                        f.write("\n".join(damaged))
                    logger.error("[data_quality] %d unreadable line(s) will NOT survive this "
                                 "compaction; copy saved to %s", len(damaged), keep)
                except Exception:              # pragma: no cover - defensive
                    logger.error("[data_quality] %d unreadable line(s) could not be preserved",
                                 len(damaged))

            rows = _read_all()
            before = len(rows)
            kept = [r for r in rows if str(r.get("timestamp") or "") >= cutoff]
            dropped_age = before - len(kept)
            if len(kept) > max_rows:
                # `kept[-0:]` is `kept[0:]` -- the WHOLE list, not an empty one. Without this
                # guard max_rows=0 silently keeps everything while reporting nothing dropped,
                # which is the worst of both: no trim and no complaint.
                kept = kept[-max_rows:] if max_rows > 0 else []
            dropped_cap = before - dropped_age - len(kept)
            if dropped_age or dropped_cap:
                tmp = "%s.%d.tmp" % (LOG_FILE, os.getpid())
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        for r in kept:
                            f.write(json.dumps(r) + "\n")
                    _replace_with_retry(tmp, LOG_FILE)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:              # pragma: no cover - defensive
                            pass
            out = {"before": before, "after": len(kept),
                   "dropped_age": dropped_age, "dropped_cap": dropped_cap,
                   "max_age_days": max_age_days}
            if dropped_age or dropped_cap:
                logger.info("[data_quality] compacted %d -> %d rows (%d aged out, %d over cap)",
                            before, len(kept), dropped_age, dropped_cap)
            return out
    except Exception as e:                     # pragma: no cover - defensive
        logger.warning("[data_quality] compaction failed (%s)", e)
        return {"error": str(e)}


def read_recent(limit: int = 200) -> List[Dict]:
    return _read_tail(limit)


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

    # Bounded tail, not the whole file: this runs on every cockpit render and every status
    # board, and the question it asks is about the NEWEST time window, which is always at the
    # end. TAIL_LINES covers many cycles' worth of readings.
    rows = _read_tail(TAIL_LINES)
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
