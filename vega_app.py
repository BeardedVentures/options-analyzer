#!/usr/bin/env python3
"""
vega_app.py — VEGA local web UI (the usable cockpit). v2.

Stdlib-only local web app (no pip installs) that turns the paper-desk workflow into point-and-click:

  • Rescan today's chains with one button (free, ~15-min delayed, no API/LLM credits)
  • Visual P&L payoff diagram on every candidate and open position (OptionStrat lesson)
  • Probability of profit + delta + theta inline on every row (tastytrade lesson)
  • One-click LOG a candidate as a paper trade; CLOSE from an inline form
  • Portfolio strip: capital-at-risk, credit collected, est net theta/day, unrealized P/L
  • Unrealized P/L on open positions, re-priced on each rescan
  • Market-hours banner so an empty pre-market board explains itself
  • Live scorecard: win rate, net-of-fee P/L, expectancy, calibration

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
HOST, PORT = "127.0.0.1", 8765
SCAN = {"min_dte": 25, "max_dte": 45, "delta_min": 0.12, "delta_max": 0.35, "top": 3,
        "max_width": float(getattr(config, "MAX_SPREAD_WIDTH", 5))}
_scan_status = {"running": False, "msg": "", "at": None}


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def market_status():
    """(is_open, label) for US equity options, in ET."""
    def _fmt_et(ts):
        # Windows strftime does not support %-I; %#I is Windows-specific.
        # Use portable formatting and trim leading zero for a clean hour.
        return ts.strftime("%I:%M %p").lstrip("0")

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True, ""  # unknown tz → don't nag
    if now.weekday() >= 5:
        return False, "Weekend — options market closed. Quotes will be empty until Monday 9:30 ET."
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 30:
        return False, f"Pre-market ({_fmt_et(now)} ET) — options open at 9:30 ET. Quotes are empty until then; rescan after open."
    if hm >= 16 * 60:
        return False, f"After hours ({_fmt_et(now)} ET) — options closed at 4:00 ET. Quotes may be stale."
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Visual P&L payoff diagram (bull put spread) — inline SVG, no data needed
# ─────────────────────────────────────────────────────────────────────────────
def payoff_svg(short, long_, credit, width, spot=None, w=168, h=54):
    try:
        short = float(short); long_ = float(long_); credit = float(credit); width = float(width)
    except Exception:
        return ""
    if width <= 0:
        return ""
    maxpl = credit * 100.0
    minpl = -(width - credit) * 100.0
    lo = long_ - max(width * 0.6, 1)
    hi = short + max(width * 0.9, 1)
    try:
        if spot is not None:
            hi = max(hi, float(spot) * 1.01)   # extend so the current-price marker is visible
    except Exception:
        pass
    pad = 6
    span = (hi - lo) or 1
    rng = (maxpl - minpl) or 1

    def px(p): return pad + (p - lo) / span * (w - 2 * pad)
    def py(v): return (h - pad) - (v - minpl) / rng * (h - 2 * pad)

    be = short - credit
    pts = [(lo, minpl), (long_, minpl), (short, maxpl), (hi, maxpl)]
    poly = " ".join(f"{px(p):.1f},{py(v):.1f}" for p, v in pts)
    zy = py(0)
    spot_line = ""
    if spot is not None:
        try:
            sp = float(spot)
            if lo <= sp <= hi:
                sx = px(sp)
                spot_line = (f'<line x1="{sx:.1f}" y1="{pad}" x2="{sx:.1f}" y2="{h-pad}" '
                             f'stroke="#2d6cdf" stroke-width="1" stroke-dasharray="2,2"/>')
        except Exception:
            pass
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="vertical-align:middle">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fff"/>'
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" stroke="#ddd" stroke-width="1"/>'
            f'<polyline points="{poly}" fill="none" stroke="#0a7d32" stroke-width="1.6"/>'
            f'<circle cx="{px(be):.1f}" cy="{zy:.1f}" r="2.4" fill="#c0392b"/>'
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
            chain = fetcher.get_options_chain(tk, 0, 200)  # wide window to find held strikes
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
                            msg=f"Scan complete — {n} candidates across {priced}/{len(rows)} names priced.")
        return _scan_status["msg"]
    except Exception as e:
        _scan_status.update(running=False, msg=f"Scan failed: {e}", at=datetime.now())
        return _scan_status["msg"]


# ─────────────────────────────────────────────────────────────────────────────
# HTML sections
# ─────────────────────────────────────────────────────────────────────────────
CSS = """
 body{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
 .wrap{max-width:1240px;margin:0 auto;padding:20px}
 h1{font-size:22px;margin:0}h2{font-size:15px;margin:20px 0 8px}
 .sub{color:#666;font-size:12px;margin:2px 0 12px}
 .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
 .cards{display:flex;flex-wrap:wrap;gap:9px;margin:6px 0 2px}
 .card{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:8px 12px;min-width:96px}
 .card .lab{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.03em}
 .card .val{font-size:20px;font-weight:700;margin:2px 0}.card .s{font-size:10px;color:#999}
 .strip{display:flex;flex-wrap:wrap;gap:16px;background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:9px 14px;margin:4px 0}
 .strip div span{color:#777;font-size:11px;text-transform:uppercase;letter-spacing:.03em;margin-right:6px}
 .strip b{font-size:15px}
 table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden}
 th{text-align:left;color:#555;font-weight:600;border-bottom:2px solid #eee;padding:6px 8px;font-size:12px}
 td{padding:5px 8px;border-bottom:1px solid #f2f2f2;vertical-align:middle}
 tr.win{background:#f0fbf3}tr.loss{background:#fdf1f1}
 .dim{color:#999;font-size:11px}.pos{color:#0a7d32;font-weight:600}.neg{color:#c0392b;font-weight:600}
 .chip{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;font-weight:600}
 .g{background:#e3f5e9;color:#0a7d32}.b{background:#fdecec;color:#c0392b}
 button{font:inherit;border:0;border-radius:6px;padding:5px 11px;cursor:pointer}
 .primary{background:#2d6cdf;color:#fff}.go{background:#0a7d32;color:#fff}.close{background:#c0392b;color:#fff}
 input,select{font:inherit;padding:4px 6px;border:1px solid #ccc;border-radius:5px}input.n{width:56px}
 .empty{color:#999;font-style:italic;padding:10px;background:#fff;border:1px dashed #ddd;border-radius:8px}
 .flash{background:#eef4ff;border:1px solid #b7d0ff;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:13px}
 .warn{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:13px}
 form.inline{display:inline-flex;gap:5px;align-items:center;margin:0}
 .foot{color:#888;font-size:11px;margin:20px 0;line-height:1.6}
"""


def stat_cards(s):
    def c(l, v, sub=""):
        return f'<div class="card"><div class="lab">{l}</div><div class="val">{v}</div><div class="s">{sub}</div></div>'
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
            utheta += -(r["short_theta"]) * 100 * ct   # short a put ≈ +theta (est, near leg)
        if isinstance(r.get("unrealized_net"), (int, float)):
            unreal += r["unrealized_net"] * ct
            have_unreal = True
    ucls = "pos" if unreal >= 0 else "neg"
    unreal_html = f'<b class="{ucls}">${unreal:+.0f}</b>' if have_unreal else '<b class="dim">—</b>'
    return ('<div class="strip">'
            f'<div><span>Open</span><b>{len(open_)}</b></div>'
            f'<div><span>Capital at risk</span><b>${car:.0f}</b></div>'
            f'<div><span>Credit collected</span><b>${credit:.0f}</b></div>'
            f'<div><span>Est net θ/day</span><b class="pos">${utheta:+.0f}</b></div>'
            f'<div><span>Unrealized P/L</span>{unreal_html}</div></div>')


def _model_pop_estimate(spot, breakeven, dte, atm_iv=None, rv=None):
    """Estimate P(spot_at_exp > breakeven) using a zero-drift lognormal model.
    Volatility source is blended from ATM IV and realized vol when available."""
    try:
        s = float(spot)
        be = float(breakeven)
        t = max(float(dte), 1.0) / 365.0
    except Exception:
        return None
    if s <= 0 or be <= 0 or t <= 0:
        return None

    vols = []
    for v in (atm_iv, rv):
        try:
            fv = float(v)
            if fv > 0:
                vols.append(fv)
        except Exception:
            continue
    if not vols:
        return None

    sigma = max(0.05, sum(vols) / len(vols))
    denom = sigma * math.sqrt(t)
    if denom <= 0:
        return None

    z = math.log(s / be) / denom
    p = NormalDist().cdf(z)
    return max(0.01, min(0.99, p))


def _priority_score(pop_score, roi, fail_count):
    """Composite ranking score (0-100): POP-heavy with ROI boost and signal penalties."""
    pop_component = max(0.0, min(1.0, float(pop_score or 0.0))) * 100.0
    roi_component = max(0.0, min(1.0, float(roi or 0.0))) * 100.0
    base = (0.65 * pop_component) + (0.35 * roi_component)
    # Penalize failed quality signals so high ROI does not dominate weak structures.
    penalized = base - (fail_count * 6.0)
    return max(0.0, min(100.0, penalized))


def candidates_section():
    data, path = _latest_candidates()
    if not data:
        return ('<h2>Candidates</h2><div class="empty">No scan yet. Click <b>↻ Rescan today</b> '
                '(during market hours) to pull live chains.</div>')
    meta = data.get("meta") or {}
    asof = meta.get("stamp") or "unknown"
    source = meta.get("source") or "unknown"
    ivr_min = getattr(config, "MIN_IV_RANK", 45)

    gate_labels = {
        "iv_rank": f"IV-Rank<{ivr_min}",
        "delta_cap": "delta>cap",
        "otm_buffer": "buffer",
        "credit_to_width": "credit/width",
        "min_credit_usd": "min_credit",
        "liquidity": "liquidity",
        "pop": "POP",
        "dte_window": "DTE",
    }

    body = ""
    for row in data.get("rows", []):
        cands = row.get("candidates") or []
        if not cands:
            continue
        ctx = row.get("ctx") or {}
        ivr = ctx.get("iv_rank")
        ivm = (ctx.get("iv_rank_method") or "?").upper()
        ivr_tag = "Est" if ivm == "APPROX" else "Hist"
        ivr_txt = f'{ivr:.0f}<span class=dim>({ivr_tag})</span>' if ivr is not None else "—"
        atm_iv = ctx.get("atm_iv")
        rv = ctx.get("rv")
        ranked_rows = []
        for orig_rank, c in enumerate(cands, start=1):
            gates = c.get("gates") or {}
            fails = []
            if ivr is None or ivr < ivr_min:
                fails.append(gate_labels["iv_rank"])
            for key in ("delta_cap", "otm_buffer", "credit_to_width", "min_credit_usd", "liquidity", "pop", "dte_window"):
                if not gates.get(key, False):
                    fails.append(gate_labels[key])

            pass_count = (1 if ivr is not None and ivr >= ivr_min else 0) + sum(1 for v in gates.values() if v)
            total_count = 1 + len(gates)
            gclass = "g" if not fails else "b"
            fail_txt = "PASS" if not fails else ", ".join(fails)

            th = c.get("short_theta")
            th_txt = f'{-th*100:+.0f}' if isinstance(th, (int, float)) else "—"  # position θ/day est ($/ct)
            model_pop = _model_pop_estimate(row.get("price"), c.get("breakeven"), c.get("dte"), atm_iv=atm_iv, rv=rv)
            implied_pop = float(c.get("pop_implied") or 0.0)
            pop_main = model_pop if model_pop is not None else implied_pop
            pop_src = "model" if model_pop is not None else "delta"
            roi = c.get("roi")
            roi_txt = f'{float(roi)*100:.0f}%' if isinstance(roi, (int, float)) else "—"
            priority = _priority_score(pop_main, roi, len(fails))
            ranked_rows.append((priority, orig_rank, c, pass_count, total_count, gclass, fail_txt, th_txt, pop_main, pop_src, implied_pop, roi_txt))

        ranked_rows.sort(key=lambda x: x[0], reverse=True)

        for priority, orig_rank, c, pass_count, total_count, gclass, fail_txt, th_txt, pop_main, pop_src, implied_pop, roi_txt in ranked_rows:
            diagram = payoff_svg(c["short_strike"], c["long_strike"], c["credit_per_share"], c["width"], row.get("price"))
            body += (
                f'<tr><td><b>{esc(row["ticker"])}</b> <span class=dim>${row["price"]:.2f}</span></td>'
                f'<td>{diagram}</td>'
                f'<td>{c["short_strike"]:g}/{c["long_strike"]:g}<div class=dim>w{c["width"]:g} · be ${c.get("breakeven", "")}</div></td>'
                f'<td>{esc(c["expiration"])}<div class=dim>{c.get("dte")}d</div></td>'
                f'<td><b>{priority:.0f}</b><div class=dim>POP+ROI adj</div></td>'
                f'<td>${c["credit_usd"]:.0f}<div class=dim>${c["credit_per_share"]:.2f}/sh</div></td>'
                f'<td><b>{roi_txt}</b><div class=dim>credit/max risk</div></td>'
                f'<td>{c["credit_to_width"]*100:.0f}%</td>'
                f'<td><b>{pop_main*100:.0f}%</b><div class=dim>{pop_src} · Δ {implied_pop*100:.0f}%</div></td>'
                f'<td>{c["short_delta"]:.2f}</td><td>{th_txt}</td><td>{ivr_txt}</td>'
                f'<td><span class="chip {gclass}">{pass_count}/{total_count}</span></td>'
                f'<td><span class="dim">{esc(fail_txt)}</span></td>'
                f'<td><form class="inline" method="post" action="/open_candidate">'
                f'<input type="hidden" name="ticker" value="{esc(row["ticker"])}">'
                f'<input type="hidden" name="rank" value="{orig_rank}">'
                f'<input class="n" type="number" name="contracts" value="1" min="1">'
                f'<button class="go" type="submit">Log</button></form></td></tr>'
            )

    return (f'<h2>Candidates <span class=dim>({esc(os.path.basename(path))})</span></h2>'
            f'<div class="sub">As-of: {esc(asof)} · source: {esc(source)} (~15-min delayed, unofficial) · '
            f'Quality signals = IV rank + delta cap + OTM buffer + credit/width + min credit + liquidity + POP + DTE · '
            f'POP model uses breakeven + DTE + blended ATM IV/RV (zero-drift lognormal), with Δ POP shown for reference. '
            f'Priority = 65% POP + 35% ROI, minus penalties for failed signals.</div>'
            f'<table><thead><tr><th>Ticker</th><th>Payoff</th><th>Short/Long</th><th>Exp</th><th>Priority</th><th>Credit</th>'
            f'<th>ROI</th><th>Cr/W</th><th>POP score</th><th>Δ</th><th>θ/day</th><th>IV-Rank</th><th>Quality</th><th>Signals</th><th>Log</th>'
            f'</tr></thead><tbody>{body}</tbody></table>')


def open_section(open_):
    if not open_:
        return ('<h2>Open positions</h2>' + portfolio_strip([]) +
                '<div class="empty">None yet — log a candidate above.</div>')
    rows = ""
    for r in open_:
        un = r.get("unrealized_net")
        ct = int(r.get("contracts") or 1)
        if isinstance(un, (int, float)):
            tot = un * ct
            un_html = f'<span class="{"pos" if tot>=0 else "neg"}">${tot:+.0f}</span><div class=dim>@${esc(r.get("current_mark"))}</div>'
        else:
            un_html = '<span class=dim>—</span>'
        diagram = payoff_svg(r.get("short_strike"), r.get("long_strike"), r.get("actual_fill_credit"),
                             r.get("spread_width"), None)
        rows += (
            f'<tr><td><b>{esc(r.get("ticker"))}</b></td><td>{diagram}</td>'
            f'<td>{esc(r.get("short_strike"))}/{esc(r.get("long_strike"))}</td>'
            f'<td>{esc(r.get("expiration"))}<div class=dim>{esc(r.get("dte"))}d</div></td>'
            f'<td>${esc(r.get("actual_fill_credit"))}</td><td>{ct}</td>'
            f'<td>${esc(r.get("max_loss_per_contract"))}</td><td>{un_html}</td>'
            f'<td><form class="inline" method="post" action="/close">'
            f'<input type="hidden" name="id" value="{esc(r.get("id"))}">'
            f'<input class="n" type="number" step="0.01" name="exit_debit" placeholder="exit" required>'
            f'<select name="outcome"><option>win</option><option>loss</option><option>scratch</option></select>'
            f'<input type="text" name="reason" placeholder="reason" style="width:110px">'
            f'<button class="close" type="submit">Close</button></form></td></tr>'
        )
    return (f'<h2>Open positions</h2>{portfolio_strip(open_)}'
            f'<table><thead><tr><th>Ticker</th><th>Payoff</th><th>Short/Long</th><th>Exp</th>'
            f'<th>Credit/sh</th><th>Ct</th><th>Max loss</th><th>Unreal P/L</th>'
            f'<th>Close (enter exit debit)</th></tr></thead><tbody>{rows}</tbody></table>')


def closed_section(closed):
    if not closed:
        return '<h2>Closed trades</h2><div class="empty">None yet.</div>'
    rows = ""
    for r in sorted(closed, key=lambda x: x.get("closed_at") or "", reverse=True):
        cls = "win" if r.get("outcome") == "win" else ("loss" if r.get("outcome") == "loss" else "")
        net = r.get("realized_net_pl_per_contract")
        rows += (f'<tr class="{cls}"><td><b>{esc(r.get("ticker"))}</b></td>'
                 f'<td>{esc(r.get("short_strike"))}/{esc(r.get("long_strike"))}</td>'
                 f'<td>{esc(r.get("expiration"))}</td><td>${esc(r.get("actual_fill_credit"))}</td>'
                 f'<td>${esc(r.get("exit_price"))}</td><td>{esc((r.get("outcome") or "").upper())}</td>'
                 f'<td>${esc(net)}</td><td class=dim>{esc(r.get("exit_reason") or "")}</td></tr>')
    return (f'<h2>Closed trades</h2><table><thead><tr><th>Ticker</th><th>Short/Long</th><th>Exp</th>'
            f'<th>Credit</th><th>Exit</th><th>Result</th><th>Net/ct</th><th>Reason</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


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
            '<button class="primary" type="submit">Log manual</button></form>')


def dashboard(flash=""):
    rows = ol.load_records()
    s, closed, open_ = compute_stats(rows)
    gold = getattr(config, "ROBINHOOD_GOLD", False)
    rt = ol._round_trip_cost_per_contract()
    is_open, mkt_msg = market_status()
    banner = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    if not is_open and mkt_msg:
        banner += f'<div class="warn">⏸ {esc(mkt_msg)}</div>'
    if _scan_status["msg"] and _scan_status["msg"] not in flash:
        banner += f'<div class="flash">{esc(_scan_status["msg"])}</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VEGA Paper Desk</title><style>{CSS}</style></head>
<body><div class="wrap">
<div class="bar"><h1>VEGA Paper Desk</h1>
 <form method="post" action="/scan" style="margin:0"><button class="primary" type="submit">↻ Rescan today</button></form></div>
<div class="sub">{datetime.now().strftime('%Y-%m-%d %H:%M')} · PAPER mode · AI disabled (no credits) ·
 Robinhood{' Gold' if gold else ''} fees ≈ ${rt:.2f} round-trip/contract · localhost only</div>
{banner}
{stat_cards(s)}
{candidates_section()}
{open_section(open_)}
{closed_section(closed)}
{manual_form()}
<div class="foot">Payoff diagram: green = P/L at expiration across price; red dot = break-even; blue dashed = current price.
 Net/Unrealized P/L are after Robinhood round-trip fees. Prove positive net expectancy and win rate ≥ implied POP
 across ~30 closes before paying for data or going live. Educational tool — not financial advice; every order is your own.</div>
</div></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def _form(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send(dashboard())
        elif p == "/favicon.ico":
            self._send("", 204)
        else:
            self._send("not found", 404, "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        f = self._form()
        try:
            if p == "/scan":
                self._send(dashboard(run_scan_now()))
            elif p == "/open_candidate":
                data, _ = _latest_candidates()
                tk = (f.get("ticker") or "").upper()
                rank = int(f.get("rank") or 1)
                contracts = int(f.get("contracts") or 1)
                row = next((r for r in (data or {}).get("rows", []) if r.get("ticker") == tk), None)
                cands = (row or {}).get("candidates") or []
                if not cands or rank < 1 or rank > len(cands):
                    self._send(dashboard(f"Could not log {tk}: candidate not found.")); return
                c = cands[rank - 1]
                tid = ol.open_paper_trade(
                    ticker=tk, short_strike=c["short_strike"], long_strike=c["long_strike"],
                    expiration=c["expiration"], entry_credit_per_share=c["credit_per_share"],
                    dte=c.get("dte"), delta=c.get("short_delta"), theta=c.get("short_theta"),
                    iv_rank=(row.get("ctx") or {}).get("iv_rank"), implied_pop=c.get("pop_implied"),
                    contracts=contracts, source="candidate")
                self._send(dashboard(f"Logged paper trade {tid} ({contracts} contract(s))."))
            elif p == "/open_manual":
                tid = ol.open_paper_trade(
                    ticker=(f.get("ticker") or "").upper(), short_strike=float(f["short"]),
                    long_strike=float(f["long"]), expiration=f.get("exp"),
                    entry_credit_per_share=float(f["credit"]),
                    dte=int(f["dte"]) if f.get("dte") else None,
                    delta=float(f["delta"]) if f.get("delta") else None,
                    contracts=int(f.get("contracts") or 1), source="manual")
                self._send(dashboard(f"Logged manual paper trade {tid}."))
            elif p == "/close":
                ok = ol.set_close(f.get("id"), float(f["exit_debit"]), f.get("outcome"), f.get("reason") or None)
                self._send(dashboard("Closed." if ok else "Close failed: id not found."))
            else:
                self._send("not found", 404, "text/plain")
        except Exception as e:
            self._send(dashboard(f"Error: {e}"))


def main():
    # Try the preferred port; if an old instance still holds it, fall through to the next free one.
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
