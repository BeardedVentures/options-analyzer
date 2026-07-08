#!/usr/bin/env python3
"""
vega_candidates.py — REAL bull-put spread candidate viewer (validation mode)

WHY THIS EXISTS
---------------
`main.py` is the disciplined scanner: it only surfaces trades that clear every hard
gate (IV-Rank >= 45, edge score >= 60, POP >= 72%, delta cap, OTM buffer, liquidity,
credit/width). In a low-vol regime that correctly produces zero qualifiers — which
leaves you with a blank tip sheet and nothing to eyeball.

This tool is the opposite lens. It pulls the SAME live, free, ~15-min-delayed chains
the scanner uses (via data/fetcher.get_options_chain) and shows you the best REAL
bull-put spreads that actually exist right now in your 25–45 DTE window — with real
strikes, real bid/ask, real net credit, width, max loss, breakeven, delta and POP —
and it annotates each one with PASS/FAIL against every VEGA gate so you can see the
real structure AND why the strict scan did or didn't surface it.

Nothing here is synthetic. Every strike and price comes from the live chain. Use it to
visually verify conditions against your broker and decide what (if anything) to tune.

It NEVER writes to scan_log.json or the Gate-1 ledger and never touches main.py's
logic — it's a read-only viewer, safe to run any time.

USAGE (run on the tower, where yfinance works)
----------------------------------------------
    cd "C:\\Users\\Josh\\AI_OS\\AI_OS\\projects\\Stock Market Tools\\options_intelligence"
    python vega_candidates.py                 # 25–45 DTE, top 3 per ticker, opens HTML
    python vega_candidates.py --min-dte 25 --max-dte 45 --top 5
    python vega_candidates.py --tickers SPY,QQQ,AMD --no-open
    python vega_candidates.py --delta-min 0.10 --delta-max 0.35

Output: output/candidates/candidates_<YYYY-MM-DD_HHMM>.html  (+ .json)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import webbrowser
from datetime import datetime
from itertools import groupby
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data import fetcher

# technicals is optional context (IV-Rank / VRP). Degrade gracefully if it errors.
try:
    from data import technicals as _tech
except Exception:  # pragma: no cover
    _tech = None

try:
    from analysis.edge_calculator import calculate_spread_metrics
except Exception:  # pragma: no cover
    calculate_spread_metrics = None

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vega_candidates")

SPY_LIKE = getattr(config, "SPY_BUFFER_TICKERS", {"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT"})


# ─────────────────────────────────────────────────────────────────────────────
# Per-ticker vol context (IV-Rank / VRP) — best effort
# ─────────────────────────────────────────────────────────────────────────────
def vol_context(ticker: str, puts: list, current_price: float) -> dict:
    """ATM IV, IV-Rank (APPROX until history accrues), VRP. All best-effort."""
    ctx = {"atm_iv": None, "iv_rank": None, "iv_rank_method": None, "rv": None, "vrp_pp": None}
    if not puts:
        return ctx
    # ATM IV = IV of the put whose strike is closest to spot
    atm = min(puts, key=lambda o: abs(o["strike"] - current_price))
    atm_iv = float(atm.get("iv") or 0)
    ctx["atm_iv"] = round(atm_iv, 4) if atm_iv else None
    if _tech is None or not atm_iv:
        return ctx
    try:
        px = fetcher.get_price_data(ticker, period="2y")
        if px is None or px.empty:
            return ctx
        close = px["Close"]
        ivr = _tech.calculate_iv_rank(ticker, atm_iv, close)
        ctx["iv_rank"] = ivr.get("iv_rank")
        ctx["iv_rank_method"] = ivr.get("iv_rank_method")
        rv = _tech._historical_vol(close, getattr(config, "VRP_HV_WINDOW", 35))
        if rv:
            ctx["rv"] = round(float(rv), 4)
            ctx["vrp_pp"] = round((atm_iv - float(rv)) * 100, 1)  # vol points
    except Exception as e:
        logger.debug(f"[{ticker}] vol_context failed: {e}")
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Spread construction
# ─────────────────────────────────────────────────────────────────────────────
def otm_buffer_ok(ticker: str, current_price: float, short_strike: float) -> bool:
    dist = current_price - short_strike
    if dist <= 0:
        return False
    if ticker in SPY_LIKE:
        return dist >= getattr(config, "MIN_STRIKE_BUFFER_SPY", 10.0)
    return (dist / current_price) >= getattr(config, "MIN_STRIKE_BUFFER_STOCK", 0.05)


def liquidity_ok(opt: dict) -> bool:
    vol = int(opt.get("volume") or 0)
    oi = int(opt.get("open_interest") or 0)
    return vol >= getattr(config, "MIN_OPTION_VOLUME", 100) or oi >= getattr(config, "MIN_OPTION_OPEN_INTEREST", 500)


def build_candidates(ticker: str, puts: list, current_price: float,
                     delta_min: float, delta_max: float, max_width: float) -> list:
    """Enumerate real bull-put spreads for one ticker. No gate filtering — annotate only."""
    cands = []
    # group by expiration
    puts = [p for p in puts if p.get("strike") and p.get("mid")]
    puts.sort(key=lambda o: (o["expiration"], o["strike"]))
    for exp, grp in groupby(puts, key=lambda o: o["expiration"]):
        legs = list(grp)
        by_strike = {round(o["strike"], 2): o for o in legs}
        # short-leg candidates: OTM puts within the delta band
        for short in legs:
            if short["strike"] >= current_price:
                continue
            d = abs(float(short.get("delta") or 0))
            if d == 0 or not (delta_min <= d <= delta_max):
                continue
            dte = short["dte"]
            # find the best long leg below the short within max_width
            best = None
            for long_strike in sorted((s for s in by_strike if s < short["strike"]), reverse=True):
                width = round(short["strike"] - long_strike, 2)
                if width <= 0 or width > max_width:
                    continue
                long_opt = by_strike[long_strike]
                # VEGA-modeled credit (mids) and conservative natural credit (sell bid / buy ask)
                if calculate_spread_metrics:
                    m = calculate_spread_metrics(short, long_strike, current_price, long_put_mid=long_opt.get("mid"))
                    if not m or m.get("spread_invalid"):
                        continue
                    credit = m["credit_per_share"]
                    credit_usd = m["credit_usd"]
                    max_loss = m["max_loss_usd"]
                else:
                    credit = round(short["mid"] - long_opt["mid"], 2)
                    if credit <= 0 or credit >= width:
                        continue
                    credit_usd = round(credit * 100, 2)
                    max_loss = round((width - credit) * 100, 2)
                natural = round(float(short.get("bid") or 0) - float(long_opt.get("ask") or 0), 2)
                ctw = round(credit / width, 3) if width else 0
                breakeven = round(short["strike"] - credit, 2)
                pop_implied = round(1 - d, 3)
                roi = round(credit_usd / max_loss, 3) if max_loss > 0 else None
                cand = {
                    "ticker": ticker, "expiration": exp, "dte": dte,
                    "short_strike": short["strike"], "long_strike": long_strike, "width": width,
                    "short_bid": short.get("bid"), "short_ask": short.get("ask"), "short_mid": short.get("mid"),
                    "long_bid": long_opt.get("bid"), "long_ask": long_opt.get("ask"), "long_mid": long_opt.get("mid"),
                    "credit_per_share": credit, "credit_usd": credit_usd, "natural_credit_per_share": natural,
                    "width_usd": round(width * 100, 2), "max_loss_usd": max_loss,
                    "credit_to_width": ctw, "breakeven": breakeven, "roi": roi,
                    "short_delta": round(float(short.get("delta") or 0), 3), "pop_implied": pop_implied,
                    "short_iv": round(float(short.get("iv") or 0), 4),
                    "short_volume": int(short.get("volume") or 0), "short_oi": int(short.get("open_interest") or 0),
                }
                # keep the widest credit/width per short leg (best premium efficiency)
                if best is None or ctw > best["credit_to_width"]:
                    best = cand
            if best:
                # gate annotations (what the strict scanner checks)
                g = {
                    "delta_cap": abs(best["short_delta"]) <= getattr(config, "SHORT_STRIKE_MAX_DELTA", 0.30),
                    "otm_buffer": otm_buffer_ok(ticker, current_price, best["short_strike"]),
                    "credit_to_width": best["credit_to_width"] >= getattr(config, "MIN_CREDIT_TO_WIDTH_PCT", 0.15),
                    "min_credit_usd": best["credit_usd"] >= getattr(config, "MIN_CREDIT_USD", 25),
                    "liquidity": liquidity_ok(short),
                    "pop": best["pop_implied"] >= getattr(config, "MIN_PROBABILITY_OF_PROFIT", 0.72),
                    "dte_window": getattr(config, "MIN_DTE", 21) <= best["dte"] <= getattr(config, "MAX_DTE", 45),
                }
                best["gates"] = g
                best["gates_passed"] = sum(1 for v in g.values() if v)
                best["gates_total"] = len(g)
                # simple ranking score: reward premium efficiency + delta near 0.20 target + liquidity
                target_d = getattr(config, "SHORT_STRIKE_TARGET_DELTA", 0.20)
                best["score"] = round(
                    best["credit_to_width"] * 100
                    - abs(abs(best["short_delta"]) - target_d) * 100
                    + (5 if g["liquidity"] else 0),
                    2,
                )
                cands.append(best)
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────
def pct(x):
    return f"{x*100:.0f}%" if isinstance(x, (int, float)) else "—"


def render_html(rows: list, meta: dict) -> str:
    def gate_chip(name, ok):
        return f'<span class="chip {"ok" if ok else "no"}">{name}</span>'

    ticker_blocks = []
    for tk in rows:
        ctx = tk["ctx"]
        ivr = ctx.get("iv_rank")
        ivr_txt = f'{ivr:.0f} <span class="dim">({ctx.get("iv_rank_method","?")})</span>' if ivr is not None else "—"
        vrp = ctx.get("vrp_pp")
        vrp_txt = f'{vrp:+.1f}pp' if vrp is not None else "—"
        atm = ctx.get("atm_iv")
        atm_txt = f'{atm*100:.1f}%' if atm else "—"
        if not tk["candidates"]:
            ticker_blocks.append(
                f'<div class="tk"><div class="tkhead"><b>{tk["ticker"]}</b> '
                f'<span class="px">${tk["price"]:.2f}</span> '
                f'<span class="dim">ATM IV {atm_txt} · IV-Rank {ivr_txt} · VRP {vrp_txt}</span></div>'
                f'<div class="empty">No spreads found in the delta band / DTE window '
                f'(thin chain or no OTM puts priced).</div></div>'
            )
            continue
        trs = []
        for c in tk["candidates"]:
            g = c["gates"]
            chips = "".join([
                gate_chip("IV-Rank", (ivr is not None and ivr >= getattr(config, "MIN_IV_RANK", 45))),
                gate_chip("Δ≤.30", g["delta_cap"]),
                gate_chip("buffer", g["otm_buffer"]),
                gate_chip("cr/w≥.15", g["credit_to_width"]),
                gate_chip("liq", g["liquidity"]),
                gate_chip("POP≥72", g["pop"]),
            ])
            allpass = "allpass" if (c["gates_passed"] == c["gates_total"] and ivr is not None and ivr >= getattr(config, "MIN_IV_RANK", 45)) else ""
            trs.append(
                f'<tr class="{allpass}">'
                f'<td><b>{c["short_strike"]:g}</b>/<b>{c["long_strike"]:g}</b><div class="dim">w {c["width"]:g}</div></td>'
                f'<td>{c["expiration"]}<div class="dim">{c["dte"]}d</div></td>'
                f'<td>${c["credit_per_share"]:.2f}<div class="dim">nat ${c["natural_credit_per_share"]:.2f}</div></td>'
                f'<td>${c["credit_usd"]:.0f}</td>'
                f'<td>${c["max_loss_usd"]:.0f}</td>'
                f'<td>{pct(c["credit_to_width"])}</td>'
                f'<td>{c["short_delta"]:.2f}<div class="dim">POP {pct(c["pop_implied"])}</div></td>'
                f'<td>${c["breakeven"]:.2f}</td>'
                f'<td>{pct(c["roi"]) if c["roi"] is not None else "—"}</td>'
                f'<td class="dim">{c["short_bid"]:.2f}/{c["short_ask"]:.2f} · {c["long_bid"]:.2f}/{c["long_ask"]:.2f}</td>'
                f'<td>{chips}</td>'
                f'</tr>'
            )
        ticker_blocks.append(
            f'<div class="tk"><div class="tkhead"><b>{tk["ticker"]}</b> '
            f'<span class="px">${tk["price"]:.2f}</span> '
            f'<span class="dim">ATM IV {atm_txt} · IV-Rank {ivr_txt} · VRP {vrp_txt}</span></div>'
            f'<table><thead><tr>'
            f'<th>Short/Long</th><th>Exp</th><th>Credit/sh</th><th>Credit</th><th>Max loss</th>'
            f'<th>Cr/W</th><th>Δ short</th><th>Break-even</th><th>ROI</th>'
            f'<th>Short b/a · Long b/a</th><th>Gates</th>'
            f'</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'
        )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>VEGA Candidates — {meta['stamp']}</title>
<style>
 body{{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1a1a1a;background:#fafafa}}
 h1{{font-size:20px;margin:0 0 4px}}
 .sub{{color:#666;margin-bottom:16px}}
 .banner{{background:#fff8e1;border:1px solid #ffe082;padding:10px 14px;border-radius:8px;margin-bottom:18px;font-size:13px}}
 .tk{{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:14px 16px;margin-bottom:16px}}
 .tkhead{{font-size:16px;margin-bottom:8px}}
 .px{{color:#0a7d32;font-weight:600;margin:0 6px}}
 .dim{{color:#888;font-size:11px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;color:#555;font-weight:600;border-bottom:2px solid #eee;padding:6px 8px}}
 td{{padding:6px 8px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
 tr.allpass{{background:#f0fbf3}}
 .empty{{color:#999;font-style:italic;padding:8px 0}}
 .chip{{display:inline-block;font-size:10px;padding:2px 5px;border-radius:4px;margin:1px}}
 .chip.ok{{background:#e3f5e9;color:#0a7d32}}
 .chip.no{{background:#fdecec;color:#c0392b}}
 .foot{{color:#888;font-size:11px;margin-top:18px;line-height:1.5}}
</style></head><body>
<h1>VEGA — Real Spread Candidates</h1>
<div class="sub">{meta['stamp']} · source: {meta['source']} (~15-min delayed) · DTE {meta['min_dte']}–{meta['max_dte']} · Δ band {meta['delta_min']:.2f}–{meta['delta_max']:.2f} · VIX {meta['vix']}</div>
<div class="banner"><b>Validation view — not the disciplined scanner.</b> These are the best REAL bull-put spreads
that exist right now, shown even when they fail the strict gates. A green row passes every gate (including
IV-Rank≥{getattr(config,'MIN_IV_RANK',45)}); red chips show which gate a row fails. Credit uses mids (what VEGA models);
"nat" is the conservative sell-bid/buy-ask fill. Verify against your broker before trading.</div>
{''.join(ticker_blocks)}
<div class="foot">
 Gate legend: <b>IV-Rank</b> current IV percentile ≥ {getattr(config,'MIN_IV_RANK',45)} (APPROX until ~30 samples accrue) ·
 <b>Δ≤.30</b> short-strike delta cap · <b>buffer</b> OTM distance floor ·
 <b>cr/w≥.15</b> credit-to-width floor · <b>liq</b> volume≥{getattr(config,'MIN_OPTION_VOLUME',100)} or OI≥{getattr(config,'MIN_OPTION_OPEN_INTEREST',500)} ·
 <b>POP≥72</b> implied prob. of profit (1−|Δ|).<br>
 Not financial advice. Educational screener. Every order is your own.
</div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="VEGA real bull-put spread candidate viewer")
    ap.add_argument("--min-dte", type=int, default=25)
    ap.add_argument("--max-dte", type=int, default=45)
    ap.add_argument("--delta-min", type=float, default=0.12)
    ap.add_argument("--delta-max", type=float, default=0.35)
    ap.add_argument("--max-width", type=float, default=float(getattr(config, "MAX_SPREAD_WIDTH", 5)))
    ap.add_argument("--top", type=int, default=3, help="max candidates shown per ticker")
    ap.add_argument("--tickers", type=str, default="", help="comma list to override the watchlist")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    fetcher.clear_cache()
    if args.tickers.strip():
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = [w["ticker"] for w in config.WATCHLIST]

    try:
        vix = fetcher.get_vix().get("current", "—")
    except Exception:
        vix = "—"

    rows = []
    total_pass = 0
    print(f"\nVEGA candidates · DTE {args.min_dte}-{args.max_dte} · Δ {args.delta_min}-{args.delta_max} · {len(tickers)} tickers\n")
    for tk in tickers:
        try:
            px = fetcher.get_price_data(tk, period="5d")
            price = float(px["Close"].iloc[-1]) if px is not None and not px.empty else None
            if not price:
                print(f"  {tk:5s}  no price — skipped")
                continue
            puts = fetcher.get_options_chain(tk, args.min_dte, args.max_dte)
            ctx = vol_context(tk, puts, price)
            cands = build_candidates(tk, puts, price, args.delta_min, args.delta_max, args.max_width)[: args.top]
            rows.append({"ticker": tk, "price": price, "ctx": ctx, "candidates": cands})
            best = cands[0] if cands else None
            npass = sum(1 for c in cands if c["gates_passed"] == c["gates_total"])
            total_pass += npass
            if best:
                print(f"  {tk:5s}  ${price:8.2f}  best {best['short_strike']:g}/{best['long_strike']:g} "
                      f"{best['expiration']} ({best['dte']}d)  cr ${best['credit_usd']:.0f} "
                      f"cr/w {best['credit_to_width']*100:.0f}%  Δ{best['short_delta']:.2f}  "
                      f"gates {best['gates_passed']}/{best['gates_total']}")
            else:
                print(f"  {tk:5s}  ${price:8.2f}  no candidates in band/DTE")
        except Exception as e:
            print(f"  {tk:5s}  ERROR {e}")
            logger.debug("error", exc_info=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    meta = {
        "stamp": datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip(),
        "source": "yfinance" if not getattr(config, "POLYGON_API_KEY", "") else "polygon",
        "min_dte": args.min_dte, "max_dte": args.max_dte,
        "delta_min": args.delta_min, "delta_max": args.delta_max, "vix": vix,
    }
    out_dir = Path(__file__).parent / "output" / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"candidates_{stamp}.html"
    json_path = out_dir / f"candidates_{stamp}.json"
    html_path.write_text(render_html(rows, meta), encoding="utf-8")
    json_path.write_text(json.dumps({"meta": meta, "rows": rows}, indent=2, default=str), encoding="utf-8")

    print(f"\n  {total_pass} candidate(s) pass ALL gates.  Report: {html_path}\n")
    if not args.no_open:
        try:
            webbrowser.open(html_path.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
