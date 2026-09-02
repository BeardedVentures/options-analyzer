# VEGA — Watchlist audit (Step 3)
**Date:** 2026-09-02
**Status:** Tier B **APPROVED AND EXECUTED** 2026-09-02 (LMT, ABBV removed; watchlist 56 → 54).
Tier A held, and **revised** by the diagnostic in §5 — CRWD and TLT come off it.
**Headline:** the premise is wrong in both directions — far fewer names have a chain-quality
problem than believed, and far more names are dead weight for a completely different reason.

---

## 0. The metric trap, first — because it is the reason the premise was wrong

The brief's list of chronic failures (JNJ, PFE, GE, RCL, LMT, BAC, USB, BLK, MAR, COP, PSX,
AMGN, ABBV, NEE, XLV, XBI) is the **whole-grid** list. That is the metric that skipped GE every
scan for weeks while GE was 100% quotable across every strike VEGA would actually sell.

Two different numbers exist and they disagree:

| | what it measures | where it lives | what it is for |
|---|---|---|---|
| **whole-grid ratio** | quotable across *every* strike | `data/data_quality_log.json` | instrumentation |
| **delta-band ratio** | quotable in the 0.12–0.30 band | the gate, `run.log` skip lines | **the decision** |

`data_quality_log` stores the whole-grid number (`measure_chain_quality`); the gate uses
`measure_tradeable_band_quality`. Ranking the watchlist on the log file would have reproduced the
GE error at scale. Everything below is built on **observed gate outcomes**, which is the band
metric, and which only exists from 2026-08-31 14:38 onward — so the sample is short by
construction and is stated as such rather than padded with the wrong number.

**Sample: 7 scans (2026-09-01), the first full day on the delta-band gate.**

---

## 1. Chain quality — 42 of 56 names never fail

Of 56 watchlist names, **only 14 have ever been skipped** on either side, and **only two are
chronic**:

| ticker | skip rate | whole-grid mean | band % when it failed | read |
|---|---|---|---|---|
| **LMT** | **71%** (5/7) | 39% | 32, 35, 40, 42, 44, 45, 47, 47 | **chronic** — clusters just under the 50% floor |
| **ABBV** | **43%** (3/7) | 51% | 31, 34, 49, 49 | **chronic-ish** |
| PLD | 29% | 51% | 38, 43, 43 | *known cause* — monthly-only, DTE window misses |
| JNJ | 29% | 54% | 46, 49, 49 | borderline |
| NEE | 29% | 58% | 9, 22, 38 | genuinely thin when it fails |
| CLF | 29% | 87% | 25, 40, 47 | **the inverse trap** — best-looking grid, fails the band |
| MAR, PFE, XLV, USB, RCL, PSX, COP, BA | 14% (1/7) | 46–64% | single failures | noise at this sample size |

**Five names the brief lists as chronic have a 0% skip rate: GE, BAC, BLK, AMGN, XBI.** Four of
them (BLK 51%, AMGN 54%, KO 55%, GE 57%) sit at or below the whole-grid floor while passing the
band gate on **every single scan**. Cutting on the brief's list would have removed working names
for the exact reason the delta-band change was made to stop.

CLF is the trap running the other way: 87% whole-grid, the third-best number on the board, and it
still failed the real gate on 2 of 7 scans.

**Chain-quality conclusion: there is no third of the watchlist to reclaim here. There are two
names, and cutting both would save roughly 3.5% of fetch volume.** The budget win this session was
Step 1 (~60%), not this.

---

## 2. The real dead weight — a different question entirely

Chain quality asks "can we see this name?" The mission asks "does this name ever produce a
high-edge, capital-efficient premium setup?" Those turn out to be almost unrelated.

From `logs/vega_counterfactuals.jsonl`, 1,557 spreads formed across 56 tickers over
2026-08-25 → 09-01:

**31 of 56 tickers produced ZERO qualified spreads.**

### The zero-qualifiers that cost the most

| ticker | spreads formed | qualified | median IV rank | top sole blocker |
|---|---|---|---|---|
| XBI | 49 | **0** | **56** | credit_to_width |
| CRWD | 43 | **0** | **46** | earnings_clear |
| GS | 38 | **0** | 27 | credit_to_width |
| PEP | 35 | **0** | **49** | otm_buffer |
| XOM | 35 | **0** | **48** | credit_to_width |
| WMT | 34 | **0** | 30 | otm_buffer |
| COP | 32 | **0** | 25 | credit_to_width |
| XLV | 31 | **0** | 28 | — |
| KO | 26 | **0** | 30 | otm_buffer |
| AAPL | 24 | **0** | 24 | credit_to_width |
| CRM | 24 | **0** | 26 | earnings_clear |
| ADBE | 24 | **0** | 27 | earnings_clear |

### And the ones actually carrying the system

| ticker | formed | qualified | rate |
|---|---|---|---|
| TSLA | 24 | 16 | **67%** |
| PLTR | 35 | 22 | **63%** |
| META | 31 | 19 | **61%** |
| NVDA | 21 | 10 | **48%** |
| GDX | 71 | 29 | **41%** |
| AMZN | 18 | 7 | 39% |
| AMD | 27 | 10 | 37% |
| COIN | 42 | 14 | 33% |
| GOOG | 23 | 7 | 30% |
| **QQQ** | **86** | **24** | **28%** |

Note QQQ: a top-ten producer, and one of the two names Step 1's term-structure truncation was
degrading. That flag is now off.

### The regime excuse does not hold

The obvious objection is that a quiet week suppresses everything, so zero-qualifiers are just
waiting for vol. The data refuses that:

```
zero-qualifier tickers (n=31) : median IV rank 26.9
producing tickers      (n=25) : median IV rank 25.4
```

Essentially identical. The split is **not** explained by IV regime. And six zero-qualifiers were
sitting in exactly the premium-rich conditions the strategy wants and still produced nothing:

```
XBI   49 formed @ median IVR 56  -> 0 qualified
BAC   19 formed @ median IVR 56  -> 0
PEP   35 formed @ median IVR 49  -> 0
XOM   35 formed @ median IVR 48  -> 0
TLT   18 formed @ median IVR 46  -> 0
CRWD  43 formed @ median IVR 46  -> 0
```

**XBI is the clearest case on the board**: 49 spreads formed at a median IV rank of 56 — a rich
regime — and not one cleared. High IV that will not convert into credit-to-width is a structural
property of the name's spread pricing, not a bad week.

---

## 3. Proposal (as submitted) — Tier B since approved and executed

Three tiers, deliberately separated because they fail for different reasons and deserve different
treatment.

**Tier A — cut candidates (structural, high confidence): XBI, CRWD, PEP, XOM, TLT, BAC.**
High IV rank, meaningful spread formation, zero output. These consume fetch and analysis in the
conditions where the strategy should work best. Combined: 198 spreads formed, 0 qualified.

**Tier B — chain-quality cuts: LMT, ABBV.** The only two names that chronically fail the band
gate. LMT also produced 0 of 20. Low regret either way.

**Tier C — hold, do not cut: GS, WMT, COP, XLV, KO, AAPL, CRM, ADBE and the remaining
zero-qualifiers.** Median IV rank 24–30 — these genuinely have not been given the regime the
strategy needs, and cutting them now would be procyclical: removing names precisely because vol
has not arrived, guaranteeing they are absent when it does. ADBE/CRM/CRWD's top blocker is
`earnings_clear`, which is a calendar state, not a property of the name.

**My recommendation was: approve Tier B now, and hold Tier A for one more week of data.** Tier B is
two names failing a data-quality gate — that is a clean, well-evidenced call on 7 scans. Tier A is
a claim about *edge*, made on 8 trading days, and the cost of being wrong is asymmetric: a cut
name produces nothing forever, while a kept name costs only fetch budget that Step 1 has already
freed 60% of. The evidence for Tier A is strong enough to name and weak enough to wait on.

### Outcome, 2026-09-02

**Tier B approved and executed.** LMT and ABBV are out; the watchlist is 54. Both lines are
commented out in `config.py` rather than deleted, each carrying the band ratios and formed/qualified
counts that condemned it, so the reason travels with the line and a future reader is not left
guessing. Josh's stated basis for approving is worth preserving: the audit's own finding undercuts
the list that prompted it, so two names standing out as chronic *on the corrected metric* is a far
stronger warrant than the original sixteen ever were.

**Tier A held** — and then revised by the diagnostic in §5, which Josh asked for rather than
waiting a week to ask the same question. That call was right: it took CRWD and TLT off the cut list
for reasons a week of data would not have surfaced.

---

## 4. Honest limits of this analysis

- **7 scans on the correct metric.** One trading day. Any 14%-skip name (a single failure) is
  indistinguishable from noise, and I have not treated those as signal.
- **8 trading days of qualification data**, spanning one broad regime. The IV-rank control above
  makes the zero-qualifier finding much harder to dismiss, but it does not make it seasonal-proof.
- **`sole_failed_gate` is only populated when exactly one gate failed**, so the "top blocker"
  column describes near-misses, not the full failure distribution.
- **`credit_to_width` dominates overall** (982 of the failed-gate tallies), consistent with the
  standing finding that the drought is a credit-floor problem. That is a *system-level* constraint
  and it is not fixed by trimming the watchlist — a point worth keeping in view, because it means
  Tier A's zeros are partly a floor calibration question wearing a ticker-selection costume.


---

# 5. Diagnostic — why XBI's spreads die, and whether it generalises

Added 2026-09-02 at Josh's direction. His argument for doing it now rather than after a week:
XBI clears every upstream filter and dies at the same downstream gate, so if the cause is
structural, another week reproduces the same zero and answers nothing. That was right, and the
answer arrived in about ten minutes.

## The finding: selection failures are survivable, execution failures are terminal

Splitting the gates by what they describe:

- **SELECTION** — `delta_cap`, `otm_buffer`, `pop`, `credit_to_width`. These say *this strike pair*
  is wrong. The scanner simply picks another pair.
- **EXECUTION** — `quote_spread`, `liquidity`. These say *this underlying's options market* is too
  wide or too thin. No choice of strikes fixes that.

Across the 53 tickers with ≥10 formed spreads:

```
failing an EXECUTION gate on >20% of spreads : 27 tickers, mean qual rate  0.9%, 4/27 ever produced
not                                          : 26 tickers, mean qual rate 21.6%, 20/26 ever produced
```

**23 of 27 execution-bound names produced nothing at all in eight trading days.** Note the
threshold is what separates them, not a smooth relationship: the linear correlation with
qualification is −0.54 for the worst execution gate against −0.61 for the worst selection gate,
so on correlation alone selection looks *stronger*. Reported that way round deliberately — the
split is the finding, and the correlation does not support the tidier story.

The producers make the point from the other side. Every one of them fails only selection gates:

```
TSLA 67%   delta_cap, otm_buffer, pop        QQQ  28%   credit_to_width          (quote_spread 0%)
META 61%   delta_cap, otm_buffer, pop        GDX  41%   delta_cap, otm_buffer, pop
PLTR 63%   delta_cap, pop                    COIN 33%   delta_cap, pop
```

QQQ fails `credit_to_width` on **69%** of its spreads and still qualifies 28% of them, because
that is the only gate it fails. XBI fails six gates at 24–71%, including `quote_spread` 57% and
`liquidity` 29%.

## What this does to Tier A

**XBI — confirmed structural. Cut candidate on evidence, not on a coin flip.**
`quote_spread` 57%, `liquidity` 29%, six gates over 20%. Its chain is *present* — 0% skip rate,
76% whole-grid — it is simply too wide to trade. Another week reproduces the zero.

**PEP and XOM — same pattern, weaker.** `quote_spread` 46% and 37%. They belong with XBI.

**CRWD — REMOVE FROM THE CUT LIST.** Its dominant failure is `earnings_clear` at **100%** — every
single one of 43 spreads blocked by a dated catalyst. `quote_spread` is 7%, `liquidity` 0%. This
is a calendar state with an expiry date, not a property of the name, and it is exactly the case
Josh's instinct to check was aimed at. CRWD is genuinely "wait", and we now know what for.

**TLT — REMOVE FROM THE CUT LIST, and it is not a ticker problem.** `min_credit_usd` 78% and
`otm_buffer` 100%, `quote_spread` only 11%. TLT trades ~$83 with median natural credit of
**$0.14/share = $14/contract** against a correctly-scaled floor of $20.75. I checked whether the
price-scaled floor was misfiring — it is not:

```
CREDIT_FLOOR_REFERENCE_PRICE = 100.0, MIN_CREDIT_USD = 25
  TLT  $83  -> $20.75      BAC  $62  -> $15.50      IBIT $37 -> $15.00
  XBI  $164 -> $25.00      SPY  $773 -> $25.00
```

The floor is doing exactly what it was built to do. TLT is a low-vol bond ETF that does not pay
enough premium at the width VEGA trades. That is a real answer, and cutting it is defensible on
those grounds — but it is a *premium* judgement, not a data-quality one, and it belongs in the
same conversation as the credit floor itself rather than in a watchlist trim.

**BAC — own case.** `support_shelter` 95% — a levels gate, not execution (`quote_spread` 26%).
Worth its own look before cutting; a technical gate failing 95% of the time on one name is more
likely to be telling us something about the gate than about BAC.

## Revised Tier A

| ticker | verdict | evidence |
|---|---|---|
| **XBI** | cut — structural | quote_spread 57%, liquidity 29%, 6 gates >20% |
| **PEP** | cut — structural | quote_spread 46% |
| **XOM** | cut — structural | quote_spread 37%, liquidity 49% |
| CRWD | **keep** | earnings_clear 100% — calendar, expires |
| TLT | keep for now | credit floor working correctly; a premium call, not a quality one |
| BAC | keep for now | support_shelter 95% — investigate the gate first |

The week of extra data is still worth having for the three structural names, but its purpose has
changed: it is no longer "do they recover", it is "does anything about their options market
change". If `quote_spread` is still failing on half their spreads next week, that is the same
answer twice and they should go.

## A generalisable rule worth keeping

**Before cutting a name for producing nothing, check whether it fails execution gates or selection
gates.** Selection failures mean the scanner is being picky and will find another pair. Execution
failures mean the name's options market cannot support the trade, and no amount of waiting or
re-scanning changes it. The same test cleanly separated a structural cut (XBI) from a calendar
artifact (CRWD) that would otherwise have been cut for a reason that expires on its own.
