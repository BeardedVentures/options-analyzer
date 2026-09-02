# VEGA — Part 2, crypto correlation research brief
**Date:** 2026-09-01
**Status:** RESEARCH AND DESIGN ONLY. No execution code was written for anything in this document.
**Bottom line:** 2.1's core question has a clear empirical answer, and it is *no*. 2.3 has a clear
factual answer, and it is also *no*. 2.2 is the only tractable path, and it already exists — which
is itself the most important finding here.

---

## 0. Where the crypto boundary actually is — CORRECTED 2026-09-02

> **This section was wrong when first written, and the error was mine.** It said crypto "ran in
> every one of today's seven scans", inferred from 38 Coinbase readings in the quality log. That
> inference does not hold: `crypto._record_quality` never passes a `scan_id`, so the `None` on
> those rows says nothing about what produced them. Measuring properly on 2026-09-02 gives a
> different and much narrower answer, below. The original text is superseded, not amended,
> because the conclusion it reached was the opposite of the correct one.

**What is actually true.** `auto_paper_cycle` spawns `main.py` as a **subprocess** and waits for
it. The crypto phases run afterwards, in the parent, once that process has exited — so they spend
none of the scan's Robinhood budget. From the 2026-09-01 14:35 cycle:

```
14:35:02  START
14:35:02  main.py  (run_scan)                17m 41s   <- the scan, a separate process
14:52:43  vega_candidates.py                  8m 23s
15:01:06  _auto_open_from_board / reprice
15:01:33  _record_btc_forecast                    ~7s   <- crypto
15:01:55  _resolve + _record_direction_forecasts  22s
15:02:00  _record_crypto_premium_view             ~5s   <- crypto
15:02:42  END                          total  27m 40s
```

**Crypto costs 12 seconds of a 27m40s cycle — 0.7% — and zero request budget.** The Coinbase
readings cluster at 09:00, 09:59, 11:00, 11:59, 13:00, 13:59, 15:00: roughly eight minutes *after*
each scan completes, exactly as this sequencing predicts.

The **one** genuine in-scan hook is `assessment._btc_cross_venue`, reached from `assess()` for
every ticker but short-circuiting before any I/O unless the ticker is in `BTC_PROXY_TICKERS`
(`{"IBIT"}`). One cached HTTP read for IBIT; nothing at all for the other 55. ETHA declares a DVOL
reference but is not on the watchlist.

So the components are:

| component | file | flag | state |
|---|---|---|---|
| BTC cross-venue IV signal | `analysis/btc_signal.py`, `analysis/cross_venue.py` | `BTC_SIGNAL_ENABLED` | **True** |
| BTC directional forecast | `analysis/btc_forecast.py` | `BTC_FORECAST_ENABLED` | **True** |
| Crypto vol forecast | `analysis/crypto_vol_forecast.py` | `CRYPTO_VOL_FORECAST_ENABLED` | **True** |
| BTC signal primitives | `analysis/btc_signal.py` | — | live |
| Coinbase/Deribit data | `data/crypto.py` | — | live |

All three are enabled and run once per cycle. IBIT is a watchlist member on purpose
(`config.py`: "iShares Bitcoin ETF — crypto exposure, high IV, zero equity correlation") and
carries its own sector mapping, so its chain is fetched by the equity scan like any other name —
that is an options fetch, not a crypto one.

**How much isolation already exists?** Effectively all of the isolation that matters:

- The BTC signal is **advisory by construction** — `config.py` states it "never enters the gates
  dict, so it cannot block a trade regardless of what it reads". That is the same discipline the
  `advisory=True` convention enforces elsewhere.
- BTC claims go to `vega_predictions.jsonl` (the **prediction** ledger), not to
  `vega_outcomes.jsonl` (the **trade** ledger). The 30-trade cohort clock is untouched.
- It does **not** share the scan's process or request budget. It shares only ~12s of cycle
  wall-clock, after the scan has finished.

**DECIDED 2026-09-02: enforce and document, do not extract.** The isolation a standalone module
would buy is already held by the process split and the gates rule. What extraction would *add* is
a second scheduled task — and every serious VEGA outage on record has been scheduler fragility
(`StopAtDurationEnd` killing the close scan, the un-hidden window, cp1252 stdout), so spending a
new task to save twelve seconds is the wrong side of that trade. The IBIT read in particular is
not overhead: DVOL-versus-IBIT-IV is a premium-richness read on a watchlist underlying held
deliberately for uncorrelated VRP, which is the mission rather than a distraction from it.

What was done instead: `tests/test_crypto_boundary.py` (12 tests) holds the boundary
**behaviourally** — driving the crypto reading to both extremes and asserting `qualified` and
`failed_gates` do not move, asserting the gate set still equals `config.REQUIRED_GATES`, asserting
the two ledger paths stay distinct, asserting the cohort key grows no crypto dimension, and
asserting the scan subprocess still runs *before* any crypto phase. The measured boundary is
recorded in `config.py` above `BTC_SIGNAL_ENABLED`.

---

## 2.1 — IBIT/BTC correlation: the core question, answered

The handoff correctly identified the question to answer first:

> "Is the 'correlation' here actually distinguishable from IBIT and BTC just moving together
> because IBIT *is* BTC exposure — i.e. is there a genuine lead-lag edge, or is this restating the
> same variable twice?"

### The test

Daily log returns, 2025-09-18 → 2026-09-01. BTC from Coinbase candles via `data/crypto.py`
(350 days, 24/7); IBIT from yfinance (252 trading days). Inner join on date: **n = 239 overlapping
trading days.** Cross-correlation at lags k = −3…+3, where positive k means BTC leads.

```
contemporaneous corr(BTC, IBIT)  = +0.9323      R^2 = 0.869

lead-lag  corr(BTC_{t-k}, IBIT_t):
    k = -3   +0.1825
    k = -2   -0.0248
    k = -1   -0.0038      <- IBIT leads BTC by a day
    k =  0   +0.9323      <- same day
    k = +1   +0.0273      <- BTC leads IBIT by a day
    k = +2   -0.0356
    k = +3   +0.1720
```

### The answer: it is the same variable wearing two tickers

At daily frequency there is **no lead-lag structure in either direction**. Both one-day lags are
statistically indistinguishable from zero (+0.027 and −0.004, against a standard error of roughly
1/√239 ≈ 0.065). All the relationship — 87% of the variance — sits at lag zero, which is exactly
what a spot ETF with functioning authorised-participant arbitrage should look like.

The sports-betting analogy is where the idea goes wrong, and it is worth being precise about why.
A QB's passing yards and a WR's receiving yards are **two distinct random variables with a shared
latent driver** (game script). That structure is what makes a correlation parlay meaningful. BTC
and IBIT are not two variables with a shared driver; IBIT is a **wrapper** on BTC. Pairing them is
not a correlation trade, it is the same bet entered twice at double the fees.

The k = ±3 readings near 0.18 are the only non-trivial numbers, and they should be ignored: they
are ~2.8 SE, they are two of six lags tested with no multiple-comparison correction, and — decisive
— there is no mechanism by which BTC three days ago informs IBIT today when BTC yesterday does not.
Treating that as signal is the error VEGA has already been burned by.

### One genuine decoupling, and why it is not an edge

BTC trades 24/7; IBIT does not. BTC's overnight and weekend moves are genuinely unobservable in
IBIT's close-to-close series. But this is a **gap, not a lead**: IBIT opens already repriced, so
there is no window in which the information is known and not yet in the price. The existing code
already knows this — `cross_venue.py` names it as failure mode #3: *"DVOL is 24/7 and IBIT's
options are not. A gap measured at 20:00 compares a live number against one frozen at the close,
and the 'widening' is the clock."*

### Where a real two-variable structure does exist

Price series: one variable. **Implied volatility series: two variables.** Deribit's DVOL and IBIT's
ATM IV are two separate options markets pricing the same underlying risk, and they can disagree
without either being wrong about the spot price. That is a real cross-venue spread.

This is what the codebase already built, and it built it correctly. `config.py` records the
measurement: DVOL 34.24 vs IBIT 32.72 (a 1.5pp gap between two prices for the same risk) but vs
COIN 65.23 (a 31pp gap) — with the explicit note that COIN "is an operating company with its own
equity risk, so comparing its IV to BTC's measures the difference between the assets, not a
mispricing of one. Do not add it." `cross_venue.py` goes further and names the trap that GVZ is
computed *from* GLD's own chain, so a GLD/GVZ gap "is not two venues disagreeing — it is our IV
reconstruction disagreeing with CBOE's, and the likelier one to be wrong is ours."

**Conclusion for 2.1: the price-based lead-lag idea is dead, and should not be built. The
IV-based cross-venue idea is alive, is the correct formulation of the same intuition, and is
already implemented and running.** The remaining work on it is not construction, it is grading —
`BTC_IV_GAP_WIDE_PP = 3.0` is flagged in config as "PROVISIONAL — reasoned, not fitted. Nothing
has graded it yet."

---

## 2.2 — BTC swing-trade prediction engine

### Already built, already running, already being graded

`analysis/btc_forecast.py` is live under `BTC_FORECAST_ENABLED = True`, writing one dated,
probability-carrying claim per day into the same prediction ledger as everything else, at a
14-day horizon. The design notes in `config.py` are candid about its own limits: the model is
"deliberately small and the confidence deliberately timid", capped at
`BTC_FORECAST_MAX_PROB = 0.62`, because "a 20/50 crossover has no business claiming 80%".

### The current score — and why nothing can be concluded from it yet

From `logs/vega_predictions.jsonl`, all BTC/IBIT claims:

```
total claims                : 68
resolved                    : 24      correct: 12    incorrect: 12

  by claim type (resolved only):
    direction                    n=8   6/8  (75%)   Brier 0.2380
    direction_1d                 n=4   2/4  (50%)   Brier 0.2770
    direction_1d_baseline        n=4   1/4  (25%)   Brier 0.1945
    direction_overnight          n=4   0/4  ( 0%)   Brier 0.1126
    direction_overnight_baseline n=4   3/4  (75%)   Brier 0.3611
```

**24 resolved claims, split exactly 12/12.** Every per-type cell is n = 4 to 8. Nothing here
supports any conclusion in any direction, and the Brier ordering is currently perverse — the 0/4
bucket has the *best* Brier score — which is what n = 4 looks like. This is a validation clock that
has started, not a result.

### Answering the handoff's three design questions

**"Directional forecast or volatility/range forecast?"** — Both already exist, and they are
different things with different homes:

- `btc_forecast.py` is **directional**, and is a different thesis type from VEGA's non-directional
  premium-selling core. Its justification in config is not that it fits the thesis, but that
  `predictions.DIRECTION` "was already built, scored and tested — and had never been recorded
  once", so recording it starts a clock cheaply. That is a reasonable reason to run it and a bad
  reason to trade it.
- `crypto_vol_forecast.py` is the **vol/range** formulation, and it is the one consistent with
  VEGA's thesis: forecast forward realised vol for BTC, map it to IBIT, and compare to the premium
  IBIT's options are paying. That is a VRP read, which is what VEGA actually does.

**Recommendation: the vol path is the one to develop; the directional path should keep running as a
free-to-collect calibration baseline and should not be routed toward capital.**

**"What would its validation gate look like?"** — It needs its own, and it must be a *separate*
gate, not a share of the 30-trade equity cohort. Two independent reasons: BTC vol is a different
regime from equity vol, and — decisively — an IBIT trade is one of exactly two things. If it is
priced off the same Robinhood chain as everything else, it is already inside the existing cohort
and needs no new gate. If it is priced off crypto-venue data, it is a different `vendor_basis` and
`cohort()` already splits it automatically. **The existing cohort key handles this correctly with
no new machinery**, which is a point in favour of the current architecture.

For the *signal* rather than the trade, the prediction ledger already grades on Brier score against
a baseline, which is the right instrument. The gate should be stated as a number before more data
arrives — my suggestion, offered as a starting point and not a fitted value: **n ≥ 40 resolved
14-day claims, beating the matched baseline on Brier by a margin larger than its own standard
error.** At the current cadence that is roughly two months away.

**"Minimum viable data source?"** — Already solved and already free. `data/crypto.py` pulls
Coinbase spot/candles and Deribit DVOL; today's log shows 38 successful Coinbase readings. No paid
source is needed, which keeps this inside the standing "no paid data until free is maxed" rule.

### The one live gap worth naming

`BTC_IV_GAP_WIDE_PP = 3.0` is the threshold that decides whether the two venues are "meaningfully
apart", and config says plainly that nothing has graded it. It is a reasoned constant of exactly
the type this codebase keeps finding and correcting. It is not urgent — the signal is
advisory-only — but it should not be allowed to quietly become load-bearing.

---

## 2.3 — Forex as an alternative venue: CONFIRMED UNAVAILABLE

### Verified against the live tool list, not the documentation

Enumerated the Robinhood MCP server's tools directly via `robinhood_mcp.list_tools()`:

```
TOTAL TOOLS: 67        (was 55 on 2026-08-28, 47 before that)

  match 'forex'    : []
  match 'fx'       : []
  match 'currenc'  : ['get_currency_pairs']
  match 'crypto'   : ['get_crypto_quotes', 'get_crypto_positions', 'get_crypto_orders',
                      'get_crypto_account_onboarding_info', 'place_crypto_order',
                      'preview_crypto_order', 'cancel_crypto_order']
```

**There is no forex access.** No FX instrument tool, no FX quote tool, no FX order tool. The
handoff was right to flag the claim as asserted rather than confirmed — it is now confirmed false.

`get_currency_pairs` is the one near-miss and it is almost certainly not FX: in Robinhood's public
API, `/currency_pairs/` enumerates **crypto** trading pairs (BTC-USD and friends), and it sits in
the tool list surrounded by the crypto family. Confirming that by calling it would require
deliberately adding it to `READ_ONLY_TOOLS`, and `_call_read_tool` refuses anything not on that
list by design. **I did not add it.** The allowlist's whole value is that additions are deliberate
and reviewable, and spending that on a research question with a near-certain answer would be a bad
trade. Flagging it as the one open thread instead.

**Do not design against forex.** What Robinhood does offer is **spot crypto** — and note there are
no crypto *options* tools, so a Robinhood crypto strategy would be directional spot, not premium
selling.

### On the read-only posture, if crypto data is ever pulled from Robinhood

The wall the handoff asks about already exists and works. `READ_ONLY_TOOLS` is a five-entry
frozenset and `_call_read_tool` raises `WriteToolBlocked` on anything outside it. Its value is not
theoretical: **22 of the 67 tools are mutating**, including `place_crypto_order`,
`place_equity_order` and `place_option_order`, and the server has grown from 47 to 67 tools in five
days. A denylist would already have failed twice; the allowlist has not.

Adding `get_crypto_quotes` would be a one-line, reviewable change — but it is not needed, because
Coinbase already serves this for free and without touching a venue that can place orders.

---

## What would have to be true before this is proposed for attachment to VEGA

Stated as a checklist so it cannot be quietly softened later:

1. **VEGA's own cohort gate has cleared first.** Non-negotiable, and further away than the handoff
   assumed: the forward-looking count is **0 of 30**, not 12 of 30 (see Part 1 §0). Nothing crypto
   should compete for attention with a system that has not opened a trade since 2026-08-10.
2. **The drought is fixed and entries are flowing.** A bolt-on that adds candidates to a pipeline
   that opens nothing adds nothing.
3. **The crypto signal has its own graded record** — n ≥ 40 resolved claims beating baseline on
   Brier by more than its own standard error. Currently n = 24, at 12/12.
4. **`BTC_IV_GAP_WIDE_PP` is fitted rather than reasoned**, or is explicitly re-affirmed as
   provisional with the consequences of being wrong written down.
5. **The isolation question in §0 is decided explicitly** — either crypto is extracted to a
   walled-off module, or the handoff's isolation requirement is formally superseded by the
   advisory-only + separate-ledger safeguards that are already in place.
6. **Any IBIT trade is priced off a labelled chain source**, so `cohort()` splits it correctly.
   As of today this works — `vendor_basis` is in the key and the condor gap that would have
   defeated it is closed (Part 1 §1.4).

---

## Summary

| item | question | answer |
|---|---|---|
| 2.1 | Genuine IBIT/BTC lead-lag, or the same variable twice? | **The same variable twice.** corr = +0.93 at lag 0, ~0.00 at ±1 day, n = 239. Do not build the price-based version |
| 2.1b | Is there any real two-variable structure? | **Yes — implied vol across two venues** (DVOL vs IBIT ATM IV). Already built, correctly, in `cross_venue.py`. Needs grading, not construction |
| 2.2 | Is a BTC swing engine tractable? | **Already exists and is running.** 24 resolved claims, 12/12 — a clock that has started, not a result. Develop the **vol** path, not the directional one |
| 2.3 | Does Robinhood offer forex? | **No.** 67 tools enumerated live, zero FX. Spot crypto only, and no crypto options |

**No execution code was written for Part 2.** The one thing that changed on disk as a result of
this section is this document. The most useful outcome is negative and cheap: the headline idea was
falsified in about ten minutes of arithmetic against free data, before anything was built on it.
