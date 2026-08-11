#!/usr/bin/env python3
"""vol_indices.py — published volatility indices, for reading an ETF's IV against.

Deribit's DVOL is the crypto half of the cross-venue read (data/crypto.py). This is the
equity/commodity half: indices a venue computes and publishes, which is the same trick — one
number, no chain fetch, no interpolation, no quality filter.

EVERY READ IS DATED, AND A STALE READ IS NOT RETURNED.

That rule is the entire reason this module exists rather than a two-line yfinance call. The
2026-08-11 survey found Yahoo still serving ^MOVE, HTTP 200, a plausible 75.46 — last updated
2026-07-17, twenty-five days earlier, while ^GVZ was current to the day. A cross-venue gap
computed from that number would have compared today's TLT IV against July's MOVE and reported
the difference as an edge. Nothing about the response says so; the staleness is only visible if
you ask for the date and check it, so this module always asks and always checks.

Free sources only, per the standing rule that no paid data is bought until the free sources are
exhausted. FRED first (dated by construction, no key needed for the CSV endpoint), yfinance as
the fallback.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from datetime import date, datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
_TIMEOUT = 15
_HEADERS = {"User-Agent": "vega-vol-indices/1.0"}

# How old a print may be and still describe today. Three calendar days covers a normal weekend
# plus one holiday; anything older is a feed that has stopped, not a market that is quiet.
MAX_AGE_DAYS = 3

# ref_signal → where to get it. Keyed by the same string ticker_profile declares, so a profile
# and its feed cannot drift apart without a KeyError rather than a silent None.
SOURCES: Dict[str, Dict] = {
    "GVZ": {"fred": "GVZCLS", "yahoo": "^GVZ",
            "label": "CBOE Gold Volatility Index"},
    "VXN": {"fred": "VXNCLS", "yahoo": "^VXN",
            "label": "CBOE Nasdaq-100 Volatility Index"},
    "VIX": {"fred": "VIXCLS", "yahoo": "^VIX",
            "label": "CBOE Volatility Index"},
    # MOVE is deliberately absent. FRED publishes no MOVE series (BAMLMOVE / MOVE /
    # ICEBOFAMOVE all 404 — FRED's BAML* series are credit spreads) and Yahoo's ^MOVE has
    # been stale since 2026-07-17. Adding it here with a source that cannot deliver would put
    # a working-looking entry in a registry whose whole job is to be trustworthy.
}

_CACHE: Dict[str, Dict] = {}
_CACHE_TTL = 900          # 15 min — these indices print once a day; this is only anti-hammer


def _cached(key: str) -> Optional[Dict]:
    hit = _CACHE.get(key)
    if hit and (time.time() - hit["at"]) < _CACHE_TTL:
        return hit["value"]
    return None


def _store(key: str, value: Optional[Dict]) -> Optional[Dict]:
    _CACHE[key] = {"at": time.time(), "value": value}
    return value


def _fred(series: str) -> Optional[Dict]:
    """Last non-missing observation from FRED's CSV endpoint, with its date.

    FRED writes "." for a missing observation rather than omitting the row, so the last LINE is
    routinely not the last VALUE — reading it without skipping dots returns a hole and calls it
    a level.
    """
    try:
        req = urllib.request.Request(FRED_CSV.format(series=series), headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:
        logger.debug("[vol_indices] FRED %s failed: %s", series, e)
        return None
    rows = list(csv.reader(io.StringIO(body)))
    if len(rows) < 2:
        return None
    for row in reversed(rows[1:]):
        if len(row) < 2:
            continue
        raw = (row[1] or "").strip()
        if not raw or raw == ".":
            continue
        try:
            return {"value": float(raw), "asof": (row[0] or "").strip()[:10], "source": "FRED"}
        except ValueError:
            continue
    return None


def _yahoo(symbol: str) -> Optional[Dict]:
    """Last close from yfinance, with the date of that close — never without it."""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="1mo")
        if hist is None or not len(hist):
            return None
        return {"value": round(float(hist["Close"].iloc[-1]), 2),
                "asof": str(hist.index[-1])[:10], "source": "Yahoo"}
    except Exception as e:
        logger.debug("[vol_indices] Yahoo %s failed: %s", symbol, e)
        return None


def _age_days(asof: str) -> Optional[int]:
    try:
        return (date.today() - datetime.strptime(asof, "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return None


def get_index(ref_signal: str, max_age_days: int = MAX_AGE_DAYS) -> Optional[Dict]:
    """One published vol index, or None.

    Returns {value, asof, age_days, source, label, stale} — never a bare float, because a
    caller handed a bare float has no way to ask how old it is and every caller that could
    forget to ask, will.

    A reading older than `max_age_days` returns None. Not a value flagged stale: the callers
    are gap calculations, and a gap is a subtraction that will happily produce a confident
    number from a month-old operand. The only safe shape is absence.
    """
    sig = (ref_signal or "").upper()
    src = SOURCES.get(sig)
    if not src:
        return None

    # The cache holds the RAW fetch, and the age check runs on every call against it. Caching
    # the post-check result instead let a value admitted under one max_age_days be returned
    # under a stricter one without being re-examined — the guard was skipped by the fast path,
    # which is precisely the code path that runs in production.
    key = f"idx_{sig}"
    got = _cached(key)
    if got is None:
        if src.get("fred"):
            got = _fred(src["fred"])
        if got is None and src.get("yahoo"):
            got = _yahoo(src["yahoo"])
        _store(key, got)
    if not got:
        return None

    age = _age_days(got.get("asof") or "")
    if age is None or age > max_age_days:
        logger.info("[vol_indices] %s discarded: asof=%s age=%s > %sd",
                    sig, got.get("asof"), age, max_age_days)
        return None

    out = dict(got)
    out.update({"age_days": age, "label": src.get("label"), "signal": sig, "stale": False})
    return out


def populate(ctx: Dict, ref_signals) -> Dict:
    """Write each requested index into ctx under its own signal key.

    Mirrors how ctx['BTC_DVOL'] is populated for the crypto path so the render loop reads one
    shape regardless of which venue the number came from. A signal that could not be fetched
    (or was stale) lands as None, which is what the placeholder path keys on.
    """
    for sig in ref_signals or ():
        sig = (sig or "").upper()
        if not sig or sig in ctx:
            continue
        ctx[sig] = get_index(sig)
    return ctx
