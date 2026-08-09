#!/usr/bin/env python3
"""
data/crypto.py — Bitcoin market data, from free unauthenticated sources only.

WHY THIS EXISTS
Bitcoin's own options market prices volatility for the same underlying risk that IBIT's listed
options price, in a different venue, with different participants, on a different clock. That
spread is measurable for free and does not require anyone to forecast anything — which makes it
a far cheaper first signal than a directional call, because a direction has to beat a coin flip
to be worth something while a spread only has to be measurable to be informative.

SOURCES, ALL FREE AND UNAUTHENTICATED
  Deribit  public/get_index_price            — BTC spot index
           public/get_volatility_index_data  — DVOL, BTC 30-day implied vol
  Coinbase /products/BTC-USD/candles         — daily OHLCV, for realised vol
           /products/BTC-USD/ticker          — spot, and a live bid/ask for quality checks

NOT ROBINHOOD. Robinhood's crypto API is an execution venue: its documented read surface is
accounts, holdings, orders, products and quotes. Signal and execution must not share one free
retail API — if it rate-limits or changes an endpoint, a system reading its signal from there
loses the ability to decide and the ability to exit in the same instant. Nothing in this module
is allowed to import a broker.

EVERY FUNCTION DEGRADES. A missing crypto read narrows what can be judged; it never raises, and
it never prevents an equity scan from completing. Failures are recorded to the data-quality log
so a thin read is visible rather than silently averaged in.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

DERIBIT = "https://www.deribit.com/api/v2/public"
COINBASE = "https://api.exchange.coinbase.com"

_TIMEOUT = 15
_HEADERS = {"User-Agent": "vega-btc/1.0"}

# Session cache, same contract as data/fetcher._cache: one scan should not hit an endpoint
# repeatedly for a number that moves on a 30-second scale.
_cache: Dict[str, tuple] = {}
_CACHE_TTL = 300


def _cfg(name, default):
    return getattr(config, name, default)


def _cached(key: str):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value):
    _cache[key] = (time.time(), value)
    return value


def _get(url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """One HTTP read. Returns None on any failure — never raises, never blocks a scan."""
    try:
        r = requests.get(url, params=params, timeout=_TIMEOUT, headers=_HEADERS)
        if r.status_code != 200:
            logger.debug("[crypto] %s returned HTTP %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.debug("[crypto] %s failed: %s", url, e)
        return None


# ── Deribit ───────────────────────────────────────────────────────────────────

def get_btc_spot() -> Optional[float]:
    """BTC index price from Deribit. The index, not a single exchange's last trade —
    it is the number Deribit's own options settle against, so it is the right spot to pair
    with DVOL."""
    if (c := _cached("btc_spot")) is not None:
        return c
    d = _get(f"{DERIBIT}/get_index_price", {"index_name": "btc_usd"})
    px = ((d or {}).get("result") or {}).get("index_price")
    return _store("btc_spot", float(px)) if px else None


def get_dvol(hours_back: int = 48) -> Optional[float]:
    """DVOL — Deribit's BTC 30-day implied volatility index, in vol points (e.g. 34.24).

    This is the free, direct equivalent of reconstructing ATM IV from an options chain, and it
    is what makes the cross-venue comparison cheap: no chain fetch, no interpolation, no
    quality filter, one number the venue publishes itself.
    """
    if (c := _cached("dvol")) is not None:
        return c
    now_ms = int(time.time() * 1000)
    d = _get(f"{DERIBIT}/get_volatility_index_data", {
        "currency": "BTC",
        "start_timestamp": now_ms - hours_back * 3600 * 1000,
        "end_timestamp": now_ms,
        "resolution": 3600,
    })
    rows = ((d or {}).get("result") or {}).get("data") or []
    if not rows:
        return None
    # Each row is [timestamp, open, high, low, close]; the last close is the current level.
    try:
        return _store("dvol", float(rows[-1][4]))
    except (IndexError, TypeError, ValueError):
        return None


# ── Coinbase ──────────────────────────────────────────────────────────────────

def get_btc_candles(days: int = 90) -> List[Dict]:
    """Daily BTC OHLCV from Coinbase, newest last.

    Coinbase returns newest-first and caps a response at 300 candles; both are normalised here
    so callers get the same ascending shape the equity path uses.
    """
    key = f"candles_{days}"
    if (c := _cached(key)) is not None:
        return c
    raw = _get(f"{COINBASE}/products/BTC-USD/candles", {"granularity": 86400})
    if not isinstance(raw, list) or not raw:
        return []
    out = []
    for row in raw:
        try:
            ts, low, high, opn, close, vol = row[:6]
            out.append({
                "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(),
                "open": float(opn), "high": float(high), "low": float(low),
                "close": float(close), "volume": float(vol),
            })
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda r: r["date"])
    return _store(key, out[-int(days):])


def get_btc_quote() -> Optional[Dict]:
    """Live bid/ask/last. Used for the quality read — a crossed or absent book is the crypto
    equivalent of a stale option quote and should be visible, not averaged away."""
    d = _get(f"{COINBASE}/products/BTC-USD/ticker")
    if not isinstance(d, dict):
        return None
    try:
        bid, ask = float(d["bid"]), float(d["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    mid = (bid + ask) / 2 if bid and ask else 0.0
    return {
        "bid": bid, "ask": ask, "mid": round(mid, 2),
        "spread_pct": round((ask - bid) / mid, 6) if mid else None,
        "price": float(d.get("price") or mid),
        "volume": float(d.get("volume") or 0),
    }


# ── Derived ───────────────────────────────────────────────────────────────────

def realised_vol(candles: Optional[List[Dict]] = None, window: int = 30) -> Optional[float]:
    """Annualised close-to-close realised vol, in vol POINTS to match DVOL.

    365 trading days, not 252: Bitcoin does not close. Using the equity convention here would
    understate BTC's realised vol by ~19% and make every VRP comparison against DVOL wrong in
    the same direction, which is worse than not computing it at all.
    """
    import math
    candles = candles if candles is not None else get_btc_candles(window + 5)
    closes = [c["close"] for c in (candles or []) if c.get("close")]
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    rets = rets[-window:]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(365) * 100, 2)


def snapshot() -> Dict:
    """Everything the BTC layer knows right now, in one advisory dict.

    Never raises and never returns a partially-true number: any field that could not be read is
    None, and `ok` says whether the core pair (DVOL + spot) is present. Callers must treat a
    not-ok snapshot as absence of information, not as a neutral reading — the difference is the
    whole reason the ravens exist.
    """
    dvol = get_dvol()
    spot = get_btc_spot()
    candles = get_btc_candles(95)
    rv = realised_vol(candles, int(_cfg("BTC_RV_WINDOW", 30)))
    quote = get_btc_quote()

    snap = {
        "at": datetime.now().isoformat(),
        "btc_spot": round(spot, 2) if spot else None,
        "dvol": dvol,
        "btc_rv_30d": rv,
        # BTC's own variance risk premium: what its options charge over what it delivered.
        "btc_vrp_pp": round(dvol - rv, 2) if (dvol is not None and rv is not None) else None,
        "quote": quote,
        "candles": len(candles),
        "sources": {"dvol": "deribit", "spot": "deribit", "candles": "coinbase"},
    }
    snap["ok"] = snap["dvol"] is not None and snap["btc_spot"] is not None
    _record_quality(snap, candles)
    return snap


def _record_quality(snap: Dict, candles: List[Dict]) -> None:
    """Log the crypto feeds to the same quality ledger the options chains use.

    The lesson from 2026-08-08 is that an unmeasured input cannot be discounted: VEGA ran for
    months on chains discarding 30-45% of records with nothing recording it. A crypto feed that
    silently starts returning 3 candles instead of 90 must be as visible as a thin chain, and
    for the same reason — the numbers look equally confident either way.
    """
    if not _cfg("CHAIN_QUALITY_LOG_ENABLED", True):
        return
    try:
        from data import data_quality_log as dq
        wanted = 90
        usable = sum(1 for c in candles if c.get("close") and c.get("high") and c.get("low"))
        dq.record("BTC-USD", "coinbase", raw_count=wanted, usable_count=min(usable, wanted),
                  note=None if snap.get("ok") else "deribit read incomplete")
    except Exception as e:                          # pragma: no cover - defensive
        logger.debug("[crypto] quality logging failed: %s", e)
