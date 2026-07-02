#!/usr/bin/env python3
"""
smoke_test_data.py — Gate 1 readiness check (VEGA)

Confirms the FREE data path (yfinance, since no paid Polygon options plan)
returns usable options chains for the watchlist, so trade-outcome logging
can begin on trustworthy inputs. Run locally where network + deps exist:

    python smoke_test_data.py

For each watchlist ticker it calls the real data/fetcher.get_options_chain()
and reports whether a chain came back with the fields the scanner needs:
strike, bid/ask, volume, open interest, IV, delta, and DTE 21-45 coverage.
"""
import sys
import config
from data import fetcher

MIN_DTE = getattr(config, "MIN_DTE", 21)
MAX_DTE = getattr(config, "MAX_DTE", 45)

def g(rec, *keys):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return 0

def tickers():
    wl = getattr(config, "WATCHLIST", [])
    out = []
    for w in wl:
        out.append(w["ticker"].upper() if isinstance(w, dict) else str(w).upper())
    return out

def assess(tk):
    try:
        chain = fetcher.get_options_chain(tk, MIN_DTE, MAX_DTE)
    except Exception as e:
        return ("FAIL", f"exception: {type(e).__name__}: {e}", {})
    n = len(chain) if chain else 0
    if n == 0:
        return ("FAIL", "no contracts returned", {})
    with_quote = sum(1 for r in chain if float(g(r,"bid")) > 0 and float(g(r,"ask")) > 0)
    with_vol   = sum(1 for r in chain if int(float(g(r,"volume"))) > 0)
    with_oi    = sum(1 for r in chain if int(float(g(r,"open_interest","oi"))) > 0)
    with_iv    = sum(1 for r in chain if float(g(r,"iv","implied_volatility")) > 0)
    with_delta = sum(1 for r in chain if abs(float(g(r,"delta"))) > 0)
    dtes = sorted({int(float(g(r,"dte"))) for r in chain if g(r,"dte")})
    in_band = [d for d in dtes if MIN_DTE <= d <= MAX_DTE]
    # a tradable candidate needs a live quote AND a target-delta put in-band
    target_delta = [r for r in chain
                    if 0.15 <= abs(float(g(r,"delta"))) <= 0.32
                    and float(g(r,"bid")) > 0 and float(g(r,"ask")) > 0
                    and MIN_DTE <= int(float(g(r,"dte"))) <= MAX_DTE]
    stats = dict(n=n, quote=with_quote, vol=with_vol, oi=with_oi, iv=with_iv,
                 delta=with_delta, dte_band=len(in_band), candidates=len(target_delta))
    if not in_band or with_quote == 0:
        return ("FAIL", "no live-quote contracts in DTE band", stats)
    if not target_delta or with_iv == 0 or with_delta == 0:
        return ("WARN", "chain present but thin (no target-delta candidate, or IV/delta missing)", stats)
    return ("PASS", f"{len(target_delta)} target-delta candidate(s) in {MIN_DTE}-{MAX_DTE} DTE", stats)

def main():
    tks = tickers()
    print(f"VEGA data smoke test — {len(tks)} tickers, DTE {MIN_DTE}-{MAX_DTE}, source=fetcher (yfinance unless POLYGON key set)\n")
    print(f"{'TICKER':<7}{'RESULT':<7}{'#opt':>5}{'quote':>7}{'vol':>6}{'OI':>7}{'IV':>5}{'delta':>7}{'cand':>6}  note")
    print("-"*92)
    tally = {"PASS":0,"WARN":0,"FAIL":0}
    for tk in tks:
        res, note, s = assess(tk)
        tally[res]+=1
        if s:
            print(f"{tk:<7}{res:<7}{s['n']:>5}{s['quote']:>7}{s['vol']:>6}{s['oi']:>7}{s['iv']:>5}{s['delta']:>7}{s['candidates']:>6}  {note}")
        else:
            print(f"{tk:<7}{res:<7}{'-':>5}{'-':>7}{'-':>6}{'-':>7}{'-':>5}{'-':>7}{'-':>6}  {note}")
    print("-"*92)
    print(f"PASS {tally['PASS']}   WARN {tally['WARN']}   FAIL {tally['FAIL']}")
    print("\nReading it: PASS = tradable candidates exist on free data. WARN = chain returns but")
    print("thin/missing Greeks (Black-Scholes fallback may fill deltas). FAIL for many tickers")
    print("means the free source can't support Gate 1 — consider Polygon Options Starter (paid).")
    sys.exit(0 if tally["FAIL"]==0 else 1)

if __name__ == "__main__":
    main()
