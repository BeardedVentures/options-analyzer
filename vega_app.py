#!/usr/bin/env python3
"""
vega_app.py — VEGA local web UI (the usable cockpit). v3.1.

Stdlib-only local web app (no pip installs). v3.1 rebuild onto the working v2 baseline:

  ENGINE-FIRST (A1): reads logs/scan_latest.json (the full main.py engine payload:
      true_pop, implied_pop, edge_score, component_breakdown, ...) as the single source
      of truth. Falls back to the lighter legacy candidates artifact only when the engine
      artifact is missing/empty — and that fast path is labelled PROVISIONAL in the UI.
  POP SEMANTICS (A2): board headlines true POP, shows implied POP secondary, and surfaces
      edge = (true_pop − implied_pop) in points as the decision metric. No raw delta-as-POP
      in the primary board semantics.
  GATE 1 TIERING (A3): gate1_tier() drives validated/provisional/unproven badges and
      auto-reverts to provisional if live calibration drifts once the sample is meaningful.
  VISUAL: dark "market terminal" theme per the design mockup (calm charcoal, one meaning per
      colour, tabular figures, gate dots, grouped columns, hero card, confidence scorecard,
      score composition), and ?view= routing across dashboard / board / open / history / research.

Nothing here places real orders or spends money. Runs entirely on localhost.
RUN:  python vega_app.py   (or double-click run_vega_app.bat)
"""

from __future__ import annotations

import glob
import html
import json
import logging
import math
import os
import sys
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import NormalDist
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from analysis import outcome_logger as ol
from paper_desk import compute_stats, _latest_candidates
try:
    import clv_tracker
except Exception:  # never let the learning module break the cockpit
    clv_tracker = None
try:
    import environment as _environment
except Exception:
    _environment = None

BASE = Path(__file__).resolve().parent
CAND_DIR = BASE / "output" / "candidates"
SCAN_LATEST = BASE / "logs" / "scan_latest.json"
LOTTERY_LATEST = BASE / "logs" / "lottery_latest.json"
HOST, PORT = "127.0.0.1", 8765
SCAN = {"min_dte": 25, "max_dte": 45, "delta_min": 0.12, "delta_max": 0.35, "top": 3,
        "max_width": float(getattr(config, "MAX_SPREAD_WIDTH", 5))}
_scan_status = {"running": False, "msg": "", "at": None}

# The Copilot drawer is rendered per row and therefore cannot see the rest of the board or
# the market context. view_today publishes both here immediately before board_table() builds
# the rows, so "Other top ideas" and "Market snapshot" have something to read. Kept as plain
# module state rather than threaded through six signatures; the server is single-threaded per
# request and each render repopulates it.
_COPILOT_PEERS: list = []
_COPILOT_CTX: dict = {}

# A cross-venue fetch that fails must narrow this page, never take the cockpit down with it —
# so those failures are logged rather than raised, and this is where they go.
logger = logging.getLogger(__name__)

VIEWS = ("today", "track", "open", "bitcoin", "history", "lottery")
IVR_MIN = getattr(config, "MIN_IV_RANK", 45)
# The two edge-score bands the density funnel and the status ladder both count against.
# Defined once so the bar, the cards and the engine's scan_summary cannot drift apart.
HIGH_EDGE = getattr(config, "HIGH_EDGE_SCORE", 65)
EXCEPTIONAL_EDGE = getattr(config, "EXCEPTIONAL_EDGE_SCORE", 80)
# Below this many resolved directional claims the grid is a horizon, not a record, and says so.
_DIRECTION_LEARNING_MIN = 5

# Component max points for the score composition panel (mirrors edge_calculator).
# The first six sum to a 100-point base; skew (0-15) and post-earnings (0-5) are
# additive bonuses on top, so the displayed edge_score can exceed 100. All eight
# are listed here so the "no black box" panel reconciles exactly to the headline.
EDGE_COMPONENTS = [
    ("vrp", "VRP / premium", 30),
    ("true_pop_edge", "True-POP edge", 25),
    ("technical", "Technical", 20),
    ("fundamentals", "Fundamentals", 10),
    ("news", "News", 10),
    ("earnings_safety", "Earnings safety", 5),
]
# Additive bonus components (beyond the 100-point base).
EDGE_BONUSES = [
    ("skew", "IV skew (bonus)", 15),
    ("post_earnings", "Post-earnings (bonus)", 5),
]

# ── Gate 1 tiering (A3). Backtest sign-off is config-driven; live fills can revert it. ──
GATE1_BACKTEST_PASSED = bool(getattr(config, "GATE1_BACKTEST_PASSED", False))
GATE1_CALIB_TOL_PP = float(getattr(config, "GATE1_CALIB_TOL_PP", 8.0))
GATE1_MIN_LIVE = int(getattr(config, "GATE1_MIN_LIVE", 30))


def gate1_tier(backtest_passed: bool, live_calib_pp, n_live: int) -> str:
    """Resolve the confidence tier for edge_score / VRP signals.

    VALIDATED once the ORATS backtest passes AND live fills (if a meaningful sample exists)
    do not contradict it beyond tolerance; PROVISIONAL otherwise. See build doc §0.5 A3.
    """
    if not backtest_passed:
        return "PROVISIONAL"
    if n_live >= GATE1_MIN_LIVE and live_calib_pp is not None and abs(live_calib_pp) > GATE1_CALIB_TOL_PP:
        return "PROVISIONAL"  # live fills contradict the backtest — revert to amber
    return "VALIDATED"


TIER_LABEL = {"VALIDATED": "Validated", "PROVISIONAL": "Provisional", "UNPROVEN": "Unproven"}
TIER_NOTE = {
    "VALIDATED": "backtested + live-confirmed",
    "PROVISIONAL": "estimate · under calibration",
    "UNPROVEN": "not yet validated",
}


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def tier_badge(tier: str) -> str:
    t = tier.lower()
    return (f'<span class="tier tier-{t}" title="{esc(TIER_NOTE.get(tier, ""))}">'
            f'{esc(TIER_LABEL.get(tier, tier))}</span>')


def market_status():
    """(is_open, label) for US equity options, in ET."""
    def _fmt_et(ts):
        return ts.strftime("%I:%M %p").lstrip("0")

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # ZoneInfo unavailable — default to CLOSED so off-hours scans never fire silently.
        return False, "Timezone library unavailable — treating market as closed for safety."
    if now.weekday() >= 5:
        return False, "Weekend — options market closed. Quotes will be empty until Monday 9:30 ET."
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 30:
        return False, f"Pre-market ({_fmt_et(now)} ET) — options open at 9:30 ET. Rescan after open."
    if hm >= 16 * 60:
        return False, f"After hours ({_fmt_et(now)} ET) — options closed at 4:00 ET. Quotes may be stale."
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Visual P&L payoff diagram (bull put spread) — inline SVG, dark theme
# ─────────────────────────────────────────────────────────────────────────────
def payoff_svg(short, long_, credit, width, spot=None, w=180, h=58,
               mode="candidate", cushion_pct=None, unrealized=None, current_mark=None):
    """The spread's P/L at expiry, in two modes.

    CANDIDATE mode answers "how much room does this have?" — the cushion between spot and the
    short strike, which is the number that decides whether to open it.

    POSITION mode answers "where is this now?" — unrealized P/L and the current mark.

    The mark is deliberately NOT plotted as a point on the curve. This curve is P/L AT EXPIRY,
    and a position's mark today includes time value that the curve does not describe; drawing
    it on the line would assert the trade is at a P/L it is not at. It gets a label instead.
    """
    short = _f(short); long_ = _f(long_); credit = _f(credit); width = _f(width)
    if None in (short, long_, credit, width) or width <= 0:
        return ""
    maxpl = credit * 100.0
    minpl = -(width - credit) * 100.0
    lo = long_ - max(width * 0.6, 1)
    hi = short + max(width * 0.9, 1)
    sp = _f(spot)
    if sp is not None:
        hi = max(hi, sp * 1.01)
    pad = 6
    span = (hi - lo) or 1
    rng = (maxpl - minpl) or 1

    def px(p): return pad + (p - lo) / span * (w - 2 * pad)
    def py(v): return (h - pad) - (v - minpl) / rng * (h - 2 * pad)

    be = short - credit
    zy = py(0)
    loss_pts = f"{px(lo):.1f},{zy:.1f} {px(lo):.1f},{py(minpl):.1f} {px(long_):.1f},{py(minpl):.1f} {px(be):.1f},{zy:.1f}"
    prof_pts = f"{px(be):.1f},{zy:.1f} {px(short):.1f},{py(maxpl):.1f} {px(hi):.1f},{py(maxpl):.1f} {px(hi):.1f},{zy:.1f}"
    line_pts = " ".join(f"{px(p):.1f},{py(v):.1f}" for p, v in
                        [(lo, minpl), (long_, minpl), (short, maxpl), (hi, maxpl)])
    spot_line = ""
    if sp is not None and lo <= sp <= hi:
        sx = px(sp)
        spot_line = (f'<line x1="{sx:.1f}" y1="{pad}" x2="{sx:.1f}" y2="{h-pad}" '
                     f'stroke="#4E8EF5" stroke-width="1" stroke-dasharray="2,2"/>')
    # The two endpoints the break-even dot was always missing its counterparts to. A payoff
    # diagram with only a break-even marked shows where the trade turns and not what it is
    # risking to get there — and on a credit spread the loss endpoint is the larger number.
    endpoints = (f'<circle cx="{px(short):.1f}" cy="{py(maxpl):.1f}" r="2.4" fill="#00C97A"/>'
                 f'<circle cx="{px(long_):.1f}" cy="{py(minpl):.1f}" r="2.4" fill="#F0455A"/>')

    overlay = ""
    if mode == "position":
        un = _f(unrealized)
        if un is not None:
            overlay += (f'<text x="{pad}" y="{pad+7}" font-size="9" font-weight="700" '
                        f'fill="{"#00C97A" if un >= 0 else "#F0455A"}">${un:+,.0f}</text>')
        mk = _f(current_mark)
        if mk is not None:
            overlay += (f'<text x="{w-pad}" y="{pad+7}" font-size="8.5" text-anchor="end" '
                        f'fill="#7f8794">@${mk:.2f}</text>')
    else:
        cush = _f(cushion_pct)
        if cush is None and sp and sp > 0:
            cush = (sp - short) / sp          # bull put: room between spot and the short strike
        if cush is not None:
            overlay += (f'<text x="{pad}" y="{pad+7}" font-size="9" font-weight="600" '
                        f'fill="#7f8794">{cush*100:.1f}% cushion</text>')

    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="vertical-align:middle">'
            f'<polygon points="{loss_pts}" fill="#F0455A" fill-opacity="0.16"/>'
            f'<polygon points="{prof_pts}" fill="#00C97A" fill-opacity="0.16"/>'
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" stroke="#2a2f3a" stroke-width="1"/>'
            f'<polyline points="{line_pts}" fill="none" stroke="#7f8794" stroke-width="1.4"/>'
            f'{endpoints}'
            f'<circle cx="{px(be):.1f}" cy="{zy:.1f}" r="2.6" fill="#F0B429"/>'
            f'{spot_line}{overlay}</svg>')


# ─────────────────────────────────────────────────────────────────────────────
# Scan + re-price open positions (lazy imports; needs yfinance → runs on the tower)
# ─────────────────────────────────────────────────────────────────────────────
def _reprice_open_positions(fetcher):
    rows = ol.load_records()
    open_ = [r for r in rows if r.get("status") == "open"]
    by_tk = {}
    for r in open_:
        by_tk.setdefault(r.get("ticker"), []).append(r)
    for tk, positions in by_tk.items():
        try:
            chain = fetcher.get_options_chain(tk, 0, 200)
            idx = {(round(float(o["strike"]), 2), o["expiration"]): o for o in chain}
            for r in positions:
                exp = r.get("expiration")
                s = idx.get((round(float(r["short_strike"]), 2), exp))
                l = idx.get((round(float(r["long_strike"]), 2), exp))
                if s and l:
                    mark = round(float(s.get("mid") or 0) - float(l.get("mid") or 0), 2)
                    ol.set_mark(r["id"], mark)
        except Exception:
            continue


def run_scan_now() -> str:
    """Fast local rescan via vega_candidates (yfinance-only). PROVISIONAL — lacks edge_score."""
    _scan_status.update(running=True, msg="Scanning live chains…", at=datetime.now())
    try:
        import vega_candidates as vc
        from data import fetcher
        fetcher.clear_cache()
        rows = []
        for w in config.WATCHLIST:
            tk = w["ticker"]
            try:
                px = fetcher.get_price_data(tk, period="5d")
                price = float(px["Close"].iloc[-1]) if px is not None and not px.empty else None
                if not price:
                    continue
                puts = fetcher.get_options_chain(tk, SCAN["min_dte"], SCAN["max_dte"])
                ctx = vc.vol_context(tk, puts, price)
                cands = vc.build_candidates(tk, puts, price, SCAN["delta_min"], SCAN["delta_max"],
                                            SCAN["max_width"])[: SCAN["top"]]
                rows.append({"ticker": tk, "price": price, "ctx": ctx, "candidates": cands})
            except Exception as e:
                rows.append({"ticker": tk, "price": None, "ctx": {}, "candidates": [], "error": str(e)})
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        meta = {"stamp": datetime.now().strftime("%Y-%m-%d %H:%M"), "source": "yfinance",
                **{k: SCAN[k] for k in ("min_dte", "max_dte", "delta_min", "delta_max")}}
        CAND_DIR.mkdir(parents=True, exist_ok=True)
        (CAND_DIR / f"candidates_{stamp}.json").write_text(
            json.dumps({"meta": meta, "rows": rows}, indent=2, default=str), encoding="utf-8")
        _reprice_open_positions(fetcher)
        n = sum(len(r["candidates"]) for r in rows)
        priced = sum(1 for r in rows if r.get("price"))
        _scan_status.update(running=False, at=datetime.now(),
                            msg=f"Fast rescan complete — {n} candidates across {priced}/{len(rows)} names "
                                f"(provisional; run the full engine for edge scores).")
        return _scan_status["msg"]
    except Exception as e:
        _scan_status.update(running=False, msg=f"Scan failed: {e}", at=datetime.now())
        return _scan_status["msg"]


# ─────────────────────────────────────────────────────────────────────────────
# Board loading — engine-first (A1), legacy fallback marked provisional
# ─────────────────────────────────────────────────────────────────────────────
def _model_pop_estimate(spot, breakeven, dte, atm_iv=None, rv=None):
    """Zero-drift lognormal P(spot_at_exp > breakeven). Used ONLY for the legacy fallback,
    where the engine's true_pop is unavailable. Always rendered as an estimate."""
    s = _f(spot); be = _f(breakeven); t = _f(dte)
    if None in (s, be, t) or s <= 0 or be <= 0 or t <= 0:
        return None
    vols = [v for v in (_f(atm_iv), _f(rv)) if v and v > 0]
    if not vols:
        return None
    sigma = max(0.05, sum(vols) / len(vols))
    denom = sigma * math.sqrt(max(t, 1.0) / 365.0)
    if denom <= 0:
        return None
    z = math.log(s / be) / denom
    return max(0.01, min(0.99, NormalDist().cdf(z)))


def _geometry(t):
    """Strategy-aware geometry -> normalized fields for any DEFINED-RISK credit structure
       (bull put, bear call, iron condor). No undefined-risk structures are produced here."""
    s=(t.get("strategy") or "").lower()
    credit_ps=_f(t.get("credit_per_share"),0.0) or 0.0
    credit_usd=_f(t.get("credit_usd"), credit_ps*100)
    price=_f(t.get("current_price"))
    if "condor" in s:
        ps=_f(t.get("put_short_strike")); pl=_f(t.get("put_long_strike"))
        cs=_f(t.get("call_short_strike")); cl=_f(t.get("call_long_strike"))
        wput=abs(ps-pl) if None not in (ps,pl) else None
        wcall=abs(cs-cl) if None not in (cs,cl) else None
        width=max([w for w in (wput,wcall) if w] or [0])
        max_loss=_f(t.get("max_loss_usd"), (width*100-(credit_usd or 0)) if width else None)
        bes=[]
        if ps is not None: bes.append(ps-credit_ps)
        if cs is not None: bes.append(cs+credit_ps)
        cushion=(min((price-ps)/price,(cs-price)/price)*100) if (price and ps and cs) else None
        struct=(f"{ps:g}/{pl:g}P  {cs:g}/{cl:g}C") if None not in (ps,pl,cs,cl) else "iron condor"
        return dict(strat_type="iron_condor",risk_dir="both",short=cs,long=cl,put_short=ps,put_long=pl,
                    call_short=cs,call_long=cl,width=width,credit_ps=credit_ps,credit_usd=credit_usd,
                    max_loss=max_loss,breakevens=bes,cushion=cushion,structure=struct)
    if "bear" in s and "call" in s:
        short=_f(t.get("short_strike")); long_=_f(t.get("long_strike"))
        width=abs(short-long_) if None not in (short,long_) else _f(t.get("spread_width"))
        max_loss=_f(t.get("max_loss_usd"), (width*100-(credit_usd or 0)) if width else None)
        be=(short+credit_ps) if short is not None else None
        cushion=((short-price)/price*100) if (price and short) else None
        struct=(f"{short:g}/{long_:g}C") if None not in (short,long_) else "bear call"
        return dict(strat_type="bear_call",risk_dir="upside",short=short,long=long_,put_short=None,put_long=None,
                    call_short=short,call_long=long_,width=width,credit_ps=credit_ps,credit_usd=credit_usd,
                    max_loss=max_loss,breakevens=[be] if be is not None else [],cushion=cushion,structure=struct)
    short=_f(t.get("short_strike")); long_=_f(t.get("long_strike"))
    width=abs(short-long_) if None not in (short,long_) else _f(t.get("spread_width"))
    max_loss=_f(t.get("max_loss_usd"), (width*100-(credit_usd or 0)) if width else None)
    be=(short-credit_ps) if short is not None else None
    cushion=((price-be)/price*100) if (price and be) else None
    struct=(f"{short:g}/{long_:g}P") if None not in (short,long_) else "bull put"
    return dict(strat_type="bull_put",risk_dir="downside",short=short,long=long_,put_short=short,put_long=long_,
                call_short=None,call_long=None,width=width,credit_ps=credit_ps,credit_usd=credit_usd,
                max_loss=max_loss,breakevens=[be] if be is not None else [],cushion=cushion,structure=struct)


_SVGH='<svg width="%d" height="%d" viewBox="0 0 %d %d" style="vertical-align:middle">'
def _payoff_bear(short,long_,credit,width,spot,w,h):
    short=_f(short);long_=_f(long_);credit=_f(credit);width=_f(width)
    if None in (short,long_,credit,width) or width<=0: return ""
    maxpl=credit*100.0; minpl=-(width-credit)*100.0
    lo=short-max(width*0.9,1); hi=long_+max(width*0.6,1); sp=_f(spot)
    if sp is not None: lo=min(lo,sp*0.99)
    pad=6; span=(hi-lo) or 1; rng=(maxpl-minpl) or 1
    def px(pp): return pad+(pp-lo)/span*(w-2*pad)
    def py(v): return (h-pad)-(v-minpl)/rng*(h-2*pad)
    be=short+credit; zy=py(0)
    line=" ".join(f"{px(pp):.1f},{py(v):.1f}" for pp,v in [(lo,maxpl),(short,maxpl),(long_,minpl),(hi,minpl)])
    prof=f"{px(lo):.1f},{zy:.1f} {px(lo):.1f},{py(maxpl):.1f} {px(short):.1f},{py(maxpl):.1f} {px(be):.1f},{zy:.1f}"
    loss=f"{px(be):.1f},{zy:.1f} {px(long_):.1f},{py(minpl):.1f} {px(hi):.1f},{py(minpl):.1f} {px(hi):.1f},{zy:.1f}"
    sl=""
    if sp is not None and lo<=sp<=hi:
        sx=px(sp); sl=f'<line x1="{sx:.1f}" y1="{pad}" x2="{sx:.1f}" y2="{h-pad}" stroke="#4E8EF5" stroke-width="1" stroke-dasharray="2,2"/>'
    return ((_SVGH%(w,h,w,h))+f'<polygon points="{prof}" fill="#00C97A" fill-opacity="0.16"/>'
            f'<polygon points="{loss}" fill="#F0455A" fill-opacity="0.16"/>'
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" stroke="#2a2f3a" stroke-width="1"/>'
            f'<polyline points="{line}" fill="none" stroke="#7f8794" stroke-width="1.4"/>'
            f'<circle cx="{px(be):.1f}" cy="{zy:.1f}" r="2.6" fill="#F0B429"/>{sl}</svg>')
def _payoff_condor(c,w,h):
    ps=_f(c.get("put_short"));pl=_f(c.get("put_long"));cs=_f(c.get("call_short"));cl=_f(c.get("call_long"))
    credit=_f(c.get("credit_ps"));price=_f(c.get("price"))
    if None in (ps,pl,cs,cl,credit): return ""
    width=max(abs(ps-pl),abs(cs-cl)); maxpl=credit*100.0; minpl=-(width-credit)*100.0
    lo=pl-max(width*0.4,1); hi=cl+max(width*0.4,1); pad=6; span=(hi-lo) or 1; rng=(maxpl-minpl) or 1
    def px(pp): return pad+(pp-lo)/span*(w-2*pad)
    def py(v): return (h-pad)-(v-minpl)/rng*(h-2*pad)
    zy=py(0); bel=ps-credit; ber=cs+credit
    line=" ".join(f"{px(pp):.1f},{py(v):.1f}" for pp,v in [(lo,minpl),(pl,minpl),(ps,maxpl),(cs,maxpl),(cl,minpl),(hi,minpl)])
    profmid=f"{px(bel):.1f},{zy:.1f} {px(ps):.1f},{py(maxpl):.1f} {px(cs):.1f},{py(maxpl):.1f} {px(ber):.1f},{zy:.1f}"
    lossL=f"{px(lo):.1f},{zy:.1f} {px(pl):.1f},{py(minpl):.1f} {px(bel):.1f},{zy:.1f}"
    lossR=f"{px(ber):.1f},{zy:.1f} {px(cl):.1f},{py(minpl):.1f} {px(hi):.1f},{zy:.1f}"
    sl=""
    if price is not None and lo<=price<=hi:
        sx=px(price); sl=f'<line x1="{sx:.1f}" y1="{pad}" x2="{sx:.1f}" y2="{h-pad}" stroke="#4E8EF5" stroke-width="1" stroke-dasharray="2,2"/>'
    return ((_SVGH%(w,h,w,h))+f'<polygon points="{profmid}" fill="#00C97A" fill-opacity="0.16"/>'
            f'<polygon points="{lossL}" fill="#F0455A" fill-opacity="0.16"/>'
            f'<polygon points="{lossR}" fill="#F0455A" fill-opacity="0.16"/>'
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" stroke="#2a2f3a" stroke-width="1"/>'
            f'<polyline points="{line}" fill="none" stroke="#7f8794" stroke-width="1.4"/>'
            f'<circle cx="{px(bel):.1f}" cy="{zy:.1f}" r="2.4" fill="#F0B429"/>'
            f'<circle cx="{px(ber):.1f}" cy="{zy:.1f}" r="2.4" fill="#F0B429"/>{sl}</svg>')
def payoff_typed(c,w=180,h=58):
    st=c.get("strat_type","bull_put")
    if st=="bear_call": return _payoff_bear(c.get("short"),c.get("long"),c.get("credit_ps"),c.get("width"),c.get("price"),w,h)
    if st=="iron_condor": return _payoff_condor(c,w,h)
    return payoff_svg(c.get("short"),c.get("long"),c.get("credit_ps"),c.get("width"),c.get("price"),w,h)
def _valtag(c):
    return ('<span class="valtag" title="New live calls path — spot-check this trade against your broker before acting">verify</span>'
            if c.get("needs_validation") else "")


def _type_chip(c):
    m={"bull_put":("BPS","Bull put spread"),"bear_call":("BCS","Bear call spread"),
       "iron_condor":("IC","Iron condor"),"long_call":("CALL","Long call")}
    lab,full=m.get(c.get("strat_type","bull_put"),("BPS","spread"))
    return f'<span class="tchip" title="{full}">{lab}</span>'


def _adapt_engine(t: dict) -> dict:
    """Normalize a main.py qualified-trade object into a board card (strategy-aware)."""
    g = _geometry(t)
    short = g["short"]; long_ = g["long"]; width = g["width"]
    credit_ps = g["credit_ps"]; credit_usd = g["credit_usd"]; max_loss = g["max_loss"]
    roi = (credit_usd / max_loss) if (credit_usd and max_loss) else None
    true_pop = _f(t.get("true_pop"))
    implied = _f(t.get("implied_pop"))
    edge_pp = (true_pop - implied) * 100 if (true_pop is not None and implied is not None) else None
    ctw = _f(t.get("credit_to_width_pct"))
    be = g["breakevens"][0] if g["breakevens"] else None
    return {
        # Where the credit came from. Dropping this in the adapter would let an after-hours
        # modelled price reach the board looking exactly like a fillable one, which is the
        # single thing the natural-basis work exists to prevent.
        "fill_basis": t.get("fill_basis"), "quotes_live": t.get("quotes_live"),
        "source": "engine", "ticker": t.get("ticker"), "strategy": t.get("strategy") or "Bull Put Spread",
        "price": _f(t.get("current_price")), "short": short, "long": long_, "width": width,
        "credit_ps": credit_ps, "credit_usd": credit_usd, "dte": t.get("dte"),
        "exp": t.get("expiration_display") or t.get("expiration"), "breakeven": be,
        "delta": _f(t.get("delta")), "theta": None, "iv_rank": _f(t.get("iv_rank")),
        "iv_method": "HIST", "true_pop": true_pop, "true_pop_conf": (t.get("true_pop_confidence") or "").upper(),
        "implied_pop": implied, "edge_pp": edge_pp, "roi": roi,
        "credit_to_width": (ctw / 100.0) if ctw is not None else None,
        "edge_score": _f(t.get("edge_score")), "component_breakdown": t.get("component_breakdown") or {},
        "drift_mode": t.get("true_pop_drift_mode"), "vrp": _f(t.get("vrp")),
        "vol_state": t.get("vol_state"), "vrp_shift_pp": _f(t.get("vrp_shift_pp")),
        "vrp_trailing_pp": _f(t.get("vrp_trailing_pp")),
        "rv_forecast_pp": _f(t.get("rv_forecast_pp")),
        "implied_band": t.get("implied_band"),
        "sector_proxy": t.get("sector_proxy"), "sector_vol_state": t.get("sector_vol_state"),
        "news_sentiment": t.get("news_sentiment"), "fundamentals_score": t.get("fundamentals_score"),
        "trend": t.get("trend"), "rsi": _f(t.get("rsi")),
        "news_summary": t.get("news_summary"), "warnings": t.get("warnings") or [],
        "auto_reasoning": t.get("auto_reasoning"), "nearest_support": _f(t.get("nearest_support")),
        "macd_crossover": t.get("macd_crossover"), "fundamentals_reasons": t.get("fundamentals_reasons") or [],
        "p_max_profit": _f(t.get("p_max_profit")), "roundtrip_cost": _f(t.get("estimated_round_trip_cost_per_contract")),
        "criteria": t.get("criteria") or [], "news_check": t.get("news_check") or {},
        "entry_timing": t.get("entry_timing") or {},
        "support_levels": t.get("support_levels") or [],
        "resistance_levels": t.get("resistance_levels") or [],
        "needs_validation": bool(t.get("needs_validation", False)),
        "sma20": _f(t.get("sma20")), "sma50": _f(t.get("sma50")), "max_loss_usd": max_loss,
        "strat_type": g["strat_type"], "risk_dir": g["risk_dir"], "breakevens": g["breakevens"],
        "structure": g["structure"], "cushion_pct": g["cushion"],
        "put_short": g.get("put_short"), "put_long": g.get("put_long"),
        "call_short": g.get("call_short"), "call_long": g.get("call_long"),
        "gates": None, "gates_passed": 8, "gates_total": 8,  # qualified → passed the gate set
        "priority": _f(t.get("edge_score"), 0.0),
        # Beta signal-quality fields (spec §3.2–§3.5 / §5.1)
        "already_in_position": bool(t.get("already_in_position")),
        "post_earnings_crush": bool(t.get("post_earnings_crush")),
        "skew_vol_pts": _f(t.get("skew_vol_pts")), "skew_score": _f(t.get("skew_score")),
        "suppressed_strategies": t.get("suppressed_strategies") or [],
        "env": (_environment.heat_assessment(t) if _environment else None),
    }


def _adapt_legacy(row: dict, c: dict) -> dict:
    ctx = row.get("ctx") or {}
    ivr = _f(ctx.get("iv_rank"))
    gates = c.get("gates") or {}
    fails = 0 if (ivr is not None and ivr >= IVR_MIN) else 1
    for k in ("delta_cap", "otm_buffer", "credit_to_width", "min_credit_usd", "liquidity", "pop", "dte_window"):
        if not gates.get(k, False):
            fails += 1
    total = 1 + len(gates)
    passed = total - fails
    model_pop = _model_pop_estimate(row.get("price"), c.get("breakeven"), c.get("dte"),
                                    ctx.get("atm_iv"), ctx.get("rv"))
    implied = _f(c.get("pop_implied"))
    roi = _f(c.get("roi"))
    pop_score = model_pop if model_pop is not None else (implied or 0)
    priority = max(0.0, min(100.0, 0.65 * pop_score * 100 + 0.35 * (roi or 0) * 100 - fails * 6))
    return {
        "source": "legacy", "ticker": row.get("ticker"), "strategy": "Bull Put Spread (fast scan)",
        "strat_type": "bull_put",
        "structure": (f'{_f(c.get("short_strike")):g}/{_f(c.get("long_strike")):g}P'
                      if (c.get("short_strike") is not None and c.get("long_strike") is not None) else None),
        "breakevens": ([_f(c.get("breakeven"))] if c.get("breakeven") is not None else []),
        "max_loss_usd": _f(c.get("max_loss_usd")),
        "env": None,
        "price": _f(row.get("price")), "short": _f(c.get("short_strike")), "long": _f(c.get("long_strike")),
        "width": _f(c.get("width")), "credit_ps": _f(c.get("credit_per_share"), 0.0),
        "credit_usd": _f(c.get("credit_usd")), "dte": c.get("dte"), "exp": c.get("expiration"),
        "breakeven": _f(c.get("breakeven")), "delta": _f(c.get("short_delta")),
        "theta": _f(c.get("short_theta")), "iv_rank": ivr,
        "iv_method": (ctx.get("iv_rank_method") or "?").upper(),
        "true_pop": None,  # engine-only; unavailable on the fast path
        # The projection only needs spot, dte and forecast vol — none of which are engine-only —
        # so the band survives a fast rescan even though true_pop does not.
        "rv_forecast_pp": _f(ctx.get("rv_forecast_pp")),
        "vol_state": ctx.get("vol_state"),
        "vrp_shift_pp": _f(ctx.get("vrp_shift_pp")),
        "vrp_trailing_pp": _f(ctx.get("vrp_trailing_pp")),
        "true_pop_conf": "", "model_pop": model_pop, "implied_pop": implied,
        "edge_pp": None, "roi": roi,
        "credit_to_width": _f(c.get("credit_to_width")),
        "edge_score": None, "component_breakdown": {}, "drift_mode": None, "vrp": _f(ctx.get("vrp_pp")),
        "gates": gates, "gates_passed": passed, "gates_total": total, "priority": priority,
    }


def load_board():
    """Return dict: {source, trades[], asof, note}. Engine artifact first, legacy fallback."""
    if SCAN_LATEST.exists():
        try:
            d = json.loads(SCAN_LATEST.read_text(encoding="utf-8"))
            qt = d.get("qualified_trades") or []
            if qt:
                trades = [_adapt_engine(t) for t in qt]
                trades.sort(key=lambda x: (x["priority"] or 0), reverse=True)
                return {"source": "engine", "trades": trades, "asof": d.get("timestamp"),
                        "session": d.get("session_type"),
                        "context": d.get("market_context") or {}, "regime": d.get("regime") or {},
                        # Absent on boards written before the funnel existed; the bar renders
                        # nothing rather than guessing a denominator from the row count.
                        "scan_summary": d.get("scan_summary") or {},
                        "book": d.get("book") or {}, "note": ""}
        except Exception:
            pass
    data, path = _latest_candidates()
    trades = []
    if data:
        for row in data.get("rows", []):
            for c in (row.get("candidates") or []):
                trades.append(_adapt_legacy(row, c))
    trades.sort(key=lambda x: (x["priority"] or 0), reverse=True)
    asof = ((data or {}).get("meta") or {}).get("stamp")
    # The market read survives the fallback. Whether any SPREAD qualified is a different
    # question from what VIX did, and this branch was discarding both — so on a session where
    # the engine qualified nothing, Market Snapshot rendered blank and the regime banner
    # vanished, as though the cockpit had lost its data feed rather than found no trade.
    # The engine artifact still holds market_context and regime in exactly that case.
    ctx, reg, sums = {}, {}, {}
    if SCAN_LATEST.exists():
        try:
            _d = json.loads(SCAN_LATEST.read_text(encoding="utf-8"))
            ctx = _d.get("market_context") or {}
            reg = _d.get("regime") or {}
            sums = _d.get("scan_summary") or {}
        except Exception:
            pass
    return {"source": "legacy", "trades": trades, "asof": asof, "context": ctx, "regime": reg,
            "scan_summary": sums,
            "note": "Fast local scan (yfinance). No edge scores — treat as provisional until the full engine runs."}


# ─────────────────────────────────────────────────────────────────────────────
# CSS — dark market-terminal theme (per design mockup)
# ─────────────────────────────────────────────────────────────────────────────
JS = """<script>
document.addEventListener('submit', function(e){
  var b = e.target.querySelector('button[type=submit]');
  if (b && !b.disabled){
    var busy = b.getAttribute('data-busy') || 'Working\u2026';
    b.innerHTML = '<span class="spin"></span>' + busy;
    b.disabled = true;
  }
}, true);
function vtoggle(i){var m=document.getElementById('vm-'+i),d=document.getElementById('vd-'+i);if(m)m.classList.toggle('open');if(d)d.classList.toggle('open');}
function vopen(i){var d=document.getElementById('vd-'+i);if(d&&!d.classList.contains('open'))vtoggle(i);var m=document.getElementById('vm-'+i);if(m)m.scrollIntoView({behavior:'smooth',block:'center'});}
function sortBoard(th,key){var tbl=th.closest('table'),tb=tbl.tBodies[0];var dir=th.getAttribute('data-dir')==='asc'?'desc':'asc';tbl.querySelectorAll('th.srt').forEach(function(h){h.removeAttribute('data-dir');var a=h.querySelector('.arw');if(a)a.textContent='';});th.setAttribute('data-dir',dir);var ar=th.querySelector('.arw');if(ar)ar.textContent=dir==='asc'?' \u25B2':' \u25BC';var mains=[].slice.call(tb.querySelectorAll('tr.vmain'));mains.sort(function(a,b){var sa=a.getAttribute('data-'+key)||'',sb=b.getAttribute('data-'+key)||'';var x=parseFloat(sa),y=parseFloat(sb);if(!isNaN(x)&&!isNaN(y)){return dir==='asc'?x-y:y-x;}var r=sa.localeCompare(sb);return dir==='asc'?r:-r;});mains.forEach(function(m){var i=m.getAttribute('data-i');tb.appendChild(m);var d=document.getElementById('vd-'+i);if(d)tb.appendChild(d);});clearGroups();}
function clearGroups(){document.querySelectorAll('tr.grouphead').forEach(function(g){g.remove();});var b=document.getElementById('grpbtn');if(b)b.classList.remove('active');}
function groupByTicker(){var btn=document.getElementById('grpbtn');var tbl=document.querySelector('.board table');if(!tbl)return;var tb=tbl.tBodies[0];clearGroups();if(btn&&btn.getAttribute('data-on')==='1'){btn.setAttribute('data-on','0');btn.classList.remove('active');return;}var mains=[].slice.call(tb.querySelectorAll('tr.vmain'));mains.sort(function(a,b){var r=(a.getAttribute('data-ticker')||'').localeCompare(b.getAttribute('data-ticker')||'');if(r!==0)return r;return parseFloat(b.getAttribute('data-score'))-parseFloat(a.getAttribute('data-score'));});var ncols=tbl.querySelectorAll('thead .col th').length||12;var last=null;mains.forEach(function(m){var tk=m.getAttribute('data-ticker');if(tk!==last){var hr=document.createElement('tr');hr.className='grouphead';hr.innerHTML='<td class="l" colspan="'+ncols+'">'+tk+'</td>';tb.appendChild(hr);last=tk;}tb.appendChild(m);var d=document.getElementById('vd-'+m.getAttribute('data-i'));if(d)tb.appendChild(d);});if(btn){btn.setAttribute('data-on','1');btn.classList.add('active');}}
function filterBoard(){var el=document.getElementById('fmaxloss');var v=el?parseFloat(el.value):NaN;var n=0,shown=0;document.querySelectorAll('tr.vmain').forEach(function(m){n++;var ml=parseFloat(m.getAttribute('data-maxloss'));var hide=!isNaN(v)&&ml>v;m.style.display=hide?'none':'';var d=document.getElementById('vd-'+m.getAttribute('data-i'));if(d){d.style.display=hide?'none':'';if(hide){m.classList.remove('open');d.classList.remove('open');}}if(!hide)shown++;});var fc=document.getElementById('fcount');if(fc)fc.textContent=isNaN(v)?'':(shown+' of '+n+' within budget');}
function clearFilter(){var e=document.getElementById('fmaxloss');if(e){e.value='';}presetOff();filterBoard();}
function presetOff(){document.querySelectorAll('.rpre button').forEach(function(b){b.classList.remove('on');});}
/* Presets write the same input the typed box does — one filter, one source of truth,
   so the two controls can never disagree about what the board is showing. */
function setRisk(btn,v){var e=document.getElementById('fmaxloss');if(e){e.value=(v===''?'':v);}presetOff();btn.classList.add('on');filterBoard();}
</script>"""


CSS = """
:root{
 --bg:#0B0D11;--panel:#111318;--panel2:#161921;--panel3:#1C2029;--line:#232833;--line2:#1a1e27;
 --ink:#E6E9EF;--ink2:#AEB6C4;--ink3:#7f8794;--ink4:#5b626f;
 --green:#00C97A;--greensoft:#0f2a22;--amber:#F0B429;--ambersoft:#2a2413;--red:#F0455A;--redsoft:#2a1519;
 --blue:#4E8EF5;--accent:#00C97A;
}
*{box-sizing:border-box}
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
.num{font-variant-numeric:tabular-nums}
a{color:inherit;text-decoration:none}
.wrap{max-width:1360px;margin:0 auto;padding:0 22px 70px}
.topnav{display:flex;align-items:center;gap:4px;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 22px;position:sticky;top:0;z-index:20}
.brand{font-weight:800;letter-spacing:.02em;margin-right:18px;font-size:16px}
.brand .sub{display:block;font-size:9px;font-weight:600;color:var(--ink3);text-transform:uppercase;letter-spacing:.12em}
.nav a{padding:7px 13px;border-radius:7px;color:var(--ink3);font-size:13px;font-weight:600}
.nav a.on{background:var(--panel3);color:var(--ink)}
.nav a:hover{color:var(--ink)}
.rside{margin-left:auto;display:flex;gap:14px;align-items:center;font-size:12px;color:var(--ink2)}
.dot{display:inline-block;width:7px;height:7px;border-radius:99px;background:var(--green);margin-right:5px}
.dot.off{background:var(--amber)}
h1{font-size:20px;margin:18px 0 2px;letter-spacing:-.01em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);margin:22px 0 9px;font-weight:700}
.q{color:var(--ink3);font-size:13px;margin:0 0 10px;font-weight:400;text-transform:none;letter-spacing:0}
.sub{color:var(--ink3);font-size:12px;margin:2px 0 12px}
.grid{display:grid;gap:14px}
.g3{grid-template-columns:1fr 1fr 1fr}.g2{grid-template-columns:1fr 1fr}.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g3,.g2,.g4{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 17px}
.panel h3{margin:0 0 12px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink3);font-weight:700}
.cards{display:flex;flex-wrap:wrap;gap:10px}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 13px;min-width:104px}
.card .lab{font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.04em}
.card .val{font-size:21px;font-weight:700;margin:2px 0}.card .s{font-size:10px;color:var(--ink4)}
.hero{display:flex;gap:22px;align-items:center;background:linear-gradient(180deg,#12161d,#0e1116);border:1px solid var(--line);border-left:3px solid var(--green);border-radius:12px;padding:16px 20px;margin:6px 0}
.hero .flag{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--green)}
.hero .tk{font-size:30px;font-weight:800;letter-spacing:-.02em;line-height:1.05}
.hero .strat{color:var(--ink2);font-size:13px}
.hero .why{color:var(--ink3);font-size:12.5px;margin-top:6px;max-width:520px}
.hero .score{margin-left:auto;text-align:center}
.hero .score .n{font-size:46px;font-weight:800;line-height:1;color:var(--green)}
.hero .score .c{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink3);margin-top:2px}
.hstats{display:flex;gap:20px;text-align:center;padding-left:20px;border-left:1px solid var(--line)}
.hstats .n{font-size:20px;font-weight:700;line-height:1}.hstats .c{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13px}
.board{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.board thead .grp th{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink4);font-weight:700;padding:10px 10px 2px;text-align:right}
.board thead .grp th.l{text-align:left}
.board thead .col th{font-size:10px;color:var(--ink3);font-weight:600;padding:0 10px 9px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
.board thead .col th.l{text-align:left}
.board tbody td{padding:9px 10px;border-bottom:1px solid var(--line2);text-align:right;vertical-align:middle}
.board tbody td.l{text-align:left}
.board tbody tr:hover{background:#12151c}
.gsep{border-left:1px solid var(--line2)}
.tk b{font-size:14px}.dim{color:var(--ink3);font-size:11px}
.pos{color:var(--green);font-weight:600}.neg{color:var(--red);font-weight:600}
.prio{font-size:16px;font-weight:700}
.mbar{position:relative;height:4px;background:var(--panel3);border-radius:9px;margin-top:4px;overflow:hidden}
.mbar i{position:absolute;left:0;top:0;bottom:0;background:var(--green);border-radius:9px}
.gdots{display:inline-flex;gap:3px;vertical-align:middle}
.gdots i{width:8px;height:8px;border-radius:99px;display:inline-block;background:var(--panel3);border:1px solid var(--line)}
.gdots i.on{background:var(--green);border-color:var(--green)}
.gdots i.off{background:transparent;border-color:var(--amber)}
.tier{display:inline-block;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:2px 7px;border-radius:99px;border:1px solid}
.tier-validated{color:var(--green);background:var(--greensoft);border-color:#1c4d3c}
.tier-provisional{color:var(--amber);background:var(--ambersoft);border-color:#4d3f16}
.tier-unproven{color:var(--ink3);background:var(--panel3);border-color:var(--line)}
.badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:5px}
.b-strong{background:var(--greensoft);color:var(--green)}.b-prov{background:var(--ambersoft);color:var(--amber)}
.sc{display:flex;flex-direction:column;gap:11px}
.sc .row{display:flex;align-items:center;gap:10px;font-size:12px}
.sc .row .lab{width:150px;color:var(--ink2)}
.sc .row .bar{flex:1;height:6px;background:var(--panel3);border-radius:99px;overflow:hidden}
.sc .row .bar i{display:block;height:100%;background:var(--green)}
.sc .row .bar i.low{background:var(--amber)}
.sc .row .v{width:38px;text-align:right;font-weight:700}
.comp{display:flex;flex-direction:column;gap:8px;font-size:12px}
.comp .row{display:flex;align-items:center;gap:10px}
.comp .row .lab{width:130px;color:var(--ink2)}
.comp .row .bar{flex:1;height:6px;background:var(--panel3);border-radius:99px;overflow:hidden}
.comp .row .bar i{display:block;height:100%;background:var(--blue)}
.comp .row .v{width:52px;text-align:right;color:var(--ink3)}
.comp .subtot{display:flex;justify-content:space-between;border-top:1px dashed var(--line);padding-top:6px;margin-top:2px;color:var(--ink2);font-weight:600}
.comp .tot{display:flex;justify-content:space-between;border-top:1px solid var(--line);padding-top:8px;margin-top:2px;font-weight:700}
.comp .dim{color:var(--ink3);font-weight:400}
/* ── Brief tab ── */
.bstrip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:8px 0 20px}
.bstrip .ms{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px 14px}
.bstrip .msl{font-size:10px;letter-spacing:.12em;color:var(--ink3);text-transform:uppercase;margin-bottom:5px}
.bstrip .msv{font-size:16px;font-weight:700;color:var(--ink)}
.bstrip .mss{font-size:11px;color:var(--ink3);margin-top:3px}
.briefcard{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:9px;padding:14px 16px;margin-bottom:14px}
.briefcard.bl-green{border-left-color:var(--green)}.briefcard.bl-amber{border-left-color:var(--amber,#F0B429)}.briefcard.bl-red{border-left-color:var(--red);opacity:.9}
.bch{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.bcl{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.bch .bt{font-size:20px;font-weight:800;color:var(--ink)}
.bch .bstrat{font-size:11px;letter-spacing:.05em;color:var(--ink3)}
.bch .bstruct{font-size:13px;color:var(--ink2)}
.bcr{margin-left:auto;text-align:right}
.bch .bcred{display:block;color:var(--green);font-weight:700;font-size:14px}
.bch .bcml{display:block;color:var(--ink3);font-size:11px}
.brec{font-size:11px;font-weight:800;letter-spacing:.1em;padding:5px 12px;border-radius:5px;border:1px solid}
.rec-green{color:var(--green);border-color:var(--green);background:rgba(0,200,122,.12)}
.rec-amber{color:var(--amber,#F0B429);border-color:var(--amber,#F0B429);background:rgba(240,180,41,.12)}
.rec-red{color:var(--red);border-color:var(--red);background:rgba(240,69,90,.12)}
.bmeter{display:flex;align-items:center;gap:10px;margin:12px 0 10px}
.bmeter .ml2{font-size:11px;color:var(--ink3);letter-spacing:.06em;white-space:nowrap}
.bmeter .mtrk{flex:1;height:5px;background:var(--panel3);border-radius:99px;overflow:hidden}
.bmeter .mtrk i{display:block;height:100%}
.mtrk i.score-high{background:var(--green)}.mtrk i.score-med{background:var(--amber,#F0B429)}.mtrk i.score-low{background:var(--red)}
.bpop{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.bpop .pb{background:var(--panel2,var(--bg));border:1px solid var(--line);border-radius:6px;padding:6px 12px;min-width:92px}
.bpop .pb.edge{background:rgba(240,180,41,.08);border-color:rgba(240,180,41,.4)}
.bpop .pl{display:block;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-bottom:2px}
.bpop .pb b{font-size:15px}
.evln{font-size:12.5px;color:var(--ink2);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin-bottom:10px}
.brwhy{font-size:12px;color:var(--ink2);margin-bottom:10px}
.tkt{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}
.tk{font-size:12.5px;color:var(--ink2);line-height:1.5;background:var(--bg);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:5px;padding:8px 12px}
.tk.entry{border-left-color:var(--green)}.tk.exit{border-left-color:var(--amber,#F0B429)}.tk.inval{border-left-color:var(--red)}
.tk .tl{display:block;font-size:9.5px;font-weight:700;letter-spacing:.1em;color:var(--ink3);margin-bottom:3px}
.tk b{color:var(--ink)}
.szg{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.szc{flex:1;min-width:96px;text-align:center;border:1px solid var(--line);border-radius:6px;padding:7px 10px;background:var(--bg)}
.szc.on{border-color:rgba(0,200,122,.5)}.szc.off{opacity:.55}
.szc .szl{font-size:10px;color:var(--ink3);margin-bottom:3px}
.szc .szn{font-size:16px;font-weight:700;color:var(--green)}.szc.off .szn{color:var(--ink3)}
.szc .szs{font-size:10.5px;color:var(--ink3);margin-top:2px}
.wflist{display:flex;flex-direction:column;gap:5px;margin-top:6px}
.wf{font-size:11.5px;color:var(--amber,#F0B429);background:rgba(240,180,41,.07);border:1px solid rgba(240,180,41,.28);border-radius:4px;padding:5px 10px}
/* ── Gate detail table + notes ("under the hood") ── */
.gatecell{cursor:pointer}
.gatecell .dim{color:var(--ink3)}
.gtbl{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
.gtbl th{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink4);font-weight:700;text-align:right;padding:4px 8px;border-bottom:1px solid var(--line)}
.gtbl th.l{text-align:left}
.gtbl td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--line2);color:var(--ink2)}
.gtbl td.l{text-align:left}
.gtbl td.num{padding-right:18px}
.gtbl th.l:nth-child(3),.gtbl td.l:nth-child(3){padding-left:6px}
.gtbl tr.gsub td{background:var(--panel2);color:var(--ink3);font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;padding:5px 8px}
.gtbl .gpass{color:var(--green);white-space:nowrap;text-align:right}
.gtbl .gwarn{color:var(--amber,#F0B429);white-space:nowrap;text-align:right}
.notes{display:flex;flex-direction:column;gap:5px;margin-top:4px}
.noterow{font-size:12px;color:var(--ink2);line-height:1.5;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:6px 10px}
.ntag{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:1px 6px;border-radius:3px;margin-right:7px;vertical-align:middle}
.nt-engine{background:var(--blue-dim,rgba(77,159,255,.14));color:var(--blue,#4E8EF5)}
.nt-news{background:rgba(0,200,122,.14);color:var(--green)}
.nt-flag{background:rgba(240,180,41,.14);color:var(--amber,#F0B429)}
.nt-you{background:rgba(180,120,255,.16);color:#b478ff}
/* ── Track Record tiles ── */
.tkgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:8px 0 16px}
.tktile{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.tktile .tkl{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
.tktile .tkv{font-size:22px;font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums}
.tktile .tkv.green{color:var(--green)}.tktile .tkv.red{color:var(--red)}
.tktile .tks{font-size:10.5px;color:var(--ink3);margin-top:3px}
/* ── Environment heat chips + brief line ── */
.bflag.envwarm{background:rgba(240,180,41,.14);color:var(--amber,#F0B429);border:1px solid rgba(240,180,41,.45)}
.bflag.envhot{background:rgba(240,69,90,.16);color:var(--red);border:1px solid rgba(240,69,90,.5)}
.envln.env-warm{font-size:12px;color:var(--ink2);background:rgba(240,180,41,.07);border:1px solid rgba(240,180,41,.3);border-radius:6px;padding:7px 11px;margin-bottom:10px;line-height:1.5}
.envln.env-hot{font-size:12px;color:var(--ink2);background:rgba(240,69,90,.08);border:1px solid rgba(240,69,90,.35);border-radius:6px;padding:7px 11px;margin-bottom:10px;line-height:1.5}
/* ── Row contract line (strikes + clear expiration) + provisional banner ── */
.subln{margin-top:3px;font-size:12px;color:var(--ink3)}
.subln .strk{color:var(--ink);font-weight:600;font-size:12.5px}
.subln .expd{color:var(--amber);font-weight:600}
.provbar{background:rgba(240,180,41,.08);border:1px solid rgba(240,180,41,.3);border-radius:8px;padding:9px 14px;margin-bottom:10px;font-size:12.5px;color:var(--ink2);line-height:1.5}
.provbar code{background:var(--panel3);padding:1px 6px;border-radius:4px;font-size:11.5px;color:var(--ink)}
/* ── Desired-range status tint on board numbers (green optimal / white neutral / red outside) ── */
.rg-opt{color:var(--green)}
.rg-neu{color:var(--ink)}
.rg-bad{color:var(--red)}
.rgkey{color:var(--ink3);font-size:10.5px;display:inline-flex;align-items:center;gap:3px}
.rgkey i{font-style:normal;font-size:9px;margin-left:8px;margin-right:1px}
/* ── Drawer contract header ── */
.chdr{padding:12px 16px;margin-bottom:6px;background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:9px}
.chtop{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.chdr .chstrat{font-size:17px;font-weight:800;color:var(--ink);letter-spacing:.01em}
.chdr .chlegs{font-size:13px;color:var(--ink2);font-variant-numeric:tabular-nums}
.chdr .chlegs b{color:var(--ink)}
.chdr .chmeta{margin-top:7px;font-size:12.5px;color:var(--ink2);font-variant-numeric:tabular-nums}
.chdr .chm{color:var(--ink3);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;margin-right:2px}
tr.grouphead td{background:var(--panel3);color:var(--amber);font-weight:800;font-size:12px;letter-spacing:.06em;padding:6px 12px;border-bottom:1px solid var(--line)}
.ghostbtn.active{background:var(--amber-dim,rgba(240,180,41,.14));border-color:var(--amber);color:var(--amber)}
.alts{display:flex;flex-direction:column;gap:5px}
.altrow{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--ink2);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:6px 10px}
.altrow .alttxt{flex:1}
.altmeta{font-size:11px}
.alttag{font-size:8.5px;font-weight:800;letter-spacing:.07em;padding:1px 6px;border-radius:3px}
.alt-primary{background:rgba(0,200,122,.14);color:var(--green)}
.alt-alt{background:var(--panel3);color:var(--ink3)}
.alt-env{background:rgba(240,180,41,.14);color:var(--amber)}
.strip{display:flex;flex-wrap:wrap;gap:20px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:11px 16px;margin:4px 0}
.strip span{color:var(--ink3);font-size:11px;text-transform:uppercase;letter-spacing:.03em;margin-right:6px}.strip b{font-size:15px}
button{font:inherit;border:0;border-radius:7px;padding:6px 12px;cursor:pointer;font-weight:600}
.primary{background:var(--green);color:#04150e}.go{background:var(--green);color:#04150e}.close{background:var(--red);color:#fff}
input,select{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--ink)}
input.n{width:58px}
.empty{color:var(--ink3);font-style:italic;padding:14px;background:var(--panel);border:1px dashed var(--line);border-radius:10px}
.flash{background:#10233b;border:1px solid #204063;border-radius:9px;padding:9px 13px;margin:10px 0;font-size:13px}
.warn{background:var(--ambersoft);border:1px solid #4d3f16;border-radius:9px;padding:9px 13px;margin:10px 0;font-size:13px;color:var(--amber)}
.ravens{margin:10px 0 14px}
.rav{border-radius:9px;padding:10px 13px;margin-bottom:7px;font-size:12.5px;line-height:1.5;border:1px solid var(--line);background:var(--panel2);border-left-width:3px;border-left-style:solid}
.rav .rhd{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:3px}
.rav .rtag{font-size:10px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;padding:2px 7px;border-radius:4px}
.rav .rtk{font-weight:700;font-size:13px}
.rav .rwhen{color:var(--ink4);font-size:11px;margin-left:auto}
.rav .rtxt{color:var(--ink2)}
.rav .rmeta{color:var(--ink3);font-size:11px;margin-top:4px}
.rav.tension{border-left-color:var(--amber);background:var(--ambersoft)}
.rav.tension .rtag{background:rgba(240,180,41,.18);color:var(--amber)}
.rav.blind{border-left-color:var(--ink3);background:var(--panel2)}
.rav.blind .rtag{background:rgba(255,255,255,.07);color:var(--ink3)}
form.inline{display:inline-flex;gap:5px;align-items:center;margin:0}
.stars{letter-spacing:2px;font-size:15px}
.foot{color:var(--ink4);font-size:11px;margin:26px 0;line-height:1.6}
.kv{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line2);font-size:12.5px}
.kv .k{color:var(--ink3)}.kv b{font-variant-numeric:tabular-nums}
/* Phase 1 polish: micro-interactions, sticky headers, reduced-motion */
.nav a{transition:background .12s ease,color .12s ease}
button{transition:filter .12s ease,opacity .12s ease}
button:hover{filter:brightness(1.08)}
button:disabled{opacity:.75;cursor:default}
.board tbody tr{transition:background .12s ease}
.mbar i,.sc .row .bar i,.comp .row .bar i{transition:width .45s cubic-bezier(.22,.61,.36,1)}
.card,.panel{transition:border-color .15s ease}
.panel:hover{border-color:#2c3342}
/* Header stays in normal flow. It was position:sticky, but .board is a scroll
   container (overflow-x:auto below), so the header stuck to the board's own top
   and its opaque background clipped the first data row. Static = no overlap. */
.board thead .grp th{position:static;background:var(--panel);z-index:6}
.board thead .col th{position:static;background:var(--panel);z-index:6}
.spin{display:inline-block;width:11px;height:11px;border:2px solid rgba(4,21,14,.35);border-top-color:#04150e;border-radius:50%;vertical-align:-1px;margin-right:4px;animation:spin .6s linear infinite}
.close .spin,.primary .spin{border-color:rgba(255,255,255,.35);border-top-color:#fff}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){
 *{animation-duration:.001ms !important;animation-iteration-count:1 !important;transition-duration:.001ms !important;scroll-behavior:auto !important}
}
.tstrip{display:grid;grid-template-columns:1.3fr 1fr 1fr 1fr;gap:12px;margin:6px 0 16px}
.tcard{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:12px 14px}
.tcard .lab{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.vd{display:flex;align-items:center;gap:11px;margin-top:6px}
.vd .d{width:11px;height:11px;border-radius:9px;background:var(--green);box-shadow:0 0 0 4px rgba(0,201,122,.15)}
.vd .d.off{background:var(--amber);box-shadow:0 0 0 4px rgba(240,180,41,.15)}
.vd .txt{font-size:14px;font-weight:800}.vd .sub{font-size:11px;color:var(--ink3)}
.tbig{font-size:20px;font-weight:800;margin-top:3px}.tcard .sub{font-size:10px;color:var(--ink3)}
.hero{cursor:pointer}
.strat{color:var(--ink2);font-size:12px}
.sbadge{display:inline-block;min-width:28px;text-align:center;font-weight:800;font-size:12px;border-radius:5px;padding:2px 7px}
.sbadge.great{background:var(--greensoft);color:var(--green);border:1px solid #1c4d3c}
.sbadge.good{background:var(--ambersoft);color:var(--amber)}
.sbadge.fair{background:#2a1a12;color:#e07b3a}
.board tbody tr.vmain{cursor:pointer}
.board tbody tr.vmain.open{background:#12161d}
.board tbody tr.vmain:hover{background:#12151c}
.vcaret{display:inline-block;color:var(--ink3);transition:transform .15s ease}
.board tbody tr.vmain.open .vcaret{transform:rotate(90deg);color:var(--green)}
.vdetail td{padding:0 !important;background:#0c0f14}
.vdraw{max-height:0;overflow:hidden;transition:max-height .3s ease}
.vdetail.open .vdraw{max-height:1200px}
.vdrin{padding:16px 18px;display:grid;grid-template-columns:240px 1fr 300px;gap:18px}
.vdrin h4{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink3);font-weight:700;margin-bottom:9px}
.payhd{display:flex;justify-content:space-between;font-size:10px;color:var(--ink3);text-transform:uppercase;margin-bottom:5px}
.chk{font-size:11px;color:var(--ink2);padding:2.5px 0;display:flex;gap:6px}
.chk .k{color:var(--green);font-weight:700}.chk.amber .k{color:var(--amber)}
.grouphd{color:var(--green);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin:0 0 5px}
.btoolbar{display:flex;align-items:center;gap:9px;padding:9px 12px;border-bottom:1px solid var(--line);font-size:12px}
.btoolbar .flab{color:var(--ink3);text-transform:uppercase;font-size:10px;letter-spacing:.05em;font-weight:700}
.ghostbtn{background:var(--panel3);color:var(--ink2);border:0;border-radius:6px;padding:5px 10px;font-size:11px;cursor:pointer}
/* Reject is destructive of an opportunity, not of data — muted, never the loudest button. */
.ghostbtn.danger{color:var(--red)}
.ghostbtn.danger:hover{background:var(--redsoft)}
/* Budget bands — the first question a small account asks, answered in one click. */
.rpre{display:inline-flex;gap:4px;flex-wrap:wrap}
.rpre button{background:var(--panel3);color:var(--ink3);border:1px solid transparent;border-radius:6px;padding:4px 9px;font-size:10.5px;font-weight:700;cursor:pointer}
.rpre button:hover{color:var(--ink)}
.rpre button.on{background:var(--greensoft);color:var(--green);border-color:rgba(0,201,122,.35)}
.ghostbtn:hover{color:var(--ink)}
th.srt{cursor:pointer;user-select:none}th.srt:hover{color:var(--ink)}
th.srt .arw{color:var(--green);font-size:9px}
.edgebox{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--green);border-radius:9px;padding:10px 12px}
.edgebox .cap{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
.edgebox .v{font-size:24px;font-weight:800;margin:2px 0}
.edgebox .v.pos{color:var(--green)}.edgebox .v.neg{color:var(--red)}
.edgebox .ln{font-size:11px;color:var(--ink2)}
.rec{font-size:11px;cursor:help}.rec.ok{color:var(--green)}.rec.bad{color:var(--amber)}
.tchip{display:inline-block;font-size:8.5px;font-weight:800;letter-spacing:.03em;color:var(--ink2);background:var(--panel3);border:1px solid var(--line);border-radius:4px;padding:1px 5px;margin-right:5px;vertical-align:1px}
.valtag{display:inline-block;font-size:8.5px;font-weight:800;text-transform:uppercase;color:var(--amber);background:#2a2413;border:1px solid #4d3f16;border-radius:4px;padding:1px 5px;margin-right:5px;vertical-align:1px}
.valbanner{background:#2a2413;border:1px solid #4d3f16;color:var(--amber);border-radius:8px;padding:8px 11px;font-size:11px;margin:6px 0;line-height:1.4}
.board{overflow-x:auto}
.specbanner{background:#2a1a12;border:1px solid #5a3a1e;color:#e8a15a;border-radius:10px;padding:10px 14px;font-size:12px;margin:8px 0 14px;line-height:1.5}
.lottowrap{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:900px){.lottowrap{grid-template-columns:1fr}}
.lotto{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:11px;padding:13px 15px}
.lotto .lh{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.lotto .tk{font-size:17px;font-weight:800}
.lotto .conv{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;color:var(--ink3);background:var(--panel3);border-radius:5px;padding:2px 7px}
/* IV rank chip: cheap options are good news for a buyer, rich ones are the warning. */
.lotto .conv.cheap{color:var(--green);background:var(--greensoft)}
.lotto .conv.rich{color:var(--red);background:var(--redsoft)}
/* Whether the call runs with the chart or against it — the part that actually varies. */
.dtag{display:inline-block;font-size:8.5px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;border-radius:4px;padding:2px 6px;margin-right:6px}
.dtag.bull{color:var(--green);background:var(--greensoft)}
.dtag.bear{color:var(--amber);background:var(--ambersoft)}
.dtag.flat{color:var(--ink3);background:var(--panel3)}
.lacts{display:flex;gap:6px;margin-top:9px}
.lotto .conv.hi{color:var(--amber);background:#2a2413}
.lgrid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:700px){.lgrid{grid-template-columns:repeat(2,1fr)}}
.lgrid .cap{font-size:8.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.04em}
.lgrid .v{font-size:16px;font-weight:800;margin-top:2px}.lgrid .v.pos{color:var(--green)}.lgrid .v.neg{color:var(--red)}
.lgrid .dim{font-size:9.5px}
.lwhy{margin-top:11px;padding-top:9px;border-top:1px solid var(--line2);font-size:11.5px;color:var(--ink2);line-height:1.5}
.bflag{display:inline-block;font-size:9px;font-weight:800;letter-spacing:.03em;padding:1px 5px;border-radius:4px;margin:0 3px;vertical-align:middle;text-transform:uppercase}
.bflag.inpos{background:rgba(230,120,40,.16);color:var(--amber,#e6a23c);border:1px solid rgba(230,120,40,.4)}
.bflag.crush{background:rgba(90,160,255,.14);color:#5aa0ff;border:1px solid rgba(90,160,255,.4)}
.bflag.skew{background:rgba(60,180,120,.14);color:var(--green,#3cba7c);border:1px solid rgba(60,180,120,.4)}
.bookfoot{margin-top:14px;padding:10px 14px;background:var(--panel,#1a1d24);border:1px solid var(--line,#2a2e37);border-radius:8px;font-size:12px;color:var(--ink2,#aab)}
.bookfoot b.num{color:var(--ink,#e6e8ee)}

/* ── MISSION CONTROL board ───────────────────────────────────────────────── */
.mcbar{display:flex;align-items:center;gap:12px;padding:9px 15px;background:var(--panel2);border:1px solid var(--line);border-radius:10px 10px 0 0;border-bottom:none}
.mcbar .ttl{font-size:11px;font-weight:800;letter-spacing:.13em;text-transform:uppercase;color:var(--ink2)}
.mcbar .meta{margin-left:auto;font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink3)}
.mcwrap{border:1px solid var(--line);border-top:none;border-radius:0 0 10px 10px;background:var(--bg);padding:14px 15px 15px}
.mccards{display:grid;grid-template-columns:1.25fr 1fr 1fr .85fr;gap:11px}
.mccard{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px;min-height:74px}
.mccard .lab{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;font-weight:700}
.mccard .big{font-size:19px;font-weight:800;margin-top:4px;line-height:1.15}
.mccard .sub{font-size:10px;color:var(--ink3);margin-top:3px}
.mccard.hi{border-color:rgba(60,180,120,.4)}
.mcmain{display:grid;grid-template-columns:290px 1fr;gap:11px;margin-top:11px;align-items:start}
@media(max-width:1100px){.mcmain{grid-template-columns:1fr}.mccards{grid-template-columns:1fr 1fr}}
/* Permanent key for the 0-10 cards — a number nobody can place is no better than a star. */
.kpikey{display:flex;flex-wrap:wrap;align-items:center;gap:13px;margin-top:7px;font-size:10px;color:var(--ink3)}
.kpikey .k{text-transform:uppercase;letter-spacing:.07em;font-weight:700;font-size:9px}
.kpikey i{display:inline-block;width:7px;height:7px;border-radius:99px;margin-right:5px;vertical-align:middle}
/* Regime read as a band the eye stops on, not body text under four competing cards. */
.regband{margin:11px 0 0;padding:9px 13px;border-radius:8px;font-size:12px;line-height:1.5;border-left:3px solid var(--ink3);background:var(--panel)}
.regband.ok{border-left-color:var(--green);background:var(--greensoft)}
.regband.warn{border-left-color:var(--amber);background:var(--ambersoft)}
.regband.bad{border-left-color:var(--red);background:var(--redsoft)}
/* Risk already committed, above the list of ways to commit more. */
.expbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:11px;padding:8px 13px;border-radius:8px;background:var(--ambersoft);border:1px solid rgba(240,180,41,.25);font-size:11.5px;color:var(--ink2);text-decoration:none}
.expbar .go{margin-left:auto;color:var(--amber);font-weight:700;font-size:10.5px}
.expbar:hover{border-color:var(--amber)}
/* Opportunity-density funnel: the denominator behind the board. */
.funnel{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:11px;padding:9px 13px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.funnel .step{display:flex;align-items:baseline;gap:6px}
.funnel .step b{font-size:15px;font-weight:800}
.funnel .lb{font-size:10.5px;color:var(--ink3)}
.funnel .arw{color:var(--ink3);font-size:12px}
@media(max-width:760px){.mccards{grid-template-columns:1fr}.funnel .step{width:100%}.funnel .arw{display:none}}
.mcpanel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.mcpanel>.hd{font-size:9.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;font-weight:700;margin-bottom:8px}
.pbrow{display:block;padding:7px 0;border-bottom:1px solid var(--line2);text-decoration:none;color:inherit;cursor:pointer}
.pbrow:last-child{border-bottom:none}
.pbrow:hover{background:rgba(255,255,255,.03)}
.pbrow .role{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
.pbrow .line{display:flex;align-items:center;gap:7px;margin-top:2px}
.pbrow .tk{font-weight:800;font-size:13px}
.pbrow .st{font-size:11px;color:var(--ink2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbrow .mv{font-size:12px;font-weight:800}
.pbavoid{margin-top:9px;padding-top:9px;border-top:1px solid var(--line2)}
.pbavoid .role{font-size:9px;color:var(--red);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.pbavoid .txt{font-size:11px;color:var(--ink3);margin-top:2px}
.mcstat{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-top:11px}
.mcstat .cell{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:9px 12px}
.mcstat .lab{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.mcstat .val{font-size:12.5px;font-weight:800;margin-top:3px}

/* ── AI COPILOT drawer ───────────────────────────────────────────────────── */
.cop{padding:14px 16px 16px;background:var(--bg)}
.coptop{display:grid;grid-template-columns:1.05fr 1.15fr .8fr;gap:12px;align-items:start}
@media(max-width:1100px){.coptop{grid-template-columns:1fr}}
.copcard{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 15px}
.copcard.rec{border-color:rgba(60,180,120,.35)}
.copcard>.hd{font-size:9.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;font-weight:700;margin-bottom:9px}
.coprec .who{font-size:9.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em}
.coprec .nm{font-size:21px;font-weight:800;margin-top:2px}
.coprec .nm span{color:var(--ink2);font-weight:600}
.copbadge{display:inline-block;margin-top:7px;padding:3px 10px;border-radius:20px;font-size:10.5px;font-weight:800;background:rgba(60,180,120,.16);color:var(--green);border:1px solid rgba(60,180,120,.42)}
.copnums{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:12px;padding-top:11px;border-top:1px solid var(--line2)}
.copnums .c .k{font-size:8.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.04em;display:block}
.copnums .c .v{font-size:15px;font-weight:800;margin-top:2px;display:block}
.copwhy .row{display:flex;gap:8px;align-items:flex-start;padding:4px 0;font-size:11.5px;color:var(--ink2);line-height:1.45}
.copwhy .row .tick{color:var(--green);flex-shrink:0;font-weight:800}
.copwhy .row.warn .tick{color:var(--amber)}
/* Three buckets: evidence, market risk, VEGA's own reservations. The last two are different
   decisions — one is priced and accepted, the other is a reason to pass. */
.whyblk{margin-top:9px}
.whyblk:first-child{margin-top:0}
.whyblk .bh{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);padding-bottom:2px;border-bottom:1px solid var(--line);margin-bottom:3px}
.whyblk.risk .bh{color:var(--amber)}
.confbadge{font-size:9.5px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-top:5px}
/* What the headline edge score is made of, where the score is shown. */
.edgedecomp{margin-top:7px;padding-top:6px;border-top:1px solid var(--line);font-size:10px;color:var(--ink3);line-height:1.5}
.copidea{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line2);font-size:11.5px;cursor:pointer;text-decoration:none;color:inherit;flex-wrap:wrap}
/* Why this one ranks lower — a leaderboard with reasons teaches the weighting. */
.copidea .wl{flex-basis:100%;font-size:10px;color:var(--ink3);padding-left:2px}
.copidea:last-of-type{border-bottom:none}
.copidea:hover{background:rgba(255,255,255,.03)}
.copidea .r{color:var(--ink3);width:14px;flex-shrink:0}
.copidea .t{font-weight:800;width:44px;flex-shrink:0}
.copidea .s{color:var(--ink2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.copidea .e{color:var(--ink3);font-size:10.5px;flex-shrink:0}
.copmid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
@media(max-width:1100px){.copmid{grid-template-columns:1fr}}
.copgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
.copgrid .c .k{font-size:8.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.04em;display:block}
.copgrid .c .v{font-size:14px;font-weight:800;margin-top:2px;display:block}
.copgrid .c .s{font-size:9px;color:var(--ink3);display:block}
.copact{display:flex;align-items:center;gap:14px;margin-top:12px;padding:12px 15px;background:var(--panel);border:1px solid var(--line);border-radius:11px;flex-wrap:wrap}
.copact .lab{font-size:9.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;font-weight:700}
.copact .txt{font-size:13px;font-weight:700;margin-top:2px}
.copact .btns{margin-left:auto;display:flex;gap:9px;align-items:center}
.copmore{margin-top:12px}
.copmore>summary{cursor:pointer;font-size:11px;color:var(--ink3);padding:7px 0;list-style:none;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.copmore>summary::-webkit-details-marker{display:none}
.copmore>summary:before{content:"\\25B8 ";color:var(--ink3)}
.copmore[open]>summary:before{content:"\\25BE "}
.copmore>summary:hover{color:var(--ink2)}
.copdeep{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;padding-top:10px}
@media(max-width:1100px){.copdeep{grid-template-columns:1fr}}
/* Cross-venue gap: the two numbers and their difference, in the order they are read. */
.pband{margin-top:11px}
.pbrow{display:flex;align-items:baseline;gap:10px;padding:4px 0;font-size:12px;flex-wrap:wrap}
.pbrow .k{min-width:130px;color:var(--ink3)}
.pbrow b{font-size:15px;font-weight:800}
.pbnote{margin-top:7px;padding-top:6px;border-top:1px solid var(--line);font-size:10.5px;color:var(--ink3);line-height:1.55}
.xvgap{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin-top:10px}
.xvrow{display:flex;align-items:baseline;gap:10px;padding:4px 0;font-size:12px}
.xvrow .k{min-width:190px;color:var(--ink3)}
.xvrow b{font-size:15px;font-weight:800}
.xvrow.tot{border-top:1px solid var(--line);margin-top:4px;padding-top:7px}
.xvnote{margin-top:8px;padding-top:7px;border-top:1px solid var(--line);font-size:11.5px;color:var(--ink2);line-height:1.5}
.xvh{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink3);font-weight:700;margin:14px 0 5px}
.xvdrv{margin-top:7px;font-size:11px;color:var(--ink3)}
.xvdrv summary{cursor:pointer;font-weight:700}
.xvdrv ul{margin:5px 0 0 16px;line-height:1.6}
@media(max-width:620px){.xvrow{flex-wrap:wrap}.xvrow .k{min-width:0;flex-basis:100%}}
/* ── Phone (A5-2) ───────────────────────────────────────────────────────────────────────────
   The SpreadSignal audience arrives from a YouTube link on a phone, and every layout above
   assumes a desk. Breakpoints only — no separate mobile document, because two documents drift
   and the second one is always the stale one. The dark theme carries over unchanged; what has
   to give is density: multi-column grids collapse to one, the wide board scrolls inside its
   own box instead of pushing the page sideways, and the nav wraps rather than truncating. */
@media(max-width:620px){
  .wrap{padding:0 12px 48px}
  .topnav{padding:8px 12px;flex-wrap:wrap;gap:6px}
  .nav{flex-wrap:wrap}
  .nav a{padding:6px 10px;font-size:12px}
  .rside{width:100%;justify-content:flex-start;font-size:10.5px}
  h1{font-size:20px}
  /* The board keeps every column and scrolls within itself — dropping columns on a phone
     hides the one the reader came to check, and which one that is cannot be known here. */
  .board{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .board table,.gtbl{min-width:560px}
  .lgrid{grid-template-columns:1fr 1fr}
  .lottowrap{grid-template-columns:1fr}
  .copnums,.copgrid{grid-template-columns:1fr 1fr}
  .mccard .big{font-size:17px}
  .expbar .go{margin-left:0;flex-basis:100%}
  .kpikey{gap:8px;font-size:9.5px}
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Shared UI pieces
# ─────────────────────────────────────────────────────────────────────────────
def _live_calib_pp(s):
    g = s.get("calibration_gap")
    return (g * 100) if g is not None else None


def edge_tier(s):
    return gate1_tier(GATE1_BACKTEST_PASSED, _live_calib_pp(s), s.get("n_closed") or 0)


def gate_dots(card):
    """8-dot gate matrix. Engine cards qualified (all-pass); legacy shows real gate booleans."""
    order = ["iv_rank", "delta_cap", "otm_buffer", "credit_to_width", "min_credit_usd", "liquidity", "pop", "dte_window"]
    labels = ["IV-Rank", "Delta cap", "OTM buffer", "Credit/Width", "Min credit", "Liquidity", "POP", "DTE"]
    gates = card.get("gates")
    dots = ""
    if gates is None:  # engine → qualified
        for lb in labels:
            dots += f'<i class="on" title="{lb}: pass"></i>'
    else:
        ivr = card.get("iv_rank")
        states = [ivr is not None and ivr >= IVR_MIN] + [bool(gates.get(k)) for k in order[1:]]
        for lb, ok in zip(labels, states):
            dots += f'<i class="{"on" if ok else "off"}" title="{lb}: {"pass" if ok else "fail"}"></i>'
    return f'<span class="gdots">{dots}</span>'


# ── Desired-range coloring (STATUS encoding: green=optimal, white=neutral, red=outside).
#    Thresholds encode premium-selling doctrine; tune them here. Each returns a css class.
#    Numbers stay fully legible; color is an at-a-glance "where does this sit vs. what I want".
RANGE_MAXLOSS_OPT = 250.0   # $ per trade at/under this reads optimal (near your risk tier)
RANGE_MAXLOSS_BAD = 500.0   # $ over this reads out-of-range for the account

def _rg(metric, v):
    """Return 'rg-opt' | 'rg-neu' | 'rg-bad' | '' for a metric value."""
    if v is None:
        return ""
    try:
        v = float(v)
    except Exception:
        return ""
    if metric == "edge":            # POP edge (pp) — higher better
        return "rg-opt" if v >= 6 else "rg-bad" if v < 0 else "rg-neu"
    if metric == "tpop":            # True POP (%) — sweet spot high, not thin
        return "rg-opt" if v >= 80 else "rg-bad" if v < 68 else "rg-neu"
    if metric == "ev":              # EV $/contract — positive expectancy wanted
        return "rg-opt" if v >= 10 else "rg-bad" if v < 0 else "rg-neu"
    if metric == "roc":             # ROC (%) — banded; absurdly high = illiquid/mispriced artifact
        if v < 12 or v > 150:
            return "rg-bad"
        return "rg-opt" if 20 <= v <= 80 else "rg-neu"
    if metric == "maxloss":         # $ risk — lower better vs account
        return "rg-opt" if v <= RANGE_MAXLOSS_OPT else "rg-bad" if v > RANGE_MAXLOSS_BAD else "rg-neu"
    if metric == "cushion":         # % distance to the short strike — higher safer
        return "rg-opt" if v >= 8 else "rg-bad" if v < 4 else "rg-neu"
    if metric == "delta":           # |short delta| — banded; too close (>0.30) is the danger
        a = abs(v)
        if a > 0.30:
            return "rg-bad"
        return "rg-opt" if 0.12 <= a <= 0.22 else "rg-neu"
    return ""


def pop_cell(card):
    """A2: headline true POP, secondary implied, edge as decision metric."""
    tp = card.get("true_pop")
    implied = card.get("implied_pop")
    if tp is not None:
        edge = card.get("edge_pp")
        edge_html = (f'<span class="{"pos" if edge >= 0 else "neg"}">{edge:+.1f}pp edge</span>'
                     if edge is not None else "")
        imp = f'impl {implied*100:.0f}%' if implied is not None else ""
        conf = card.get("true_pop_conf")
        cf = ' <span class="dim">est</span>' if conf == "LOW" else ""
        return f'<b class="num {_rg("tpop",tp*100)}">{tp*100:.0f}%</b>{cf}<div class="dim num">{imp} · {edge_html}</div>'
    mp = card.get("model_pop")
    if mp is not None:
        imp = f'impl {implied*100:.0f}%' if implied is not None else ""
        return f'<b class="num">{mp*100:.0f}%</b> <span class="dim">est</span><div class="dim num">{imp} · no true-POP</div>'
    if implied is not None:
        return f'<b class="num">{implied*100:.0f}%</b><div class="dim">implied only</div>'
    return "—"


def _card_extras(c):
    price=c.get("price"); be=c.get("breakeven")
    cushion=c.get("cushion_pct")
    if cushion is None:
        cushion=((price-be)/price*100) if (price and be) else None
    width_usd=(c.get("width") or 0)*100
    max_loss=c.get("max_loss_usd")
    if max_loss is None:
        max_loss=(width_usd-(c.get("credit_usd") or 0)) if width_usd else None
    sc = c.get("edge_score") if c.get("edge_score") is not None else (c.get("priority") or 0)
    scls = "great" if sc>=80 else ("good" if sc>=70 else "fair")
    return cushion, max_loss, sc, scls


def _why_chosen(c):
    sig=[]
    vrp=c.get("vrp"); edge=c.get("edge_pp"); delta=c.get("delta")
    cushion,_,_,_=_card_extras(c); ivr=c.get("iv_rank"); roi=c.get("roi")
    if vrp is not None and vrp>0: sig.append((f"Positive VRP premium (+{vrp:.1f})", False))
    if edge is not None and edge>0: sig.append((f"True POP exceeds implied (+{edge:.1f}pp)", False))
    if delta is not None and abs(delta)<=0.25: sig.append((f"Short strike outside ~1SD (delta {delta:.2f})", True))
    if cushion is not None and cushion>=8: sig.append((f"Large downside cushion ({cushion:.1f}%)", True))
    if ivr is not None: sig.append((f"IV rank {ivr:.0f}", True))
    if roi is not None: sig.append((f"Return on capital {roi*100:.0f}%", True))
    if not sig: sig.append(("Passed the qualifying gate set", True))
    rows=[]
    for txt,ok in sig:
        if ok:
            rows.append('<div class="chk"><span class="k">'+'✓'+'</span>'+txt+'</div>')
        else:
            rows.append('<div class="chk amber"><span class="k">'+'▲'+'</span>'+txt+' <span style="color:var(--amber);font-size:9px">prov.</span></div>')
    return "".join(rows)


def _ev(c):
    """Gambler-edge expectancy. Returns dict or None if true_pop unavailable (legacy fast scan).
       EV/contract = p*max_profit − (1−p)*max_loss, net of modeled round-trip cost.
       breakeven win-rate = max_loss / (max_profit + max_loss) — the win% needed to break even."""
    tp=c.get("true_pop")
    _, max_loss, _, _ = _card_extras(c)
    mp=c.get("credit_usd")
    if mp is None or max_loss is None or max_loss<=0:
        return None
    cost=c.get("roundtrip_cost") or 0.0
    mp_net=mp-cost
    be_wr=max_loss/(mp_net+max_loss) if (mp_net+max_loss)>0 else None
    if tp is None:
        return {"ev": None, "be_wr": be_wr, "mp": mp_net, "ml": max_loss, "positive": None}
    ev=tp*mp_net-(1-tp)*max_loss
    return {"ev": ev, "be_wr": be_wr, "mp": mp_net, "ml": max_loss, "positive": ev>0}


def _reconcile(c):
    """Recompute displayed figures from primitives per strategy type and flag mismatches."""
    issues=[]
    st=c.get("strat_type","bull_put")
    short=c.get("short"); credit_ps=c.get("credit_ps"); width=c.get("width")
    credit_usd=c.get("credit_usd"); ml=c.get("max_loss_usd"); bes=c.get("breakevens") or []
    tp=c.get("true_pop"); imp=c.get("implied_pop"); edge=c.get("edge_pp"); roi=c.get("roi")
    if st=="bull_put" and short is not None and credit_ps is not None and bes:
        if abs(bes[0]-(short-credit_ps))>0.05: issues.append("breakeven!=short-credit")
    if st=="bear_call" and short is not None and credit_ps is not None and bes:
        if abs(bes[0]-(short+credit_ps))>0.05: issues.append("breakeven!=short+credit")
    if st=="iron_condor" and credit_ps is not None and len(bes)>=2:
        if c.get("put_short") is not None and abs(bes[0]-(c["put_short"]-credit_ps))>0.05: issues.append("put breakeven off")
        if c.get("call_short") is not None and abs(bes[1]-(c["call_short"]+credit_ps))>0.05: issues.append("call breakeven off")
    if width is not None and credit_usd is not None and ml is not None:
        if abs(ml-(width*100-credit_usd))>1.0: issues.append("max_loss!=width-credit")
    if credit_usd and ml and roi is not None:
        if abs(roi-(credit_usd/ml))>0.02: issues.append("ROC!=credit/max_loss")
    if tp is not None and imp is not None and edge is not None:
        if abs(edge-(tp-imp)*100)>0.2: issues.append("edge!=true-implied")
    if credit_ps is not None and credit_ps<=0: issues.append("non-positive credit")
    return (len(issues)==0), issues


def _freshness(board):
    """(label, css_class, stale_bool) for the data as-of stamp vs the ~15-min pull window."""
    # seed_demo.py writes fabricated trades to the SAME artifact the live engine uses, stamped
    # with a current timestamp — so an offline demo board otherwise reads as a fresh live one.
    # Say so before anything else: these are invented numbers, not quotes.
    if str(board.get("session") or "").upper() == "DEMO":
        return ("DEMO DATA — fabricated sample trades, NOT live quotes. Do not trade off this board.",
                "warn", True)
    asof=board.get("asof")
    if not asof:
        return ("no scan yet", "warn", True)
    try:
        # main.py stamps tz-aware ET. Stripping the offset and diffing against naive local time
        # made age negative on a non-ET box, which the age<0 clamp then showed as "0 min old" —
        # so a genuinely stale board always read as fresh.
        ts=datetime.fromisoformat(str(asof).replace("Z","+00:00").strip())
    except Exception:
        try: ts=datetime.strptime(str(asof)[:16], "%Y-%m-%d %H:%M")
        except Exception: return (f"as-of {esc(asof)}", "flash", False)
    now=datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    age=(now-ts).total_seconds()/60.0
    if age < -5:
        return (f"as-of {ts.strftime('%H:%M')} · timestamp is in the FUTURE (clock skew?) — freshness unknown",
                "warn", True)
    if age<0: age=0
    if age<=20: return (f"as-of {ts.strftime('%H:%M')} · {age:.0f} min old (within 15-min feed)", "flash", False)
    if age<=90: return (f"as-of {ts.strftime('%H:%M')} · {age:.0f} min old — rescan for fresh quotes", "warn", True)
    return (f"as-of {ts.strftime('%Y-%m-%d %H:%M')} · {age/60:.1f}h old — STALE, rescan", "warn", True)


def _criteria_panel(c):
    crit=c.get("criteria") or []
    nc=c.get("news_check") or {}
    if not crit and not nc: return ""
    lead=('<div class="valbanner">&#9888; New live calls-based strategy — spot-check the strikes, '
          'credit and greeks against your broker before acting (first-run validation).</div>'
          if c.get("needs_validation") else "")
    rows=""
    for x in crit:
        det=(' <span class="dim">'+esc(x.get("detail",""))+'</span>') if x.get("detail") else ""
        rows+='<div class="chk %s"><span class="k">%s</span>%s%s</div>'%(
            "" if x.get("ok") else "amber", chr(10003) if x.get("ok") else chr(9888), esc(x.get("label","")), det)
    news=""
    if nc:
        verd=nc.get("verdict",""); vcls={"CONFIRMS":"pos","BLOCKED":"neg"}.get(verd,"dim")
        news=(f'<div class="kv" style="margin-top:6px"><span class="k">News validation</span>'
              f'<b class="{vcls}">{esc(verd)}</b></div>'
              f'<div class="dim" style="font-size:11px">{esc(nc.get("detail",""))}</div>')
    return (f'<h4 style="margin-top:14px">Selection criteria &middot; fitted to strategy</h4>'
            f'{lead}{rows}{news}{_timing_block(c)}')


# Readiness → colour, shared by the drawer block, the order ticket and the row chip.
_TIMING_COLORS = {"OPTIMAL": "#00C97A", "WATCH": "#F0B429", "NEUTRAL": "#8892A0",
                  "EARLY": "#F0455A", "CAUTION": "#F0455A"}


def _levels_block(c):
    """Support / resistance with the evidence behind each level, and whether the short strike
    is actually sheltered by one. A bare price told the trader nothing about whether the
    market has ever defended it — a level tested four times and one random low read
    identically before 2026-08-05."""
    sup = c.get("support_levels") or []
    res = c.get("resistance_levels") or []
    if not sup and not res:
        return ""
    short = c.get("short") if c.get("strat_type") != "iron_condor" else c.get("put_short")
    rows = ""
    for kind, levels, colour in (("Resistance", res[:2], "#F0455A"),
                                 ("Support", sup[:2], "#00C97A")):
        for lv in levels:
            shield = ""
            if short and kind == "Support" and lv["price"] > float(short):
                shield = ' <span style="color:#00C97A">&#9679; shields the short strike</span>'
            elif short and kind == "Resistance" and lv["price"] < float(short):
                shield = ' <span style="color:#00C97A">&#9679; shields the short strike</span>'
            rows += (f'<div class="kv"><span class="k" style="color:{colour}">{kind}</span>'
                     f'<b>${lv["price"]:,.2f}</b></div>'
                     f'<div class="dim" style="font-size:11px">'
                     f'{lv["touches"]} touch{"es" if lv["touches"] != 1 else ""} &middot; '
                     f'last {lv["last_touch_bars_ago"]}b ago &middot; strength '
                     f'{lv["strength"]:.0f}{" &middot; flipped" if lv.get("flipped") else ""}'
                     f'{shield}</div>')
    return f'<h4 style="margin-top:14px">Key levels</h4>{rows}'


def _timing_block(c):
    """Pattern-phase explainer for the drawer. Advisory only — it never means the trade is
    disqualified, so the copy has to read as guidance, not as a failed gate."""
    et = c.get("entry_timing") or {}
    if not et:
        return ""
    readiness = et.get("readiness", "NEUTRAL")
    headline = et.get("headline") or (et.get("phase") or "").replace("_", " ").title()
    col = _TIMING_COLORS.get(readiness, "#8892A0")
    tgt = et.get("target_rsi")
    tgt_html = (f'<div class="dim" style="font-size:11px;margin-top:3px">Better timing: '
                f'{esc(str(tgt))}</div>') if tgt and tgt != "n/a" else ""

    # Measured evidence behind the phrase. Shown so the read can be checked against the
    # chart rather than taken on faith — these are heuristics, and they do misread.
    st = et.get("structure") or {}
    ev = []
    if st.get("impulse_pct") is not None:
        ev.append(f"impulse {st['impulse_pct']:.1f}%")
    if st.get("retracement_pct") is not None:
        ev.append(f"retraced {st['retracement_pct']:.0f}%")
    if st.get("swing_structure") and st["swing_structure"] != "FLAT":
        ev.append(st["swing_structure"].replace("_", " ").lower())
    for flag, yes, no in (("contracting", "range contracting", "range expanding"),
                          ("volume_drying", "volume drying up", "volume rising"),
                          ("momentum_flattening", "momentum flattening", None)):
        val = st.get(flag)
        if val is True and yes:
            ev.append(yes)
        elif val is False and no:
            ev.append(no)
    conf = st.get("confidence")
    if conf:
        ev.append(f"{conf.lower()} confidence")
    ev_html = (f'<div class="dim" style="font-size:11px;margin-top:3px">'
               f'{esc(" · ".join(ev))}</div>') if ev else ""
    lvl = st.get("level")
    lvl_html = ""
    if lvl:
        lvl_html = (f'<div class="kv"><span class="k">{esc(lvl["kind"].title())}</span>'
                    f'<b>${lvl["price"]:,.2f} &middot; {lvl["touches"]} touches</b></div>')
    return (f'{_levels_block(c)}'
            f'<h4 style="margin-top:14px">Entry timing &middot; chart structure '
            f'<span class="dim" style="font-weight:400">(advisory)</span></h4>'
            f'<div class="kv"><span class="k">{esc(headline)}</span>'
            f'<b style="color:{col}">{esc(et.get("readiness_icon",""))} {esc(readiness)}</b></div>'
            f'{lvl_html}'
            f'<div class="dim" style="font-size:11px">{esc(et.get("reason",""))}</div>'
            f'{ev_html}{tgt_html}')


def gate_detail_table(c):
    """The 'under the hood' data table: each hard gate + fitted criterion with its actual
       value and pass/warn status. Simple 8/8 lives on the row; this is what it expands into."""
    # Hard qualifying gates with the live value that satisfied each (qualified → all pass).
    ivr=c.get("iv_rank"); dl=c.get("delta"); ctw=c.get("credit_to_width")
    cush=c.get("cushion_pct"); cu=c.get("credit_usd"); dte=c.get("dte")
    tp=c.get("true_pop"); ip=c.get("implied_pop")
    hard=[
        ("IV rank",        f"{ivr:.0f}" if ivr is not None else "—",           f"≥ {IVR_MIN}"),
        ("Short delta",    f"{abs(dl):.2f}" if dl is not None else "—",          "within band"),
        ("OTM cushion",    f"{cush:.1f}%" if cush is not None else "—",          "short strike OTM"),
        ("Credit / width", f"{ctw*100:.0f}%" if ctw is not None else "—",        "≥ min"),
        ("Min credit",     f"${cu:.0f}" if cu is not None else "—",              "> 0"),
        ("Liquidity",      "ok",                                                 "vol / OI screen"),
        ("POP",            f"{(tp if tp is not None else ip or 0)*100:.0f}%",    "≥ threshold"),
        ("DTE window",     f"{dte}d" if dte is not None else "—",                "25–45d"),
    ]
    hrows="".join(
        f'<tr><td class="l">{esc(lab)}</td><td class="num">{esc(val)}</td>'
        f'<td class="l dim">{esc(rule)}</td><td class="gpass">&#10003; pass</td></tr>'
        for lab,val,rule in hard)
    # Fitted, strategy-specific criteria (these CAN warn while the trade still qualifies).
    crit=c.get("criteria") or []
    crows=""
    for x in crit:
        ok=x.get("ok"); st=('<td class="gpass">&#10003; pass</td>' if ok else '<td class="gwarn">&#9888; soft</td>')
        crows+=(f'<tr><td class="l">{esc(x.get("label",""))}</td><td class="num" colspan="2">'
                f'{esc(x.get("detail",""))}</td>{st}</tr>')
    crit_hdr=('<tr class="gsub"><td colspan="4" class="l">Strategy-fitted criteria</td></tr>'+crows) if crows else ""
    passed=c.get("gates_passed"); total=c.get("gates_total")
    return (f'<h4 style="margin-top:14px">Gate detail — {passed}/{total} hard gates '
            f'<span class="dim">(click any row header on the board to collapse)</span></h4>'
            f'<table class="gtbl"><thead><tr><th class="l">Check</th><th>Value</th>'
            f'<th class="l">Rule</th><th>Status</th></tr></thead><tbody>{hrows}{crit_hdr}</tbody></table>')


def notes_section(c):
    """Notes / analyst annotations. Shows the engine's own reasoning + any warnings as notes,
       and the persisted per-trade note when one exists (outcome_logger 'note' field)."""
    bits=[]
    auto=c.get("auto_reasoning")
    if auto: bits.append(('engine', esc(auto)))
    nc=c.get("news_check") or {}
    if nc.get("detail"): bits.append(('news', esc(nc.get("detail"))))
    for w in (c.get("warnings") or []):
        bits.append(('flag', esc(w)))
    note=c.get("note")
    if note: bits.append(('you', esc(note)))
    if not bits:
        body='<div class="dim" style="font-size:12px">No notes yet.</div>'
    else:
        body="".join(f'<div class="noterow"><span class="ntag nt-{t}">{t}</span>{txt}</div>' for t,txt in bits)
    return f'<h4 style="margin-top:14px">Notes</h4><div class="notes">{body}</div>'


def _cost_bases():
    """The two round-trip cost figures, computed from config so labels stay truthful.
       modeled (pre-trade EV, conservative: commission + assumed slippage) vs
       paper (realized ledger P/L, commission-only — real fills capture slippage)."""
    try:
        leg = float(getattr(config, "COMMISSION_PER_CONTRACT_PER_LEG", 0.54))
        legs = int(getattr(config, "LEGS_PER_SPREAD", 2))
        paper = round(leg * legs * 2, 2)  # both legs, both directions
        slip = (float(getattr(config, "ASSUMED_ENTRY_SLIPPAGE_PER_SHARE", 0.02))
                + float(getattr(config, "ASSUMED_EXIT_SLIPPAGE_PER_SHARE", 0.02))) * 100
        modeled = round(paper + slip, 2)
        return paper, modeled
    except Exception:
        return 2.16, 6.16


def _cost_note():
    paper, modeled = _cost_bases()
    return (f'<div class="ln" style="color:var(--ink4);margin-top:4px">EV is net of modeled friction '
            f'~${modeled:.2f}/ct (commission + assumed slippage). Realized paper P/L in Track Record nets '
            f'commission-only ~${paper:.2f}/ct — real fills capture slippage.</div>')


def _edge_block(c):
    e=_ev(c)
    if not e:
        return '<div class="edgebox"><div class="cap">Gambler edge (EV)</div><div class="v" style="color:var(--ink3)">n/a</div></div>'
    if e["ev"] is None:
        bw=("%.0f%%"%(e["be_wr"]*100)) if e["be_wr"] is not None else "-"
        return (f'<div class="edgebox"><div class="cap">Gambler edge - break-even win rate</div>'
                f'<div class="v" style="color:var(--ink2)">{bw}</div>'
                f'<div class="ln">Needs engine true-POP for expected value (run the full engine).</div></div>')
    cls="pos" if e["positive"] else "neg"
    tp=c.get("true_pop")*100; bw=e["be_wr"]*100
    verdict="POSITIVE expectancy - the bet pays" if e["positive"] else "negative expectancy - skip on the math"
    return (f'<div class="edgebox"><div class="cap">Gambler edge - expected value / contract</div>'
            f'<div class="v {cls}">${e["ev"]:+.0f}</div>'
            f'<div class="ln">Need to win <b>{bw:.0f}%</b> to break even &middot; model says <b>{tp:.0f}%</b> &rarr; {verdict}</div>'
            f'{_cost_note()}</div>')


def _verification(c):
    rows=[]
    sent=(c.get("news_sentiment") or "").upper(); summ=c.get("news_summary") or ""
    if sent:
        ok = sent in ("POSITIVE","NEUTRAL")
        rows.append((f"News sentiment: {sent}"+(f" - {summ}" if summ else ""), ok))
    warns=c.get("warnings") or []
    earn=[w for w in warns if "earn" in str(w).lower()]
    if earn: rows.append(("Earnings risk: "+"; ".join(map(str,earn)), False))
    else: rows.append(("No blocking earnings within the trade window", True))
    trend=c.get("trend"); rsi=c.get("rsi"); sup=c.get("nearest_support")
    tl=[]
    if trend: tl.append(f"trend {trend}")
    if rsi is not None: tl.append(f"RSI {rsi:.0f}")
    if sup is not None: tl.append(f"support ${sup:.2f}")
    if tl: rows.append(("Technical: "+", ".join(tl), True))
    for w in warns:
        if w not in earn: rows.append((f"Flag: {w}", False))
    fr=c.get("fundamentals_reasons") or []
    if fr: rows.append(("Fundamentals: "+"; ".join(map(str,fr[:2])), True))
    if not rows: return ""
    body="".join('<div class="chk %s"><span class="k">%s</span>%s</div>'%("" if ok else "amber", chr(10003) if ok else chr(9888), esc(t)) for t,ok in rows)
    auto=c.get("auto_reasoning")
    lead=f'<p class="q" style="margin:0 0 6px">{esc(auto)}</p>' if auto else ""
    return f'<h4 style="margin-top:14px">Verification - theory vs the tape</h4>{lead}{body}'


def alternatives_panel(c):
    """Suggested strategies for THIS ticker: the engine's ranked alternatives (suppressed_strategies)
       plus the environment-recommended structure when the tape is heating against the primary side."""
    rows = []
    prim = (c.get("strategy") or (c.get("strat_type") or "").replace("_", " ").title())
    rows.append(('primary', f'<b>{esc(prim)}</b> {esc(c.get("structure") or "")}',
                 f'score {c.get("edge_score") or 0:.0f}', 'gpass'))
    for s in (c.get("suppressed_strategies") or []):
        nm = (s.get("strategy") or "").replace("_", " ").title()
        strk = ""
        if s.get("short_strike") is not None and s.get("long_strike") is not None:
            strk = f'{s["short_strike"]:g}/{s["long_strike"]:g}'
        rows.append(('alt', f'{esc(nm)} {esc(strk)}', f'score {s.get("edge_score") or 0:.0f}', 'dim'))
    e = c.get("env") or {}
    rec = e.get("recommendation") or {}
    if e.get("band") in ("warm", "hot"):
        tgt = rec.get("alt_structure") or (rec.get("strike_note") if rec.get("action", "").startswith(("flip", "condor")) else None)
        if tgt:
            rows.append(('env', f'{esc(tgt)}', f'environment {e.get("band")}', 'gwarn'))
    if len(rows) == 1:
        body = ('<div class="dim" style="font-size:12px">Top-ranked structure for this name — '
                'the engine found no competitive alternative and the environment is calm.</div>')
    else:
        body = ""
        for kind, txt, meta, cls in rows:
            tag = {"primary": "PRIMARY", "alt": "ALSO RANKED", "env": "ENV FLIP"}.get(kind, "")
            body += (f'<div class="altrow"><span class="alttag alt-{kind}">{tag}</span>'
                     f'<span class="alttxt">{txt}</span><span class="{cls} altmeta">{esc(meta)}</span></div>')
    return f'<h4 style="margin-top:14px">Suggested structures — {esc(c["ticker"])}</h4><div class="alts">{body}</div>'


def _legs_text(c):
    """Human-readable legs for the contract header, spelled out per structure."""
    st = c.get("strat_type", "bull_put")
    def g(x): return f"{x:g}" if x is not None else "?"
    if st == "iron_condor":
        return (f'Put spread <b>{g(c.get("put_short"))}/{g(c.get("put_long"))}</b> '
                f'<span class="dim">+</span> Call spread <b>{g(c.get("call_short"))}/{g(c.get("call_long"))}</b>')
    if st == "bear_call":
        return f'Call spread <b>{g(c.get("short"))}/{g(c.get("long"))}</b> (sell {g(c.get("short"))}C / buy {g(c.get("long"))}C)'
    return f'Put spread <b>{g(c.get("short"))}/{g(c.get("long"))}</b> (sell {g(c.get("short"))}P / buy {g(c.get("long"))}P)'


def contract_header(c):
    """Prominent banner atop the drawer so the strategy + full contract is unmistakable."""
    strat = (c.get("strategy") or (c.get("strat_type") or "bull_put").replace("_", " ").title())
    strat = strat.replace(" (fast scan)", "")
    chip = _type_chip(c)
    lab, dte, approx, dt = _exp_parts(c)
    exp_txt = ""
    if dt is not None:
        exp_txt = f'{dt.strftime("%a %b %d, %Y")}' + ("~" if approx else "") + (f' · {dte} DTE' if dte is not None else "")
    elif dte is not None:
        exp_txt = f'{dte} DTE'
    credit = c.get("credit_usd")
    ml = c.get("max_loss_usd")
    meta = []
    if exp_txt: meta.append(f'<span class="chm">Expires</span> <b class="expd">{esc(exp_txt)}</b>')
    if credit is not None: meta.append(f'<span class="chm">Credit</span> <b class="rg-opt">+${credit:.0f}</b>')
    if ml is not None: meta.append(f'<span class="chm">Max loss</span> <b style="color:var(--red)">-${ml:.0f}</b>')
    return (f'<div class="chdr"><div class="chtop">{chip}<span class="chstrat">{esc(strat)}</span>'
            f'<span class="chlegs">{_legs_text(c)}</span></div>'
            f'<div class="chmeta">{" &nbsp;·&nbsp; ".join(meta)}</div></div>')


def detail_drawer(c, i, tier):
    cushion, max_loss, sc, scls = _card_extras(c)
    rok, riss = _reconcile(c)
    rec_html=('<span style="color:var(--green)">&#10003; reconciled</span>' if rok else '<span style="color:var(--amber)">&#9888; '+esc("; ".join(riss))+'</span>')
    diag = payoff_typed(c, 300, 110)
    delta=c.get("delta")

    def kv(k,v): return f'<div class="kv"><span class="k">{k}</span><b class="num">{v}</b></div>'
    _elab, _edte, _eapprox, _edt = _exp_parts(c)
    if _edt is not None:
        _efull = _edt.strftime("%a %b %d, %Y") + (" ~" if _eapprox else "")
        _eval = f'{_efull} &middot; {_edte} DTE' if _edte is not None else _efull
    else:
        _eval = f'{_edte} DTE' if _edte is not None else "-"
    kvs=[kv("Structure", f'{esc(c.get("structure") or "")} &middot; w{(c.get("width") or 0):g}'),
         f'<div class="kv"><span class="k">Expiration</span><b class="num" style="color:var(--amber)">{_eval}</b></div>',
         kv("Strategy", esc((c.get("strat_type") or "bull_put").replace("_"," ").title())),
         kv("Credit", f'${(c["credit_usd"] or 0):.0f} (${c["credit_ps"]:.2f}/sh)')]
    if max_loss is not None: kvs.append(f'<div class="kv"><span class="k">Max loss</span><b class="num" style="color:var(--red)">-${max_loss:.0f}</b></div>')
    _bes=c.get("breakevens") or ([c["breakeven"]] if c.get("breakeven") is not None else [])
    kvs.append(kv("Breakeven"+("s" if len(_bes)>1 else ""), " / ".join(f"${b:.2f}" for b in _bes) or "-"))
    if c.get("true_pop") is not None: kvs.append(f'<div class="kv"><span class="k">True POP</span><b class="num" style="color:var(--green)">{c["true_pop"]*100:.0f}%</b></div>')
    if c.get("implied_pop") is not None: kvs.append(kv("Implied POP", f'{c["implied_pop"]*100:.0f}%'))
    if c.get("edge_pp") is not None: kvs.append(f'<div class="kv"><span class="k">POP edge</span><b class="num" style="color:var(--{"green" if c["edge_pp"]>=0 else "red"})">{c["edge_pp"]:+.1f}pp</b></div>')
    if c.get("roi") is not None: kvs.append(kv("ROC", f'{c["roi"]*100:.0f}%'))
    kvs.append(f'<div class="kv"><span class="k">VRP (IV-RV)</span><b class="num">{esc(c.get("vrp"))}</b></div>')
    kvs.append(kv("True-POP drift", esc(c.get("drift_mode") or "-")))

    hidden=(f'<input type="hidden" name="ticker" value="{esc(c["ticker"])}">'
            f'<input type="hidden" name="short" value="{esc(c["short"])}">'
            f'<input type="hidden" name="long" value="{esc(c["long"])}">'
            f'<input type="hidden" name="exp" value="{esc(c["exp"])}">'
            f'<input type="hidden" name="credit" value="{esc(c["credit_ps"])}">'
            f'<input type="hidden" name="dte" value="{esc(c.get("dte") or "")}">'
            f'<input type="hidden" name="delta" value="{esc(delta if delta is not None else "")}">'
            f'<input type="hidden" name="implied_pop" value="{esc(c.get("implied_pop") or "")}">'
            f'<input type="hidden" name="iv_rank" value="{esc(c.get("iv_rank") if c.get("iv_rank") is not None else "")}">')
    # The decision snapshot. Wider than the open form's fields because a WATCH or a REJECT has
    # to be gradeable months later against a chain that no longer exists — a row saying
    # "rejected WMT" with no entry state is a diary entry, not data.
    dec_hidden = hidden + "".join(
        f'<input type="hidden" name="{k}" value="{esc(v if v is not None else "")}">'
        for k, v in (("strategy", c.get("strat_type") or c.get("strategy")),
                     ("true_pop", c.get("true_pop")),
                     ("pop_implied", c.get("implied_pop")),
                     ("edge_score", c.get("edge_score") or c.get("priority")),
                     ("vrp", c.get("vrp")),
                     ("roi", c.get("roi")),
                     ("credit_usd", c.get("credit_usd")),
                     ("max_loss_usd", c.get("max_loss_usd")),
                     ("spot", c.get("price"))))
    log_form=(f'<form class="inline" method="post" action="/open_board" style="margin-top:12px">{hidden}'
              f'<input class="n" type="number" name="contracts" value="1" min="1">'
              f'<button class="go" type="submit" data-busy="Logging...">Log paper trade</button></form>'
              f'<form class="inline" method="post" action="/watch" style="margin-top:12px">{dec_hidden}'
              f'<button class="ghostbtn" type="submit" data-busy="Saving..." '
              f'title="Track it without committing. A setup you passed on that later works is '
              f'the cheapest lesson available.">Watch</button></form>'
              f'<form class="inline" method="post" action="/reject" style="margin-top:12px">{dec_hidden}'
              f'<button class="ghostbtn danger" type="submit" data-busy="Saving..." '
              f'title="Record that you declined this. Over enough rows this answers whether '
              f'your overrides beat the engine or cost you.">Reject</button></form>')

    conf=c.get("true_pop_conf") or "-"
    deep=(f'<div><div class="payhd"><span>Payoff at expiration</span><span>{esc(c.get("dte"))} DTE</span></div>{diag}'
          f'<h4 style="margin-top:12px">Key metrics - {esc(c["ticker"])}</h4>{"".join(kvs)}</div>'
          f'<div>{_edge_block(c)}{_verification(c)}{env_panel(c)}{_levels_block(c)}'
          f'{_timing_block(c)}{alternatives_panel(c)}</div>'
          f'<div>{score_composition(c)}{gate_detail_table(c)}'
          f'<h4 style="margin-top:14px">Confidence</h4>'
          f'<div class="kv"><span class="k">Hard gates</span><b>{gate_dots(c)} {c["gates_passed"]}/{c["gates_total"]}</b></div>'
          f'<div class="kv"><span class="k">True-POP confidence</span><b>{esc(conf)}</b></div>'
          f'<div class="kv"><span class="k">Gate 1 status</span><b>{tier_badge(tier)}</b></div>'
          f'<div class="kv"><span class="k">Figures reconciled</span><b>{rec_html}</b></div>'
          f'{notes_section(c)}</div>')
    return _copilot(c, i, tier, log_form, deep)


def _edge_decomposition(c):
    """What the headline edge score is made of, on one line.

    A bare "78" cannot be argued with: the reader has no way to tell whether it came from a
    rich premium, a probability gap, or the chart. The full composition panel already exists
    behind "Full analysis", but a number the reader has to click to understand is a number
    they take on faith at the moment they are deciding. This surfaces the three or four
    largest contributors where the score is shown, and stays silent when the breakdown is
    absent (fast scan) rather than reconstructing one from the score.
    """
    bd = c.get("component_breakdown") or {}
    if not bd:
        return ""
    labels = dict((k, lbl) for k, lbl, _ in EDGE_COMPONENTS)
    labels.update((k, lbl) for k, lbl, _ in EDGE_BONUSES)
    got = [(labels.get(k, k.replace("_", " ").title()), v)
           for k, v in bd.items() if isinstance(v, (int, float)) and v > 0]
    if not got:
        return ""
    got.sort(key=lambda kv: kv[1], reverse=True)
    parts = " &middot; ".join(f'+{v:g} {esc(lbl)}' for lbl, v in got[:4])
    return f'<div class="edgedecomp">{parts}</div>'


def _pattern_read(c):
    """(pattern label, direction) for a card, or (None, None) when the chart was unreadable.

    The shape lives on the entry-timing payload — `structure` there is the chart-pattern dict
    from analysis.structure, NOT the candidate's own `structure` field, which is the strike
    string ("100/95P"). Same word, two meanings, one of them a nested dict.
    """
    st = ((c.get("entry_timing") or {}).get("structure") or {})
    pat = st.get("pattern") or st.get("phrase")
    if not pat:
        return None, None
    from analysis.structure import get_pattern_direction
    return pat, get_pattern_direction(pat)


def _thesis_contradiction(c):
    """The caution string when the chart points against the trade, else None."""
    _, direction = _pattern_read(c)
    if not direction:
        return None
    from analysis.structure import check_thesis_contradiction
    strat = c.get("strat_type") or c.get("strategy")
    return check_thesis_contradiction(direction, strat)


def _copilot_why(c):
    """The case for the trade, split into the three questions a reader actually asks.

    The old drawer led with a payoff diagram and a 14-row metric table, which asks the reader
    to assemble the case themselves. The engine already HAS the case — it just never stated
    it. But stating it as one undifferentiated stream had its own failure: IV rank, VRP, POP,
    resistance, timing, news and concentration all arrived at the same visual weight, so the
    reader had to sort supporting evidence from risk from VEGA's own reservations while
    reading. Three labelled buckets do that sorting once, here:

      WHY IT WORKS          the evidence for the trade
      WHAT CAN BREAK IT     what the MARKET would have to do to hurt it
      WHAT VEGA DOESN'T LIKE  what is wrong with the setup or your book regardless

    The split matters because the last two are different decisions. A market risk is priced
    and accepted; a reservation about concentration or a chart that disagrees is a reason to
    pass. Merging them lets a real objection read as ordinary trade risk.
    """
    works, breaks, dislikes = [], [], []

    def _row(bucket, glyph, cls, txt):
        bucket.append(f'<div class="row {cls}"><span class="tick">{glyph}</span>'
                      f'<span>{txt}</span></div>')

    def ok(txt):       _row(works, "&#10003;", "", txt)
    def risk(txt):     _row(breaks, "&#9888;", "warn", txt)
    def dislike(txt):  _row(dislikes, "&#9888;", "warn", txt)

    ivr = c.get("iv_rank")
    if ivr is not None and ivr >= 50:
        ok(f'IV rank {ivr:.0f} — options are priced rich relative to the past year')
    elif ivr is not None:
        dislike(f'IV rank {ivr:.0f} — premium is not especially rich here')

    vrp = c.get("vrp")
    if vrp is not None and vrp > 0:
        ok(f'Implied vol exceeds FORECAST realised by {vrp:.1f}pp — the premium you sell is '
           f'overpriced versus how much the stock is expected to move over this horizon')
    elif vrp is not None:
        dislike(f'Implied vol is {abs(vrp):.1f}pp BELOW forecast realised — the options are '
                f'cheap relative to the movement expected, which is the wrong side to sell')

    # Where the vol forecast differs from the trailing number, and why. This is the single
    # correction that most often changes whether a name qualifies at all, so it is stated
    # rather than left inside the score.
    st, shift = c.get("vol_state"), c.get("vrp_shift_pp")
    if st and shift is not None and abs(shift) >= 0.5:
        if st == "EXPANDING":
            ok(f'Vol has spiked and typically reverts — forecasting the horizon instead of the '
               f'trailing window moves VRP {shift:+.1f}pp in favour of this trade')
        elif st == "COMPRESSING":
            risk(f'Vol is unusually compressed and typically drifts back up — forecasting the '
                 f'horizon moves VRP {shift:+.1f}pp against this trade')

    edge = c.get("edge_pp")
    if edge is not None and edge > 0:
        ok(f'VEGA POP beats what the market is pricing by {edge:.1f}pp')
    elif edge is not None:
        dislike(f'VEGA POP trails the market\'s own pricing by {abs(edge):.1f}pp')

    # The structural read — this is the part no other retail screener has.
    sh = _shelter_note(c)
    if sh:
        # A condor's note is a standalone clause ("both wings sheltered"); the directional
        # ones are prepositional ("under $240.07 support"). Reads as broken English if the
        # sentence assumes one shape.
        lead = ("Short strike sits " if sh[:5] in ("under", "over ") else "")
        ok(f'{(lead + sh) if lead else sh[:1].upper() + sh[1:]} — the market has to break a '
           f'level it has defended before the position is threatened')
        # Same level, stated as the thing that would go wrong. The shelter is the reason to
        # take the trade AND the single point of failure, and only ever showing the first
        # half is how a defended level starts reading as a guarantee.
        lvl = _defended_level(c)
        if lvl is not None:
            risk(f'That level failing — a decisive break of ${lvl:,.2f} puts the short strike '
                 f'in open air')

    et = c.get("entry_timing") or {}
    if et:
        if et.get("timing_gate_pass", True):
            head = et.get("headline") or ""
            if head:
                ok(f'Entry timing: {head.lower()}')
        else:
            risk(f'Entry timing: {et.get("readiness", "").title()} — '
                 f'{(et.get("headline") or "").lower()}. Premium may improve if deferred')

    roi = c.get("roi")
    if roi is not None:
        ok(f'Return on capital {roi*100:.0f}% for the risk taken')

    if (c.get("news_sentiment") or "").upper() in ("NEUTRAL", "POSITIVE", "CLEAR"):
        ok('No blocking news against the thesis')

    # Selling premium is short vol by construction, so an IV expansion is the standing risk on
    # every one of these trades and it belongs on the list whether or not anything flagged it.
    if ivr is not None and ivr >= 50:
        risk(f'Volatility expanding — you are short vol at IV rank {ivr:.0f}, and a spike '
             f'marks the position against you before any move in the underlying')

    env = c.get("env") or {}
    if env.get("band") in ("warm", "hot"):
        risk(f'Environment is {env["band"]} ({env.get("heat", 0)}/100) — '
             f'consider a smaller size')

    if c.get("already_in_position"):
        dislike(f'You already hold {esc(c["ticker"])} — this adds concentration, not diversification')
    # Chart shape against trade direction. A reservation, not a market risk: the conflict is
    # already true at entry rather than something the market might later do.
    contra = _thesis_contradiction(c)
    if contra:
        dislike(esc(contra))

    if not works:
        ok('Passed the full qualifying gate set')

    conf = (c.get("true_pop_conf") or "").upper()
    conf_badge = ""
    if conf:
        ccol = ("var(--green)" if conf == "HIGH" else
                "var(--red)" if conf == "LOW" else "var(--amber)")
        # Directly under the evidence it qualifies. At the foot of the card it read as a
        # footnote to the whole section, when what it actually grades is the POP claim above.
        conf_badge = (f'<div class="confbadge" style="color:{ccol}">'
                      f'Model confidence: {conf.title()}</div>')

    def section(title, rows, cls=""):
        if not rows:
            return ""
        return (f'<div class="whyblk {cls}"><div class="bh">{title}</div>'
                + "".join(rows) + '</div>')

    return (section("Why it works", works) + conf_badge
            + section("What can break it", breaks, "risk")
            + section("What VEGA doesn't like", dislikes, "risk"))


def _defended_level(c):
    """The price the shelter note is asserting — the one that has to hold.

    Reads the level from `strike_cushion`, the SAME selector _shelter_note uses, because the
    two sentences sit next to each other and are about the same level. Picking the nearest
    price independently looked equivalent and was not: strike_cushion returns the
    highest-STRENGTH level clearing the buffer, not the closest one, so the card printed
    "under $238.00 support" directly above "a decisive break of $245.00 puts the short strike
    in open air". Two numbers for one level, and the risk line was the false one.

    Returns None when no level was found, so the failure mode is a missing sentence rather
    than an invented price.
    """
    try:
        from analysis.levels import strike_cushion
    except Exception:
        return None
    strat = c.get("strat_type") or "bull_put"
    if strat == "iron_condor":
        return None                      # two wings, and naming one would say the other is safe
    if strat == "bear_call":
        levels, side = c.get("resistance_levels"), "call"
    else:
        levels, side = c.get("support_levels"), "put"
    strike = c.get("short")
    if not strike or not levels:
        return None
    try:
        cush = strike_cushion(float(strike), levels, side,
                              min_buffer_pct=float(getattr(config, "LEVEL_MIN_BUFFER_PCT", 0.005)))
    except Exception:
        return None
    return _f(cush.get("level")) if cush else None


def _copilot(c, i, tier, log_form, deep):
    """AI Copilot: the recommendation, the case for it, and the alternatives — in that order.

    Metrics are still all here; they moved behind "Full analysis" so the first screen answers
    "should I take this and why" rather than "here is everything the engine computed".
    """
    sc = (c.get("edge_score") or c.get("priority") or 0)
    tp = c.get("true_pop")
    roi = c.get("roi")
    ml = c.get("max_loss_usd")
    # "Best overall opportunity" says nothing about the field it won. #1 of 27 is a different
    # claim from #1 of 2, and the reader cannot tell which they are looking at without the
    # denominator — the same gap the density funnel closes for the board as a whole.
    n_board = len(_COPILOT_PEERS) or 0
    of_n = f" of {n_board} qualified" if n_board > 1 else ""
    rank_note = (f"Top pick{of_n} today" if i == 0
                 else f"Ranked #{i+1}{of_n} on today's board")

    def num(k, v, col=""):
        return (f'<div class="c"><span class="k">{k}</span>'
                f'<span class="v num"{f" style=color:{col}" if col else ""}>{v}</span></div>')

    # "Win probability" said nothing about WHOSE probability it was. The number has always
    # been true_pop — VEGA's drift-removed estimate, not the market's — and on a system whose
    # entire claim is that the market misprices probability, showing one of the two without
    # naming it is the one ambiguity that cannot be afforded. Both are shown, and the gap
    # between them IS the claim, so it is displayed rather than left to be worked out.
    ip = c.get("implied_pop")
    gap_pp = c.get("edge_pp")                       # already in percentage points
    if gap_pp is None and tp is not None and ip is not None:
        gap_pp = (float(tp) - float(ip)) * 100
    gap_col = ("var(--green)" if gap_pp > 0 else "var(--red)") if gap_pp is not None else ""

    nums = ('<div class="copnums">'
            + num("Edge score", f'{sc:.0f}', "var(--green)" if sc >= 80 else "var(--amber)")
            + num("VEGA POP", f'{tp*100:.0f}%' if tp is not None else "-")
            + num("Market POP", f'{ip*100:.0f}%' if ip is not None else "-")
            + num("VRP edge (VEGA &minus; market)",
                  f'{gap_pp:+.1f}pp' if gap_pp is not None else "-", gap_col)
            + num("Return on capital", f'{roi*100:.0f}%' if roi is not None else "-")
            + num("Credit", f'${c["credit_usd"]:.0f}' if c.get("credit_usd") is not None else "-",
                  "var(--green)")
            + num("Max loss", f'-${ml:.0f}' if ml is not None else "-", "var(--red)")
            + '</div>')

    rec = ('<div class="copcard rec coprec">'
           '<div class="who">VEGA recommendation</div>'
           f'<div class="nm">{esc(c["ticker"])} <span>{esc(_strat_label(c))}</span></div>'
           f'<div class="copbadge">{rank_note}</div>'
           f'{nums}{_edge_decomposition(c)}</div>')

    why = ('<div class="copcard copwhy"><div class="hd">Why VEGA likes this trade</div>'
           f'{_copilot_why(c)}</div>')

    ideas = _copilot_other_ideas(i)
    snap = _copilot_snapshot(c)
    impact = _copilot_impact(c)
    action = _copilot_action(c, log_form)

    return ('<div class="cop">'
            f'<div class="coptop">{rec}{why}{ideas}</div>'
            f'<div class="copmid">{snap}{impact}</div>'
            f'{_price_band_html(c)}'
            f'{action}'
            '<details class="copmore"><summary>Full analysis &mdash; payoff, metrics, gates, '
            'score composition</summary>'
            f'<div class="copdeep">{deep}</div></details>'
            '</div>')


def _copilot_other_ideas(i):
    """Alternatives, so a recommendation never reads as the only option. Populated by
    view_today via _COPILOT_PEERS because the drawer is built per row and has no view of the
    rest of the board."""
    peers = [p for j, p in enumerate(_COPILOT_PEERS) if j != i][:5]
    if not peers:
        return ('<div class="copcard"><div class="hd">Other top ideas</div>'
                '<div class="dim" style="font-size:11.5px">No other qualified setups today.</div></div>')
    # Compare against the TOP pick, not the card being viewed. Using the open card as the
    # reference made the annotation contradict the two numbers beside it: in the #2 drawer,
    # the #1 row rendered as "1 | AAPL | Edge 92 | Technical 2 pts behind PEP" — higher rank,
    # higher score, and a line claiming it trails. A row that outranks the lead gets no
    # annotation at all, because "ranks lower" is not true of it.
    lead = _COPILOT_PEERS[0][1] if _COPILOT_PEERS else {}
    rows = ""
    for j, p in peers:
        sc = (p.get("edge_score") or p.get("priority") or 0)
        why = "" if j == 0 else _ranks_lower_because(p, lead)
        rows += (f'<a class="copidea" onclick="event.stopPropagation();vopen({j})">'
                 f'<span class="r num">{j+1}</span><span class="t">{esc(p["ticker"])}</span>'
                 f'<span class="s">{esc(_strat_label(p))}</span>'
                 f'<span class="e num">Edge {sc:.0f}</span>'
                 + (f'<span class="wl">{why}</span>' if why else "")
                 + '</a>')
    return f'<div class="copcard"><div class="hd">Other top ideas</div>{rows}</div>'


def _ranks_lower_because(p, lead):
    """One clause naming the largest component where this setup lost to the top pick.

    A ranked list with no reasons is a leaderboard; a ranked list WITH reasons teaches the
    reader how VEGA weighs things, which is the only way they ever learn to disagree with it
    usefully. Derived from the score composition both cards already carry — nothing new is
    computed, and when either breakdown is missing the line is simply omitted.
    """
    pb, lb = (p.get("component_breakdown") or {}), (lead.get("component_breakdown") or {})
    if not pb or not lb or p is lead:
        return ""
    labels = dict((k, lbl) for k, lbl, _ in EDGE_COMPONENTS)
    labels.update((k, lbl) for k, lbl, _ in EDGE_BONUSES)
    deficits = [(labels.get(k, k.replace("_", " ")), lb.get(k, 0) - pb.get(k, 0))
                for k in set(lb) | set(pb)
                if isinstance(lb.get(k, 0), (int, float))
                and isinstance(pb.get(k, 0), (int, float))]
    deficits = [d for d in deficits if d[1] > 0]
    if not deficits:
        return "Ranks lower on the composite, not on any single component"
    lbl, gap = max(deficits, key=lambda kv: kv[1])
    return f'{esc(lbl)} {gap:g} pts behind {esc(lead.get("ticker") or "the top pick")}'


def _copilot_snapshot(c):
    """What the wider tape looks like, so the trade is read in context rather than alone."""
    ctx = _COPILOT_CTX or {}
    vix = (ctx.get("vix") or {})
    vixc = vix.get("current")
    spy = (ctx.get("spy") or {})
    ivr = c.get("iv_rank")
    rsi = c.get("rsi")

    def cell(k, v, s="", col=""):
        return (f'<div class="c"><span class="k">{k}</span>'
                f'<span class="v num"{f" style=color:{col}" if col else ""}>{v}</span>'
                f'<span class="s">{s}</span></div>')
    return ('<div class="copcard"><div class="hd">Market snapshot</div><div class="copgrid">'
            + cell("VIX", f'{vixc:.1f}' if vixc is not None else "-",
                   esc((vix.get("label") or "").title()))
            + cell("IV rank", f'{ivr:.0f}' if ivr is not None else "-",
                   "Rich" if (ivr or 0) >= 50 else "Thin")
            + cell("SPY today", f'{spy.get("day_change_pct", 0):+.1f}%' if spy else "-",
                   esc((ctx.get("bias") or "").title()))
            + cell("RSI", f'{rsi:.0f}' if rsi is not None else "-",
                   esc((c.get("trend") or "").replace("_", " ").title()))
            + '</div></div>')


def _price_band(c, confidence=None):
    """The projected price window at expiry for one card, or None.

    Built from the SAME forecast vol the VRP uses, so the band and the edge score cannot tell
    the reader two different stories about how much the stock is expected to move.
    """
    try:
        from analysis import price_projection as pp
        vol = c.get("rv_forecast_pp")
        if vol is None:
            return None
        st = c.get("strat_type") or "bull_put"
        side = "call" if st == "bear_call" else "put"
        strike = c.get("call_short") if side == "call" else (c.get("short") or c.get("put_short"))
        return pp.for_candidate(c.get("price"), c.get("dte"), {"forecast_pp": vol},
                                short_strike=strike, side=side, confidence=confidence)
    except Exception as e:                                # pragma: no cover - defensive
        logger.debug("[projection] failed for %s: %s", c.get("ticker"), e)
        return None


def _market_band_rows(b, mkt):
    """The market's own range beside VEGA's, and the disagreement between them.

    The market band is read off option DELTAS, not modelled: an option's delta is roughly the
    risk-neutral probability it finishes in the money, so the strike of a 10-delta put IS the
    price the market gives a 10% chance of being below. That band already contains skew, fat
    tails and event risk — everything a single ATM vol plugged into a lognormal cannot say —
    and it is ASYMMETRIC, which a symmetric band actively contradicts.

    The comparison is the whole point. A narrower VEGA band means the market is paying for
    more movement than the engine expects, which IS the premium-selling thesis, stated as a
    picture the reader can check rather than a score they have to trust.
    """
    if not mkt:
        return ""
    try:
        from analysis import price_projection as pp
        cmp_ = pp.compare_bands(b, mkt)
    except Exception:
        cmp_ = None
    rows = (f'<div class="pbrow"><span class="k">Market expects</span>'
            f'<b class="num">${mkt["low"]:,.2f} &ndash; ${mkt["high"]:,.2f}</b>'
            f'<span class="dim">{mkt["low_pct"]:+.1f}% / {mkt["high_pct"]:+.1f}% '
            f'&middot; read off {mkt["low_delta"]:.2f}&Delta; put and {mkt["high_delta"]:.2f}&Delta; '
            f'call, so it carries the skew</span></div>')
    if cmp_:
        cls = "pos" if cmp_["favours_seller"] else "neg"
        rows += (f'<div class="pbrow"><span class="k">Disagreement</span>'
                 f'<b class="num {cls}">{cmp_["width_ratio"]:.2f}&times;</b>'
                 f'<span class="dim">{esc(cmp_["verdict"])} &middot; '
                 f'downside {cmp_["downside_gap_pct"]:+.1f}%, upside {cmp_["upside_gap_pct"]:+.1f}%'
                 f'</span></div>')
    return rows


def _price_band_html(c):
    """Where the engine thinks the stock will be, and where the short strike sits in it.

    A credit spread IS a bet about a range, and the board showed a strike, a breakeven and a
    probability without ever drawing the distribution those came from. Stated plainly here,
    with the MEASURED coverage rather than the claimed one — an 80% window is wrong one time
    in five by design, and saying so is what makes it usable rather than reassuring.
    """
    b = _price_band(c)
    if not b:
        return ""
    mkt = c.get("implied_band") or None
    cov = b.get("measured_coverage")
    cov_txt = (f"held-out coverage {cov:.0%}" if cov else "coverage untested at this level")
    sp = b.get("strike") or {}
    if sp:
        cls = "neg" if sp.get("inside_band") else "pos"
        strike_row = (f'<div class="pbrow"><span class="k">Short strike</span>'
                      f'<b class="num {cls}">${sp["strike"]:,.2f}</b>'
                      f'<span class="dim">{esc(sp["note"])}</span></div>')
    else:
        strike_row = ""
    return (
        '<div class="copcard pband"><div class="hd">Projected price at expiry &middot; '
        f'{b["confidence"]:.0%} window</div>'
        f'<div class="pbrow"><span class="k">Range in {b["dte"]} days</span>'
        f'<b class="num">${b["low"]:,.2f} &ndash; ${b["high"]:,.2f}</b>'
        f'<span class="dim">{b["low_pct"]:+.1f}% / {b["high_pct"]:+.1f}% from ${b["spot"]:,.2f}</span></div>'
        f'{strike_row}'
        f'{_market_band_rows(b, mkt)}'
        f'<div class="pbnote">Zero drift, lognormal, sigma from the same forecast the edge '
        f'score uses ({b["vol_pp"]:.1f}% annualised). One-sigma move &asymp; '
        f'${b["one_sigma_usd"]:,.2f}. Direction is not predicted &mdash; the band is symmetric '
        f'by construction. {esc(cov_txt)}, so it is wrong about one time in '
        f'{max(1, round(1/(1-b["confidence"]))):.0f}.</div></div>')


def _copilot_impact(c):
    """What one contract does to the account — the question a metric table never answers."""
    ml = c.get("max_loss_usd")
    cr = c.get("credit_usd")
    roi = c.get("roi")
    conf = (c.get("true_pop_conf") or "-").title()
    ccol = ("var(--green)" if conf.upper() == "HIGH" else
            "var(--red)" if conf.upper() == "LOW" else "var(--amber)")
    ev = _ev(c)
    evv = ev["ev"] if (ev and ev.get("ev") is not None) else None

    def cell(k, v, s="", col=""):
        return (f'<div class="c"><span class="k">{k}</span>'
                f'<span class="v num"{f" style=color:{col}" if col else ""}>{v}</span>'
                f'<span class="s">{s}</span></div>')
    return ('<div class="copcard"><div class="hd">Position impact &middot; 1 contract</div>'
            '<div class="copgrid">'
            + cell("Capital at risk", f'${ml:.0f}' if ml is not None else "-", "Max loss",
                   "var(--red)")
            + cell("Credit received", f'${cr:.0f}' if cr is not None else "-", "Up front",
                   "var(--green)")
            # A blank cell reads as "we computed this and got nothing". Both of these depend on
            # true_pop, which the fast yfinance rescan does not produce at all — so on a
            # provisional board half this card was empty with no indication that the gap was
            # structural rather than a failure. Say which it is.
            + cell("Expected value", f'${evv:+.0f}' if evv is not None else "—",
                   "Per contract" if evv is not None else "needs the full engine",
                   ("var(--green)" if (evv or 0) > 0 else "var(--red)") if evv is not None
                   else "var(--ink3)")
            + cell("Confidence", conf if conf and conf != "-" else "—",
                   "True-POP model" if (conf and conf != "-") else "no true-POP on this board",
                   ccol if (conf and conf != "-") else "var(--ink3)")
            + '</div></div>')


def _copilot_action(c, log_form):
    """One sentence naming exactly what to place, with the log button beside it."""
    st = c.get("strat_type") or "bull_put"
    def g(x): return f"{x:g}" if x is not None else "?"
    if st == "iron_condor":
        legs = (f'{g(c.get("put_short"))}/{g(c.get("put_long"))}P '
                f'{g(c.get("call_short"))}/{g(c.get("call_long"))}C')
    else:
        legs = f'{g(c.get("short"))}/{g(c.get("long"))}{"C" if st == "bear_call" else "P"}'
    cr = c.get("credit_ps")
    txt = (f'Sell 1x {esc(c["ticker"])} {esc(_strat_label(c))} {legs} '
           f'exp {esc(c.get("exp") or "")}'
           + (f' &middot; {esc(c.get("dte"))} DTE' if c.get("dte") is not None else "")
           + (f' for ${cr:.2f} credit' if cr is not None else ""))
    return ('<div class="copact"><div><div class="lab">Recommended setup</div>'
            f'<div class="txt">{txt}</div></div>'
            f'<div class="btns">{log_form}</div></div>')


def env_badge(c):
    """Small board/brief chip for a heating environment (warm/hot only; cool stays quiet)."""
    e = c.get("env")
    if not e or e.get("band") == "cool":
        return ""
    band = e["band"]; heat = e.get("heat", 0)
    cls = "envhot" if band == "hot" else "envwarm"
    lbl = "HEATING" if band == "hot" else "WARM"
    return (f'<span class="bflag {cls}" title="Environment heat {heat}/100 ({e.get("threat_dir")}) — '
            f'click the row for the recommended adjustment">{lbl} {heat}</span>')


def env_panel(c):
    """Drawer panel: the 'game environment' read + best-practice recommendation."""
    e = c.get("env")
    if not e:
        return ""
    band = e["band"]; heat = e.get("heat", 0); rec = e.get("recommendation") or {}
    tone = {"hot": "gwarn", "warm": "gwarn", "cool": "gpass"}.get(band, "")
    dot = {"hot": "🔴", "warm": "🟠", "cool": "🟢"}.get(band, "")
    drivers = "".join(f'<div class="chk amber"><span class="k">&#9888;</span>{esc(d)}</div>'
                      for d in (e.get("drivers") or [])) or \
              '<div class="chk"><span class="k">&#10003;</span>Calm — no directional or vol stress against the short side.</div>'
    sm = rec.get("size_mult")
    size_line = ("stand aside / flip" if sm == 0 else f"{sm:g}× size" if sm is not None else "—")
    alt = rec.get("alt_structure")
    struct_line = ""
    if rec.get("action") in ("flip_or_stand_aside", "condor_or_stand_aside"):
        struct_line = f'<div class="kv"><span class="k">Recommended structure</span><b>{esc(rec.get("strike_note") or rec.get("structure"))}</b></div>'
    elif alt:
        struct_line = f'<div class="kv"><span class="k">Or play the protected side</span><b>{esc(alt)}</b></div>'
    strike = rec.get("strike_note")
    strike_line = (f'<div class="kv"><span class="k">Strike guidance</span><b>{esc(strike)}</b></div>'
                   if strike and rec.get("action") == "trim" else "")
    return (f'<h4 style="margin-top:14px">Game environment — {dot} {band.upper()} ({heat}/100, {esc(e.get("threat_dir"))})</h4>'
            f'{drivers}'
            f'<div class="kv"><span class="k">Best-practice action</span><b class="{tone}">{esc((rec.get("action") or "").replace("_"," "))} · {size_line}</b></div>'
            f'{struct_line}{strike_line}'
            f'<div class="dim" style="font-size:11.5px;margin-top:4px">{esc(rec.get("rationale") or "")}</div>')


def _exp_parts(c):
    """Clear expiration for the row/drawer: (short_label, dte, approx, dt).
       Uses the real expiration date when present; else derives it from today+DTE (flagged ~)."""
    dte = c.get("dte")
    dt = None
    exp = c.get("exp")
    if exp:
        try:
            dt = datetime.strptime(str(exp)[:10], "%Y-%m-%d")
        except Exception:
            dt = None
    approx = False
    if dt is None and isinstance(dte, (int, float)):
        try:
            dt = datetime.now() + timedelta(days=int(dte))
            approx = True
        except Exception:
            dt = None
    if dt is None:
        return (None, dte, False, None)
    lab = f"{dt.strftime('%b')} {dt.day}"
    if dt.year != datetime.now().year:
        lab += f" '{dt.strftime('%y')}"
    return (lab, dte, approx, dt)


def _row_contract(c):
    """The scannable contract line for a board row: strikes + a clear expiration date + DTE."""
    strikes = c.get("structure")
    if not strikes:
        sh, lg = c.get("short"), c.get("long")
        strikes = f"{sh:g}/{lg:g}" if (sh is not None and lg is not None) else ""
    lab, dte, approx, _ = _exp_parts(c)
    exp_txt = ""
    if lab:
        tilde = '<span class="dim" title="approx from DTE — run full engine for the listed date">~</span>' if approx else ''
        dtxt = f' <span class="dim">· {dte}d</span>' if dte is not None else ''
        exp_txt = f'<span class="expd">{esc(lab)}{tilde}</span>{dtxt}'
    elif dte is not None:
        exp_txt = f'<span class="dim">{dte}d</span>'
    sk = f'<b class="strk num">{esc(strikes)}</b>' if strikes else ''
    sep = ' <span class="dim">·</span> ' if (sk and exp_txt) else ''
    return f'<div class="subln">{sk}{sep}{exp_txt}</div>'


def _beta_flags(c):
    """Board chips for beta signal-quality flags (spec §5.1)."""
    out = ""
    if c.get("already_in_position"):
        out += '<span class="bflag inpos" title="An open position already exists in this underlying">IN POSITION</span>'
    if c.get("post_earnings_crush"):
        out += '<span class="bflag crush" title="Reported 1-3 days ago with IV still elevated (post-earnings crush)">POST-ER</span>'
    sk = c.get("skew_score")
    if sk:
        out += f'<span class="bflag skew" title="IV-skew component — richer downside insurance being sold">SKEW +{sk:.0f}</span>'
    return out


def _risk_presets():
    """One-click budget bands beside the typed max-loss box.

    Typing a number requires already knowing which number, which quietly makes the most
    important control on the board a power-user feature: for a small account the first
    question is not "which setup is best" but "which of these can I even take". The bands
    answer it in one click.

    "Any" is deliberately present and deliberately last. It states that VEGA finds the
    opportunity and the operator sets the risk — the same decision-support framing as
    "Recommended setup" rather than "Recommended action".
    """
    bands = [("100", "&lt; $100"), ("500", "&lt; $500"), ("1000", "&lt; $1K"),
             ("5000", "&lt; $5K"), ("", "Any")]
    btns = "".join(
        f'<button type="button" onclick="setRisk(this,\'{v}\')">{lbl}</button>'
        for v, lbl in bands)
    return f'<span class="rpre">{btns}</span>'


def _exposure_bar(board):
    """What is already at risk, stated BEFORE the reader picks up something new.

    The same facts are in the book-health footer, but the footer sits under everything the
    page is trying to sell — by the time it is read the decision is made. Risk that is
    already committed belongs above the list of ways to commit more. Hidden below two open
    positions, where the answer is obvious from the IN POSITION badges alone.
    """
    bk = board.get("book") or {}
    n = bk.get("open_positions") or 0
    if n < 2:
        return ""
    risk = bk.get("current_book_risk_usd") or 0
    tks = bk.get("open_tickers") or []
    hold = (" &middot; " + ", ".join(esc(t) for t in tks)) if tks else ""
    return (f'<a class="expbar" href="/?view=open">'
            f'<b>{n} open positions</b> &middot; max remaining loss '
            f'<b class="num">${risk:,.0f}</b>{hold}'
            f'<span class="go">Open tab &rarr;</span></a>')


def book_footer(board):
    """Book-health footer for the Today board (spec §5.4)."""
    bk = board.get("book") or {}
    if not bk:
        return ""
    n = bk.get("open_positions") or 0
    risk = bk.get("current_book_risk_usd") or 0
    tks = bk.get("open_tickers") or []
    hold = (' &middot; holding: ' + ", ".join(esc(t) for t in tks)) if tks else ''
    return (f'<div class="bookfoot"><b>Book health</b> &middot; {n} open position(s) '
            f'&middot; current book risk <b class="num">${risk:,.0f}</b>{hold}</div>')


def _shelter_note(c):
    """"under $240.07 support (3x, 4.4%)" — is a tested level standing between spot and the
    short strike?

    This is the structural edge, and the row had no way to show it: the thesis line was built
    from VRP, edge and a raw cushion percentage, none of which say whether the market has
    ever defended the price beneath your strike. A 9% cushion to nothing is a weaker trade
    than a 4% cushion under a level bought three times."""
    try:
        from analysis.levels import strike_cushion
    except Exception:
        return ""
    min_buf = float(getattr(config, "LEVEL_MIN_BUFFER_PCT", 0.005))

    def _cush(strike, levels, side):
        if not strike or not levels:
            return None
        try:
            return strike_cushion(float(strike), levels, side, min_buffer_pct=min_buf)
        except Exception:
            return None

    st = c.get("strat_type") or "bull_put"
    sup, res = c.get("support_levels"), c.get("resistance_levels")

    # A condor has TWO short strikes and reporting only one of them is a half-truth: a put
    # wing tucked under a 3-touch floor tells you nothing about an unprotected call wing,
    # which is the side that would actually be tested first in a rally.
    if st == "iron_condor":
        p = _cush(c.get("put_short"), sup, "put")
        k = _cush(c.get("call_short"), res, "call")
        if p and k:
            return "both wings sheltered"
        if p:
            return f'put wing under ${p["level"]:,.2f} ({p["touches"]}x) · call wing open'
        if k:
            return f'call wing over ${k["level"]:,.2f} ({k["touches"]}x) · put wing open'
        return ""

    if st == "bear_call":
        cush, word, kind = _cush(c.get("short"), res, "call"), "over", "resistance"
    else:
        cush, word, kind = _cush(c.get("short"), sup, "put"), "under", "support"
    if not cush:
        return ""
    return (f'{word} ${cush["level"]:,.2f} {kind} '
            f'({cush["touches"]}x, {cush["buffer_pct"] * 100:.1f}%)')


def _row_thesis(c):
    """One-line thesis under the strategy cell — the top 2-3 reasons the trade passed.

    Ordered by what changes a decision, not by what is easiest to compute: a timing warning
    first (it is the only reason on this line to NOT act), then the structural shelter, then
    premium richness. The older metric-only ordering led with VRP even when the entry-timing
    read said the pullback had barely started."""
    parts = []

    # Only surfaced when timing disagrees with acting now — a clean setup keeps the line
    # about the trade rather than spending a slot saying "nothing wrong".
    et = c.get("entry_timing") or {}
    if et and not et.get("timing_gate_pass", True):
        parts.append(f'<span style="color:var(--amber)">'
                     f'{esc(et.get("readiness", "").title())} timing</span>')

    shelter = _shelter_note(c)
    if shelter:
        parts.append(shelter)

    vrp = c.get("vrp"); edge = c.get("edge_pp"); cushion = c.get("cushion_pct")
    tp = c.get("true_pop"); roi = c.get("roi"); ivr = c.get("iv_rank")
    if len(parts) < 3 and vrp is not None and vrp > 0:
        parts.append(f"VRP +{vrp:.1f}")
    if len(parts) < 3 and edge is not None and edge > 2:
        parts.append(f"+{edge:.1f}pp edge")
    # Only fall back to a bare cushion when no real level was found to anchor it.
    if len(parts) < 3 and not shelter and cushion is not None and cushion >= 8:
        parts.append(f"{cushion:.0f}% cushion")
    if not parts and tp is not None:
        parts.append(f"{tp*100:.0f}% true POP")
    if not parts and roi is not None:
        parts.append(f"{roi*100:.0f}% ROC")
    if not parts and ivr is not None:
        parts.append(f"IV rank {ivr:.0f}")
    return (f'<div class="dim" style="font-size:10.5px;margin-top:2px">'
            f'{" · ".join(parts[:3])}</div>') if parts else ""



def board_table(trades, tier):
    if not trades:
        return '<div class="empty">No qualified opportunities in the latest scan. Not a strong day to sell premium.</div>'
    body=""
    for i,c in enumerate(trades):
        cushion, max_loss, sc, scls = _card_extras(c)
        delta=c.get("delta"); roi=c.get("roi"); edge=c.get("edge_pp"); tpop=c.get("true_pop")
        edge_c=(f'<span class="{_rg("edge",edge)} num">{edge:+.1f}pp</span>') if edge is not None else '<span class="dim num">-</span>'
        roi_c=f'<span class="{_rg("roc",roi*100)}">{roi*100:.0f}%</span>' if roi is not None else "-"
        cush_c=f'<span class="{_rg("cushion",cushion)}">{cushion:.1f}%</span>' if cushion is not None else "-"
        delta_c=f'<span class="{_rg("delta",delta)}">{delta:.2f}</span>' if delta is not None else "-"
        maxloss_c=f'<span class="{_rg("maxloss",max_loss)}">${max_loss:.0f}</span>' if max_loss is not None else "-"
        why=c.get("why") or ""
        # The per-row reconciliation badge moved into the detail drawer (see "Figures
        # reconciled" there) when the board slimmed to 8 columns. _reconcile() was still
        # being called here for every row and the result discarded.
        datts=(f' data-i="{i}" data-edge="{edge if edge is not None else -999}"'
               f' data-tpop="{(tpop*100) if tpop is not None else -999}"'
               f' data-roc="{(roi*100) if roi is not None else -999}"'
               f' data-maxloss="{max_loss if max_loss is not None else 999999}"'
               f' data-cushion="{cushion if cushion is not None else -999}"'
               f' data-delta="{abs(delta) if delta is not None else 999}"'
               f' data-gates="{c["gates_passed"]}" data-score="{sc}"'
               f' data-credit="{c.get("credit_usd") if c.get("credit_usd") is not None else -1}"'
               f' data-ticker="{esc(c["ticker"])}" data-strat="{esc((c.get("strat_type") or "bull_put"))}"')
        credit_c = (f'<span class="num">${c["credit_usd"]:.0f}</span>'
                    if c.get("credit_usd") is not None else "-")
        # Eye path: Rank -> Ticker -> Strategy -> Edge -> Win prob -> ROC -> Credit -> Max
        # loss. Everything that used to compete with the ticker (validation tags, beta flags,
        # environment badges, the contract string) moved under it as muted secondary text.
        body+=(f'<tr class="vmain" id="vm-{i}"{datts} onclick="vtoggle({i})">'
               f'<td class="l dim num" style="width:26px">{i+1}</td>'
               f'<td class="l tk"><b>{esc(c["ticker"])}</b></td>'
               f'<td class="l">{esc(_strat_label(c))} {_valtag(c)}{_beta_flags(c)}{env_badge(c)}'
               f'{_row_contract(c)}{_row_thesis(c)}</td>'
               f'<td><span class="sbadge {scls} num">{sc:.0f}</span></td>'
               f'<td>{pop_cell(c)}</td>'
               f'<td class="num">{roi_c}</td>'
               f'<td class="num">{credit_c}</td>'
               f'<td class="num">{maxloss_c}</td>'
               f'</tr>'
               f'<tr class="vdetail" id="vd-{i}"><td colspan="8">{detail_drawer(c,i,tier)}</td></tr>')
    order=[("score","Edge",""),("tpop","VEGA POP",""),("roc","ROC",""),
           ("credit","Credit",""),("maxloss","Max loss","")]
    def sth(key,label,cls):
        c2=("%s srt"%cls) if cls else "srt"
        return "<th class=\"%s\" onclick=\"sortBoard(this,'%s')\">%s<span class=\"arw\"></span></th>"%(c2,key,label)
    head=("<thead><tr class=\"col\"><th class=\"l\" style=\"width:26px\">#</th>"
          + sth("ticker","Ticker","l") + sth("strat","Strategy","l")
          + "".join(sth(*o) for o in order) + "</tr></thead>")
    toolbar=("<div class=\"btoolbar\"><span class=\"flab\">Max loss &le; $</span>"
             "<input id=\"fmaxloss\" type=\"number\" class=\"n\" placeholder=\"any\" oninput=\"presetOff();filterBoard()\">"
             + _risk_presets() +
             "<button type=\"button\" id=\"grpbtn\" class=\"ghostbtn\" data-on=\"0\" onclick=\"groupByTicker()\">Group by ticker</button>"
             "<span class=\"dim\" id=\"fcount\" style=\"margin-left:auto\"></span>"
             "<span class=\"rgkey\" title=\"Numbers are tinted by where they sit vs the desired trading range\">"
             "<i class=\"rg-opt\">●</i>optimal <i class=\"rg-neu\">●</i>neutral <i class=\"rg-bad\">●</i>outside range</span>"
             "<span class=\"dim\">Click a header to sort</span></div>")
    return f'<div class="board">{toolbar}<table>{head}<tbody>{body}</tbody></table></div>'



def _mc_status_cards(board, trades, tier):
    """The four things that decide whether to trade at all, before any single setup:
    today's call, the regime, how rich premium is, and the best edge on the board."""
    n = len(trades)
    great = sum(1 for t in trades if (t.get("edge_score") or t.get("priority") or 0) >= 80)
    elite = sum(1 for t in trades if (t.get("edge_score") or 0) >= 90)
    solid = sum(1 for t in trades if (t.get("edge_score") or t.get("priority") or 0) >= HIGH_EDGE)
    suppressed = (board.get("regime") or {}).get("trade_suppressed")
    good = n > 0 and (great or elite) and not suppressed

    # Headline and sub-line have to agree. Computed independently they contradicted each
    # other on 2026-08-05: a suppressed low-vol regime read "Stand Aside" over "High
    # conviction setup", because the sub only looked at whether an elite score existed.
    #
    # "Stand Aside" is reserved for the two cases that genuinely mean take no trade today —
    # nothing qualified, or the regime forbids new risk. It must never appear over a board
    # that is simultaneously recommending a setup, which is what made the word read as a
    # contradiction rather than an instruction. Everything between that and a clear green
    # light is graded, not binary: Selective (thin) → Cautious (workable) → Sell Premium.
    if suppressed:
        call, call_sub = "Stand Aside", "Regime suppresses new risk"
    elif n == 0:
        call, call_sub = "Stand Aside", "Nothing qualified today"
    elif good:
        call = "Sell Premium"
        call_sub = "High conviction setup" if elite else f"{n} qualified &middot; {great} great"
    elif n >= 5 and solid:
        call = "Cautious"
        call_sub = f"{n} qualified &middot; {solid} above {HIGH_EDGE} &middot; none scoring 80+"
    else:
        call, call_sub = "Selective", f"{n} qualified &middot; none scoring 80+"

    reg = board.get("regime") or {}
    flag = (reg.get("regime_flag") or "").replace("_", " ") or "NORMAL"
    vix = ((board.get("context") or {}).get("vix") or {})
    vixc = vix.get("current")
    vix_sub = (f'VIX {vixc:.1f} &middot; {esc(vix.get("trend") or "")}' if vixc is not None
               else esc(vix.get("label") or "-"))
    reg_col = "var(--amber)" if suppressed else "var(--green)"

    # Premium environment, scored from the thing the card is named after.
    #
    # Stars came first (decoration: min(5, great+1) printed four stars for a board with one
    # 80+ setup and for a board with four). Replacing them with a number off the SAME count
    # fixed the unit and left the meaning wrong: the score counted high-EDGE setups while the
    # sub-line quoted VRP, so the live board read "1.5 · VRP +7.5pp · Thin" — a healthy
    # variance premium sitting next to a score calling it thin, because the two halves were
    # measuring different things. One card, one question: how rich is premium right now.
    #
    # VRP is that question. Bands line up with the legend the row already carries (0-4
    # unfavorable / 5-6 neutral / 7-10 favorable), so the number decodes against the key
    # printed beneath it instead of against nothing.
    vrps = [t.get("vrp") for t in trades if t.get("vrp") is not None]
    best_vrp = max(vrps) if vrps else None
    if best_vrp is None:
        prem, prem_lab = 0.0, "No read"
    else:
        prem = max(0.0, min(10.0, 5.0 + best_vrp * 0.6))   # 0pp→5.0, +5pp→8.0, −5pp→2.0
        prem_lab = ("Excellent" if prem >= 8 else "Good" if prem >= 7 else
                    "Fair" if prem >= 5 else "Thin" if prem >= 3 else "Negative")
    prem_sub = (f'VRP {best_vrp:+.1f}pp &middot; {prem_lab}' if best_vrp is not None
                else "No VRP on the board")

    top = max((t.get("edge_score") or t.get("priority") or 0) for t in trades) if trades else 0
    edge_lab = ("Very high" if top >= 90 else "High" if top >= 80 else
                "Moderate" if top >= 70 else "Low")
    edge_col = _band_color(top / 10.0)

    return (
        '<div class="mccards">'
        f'<div class="mccard{" hi" if good else ""}"><div class="lab">Today\'s call</div>'
        f'<div class="big" style="color:{"var(--green)" if good else "var(--amber)"}">{esc(call)}</div>'
        f'<div class="sub">{call_sub}</div></div>'
        f'<div class="mccard"><div class="lab">Market regime</div>'
        f'<div class="big" style="font-size:15px;color:{reg_col}">{esc(flag)}</div>'
        f'<div class="sub">{vix_sub}</div></div>'
        f'<div class="mccard"><div class="lab">Premium environment</div>'
        f'<div class="big num" style="color:{_band_color(prem)}">{prem:.1f}</div>'
        f'<div class="sub">{prem_sub}</div></div>'
        f'<div class="mccard"><div class="lab">Edge score</div>'
        f'<div class="big num" style="color:{edge_col}">{top:.0f}</div>'
        f'<div class="sub">{edge_lab}</div></div>'
        '</div>'
        + _kpi_legend())


def _band_color(v):
    """Shared 0-10 banding. One scale across every KPI card, so the legend decodes all of them."""
    return ("var(--green)" if v >= 7 else "var(--amber)" if v >= 5 else "var(--red)")


def _kpi_legend():
    """Permanently visible key for the 0-10 cards. A number the reader cannot place is
    no better than the stars it replaced, and a tooltip is not visible at the moment of
    reading. Colour-matched to the bands it names."""
    return ('<div class="kpikey"><span class="k">Score guide</span>'
            '<span><i style="background:var(--red)"></i>0&ndash;4 Unfavorable</span>'
            '<span><i style="background:var(--amber)"></i>5&ndash;6 Neutral</span>'
            '<span><i style="background:var(--green)"></i>7&ndash;10 Favorable</span></div>')


def _mc_density_bar(board, trades):
    """The opportunity-density funnel (scan_summary from the engine).

    Five setups on a board is uninterpretable without the denominator: it reads the same
    whether they are the best five of two thousand or the only five a thin session could
    build. This renders the engine's own counts and nothing else — when the artifact carries
    no scan_summary (fast rescan, or a board written before the field existed) it renders
    nothing at all rather than inventing a total from the row count.
    """
    s = board.get("scan_summary") or {}
    scanned = s.get("total_scanned")
    if not scanned:
        return ""
    qual = s.get("total_qualified", len(trades))
    high = s.get("high_edge_count", 0)
    exc = s.get("exceptional_count", 0)
    tk = s.get("tickers_scanned")
    tk_note = f" across {tk} tickers" if tk else ""
    steps = [(f"{scanned:,}", f"structures scanned{tk_note}", "var(--ink3)"),
             (f"{qual:,}", "qualified", "var(--ink)"),
             (f"{high:,}", f"high edge (&ge;{HIGH_EDGE})", "var(--amber)"),
             (f"{exc:,}", f"exceptional (&ge;{EXCEPTIONAL_EDGE})", "var(--green)")]
    cells = '<span class="arw">&rarr;</span>'.join(
        f'<span class="step"><b class="num" style="color:{col}">{val}</b>'
        f'<span class="lb">{lab}</span></span>' for val, lab, col in steps)
    return (f'<div class="funnel" title="Bull-put structures the engine enumerated this scan. '
            f'Bear call and condor structures are not counted, so this is a floor.">{cells}</div>')


def _mc_playbook(trades):
    """Trade archetypes as the primary decision surface.

    The board answers "what qualified"; this answers "which one is for me". A trader picks a
    role — safest, most aggressive, best expected value — far faster than they compare eight
    columns across six rows. Each entry opens that trade's Copilot view.
    """
    if not trades:
        return ('<div class="mcpanel"><div class="hd">Today\'s playbook</div>'
                '<div class="dim" style="font-size:11.5px">Nothing qualified. '
                'Standing aside is the position.</div></div>')

    def _sc(t): return (t.get("edge_score") or t.get("priority") or 0)
    def _pop(t): return (t.get("true_pop") or 0) * 100
    def _ml(t): return t.get("max_loss_usd") or 9999
    def _roc(t): return (t.get("roi") or 0) * 100
    def _evv(t):
        e = _ev(t)
        return e["ev"] if (e and e.get("ev") is not None) else None

    idx = {id(t): i for i, t in enumerate(trades)}
    best = max(trades, key=_sc)
    picks = [("Best overall", best, f'{_sc(best):.0f}', "edge")]

    hw = max(trades, key=_pop)
    if hw is not best and _pop(hw) > 0:
        picks.append(("Highest win rate", hw, f'{_pop(hw):.0f}%', "pop"))
    evp = [(t, _evv(t)) for t in trades if _evv(t) is not None]
    if evp:
        be = max(evp, key=lambda x: x[1])
        if be[0] is not best:
            picks.append(("Highest expected value", be[0], f'${be[1]:+.0f}', "ev"))
    sf = min(trades, key=lambda t: (_ml(t), -_pop(t)))
    if sf is not best:
        picks.append(("Safest", sf, f'${_ml(sf):.0f} risk', "safe"))
    ag = max(trades, key=_roc)
    if ag is not best and _roc(ag) > 0:
        picks.append(("Aggressive", ag, f'{_roc(ag):.0f}% ROC', "roc"))

    rows = ""
    for role, t, metric, kind in picks[:5]:
        col = ("var(--green)" if kind in ("edge", "pop", "ev") else
               "var(--amber)" if kind == "roc" else "var(--ink2)")
        rows += (
            f'<a class="pbrow" onclick="vopen({idx[id(t)]})">'
            f'<div class="role">{role}</div>'
            f'<div class="line"><span class="tk">{esc(t["ticker"])}</span>'
            f'<span class="st">{esc(_strat_label(t))}</span>'
            f'<span class="mv num" style="color:{col}">{metric}</span></div></a>')

    # What the board is NOT offering is a decision too: silence about a missing side reads as
    # "none available" only if you say so.
    have = {(t.get("strat_type") or "bull_put") for t in trades}
    missing = [lab for key, lab in (("bear_call", "call spreads"),
                                    ("bull_put", "put spreads"),
                                    ("iron_condor", "condors")) if key not in have]
    avoid = ""
    if missing:
        avoid = ('<div class="pbavoid"><div class="role">Avoid</div>'
                 f'<div class="txt">No qualified {", ".join(missing)} today.</div></div>')
    return f'<div class="mcpanel"><div class="hd">Today\'s playbook</div>{rows}{avoid}</div>'


def _strat_label(c):
    st = (c.get("strat_type") or "bull_put")
    return {"bull_put": "Bull Put", "bear_call": "Bear Call",
            "iron_condor": "Iron Condor"}.get(st, st.replace("_", " ").title())


def _mc_system_status(board, trades, tier):
    """Four integrity readouts in one strip. They used to be scattered across the page as
    separate badges, which made the state of the system something you had to assemble."""
    gp = sum(t.get("gates_passed") or 0 for t in trades)
    gt = sum(t.get("gates_total") or 0 for t in trades)
    gates = f'{gp}/{gt}' if gt else "-"
    gates_ok = gt and gp == gt

    fresh_label, fcls, _stale = _freshness(board)
    # This cell was labelled "Data quality" and measured how OLD the board was. Two different
    # questions wearing one name: a board can be seconds old and built on a chain that was 20%
    # quotable. Freshness keeps its cell under its real name; quality gets its own, from the
    # per-scan readings fetcher now writes.
    fresh = "Good" if board.get("source") == "engine" and not _stale else (
        "Stale" if _stale else "Provisional")
    fcol = "var(--green)" if fresh == "Good" else "var(--amber)"
    chain_val, chain_col, chain_sub = _chain_quality_cell()

    rec_all = all(_reconcile(t)[0] for t in trades) if trades else True
    confs = [(t.get("true_pop_conf") or "").upper() for t in trades if t.get("true_pop_conf")]
    conf = ("HIGH" if confs and all(c == "HIGH" for c in confs) else
            "LOW" if confs and any(c == "LOW" for c in confs) else
            "MEDIUM" if confs else "-")
    ccol = ("var(--green)" if conf == "HIGH" else
            "var(--red)" if conf == "LOW" else "var(--amber)")

    def cell(lab, val, col, sub=""):
        return (f'<div class="cell"><div class="lab">{lab}</div>'
                f'<div class="val" style="color:{col}">{val}</div>'
                + (f'<div class="dim">{esc(sub)}</div>' if sub else '') + '</div>')
    return ('<div class="mcstat">'
            + cell("Hard gates", gates, "var(--green)" if gates_ok else "var(--amber)")
            + cell("Chain quality", chain_val, chain_col, chain_sub)
            + cell("Board freshness", fresh, fcol)
            + cell("Figures reconciled", "Yes" if rec_all else "Check",
                   "var(--green)" if rec_all else "var(--amber)")
            + cell("Confidence", conf.title(), ccol)
            + '</div>')


def _chain_quality_cell():
    """(value, colour, subtitle) for the chain-quality readout.

    Reports the WORST ticker in the last scan, not the mean. An average of 0.82 across 56
    tickers is compatible with the one name you were about to trade being 20% quotable, and
    that name is the only one the number needed to warn you about.
    """
    try:
        from data import data_quality_log as dq
        s = dq.latest_scan()
    except Exception:
        return "—", "var(--ink3)", "unavailable"
    if not s["count"] or s["worst_ratio"] is None:
        return "—", "var(--ink3)", "no readings yet"
    col = {"green": "var(--green)", "amber": "var(--amber)",
           "red": "var(--red)"}.get(dq.band(s["worst_ratio"]), "var(--ink3)")
    sub = f'worst: {s["worst_ticker"]}'
    if s["below_floor"]:
        sub += f' · {s["below_floor"]} below floor'
    return f'{s["worst_ratio"]*100:.0f}%', col, sub


def view_today(board, s, tier):
    """Mission Control: decide whether to trade, then which trade, then open it.

    Replaces the previous stack (verdict strip -> hero card -> wide table) which presented
    everything at one visual weight and left the user to work out the order themselves.
    """
    trades = board["trades"]
    # Publish board-wide context for the per-row Copilot drawers (see _COPILOT_PEERS).
    global _COPILOT_PEERS, _COPILOT_CTX
    _COPILOT_PEERS = list(enumerate(trades))
    _COPILOT_CTX = board.get("context") or {}
    fresh_label, _fc, _fs = _freshness(board)
    prov = ""
    if board.get("source") == "legacy":
        prov = ('<div class="provbar">⚡ <b>Fast scan (provisional)</b> — ranked by model POP + ROC. '
                'Edge, EV $ and True POP are blank here because they need the full engine. '
                'Run the engine (<code>python main.py</code>) for the graded board.</div>')
    # Quotes stale → the credits shown are MODELLED, not fillable. Said out loud, because the
    # whole point of pricing on the natural basis is that the board never quotes a price its
    # reader cannot get, and after the close nobody can get any of them. Measured 2026-08-10:
    # GOOG 335/330 was worth $100 fillable at 14:47 and $30 at 18:03 on the same underlying.
    if trades and any(t.get("fill_basis") == "modelled" for t in trades):
        prov += ('<div class="provbar">🌙 <b>Markets closed — prices are indicative.</b> '
                 'Option quotes are not being maintained, so the bid-ask widens and the '
                 'fillable credit cannot be measured. Credits below are modelled from the mid '
                 'and are for review, not execution. Re-check at the open before trading.</div>')
    # The regime read decides whether to trade at all, so it gets a band the eye stops on
    # rather than a line of body text below four cards competing for the same attention.
    reg = board.get("regime") or {}
    reg_note = reg.get("regime_note")
    _tone = ("bad" if (reg.get("trade_suppressed") or not trades)
             else "ok" if any((t.get("edge_score") or 0) >= EXCEPTIONAL_EDGE for t in trades)
             else "warn")
    reg_html = (f'<div class="regband {_tone}">{esc(reg_note)}</div>' if reg_note else "")
    return (
        '<div class="mcbar"><span class="ttl">Mission Control</span>'
        f'<span class="meta">Data as of {esc(fresh_label)}</span>'
        f'<span class="meta">{tier_badge(tier)}</span></div>'
        '<div class="mcwrap">'
        + _mc_status_cards(board, trades, tier)
        + reg_html
        + _mc_density_bar(board, trades)
        + _exposure_bar(board)
        + prov
        + '<div class="mcmain">'
        + _mc_playbook(trades)
        + '<div class="mcpanel" style="padding:11px 12px"><div class="hd">Top opportunities'
          '<span style="float:right;text-transform:none;letter-spacing:0;font-weight:400">'
          'Click a row to open the Copilot view</span></div>'
        + board_table(trades, tier)
        + '</div></div>'
        + _mc_tickets(trades, tier)
        + _mc_system_status(board, trades, tier)
        + '</div>'
        + book_footer(board))


def _mc_tickets(trades, tier):
    """The order tickets, folded in from what used to be a separate Brief tab.

    Brief rendered the SAME trades from the SAME artifact as this page and said so in its own
    intro — "the same engine scan as the board, written as actionable tickets". A tab that
    answers no question no other tab answers is a layout, not a section of the product, and it
    read as a mystery rather than a second opinion.

    What it genuinely added was the part below: the executable ticket and the position size for
    the current risk tier. That belongs directly under the table it sizes, not one click away.
    """
    if not trades:
        return ""
    cards = "".join(brief_card(c, tier) for c in trades[:5])
    return ('<div class="mcpanel" style="padding:11px 12px;margin-top:14px">'
            '<div class="hd">Order tickets'
            '<span style="float:right;text-transform:none;letter-spacing:0;font-weight:400">'
            'Strikes, expectancy and size for the tier above</span></div>'
            f'{cards}</div>')


def hero_card(trades, tier):
    if not trades:
        return ('<div class="empty">No top setup — the latest scan qualified nothing. '
                'Not a strong day to sell premium.</div>')
    c = trades[0]
    tp = c.get("true_pop")
    pop_txt = (f'{tp*100:.0f}% true' if tp is not None else
               (f'{c["model_pop"]*100:.0f}% est' if c.get("model_pop") is not None else "— "))
    roi = c.get("roi")
    score = c.get("edge_score")
    score_html = (f'<div class="score"><div class="n num">{score:.0f}</div><div class="c">Edge score /100</div>{tier_badge(tier)}</div>'
                  if score is not None else
                  f'<div class="score"><div class="n num" style="color:var(--amber)">{c["priority"]:.0f}</div><div class="c">Priority (no edge score)</div>{tier_badge("PROVISIONAL")}</div>')
    roi_txt = f'{roi*100:.0f}%' if roi is not None else "—"
    why = f'{esc(c["ticker"])} {c["short"]:g}/{c["long"]:g}, {esc(c.get("dte"))} DTE — {pop_txt} POP, {roi_txt} ROC'
    edge = c.get("edge_pp")
    edge_stat = (f'<div><div class="n {"pos" if edge >= 0 else "neg"} num">{edge:+.1f}</div><div class="c">POP edge (pp)</div></div>'
                 if edge is not None else '<div><div class="n dim">—</div><div class="c">POP edge</div></div>')
    reason = ("Why it ranks: highest composite edge that passed every gate."
              if score is not None else
              "Fast-scan candidate — full engine not yet run, so this is provisional.")
    return (
        '<div class="hero">'
        f'<div><div class="flag">Today\'s best opportunity</div>'
        f'<div class="tk">{esc(c["ticker"])}</div><div class="strat">{esc(c["strategy"])} · exp {esc(c["exp"])}</div>'
        f'<div class="why">{why}. {reason}</div></div>'
        f'<div class="hstats">'
        f'<div><div class="n num">{pop_txt.split()[0]}</div><div class="c">POP</div></div>'
        f'{edge_stat}'
        f'<div><div class="n num">{roi_txt}</div><div class="c">ROC</div></div>'
        f'</div>{score_html}</div>'
    )


def confidence_scorecard(board, trades, s):
    """Live-derived confidence, per build doc §7."""
    top = trades[0] if trades else {}
    if top.get("edge_score") is not None:
        signal = top["edge_score"]
    elif top:
        signal = (top["gates_passed"] / top["gates_total"]) * 100
    else:
        signal = 0
    n = s.get("n_closed") or 0
    hist = min(100, n / 30 * 100)
    vol = 88 if top.get("iv_method") == "HIST" else 45
    if board["source"] == "engine" and top:
        feeds = [top.get("news_sentiment"), top.get("fundamentals_score"), top.get("trend"), top.get("iv_rank")]
        data_c = sum(1 for f in feeds if f not in (None, "")) / len(feeds) * 100
    else:
        data_c = 40
    rows = [("Signal Strength", signal), ("Historical Confidence", hist),
            ("Volatility Confidence", vol), ("Data Completeness", data_c)]
    inner = ""
    for lab, v in rows:
        low = "low" if v < 60 else ""
        extra = f' <span class="dim">({n}/30)</span>' if lab.startswith("Historical") else ""
        inner += (f'<div class="row"><div class="lab">{lab}{extra}</div>'
                  f'<div class="bar"><i class="{low}" style="width:{v:.0f}%"></i></div>'
                  f'<div class="v num">{v:.0f}%</div></div>')
    return f'<div class="panel"><h3>Confidence scorecard</h3><div class="sc">{inner}</div></div>'


def score_composition(card):
    bd = card.get("component_breakdown") or {}
    if not bd:
        return ('<div class="panel"><h3>Score composition</h3>'
                '<div class="empty">No edge breakdown — this is a fast-scan (provisional) candidate. '
                'Run the full engine for the component scores.</div></div>')
    def _row(key, lab, mx):
        v = _f(bd.get(key), 0) or 0
        w = max(2, v / mx * 100) if mx else 2
        return v, (f'<div class="row"><div class="lab">{lab}</div>'
                   f'<div class="bar"><i style="width:{w:.0f}%"></i></div>'
                   f'<div class="v num">{v:.0f} / {mx}</div></div>')
    inner = ""
    base = 0
    for key, lab, mx in EDGE_COMPONENTS:
        v, h = _row(key, lab, mx); base += v; inner += h
    inner += f'<div class="subtot"><span>Base subtotal</span><span class="num">{base:.0f} / 100</span></div>'
    bonus = 0
    for key, lab, mx in EDGE_BONUSES:
        v, h = _row(key, lab, mx); bonus += v; inner += h
    total = base + bonus
    tail = f' <span class="dim">({base:.0f} base + {bonus:.0f} bonus)</span>' if bonus else ''
    inner += f'<div class="tot"><span>Total edge score</span><span class="num">{total:.0f}{tail}</span></div>'
    return f'<div class="panel"><h3>Score composition — no black box</h3><div class="comp">{inner}</div></div>'


# ─────────────────────────────────────────────────────────────────────────────
# Legacy paper-desk pieces (open/closed/portfolio/manual) — dark-themed
# ─────────────────────────────────────────────────────────────────────────────
def stat_cards(s):
    def c(l, v, sub=""):
        return f'<div class="card"><div class="lab">{l}</div><div class="val num">{v}</div><div class="s">{sub}</div></div>'
    wr = f'{s["win_rate"]*100:.0f}%' if s["win_rate"] is not None else "—"
    net = f'${s["net_total"]:+.0f}' if s["n_closed"] else "—"
    exp = f'${s["expectancy_per_contract"]:+.2f}' if s["expectancy_per_contract"] is not None else "—"
    pf = ("∞" if s["profit_factor"] == float("inf") else f'{s["profit_factor"]:.2f}') if s["profit_factor"] is not None else "—"
    cal = f'{s["calibration_gap"]*100:+.0f}pp' if s["calibration_gap"] is not None else "—"
    return ('<div class="cards">'
            + c("Closed", s["n_closed"], "of 30") + c("Win rate", wr, f'{s["wins"]}W/{s["losses"]}L/{s["scratch"]}S')
            + c("Net P/L", net, "after RH fees") + c("Expectancy", exp, "net/ct/trade")
            + c("Profit factor", pf, "&gt;1 winning") + c("Calibration", cal, "real−POP") + '</div>')


def portfolio_strip(open_):
    car = credit = utheta = unreal = 0.0
    have_unreal = False
    for r in open_:
        ct = int(r.get("contracts") or 1)
        if isinstance(r.get("max_loss_per_contract"), (int, float)):
            car += r["max_loss_per_contract"] * ct
        if isinstance(r.get("actual_fill_credit"), (int, float)):
            credit += r["actual_fill_credit"] * 100 * ct
        if isinstance(r.get("short_theta"), (int, float)):
            utheta += -(r["short_theta"]) * 100 * ct
        if isinstance(r.get("unrealized_net"), (int, float)):
            unreal += r["unrealized_net"] * ct
            have_unreal = True
    ucls = "pos" if unreal >= 0 else "neg"
    unreal_html = f'<b class="{ucls} num">${unreal:+.0f}</b>' if have_unreal else '<b class="dim">—</b>'
    return ('<div class="strip">'
            f'<div><span>Open</span><b class="num">{len(open_)}</b></div>'
            f'<div><span>Capital at risk</span><b class="num">${car:.0f}</b></div>'
            f'<div><span>Credit collected</span><b class="num">${credit:.0f}</b></div>'
            f'<div><span>Est net θ/day</span><b class="pos num">${utheta:+.0f}</b></div>'
            f'<div><span>Unrealized P/L</span>{unreal_html}</div></div>')


# ── Ravens alerts ───────────────────────────────────────────────────────────
# WHY THIS READS raven_alerts AND NOT thesis_status.
#
# The build plan specified cards keyed on thesis_status, with a red WOLF card. That card
# could never render. WOLF and VIOLATED-with-agreement resolve to WOLF_CLOSE and CLOSE in
# odin.synthesize, and auto_paper_cycle closes the position on both inside the same cycle —
# so by the time the cockpit lists a position as OPEN, those states are gone. The two
# recommendations that PERSIST on an open position are exactly the two the plan said did not
# exist: HOLD_TENSION and MUNINN_BLIND, appended to the trade by _record_raven_alert.
#
# Both mean the same thing operationally: the system has deliberately declined to act and is
# handing the decision to Josh. That is the only class of event that belongs at the top of
# this page, and until now it was written to the ledger and to a log file and shown nowhere.

_RAVEN_CARDS = {
    "HOLD_TENSION": ("tension", "Ravens disagree",
                     "Thought says the structure is broken; memory says situations like this "
                     "recovered. Hold deliberately or close deliberately — but decide."),
    "MUNINN_BLIND": ("blind", "Memory is blind",
                     "The thesis is under strain and there is no comparable history to weigh "
                     "against it. No base rate exists yet; this one is yours."),
}


def _raven_age(ts):
    try:
        d = datetime.now() - datetime.fromisoformat(str(ts))
    except Exception:
        return ""
    h = d.total_seconds() / 3600
    if h < 1:
        return f"{int(d.total_seconds() // 60)}m ago"
    return f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f}d ago"


def raven_alerts(open_):
    """The unresolved raven divergences across every open position, newest first.

    Only the LATEST alert per position is shown. A position under sustained strain re-alerts
    on every cycle, and eleven copies of one disagreement would bury the other ten positions —
    the repeat count carries that information without the volume.
    """
    cards = []
    for r in open_ or []:
        alerts = [a for a in (r.get("raven_alerts") or [])
                  if a.get("recommendation") in _RAVEN_CARDS]
        if not alerts:
            continue
        a = alerts[-1]
        cls, title, gloss = _RAVEN_CARDS[a["recommendation"]]

        bits = []
        if a.get("huginn_status"):
            bits.append(f'Huginn: {esc(a["huginn_status"])}')
        prob = a.get("muninn_probability")
        bits.append(f"Muninn: {prob*100:.0f}% recovered in comparable situations"
                    if isinstance(prob, (int, float))
                    else "Muninn: no comparable history")
        if a.get("confidence"):
            bits.append(f'confidence {esc(a["confidence"])}')
        if len(alerts) > 1:
            bits.append(f"raised {len(alerts)}x")

        cards.append(
            f'<div class="rav {cls}"><div class="rhd">'
            f'<span class="rtag">{esc(title)}</span>'
            f'<span class="rtk">{esc(r.get("ticker"))} '
            f'{esc(r.get("short_strike"))}/{esc(r.get("long_strike"))}</span>'
            f'<span class="rwhen">{esc(_raven_age(a.get("at")))}</span></div>'
            f'<div class="rtxt">{esc(a.get("plain_english") or gloss)}</div>'
            f'<div class="rmeta">{" · ".join(bits)}</div></div>'
        )
    if not cards:
        return ""
    n = len(cards)
    return (f'<div class="ravens"><h2 style="margin-bottom:6px">Needs your decision '
            f'<span class="dim">({n} position{"" if n == 1 else "s"})</span></h2>'
            + "".join(cards) + '</div>')


def open_section(open_):
    if not open_:
        return ('<h2>Open positions</h2>' + portfolio_strip([]) +
                '<div class="empty">None yet — log a candidate from the board.</div>')
    rows = ""
    for r in open_:
        un = r.get("unrealized_net")
        ct = int(r.get("contracts") or 1)
        if isinstance(un, (int, float)):
            tot = un * ct
            un_html = f'<span class="{"pos" if tot >= 0 else "neg"} num">${tot:+.0f}</span><div class="dim num">@${esc(r.get("current_mark"))}</div>'
        else:
            un_html = '<span class="dim">—</span>'
        diagram = payoff_svg(r.get("short_strike"), r.get("long_strike"),
                             r.get("actual_fill_credit"), r.get("spread_width"), None,
                             mode="position",
                             unrealized=(un * ct) if isinstance(un, (int, float)) else None,
                             current_mark=r.get("current_mark"))
        rows += (
            f'<tr><td class="l"><b>{esc(r.get("ticker"))}</b></td><td class="l">{diagram}</td>'
            f'<td class="l num">{esc(r.get("short_strike"))}/{esc(r.get("long_strike"))}</td>'
            f'<td class="l num">{esc(r.get("expiration"))}<div class="dim num">{esc(r.get("dte"))}d</div></td>'
            f'<td class="num">${esc(r.get("actual_fill_credit"))}</td><td class="num">{ct}</td>'
            f'<td class="num">${esc(r.get("max_loss_per_contract"))}</td><td>{un_html}</td>'
            f'<td class="l"><form class="inline" method="post" action="/close">'
            f'<input type="hidden" name="id" value="{esc(r.get("id"))}">'
            f'<input class="n" type="number" step="0.01" name="exit_debit" placeholder="exit" required>'
            f'<select name="outcome"><option>win</option><option>loss</option><option>scratch</option></select>'
            f'<input type="text" name="reason" placeholder="reason" style="width:110px">'
            f'<button class="close" type="submit" data-busy="Closing…">Close</button></form></td></tr>'
        )
    # Alerts sit ABOVE the strip and the table, without scrolling. A decision the system has
    # refused to make on its own is not a footnote to the position list.
    return (raven_alerts(open_) +
            f'<h2>Open positions</h2>{portfolio_strip(open_)}'
            f'<div class="board"><table><thead><tr class="col"><th class="l">Ticker</th><th class="l">Payoff</th>'
            f'<th class="l">Short/Long</th><th class="l">Exp</th><th>Credit/sh</th><th>Ct</th><th>Max loss</th>'
            f'<th>Unreal P/L</th><th class="l">Close (enter exit debit)</th></tr></thead><tbody>{rows}</tbody></table></div>')


def closed_section(closed):
    if not closed:
        return '<h2>Closed trades</h2><div class="empty">None yet. Gate 1 needs ~30 closed outcomes.</div>'
    rows = ""
    for r in sorted(closed, key=lambda x: x.get("closed_at") or "", reverse=True):
        res = (r.get("outcome") or "").upper()
        cls = "pos" if r.get("outcome") == "win" else ("neg" if r.get("outcome") == "loss" else "dim")
        net = r.get("realized_net_pl_per_contract")
        rows += (f'<tr><td class="l"><b>{esc(r.get("ticker"))}</b></td>'
                 f'<td class="l num">{esc(r.get("short_strike"))}/{esc(r.get("long_strike"))}</td>'
                 f'<td class="l num">{esc(r.get("expiration"))}</td><td class="num">${esc(r.get("actual_fill_credit"))}</td>'
                 f'<td class="num">${esc(r.get("exit_price"))}</td><td><span class="{cls}">{esc(res)}</span></td>'
                 f'<td class="num">${esc(net)}</td><td class="l dim">{esc(r.get("exit_reason") or "")}</td></tr>')
    return (f'<h2>Closed trades</h2><div class="board"><table><thead><tr class="col"><th class="l">Ticker</th>'
            f'<th class="l">Short/Long</th><th class="l">Exp</th><th>Credit</th><th>Exit</th><th>Result</th>'
            f'<th>Net/ct</th><th class="l">Reason</th></tr></thead><tbody>{rows}</tbody></table></div>')


def manual_form():
    return ('<h2>Manual entry</h2>'
            '<form class="inline" method="post" action="/open_manual" style="flex-wrap:wrap;gap:6px">'
            '<input type="text" name="ticker" placeholder="TICKER" style="width:80px" required>'
            '<input class="n" type="number" step="0.5" name="short" placeholder="short" required>'
            '<input class="n" type="number" step="0.5" name="long" placeholder="long" required>'
            '<input type="text" name="exp" placeholder="YYYY-MM-DD" style="width:110px" required>'
            '<input class="n" type="number" step="0.01" name="credit" placeholder="credit" required>'
            '<input class="n" type="number" name="dte" placeholder="dte">'
            '<input class="n" type="number" step="0.01" name="delta" placeholder="delta">'
            '<input class="n" type="number" name="contracts" value="1" min="1">'
            '<button class="primary" type="submit" data-busy="Logging…">Log manual</button></form>')


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────
def _source_banner(board):
    label, cls, stale = _freshness(board)
    if board["source"] == "engine":
        return f'<div class="{cls}">Source: <b>engine artifact</b> &middot; {label} &middot; per-row figures reconciled against the pull.</div>'
    return f'<div class="warn">&#9888; {esc(board.get("note"))} {label}.</div>'


def view_dashboard(board, s, tier):
    trades = board["trades"]
    n_total = len(trades)
    great = sum(1 for t in trades if (t.get("edge_score") or 0) >= 80)
    elite = sum(1 for t in trades if (t.get("edge_score") or 0) >= 90)
    ctx = board.get("context") or {}
    regime = board.get("regime") or {}
    vix = ctx.get("vix")
    regime_note = regime.get("note") or regime.get("regime_note") or ("Fast-scan mode" if board["source"] == "legacy" else "—")
    premium_stars = min(5, max(0, great + 1))
    good_day = n_total > 0 and (elite or great)
    takeaway = ("Quality opportunities available — reasonable environment for selling premium."
                if good_day else
                "Weak board — few or no qualified setups. Not a strong day to sell premium.")
    summary = (
        '<div class="panel"><h3>Market summary</h3>'
        f'<div class="kv"><span class="k">Qualified setups</span><b class="num">{n_total}</b></div>'
        f'<div class="kv"><span class="k">Great (≥80)</span><b class="num">{great}</b></div>'
        f'<div class="kv"><span class="k">Elite (≥90)</span><b class="num">{elite}</b></div>'
        f'<div class="kv"><span class="k">VIX</span><b class="num">{esc(vix) if vix is not None else "—"}</b></div>'
        f'<div class="kv"><span class="k">Regime</span><b>{esc(regime_note)}</b></div></div>'
    )
    premium = (
        '<div class="panel"><h3>Premium environment</h3>'
        f'<div class="stars" style="color:var(--amber)">{"★" * premium_stars}{"☆" * (5 - premium_stars)}</div>'
        f'<div class="sub">Edge model gate: {tier_badge(tier)}</div>'
        f'<p class="q" style="margin-top:8px">{takeaway}</p></div>'
    )
    return (
        '<h1>Market dashboard</h1><p class="q">Is today worth trading?</p>'
        + _source_banner(board)
        + '<div class="grid g3">' + summary + premium
        + confidence_scorecard(board, trades, s) + '</div>'
        + '<h2>Today\'s best opportunity</h2>' + hero_card(trades, tier)
    )


def view_board(board, s, tier):
    trades = board["trades"]
    subtitle = ("Score = engine composite edge (0–100): VRP 30 · true-POP edge 25 · technical 20 · "
                "fundamentals 10 · news 10 · earnings 5. Sorted by Priority."
                if board["source"] == "engine" else
                "Priority = 65% POP + 35% ROC − gate penalties (fast scan; no engine edge score).")
    return (
        '<h1>Market leaderboard</h1><p class="q">What are today\'s best opportunities?</p>'
        + _source_banner(board)
        + f'<div class="sub">{subtitle} Gate 1: {TIER_LABEL[tier]} — {TIER_NOTE[tier]}.</div>'
        + board_table(trades, tier)
    )


def view_research(board, s, tier):
    trades = board["trades"]
    if not trades:
        return '<h1>Research</h1><p class="q">Show me every angle.</p><div class="empty">No trade to inspect.</div>'
    c = trades[0]
    tp = c.get("true_pop")
    diag = payoff_svg(c["short"], c["long"], c["credit_ps"], c["width"], c["price"], w=360, h=120)

    def kv(k, v):
        return f'<div class="kv"><span class="k">{k}</span><b class="num">{v}</b></div>'

    kvs = [
        kv("Structure", f'{c["short"]:g}/{c["long"]:g} · w{(c["width"] or 0):g}'),
        kv("Expiration / DTE", f'{esc(c["exp"])} · {esc(c.get("dte"))}d'),
        kv("Credit", f'${(c["credit_usd"] or 0):.0f} (${c["credit_ps"]:.2f}/sh)'),
        kv("Breakeven", f'${c["breakeven"]:.2f}'),
    ]
    if tp is not None:
        kvs.append(kv("True POP", f'{tp*100:.0f}%'))
    if c.get("implied_pop") is not None:
        kvs.append(kv("Implied POP", f'{c["implied_pop"]*100:.0f}%'))
    if c.get("edge_pp") is not None:
        kvs.append(kv("POP edge", f'{c["edge_pp"]:+.1f}pp'))
    kvs.append(kv("True-POP drift mode", esc(c.get("drift_mode") or "—")))
    kvs.append(kv("True-POP confidence", esc(c.get("true_pop_conf") or "—")))
    kvs.append(f'<div class="kv"><span class="k">VRP (IV−RV)</span>'
               f'<b class="num">{esc(c.get("vrp"))}</b> {tier_badge(tier)}</div>')
    facts = ('<div class="panel"><h3>Trade profile — ' + esc(c["ticker"]) + '</h3>'
             + "".join(kvs) + '</div>')

    payoff_panel = (f'<div class="panel"><h3>Payoff at expiration</h3>{diag}'
                    f'<div class="sub">Green = profit · red = loss · amber dot = breakeven · blue = current price.</div></div>')
    diagnostics = (
        '<div class="panel"><h3>Scanner diagnostics</h3>'
        f'<p class="q">Gate 1 status: {tier_badge(tier)} — {TIER_NOTE[tier]}. '
        f'{"Backtest signed off." if GATE1_BACKTEST_PASSED else "ORATS backtest not yet recorded; edge/VRP shown as provisional."}</p>'
        f'<div class="kv"><span class="k">Live-confirmation</span><b class="num">{s.get("n_closed") or 0}/{GATE1_MIN_LIVE}</b></div>'
        f'<div class="kv"><span class="k">Live calibration gap</span><b class="num">{(_live_calib_pp(s) or 0):+.1f}pp</b></div>'
        '</div>'
    )
    return (
        '<h1>Research — deep dive</h1><p class="q">Show me every angle.</p>'
        + '<div class="grid g2">' + facts + payoff_panel + '</div>'
        + '<div class="grid g2">' + score_composition(c) + diagnostics + '</div>'
    )


def load_lottery():
    if LOTTERY_LATEST.exists() and LOTTERY_LATEST.stat().st_size > 0:
        try:
            return json.loads(LOTTERY_LATEST.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _lottery_card(x):
    tk=esc(x.get("ticker")); price=_f(x.get("current_price"))
    strike=_f(x.get("strike")); prem=_f(x.get("premium_usd")); prem_ps=_f(x.get("premium_per_share"))
    mult=_f(x.get("target_multiple")); tgt_px=_f(x.get("target_price"))
    bemv=_f(x.get("breakeven_move_pct")); delta=_f(x.get("delta")); iv=_f(x.get("iv"))
    be=_f(x.get("breakeven")); dte=x.get("dte"); conv=(x.get("conviction") or "").upper()
    setup=esc(x.get("setup") or ""); cat=esc(x.get("catalyst") or ""); sent=esc(x.get("news_sentiment") or "")
    rsi=_f(x.get("rsi")); sup=_f(x.get("nearest_support")); trend=esc(x.get("trend") or "")
    prob=f"{delta*100:.0f}%" if delta is not None else "-"
    mult_txt=(f"{mult:.0f}x" if mult else "-")
    # A multiple is a ratio, not an amount: "3x" reads the same on a $62 ticket and a $389
    # one, and the question the buyer is actually asking is whether the payout is worth the
    # effort. Lead with the dollars and keep the multiple as the explanation.
    tgt_usd=(prem*mult) if (prem is not None and mult) else None
    tgt_txt=(f"${tgt_usd:,.0f}" if tgt_usd is not None else mult_txt)
    tgt_sub=(f"{mult:.0f}x if ${tgt_px:,.0f}" if (mult and tgt_px) else "-")
    # IV rank replaces the conviction chip when it is known. Conviction was amber "HIGH" on
    # nearly every card, and a badge that never varies is decoration; IV rank varies and it
    # is the fact that decides whether the buyer is overpaying for the move they need.
    # The buyer's edge: realized vol minus implied. Positive means the stock has been moving
    # more than its options are priced for, which is the only thing on this card that says the
    # option is CHEAP rather than that the chart looks good.
    ve = _f(x.get("vol_edge_pp"))
    if ve is None:
        vetxt, vecls, vesub = "—", "", "not measurable"
    else:
        vetxt = f"{ve:+.1f}pp"
        vecls = "pos" if ve > 0 else "neg"
        vesub = ("realised above implied — moving more than priced" if ve > 0
                 else "realised below implied — paying up for the move")
    ivr=_f(x.get("iv_rank"))
    if ivr is not None:
        chip_txt=f"IV rank {ivr:.0f}"
        chip_cls=("cheap" if ivr <= 30 else "rich" if ivr >= 70 else "")
    else:
        chip_txt=esc(conv or "SPEC"); chip_cls=("hi" if conv=="HIGH" else "")
    # Prefer the generator's per-ticker signals (spec §5.5). Fall back to a small
    # tech summary only when signals are absent (older lottery_latest.json).
    signals=[s for s in (x.get("signals") or []) if s]
    tech=[]
    if not signals:
        if trend: tech.append(f"trend {trend}")
        if rsi is not None: tech.append(f"RSI {rsi:.0f}")
        if sup is not None: tech.append(f"support ${sup:.2f}")
    # Suppress the generic "no significant news" filler line.
    if cat and "no significant" in cat.lower():
        cat=""
    dir_tag=_lottery_direction_tag(trend)
    return (
        '<div class="lotto">'
        f'<div class="lh"><div><span class="tchip" title="Long call">CALL</span> <b class="tk">{tk}</b>'
        f'<span class="dim"> ${price:.2f}</span></div>'
        f'<div class="conv {chip_cls}" title="Where implied vol sits against its own past year. '
        f'A call bought at a high IV rank needs a bigger move to overcome the premium paid.">'
        f'{chip_txt}</div></div>'
        f'<div class="lgrid">'
        f'<div><div class="cap">Buy</div><div class="v">{strike:g} C</div><div class="dim">{esc(x.get("expiration"))} · {esc(dte)}d</div></div>'
        f'<div><div class="cap">Cost / max loss</div><div class="v neg">${(prem or 0):.0f}</div><div class="dim">${(prem_ps or 0):.2f}/sh</div></div>'
        f'<div><div class="cap">Target</div><div class="v pos">{tgt_txt}</div><div class="dim">{tgt_sub}</div></div>'
        f'<div><div class="cap">Breakeven move</div><div class="v">{(bemv or 0):+.1f}%</div><div class="dim">to ${(be or 0):.2f}</div></div>'
        f'<div><div class="cap">Vol edge</div><div class="v {vecls}">{vetxt}</div>'
        f'<div class="dim">{vesub}</div></div>'
        f'<div><div class="cap">Chance past strike</div><div class="v">{prob}</div>'
        f'<div class="dim" title="Delta — the market\'s own estimate">&Delta; {(delta or 0):.2f} &middot; IV {(iv*100 if iv and iv<3 else iv) or 0:.0f}%</div></div>'
        f'</div>'
        f'<div class="lwhy">{dir_tag}<b>Why {tk}:</b> '
        f'{(" · ".join(esc(sg) for sg in signals)) if signals else (setup or "-")}'
        f'{(" · <b>Catalyst:</b> "+cat) if cat else ""}'
        f'{(" · News "+sent) if sent else ""}{(" · "+", ".join(tech)) if tech else ""}</div>'
        f'{_lottery_actions(x)}'
        '</div>'
    )


def _lottery_direction_tag(trend):
    """Whether this call runs with the trend or against it.

    A long call is bullish by construction, so a "BULLISH" tag on every card would be the
    same decoration the fixed HIGH badge was. What varies — and what the buyer should see
    before anything else — is whether the chart agrees. A call on a name making lower lows
    is a counter-trend bet, and that is a different trade from a momentum continuation
    whatever the payoff diagram looks like.
    """
    t = (trend or "").strip().upper().replace(" ", "_")
    if t.startswith("STRONG_UP") or t == "UP":
        return '<span class="dtag bull">Momentum &middot; with the trend</span>'
    if t.startswith("STRONG_DOWN") or t == "DOWN":
        return '<span class="dtag bear">Counter-trend &middot; buying into weakness</span>'
    if t:
        return '<span class="dtag flat">No clear trend</span>'
    return ""


def _lottery_actions(x):
    """Watch / Reject on the asymmetry cards too.

    A watched call that later runs +300% is exactly the counterfactual that trains judgement,
    and it is the cheapest one available — nothing was risked to learn it. The paper-trade
    path for single long calls is not wired here, so this card offers the two decisions it
    can actually honour rather than a button that does nothing.
    """
    fields = {"ticker": x.get("ticker"), "strategy": "long_call",
              "short": x.get("strike"), "exp": x.get("expiration"), "dte": x.get("dte"),
              "credit": x.get("premium_per_share"), "credit_usd": x.get("premium_usd"),
              "max_loss_usd": x.get("max_loss_usd") or x.get("premium_usd"),
              "delta": x.get("delta"), "iv_rank": x.get("iv_rank"),
              "spot": x.get("current_price")}
    hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v if v is not None else "")}">'
                     for k, v in fields.items())
    return ('<div class="lacts">'
            f'<form class="inline" method="post" action="/watch">{hidden}'
            f'<button class="ghostbtn" type="submit" data-busy="Saving...">Watch</button></form>'
            f'<form class="inline" method="post" action="/reject">{hidden}'
            f'<button class="ghostbtn danger" type="submit" data-busy="Saving...">Reject</button>'
            f'</form></div>')


def view_lottery():
    d=load_lottery()
    banner=('<div class="specbanner">&#9888; SPECULATIVE — buying single calls is a low-probability, '
            'high-variance bet. Max loss is 100% of the premium paid. This is NOT the defined-risk premium-selling '
            'edge; it is a capped-cost home-run swing for specific momentum/reversal conditions. Size tiny.</div>')
    if not d or not (d.get("lottery_calls") or []):
        return ('<h1>Momentum plays</h1><p class="q">High-variance single calls on names moving more than their options are priced for. Capped cost.</p>'
                + banner +
                '<div class="empty">No lottery candidates right now. The lottery scanner (lottery_scanner.py) '
                'runs separately and only surfaces setups in specific conditions (oversold-at-support bounce or '
                'confirmed momentum breakout with catalyst). Run it on the tower to populate this view.</div>')
    asof=esc(d.get("timestamp") or "")
    cards="".join(_lottery_card(x) for x in d["lottery_calls"])
    return ('<h1>Momentum plays</h1><p class="q">High-variance single calls, ranked by vol edge &middot; as-of '
            + asof + '</p>' + banner + f'<div class="lottowrap">{cards}</div>')


def view_open(open_):
    return '<h1>Open positions</h1><p class="q">What am I currently holding?</p>' + open_section(open_)


def view_history(s, closed):
    return ('<h1>History</h1><p class="q">Can I trust it? — closed outcomes build Gate 1 confidence.</p>'
            + stat_cards(s) + closed_section(closed) + manual_form())


def _brief_reco(c, ev):
    """Derive a TAKE/WATCH/PASS-style recommendation from engine truth.
       PASS on negative expectancy (surfaces the EV-sign issue directly);
       CONSIDER on a high score with positive EV; otherwise WATCH."""
    score = c.get("edge_score") or 0
    if ev and ev.get("positive") is False:
        return ("PASS", "red", "Negative expectancy — the math says skip.")
    if score >= 80 and ev and ev.get("positive"):
        return ("CONSIDER", "green", "High composite edge with positive expectancy.")
    return ("WATCH", "amber", "Qualifies, but edge is thinner — size small or wait for a better fill.")


def _brief_ticket(c):
    """Actionable order ticket from real strikes (fixes the $0/$0 condor bug at the source)."""
    st = c.get("strat_type", "bull_put")
    cr = c.get("credit_ps") or 0.0
    exp = esc(c.get("exp") or "")
    tp = getattr(config, "TARGET_PROFIT_PCT", 0.5)
    sl = getattr(config, "STOP_LOSS_MULTIPLIER", 2.0)
    target = cr * (1 - tp); stop = cr * sl
    def g(x): return f"{x:g}" if x is not None else "?"
    if st == "iron_condor":
        ps, pl = c.get("put_short"), c.get("put_long")
        cs, cl = c.get("call_short"), c.get("call_long")
        entry = (f'Sell <b>{esc(c["ticker"])}</b> iron condor for <b>${cr:.2f}</b> credit, exp {exp} — one spread order: '
                 f'put spread <b>{g(ps)}/{g(pl)}P</b> (sell {g(ps)}P, buy {g(pl)}P) + '
                 f'call spread <b>{g(cs)}/{g(cl)}C</b> (sell {g(cs)}C, buy {g(cl)}C).')
    elif st == "bear_call":
        s, l = c.get("short"), c.get("long")
        entry = (f'Sell <b>{esc(c["ticker"])}</b> <b>{g(s)}/{g(l)}</b> call spread for <b>${cr:.2f}</b> credit, exp {exp} '
                 f'— sell {g(s)}C, buy {g(l)}C simultaneously (spread order).')
    else:
        s, l = c.get("short"), c.get("long")
        entry = (f'Sell <b>{esc(c["ticker"])}</b> <b>{g(s)}/{g(l)}</b> put spread for <b>${cr:.2f}</b> credit, exp {exp} '
                 f'— sell {g(s)}P, buy {g(l)}P simultaneously (spread order).')
    exit_ = (f'Buy back at <b>${target:.2f}</b> ({tp*100:.0f}% profit target). '
             f'Stop if the spread reaches <b>${stop:.2f}</b> ({sl:g}× credit). Set the GTC close order right after fill.')
    bes = c.get("breakevens") or []
    sup = c.get("nearest_support")
    if st == "iron_condor" and len(bes) >= 2:
        inval = f'Exit if {esc(c["ticker"])} closes outside the breakevens <b>${bes[0]:.2f} / ${bes[1]:.2f}</b> on volume.'
    elif sup is not None:
        inval = f'Exit if {esc(c["ticker"])} breaks support at <b>${sup:.2f}</b> on high volume, regardless of spread price.'
    else:
        inval = 'Exit on a decisive break of the short strike on high volume.'
    # Timing advisory rides on the ticket only when it disagrees with acting now, so a clean
    # setup stays a clean three-line ticket.
    et = c.get("entry_timing") or {}
    timing_html = ""
    if et and not et.get("timing_gate_pass", True):
        col = _TIMING_COLORS.get(et.get("readiness", ""), "#F0B429")
        headline = et.get("headline") or (et.get("phase") or "").replace("_", " ").title()
        timing_html = (
            f'<div class="tk" style="border-left:3px solid {col}">'
            f'<span class="tl" style="color:{col}">TIMING</span>'
            f'<b>{esc(et.get("readiness",""))} &mdash; {esc(headline)}.</b> '
            f'{esc(et.get("reason",""))} '
            f'<span class="dim">Advisory: the trade still qualifies on every hard gate.</span></div>')
    return (f'<div class="tkt"><div class="tk entry"><span class="tl">ENTRY</span>{entry}</div>'
            f'<div class="tk exit"><span class="tl">EXIT / TARGET</span>{exit_}</div>'
            f'<div class="tk inval"><span class="tl">INVALIDATION</span>{inval}</div>'
            f'{timing_html}</div>')


def _brief_sizing(c):
    ml = c.get("max_loss_usd")
    if not ml or ml <= 0:
        return ""
    cr = c.get("credit_usd") or 0.0
    cells = ""
    for tier in getattr(config, "RISK_TIERS", [{"label": "< $500", "max_risk": 500}]):
        n = int(tier["max_risk"] // ml)
        if n < 1:
            cells += (f'<div class="szc off"><div class="szl">{esc(tier["label"])}</div>'
                      f'<div class="szn">—</div><div class="szs">n/a</div></div>')
        else:
            cells += (f'<div class="szc on"><div class="szl">{esc(tier["label"])}</div>'
                      f'<div class="szn">{n}×</div><div class="szs">+${cr*n:.0f} / -${ml*n:.0f}</div></div>')
    return f'<div class="szg">{cells}</div>'


def brief_card(c, tier):
    ev = _ev(c)
    rec, rc, rwhy = _brief_reco(c, ev)
    score = c.get("edge_score") or 0
    scls = "score-high" if score >= 80 else "score-med" if score >= 65 else "score-low"
    tpp = (c.get("true_pop") or 0) * 100
    ipp = (c.get("implied_pop") or 0) * 100
    edge = c.get("edge_pp")
    edge_s = f'{edge:+.1f}pp' if edge is not None else '—'
    ec = "green" if (edge or 0) >= 0 else "red"
    roi = c.get("roi")
    # EV line
    if ev and ev.get("ev") is not None:
        evv = ev["ev"]; ecls = "green" if evv > 0 else "red"
        bewr = ev.get("be_wr")
        ev_line = (f'<div class="evln"><b class="num {_rg("ev",evv)}">${evv:+.0f}</b> expected value / contract '
                   f'· need <b>{bewr*100:.0f}%</b> to break even · model POP <b>{tpp:.0f}%</b> → '
                   f'<b class="{ecls}">{"positive" if evv>0 else "negative"} expectancy</b></div>')
    else:
        ev_line = ''
    flags = "".join(f'<div class="wf">{esc(w)}</div>' for w in (c.get("warnings") or []))
    struct = esc(c.get("structure") or "")
    ml = c.get("max_loss_usd") or 0
    head = (f'<div class="bch"><div class="bcl"><span class="bt">{esc(c["ticker"])}</span>'
            f'<span class="bstrat">{esc((c.get("strat_type") or "").replace("_"," ").upper())}</span>'
            f'<span class="bstruct num">{struct}</span></div>'
            f'<div class="bcr"><span class="bcred num">+${(c.get("credit_usd") or 0):.0f} credit</span>'
            f'<span class="bcml num">max loss ${ml:.0f} · {(c.get("dte") or 0)}DTE</span></div>'
            f'{env_badge(c)}<div class="brec rec-{rc}">{rec}</div></div>')
    meter = (f'<div class="bmeter"><span class="ml2">EDGE {score:.0f}/100</span>'
             f'<div class="mtrk"><i class="{scls}" style="width:{min(100,score):.0f}%"></i></div></div>')
    edge_cls = _rg("edge", edge) if edge is not None else ""
    pop = (f'<div class="bpop"><div class="pb"><span class="pl">True POP</span><b class="num {_rg("tpop",tpp)}">{tpp:.0f}%</b></div>'
           f'<div class="pb"><span class="pl">Implied POP</span><b class="num">{ipp:.0f}%</b></div>'
           f'<div class="pb edge"><span class="pl">POP edge</span><b class="num {edge_cls}">{edge_s}</b></div>'
           + (f'<div class="pb"><span class="pl">ROC</span><b class="num {_rg("roc",roi*100)}">{roi*100:.0f}%</b></div>' if roi is not None else '')
           + f'<div class="pb"><span class="pl">IV rank</span><b class="num">{(c.get("iv_rank") or 0):.0f}</b></div></div>')
    why = f'<div class="brwhy">{tier_badge(tier)} {esc(rwhy)}</div>'
    envln = ""
    e = c.get("env")
    if e and e.get("band") != "cool":
        r = e.get("recommendation") or {}
        # rationale already names the alt for warm/trim; only append a tail for flip/condor actions
        tail = ''
        if r.get("action") in ("flip_or_stand_aside", "condor_or_stand_aside"):
            tail = f' → <b>{esc(r.get("strike_note") or r.get("structure"))}</b>'
        envln = (f'<div class="envln env-{e["band"]}">⚠ Environment {e["band"].upper()} ({e.get("heat")}/100, '
                 f'{esc(e.get("threat_dir"))}): {esc(r.get("rationale") or "")}{tail}</div>')
    sizing = _brief_sizing(c)
    return (f'<div class="briefcard bl-{rc}">{head}{meter}{pop}{ev_line}{why}{envln}'
            f'{_brief_ticket(c)}{sizing}'
            + (f'<div class="wflist">{flags}</div>' if flags else '') + '</div>')


def _tile(label, value, sub, cls=""):
    return (f'<div class="tktile"><div class="tkl">{esc(label)}</div>'
            f'<div class="tkv {cls}">{value}</div><div class="tks">{esc(sub)}</div></div>')


def view_track():
    """Track Record — the ever-learning CLV scorecard. Reads the outcome ledger via
       clv_tracker and grades every prediction against a theta baseline (closing-line value)."""
    intro = ('<p class="q" style="margin:2px 0 14px">Every prediction, graded like a closing-line-value bet. '
             '<b>CLV</b> = how much the spread beat pure time-decay (theta) — a leading, low-variance signal of '
             'real edge that shows up before a trade resolves. News-catalyst trades (exogenous shocks) are '
             'flagged so they don\'t distort the model\'s calibration.</p>')
    if clv_tracker is None:
        return f'<h2>Track Record</h2>{intro}<div class="empty">clv_tracker module not found next to vega_app.py.</div>'
    try:
        s = clv_tracker.summary()
    except Exception as e:
        return f'<h2>Track Record</h2>{intro}<div class="warn">Could not read the outcome ledger: {esc(e)}</div>'

    fr = s.get("freshness") or {}
    stale_banner = ""
    if fr.get("stale"):
        ds = fr.get("days_stale")
        stale_banner = (f'<div class="provbar" style="border-color:rgba(240,69,90,.5);background:rgba(240,69,90,.08)">'
                        f'⚠ <b>Marks are {ds} days stale</b> (last re-mark {esc(fr.get("last_mark") or "?")}). '
                        f'CLV and unrealized figures below are frozen at that snapshot — the re-mark loop '
                        f'(<code>auto_paper_cycle.py</code>) has not run. Clear any stale '
                        f'<code>logs/auto_paper_cycle.lock</code> and re-run it to refresh.</div>')
    c = s["counts"]; clv = s["clv"]; ex = s["clv_ex_catalyst"]; er = s["edge_retention"]
    def pct(x): return f'{x*100:.0f}%' if x is not None else '—'
    def money(x): return f'${x:+.3f}' if x is not None else '—'
    beat_cls = "green" if (clv["beat_rate"] or 0) >= 0.5 else "red"
    cg = s["calibration_gap_pp"]
    cg_cls = "green" if (cg or 0) >= 0 else "red"
    tiles = (
        _tile("CLV beat-rate", pct(clv["beat_rate"]), f'{clv["n"]} positions scored vs theta', beat_cls)
        + _tile("Avg CLV / share", money(clv["avg_clv"]), '+ = beating time decay', beat_cls)
        # Named cohort, not a pooled figure. The ledger holds three incompatible regimes and
        # pooling them reported -56.8pp, which describes the fill model rather than the POP
        # model — the same number vega_status has refused to pool since the cohorts existed.
        + _tile("Calibration gap", (f'{cg:+.0f}pp' if cg is not None else '—'),
                (f'{str(s.get("calibration_cohort") or "")[:34]} · n={s.get("calibration_cohort_n")}'
                 + (f' · {s.get("calibration_cohorts_present")} cohorts in ledger'
                    if (s.get("calibration_cohorts_present") or 0) > 1 else ''))
                if cg is not None else 'realized − predicted POP', cg_cls)
        + _tile("Realized net P/L", (f'${er["total_realized_net_pl"]:+.0f}' if er["n_closed"] else '—'),
                f'{er["n_closed"]} closed · avg ${er["avg_realized_net_pl"]:+.2f}/ct' if er["avg_realized_net_pl"] is not None else 'no closes yet')
        + _tile("Ledger", f'{c["total"]}', f'{c["modeled"]} modeled · {c["open"]} open · {c["closed"]} closed')
    )
    tilerow = f'<div class="tkgrid">{tiles}</div>'
    if ex["n"] != clv["n"] and ex["n"]:
        tilerow += (f'<div class="dim" style="margin:-6px 0 14px">Ex-catalyst (news shocks quarantined): '
                    f'{pct(ex["beat_rate"])} beat, avg {money(ex["avg_clv"])} over {ex["n"]}.</div>')

    # Calibration reliability table
    crows = ""
    for b in s["calibration_curve"]:
        band = f'{b["lo"]*100:.0f}–{b["hi"]*100:.0f}%'
        if b["n"]:
            gap = b["gap"] * 100
            gcls = "gpass" if gap >= 0 else "gwarn"
            crows += (f'<tr><td class="l">{band}</td><td class="num">{b["n"]}</td>'
                      f'<td class="num">{b["predicted"]*100:.0f}%</td><td class="num">{b["realized"]*100:.0f}%</td>'
                      f'<td class="{gcls}">{gap:+.0f}pp</td></tr>')
        else:
            crows += (f'<tr><td class="l">{band}</td><td class="num">0</td>'
                      f'<td class="num dim">—</td><td class="num dim">—</td><td class="dim">—</td></tr>')
    cal = (f'<h3 style="margin-top:20px">Calibration — predicted POP vs realized hit-rate</h3>'
           f'<table class="gtbl"><thead><tr><th class="l">Predicted POP band</th><th>n</th>'
           f'<th>Predicted</th><th>Realized</th><th>Gap</th></tr></thead><tbody>{crows}</tbody></table>'
           f'<div class="dim" style="font-size:11px;margin-top:4px">Rows describe ONE cohort — '
           f'{esc(str(s.get("calibration_cohort") or "—"))}, n={s.get("calibration_cohort_n") or 0} of '
           f'{c["closed"]} closed. Grows meaningful past ~30 trades <b>within a cohort</b>; '
           f'pooling regimes that were selected or filled differently measures the fill model, '
           f'not the forecast.'
           + ('' if s.get("calibration_lead_eligible") else
              ' <b>No cohort here passes analysis_eligible</b> — every trade in this table was '
              'selected or filled on a basis the desk could not execute, so this is not yet '
              'evidence about the model.')
           + '</div>')

    # Per-position CLV table (worst → best; worst are the catalyst suspects)
    prows = ""
    for r in s["records"]:
        clvv = r["clv"]; cls = "gpass" if clvv > 0 else "gwarn"
        cat = ' <span class="ntag nt-flag">catalyst</span>' if r.get("news_catalyst") else ''
        adv = ' <span class="dim" title="mark moved ≥25% above entry">adverse</span>' if r.get("adverse") and not r.get("news_catalyst") else ''
        prows += (f'<tr><td class="l"><b>{esc(r["ticker"])}</b> <span class="dim">{esc((r.get("strategy") or "")[:8])}</span>{cat}{adv}</td>'
                  f'<td class="num">${r["entry"]:.2f}</td><td class="num">${r["theta_expected"]:.2f}</td>'
                  f'<td class="num">${r["mark"]:.2f}</td><td class="{cls}">{clvv:+.2f}</td></tr>')
    postbl = (f'<h3 style="margin-top:20px">Open predictions — CLV vs theta baseline</h3>'
              f'<table class="gtbl"><thead><tr><th class="l">Position</th><th>Entry</th>'
              f'<th>Theta-exp</th><th>Mark</th><th>CLV</th></tr></thead><tbody>{prows}</tbody></table>') if prows else ''

    # News split
    nw = s["news"]; cf = nw["confirms"]; ot = nw["other"]
    def nf(a): return f'{pct(a["beat_rate"])} beat · {money(a["avg_clv"])} avg (n={a["n"]})' if a["n"] else 'no data yet'
    newsblk = (f'<h3 style="margin-top:20px">News verdict &amp; catalysts</h3>'
               f'<div class="kv"><span class="k">Trades where news CONFIRMED the thesis</span><b>{nf(cf)}</b></div>'
               f'<div class="kv"><span class="k">Other (neutral / not logged)</span><b>{nf(ot)}</b></div>')
    if nw["catalyst_flagged"]:
        cl = "".join(f'<div class="noterow"><span class="ntag nt-flag">catalyst</span>'
                     f'<b>{esc(x["ticker"])}</b> CLV {x["clv"]:+.2f} — {esc(x.get("headline") or "flagged")}</div>'
                     for x in nw["catalyst_flagged"])
        newsblk += f'<div class="notes" style="margin-top:8px">{cl}</div>'
    else:
        newsblk += ('<div class="dim" style="font-size:11px;margin-top:4px">No catalyst-flagged trades. '
                    'The grader flags a position when it moves ≥25% against entry AND a material story hit the '
                    'ticker in that window — quarantined from calibration like an injury-driven bet loss.</div>')

    _pc, _mc = _cost_bases()
    costfoot = (f'<div class="dim" style="font-size:11px;margin-top:16px;border-top:1px solid var(--line);padding-top:8px">'
                f'Cost basis: realized paper P/L here nets <b>commission-only ~${_pc:.2f}/ct</b> (your real fills '
                f'capture slippage). The board/Brief pre-trade EV is more conservative — net of '
                f'<b>~${_mc:.2f}/ct</b> (commission + assumed slippage) — so a trade can read a touch better here '
                f'than its pre-trade EV. Same trade, two honest cost lenses.</div>')
    return (f'<h2>Track Record — the learning loop</h2>{intro}{stale_banner}{tilerow}{cal}'
            f'{_forecast_ledger_block()}{_gate_value_block()}{postbl}{newsblk}{costfoot}')


def _forecast_ledger_block() -> str:
    """Per claim type: is it calibrated, and does it DISCRIMINATE?

    Grades every claim in the ledger. The only grading UI that existed filtered to the BTC
    forecast cohort, so the 32 claims the trade engine has written — strike_holds,
    strike_untouched, direction — were invisible to it: made, stored, and shown nowhere.

    Two columns carry the weight. `Brier` says whether the stated confidence was honest.
    `Resolution` says whether the numbers distinguished one trade from another, which is the
    question Brier cannot answer — a forecaster who says the base rate about everything scores
    a respectable Brier and knows nothing.
    """
    try:
        from analysis import predictions as pred
        try:
            from analysis import btc_forecast as _bf
            btc_cohort = getattr(_bf, "COHORT", None)
        except Exception:
            btc_cohort = None
        # Everything EXCEPT the BTC forecast cohort, which has its own page. The filter used to
        # run the other way round and was the only grading UI in the app.
        rows = [r for r in pred.load()
                if not btc_cohort or (r.get("context") or {}).get("cohort") != btc_cohort]
        g = pred.grade(rows)
    except Exception as e:
        return (f'<h3 style="margin-top:20px">Forecast ledger</h3>'
                f'<div class="empty">Ledger unavailable: {esc(str(e))}</div>')

    if not g["total_claims"]:
        return ('<h3 style="margin-top:20px">Forecast ledger — what the engine claimed</h3>'
                '<div class="empty">No trade claims recorded yet.</div>')

    trows = ""
    for t, d in sorted(g["by_type"].items()):
        res = d.get("resolution")
        p = d.get("resolution_p")
        if p is None:
            disc, dcls = "—", "dim"
        elif p < 0.05:
            disc, dcls = f"yes (p={p:.3f})", "gpass"
        else:
            disc, dcls = f"no (p={p:.2f})", "gwarn"
        trows += (f'<tr><td class="l">{esc(t.replace("_", " "))}</td>'
                  f'<td class="num">{d["n"]}</td>'
                  f'<td class="num">{d["hit_rate"]:.0f}%</td>'
                  f'<td class="num">{d["avg_confidence"]:.0f}%</td>'
                  f'<td class="num">{d["brier"] if d["brier"] is not None else "—"}</td>'
                  f'<td class="num">{res if res is not None else "—"}</td>'
                  f'<td class="{dcls}">{disc}</td></tr>')
    if not trows:
        # An empty table under a live heading reads as "measured, found nothing". It is the
        # opposite: nothing has come due yet.
        nxt = min((r.get("resolves_on") or "" for r in rows if r.get("status") == "open"),
                  default="")
        trows = (f'<tr><td class="l dim" colspan="7">Nothing has resolved yet — no claim has '
                 f'reached its horizon'
                 f'{f", the first on {esc(nxt)}" if nxt else ""}. '
                 f'A claim type appears here once it can be graded.</td></tr>')

    # The ceiling. Resolution is bounded by how much the forecasts actually vary, so a ledger
    # whose claims all sit between 0.70 and 0.85 cannot demonstrate much discrimination however
    # right it is. That is a fact about the ENGINE, not about the sample size, and it will not
    # improve by waiting.
    probs = [r["probability"] for r in rows if r.get("probability") is not None]
    ceiling = ""
    if probs:
        mean = sum(probs) / len(probs)
        sd = (sum((x - mean) ** 2 for x in probs) / len(probs)) ** 0.5
        note = ("" if sd >= 0.12 else
                " — narrow. Resolution is capped by how much the forecasts vary, so widening "
                "what the engine is willing to claim matters as much as resolving more of them.")
        ceiling = (f'<div class="dim" style="font-size:11px;margin-top:4px">'
                   f'Forecast spread: sd {sd:.3f} across {len(probs)} claims '
                   f'({min(probs):.2f}–{max(probs):.2f}){esc(note)}</div>')

    pend = ('' if not g["open"] else
            f'<div class="dim" style="font-size:11px;margin-top:4px">'
            f'{g["open"]} claim(s) still open — nothing grades until its horizon passes.</div>')

    return (f'<h3 style="margin-top:20px">Forecast ledger — was the engine right, and did it '
            f'know something?</h3>'
            f'<table class="gtbl"><thead><tr><th class="l">Claim type</th><th>n</th>'
            f'<th>Correct</th><th>Claimed</th><th>Brier</th><th>Resolution</th>'
            f'<th>Discriminates?</th></tr></thead><tbody>{trows}</tbody></table>'
            f'{ceiling}{pend}'
            f'<div class="dim" style="font-size:11px;margin-top:4px">'
            f'<b>Brier</b> asks whether the stated confidence was honest. <b>Resolution</b> asks '
            f'whether it separated winners from losers — a forecaster who says the same number '
            f'about every trade scores a good Brier and knows nothing. Discrimination is judged '
            f'by shuffling outcomes against forecasts, so noise cannot pass as skill.</div>')


def _gate_value_block() -> str:
    """Per gate: did the candidates it BLOCKED actually behave worse than the ones it let through?

    The trade ledger can only say whether the picks were good. Eleven gates decide every entry
    and none had ever been measured, because a rejected candidate leaves no record — except
    that it does: every scan snapshot already stores each candidate's full gate results.
    """
    try:
        from analysis import counterfactuals as cf
        v = cf.value_of_information()
    except Exception as e:
        return (f'<h3 style="margin-top:20px">Gate value</h3>'
                f'<div class="empty">Counterfactual ledger unavailable: {esc(str(e))}</div>')

    if not v["n_total"]:
        return ('<h3 style="margin-top:20px">Gate value — are the eleven gates earning their '
                'place?</h3>'
                '<div class="empty">No counterfactual ledger yet. Build it with '
                '<code>python analysis/counterfactuals.py</code>.</div>')

    grows = ""
    for gate, d in sorted(v["gates"].items(),
                          key=lambda kv: -(kv[1].get("lift_pp") if kv[1].get("lift_pp") is not None else -999)):
        lift = d.get("lift_pp")
        if lift is None:
            lcell, lcls, verdict = "—", "dim", "not enough yet"
        elif lift > 0:
            lcell, lcls, verdict = f"{lift:+.0f}pp", "gpass", "avoids trouble"
        else:
            lcell, lcls, verdict = f"{lift:+.0f}pp", "gwarn", "no measured value"
        rate = (f'{d["touch_rate"]*100:.0f}%' if d.get("touch_rate") is not None else "—")
        grows += (f'<tr><td class="l">{esc(gate.replace("_", " "))}</td>'
                  f'<td class="num">{d["n_blocked"]}</td><td class="num">{rate}</td>'
                  f'<td class="{lcls}">{lcell}</td>'
                  f'<td class="l dim">{esc(verdict)}</td></tr>')

    base = v["qualified_touch_rate"]
    if base is None:
        head = (f'<div class="warn" style="margin-bottom:8px">Not yet measurable — '
                f'{v["n_total"]} spreads on record, none has lived the full '
                f'{v["horizon_days"]}-day horizon. Every spread is judged over the same window, '
                f'so a rate computed before that window closes would say the gates avoid nothing '
                f'when what actually happened is that nothing has had time to happen.</div>')
    else:
        head = (f'<div class="kv"><span class="k">Baseline — spreads that passed every gate</span>'
                f'<b>{base*100:.0f}% touched</b></div>')

    return (f'<h3 style="margin-top:20px">Gate value — are the eleven gates earning their place?</h3>'
            f'{head}'
            f'<table class="gtbl"><thead><tr><th class="l">Gate</th><th>Blocked</th>'
            f'<th>Touched</th><th>vs baseline</th><th class="l">Reading</th></tr></thead>'
            f'<tbody>{grows}</tbody></table>'
            f'<div class="dim" style="font-size:11px;margin-top:4px">'
            f'{v["n_total"]} spreads · {v["n_records"]} measurable · {v["n_maturing"]} still '
            f'maturing. Each row counts only candidates whose <b>only</b> failure was that gate '
            f'— the single clean read on it. A gate that blocks spreads which go on to be '
            f'touched more often is doing work; one whose rejects fare no worse is costing '
            f'opportunity and belongs in the ranking, not the contract. Touch is a leading '
            f'indicator, not a loss, and the sample is the top 3 candidates per ticker.</div>')


# Per-asset glyphs for the block headers. Absent is fine — the header just carries the ticker.
_ASSET_ICONS = {"IBIT": "&#8383;", "ETHA": "&#926;", "SOLZ": "&#9678;",
                "GLD": "&#9679;", "GDX": "&#9935;", "TLT": "&#8362;"}


def _tradeable_block(ticker):
    """The spreads actually available on THIS name, with their gate results.

    Generalised from the BTC-only version: the page about an asset that contains no trade in
    that asset is a readout, and that was the original complaint. Same table, driven by
    whichever ticker's block it appears under.
    """
    try:
        data, _path = _latest_candidates()
    except Exception as e:
        return f'<div class="empty">Candidate snapshot unavailable: {esc(str(e))}</div>'
    if not data:
        return ('<div class="dim" style="font-size:11px;margin-top:6px">No candidate snapshot '
                'yet — run a scan and any tradeable spreads appear here.</div>')

    cands = [c for row in (data.get("rows") or [])
             if str(row.get("ticker") or "").upper() == ticker.upper()
             for c in (row.get("candidates") or [])]
    if not cands:
        return (f'<div class="dim" style="font-size:11px;margin-top:6px">Nothing tradeable on '
                f'{esc(ticker)} in the latest scan — no spread in the delta band survived the '
                f'chain filter.</div>')

    def _gap_of(c):
        g = c.get("pop_gap")
        if g is None and c.get("true_pop") is not None and c.get("pop_implied") is not None:
            g = float(c["true_pop"]) - float(c["pop_implied"])
        return g

    # Gates first, then EDGE — not the raw ranking score. Sorting on gates alone put a spread
    # VEGA rates worse than the market at the top of a table headed "Tradeable now", which is
    # the contradiction this page was reporting.
    cands.sort(key=lambda c: ((c.get("gates_passed") or 0) == (c.get("gates_total") or 0),
                              (_gap_of(c) if _gap_of(c) is not None else -9),
                              c.get("score") or 0), reverse=True)
    rows_html = ""
    for c in cands[:5]:
        gp, gt = c.get("gates_passed") or 0, c.get("gates_total") or 0
        failed = [k for k, v in (c.get("gates") or {}).items() if not v]
        ok = gp == gt and gt
        # "Passes every gate" and a negative edge are both true at once, and the table said
        # only the flattering half. None of the eleven gates tests the edge: `pop` checks the
        # ABSOLUTE probability against a floor, never true_pop minus what the market implies.
        # So a spread can clear the whole contract while VEGA's own model says it is LESS
        # likely to work than the price assumes — which is the one fact that should stop you.
        _tp, _ip = c.get("true_pop"), c.get("pop_implied")
        _gap = c.get("pop_gap")
        if _gap is None and _tp is not None and _ip is not None:
            _gap = float(_tp) - float(_ip)
        if ok and _gap is not None and _gap < 0:
            why = (f"passes all {gt} gates, but VEGA POP is {abs(_gap)*100:.1f}pp BELOW the "
                   f"market's — no gate tests this")
        elif ok:
            why = "passes every gate"
        else:
            why = "blocked by " + ", ".join(f.replace("_", " ") for f in failed[:3])
        tp_ = c.get("true_pop")
        gap = c.get("pop_gap")
        if gap is None and tp_ is not None and c.get("pop_implied") is not None:
            gap = float(tp_) - float(c["pop_implied"])
        gtxt = f'{gap*100:+.1f}pp' if gap is not None else "—"
        gcol = ("var(--green)" if gap > 0 else "var(--red)") if gap is not None else "var(--ink3)"
        rows_html += (
            f'<tr><td class="l"><b>{esc(c.get("ticker"))}</b> '
            f'<span class="num">{c.get("short_strike"):g}/{c.get("long_strike"):g}</span> '
            f'<span class="dim">{esc(str(c.get("expiration"))[:10])} · {c.get("dte")}d</span></td>'
            f'<td class="num">${(c.get("natural_credit_usd") or 0):.0f}</td>'
            f'<td class="num">{abs(c.get("short_delta") or 0):.2f}</td>'
            f'<td class="num">{f"{tp_*100:.0f}%" if tp_ is not None else "—"}</td>'
            f'<td class="num" style="color:{gcol}">{gtxt}</td>'
            f'<td class="num">${(c.get("max_loss_usd") or 0):.0f}</td>'
            f'<td class="{"gpass" if ok else "gwarn"}">{gp}/{gt}</td>'
            f'<td class="l dim">{esc(why)}</td></tr>')

    return (f'<table class="gtbl"><thead><tr><th class="l">Spread</th><th>Credit</th>'
            f'<th>&Delta;</th>'
            f'<th title="The drift-removed probability of profit VEGA computes">VEGA POP</th>'
            f'<th title="VEGA POP minus the market-implied POP (1-delta). A VARIANCE read, not a directional one: negative means the stock has been moving more than its options price, so BOTH sides of a spread are worse than delta suggests and the mirror trade is not the fix. Positive means premium here is genuinely rich.">VRP edge</th>'
            f'<th>Max risk</th><th>Gates</th>'
            f'<th class="l">Reading</th></tr></thead><tbody>{rows_html}</tbody></table>')


def _cross_venue_ctx():
    """Every declared reference signal, fetched once for the whole page.

    Keyed by `cross_venue_ref_signal` exactly as the profiles declare it, so a profile and its
    feed cannot drift apart without landing as None — which the placeholder path already
    handles — rather than raising in the middle of a render.

    Only ENABLED assets are fetched. TLT declares MOVE and SOLZ declares SOL DVOL, and neither
    has a working feed; asking for them every page load would be two guaranteed network
    failures per render to produce a placeholder that is already decided by config.
    """
    ctx = {}
    try:
        from analysis import ticker_profile as tp
    except Exception:
        return ctx
    wanted = set()
    for tk in tp.cross_venue_tickers(enabled_only=True):
        cv = tp.cross_venue(tk)
        if cv and cv.get("ref_signal"):
            wanted.add(cv["ref_signal"])

    # Deribit: one snapshot per currency, reused by every ETF tracking it.
    for sig, cur in (("BTC_DVOL", "BTC"), ("ETH_DVOL", "ETH")):
        if sig not in wanted:
            continue
        try:
            from data import crypto
            snap = crypto.snapshot(cur)
            ctx[sig] = snap.get("dvol")
            ctx[f"_snap_{cur}"] = snap
        except Exception as e:
            logger.debug("[crypto] %s snapshot failed: %s", cur, e)
            ctx[sig] = None

    # Published indices, each carrying its own as-of date so staleness is checkable.
    try:
        from data import vol_indices
        vol_indices.populate(ctx, [s for s in wanted if s in vol_indices.SOURCES])
    except Exception as e:
        logger.debug("[vol_indices] populate failed: %s", e)
    return ctx


def _asset_block(ticker, ctx):
    """One asset's cross-venue block: four cards, the gap read, and its drivers.

    The same shell for every asset — an unenabled one gets the placeholder and the reason,
    never a card with blank numbers in it. A blank card claims the read was attempted and came
    back empty; the placeholder says it was never possible, and only one of those is true.
    """
    try:
        from analysis import ticker_profile as tp, cross_venue as cvmod
    except Exception as e:
        return f'<div class="empty">Cross-venue layer unavailable: {esc(str(e))}</div>'
    cv = tp.cross_venue(ticker)
    if cv is None:
        return ""
    icon = _ASSET_ICONS.get(ticker.upper(), "")
    # .title() alone renders the tickers-as-assets as "Btc"/"Eth"/"Sol"; the acronyms keep
    # their case and the words get title case.
    _tracks = (tp.declared(ticker).get("tracks") or ticker).replace("_", " ")
    name = " ".join(w.upper() if w.lower() in ("btc", "eth", "sol") else w.title()
                    for w in _tracks.split())
    hd = (f'<h2 style="margin-top:22px">{icon} {esc(ticker)} '
          f'<span class="dim" style="font-weight:400;font-size:13px">{esc(name)}</span></h2>')

    if not cv["enabled"]:
        return (hd + f'<div class="empty"><b>No native volatility reference in service.</b><br>'
                     f'{esc(cv["blocked_reason"] or "Reference declared but not fed.")}<br>'
                     f'<span class="dim">Declared reference: {esc(cv["ref_name"] or "—")} '
                     f'({esc(cv["source"] or "—")}). The block appears here the moment a '
                     f'current feed exists — nothing else has to change.</span></div>')

    # The currency comes from the ref_signal and is None for anything that is not a DVOL
    # reference. A default of "BTC" for the else-branch put Bitcoin's spot, DVOL, realised vol
    # and variance premium on GLD's card under gold's heading — one asset's numbers wearing
    # another's name, which is precisely the failure this whole layer exists to prevent and
    # which no test would have caught because every value rendered was individually true.
    cur = {"BTC_DVOL": "BTC", "ETH_DVOL": "ETH"}.get(cv["ref_signal"])
    snap = (ctx.get(f"_snap_{cur}") or {}) if cur else {}
    ref = cvmod.ref_value(ctx.get(cv["ref_signal"]))
    # A dead feed narrows the page and says so. Silence here would leave four empty cards
    # looking like a market with nothing to say, which is the opposite of the truth.
    dead = ""
    if cur and not snap.get("ok"):
        dead = (f'<div class="warn">No {esc(cur)} read this cycle — Deribit or Coinbase did not '
                f'answer. Treat this as absence of information, not a neutral reading.</div>')
    tiles = "" if dead else _asset_tiles(ticker, cv, ref, snap, ctx)

    # The gap itself, against this asset's own IV.
    x = None
    try:
        from data import fetcher, technicals
        ch = fetcher.get_options_chain(ticker, config.MIN_DTE, config.MAX_DTE)
        px = fetcher.get_price_data(ticker, period="5d")
        if ch and px is not None and not px.empty:
            iv = technicals.estimate_atm_iv(ch, float(px["Close"].iloc[-1]))
            x = cvmod.evaluate(ticker, iv, ctx)
    except Exception as e:
        logger.debug("[cross_venue] %s read failed: %s", ticker, e)

    if not (x and x.get("available")):
        gap_html = ('<div class="empty">No comparable read this cycle — '
                    + esc((x or {}).get("note") or "the chain or the reference did not answer.")
                    + '</div>')
    else:
        gap = x["gap_pp"]
        floor = x["noise_floor_pp"] or 0
        # Colour on the READING, not the sign — and only where the gap is a like-for-like
        # comparison. GDX sits 10-15pp above GVZ permanently, because GVZ prices bullion and
        # GDX is a levered equity claim on it; that spread is real information but it is not a
        # seller's edge, and a green badge parked on it every single day beside a note saying
        # "not a mispricing of either" is the badge people believe. Any derived reference —
        # self-derived or cross-asset — renders neutral.
        cls = ("dim" if x["derived_from"] else
               {"etf_rich": "pos", "etf_cheap": "neg"}.get(x["reading"], "dim"))
        asof = (f' <span class="dim">as of {esc(x["ref_asof"])}</span>'
                if x.get("ref_asof") else "")
        gap_html = (
            '<div class="xvgap">'
            f'<div class="xvrow"><span class="k">{esc(ticker)} ATM IV</span>'
            f'<b class="num">{x["proxy_iv_pp"]:.1f}%</b>'
            f'<span class="dim">this ETF&rsquo;s option chain</span></div>'
            f'<div class="xvrow"><span class="k">{esc(cv["ref_name"])}</span>'
            f'<b class="num">{x["ref_pp"]:.1f}%</b>'
            f'<span class="dim">{esc(cv["source"] or "")}{asof}</span></div>'
            f'<div class="xvrow tot"><span class="k">Gap</span>'
            f'<b class="num {cls}">{gap:+.1f}pp</b>'
            f'<span class="dim">{esc(x["reading"].replace("_", " ").upper())} '
            f'&middot; noise floor {floor:g}pp</span></div>'
            f'<div class="xvnote">{esc(x["note"])}</div>'
            '</div>')

    drivers = ""
    if cv["drivers"]:
        items = "".join(f'<li>{esc(d)}</li>' for d in cv["drivers"])
        drivers = ('<details class="xvdrv"><summary>What moves this gap</summary>'
                   f'<ul>{items}</ul></details>')

    # Trades first, research under them — per asset, so the reason sits beside the thing it is
    # a reason FOR. A page that only reads instruments is a readout; the spreads make it a
    # board, and burying them under the vol study is how it became a readout the first time.
    return (hd
            + f'<h3 class="xvh">Tradeable now — {esc(ticker)}</h3>'
            + _tradeable_block(ticker)
            + f'<h3 class="xvh">Why — what {esc(name)}{"&rsquo;" if name.endswith("s") else "&rsquo;s"}'
              f' own market is pricing</h3>'
            + dead + tiles
            + '<h3 class="xvh">Cross-venue volatility</h3>'
            + gap_html + drivers)


def _asset_tiles(ticker, cv, ref, snap, ctx):
    """The four-card shell — spot, implied, realised, variance premium.

    Identical shape for every asset, populated from whatever that asset actually has. The
    crypto path carries a full snapshot (spot and realised vol from Coinbase); a published
    index carries only the index level, so the cards it cannot fill say so rather than
    printing a zero.
    """
    def card(lab, val, sub, col=None):
        return _btc_card(lab, val, sub, col)

    if snap and snap.get("ok"):
        vrp = snap.get("vrp_pp")
        return ('<div class="cards">'
                + card(f'{snap.get("currency", "")} spot', f'${snap["spot"]:,.0f}',
                       "Deribit index")
                + card("Implied (DVOL)", f'{snap["dvol"]:.1f}%', "30-day implied vol")
                + card("Realised 30d", f'{snap["rv_30d"]:.1f}%'
                       if snap.get("rv_30d") is not None else "—",
                       "annualised on 365 — crypto never closes")
                + card("Variance premium",
                       f'{vrp:+.1f}pp' if vrp is not None else "—",
                       "implied over realised",
                       "var(--green)" if (vrp or 0) > 0 else "var(--amber)")
                + '</div>')

    raw = ctx.get(cv["ref_signal"])
    asof = raw.get("asof") if isinstance(raw, dict) else None
    return ('<div class="cards">'
            + card(cv["ref_name"] or "Reference",
                   f'{ref:.1f}%' if ref is not None else "—",
                   f'{cv["source"] or ""}{" · " + asof if asof else ""}')
            + card("Venue", cv["source"] or "—", cv["hours"].replace("_", " ")
                   if cv.get("hours") else "")
            + card("Noise floor", f'{cv["noise_floor_pp"]:g}pp'
                   if cv["noise_floor_pp"] is not None else "—",
                   "below this the gap is noise")
            + card("Independent?", "No" if cv["derived_from"] else "Yes",
                   f'derived from {cv["derived_from"].replace("_", " ")}'
                   if cv["derived_from"] else "separate venue",
                   "var(--amber)" if cv["derived_from"] else "var(--green)")
            + '</div>')


def view_bitcoin():
    """The BTC layer: what is tradeable on the proxies, and whether the vol read justifies it.

    The volatility research here is advisory and stays that way — the cross-venue gap never
    enters the gates dict, and the daily forecast writes a claim to the prediction ledger and
    nothing else. What changed is the order: the tradeable spreads come first, and the research
    sits under them as the reason to care. A page that only reads instruments is a readout; the
    trades are what make it a board.
    """
    try:
        from analysis import btc_forecast as bf
        from analysis import predictions as pred
        from analysis import ticker_profile as tp
    except Exception as e:
        return f'<h2>Crypto research</h2><div class="empty">Crypto layer unavailable: {esc(str(e))}</div>'

    intro = ('<p class="q">What is tradeable on these names right now, and does the underlying '
             'asset\'s own options market justify it? One block per asset that DECLARES a '
             'volatility reference — crypto against Deribit\'s DVOL, gold against CBOE\'s '
             'GVZ.</p>')

    # ── One block per declared asset, looped from config ──────────────────────
    # This page used to be BTC and nothing else: one snapshot, one proxy set, one hardcoded
    # DVOL label. Every asset now comes from ticker_profile.DECLARED, so adding ETH was a
    # config entry rather than a second copy of this view — and a copy is exactly where one
    # asset's noise floor silently becomes another's.
    ctx = _cross_venue_ctx()
    xv = "".join(_asset_block(tk, ctx) for tk in tp.cross_venue_tickers())

    # ── The forecast ledger ──
    try:
        g = pred.grade(cohort=bf.COHORT)
        claims = [r for r in pred.load()
                  if (r.get("context") or {}).get("cohort") == bf.COHORT]
    except Exception as e:
        return f'{intro}{head}{tiles}{xv}<div class="empty">Ledger unavailable: {esc(str(e))}</div>'

    d = (g.get("by_type") or {}).get("direction")
    if not claims:
        fc_block = ('<h2>Daily directional claim</h2>'
                    '<div class="empty">No claims yet — the first records on the next cycle.</div>')
    else:
        min_n = int(getattr(config, "PREDICTION_MIN_FOR_GRADE", 10))
        n_res = g["resolved"]
        bar = int(min(n_res / max(min_n, 1), 1.0) * 100)
        verdict = (d["verdict"] if d else
                   f"Nothing resolved yet — the first claims mature on their 14-day horizon.")
        # Below a handful of resolutions the table of awaiting rows is not a track record and
        # should not be laid out like one. Say what is being built and when the first answer
        # arrives, so an empty column reads as a horizon rather than as a failure — and so
        # nobody mistakes four pending claims for evidence.
        learning = ""
        if n_res < _DIRECTION_LEARNING_MIN:
            pending = [r for r in claims if r.get("status") not in ("resolved", "unresolvable")]
            # Only dates still ahead. predictions._defer keeps a claim open past its due date
            # when the horizon cannot be graded yet, so the earliest resolves_on is routinely
            # in the past — and "first resolutions expected 2026-08-04" printed on the 11th
            # reads as a broken promise rather than as a deferral.
            today = datetime.now().strftime("%Y-%m-%d")
            due = sorted(str(r.get("resolves_on") or "")[:10] for r in pending
                         if r.get("resolves_on"))
            ahead = [d for d in due if d >= today]
            when = (f" — first resolutions expected {esc(ahead[0])}" if ahead
                    else " — earliest claims are past their horizon, awaiting a gradeable close")
            learning = ('<div class="warn" style="margin:8px 0">'
                        'VEGA is building its directional track record. '
                        f'<b>{len(pending)}</b> claim(s) in flight{when}. '
                        'Nothing here is a hit rate yet.</div>')
        grade_line = (f'<div class="kv"><span class="k">Grading progress</span>'
                      f'<b>{n_res}/{min_n} resolved</b></div>'
                      f'{learning}'
                      f'<div class="sc"><div class="row"><div class="bar">'
                      f'<i class="{"low" if bar < 50 else ""}" style="width:{bar}%"></i></div></div></div>'
                      f'<div class="dim" style="margin-top:6px">{esc(verdict)}</div>')

        crows = ""
        for r in sorted(claims, key=lambda x: x.get("made_at") or "", reverse=True)[:20]:
            ctx = r.get("context") or {}
            st = r.get("status")
            if st == "resolved":
                mark = ('<span class="pos">correct</span>' if r.get("correct")
                        else '<span class="neg">wrong</span>')
            elif st == "unresolvable":
                mark = '<span class="dim">unresolvable</span>'
            else:
                mark = '<span class="dim">awaiting</span>'
            exp = (ctx.get("expected") or "").upper()
            ecls = "pos" if exp == "UP" else ("neg" if exp == "DOWN" else "dim")
            crows += (f'<tr><td class="l num">{esc(str(r.get("made_at"))[:10])}</td>'
                      f'<td class="l"><span class="{ecls}">{esc(exp)}</span></td>'
                      f'<td class="num">{(r.get("probability") or 0)*100:.0f}%</td>'
                      f'<td class="num">±{ctx.get("flat_band_pct", "—")}%</td>'
                      f'<td class="num">${(ctx.get("price_at_claim") or 0):,.0f}</td>'
                      f'<td class="l num">{esc(r.get("resolves_on"))}</td>'
                      f'<td class="l">{mark}</td>'
                      f'<td class="l dim">{esc((r.get("resolution_note") or "")[:70])}</td></tr>')
        fc_block = (f'<h2>Daily directional claim</h2>{grade_line}'
                    f'<div class="board" style="margin-top:10px"><table><thead><tr class="col">'
                    f'<th class="l">Made</th><th class="l">Call</th><th>Conf</th><th>Flat band</th>'
                    f'<th>BTC at claim</th><th class="l">Resolves</th><th class="l">Result</th>'
                    f'<th class="l">Note</th></tr></thead><tbody>{crows}</tbody></table></div>')

    foot = ('<div class="foot">All data here is free and unauthenticated: Deribit publishes DVOL '
            '(30-day implied vol) and a spot index per currency; Coinbase serves daily candles; '
            'CBOE\'s GVZ comes from FRED. No broker is connected and no crypto order can be '
            'placed from this system. The cross-venue gap never enters the gates dict, so it '
            'cannot block or force a trade whatever it reads. Noise floors are DECLARED per '
            'asset and never shared — BTC\'s ordinary venue basis would be a large move in '
            'gold. A reference older than three days is discarded rather than shown, because a '
            'gap is a subtraction and will happily produce a confident number from a stale '
            'operand.</div>')
    # Trades and research per asset, interleaved by _asset_block: the vol read is the REASON a
    # spread on that name might be worth taking, so it belongs beside those spreads rather than
    # in a separate section the reader has to hold two pages of context to connect.
    #
    # Titled for what it now holds. "Crypto research" was accurate when the page was BTC and
    # nothing else; with gold and rates blocks under it, a crypto-only heading over a GVZ card
    # is the same category error as a shared noise floor.
    return f'<h1>Research — cross-venue volatility</h1>{intro}{xv}{fc_block}{foot}'


def _btc_card(label, value, sub, color=None):
    style = f' style="color:{color}"' if color else ""
    return (f'<div class="card"><div class="lab">{esc(label)}</div>'
            f'<div class="val"{style}>{esc(value)}</div>'
            f'<div class="s">{esc(sub)}</div></div>')


def nav(view):
    links = ""
    labels = {"today": "Today", "track": "Track Record", "open": "Open",
              "bitcoin": "Research", "history": "History", "lottery": "Momentum"}
    for v in VIEWS:
        links += f'<a class="{"on" if v == view else ""}" href="/?view={v}">{labels[v]}</a>'
    is_open, _ = market_status()
    mkt = f'<span><span class="dot {"" if is_open else "off"}"></span>{"Market open" if is_open else "Market closed"}</span>'
    return (f'<div class="topnav"><div class="brand">VEGA<span class="sub">Market Opportunity Engine</span></div>'
            f'<div class="nav">{links}</div><div class="rside">{mkt}'
            f'<span>{datetime.now().strftime("%Y-%m-%d %H:%M")}</span></div></div>')


def render(view="today", flash=""):
    view = view if view in VIEWS else "today"
    rows = ol.load_records()
    s, closed, open_ = compute_stats(rows)
    board = load_board()
    tier = edge_tier(s)
    is_open, mkt_msg = market_status()

    banner = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    if not is_open and mkt_msg:
        banner += f'<div class="warn">⏸ {esc(mkt_msg)}</div>'
    if _scan_status["msg"] and _scan_status["msg"] not in flash:
        banner += f'<div class="flash">{esc(_scan_status["msg"])}</div>'

    if view == "track":
        content = view_track()
    elif view == "open":
        content = view_open(open_)
    elif view == "bitcoin":
        content = view_bitcoin()
    elif view == "history":
        content = view_history(s, closed)
    elif view == "lottery":
        content = view_lottery()
    else:
        content = view_today(board, s, tier)

    rescan = ('<form method="post" action="/scan" style="margin:0">'
              '<button class="primary" type="submit" data-busy="Scanning…">↻ Fast rescan</button></form>')
    foot = ('<div class="foot">Engine artifact (scan_latest.json) is the source of truth; the fast rescan is a '
            'provisional yfinance refresh without edge scores. True POP is the drift-removed historical frequency '
            '(C2); implied POP is what the market prices; edge = true − implied. Educational tool — no orders are '
            'placed and no money moves. Not financial advice.</div>')
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>VEGA · {view}</title><style>{CSS}</style></head><body>'
            f'{nav(view)}<div class="wrap">'
            f'<div style="display:flex;justify-content:flex-end;margin-top:12px">{rescan}</div>'
            f'{banner}{content}{foot}</div>{JS}</body></html>')


# ─────────────────────────────────────────────────────────────────────────────
# HTTP server
# ─────────────────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def _form(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            q = parse_qs(u.query)
            view = (q.get("view") or ["today"])[0]
            self._send(render(view))
        elif u.path == "/favicon.ico":
            self._send("", 204)
        else:
            self._send("not found", 404, "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        f = self._form()
        try:
            if p == "/scan":
                self._send(render("today", run_scan_now()))
            elif p == "/open_board":
                tk = (f.get("ticker") or "").upper()
                try:
                    tid = ol.open_paper_trade(
                        ticker=tk, short_strike=float(f["short"]), long_strike=float(f["long"]),
                        expiration=f.get("exp"), entry_credit_per_share=float(f["credit"]),
                        dte=int(f["dte"]) if f.get("dte") else None,
                        delta=float(f["delta"]) if f.get("delta") else None,
                        iv_rank=float(f["iv_rank"]) if f.get("iv_rank") else None,
                        implied_pop=float(f["implied_pop"]) if f.get("implied_pop") else None,
                        contracts=int(f.get("contracts") or 1), source="board")
                    self._send(render("today", f"Logged paper trade {tid} — {tk} ({f.get('contracts', 1)} ct)."))
                except Exception as e:
                    self._send(render("today", f"Could not log {tk}: {e}"))
            elif p == "/open_manual":
                tid = ol.open_paper_trade(
                    ticker=(f.get("ticker") or "").upper(), short_strike=float(f["short"]),
                    long_strike=float(f["long"]), expiration=f.get("exp"),
                    entry_credit_per_share=float(f["credit"]),
                    dte=int(f["dte"]) if f.get("dte") else None,
                    delta=float(f["delta"]) if f.get("delta") else None,
                    contracts=int(f.get("contracts") or 1), source="manual")
                self._send(render("history", f"Logged manual paper trade {tid}."))
            elif p in ("/watch", "/reject"):
                # Same handler for both: the only thing that differs is the word stored, and
                # splitting them would be two chances for the snapshot to drift apart.
                from analysis import decisions as dec
                which = dec.WATCH if p == "/watch" else dec.REJECT
                tk = (f.get("ticker") or "").upper()
                try:
                    dec.record(which, tk, snapshot={
                        "strategy": f.get("strategy"), "short_strike": f.get("short"),
                        "long_strike": f.get("long"), "expiration": f.get("exp"),
                        "dte": f.get("dte"), "credit_per_share": f.get("credit"),
                        "credit_usd": f.get("credit_usd"), "max_loss_usd": f.get("max_loss_usd"),
                        "delta": f.get("delta"), "true_pop": f.get("true_pop"),
                        "pop_implied": f.get("pop_implied") or f.get("implied_pop"),
                        "edge_score": f.get("edge_score"), "iv_rank": f.get("iv_rank"),
                        "vrp": f.get("vrp"), "spot": f.get("spot"), "roi": f.get("roi"),
                    })
                    word = "Watching" if which == dec.WATCH else "Rejected"
                    self._send(render("today", f"{word} {tk} — recorded with its entry state."))
                except Exception as e:
                    self._send(render("today", f"Could not record {tk}: {e}"))
            elif p == "/close":
                ok = ol.set_close(f.get("id"), float(f["exit_debit"]), f.get("outcome"), f.get("reason") or None)
                self._send(render("open", "Closed." if ok else "Close failed: id not found."))
            else:
                self._send("not found", 404, "text/plain")
        except Exception as e:
            self._send(render("dashboard", f"Error: {e}"))


# ─────────────────────────────────────────────────────────────────────────────
# Market-hours intraday scheduler — the single place cadence lives (no Task Scheduler)
# ─────────────────────────────────────────────────────────────────────────────
# While the cockpit is running AND US equity options are open (weekdays 9:30–16:00 ET), this
# refreshes the board off the free ≈15-min-delayed data and runs the paper cycle. News re-scrapes
# only ~hourly (data/news.py disk-cache TTL), so the 15-min board scans stay cheap. Each job runs
# as an isolated subprocess with PYTHONUTF8=1 — a job crash can't take down the cockpit, and the
# UTF-8 env prevents the cp1252 print crash that used to stall the old scheduled cycle.
_sched_state = {"board_at": None, "paper_at": None, "running": set()}
_sched_lock = threading.Lock()


def _spawn_job(name: str, argv: list, extra_env: dict) -> None:
    with _sched_lock:
        if name in _sched_state["running"]:
            print(f"[scheduler] {name} still running — skipping this tick")
            return
        _sched_state["running"].add(name)

    def _run():
        log_dir = BASE / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra_env)
        log_path = log_dir / f"intraday_{name}.log"
        start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n[{start}] START {name}: {' '.join(argv)}\n")
                fh.flush()
                proc = subprocess.run(argv, cwd=str(BASE), env=env,
                                      stdout=fh, stderr=subprocess.STDOUT)
                fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] END {name} exit={proc.returncode}\n")
            print(f"[scheduler] {name} finished (exit {proc.returncode}) — see {log_path.name}")
        except Exception as exc:
            print(f"[scheduler] {name} failed to run: {exc}")
        finally:
            with _sched_lock:
                _sched_state["running"].discard(name)

    threading.Thread(target=_run, name=f"vega-{name}", daemon=True).start()


def _scheduler_loop() -> None:
    if not getattr(config, "INTRADAY_SCHEDULER_ENABLED", True):
        print("[scheduler] disabled (config.INTRADAY_SCHEDULER_ENABLED=False)")
        return
    py = sys.executable
    board_min = float(getattr(config, "BOARD_REFRESH_MIN", 15))
    paper_min = float(getattr(config, "PAPER_CYCLE_MIN", 60))
    print(f"[scheduler] market-hours refresh armed — board every {board_min:.0f}m "
          f"(local only), paper cycle every {paper_min:.0f}m")
    was_open = None
    while True:
        try:
            is_open, _label = market_status()
            if is_open != was_open:
                print(f"[scheduler] market {'OPEN' if is_open else 'closed'} — "
                      f"{'refreshing' if is_open else 'idle until next session'}")
                was_open = is_open
            if is_open:
                now = datetime.now()
                b = _sched_state["board_at"]
                if b is None or (now - b).total_seconds() >= board_min * 60:
                    _sched_state["board_at"] = now
                    _spawn_job("board", [py, "main.py"], {"VEGA_NO_JARVIS": "1"})
                p = _sched_state["paper_at"]
                if p is None or (now - p).total_seconds() >= paper_min * 60:
                    _sched_state["paper_at"] = now
                    # VEGA_COCKPIT_SPAWNED=1 tells auto_paper_cycle it is already
                    # inside the cockpit-managed, market-hours-checked scheduler.
                    _spawn_job("paper", [py, "auto_paper_cycle.py"],
                               {"VEGA_COCKPIT_SPAWNED": "1"})
        except Exception as exc:
            print(f"[scheduler] tick error: {exc}")
        time.sleep(30)


def main():
    srv = None
    port = PORT
    for p in range(PORT, PORT + 12):
        try:
            srv = ThreadingHTTPServer((HOST, p), H)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"Could not bind any port in {PORT}-{PORT+11}. Close the old app window and retry.")
        return
    url = f"http://{HOST}:{port}/"
    if port != PORT:
        print(f"(port {PORT} was busy — using {port} instead)")
    print(f"VEGA app running at {url}  (Ctrl+C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Market-hours intraday refresh lives here (not in Task Scheduler) — see _scheduler_loop.
    threading.Thread(target=_scheduler_loop, name="vega-scheduler", daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped."); srv.shutdown()


if __name__ == "__main__":
    main()
