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

import contextlib
import json
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import durable_write  # noqa: E402  (needs the path insert above)
try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None

import logging
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
PREDICTIONS_FILE = BASE_DIR / "logs" / "vega_predictions.jsonl"

# Claim types. Each is a thing the engine asserts today with no way of being wrong.
STRIKE_HOLDS = "strike_holds"        # price finishes on the safe side of the short strike
STRIKE_UNTOUCHED = "strike_untouched"  # price never trades through the short strike
LEVEL_HOLDS = "level_holds"          # a named support/resistance is not breached on a close
TIMING_IMPROVES = "timing_improves"  # EARLY: waiting would have paid better premium
EVENT_REALISED = "event_realised"    # term-structure event spike showed up as a real move
DIRECTION = "direction"              # the regime call was right about which way price went

# Direction, one claim type PER HORIZON. grade() buckets by claim_type, so pooling a one-day
# call with a one-month call would average two populations with different base rates, different
# flat bands and different amounts of noise — the same mistake outcome_logger.cohort exists to
# prevent on the trade side. They share the single scorer below; only the bucket differs.
DIRECTION_1D = "direction_1d"                # pre-open: where this session closes
DIRECTION_OVERNIGHT = "direction_overnight"  # post-close: the gap to the next open
DIRECTION_1W = "direction_1w"                # five trading days out
DIRECTION_1M = "direction_1m"                # ~21 trading days out

# Does the underlying travel far enough, in a stated direction, to pay a long call? This is the
# lottery tab's own question — it already computes `breakeven_move_pct` per candidate and has
# never once recorded whether that move arrived. Distinct from EVENT_REALISED, which is
# undirected and measures a single day's range rather than cumulative travel.
# A volatility claim, not a price one: does realised vol over the window finish BELOW the
# implied that was sold? This is the question a credit spread actually asks, and it is
# deliberately a SEPARATE type from the direction claims so the ledger can say which of
# the two earns its place. Written by analysis/crypto_vol_forecast.py.
CRYPTO_VRP_POSITIVE = "crypto_vrp_positive"

MOVE_EXCEEDS = "move_exceeds"

# Did price finish inside the band the engine drew? price_projection states a range at a stated
# confidence and nothing has ever checked the coverage in production. A 68% band that contains
# price 45% of the time is not a tighter band, it is a wrong one, and only this catches it.
#
# The constant existed with a working scorer from the day the band was drawn, and NOTHING EVER
# WROTE ONE. Zero rows carried this type on 2026-09-03 while price_projection had been quoting
# coverage numbers from a 14,900-observation backtest as though they were production evidence.
# A measurement channel that is built, registered and never fed reads exactly like a healthy
# one from the outside; that is the failure this whole module exists to make impossible.
BAND_CONTAINS = "band_contains"

# One type per horizon, because a band is only meaningful against the horizon it was drawn for.
# Pooling an overnight band with a monthly one produces a coverage number describing neither --
# the same defect as pooling mid-fill and natural-fill trade outcomes, and `grade()` buckets by
# claim_type, so separate types is all separate grading takes.
BAND_CONTAINS_1D = "band_contains_1d"
BAND_CONTAINS_OVERNIGHT = "band_contains_overnight"
BAND_CONTAINS_1W = "band_contains_1w"
BAND_CONTAINS_1M = "band_contains_1m"

BAND_TYPES = (BAND_CONTAINS, BAND_CONTAINS_1D, BAND_CONTAINS_OVERNIGHT,
              BAND_CONTAINS_1W, BAND_CONTAINS_1M)

DIRECTION_TYPES = (DIRECTION, DIRECTION_1D, DIRECTION_OVERNIGHT, DIRECTION_1W, DIRECTION_1M)


def is_direction_claim(claim_type: Optional[str]) -> bool:
    """One scorer, many buckets — including the `_baseline` twins.

    Every directional variant is scored identically and graded separately. Membership is by
    PREFIX rather than by list so that adding a horizon, or the climatology twin that charges a
    signal for its own existence, cannot silently land in the "no scorer for claim type" branch
    and mark a whole population unresolvable while the ledger still looks healthy.
    """
    return bool(claim_type) and str(claim_type).startswith("direction")


def is_band_claim(claim_type: Optional[str]) -> bool:
    """Same prefix rule as `is_direction_claim`, and for the same reason.

    A per-horizon type added without touching the scorer would fall through to "no scorer for
    claim type" and mark its whole population unresolvable, while the ledger kept reporting a
    healthy row count. Matching by prefix means a new horizon is gradeable the moment it is
    written. The `_baseline` twins match too, which is intended -- the trailing-vol band is
    scored by exactly the same rule as the forecast band or the comparison is not a comparison.
    """
    return bool(claim_type) and str(claim_type).startswith("band_contains")


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
    durable_write.atomic_write_text(
        PREDICTIONS_FILE, "".join(json.dumps(r) + "\n" for r in rows))


def load() -> List[Dict]:
    return _read()


# Set by batch(). While it holds a list, record() reads and appends to it instead of touching
# the file, and writes once at the end.
_BUFFER = None
_BUFFER_IDS = None


@contextlib.contextmanager
def batch():
    """Hold the ledger in memory for a burst of record() calls and write it once.

    record() re-read and re-wrote the ENTIRE ledger per claim. That was invisible while this
    file held 43 rows. It stopped being invisible on 2026-08-25, when fixing the close scan let
    the daily direction sweep complete for the first time and write 449 claims in one run --
    56 watchlist tickers x 4 horizons x (forecast + climatology baseline).

    Measured cost of the un-batched path, per 449-claim sweep:

        491 rows on disk (2026-08-26)   ~10.5 s
        50,000 rows (about 4 months)    ~396 s

    The cycle's Task Scheduler ExecutionTimeLimit is 30 minutes and a cycle already runs 11-16.
    Left alone, the quadratic term would have started killing the close scan again within
    months -- the same daily failure that was just fixed, arriving from a different direction
    and looking nothing like a regression.

    Batched, the sweep is one read and one write regardless of ledger size.
    """
    global _BUFFER, _BUFFER_IDS
    if _BUFFER is not None:                  # already batching; let the outermost own the write
        yield
        return
    # Buffer only the NEW rows, and take the lock at flush rather than for the whole batch. A
    # sweep fetches a year of prices for 56 tickers between claims, so holding the lock across
    # it would block every other writer for minutes. Re-reading at flush is what makes that
    # safe: anything another process appended meanwhile is still there when we merge.
    _BUFFER = []
    _BUFFER_IDS = {r.get("id") for r in _read()}
    try:
        yield
    finally:
        fresh, _BUFFER, _BUFFER_IDS = _BUFFER, None, None
        if fresh:
            with durable_write.exclusive(PREDICTIONS_FILE):
                rows = _read()
                have = {r.get("id") for r in rows}
                rows.extend(r for r in fresh if r.get("id") not in have)
                _write(rows)


def record(trade_id: str, ticker: str, claim_type: str, claim: str,
           probability: Optional[float], resolves_on: str,
           context: Optional[Dict] = None) -> Optional[str]:
    """Log one falsifiable claim.

    `probability` is what the engine believed at the time (0-1) — required for Brier scoring
    and the reason accuracy alone is not enough. `resolves_on` is the date the claim can first
    be marked; a claim with no resolution date is an opinion, not a prediction, and is
    rejected. A resolution date that is not a real DATE is rejected for the same reason: it can
    never come due, so the claim can never be graded.

    That last check is not hypothetical. `2026-13-18` sat in this ledger from 2026-08-20,
    written through here without complaint and only caught downstream by the resolver, which
    could do nothing with it but mark it unresolvable. Three audits then chased it as an
    upstream quote-feed glitch. Refusing it at the point of writing turns a permanently
    unreadable row into a logged rejection at the moment the caller can still be identified.
    """
    if not _cfg("PREDICTION_LEDGER_ENABLED", True):
        return None
    if not (trade_id and claim_type and resolves_on):
        return None
    try:
        date.fromisoformat(str(resolves_on)[:10])
    except (ValueError, TypeError):
        logger.warning(
            "[predictions] refusing claim %s on %s — resolution date %r is not a real date, so "
            "the claim could never come due.", claim_type, ticker, resolves_on)
        return None
    pid = f"{trade_id}::{claim_type}"
    if _BUFFER is not None:
        # Inside batch(): the ledger is already in memory and the id index is a set, so the
        # duplicate check is O(1) and nothing is written until the batch closes.
        rows, seen = _BUFFER, _BUFFER_IDS
        if pid in seen:
            return pid
        seen.add(pid)
    else:
        rows = None         # filled under the lock below
    row = {
        "id": pid, "trade_id": trade_id, "ticker": ticker,
        "claim_type": claim_type, "claim": claim,
        "probability": round(float(probability), 4) if probability is not None else None,
        "made_at": datetime.now().isoformat(),
        "resolves_on": str(resolves_on)[:10],
        "context": context or {},
        "status": "open", "correct": None, "resolved_at": None, "resolution_note": None,
    }
    if rows is not None:                     # batching: the flush writes it
        rows.append(row)
        return pid
    with durable_write.exclusive(PREDICTIONS_FILE):
        rows = _read()
        if any(r.get("id") == pid for r in rows):
            return pid      # one claim of each type per trade; re-recording is a no-op
        rows.append(row)
        _write(rows)
    return pid


def _mark(row: Dict, correct: Optional[bool], note: str) -> None:
    row["status"] = "resolved" if correct is not None else "unresolvable"
    row["correct"] = correct
    row["resolved_at"] = datetime.now().isoformat()
    row["resolution_note"] = note


def _defer(row: Dict, due: date, today: date, note: str, stats: Dict) -> None:
    """A claim that could not be read today stays OPEN and is retried on the next cycle.

    It is written off only once it has been unreadable for PREDICTION_RESOLVE_GRACE_DAYS past
    its resolution date, which distinguishes "the data source was down" from "this claim can
    genuinely never be scored". The attempt count is kept so a claim that quietly fails every
    day is visible rather than merely absent.
    """
    grace = int(_cfg("PREDICTION_RESOLVE_GRACE_DAYS", 5))
    row["resolve_attempts"] = int(row.get("resolve_attempts") or 0) + 1
    row["last_resolve_error"] = note
    if (today - due).days > grace:
        _mark(row, None, f"{note} (gave up after {grace}d and "
                         f"{row['resolve_attempts']} attempts)")
        stats["unresolvable"] += 1
    else:
        stats["deferred"] = stats.get("deferred", 0) + 1


def resolve(price_lookup, today: Optional[date] = None) -> Dict:
    """Score every claim whose resolution date has passed.

    `price_lookup(ticker, start, end)` must return a list of (date, high, low, close) covering
    the claim window; the caller owns data fetching so this module stays pure and testable.
    """
    today = today or date.today()
    rows = _read()
    stats = {"checked": 0, "resolved": 0, "unresolvable": 0, "deferred": 0}
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
        # A failed price lookup used to mark the claim `unresolvable` FOREVER. One network blip
        # on the day a claim came due permanently deleted it from the record — and because the
        # sample only ever shrinks, the loss is invisible: the hit rate still computes, over
        # fewer claims than were made. On a daily 24/7 asset that quietly eats the validation
        # sample. A lookup failure is now a RETRY, and only a claim that has stayed unreadable
        # past the grace window is written off.
        try:
            made = datetime.fromisoformat(r["made_at"]).date()
            bars = price_lookup(r["ticker"], made, due)
        except Exception as e:
            _defer(r, due, today, f"price history unavailable: {e}", stats)
            changed = True
            continue
        if not bars:
            _defer(r, due, today, "no price history in the claim window", stats)
            changed = True
            continue

        correct, note = _score(r, bars)
        _mark(r, correct, note)
        stats["resolved" if correct is not None else "unresolvable"] += 1
        changed = True

    if changed:
        _write(rows)
    return stats


def _bar_date(bar) -> str:
    d = bar[0]
    return d.isoformat()[:10] if hasattr(d, "isoformat") else str(d)[:10]


def _bar_on(bars: Sequence, target_iso: str):
    """The bar for a date, or the last one at or before it (the date may be a holiday)."""
    chosen = None
    for b in bars:
        ds = _bar_date(b)
        if ds == target_iso:
            return b
        if ds < target_iso:
            chosen = b
    return chosen


def _settle_price(bars: Sequence, ctx: Dict):
    """The price that settles a claim: None to use the window's last close, or an error string.

    `score_on` names the settling bar. `score_field` selects open or close on that bar — an
    OVERNIGHT claim is about the gap from one close to the next OPEN, and scoring it on the
    following close would fold a whole session into a claim that never mentioned one. Opens are
    the fifth element of a bar and are optional, so a caller whose price_lookup predates them
    gets an explicit refusal instead of a silently wrong number.
    """
    target = ctx.get("score_on")
    if not target:
        return None
    bar = _bar_on(bars, str(target)[:10])
    if bar is None:
        return f"no bar on or before the settling date {str(target)[:10]}"
    if (ctx.get("score_field") or "close").lower() == "open":
        if len(bar) < 5 or bar[4] is None:
            return "this claim settles on the OPEN and price_lookup returned no opening price"
        return float(bar[4])
    return float(bar[3])


def _score(r: Dict, bars: Sequence) -> tuple:
    """Mark one claim. bars = [(date, high, low, close)] or [(date, high, low, close, open)].

    The fifth element is optional so that adding it did not break every existing caller; only
    claims that settle on the open require it.
    """
    ctx = r.get("context") or {}
    ct = r.get("claim_type")
    closes = [b[3] for b in bars]
    lows = [b[2] for b in bars]
    highs = [b[1] for b in bars]
    final = closes[-1]

    # A CLAIM WHOSE SETTLING BAR IS ITS OWN ANCHOR BAR MEASURED NOTHING.
    #
    # `next_trading_day` counts weekdays and does not model holidays, which is fine when the
    # holiday lands on `resolves_on` -- resolution just reads the last bar at or before it and
    # the grace window absorbs the slip. It is NOT fine when the holiday lands on `score_on`,
    # because then `_bar_on` falls back to the bar the claim was anchored to and the horizon
    # collapses to zero. Settled price equals price at claim, and every band contains it.
    #
    # Concretely: 2026-09-07 is Labor Day. 216 one-day band claims written on Friday 2026-09-04
    # carry score_on 2026-09-07, and ALL 216 would have graded correct on a zero-day move --
    # 100% -- as the first grades this channel ever produced. A calibration read opening on a
    # third of its sample scored for free is worse than no read.
    #
    # Refused rather than deferred: the bar will never appear, so waiting cannot help. Marked
    # unresolvable, which is the existing signal for "this claim cannot be graded as written".
    if ctx.get("score_on"):
        _sb = _bar_on(bars, str(ctx["score_on"])[:10])
        _anchor = str(r.get("made_at") or "")[:10]
        if _sb is not None and _anchor and _bar_date(_sb) <= _anchor:
            return None, (f"settling bar {_bar_date(_sb)} is at or before the claim date "
                          f"{_anchor} — score_on {str(ctx['score_on'])[:10]} had no bar "
                          f"(market holiday), so the horizon collapsed to zero and the claim "
                          f"measures nothing")

    if ct == STRIKE_HOLDS:
        k = ctx.get("short_strike")
        if k is None:
            return None, "no strike in context"
        is_call = "call" in (ctx.get("strategy") or "").lower()
        ok = final < float(k) if is_call else final > float(k)
        return ok, (f"settled {final:.2f} vs strike {float(k):.2f} "
                    f"({'call' if is_call else 'put'} side)")

    if ct == CRYPTO_VRP_POSITIVE:
        # "Realised vol finishes below the implied that was sold." Scored on the SAME
        # annualisation the forecast used (sqrt(252), market days) -- comparing a 252-day
        # annualised realised against a 365-day annualised forecast would bias every verdict in
        # one direction and the sign of that bias would never show up in the hit rate.
        iv = ctx.get("ibit_iv")
        if iv is None:
            return None, "no implied vol in context"
        if len(closes) < 10:
            return None, f"only {len(closes)} bars in the window; too few to measure vol"
        import math
        rets = [math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
        if len(rets) < 9:
            return None, "not enough usable returns in the window"
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        realised = math.sqrt(var) * math.sqrt(252.0)
        ok = realised < float(iv)
        return ok, (f"realised {realised * 100:.1f}% vs implied {float(iv) * 100:.1f}% over "
                    f"{len(rets)} sessions -> premium {'held' if ok else 'did NOT hold'} "
                    f"({(float(iv) - realised) * 100:+.1f} vol pts)")

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

    if is_direction_claim(ct):
        entry_px = ctx.get("price_at_claim")
        expect = (ctx.get("expected") or "").lower()
        if entry_px is None or expect not in ("up", "down", "flat"):
            return None, "missing direction context"
        # WHICH bar settles the claim. Without this the scorer reads the last bar in the
        # window, which is correct only when the claim matures exactly at the window's end.
        # A one-day call made Monday cannot be resolved until Monday's bar is COMPLETE, so it
        # is given a Tuesday resolution date — and then the last bar is Tuesday's, scoring a
        # two-day move against a one-day claim. `score_on` names the bar that settles it and
        # the resolution date only says when it is safe to read. Absent, behaviour is
        # unchanged, which is what keeps the existing BTC claims scoring as they always have.
        settled = _settle_price(bars, ctx)
        if isinstance(settled, str):
            return None, settled
        if settled is not None:
            final = settled
        # The "flat" band was hard-coded at 1%, which is calibrated for an equity over a couple
        # of weeks. Bitcoin at 34 vol moves ±6.7% over 14 days, so a 1% band is 0.15 sigma —
        # "flat" becomes unreachable and the claim degrades into a coin flip on noise while
        # still reporting a hit rate that looks like skill. The band travels WITH the claim,
        # set by whoever makes it from that asset's own volatility.
        band = ctx.get("flat_band_pct")
        try:
            band = float(band) if band is not None else 1.0
        except (TypeError, ValueError):
            band = 1.0
        chg = (final / float(entry_px) - 1) * 100
        got = "up" if chg > band else ("down" if chg < -band else "flat")
        return got == expect, (f"expected {expect}, price went {got} "
                               f"({chg:+.1f}% vs ±{band:.1f}% flat band)")

    if ct == MOVE_EXCEEDS:
        entry_px = ctx.get("price_at_claim")
        need = ctx.get("move_pct")
        side = (ctx.get("direction") or "up").lower()
        if entry_px is None or need is None or side not in ("up", "down"):
            return None, "missing move context (price_at_claim, move_pct, direction)"
        entry_px, need = float(entry_px), abs(float(need))
        # TOUCH or SETTLE, stated per claim rather than assumed. A long call that only has to
        # be SOLD is made whole the moment price trades through the level, so touch is the
        # honest read for the lottery tab's target multiple. A call HELD to expiry is made
        # whole only by where it settles. Scoring one on the other's basis inflates or deflates
        # the hit rate by exactly the amount of the path, which is most of the variance.
        basis = (ctx.get("basis") or "touch").lower()
        if basis == "settle":
            settled = _settle_price(bars, ctx)
            if isinstance(settled, str):
                return None, settled
            reached = settled if settled is not None else final
        else:
            reached = max(highs) if side == "up" else min(lows)
        moved = (reached / entry_px - 1) * 100
        got = moved >= need if side == "up" else moved <= -need
        return got, (f"needed {side} {need:.1f}%, {basis} reached {reached:.2f} "
                     f"from {entry_px:.2f} ({moved:+.1f}%)")

    if is_band_claim(ct):
        lo, hi = ctx.get("band_low"), ctx.get("band_high")
        if lo is None or hi is None:
            return None, "no band in context"
        settled = _settle_price(bars, ctx)
        if isinstance(settled, str):
            return None, settled
        px = settled if settled is not None else final
        lo, hi = float(lo), float(hi)
        return (lo <= px <= hi), (f"settled {px:.2f} vs band {lo:.2f}-{hi:.2f} "
                                  f"({'inside' if lo <= px <= hi else 'outside'})")

    return None, f"no scorer for claim type {ct}"


def _quantile_bins(obs: Sequence[tuple], k: int) -> List[List[tuple]]:
    """Split (probability, outcome) pairs into k roughly equal-COUNT bins.

    Equal-count rather than equal-width, because VEGA's forecasts are clustered: nearly every
    claim lands between 0.70 and 0.85. Fixed-width bins would drop that whole mass into one
    cell, and a single bin makes resolution identically zero by construction — the engine would
    be reported as having no discrimination when what actually happened is that the bins could
    not see any.

    IDENTICAL FORECASTS ARE NEVER SPLIT ACROSS BINS, and that is load-bearing rather than
    tidy. Splitting on position meant that when every forecast was the same number, the sort
    fell back to input order — so a ledger that happened to list its hits before its misses got
    cut into a "winners" bin and a "losers" bin, and a forecaster saying 70% about everything
    scored resolution 0.16. The decomposition would have manufactured discrimination out of
    row order. A bin means "the forecaster said approximately X"; two claims of exactly X
    cannot belong to different cells.
    """
    ordered = sorted(obs, key=lambda x: x[0])
    if k <= 1 or not ordered:
        return [ordered]

    groups: List[List[tuple]] = []
    for pair in ordered:
        if groups and groups[-1][0][0] == pair[0]:
            groups[-1].append(pair)
        else:
            groups.append([pair])

    target = len(ordered) / k
    out: List[List[tuple]] = []
    cur: List[tuple] = []
    left = len(groups)
    for g in groups:
        cur.extend(g)
        left -= 1
        # Close once the bin is full enough AND at least one group is left to start the next.
        # The guard is only there to avoid emitting an empty final bin: requiring enough groups
        # to fill ALL k bins collapsed the whole sample into one cell whenever the forecasts
        # took fewer distinct values than there were bins — which is exactly VEGA's case, and
        # made a perfectly discriminating forecaster score resolution 0.000.
        if len(out) < k - 1 and len(cur) >= target and left >= 1:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def decompose(pairs: Sequence[tuple], n_bins: Optional[int] = None,
              bootstrap: int = 2000, seed: int = 20260810) -> Dict:
    """Murphy's decomposition of the Brier score, with a bootstrap interval on resolution.

        BS = reliability - resolution + uncertainty

    Raw Brier conflates two things this system needs kept apart:

      reliability  is 78% actually 78%?          (calibration; lower is better, 0 is perfect)
      resolution   do the confident calls differ from the timid ones?  (higher is better)
      uncertainty  o(1-o), fixed by the base rate and identical for every model

    A forecaster that ALWAYS predicts the base rate scores a respectable Brier and has exactly
    ZERO resolution. It is perfectly calibrated and completely useless, and raw Brier calls that
    "well calibrated". That is not a hypothetical here: the claim probabilities on file span
    0.535 to 0.848 with most inside 0.70-0.85, so the ledger could report a good Brier while
    saying nearly the same number about every trade.

    `skill` is Murphy's skill score, (resolution - reliability) / uncertainty — equivalently
    1 - BS/BS_climatology. Above 0 means beating "always guess the base rate"; at or below 0
    means not.

    The bootstrap matters more than the point estimate. At the sample sizes this ledger will
    have for months, resolution computed once is noise with a decimal point on it. The bar for
    "this claim type carries information" is the LOWER bound of the interval above zero, not
    the point estimate above zero.
    """
    obs = [(float(p), 1.0 if o else 0.0) for p, o in pairs if p is not None]
    n = len(obs)
    empty = {"n": n, "brier": None, "reliability": None, "resolution": None,
             "uncertainty": None, "skill": None, "resolution_ci": None,
             "resolution_p": None, "residual": None,
             "n_bins": 0, "forecast_spread": None, "base_rate": None}
    if n == 0:
        return empty

    def _terms(sample: Sequence[tuple], k: int) -> tuple:
        m = len(sample)
        base = sum(o for _, o in sample) / m
        unc = base * (1.0 - base)
        rel = res = 0.0
        for cell in _quantile_bins(sample, k):
            if not cell:
                continue
            w = len(cell) / m
            p_bar = sum(p for p, _ in cell) / len(cell)
            o_bar = sum(o for _, o in cell) / len(cell)
            rel += w * (p_bar - o_bar) ** 2
            res += w * (o_bar - base) ** 2
        return rel, res, unc, base

    # One bin per ~5 observations, never more than 5 and never fewer than 2. Ten bins on
    # fifteen claims would give every cell its own perfect record and report resolution as
    # near-total — overfitting the decomposition rather than measuring anything.
    k = n_bins if n_bins else max(2, min(5, n // 5))
    rel, res, unc, base = _terms(obs, k)
    # What the binner ACTUALLY produced. Ties are never split, so a ledger whose forecasts take
    # only two distinct values yields two bins however many were asked for — and reporting the
    # requested number would overstate how finely this was measured.
    k_used = len(_quantile_bins(obs, k))
    brier = sum((p - o) ** 2 for p, o in obs) / n
    mean_p = sum(p for p, _ in obs) / n
    spread = (sum((p - mean_p) ** 2 for p, _ in obs) / n) ** 0.5

    ci = p_value = None
    if bootstrap and n >= 2:
        rng = random.Random(seed)          # seeded: a calibration report must not move on re-run
        draws = []
        for _ in range(int(bootstrap)):
            sample = [obs[rng.randrange(n)] for _ in range(n)]
            if len({o for _, o in sample}) < 2:
                continue                   # all-hit or all-miss resample: resolution undefined
            draws.append(_terms(sample, k)[1])
        if len(draws) >= 100:
            draws.sort()
            lo = draws[int(0.025 * len(draws))]
            hi = draws[min(len(draws) - 1, int(0.975 * len(draws)))]
            ci = [round(lo, 4), round(hi, 4)]

        # A PERMUTATION test, not the CI, is what answers "could this have been luck?".
        #
        # Resolution is a sum of squares and therefore non-negative, so pure noise still scores
        # above zero and a bootstrap interval around it can sit entirely above zero while the
        # forecaster knows nothing. Measured here: 40 random forecasts produced resolution
        # 0.019 with a bootstrap CI of [0.004, 0.055] — an interval that "excludes zero" for a
        # model with no signal at all. Reading that as skill is precisely the error this whole
        # decomposition exists to prevent.
        #
        # Shuffling the outcomes against the forecasts breaks any real association while keeping
        # both margins intact, so the shuffled scores ARE the distribution of resolution under
        # "this forecaster knows nothing". p is where the observed value falls in it.
        probs = [p for p, _ in obs]
        outs = [o for _, o in obs]
        null = []
        for _ in range(int(bootstrap)):
            shuffled = outs[:]
            rng.shuffle(shuffled)
            null.append(_terms(list(zip(probs, shuffled)), k)[1])
        if null:
            p_value = round((sum(1 for x in null if x >= res) + 1) / (len(null) + 1), 4)

    return {
        "n": n,
        "brier": round(brier, 4),
        "reliability": round(rel, 4),
        "resolution": round(res, 4),
        "uncertainty": round(unc, 4),
        "skill": round((res - rel) / unc, 4) if unc > 0 else None,
        "resolution_ci": ci,
        # P(resolution this high | the forecaster knows nothing). Below 0.05 is the bar for
        # claiming discrimination. This is the number the verdict conditions on.
        "resolution_p": p_value,
        # BS = reliability - resolution + uncertainty holds EXACTLY only when every forecast in
        # a bin is identical. Binning continuous forecasts leaves this remainder, and reporting
        # the three terms without it would mean they silently do not add up to the Brier they
        # claim to decompose. Exposed rather than hidden so the identity is checkable.
        #
        # Computed from the ROUNDED terms, so the identity holds on the numbers actually
        # published rather than only on the full-precision ones nobody can see.
        "residual": round(round(brier, 4)
                          - (round(rel, 4) - round(res, 4) + round(unc, 4)), 4),
        "n_bins": k_used,
        # Resolution is capped by how much the forecasts actually vary. A model that says 75%
        # about everything cannot discriminate no matter how right it is, and this is the number
        # that says whether the ceiling or the model is the binding constraint.
        "forecast_spread": round(spread, 4),
        "base_rate": round(base, 4),
    }


def cluster_sample(rows: Sequence[Dict]) -> Dict:
    """How many INDEPENDENT observations a set of claims of one type really represents.

    A raw resolved count is not a sample size here, and reporting one is a defect rather than a
    simplification. Three things make these rows dependent, and all three are live:

    1. OVERLAPPING HORIZONS. A 21-day claim written every session shares 20 of its 21 days with
       the claim written the day before. Ninety of them are not ninety draws; they are closer to
       four. This is the identical error already found in the HAR work, where 2,045 "out-of-
       sample" predictions from daily-stepped 30-day windows were an effective ~68 blocks.

    2. CROSS-SECTIONAL CORRELATION. SPY, QQQ, IWM and the megacaps are one bet on one market.
       When it gaps they miss together. Tickers are therefore counted by their SECTOR PROXY --
       the same grouping vol_forecast already uses -- rather than individually.

    3. SAME-DAY MULTIPLICITY. Several claims about one ticker on one day are one observation of
       that ticker on that day, whatever they are about.

    The estimate is deliberately CONSERVATIVE: independent time blocks multiplied by distinct
    cross-sectional clusters, capped by the raw count. It is not a variance-corrected effective
    sample size and does not pretend to be; it is a number that cannot flatter the sample, which
    is the only property that matters when the failure mode being guarded against is a
    confident conclusion drawn from a thousand correlated readings of the same week.
    """
    import math as _math
    rows = list(rows)
    if not rows:
        return {"n_raw": 0, "n_ticker_days": 0, "n_time_blocks": 0,
                "n_clusters": 0, "n_effective": 0, "horizon_days": None}

    def _day(r):
        return str(r.get("made_at") or "")[:10]

    ticker_days = {(str(r.get("ticker") or "?").upper(), _day(r)) for r in rows}
    days = {d for _, d in ticker_days if d}

    horizons = [(r.get("context") or {}).get("horizon_days") for r in rows]
    horizons = [int(h) for h in horizons if isinstance(h, (int, float)) and h and h > 0]
    # The LONGEST horizon present, not the average: overlap is set by the widest window in the
    # bucket, and averaging would credit the sample with independence the long claims destroy.
    horizon = max(horizons) if horizons else 1

    # Distinct calendar days a claim was made on, divided into non-overlapping windows.
    n_days = len(days) or 1
    time_blocks = max(1, int(_math.ceil(n_days / float(horizon))))

    clusters = set()
    for r in rows:
        tk = str(r.get("ticker") or "?").upper()
        ctx = r.get("context") or {}
        proxy = ctx.get("sector_proxy")
        if not proxy:
            try:
                from analysis import vol_forecast as _vf
                proxy = _vf.sector_proxy_for(tk)
            except Exception:
                proxy = None
        clusters.add(proxy or tk)

    n_eff = min(len(rows), time_blocks * max(1, len(clusters)))
    return {
        "n_raw": len(rows),
        "n_ticker_days": len(ticker_days),
        "n_time_blocks": time_blocks,
        "n_clusters": len(clusters),
        "n_effective": n_eff,
        "horizon_days": horizon,
    }


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
        b = by_type.setdefault(t, {"n": 0, "hits": 0, "probs": [], "briers": [], "pairs": []})
        b["n"] += 1
        b["hits"] += 1 if r["correct"] else 0
        b.setdefault("rows", []).append(r)
        p = r.get("probability")
        if p is not None:
            b["probs"].append(p)
            b["briers"].append((p - (1.0 if r["correct"] else 0.0)) ** 2)
            b["pairs"].append((p, bool(r["correct"])))

    out = {}
    min_n = int(_cfg("PREDICTION_MIN_FOR_GRADE", 10))
    for t, b in by_type.items():
        hit = b["hits"] / b["n"]
        brier = (sum(b["briers"]) / len(b["briers"])) if b["briers"] else None
        avg_p = (sum(b["probs"]) / len(b["probs"])) if b["probs"] else None
        # The decomposition, not just the aggregate. Raw Brier cannot tell a model that knows
        # something from one that has memorised the base rate; resolution can.
        dec = decompose(b["pairs"])
        # Both counts, side by side, always. A headline quoting only the raw one is the defect
        # this pair exists to prevent -- see cluster_sample().
        clus = cluster_sample(b.get("rows") or [])
        out[t] = {
            "n": b["n"],
            "n_effective": clus["n_effective"],
            "n_ticker_days": clus["n_ticker_days"],
            "clustering": clus,
            "hit_rate": round(hit * 100, 1),
            "avg_confidence": round(avg_p * 100, 1) if avg_p is not None else None,
            "brier": round(brier, 4) if brier is not None else None,
            "bias_pp": round((avg_p - hit) * 100, 1) if avg_p is not None else None,
            "reliability": dec["reliability"],
            "resolution": dec["resolution"],
            "uncertainty": dec["uncertainty"],
            "skill": dec["skill"],
            "resolution_ci": dec["resolution_ci"],
            "forecast_spread": dec["forecast_spread"],
            # Gradeability is judged on the EFFECTIVE count. Ten correlated readings of one
            # week are not ten observations, and letting them clear the floor is how a channel
            # starts issuing verdicts before it has earned one.
            "gradeable": clus["n_effective"] >= min_n,
            "verdict": _verdict(b["n"], min_n, hit, avg_p, brier, dec,
                                n_effective=clus["n_effective"]),
        }
    return {
        "total_claims": len(rows),
        "open": sum(1 for r in rows if r.get("status") == "open"),
        "resolved": len(resolved),
        "unresolvable": sum(1 for r in rows if r.get("status") == "unresolvable"),
        "by_type": out,
    }


def _verdict(n: int, min_n: int, hit: float, avg_p: Optional[float],
             brier: Optional[float], dec: Optional[Dict] = None,
             n_effective: Optional[int] = None) -> str:
    """Leads with RESOLUTION, because that is the question raw Brier cannot answer.

    "78% correct, Brier 0.17, well calibrated" is what a forecaster saying the base rate about
    every trade also scores. Calibration says the numbers are honest; resolution says they
    distinguish one trade from another. A claim type can be perfectly calibrated and carry no
    information at all, and only the second sentence catches it.
    """
    n_eff = n if n_effective is None else int(n_effective)
    if n_eff < min_n:
        if n_eff < n:
            return (f"{n} resolved, but only ~{n_eff} INDEPENDENT after clustering by "
                    f"overlapping horizon and market factor — not gradeable yet "
                    f"({min_n} needed)")
        return f"only {n} resolved — not gradeable yet ({min_n} needed)"
    if avg_p is None:
        return f"{hit*100:.0f}% correct over {n}, but no confidence was recorded to calibrate"
    bias = (avg_p - hit) * 100
    if brier is not None and brier > 0.25:
        return (f"{hit*100:.0f}% correct over {n} with a Brier of {brier:.2f} — worse than "
                f"always guessing 50%. This claim type is not adding information.")

    # Does it discriminate? Judged on the PERMUTATION p-value, not the bootstrap interval:
    # resolution is a sum of squares, so noise scores above zero and its CI can sit entirely
    # above zero for a forecaster that knows nothing.
    disc = ""
    p = (dec or {}).get("resolution_p")
    if p is not None:
        if p < 0.05:
            disc = (f" It also DISCRIMINATES: resolution {dec['resolution']:.3f} "
                    f"(p={p:.3f} against shuffled outcomes), skill {dec['skill']:+.2f} "
                    f"versus always guessing the base rate.")
        else:
            disc = (f" But it does NOT discriminate: resolution {dec['resolution']:.3f} is "
                    f"what shuffling the outcomes produces {p*100:.0f}% of the time. On this "
                    f"evidence it is no better than saying {hit*100:.0f}% about every trade")
            if (dec or {}).get("forecast_spread") is not None and dec["forecast_spread"] < 0.05:
                disc += (f", and with a forecast spread of only {dec['forecast_spread']:.3f} it "
                         f"barely could be — the probabilities hardly vary")
            disc += "."

    if bias > 10:
        return (f"{hit*100:.0f}% correct but claiming {avg_p*100:.0f}% — overconfident by "
                f"{bias:.0f}pp. The direction is useful; the certainty is not earned.{disc}")
    if bias < -10:
        return (f"{hit*100:.0f}% correct while only claiming {avg_p*100:.0f}% — "
                f"underconfident by {abs(bias):.0f}pp. This signal deserves more weight.{disc}")
    return f"{hit*100:.0f}% correct over {n}, well calibrated (Brier {brier:.2f}).{disc}"


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
