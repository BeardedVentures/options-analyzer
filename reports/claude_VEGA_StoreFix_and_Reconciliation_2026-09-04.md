# VEGA — Store divergence, the 1w reconciliation, and the 19-of-54 block

**Date:** 2026-09-04
**Branch:** `session/2026-09-03-prediction-engine`
**Scope:** revised order — store divergence, 1w bias/coverage reconciliation, 19-of-54, direction-channel brief. Plus the 95% falsification test and the three methodology rules.

---

## §A — Refutations, including one of my own from this morning

### A1. My leptokurtosis attribution was wrong, and the falsification test caught it

I attributed the overnight band's residual over-coverage to fat tails and predicted, in advance, that a **95% band would UNDER-cover**. It does not.

| claimed | actual | 95% CI | error |
|---|---|---|---|
| 50% | 64.3% | [62.8, 65.6] | **+14.3** |
| 68% | 78.8% | [77.5, 80.1] | +10.8 |
| 80% | 86.8% | [85.8, 87.8] | +6.8 |
| 90% | 92.8% | [92.1, 93.6] | +2.8 |
| **95%** | **95.3%** | [94.7, 95.9] | **+0.3** |

The prediction failed. The more detailed measurement shows why — empirical `|z|` quantiles of the standardized gap against the normal:

| quantile | 50% | 68% | 80% | 90% | 95% |
|---|---|---|---|---|---|
| empirical | 0.476 | 0.748 | 1.026 | 1.446 | **1.910** |
| normal | 0.674 | 0.995 | 1.282 | 1.645 | **1.960** |

The gap distribution is sharply **more peaked** than lognormal through the entire body and **converges to it at ~1.91σ — essentially exactly the 95% level.** So the named mechanism (excess kurtosis) survives; my specific claim about *where it inverts* was wrong, because I assumed ±1.96σ was out in the fat tail when for overnight gaps it is precisely the crossover.

The test cost nothing and was worth running exactly because it could fail. It did.

**Operational consequence, which is the real prize:** the overnight horizon is **calibrated as-is at 95% confidence**, and 80% is its second-worst practical choice. I did **not** change the default to 0.95 — picking the confidence level that makes coverage look right is the curve-fit the variance-share correction was careful not to be. That is an operator decision, now made against a measured curve instead of a guess.

### A2. A real bug this surfaced: overnight claims were recording the wrong coverage table

`price_projection.MEASURED_COVERAGE` was measured on **close-to-close** outcomes and reads `0.815` at the 80% level. Overnight claims were recording it as their own expected coverage while actually delivering **0.868** against real opens.

One field, two populations, no way to tell them apart after the fact — the `credit_per_share` defect exactly, in a field created two days ago. Gap claims now carry `GAP_MEASURED_COVERAGE`, measured on 5,296 real gap outcomes per level.

### A3. The 1w "contradiction" was a broken yardstick, not a hidden absorber

You proposed three reconciliations. It is (1) — the two tests aren't comparable — for a specific, nameable reason that is worth more than the reconciliation itself.

**Realized vol computed as `sqrt(unbiased variance)` is a downward-biased estimator of sigma at small sample sizes** (Jensen: `E[√s²] < √E[s²]`). The correction factor `c4(df)` is 0.940 on 5 returns, 0.973 on 10, 0.988 on 21, 0.994 on 42. On a 20-vol base that is 1.20, 0.55, 0.25, 0.12 points of *apparent* forecast bias created purely by the yardstick.

Compare against what I measured yesterday: **+2.33, +1.19, +0.50, +0.06.** The decay profile matches the correction almost exactly. Applying it:

| horizon | raw bias | 95% CI | **c4-corrected** | 95% CI | removed |
|---|---|---|---|---|---|
| 1w | +2.33 | [+1.91, +2.73] | **+0.60** | [+0.15, +1.04] | 1.72 |
| 2w | +1.19 | [+0.72, +1.65] | +0.40 | [−0.06, +0.87] | 0.79 |
| 1m | +0.50 | [−0.06, +1.03] | **+0.14** | [−0.45, +0.67] | 0.36 |
| 2m | +0.06 | [−0.48, +0.59] | −0.12 | [−0.69, +0.42] | 0.18 |

**The band-coverage test never estimates a volatility at all** — it compares a price to an interval — so it was the test with the intact yardstick throughout.

And the two now agree quantitatively, which is the check that matters. A residual bias of +0.60 on a 20-vol base makes the band 1.030× too wide, which predicts **81.3%** coverage. The CI bounds [+0.15, +1.04] predict [80.3%, 82.2%].

**Measured 1w coverage: 81.2% [80.2, 82.2].**

The prediction and the measurement are the same interval to within a tenth of a point. There is no hidden absorber and nothing in the path is insulating the band from the forecaster.

**The regime-bucketed coverage you asked for, which independently rules out (3):**

| regime | n | 1w coverage | 95% CI |
|---|---|---|---|
| quiet (VIX < 15.7) | 5,184 | 81.4% | [79.7, 83.1] |
| normal (15.7–18.7) | 5,168 | 82.9% | [81.3, 84.4] |
| stressed (≥ 18.7) | 5,200 | 79.3% | [77.7, 81.0] |

Coverage is **not** flat — it varies by 3.6pp across regimes and stressed under-covers. An absorber would have produced flatness and insensitivity. It does not track the raw bias pattern either (+3.11 → +1.49 monotone), which is exactly what you would expect once 1.72 points of that pattern turn out to belong to the yardstick.

### A4. The store divergence was larger than "advice corruption"

On 2026-09-04 the JARVIS endpoint returned **200 open rows across 25 distinct tickers** — WMT ×30, GS ×22, QCOM ×18 — against a real book of **four**.

**25 of a 54-name watchlist were flagged ALREADY IN POSITION, and the flag was wrong in both directions:** 24 names falsely held, and **3 of the 4 genuinely held names (AMGN, SMH, NEE) missing from it entirely.** Only NKE overlapped. The documented local fallback could not have rescued it either — `open_positions.json` does not exist, so the phantom rows were the *only* source.

Fixed: `_get_open_position_tickers` now reads `logs/vega_outcomes.jsonl`, the store the caps cohort counts from, `log_outcome.py` and the manual form write to, and the shadow book grades from. **25 tickers → 4.**

**Second defect in the same function:** book risk was reporting **$0.00** for a book of four real spreads, because outcome rows carry width and credit rather than a `max_loss` field and the code read only `max_loss_usd`. A zero reads as "no exposure", not as "not computed". Now derived, with anything genuinely unpriceable counted and the total named a **floor**. **$0.00 → $1,266.00.**

---

## §B — The 19-of-54 block, answered

The diagnostics were in the board all along — `pair_selection_diagnostics` rides on the rejection payload; I looked at the wrong row yesterday and reported the field absent.

**15 of the 19 tickers did enumerate spreads** (41 in total across them). This is not a chain-absence story for most of them. Pooled rejection counters from the enumeration itself, across all 19:

| count | reason | share |
|---|---|---|
| **501** | **short_quote_not_tradeable** | **46%** |
| **240** | **short_liquidity_below_floor** | **22%** |
| 89 | long_quote_not_tradeable | 8% |
| 79 | short_delta_too_high | 7% |
| 61 | short_buffer_too_tight | 6% |
| 52 | short_delta_too_low | 5% |
| 35 | natural_credit_non_positive | 3% |
| 27 | credit_to_width_below_min | 2% |
| 16 | long_liquidity_below_floor | 1% |
| 2 | credit_below_min_usd | <1% |

**68% of all enumeration rejections are quote or liquidity failures.** Not the DTE window — `dte_window` failed on 0 of 173 enumerated spreads. Not expiration alignment. **Chain quality, at the strikes the strategy actually wants.**

### The contradiction that makes this important

The **same scan** reported:

> `scan_coverage: 54/54 tickers (100%) cleared the chain-quality floor, minimum is 70%`

The chain-quality metric says every ticker is healthy while two thirds of the enumeration's rejections are strikes it cannot quote. **The health metric and the enumeration are measuring different strikes and disagreeing completely.** This is the third recurrence of that shape (the coverage metric crediting a ticker seen in any window; the quality floor judging far-OTM strikes).

### Two tickers that are genuinely different

- **TLT** is not a chain problem at all: `short_buffer_too_tight=33, short_delta_too_high=17`. Its vol is so low that every strike inside the 5% buffer is also inside the delta cap. That is a strategy-fit refusal and correct.
- **BLK, XLV, TLT** produced **zero** short candidates — nothing even entered the pair loop.

### What I changed, and what I deliberately did not

`quote_not_tradeable` conflated two failures needing opposite responses: **no two-sided quote** (a data-path question) versus **spread too wide to cross** (genuine illiquidity, a correct refusal). One counter could not tell them apart, and that distinction is now the whole question.

Split into `quote_absent` / `quote_crossed` / `quote_spread_too_wide`, on both legs. **The gate decision is unchanged and a parametrized test re-derives the original predicate to keep it that way.** The next scan answers the question instead of the next session re-deriving it.

One thing the split already established: a strike with **no price at all** is caught one gate earlier as `short_missing_price_delta`, which did not appear in any ticker's top reasons. **So all 501 of those strikes had a positive mid and a failing bid/ask** — they were priced, and simply not two-sidedly quotable. That narrows the diagnosis considerably and makes "is this our data path or the market?" the precise next question.

**No gate was loosened.** `MAX_QUOTE_SPREAD_PCT`, `MIN_OPTION_VOLUME`, `MIN_OPTION_OPEN_INTEREST` are untouched.

---

## §C — Direction channel: the brief, not the decision

Every horizon with enough power to be graded shows **zero discriminating information**:

| claim type | raw | **effective** | hit% | Brier | skill | **resolution** |
|---|---|---|---|---|---|---|
| direction_overnight | 336 | **96** | 18.5 | 0.174 | −0.154 | **0.0000** |
| direction_overnight_baseline | 336 | 96 | 64.6 | 0.326 | −0.427 | 0.0000 |
| direction_1d | 336 | **96** | 29.8 | 0.211 | −0.007 | **0.0000** |
| direction_1d_baseline | 336 | 96 | 36.9 | 0.234 | −0.006 | 0.0000 |
| direction_1w | 112 | 16 | 31.2 | 0.216 | −0.003 | 0.0013 |
| direction_1m | 444 | — | — | — | — | **never resolved** |

Three facts an operator decision needs:

1. **Overnight and 1d are decisively measured, not merely early.** Effective N of 96 each, well past the 10-block floor. Resolution 0.0000 is what shuffling the outcomes produces. These two horizons have had their chance.
2. **1w has effective N of 16** — gradeable but thin. **1m has never resolved a single claim**; earliest `resolves_on` is 2026-09-24. The horizon closest to VEGA's trade window is entirely unproven.
3. **Cost: ~178 claims/day, 3,572 rows, 88% of the shared prediction ledger by volume** — and it shares that file with the band channel, which is why the band channel could sit empty while `prediction_ledger` read OK.

**Recommendation (yours to take):** retire `direction_overnight` and `direction_1d`; keep `direction_1w` and `direction_1m` until they reach comparable power. That is evidence-proportionate rather than all-or-nothing, cuts ledger volume roughly in half, and preserves the only two horizons that have not yet had a fair test. Nothing here touches selection, so the change is low-risk either way.

**Not a bug, so nobody chases it:** 329 direction claims currently sit `open` past their `resolves_on`. All 329 are due **today**, and the resolver runs inside the cycle, which first fires at 08:35 CDT. They are not late.

---

## §D — Methodology

Three rules added to `VEGA_METHODOLOGY.md` §4:

1. **The consumption rule you identified.** *A figure quoted INTO a decision carries its effective N inline, and a figure under ~30 independent blocks cannot be a load-bearing premise.* Stated explicitly as downstream-of-production, because producing the uncertainty correctly bought nothing — `cluster_sample()` printed `n_effective = 6` on the same line as the 86.1% figure.
2. **Check the yardstick before concluding about the thing measured.** The Jensen finding, with the note that two measurements of one quantity disagreeing is a fact about one of the measurements until proven otherwise.
3. **Level 2 applies to code this process wrote 48 hours ago.** `compact()` has exactly one caller, gated to `--mark-only`, and one Windows scheduled task exists which runs `run_auto_paper_cycle.ps1`. The retention policy that justified the JSONL format change has no execution path. Confirm against the scheduler, not the source.

---

## §E — New findings

1. **`compute_decay_alerts` reads the same nonexistent file.** `load_open_positions` survives for one other caller (`main.py:1682`), feeding decay alerts on close scans from `open_positions.json` — which does not exist. **Decay alerts have therefore never fired.** Same class as the book-awareness bug, same file, not fixed here because it needs a mark-source decision rather than a store swap.
2. **All 501 unquotable strikes had a positive mid** (§B). Strikes with no price at all land in a different counter that did not appear at all.
3. **The overnight gap band is calibrated at 95% and badly miscalibrated at 50%** (+14.3pp). The error is monotone in the confidence level (§A1).
4. **The 1w apparent bias was ~74% yardstick** (1.72 of 2.33 points).
5. **`direction_1m` has never produced a graded output** and cannot before 2026-09-24 — the direction channel's most decision-relevant horizon is unproven while its two weakest are decisively measured.
6. **`main.py` carries a UTF-8 BOM**, so tooling that reads it as plain `utf-8` sees `U+FEFF` as the first character. Cost me one failed parse; harmless but worth knowing before someone writes a linter step.

---

## §F — Open items by consequence

1. **Log the CRWD and QQQ trades.** Unchanged, still zero `source: "manual"` rows, still the only fill-verified ground truth in the project.
2. **Decide the overnight band's confidence level** — 95% is calibrated today, 80% is not. A measured curve now exists; the choice is yours and I deliberately left the default alone.
3. **Is `quote_absent` our data path or the market?** The next scan's counters answer it. If a large share of priced-but-unquotable strikes turns out to be a fetch artifact, this is the drought's largest remaining mechanical cause.
4. **Reconcile the chain-quality metric with the enumeration** (§B). A health metric reading 100% while 68% of rejections are quote failures is a metric measuring the wrong strikes — third recurrence of that shape.
5. **Direction-channel decision** (§C).
6. **`compute_decay_alerts` has never fired** (§E1).
7. **Confirm corrected overnight coverage on production rows** from 2026-09-07.
8. **`MIN_IV_RANK` under `APPROX`**; **two `iv_rank` fields**; **`--mark-only` / retention**; **`n_effective` not variance-corrected**.
9. **Rotate the NewsAPI key**; **delete the three stale branches**; **decide `ENTRY_HOLD`**.

---

## §G — What I would not do

**1. I would not change the overnight band's default confidence to 95% just because it is calibrated there.** It is the single most tempting move in this report and it is selecting a parameter on the validation sample. The measured curve is recorded so the choice can be made deliberately; that is the correct output.

**2. I did not retire any direction horizon.** The evidence supports retiring two, the change is low-risk, and it is still a cohort-adjacent decision about what the system measures. §C is the brief; the call is yours.

**3. I did not touch the chain-quality floor, `MAX_QUOTE_SPREAD_PCT`, or the liquidity floors,** despite §B showing they account for 68% of the block. The finding is that a health metric disagrees with the enumeration — that is a measurement problem, and loosening a floor because it rejects a lot is how a premium-selling thesis gets destroyed. The floors may well be correct and the metric wrong.

**4. I would not treat the drought hypothesis space as closed, but I would stop searching it the same way.** Six candidate causes have now died to measurement. What survives is the plain reading plus one genuinely open mechanical question — §B's priced-but-unquotable strikes — which is the first drought candidate in weeks that is *measurable on the next scan* rather than by argument. I would resolve that before concluding.

**5. On your planning point, I agree and would put a number on it.** If negative VRP persists, `caps_v1` is not reachable: the board has qualified zero trades since 2026-08-10, and thirty executed-and-closed trades at that rate is unbounded. The band channel writes 432 gradeable claims a day and the 1m horizon settles in 21 sessions. **That is the evidence engine now, not a supplement** — and it is worth saying plainly that the cohort target may need restating in terms the current regime can actually deliver, rather than left to fail quietly in October.
