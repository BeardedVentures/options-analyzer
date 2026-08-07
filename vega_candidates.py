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
from analysis import edge_calculator

_ETF_TICKERS = {w["ticker"] for w in getattr(config, "WATCHLIST", []) if w.get("type") == "ETF"}

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


def _earnings_clear(earnings_date, expiration: str, is_etf: bool) -> bool:
    """True when no earnings print falls inside the position's lifetime.

    Selling a 25-45 DTE credit spread through an earnings report converts a probabilistic edge
    into a binary event bet — the single largest uncontrolled risk in premium selling, and the
    reason config.EARNINGS_BLACKOUT_DAYS exists. That knob was defined but enforced nowhere on
    the auto-open path (the fourth leak of this shape).

    Any earnings on or before expiry disqualifies, which is stricter than the 7-day blackout and
    the correct test for a position held to expiration.

    Fails CLOSED for a non-ETF with no known earnings date: a missing date is a data gap, and
    skipping a tradeable name for one cycle costs far less than selling premium into a print.
    ETFs have no earnings and always pass.
    """
    if is_etf:
        return True
    if earnings_date is None:
        return False
    try:
        exp = datetime.strptime(str(expiration)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    e = earnings_date.date() if hasattr(earnings_date, "date") else earnings_date
    try:
        return e > exp
    except TypeError:
        return False


def attach_earnings_gate(cands: list, ticker: str, earnings_date, is_etf: bool) -> int:
    """Annotate the earnings gate and refresh gate counts. Returns candidates blocked."""
    blocked = 0
    for c in cands:
        clear = _earnings_clear(earnings_date, c.get("expiration"), is_etf)
        c["earnings_date"] = (
            (earnings_date.date() if hasattr(earnings_date, "date") else earnings_date).isoformat()
            if earnings_date else None
        )
        c["gates"]["earnings_clear"] = clear
        # build_candidates computed these before this gate existed; keep them truthful.
        c["gates_passed"] = sum(1 for v in c["gates"].values() if v)
        c["gates_total"] = len(c["gates"])
        if not clear:
            blocked += 1
    return blocked


def _leg_spread_pct(bid, ask) -> float:
    """Relative bid-ask spread of one leg. Returns inf when unquotable, so it fails the gate."""
    try:
        b, a = float(bid or 0), float(ask or 0)
    except (TypeError, ValueError):
        return float("inf")
    mid = (a + b) / 2
    if mid <= 0 or a <= 0:
        return float("inf")
    return (a - b) / mid


def _quote_spread_ok(c: dict) -> bool:
    """Both legs must quote inside MAX_QUOTE_SPREAD_PCT. Checked per leg, not on the spread."""
    cap = float(getattr(config, "MAX_QUOTE_SPREAD_PCT", 0.35))
    return (_leg_spread_pct(c.get("short_bid"), c.get("short_ask")) <= cap
            and _leg_spread_pct(c.get("long_bid"), c.get("long_ask")) <= cap)


def attach_true_pop(cands: list, current_price: float, prices_hist) -> int:
    """Attach the engine's drift-removed POP to fast-scan candidates.

    Without this the candidates JSON carries only pop_implied (1 - |delta|), so every
    auto-paper trade opened from a snapshot graded against a delta proxy instead of the
    calibrated signal — `true_pop` was null on all 59 ledger records. Mirrors main.py:608-609
    exactly: true_pop = P(price > breakeven), p_max_profit = P(price > short strike).

    Returns how many candidates were successfully annotated. Never raises: a history gap must
    degrade the scan to implied-only, not kill it.
    """
    if prices_hist is None or len(prices_hist) < 2 or not current_price:
        return 0
    n = 0
    for c in cands:
        try:
            be_distance_pct = (current_price - c["breakeven"]) / current_price
            otm_distance_pct = (current_price - c["short_strike"]) / current_price
            dte = int(c.get("dte") or 0)
            if dte <= 0:
                continue
            p_profit = edge_calculator.calculate_true_pop(
                strike_distance_pct=be_distance_pct,
                expiration_days=dte,
                historical_prices=prices_hist,
            )
            p_maxprofit = edge_calculator.calculate_true_pop(
                strike_distance_pct=otm_distance_pct,
                expiration_days=dte,
                historical_prices=prices_hist,
            )
            c["true_pop"] = p_profit.get("true_pop")
            c["true_pop_confidence"] = p_profit.get("confidence")
            c["true_pop_drift_mode"] = p_profit.get("drift_mode")
            c["p_max_profit"] = p_maxprofit.get("true_pop")
            if c["true_pop"] is not None:
                n += 1
        except Exception:
            logger.debug("true_pop failed for %s", c.get("ticker"), exc_info=True)
    return n


def _assess_candidate(spread: dict, ctx: dict) -> dict:
    """Delegate to the shared assessment. Degrades to gates-only if the analysis layer is
    unavailable — a scan must never fail because an advisory signal did."""
    from analysis import assessment as A
    try:
        return A.assess(spread, ctx, strategy=A.BULL_PUT)
    except Exception as e:
        logger_msg = f"[candidates] assessment failed for {ctx.get('ticker')}: {e}"
        print("  " + logger_msg)
        return {"gates": A.evaluate_gates(spread, ctx), "analysis": {}, "narrative": ""}


def _shelter_ok(short_strike, support_levels) -> bool:
    """True when a real support level stands above the short strike.

    Fails OPEN when levels are unavailable (too little history, fetch failure): a data gap
    must not silently reject every candidate the way the earnings gate's fail-closed design
    could. When SUPPORT_SHELTER_GATE_ENABLED is off this always passes.
    """
    if not getattr(config, "SUPPORT_SHELTER_GATE_ENABLED", True):
        return True
    if not support_levels:
        return True
    try:
        from analysis.levels import strike_cushion
        return strike_cushion(float(short_strike), support_levels, "put",
                              min_buffer_pct=float(getattr(config, "LEVEL_MIN_BUFFER_PCT", 0.005))) is not None
    except Exception:
        return True


def build_candidates(ticker: str, puts: list, current_price: float,
                     delta_min: float, delta_max: float, max_width: float,
                     support_levels: list = None, assess_ctx: dict = None) -> list:
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
                # DECIDE ON THE BASIS YOU EXECUTE ON. credit/credit_usd/ctw above are MID
                # values, but auto_paper_cycle opens at `natural`. Gating and ranking on mid
                # while filling at natural is the same gate/execution mismatch REQUIRED_GATES
                # exists to prevent, and it is what let GDX 82/81 open twice on 2026-08-07
                # for $9 and $7 of credit against a $25 minimum: mid said $31 and $29. Both
                # were closed by the wolf floor within the same cycle for -$45.16 each,
                # because a $7 credit on a $1-wide spread is dead at inception.
                natural_usd = round(natural * 100, 2)
                natural_ctw = round(natural / width, 3) if width else 0
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
                    "natural_credit_usd": natural_usd, "natural_credit_to_width": natural_ctw,
                    "width_usd": round(width * 100, 2), "max_loss_usd": max_loss,
                    "credit_to_width": ctw, "breakeven": breakeven, "roi": roi,
                    "short_delta": round(float(short.get("delta") or 0), 3), "pop_implied": pop_implied,
                    "short_iv": round(float(short.get("iv") or 0), 4),
                    "short_theta": round(float(short.get("theta") or 0), 4),
                    "short_volume": int(short.get("volume") or 0), "short_oi": int(short.get("open_interest") or 0),
                }
                # keep the widest credit/width per short leg (best premium efficiency)
                # Rank on the natural basis too — picking the best mid spread and then
                # gating the natural one can discard a pair that would have qualified.
                if best is None or natural_ctw > best["natural_credit_to_width"]:
                    best = cand
            if best:
                # ONE definition of the gates. This block previously re-implemented the same
                # config constants that main.py also checked, which is the shape behind four
                # enforcement leaks (IV rank, POP floor, quote spread, mid-vs-natural credit).
                # analysis/assessment.py owns the contract now; adding a rule there enforces
                # it on both the cockpit and the auto-trader by construction.
                best["side"] = "put"
                best["short_leg"] = short
                best["pop"] = best["pop_implied"]
                # Gate first — cheap. Only a candidate that survives earns the expensive
                # surface read and the full narrative. Analysis follows selection.
                from analysis import assessment as _A2
                g = _A2.evaluate_gates(best, assess_ctx)
                if all(g.values()):
                    _A2.enrich_surface(assess_ctx)
                    _asmt = _assess_candidate(best, assess_ctx)
                    g = _asmt["gates"]
                    best["analysis"] = _asmt["analysis"]
                    best["narrative"] = _asmt["narrative"]
                else:
                    best["analysis"] = {}
                    best["narrative"] = ("Blocked by "
                                         + ", ".join(k for k, v in g.items() if not v) + ".")
                best.pop("short_leg", None)   # not JSON-serialisable and already summarised
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
        ivm = (ctx.get("iv_rank_method") or "?").upper()
        ivr_tag = "Est" if ivm == "APPROX" else "Hist"
        ivr_txt = f'{ivr:.0f} <span class="dim">({ivr_tag})</span>' if ivr is not None else "—"
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
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the HTML file in a browser after saving.")
    ap.add_argument("--no-html", action="store_true",
                    help="Skip writing the HTML file; only write the JSON snapshot. "
                         "Used by auto_paper_cycle.py to avoid accumulating large HTML files "
                         "on every scheduled run.")
    ap.add_argument("--no-save", action="store_true",
                    help="Skip writing both HTML and JSON output files entirely. "
                         "Implies --no-open. Useful for interactive/validation runs where "
                         "you only want the console summary.")
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
            # ONE history fetch per ticker, 2y — reused by the level detector AND the
            # calibrated-POP pass below, which used to pull its own 2y series separately.
            px = fetcher.get_price_data(tk, period="2y")
            price = float(px["Close"].iloc[-1]) if px is not None and not px.empty else None
            if not price:
                print(f"  {tk:5s}  no price — skipped")
                continue
            puts = fetcher.get_options_chain(tk, args.min_dte, args.max_dte)
            ctx = vol_context(tk, puts, price)
            # ONE market context per ticker, shared by every spread on it. Previously each
            # path re-fetched price history and chains independently, per scan.
            # GATE PHASE ONLY — price history (for levels) plus the chain already in hand.
            # The volatility surface is deliberately not loaded here: it gates nothing and
            # costs two more chain fetches per ticker. Survivors get it below.
            from analysis import assessment as _A
            # Earnings resolved BEFORE the build so the shared gate owns it. It was applied
            # after build_candidates, which meant a candidate could pass every gate, earn the
            # expensive surface read, and only then be rejected for an earnings date — three
            # wasted enrichments on CRWD alone in the 17:18 scan.
            _is_etf = tk in _ETF_TICKERS
            _earn_days = None
            if getattr(config, "EARNINGS_GATE_ENABLED", True) and not _is_etf:
                try:
                    from data import fundamentals as _fund
                    _earn_days = _fund.days_until_earnings(fetcher.get_earnings_date(tk))
                except Exception:
                    logger.debug("earnings lookup failed for %s", tk, exc_info=True)
            assess_ctx = _A.load_context(tk, price_data=px, puts=puts, tech=ctx,
                                         earnings_days=_earn_days)
            _sup = (assess_ctx.get("levels") or {}).get("support_levels")
            cands = build_candidates(tk, puts, price, args.delta_min, args.delta_max,
                                     args.max_width, support_levels=_sup,
                                     assess_ctx=assess_ctx)[: args.top]

            # Earnings gate — one calendar lookup per ticker, reused across its candidates.
            n_earn_blocked = 0
            # earnings_clear is set by analysis.assessment.evaluate_gates during the build.
            n_earn_blocked = sum(1 for _c in cands
                                 if not (_c.get("gates") or {}).get("earnings_clear", True))

            # Calibrated POP for the fast path. Needs a long history (the 5d price fetch above
            # is only for the last close), so pull it once per ticker and reuse for every leg.
            n_tp = 0
            if cands:
                try:
                    n_tp = attach_true_pop(cands, price,
                                           px["Close"] if px is not None and not px.empty else None)
                except Exception:
                    logger.debug("history fetch failed for %s", tk, exc_info=True)

            rows.append({"ticker": tk, "price": price, "ctx": ctx, "candidates": cands})
            best = cands[0] if cands else None
            npass = sum(1 for c in cands if c["gates_passed"] == c["gates_total"])
            total_pass += npass
            if best:
                tp = best.get("true_pop")
                tp_s = f"  tPOP {tp*100:.0f}%" if tp is not None else "  tPOP —"
                if n_earn_blocked:
                    tp_s += f"  [EARN blocks {n_earn_blocked}/{len(cands)}]"
                print(f"  {tk:5s}  ${price:8.2f}  best {best['short_strike']:g}/{best['long_strike']:g} "
                      f"{best['expiration']} ({best['dte']}d)  cr ${best['credit_usd']:.0f} "
                      f"cr/w {best['credit_to_width']*100:.0f}%  Δ{best['short_delta']:.2f}{tp_s}  "
                      f"gates {best['gates_passed']}/{best['gates_total']}  [tpop {n_tp}/{len(cands)}]")
            else:
                # A bare "no candidates" line is undiagnosable after the fact: on 2026-07-31
                # 08:36 all 50 tickers returned zero and the snapshot recorded no reason why.
                # Report where the chain actually died so the next occurrence is readable.
                _mid = [p for p in puts if p.get("strike") and p.get("mid")]
                _otm = [p for p in _mid if p["strike"] < price]
                _dlt = [p for p in _otm if abs(float(p.get("delta") or 0)) > 0]
                _band = [p for p in _dlt if args.delta_min <= abs(float(p["delta"])) <= args.delta_max]
                print(f"  {tk:5s}  ${price:8.2f}  no candidates — chain {len(puts)} "
                      f"→ priced {len(_mid)} → OTM {len(_otm)} → with-Δ {len(_dlt)} "
                      f"→ in Δ band {args.delta_min:.2f}-{args.delta_max:.2f} {len(_band)}")
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

    # --no-save skips all file output (console-only run).
    # --no-html writes only the JSON (for auto_paper_cycle.py consumption).
    no_save = args.no_save
    no_html = args.no_html or no_save

    html_path = None
    json_path = None

    if not no_save:
        out_dir = Path(__file__).parent / "output" / "candidates"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"candidates_{stamp}.json"
        json_path.write_text(
            json.dumps({"meta": meta, "rows": rows}, indent=2, default=str), encoding="utf-8"
        )
        if not no_html:
            html_path = out_dir / f"candidates_{stamp}.html"
            html_path.write_text(render_html(rows, meta), encoding="utf-8")

    if html_path:
        print(f"\n  {total_pass} candidate(s) pass ALL gates.  Report: {html_path}\n")
    elif json_path:
        print(f"\n  {total_pass} candidate(s) pass ALL gates.  JSON: {json_path}\n")
    else:
        print(f"\n  {total_pass} candidate(s) pass ALL gates.  (no files saved)\n")

    if not (args.no_open or no_save) and html_path:
        try:
            webbrowser.open(html_path.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
