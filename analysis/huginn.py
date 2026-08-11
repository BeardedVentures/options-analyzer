#!/usr/bin/env python3
"""
huginn.py — Thought. Reads the present state of an open position and asks one question:
is the story that justified this trade still true?

Huginn never reads P&L, credit, or cost basis. A position is not wrong because it is down;
it is wrong because the structure that justified it has broken. Those are different claims,
and conflating them is what a credit-multiplier stop does.

Also houses the wolves — Geri and Freki — the hard floors that fire without raven synthesis.
They are deliberately few and deliberately blunt.

Pure apart from the OHLCV frame handed to it: no fetching, no IO.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _cfg(name, default):
    return getattr(config, name, default)


# ── Small numeric helpers (plain lists; no pandas dependency) ────────────────────
def _closes(o) -> List[float]:
    try:
        return [float(x) for x in o["Close"].tolist()]
    except Exception:
        return [float(x) for x in (o.get("close") or [])]


def _series(o, key) -> List[float]:
    try:
        return [float(x) for x in o[key].tolist()]
    except Exception:
        return [float(x) for x in (o.get(key.lower()) or [])]


def _atr(highs, lows, closes, n=14) -> Optional[float]:
    if len(closes) < 2:
        return None
    trs = []
    for i in range(max(1, len(closes) - n), len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return (sum(trs) / len(trs)) if trs else None


def _ema(vals: Sequence[float], span: int) -> List[float]:
    if not vals:
        return []
    a = 2.0 / (span + 1.0)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(a * float(v) + (1 - a) * out[-1])
    return out


def _rsi(closes: Sequence[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _adx(highs, lows, closes, n=14) -> Optional[float]:
    """Trend strength. Below 20 is range-bound — the premium seller's friend. Rising above
    25 means a trend is forming, and a trend forming AGAINST the position is the danger."""
    if len(closes) < n * 2:
        return None
    pdm, ndm, trs = [], [], []
    for i in range(1, len(closes)):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < n:
        return None
    dxs = []
    for j in range(n, len(trs) + 1):
        tr = sum(trs[j - n:j]) or 1e-9
        pdi = 100 * sum(pdm[j - n:j]) / tr
        ndi = 100 * sum(ndm[j - n:j]) / tr
        s = pdi + ndi
        dxs.append(100 * abs(pdi - ndi) / s if s else 0.0)
    return sum(dxs[-n:]) / len(dxs[-n:]) if dxs else None


# ── The wolves ──────────────────────────────────────────────────────────────────
def check_wolves(trade: Dict, data: Dict) -> Dict:
    """Hard floors. These fire without raven synthesis and nothing overrides them.

    Deliberately few. Every condition here is one where the market structure has changed,
    not merely moved against the position.
    """
    out = {"gap_event": False, "delta_breach": False, "earnings_in_window": False,
           "blocking_news": False, "hard_floor_loss": False, "any_wolf": False, "detail": []}

    ks = trade.get("short_strike")
    highs, lows, closes = data.get("highs") or [], data.get("lows") or [], data.get("closes") or []
    price = data.get("current_price")

    # Gap through the short strike by more than 1.5 ATR — an event, not a trend.
    #
    # SIGNED BY WHICH SIDE WAS SOLD. This tested `price < strike` and a DOWNWARD gap only,
    # which is the danger for a bull put and the opposite of the danger for a bear call: a
    # short call is threatened by price gapping UP through the strike. Left unsigned, this
    # wolf would sit silent through exactly the event it exists to catch on every call-side
    # position — and the board is now mostly bear calls.
    atr = _atr(highs, lows, closes) if closes else None
    if ks and atr and len(closes) >= 2 and price is not None:
        short_is_call = "call" in (trade.get("strategy") or "").lower() or \
                        "condor" in (trade.get("strategy") or "").lower()
        gap = (price - closes[-2]) if short_is_call else (closes[-2] - price)
        through = (price > float(ks)) if short_is_call else (price < float(ks))
        mult = float(_cfg("WOLF_GAP_ATR_MULT", 1.5))
        if through and gap > mult * atr:
            out["gap_event"] = True
            out["detail"].append(
                f"Gapped {gap:.2f} ({gap/atr:.1f} ATR) through the {ks:g} strike overnight.")

    # The market now prices the short strike as more likely than not to finish ITM. At that
    # point the spread has stopped behaving like the thing that was sold.
    d = data.get("current_delta")
    thr = float(_cfg("WOLF_DELTA_THRESHOLD", 0.55))
    if d is not None and abs(float(d)) >= thr:
        out["delta_breach"] = True
        out["detail"].append(
            f"Short delta {abs(float(d)):.2f} at or past {thr:.2f} — the spread is now a "
            f"directional bet, not the premium sale that was opened.")

    ec = data.get("earnings_check") or {}
    if ec.get("in_window") and not trade.get("earnings_in_window_at_entry"):
        out["earnings_in_window"] = True
        out["detail"].append("An earnings date now falls inside the remaining window and did "
                             "not at entry — the risk profile changed structurally.")

    if (data.get("news_sentiment") or "").upper() == "BLOCKING":
        out["blocking_news"] = True
        out["detail"].append("Blocking-level news — the thesis is invalidated, not delayed.")

    # Backstop only. The credit-multiplier stop was the primary close rule and it fired at
    # entry on bid-ask spread alone; it survives here as a wide floor, not a trigger.
    mark, credit = data.get("mark"), trade.get("actual_fill_credit")
    floor = float(_cfg("WOLF_STOP_MULTIPLIER", 3.0))
    if mark is not None and credit and float(credit) > 0 and float(mark) >= floor * float(credit):
        out["hard_floor_loss"] = True
        out["detail"].append(
            f"Mark {float(mark):.2f} is {float(mark)/float(credit):.1f}x the {float(credit):.2f} "
            f"credit, past the {floor:g}x hard floor.")

    out["any_wolf"] = any(out[k] for k in
                          ("gap_event", "delta_breach", "earnings_in_window",
                           "blocking_news", "hard_floor_loss"))
    return out


# ── Signal readers ──────────────────────────────────────────────────────────────
def read_support(trade: Dict, data: Dict) -> Dict:
    """The primary signal. Is the level that sheltered the short strike still holding?

    A single close below is BREACH, not VIOLATED. Levels get wicked constantly; requiring
    confirmation on volume is what separates a break from noise.
    """
    sp = trade.get("support_level_at_entry")
    price = data.get("current_price")
    highs, lows, closes = data.get("highs") or [], data.get("lows") or [], data.get("closes") or []
    vols = data.get("volumes") or []
    if sp is None or price is None or not closes:
        return {"status": "UNKNOWN",
                "reason": "no support level recorded at entry — Huginn is blind to structure"}

    sp = float(sp)
    atr = _atr(highs, lows, closes) or 0.0
    dist = price - sp
    recent = closes[-5:]
    below = sum(1 for c in recent if c < sp)

    vol_ratio = None
    if vols and len(vols) >= 20:
        breach_v = [v for c, v in zip(closes[-5:], vols[-5:]) if c < sp]
        avg = sum(vols[-20:]) / 20
        if breach_v and avg:
            vol_ratio = (sum(breach_v) / len(breach_v)) / avg

    confirms = int(_cfg("HUGINN_SUPPORT_BREACH_CONFIRMS", 2))
    if dist > atr:
        status = "INTACT"
    elif dist > 0:
        status = "NEAR"
    elif below >= confirms and (vol_ratio or 0) > 1.2:
        status = "VIOLATED"
    elif below >= 1:
        status = "BREACH"
    else:
        status = "WICKING"
    return {"status": status, "support_price": round(sp, 2), "current_price": round(price, 2),
            "atr_units_above": round(dist / atr, 2) if atr else None,
            "closes_below_count": below,
            "volume_on_breach": round(vol_ratio, 2) if vol_ratio else None}


def read_ma_stack(data: Dict) -> Dict:
    closes = data.get("closes") or []
    if len(closes) < 50:
        return {"status": "UNKNOWN"}
    p = closes[-1]
    e20, e50 = _ema(closes, 20)[-1], _ema(closes, 50)[-1]
    e200 = _ema(closes, 200)[-1] if len(closes) >= 200 else None
    if e200 and p < e200:
        return {"status": "BROKEN", "detail": "below the 200-day — structural breakdown"}
    if p < e50:
        return {"status": "WEAK", "detail": "below the 50-day"}
    if p < e20:
        return {"status": "CONSOLIDATING", "detail": "below the 20-day but above the 50-day"}
    return {"status": "STRONG", "detail": "above the 20- and 50-day"}


def read_rsi_divergence(data: Dict, lookback: int = 14) -> Dict:
    """Price making a lower low while RSI makes a higher low. On a position under pressure
    this is the single most useful "hold" signal available: price is still falling but the
    force behind it is not."""
    closes = data.get("closes") or []
    if len(closes) < lookback * 3:
        return {"divergence": None}
    mid = len(closes) - lookback
    p_prev, p_now = min(closes[mid - lookback:mid]), min(closes[mid:])
    r_prev, r_now = _rsi(closes[:mid]), _rsi(closes)
    if r_prev is None or r_now is None:
        return {"divergence": None}
    if p_now < p_prev and r_now > r_prev:
        return {"divergence": "bullish", "rsi_prev": round(r_prev, 1), "rsi_now": round(r_now, 1)}
    if p_now > p_prev and r_now < r_prev:
        return {"divergence": "bearish", "rsi_prev": round(r_prev, 1), "rsi_now": round(r_now, 1)}
    return {"divergence": None}


def read_obv(data: Dict, window: int = 20) -> Dict:
    """OBV rising while price is flat or falling is accumulation under the surface."""
    closes, vols = data.get("closes") or [], data.get("volumes") or []
    if len(closes) < window + 1 or len(vols) < window + 1:
        return {"trend": None}
    obv, cur = [], 0.0
    for i in range(1, len(closes)):
        cur += vols[i] if closes[i] > closes[i - 1] else (-vols[i] if closes[i] < closes[i - 1] else 0)
        obv.append(cur)
    if len(obv) < window:
        return {"trend": None}
    rising = obv[-1] > obv[-window]
    px_rising = closes[-1] > closes[-window]
    kind = ("accumulation" if rising and not px_rising else
            "distribution" if (not rising) and px_rising else
            "confirming" if rising == px_rising else None)
    return {"trend": "rising" if rising else "falling", "divergence": kind}


def read_volume_quality(data: Dict, window: int = 20) -> Dict:
    """Volume on up days versus down days. Higher on up days is accumulation."""
    closes, vols = data.get("closes") or [], data.get("volumes") or []
    if len(closes) < window + 1:
        return {"bias": None}
    up = [v for i, v in enumerate(vols[-window:], start=len(vols) - window)
          if i > 0 and closes[i] > closes[i - 1]]
    dn = [v for i, v in enumerate(vols[-window:], start=len(vols) - window)
          if i > 0 and closes[i] < closes[i - 1]]
    if not up or not dn:
        return {"bias": None}
    ratio = (sum(up) / len(up)) / ((sum(dn) / len(dn)) or 1e-9)
    return {"bias": "accumulation" if ratio > 1.1 else ("distribution" if ratio < 0.9 else "balanced"),
            "up_down_volume_ratio": round(ratio, 2)}


def read_strike_buffer(trade: Dict, data: Dict) -> Dict:
    """How many ATRs of room remain between price and the short strike."""
    ks, price = trade.get("short_strike"), data.get("current_price")
    atr = _atr(data.get("highs") or [], data.get("lows") or [], data.get("closes") or [])
    if not ks or price is None or not atr:
        return {"atr_buffer": None}
    return {"atr_buffer": round((price - float(ks)) / atr, 2), "atr": round(atr, 2)}


# ── Synthesis ───────────────────────────────────────────────────────────────────
def evaluate(trade: Dict, data: Dict) -> Dict:
    """Wolves first, then the signal stack, then a thesis verdict.

    Returns thesis_status in {WOLF, INTACT, EXCEEDED, UNDER_PRESSURE, VIOLATED} with a
    plain-English reason and the full signal readings for audit.
    """
    wolves = check_wolves(trade, data)
    if wolves["any_wolf"]:
        return {"thesis_status": "WOLF", "confidence": "high",
                "reason": " ".join(wolves["detail"]) or "Hard-floor condition.",
                "signal_readings": {}, "wolf_check": wolves}

    s = {
        "support": read_support(trade, data),
        "ma_stack": read_ma_stack(data),
        "rsi": _rsi(data.get("closes") or []),
        "rsi_divergence": read_rsi_divergence(data),
        "obv": read_obv(data),
        "volume_quality": read_volume_quality(data),
        "adx": _adx(data.get("highs") or [], data.get("lows") or [], data.get("closes") or []),
        "strike_buffer": read_strike_buffer(trade, data),
    }

    sup = s["support"]["status"]
    ma = s["ma_stack"].get("status")
    buf = s["strike_buffer"].get("atr_buffer")
    div = s["rsi_divergence"].get("divergence")

    bad = 0
    notes = []
    if sup == "VIOLATED":
        bad += 2; notes.append(f"support at {s['support'].get('support_price')} broke on volume")
    elif sup == "BREACH":
        bad += 1; notes.append("one close below support, not yet confirmed")
    if ma == "BROKEN":
        bad += 2; notes.append("price below the 200-day")
    elif ma == "WEAK":
        bad += 1; notes.append("price below the 50-day")
    if buf is not None and buf < 0:
        bad += 2; notes.append(f"price is {abs(buf):.1f} ATR below the short strike")
    elif buf is not None and buf < 1.0:
        bad += 1; notes.append(f"only {buf:.1f} ATR of room above the short strike")
    if s["obv"].get("divergence") == "distribution":
        bad += 1; notes.append("OBV falling while price holds — distribution")
    if s["volume_quality"].get("bias") == "distribution":
        bad += 1; notes.append("heavier volume on down days")
    adx = s["adx"]
    if adx and adx > 25 and buf is not None and buf < 1.0:
        bad += 1; notes.append(f"ADX {adx:.0f} — a trend is forming against the position")

    # ── Time remaining changes what the same chart means ──
    # A strike breached with 40 days left is a problem the position has time to solve; the
    # identical chart with 5 days left is nearly settled. Reading structure without the clock
    # treats those as the same fact, which is how a thesis gets abandoned while it still has
    # most of its life to work.
    dte = data.get("dte_remaining")
    iv = data.get("current_iv") or trade.get("iv_at_entry")
    s["horizon"] = {"dte_remaining": dte}
    if dte is not None and iv and data.get("current_price") and trade.get("short_strike"):
        try:
            from analysis.horizon import expected_move, sigmas_away, classify_reach
            em = expected_move(data["current_price"], float(iv), int(dte))
            sig = sigmas_away(data["current_price"], float(trade["short_strike"]), em)
            s["horizon"] = {"dte_remaining": dte, "expected_move": round(em, 2) if em else None,
                            "strike_sigmas": round(sig, 2) if sig is not None else None,
                            "strike_reach": classify_reach(sig)}
            if sig is not None:
                # `sig` is (strike - price) / expected_move. Convert to a SAFETY buffer in
                # expected moves, signed so positive always means "the position is winning",
                # whichever side was sold. Reading the raw sig as safety told a deeply ITM
                # short put that time was on its side.
                short_is_call = "call" in (trade.get("strategy") or "").lower()
                em_buffer = sig if short_is_call else -sig
                s["horizon"]["em_buffer"] = round(em_buffer, 2)

                near = int(_cfg("HUGINN_LATE_DTE", 10))
                if dte <= near and em_buffer > 0.25:
                    # Late and still on the right side: time is now an ally, not a risk.
                    bad = max(0, bad - 1)
                    notes.append(f"only {dte}d left with the strike still {em_buffer:.1f} "
                                 f"expected moves away — time is on your side")
                elif dte <= near and em_buffer <= -0.5:
                    bad += 1
                    notes.append(f"{dte}d left and price is {abs(em_buffer):.1f} expected "
                                 f"moves through the strike — little time to recover")
                elif dte > near * 3 and em_buffer < 0 and bad >= 2:
                    # Early and under water: the position still owns most of its life.
                    bad = max(0, bad - 1)
                    notes.append(f"{dte}d still remaining for the thesis to work")
        except Exception:
            pass

    # A bullish divergence on a position under pressure is the strongest single argument that
    # the move is exhausting. It pulls a verdict back one step; it never clears a violation
    # outright, because momentum stabilising below a broken level is still below it.
    softened = False
    if div == "bullish" and bad >= 2:
        bad -= 1
        softened = True
        notes.append("bullish RSI divergence — the selling is losing force")

    if bad >= 4:
        status = "VIOLATED"
    elif bad >= 2:
        status = "UNDER_PRESSURE"
    elif (buf is not None and buf > 2.0 and sup in ("INTACT", "UNKNOWN")
          and ma == "STRONG"):
        # EXCEEDED means the thesis has been PROVEN, not merely that the strike is distant.
        # Requiring a strong MA stack matters: live QCOM sat 2.1 ATR above its strike while
        # trading below its own 50-day, and reading that as "exceeded" would have claimed a
        # weakening trend as a win.
        status = "EXCEEDED"
    else:
        status = "INTACT"

    if status == "EXCEEDED":
        reason = (f"Thesis proven early: {buf:.1f} ATR above the short strike with price "
                  f"above the 20- and 50-day.")
    elif status == "INTACT":
        bits = []
        if buf is not None:
            bits.append(f"{buf:.1f} ATR of room above the short strike")
        if s["ma_stack"].get("detail"):
            bits.append(s["ma_stack"]["detail"])
        # Lead with what is actually holding, then note any caveat — the old wording produced
        # "Structure is holding: below the 50-day", which contradicts itself.
        head = "No structural break. " + (", ".join(bits) if bits else "signals mixed")
        reason = head.rstrip(". ") + ("" if not notes else f". Watching: {notes[0]}") + "."
    else:
        reason = ("; ".join(notes[:3]) or "structure weakening").capitalize() + "."
    return {"thesis_status": status,
            "confidence": "high" if len(notes) >= 2 else "medium",
            "reason": reason, "signal_readings": s, "wolf_check": wolves,
            "pressure_score": bad, "divergence_softened": softened}
