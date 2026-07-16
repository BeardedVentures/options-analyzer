# VEGA Multi-Strategy Engine Spec — bear-call, iron-condor, lottery

**Date:** 2026-07-16
**Status:** cockpit rendering DONE + verified; engine emission = TODO on the tower (needs live yfinance).

## What's already done (cockpit — verified with synthetic data)

`vega_app.py` is now **strategy-aware**. It renders any of these from `logs/scan_latest.json` with correct
geometry, breakevens, cushion, EV, reconcile, payoff, and a type chip (BPS / BCS / IC):

| strategy | `strategy` string contains | strikes it reads | breakeven(s) | risk dir |
|---|---|---|---|---|
| Bull put spread | (default) | `short_strike`,`long_strike` (puts) | short − credit | downside |
| Bear call spread | "bear" + "call" | `short_strike`,`long_strike` (calls) | short + credit | upside |
| Iron condor | "condor" | `put_short_strike`,`put_long_strike`,`call_short_strike`,`call_long_strike` | put_short − credit **and** call_short + credit | both |

All three reconcile clean and compute EV = `true_pop·max_profit − (1−true_pop)·max_loss` (net of round-trip cost).
**Lottery (single long calls)** is a separate surface reading `logs/lottery_latest.json`, populated by
`lottery_scanner.py` — deliberately NOT in the defined-risk board.

So the moment `main.py` emits these objects, the cockpit shows them correctly. No further UI work needed.

## Engine work to do in `main.py` (test on the tower)

The engine currently generates **bull-put spreads only**. Add two generators, mirroring the existing one.
Keep every hard gate (edge score, POP, liquidity, delta cap, credit/width). **Defined-risk only — no naked/undefined.**

### 1. Bear call spread (bearish / overbought fade)
- Pull the **call** chain (not puts): needs `fetcher.get_call_chain(ticker, min_dte, max_dte)` (add if missing, symmetric to the put fetch).
- Short a call above price at target delta ≈ 0.20–0.30; buy the next call `MAX_SPREAD_WIDTH` higher.
- `credit = short.mid − long.mid`; `max_loss_usd = width·100 − credit_usd`; `breakeven = short + credit`.
- `true_pop` = P(price **below** short strike at expiry) — reuse `calculate_true_pop` with the direction flipped (probability of staying under, not over).
- Emit with `"strategy": "Bear Call Spread"` and the same field names the bull-put emits (`short_strike`,`long_strike`,`credit_per_share`,`credit_usd`,`max_loss_usd`,`delta`,`true_pop`,`implied_pop`,`edge_score`,`component_breakdown`,`vrp`, news/technical fields).
- **Regime gate:** only surface when the trend/technical read is neutral-to-bearish (don't sell calls into a strong uptrend).

### 2. Iron condor (range-bound / balanced)
- Combine a bull-put spread **and** a bear-call spread on the same expiry.
- Emit the four strikes: `put_short_strike`,`put_long_strike`,`call_short_strike`,`call_long_strike`.
- `credit_usd` = both spreads' credit; `max_loss_usd = max(put_width, call_width)·100 − credit_usd`.
- `true_pop` = P(price **between** the two short strikes at expiry).
- `"strategy": "Iron Condor"`.
- **Regime gate:** only when IV is elevated AND trend is flat/range-bound (both sides need premium and neither side trending through).

### 3. Persist
`write_scan_latest()` already serializes `qualified_trades` — no change needed; the new objects flow straight through to the cockpit. Just make sure the sector-cap / ranking sort in `main.py` treats all strategies together by `edge_score`.

## Lottery scanner (`lottery_scanner.py` — already written)
Standalone, separate from the income engine. Surfaces single long calls **only** in specific conditions
(momentum breakout: RSI 55–70 above SMA20/50 with non-negative news; or oversold-at-support bounce). Writes
`logs/lottery_latest.json`. Run: `python lottery_scanner.py` (live) or `--demo` (synthetic sample to preview the view),
`--budget N` to cap premium/contract. It always shows the true (low) probability, the required move, and that
max loss = 100% of premium. **Not a positive-edge trade — a capped-cost swing.** Add it to the launcher only if you
want the Lottery view populated on every start.

## Data-accuracy guardrails (already live)
- Every displayed number is recomputed from primitives per strategy type via `_reconcile()`; each row carries a ✓/⚠ badge.
- `verify_numbers.py` cross-checks the whole artifact + freshness (exit 1 on failure/stale) — run it in the launcher or CI before trusting a board.
- The board stamps data as-of / minutes-old and warns outside the ~15-min feed window.

## Test plan on the tower
1. `python lottery_scanner.py --demo` → open the cockpit → confirm the **Lottery** tab shows the two demo cards.
2. Implement generators, run `python main.py` on a day with live chains → confirm `scan_latest.json` contains
   bear-call and/or iron-condor objects.
3. `python verify_numbers.py` → expect all rows reconcile, data fresh.
4. Open the cockpit **Today** board → confirm BPS/BCS/IC chips, correct breakevens, and typed payoffs in the drawer.
5. Spot-check 2–3 trades against your broker's option chain to confirm the 15-min-delayed numbers match.

## Not yet built (next decisions)
- **ORATS historical backtester** to flip the Gate 1 edge tier from Provisional → Validated (needs the paid data feed — your call).
- Directional true-POP flip for bear-call is a small `calculate_true_pop` change; validate it against realized frequency once logged.
