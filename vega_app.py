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
import math
import os
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import NormalDist
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from analysis import outcome_logger as ol
from paper_desk import compute_stats, _latest_candidates

BASE = Path(__file__).resolve().parent
CAND_DIR = BASE / "output" / "candidates"
SCAN_LATEST = BASE / "logs" / "scan_latest.json"
LOTTERY_LATEST = BASE / "logs" / "lottery_latest.json"
HOST, PORT = "127.0.0.1", 8765
SCAN = {"min_dte": 25, "max_dte": 45, "delta_min": 0.12, "delta_max": 0.35, "top": 3,
        "max_width": float(getattr(config, "MAX_SPREAD_WIDTH", 5))}
_scan_status = {"running": False, "msg": "", "at": None}

VIEWS = ("today", "open", "history", "lottery")
IVR_MIN = getattr(config, "MIN_IV_RANK", 45)

# Component max points for the score composition panel (mirrors edge_calculator).
EDGE_COMPONENTS = [
    ("vrp", "VRP / premium", 30),
    ("true_pop_edge", "True-POP edge", 25),
    ("technical", "Technical", 20),
    ("fundamentals", "Fundamentals", 10),
    ("news", "News", 10),
    ("earnings_safety", "Earnings safety", 5),
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
        return True, ""
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
def payoff_svg(short, long_, credit, width, spot=None, w=180, h=58):
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
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="vertical-align:middle">'
            f'<polygon points="{loss_pts}" fill="#F0455A" fill-opacity="0.16"/>'
            f'<polygon points="{prof_pts}" fill="#00C97A" fill-opacity="0.16"/>'
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" stroke="#2a2f3a" stroke-width="1"/>'
            f'<polyline points="{line_pts}" fill="none" stroke="#7f8794" stroke-width="1.4"/>'
            f'<circle cx="{px(be):.1f}" cy="{zy:.1f}" r="2.6" fill="#F0B429"/>'
            f'{spot_line}</svg>')


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
        "news_sentiment": t.get("news_sentiment"), "fundamentals_score": t.get("fundamentals_score"),
        "trend": t.get("trend"), "rsi": _f(t.get("rsi")),
        "news_summary": t.get("news_summary"), "warnings": t.get("warnings") or [],
        "auto_reasoning": t.get("auto_reasoning"), "nearest_support": _f(t.get("nearest_support")),
        "macd_crossover": t.get("macd_crossover"), "fundamentals_reasons": t.get("fundamentals_reasons") or [],
        "p_max_profit": _f(t.get("p_max_profit")), "roundtrip_cost": _f(t.get("estimated_round_trip_cost_per_contract")),
        "criteria": t.get("criteria") or [], "news_check": t.get("news_check") or {},
        "needs_validation": bool(t.get("needs_validation", False)),
        "sma20": _f(t.get("sma20")), "sma50": _f(t.get("sma50")), "max_loss_usd": max_loss,
        "strat_type": g["strat_type"], "risk_dir": g["risk_dir"], "breakevens": g["breakevens"],
        "structure": g["structure"], "cushion_pct": g["cushion"],
        "put_short": g.get("put_short"), "put_long": g.get("put_long"),
        "call_short": g.get("call_short"), "call_long": g.get("call_long"),
        "gates": None, "gates_passed": 8, "gates_total": 8,  # qualified → passed the gate set
        "priority": _f(t.get("edge_score"), 0.0),
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
        "price": _f(row.get("price")), "short": _f(c.get("short_strike")), "long": _f(c.get("long_strike")),
        "width": _f(c.get("width")), "credit_ps": _f(c.get("credit_per_share"), 0.0),
        "credit_usd": _f(c.get("credit_usd")), "dte": c.get("dte"), "exp": c.get("expiration"),
        "breakeven": _f(c.get("breakeven")), "delta": _f(c.get("short_delta")),
        "theta": _f(c.get("short_theta")), "iv_rank": ivr,
        "iv_method": (ctx.get("iv_rank_method") or "?").upper(),
        "true_pop": None,  # engine-only; unavailable on the fast path
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
                        "context": d.get("market_context") or {}, "regime": d.get("regime") or {},
                        "note": ""}
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
    return {"source": "legacy", "trades": trades, "asof": asof, "context": {}, "regime": {},
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
function sortBoard(th,key){var tbl=th.closest('table'),tb=tbl.tBodies[0];var dir=th.getAttribute('data-dir')==='asc'?'desc':'asc';tbl.querySelectorAll('th.srt').forEach(function(h){h.removeAttribute('data-dir');var a=h.querySelector('.arw');if(a)a.textContent='';});th.setAttribute('data-dir',dir);var ar=th.querySelector('.arw');if(ar)ar.textContent=dir==='asc'?' \u25B2':' \u25BC';var mains=[].slice.call(tb.querySelectorAll('tr.vmain'));mains.sort(function(a,b){var x=parseFloat(a.getAttribute('data-'+key)),y=parseFloat(b.getAttribute('data-'+key));if(isNaN(x))x=-1e12;if(isNaN(y))y=-1e12;return dir==='asc'?x-y:y-x;});mains.forEach(function(m){var i=m.getAttribute('data-i');tb.appendChild(m);var d=document.getElementById('vd-'+i);if(d)tb.appendChild(d);});}
function filterBoard(){var el=document.getElementById('fmaxloss');var v=el?parseFloat(el.value):NaN;var n=0,shown=0;document.querySelectorAll('tr.vmain').forEach(function(m){n++;var ml=parseFloat(m.getAttribute('data-maxloss'));var hide=!isNaN(v)&&ml>v;m.style.display=hide?'none':'';var d=document.getElementById('vd-'+m.getAttribute('data-i'));if(d){d.style.display=hide?'none':'';if(hide){m.classList.remove('open');d.classList.remove('open');}}if(!hide)shown++;});var fc=document.getElementById('fcount');if(fc)fc.textContent=isNaN(v)?'':(shown+' of '+n+' within budget');}
function clearFilter(){var e=document.getElementById('fmaxloss');if(e){e.value='';filterBoard();}}
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
.comp .tot{display:flex;justify-content:space-between;border-top:1px solid var(--line);padding-top:8px;margin-top:2px;font-weight:700}
.strip{display:flex;flex-wrap:wrap;gap:20px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:11px 16px;margin:4px 0}
.strip span{color:var(--ink3);font-size:11px;text-transform:uppercase;letter-spacing:.03em;margin-right:6px}.strip b{font-size:15px}
button{font:inherit;border:0;border-radius:7px;padding:6px 12px;cursor:pointer;font-weight:600}
.primary{background:var(--green);color:#04150e}.go{background:var(--green);color:#04150e}.close{background:var(--red);color:#fff}
input,select{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--ink)}
input.n{width:58px}
.empty{color:var(--ink3);font-style:italic;padding:14px;background:var(--panel);border:1px dashed var(--line);border-radius:10px}
.flash{background:#10233b;border:1px solid #204063;border-radius:9px;padding:9px 13px;margin:10px 0;font-size:13px}
.warn{background:var(--ambersoft);border:1px solid #4d3f16;border-radius:9px;padding:9px 13px;margin:10px 0;font-size:13px;color:var(--amber)}
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
.board thead .grp th{position:sticky;top:47px;background:var(--panel);z-index:6}
.board thead .col th{position:sticky;top:71px;background:var(--panel);z-index:6}
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
.lotto .conv.hi{color:var(--amber);background:#2a2413}
.lgrid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:700px){.lgrid{grid-template-columns:repeat(2,1fr)}}
.lgrid .cap{font-size:8.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.04em}
.lgrid .v{font-size:16px;font-weight:800;margin-top:2px}.lgrid .v.pos{color:var(--green)}.lgrid .v.neg{color:var(--red)}
.lgrid .dim{font-size:9.5px}
.lwhy{margin-top:11px;padding-top:9px;border-top:1px solid var(--line2);font-size:11.5px;color:var(--ink2);line-height:1.5}
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
        return f'<b class="num">{tp*100:.0f}%</b>{cf}<div class="dim num">{imp} · {edge_html}</div>'
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
    asof=board.get("asof")
    if not asof:
        return ("no scan yet", "warn", True)
    try:
        ts=datetime.fromisoformat(str(asof).replace("Z","").split(".")[0].replace("T"," ").strip())
    except Exception:
        try: ts=datetime.strptime(str(asof)[:16], "%Y-%m-%d %H:%M")
        except Exception: return (f"as-of {esc(asof)}", "flash", False)
    age=(datetime.now()-ts).total_seconds()/60.0
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
    return f'<h4 style="margin-top:14px">Selection criteria &middot; fitted to strategy</h4>{lead}{rows}{news}'


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
            f'<div class="ln">Need to win <b>{bw:.0f}%</b> to break even &middot; model says <b>{tp:.0f}%</b> &rarr; {verdict}</div></div>')


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


def detail_drawer(c, i, tier):
    cushion, max_loss, sc, scls = _card_extras(c)
    rok, riss = _reconcile(c)
    rec_html=('<span style="color:var(--green)">&#10003; reconciled</span>' if rok else '<span style="color:var(--amber)">&#9888; '+esc("; ".join(riss))+'</span>')
    diag = payoff_typed(c, 300, 110)
    delta=c.get("delta")

    def kv(k,v): return f'<div class="kv"><span class="k">{k}</span><b class="num">{v}</b></div>'
    kvs=[kv("Structure", f'{esc(c.get("structure") or "")} &middot; w{(c.get("width") or 0):g}'),
         kv("Strategy", esc((c.get("strat_type") or "bull_put").replace("_"," ").title())),
         kv("Credit", f'${(c["credit_usd"] or 0):.0f} (${c["credit_ps"]:.2f}/sh)')]
    if max_loss is not None: kvs.append(f'<div class="kv"><span class="k">Max loss</span><b class="num" style="color:var(--red)">-${max_loss:.0f}</b></div>')
    _bes=c.get("breakevens") or ([c["breakeven"]] if c.get("breakeven") is not None else [])
    kvs.append(kv("Breakeven"+("s" if len(_bes)>1 else ""), " / ".join(f"${b:.2f}" for b in _bes) or "-"))
    if c.get("true_pop") is not None: kvs.append(f'<div class="kv"><span class="k">True POP</span><b class="num" style="color:var(--green)">{c["true_pop"]*100:.0f}%</b></div>')
    if c.get("implied_pop") is not None: kvs.append(kv("Implied POP", f'{c["implied_pop"]*100:.0f}%'))
    if c.get("edge_pp") is not None: kvs.append(f'<div class="kv"><span class="k">POP edge</span><b class="num" style="color:var(--{"green" if c["edge_pp"]>=0 else "red"})">{c["edge_pp"]:+.1f}pp</b></div>')
    if c.get("roi") is not None: kvs.append(kv("ROC", f'{c["roi"]*100:.0f}%'))
    kvs.append(f'<div class="kv"><span class="k">VRP (IV-RV)</span><b class="num">{esc(c.get("vrp"))}</b> {tier_badge(tier)}</div>')
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
    log_form=(f'<form class="inline" method="post" action="/open_board" style="margin-top:12px">{hidden}'
              f'<input class="n" type="number" name="contracts" value="1" min="1">'
              f'<button class="go" type="submit" data-busy="Logging...">Log paper trade</button></form>')

    colA=f'<div>{_edge_block(c)}<h4 style="margin-top:12px">Key metrics - {esc(c["ticker"])}</h4>{"".join(kvs)}{log_form}</div>'
    colB=(f'<div><div class="payhd"><span>Payoff at expiration</span><span>{esc(c.get("dte"))} DTE</span></div>{diag}'
          f'<h4 style="margin-top:14px">Why VEGA chose this</h4>{_why_chosen(c)}{_verification(c)}{_criteria_panel(c)}</div>')
    conf=c.get("true_pop_conf") or "-"
    colC=(f'<div>{score_composition(c)}'
          f'<h4 style="margin-top:14px">Gates &amp; confidence</h4>'
          f'<div class="kv"><span class="k">Gates passed</span><b>{gate_dots(c)} {c["gates_passed"]}/{c["gates_total"]}</b></div>'
          f'<div class="kv"><span class="k">True-POP confidence</span><b>{esc(conf)}</b></div>'
          f'<div class="kv"><span class="k">Gate 1 status</span><b>{tier_badge(tier)}</b></div>'
          f'<div class="kv"><span class="k">Figures reconciled</span><b>{rec_html}</b></div></div>')
    return f'<div class="vdraw"><div class="vdrin">{colA}{colB}{colC}</div></div>'


def board_table(trades, tier):
    if not trades:
        return '<div class="empty">No qualified opportunities in the latest scan. Not a strong day to sell premium.</div>'
    body=""
    for i,c in enumerate(trades):
        cushion, max_loss, sc, scls = _card_extras(c)
        delta=c.get("delta"); roi=c.get("roi"); edge=c.get("edge_pp"); tpop=c.get("true_pop")
        edge_c=(f'<span class="{"pos" if edge>=0 else "neg"} num">{edge:+.1f}pp</span>') if edge is not None else '<span class="dim num">-</span>'
        roi_c=f'{roi*100:.0f}%' if roi is not None else "-"
        cush_c=f'{cushion:.1f}%' if cushion is not None else "-"
        delta_c=f'{delta:.2f}' if delta is not None else "-"
        maxloss_c=f'${max_loss:.0f}' if max_loss is not None else "-"
        why=c.get("why") or ""
        ev=_ev(c); ev_val=ev["ev"] if ev else None
        ev_c=(f'<span class="{"pos" if ev_val>=0 else "neg"} num">${ev_val:+.0f}</span>') if ev_val is not None else '<span class="dim num">-</span>'
        rec_ok, rec_issues=_reconcile(c)
        rec_badge=('<span class="rec ok" title="figures reconciled">&#10003;</span>' if rec_ok else '<span class="rec bad" title="'+esc("; ".join(rec_issues))+'">&#9888;</span>')
        datts=(f' data-i="{i}" data-edge="{edge if edge is not None else -999}"'
               f' data-tpop="{(tpop*100) if tpop is not None else -999}"'
               f' data-roc="{(roi*100) if roi is not None else -999}"'
               f' data-maxloss="{max_loss if max_loss is not None else 999999}"'
               f' data-cushion="{cushion if cushion is not None else -999}"'
               f' data-delta="{abs(delta) if delta is not None else 999}"'
               f' data-gates="{c["gates_passed"]}" data-score="{sc}" data-ev="{ev_val if ev_val is not None else -1e9}"')
        body+=(f'<tr class="vmain" id="vm-{i}"{datts} onclick="vtoggle({i})">'
               f'<td class="l"><span class="vcaret">&#9656;</span> {rec_badge}</td>'
               f'<td class="l tk"><span class="dim">{i+1}</span> <b>{esc(c["ticker"])}</b><div class="dim num">score {sc:.0f}</div></td>'
               f'<td class="l"><span class="sbadge {scls} num">{sc:.0f}</span> {_type_chip(c)}{_valtag(c)}<span class="strat">{esc(c["strategy"])}</span>'
               f'<div class="dim num">{esc(c.get("structure") or "")} - {esc(c.get("dte"))}d</div></td>'
               f'<td>{edge_c}</td>'
               f'<td>{pop_cell(c)}</td>'
               f'<td>{ev_c}</td>'
               f'<td class="num">{roi_c}</td>'
               f'<td class="num">{maxloss_c}</td>'
               f'<td class="num">{cush_c}</td>'
               f'<td class="gsep num">{delta_c}</td>'
               f'<td>{gate_dots(c)}<div class="dim num">{c["gates_passed"]}/{c["gates_total"]}</div></td>'
               f'<td class="l dim">{esc(why)}</td></tr>'
               f'<tr class="vdetail" id="vd-{i}"><td colspan="12">{detail_drawer(c,i,tier)}</td></tr>')
    order=[("score","Ticker","l"),("edge","Edge",""),("tpop","True POP",""),("ev","EV $",""),("roc","ROC",""),
           ("maxloss","Max loss",""),("cushion","Cushion",""),("delta","&#916;","gsep"),("gates","Gates","")]
    def sth(key,label,cls):
        c2=("%s srt"%cls) if cls else "srt"
        return "<th class=\"%s\" onclick=\"sortBoard(this,'%s')\">%s<span class=\"arw\"></span></th>"%(c2,key,label)
    head=("<thead><tr class=\"col\"><th class=\"l\" style=\"width:18px\"></th>"
          + sth(*order[0]) + "<th class=\"l\">Strategy</th>"
          + "".join(sth(*o) for o in order[1:]) + "<th class=\"l\">Why</th></tr></thead>")
    toolbar=("<div class=\"btoolbar\"><span class=\"flab\">Max loss &le; $</span>"
             "<input id=\"fmaxloss\" type=\"number\" class=\"n\" placeholder=\"any\" oninput=\"filterBoard()\">"
             "<button type=\"button\" class=\"ghostbtn\" onclick=\"clearFilter()\">Clear</button>"
             "<span class=\"dim\" id=\"fcount\" style=\"margin-left:auto\"></span>"
             "<span class=\"dim\">Click a header to sort</span></div>")
    return f'<div class="board">{toolbar}<table>{head}<tbody>{body}</tbody></table></div>'


def today_strip(board, s, tier):
    trades=board["trades"]; n=len(trades)
    fresh_label, _fc, _fs = _freshness(board)
    great=sum(1 for t in trades if (t.get("edge_score") or t.get("priority") or 0)>=80)
    elite=sum(1 for t in trades if (t.get("edge_score") or 0)>=90)
    regime=(board.get("regime") or {}).get("note") or (board.get("regime") or {}).get("regime_note") or ("Fast-scan mode" if board["source"]=="legacy" else "-")
    stars=min(5,max(0,great+1)); good=n>0 and (great or elite)
    verdict="Good day to sell premium" if good else "Weak board - few qualified setups"
    return ('<div class="tstrip">'
            f'<div class="tcard"><div class="lab">Verdict</div><div class="vd"><span class="d {"" if good else "off"}"></span>'
            f'<div><div class="txt">{verdict}</div><div class="sub">{n} qualified / {great} great / {elite} elite</div></div></div></div>'
            f'<div class="tcard"><div class="lab">Premium environment</div><div class="stars" style="margin-top:5px;color:var(--amber)">{"&#9733;"*stars}{"&#9734;"*(5-stars)}</div></div>'
            f'<div class="tcard"><div class="lab">Board source</div><div class="tbig num">{n}</div><div class="sub">{"engine artifact" if board["source"]=="engine" else "fast scan (provisional)"} &middot; {fresh_label}</div></div>'
            f'<div class="tcard"><div class="lab">Market regime</div><div class="tbig" style="font-size:15px;color:var(--green)">{esc(regime)}</div><div class="sub">Edge model: {tier_badge(tier)}</div></div>'
            '</div>')


def view_today(board, s, tier):
    trades=board["trades"]
    hero=hero_card(trades, tier).replace('class="hero"','class="hero" onclick="vopen(0)"',1)
    return ('<h1>Today\'s board</h1><p class="q">Is today worth trading - and what are the best setups right now?</p>'
            + today_strip(board, s, tier) + hero
            + '<div class="h2row" style="display:flex;align-items:baseline;gap:10px;margin:18px 0 8px">'
              '<h2 style="margin:0">Qualified opportunities</h2>'
              '<span class="dim" style="margin-left:auto">Click any row to open the full trade -&gt;</span></div>'
            + board_table(trades, tier))


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
    inner = ""
    total = 0
    for key, lab, mx in EDGE_COMPONENTS:
        v = _f(bd.get(key), 0) or 0
        total += v
        w = max(2, v / mx * 100)
        inner += (f'<div class="row"><div class="lab">{lab}</div>'
                  f'<div class="bar"><i style="width:{w:.0f}%"></i></div>'
                  f'<div class="v num">{v:.0f} / {mx}</div></div>')
    inner += f'<div class="tot"><span>Total edge score</span><span class="num">{total:.0f} / 100</span></div>'
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
        diagram = payoff_svg(r.get("short_strike"), r.get("long_strike"), r.get("actual_fill_credit"),
                             r.get("spread_width"), None)
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
    return (f'<h2>Open positions</h2>{portfolio_strip(open_)}'
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
    tech=[]
    if trend: tech.append(f"trend {trend}")
    if rsi is not None: tech.append(f"RSI {rsi:.0f}")
    if sup is not None: tech.append(f"support ${sup:.2f}")
    return (
        '<div class="lotto">'
        f'<div class="lh"><div><span class="tchip" title="Long call">CALL</span> <b class="tk">{tk}</b>'
        f'<span class="dim"> ${price:.2f}</span></div>'
        f'<div class="conv {"hi" if conv=="HIGH" else ""}">{esc(conv or "SPEC")}</div></div>'
        f'<div class="lgrid">'
        f'<div><div class="cap">Buy</div><div class="v">{strike:g} C</div><div class="dim">{esc(x.get("expiration"))} · {esc(dte)}d</div></div>'
        f'<div><div class="cap">Cost / max loss</div><div class="v neg">${(prem or 0):.0f}</div><div class="dim">${(prem_ps or 0):.2f}/sh</div></div>'
        f'<div><div class="cap">Home-run</div><div class="v pos">{mult_txt}</div><div class="dim">if ${(tgt_px or 0):.0f}</div></div>'
        f'<div><div class="cap">Breakeven move</div><div class="v">{(bemv or 0):+.1f}%</div><div class="dim">to ${(be or 0):.2f}</div></div>'
        f'<div><div class="cap">Prob ITM (Δ)</div><div class="v">{prob}</div><div class="dim">IV {(iv*100 if iv and iv<3 else iv) or 0:.0f}%</div></div>'
        f'</div>'
        f'<div class="lwhy"><b>Setup:</b> {setup or "-"}{(" · <b>Catalyst:</b> "+cat) if cat else ""}'
        f'{(" · News "+sent) if sent else ""}{(" · "+", ".join(tech)) if tech else ""}</div>'
        '</div>'
    )


def view_lottery():
    d=load_lottery()
    banner=('<div class="specbanner">&#9888; SPECULATIVE — buying single calls is a low-probability, '
            'high-variance bet. Max loss is 100% of the premium paid. This is NOT the defined-risk premium-selling '
            'edge; it is a capped-cost home-run swing for specific momentum/reversal conditions. Size tiny.</div>')
    if not d or not (d.get("lottery_calls") or []):
        return ('<h1>Lottery tickets</h1><p class="q">High-variance single-call swings, capped cost.</p>'
                + banner +
                '<div class="empty">No lottery candidates right now. The lottery scanner (lottery_scanner.py) '
                'runs separately and only surfaces setups in specific conditions (oversold-at-support bounce or '
                'confirmed momentum breakout with catalyst). Run it on the tower to populate this view.</div>')
    asof=esc(d.get("timestamp") or "")
    cards="".join(_lottery_card(x) for x in d["lottery_calls"])
    return ('<h1>Lottery tickets</h1><p class="q">High-variance single-call swings, capped cost &middot; as-of '
            + asof + '</p>' + banner + f'<div class="lottowrap">{cards}</div>')


def view_open(open_):
    return '<h1>Open positions</h1><p class="q">What am I currently holding?</p>' + open_section(open_)


def view_history(s, closed):
    return ('<h1>History</h1><p class="q">Can I trust it? — closed outcomes build Gate 1 confidence.</p>'
            + stat_cards(s) + closed_section(closed) + manual_form())


def nav(view):
    links = ""
    labels = {"today": "Today", "open": "Open", "history": "History", "lottery": "Lottery"}
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

    if view == "open":
        content = view_open(open_)
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
            elif p == "/close":
                ok = ol.set_close(f.get("id"), float(f["exit_debit"]), f.get("outcome"), f.get("reason") or None)
                self._send(render("open", "Closed." if ok else "Close failed: id not found."))
            else:
                self._send("not found", 404, "text/plain")
        except Exception as e:
            self._send(render("dashboard", f"Error: {e}"))


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
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped."); srv.shutdown()


if __name__ == "__main__":
    main()
