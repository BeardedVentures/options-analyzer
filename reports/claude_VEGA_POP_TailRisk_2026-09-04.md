# VEGA — Does the fat tail reach POP and sizing?

**Date:** 2026-09-04
**Branch:** `session/2026-09-03-prediction-engine`
**Question:** the 99% gap band under-covers by 1.5pp and empirical `|z|` at 99% is 3.952 against an assumed 2.576. Does that understate the probability of full max loss on a defined-risk credit spread?

**Answer: no. At the horizon VEGA trades, the model is conservative at every real strike location — by 1.8 to 5.1 percentage points, with every confidence interval entirely below the modeled probability.**

---

## §A — Refutation: the escalation does not survive measurement

We both escalated this. I called it "a risk-model finding that reaches further than the band channel"; you called it "a risk-of-ruin input, not a calibration curiosity" and put it first in the order. The measurement says the concern is real about the *instrument* and does not reach *sizing*.

Two things were being conflated, and separating them is the whole result.

### A1. The fat tail is an overnight property and largely decays by the trade horizon

Standardized **downside** returns — the max-loss side of a bull put — measured against `band_forecast`'s own forecast sigma, 16 names, 5 years, 15,520 observations per horizon. Positive `emp − norm` means the empirical quantile is *less* extreme than the normal, i.e. **the model is conservative**.

| quantile | overnight | 1w | 1m (21d) | 35d (24d) |
|---|---|---|---|---|
| 10% | +0.269 | +0.125 | +0.276 | +0.312 |
| 5% | +0.195 | +0.078 | +0.260 | +0.283 |
| 2% | −0.065 | −0.056 | +0.117 | +0.155 |
| **1%** | **−0.538** | −0.266 | **−0.048** | **+0.067** |
| **0.5%** | **−1.263** | −0.412 | −0.178 | **−0.014** |

The dramatic overnight fat tail (−1.263 sigma at the 0.5% quantile) is almost entirely gone by 21 days and absent at 35. At the trade horizon the downside tail is **indistinguishable from normal at 1% and 0.5%, and conservative everywhere shallower.**

That is the CLT doing what it does to a sum of ~24 daily returns, and it was the prediction I registered before running it. I predicted "still fatter but much less so, empirical |z| at 99% around 2.8–3.3"; measured 1m is −2.374 against a normal −2.326 — **less fat than I predicted**, and at 35d it is thinner than normal.

### A2. Real spreads take max loss in the body, not the tail

From 2,899 counterfactual spreads: the long strike sits **7.56% below spot at the median** (p10 3.99%, p90 13.24%), at a median 32 calendar days.

Converted to the model's own z-axis, the max-loss boundary of a real VEGA spread sits at roughly **−0.7 to −1.0 sigma**. Not −2.6.

- Fraction of ledger spreads whose max-loss boundary is beyond **−2.576σ** (the 99% level): **0.00%**
- Beyond −2.0σ: **0.17%**
- Beyond −1.5σ: **2.93%**

**The region where the model understates risk is a region these spreads essentially never reach.** A defined-risk credit spread at 0.25 delta is a body bet by construction.

### A3. The decisive measurement: modeled vs actual, at real strike locations

No delta inversion (that broke down — back-solved vols ran to 356% on the condor and call-side rows, so I discarded it). This uses `band_forecast`'s live forecast sigma at each anchor and the ledger's real OTM geometry, over 24 trading days, 18,720 observations per row:

| strike (real geometry) | modeled P(breach) | **actual** | 95% CI | model error |
|---|---|---|---|---|
| short strike, p10 (2.82% OTM) | 34.90% | 29.77% | [27.70, 32.02] | **+5.13** |
| short strike, median (5.12%) | 24.62% | 20.64% | [18.65, 22.47] | **+3.98** |
| short strike, p90 (9.55%) | 11.84% | 9.34% | [8.06, 10.69] | **+2.51** |
| **long strike, p10 (3.99%)** | 29.36% | 24.87% | [22.76, 26.83] | **+4.49** |
| **long strike, median (7.56%)** | **16.52%** | **13.52%** | **[11.94, 15.04]** | **+3.00** |
| **long strike, p90 (13.24%)** | 6.43% | 4.59% | [3.72, 5.56] | **+1.83** |

**Every confidence interval sits entirely below the modeled probability.** The model overstates the chance of full max loss by a factor of **1.2× to 1.4×**.

So a modeled 78% POP is not "optimistic in the tail." At real strike geometry it is **pessimistic in the body** — which is the direction a premium seller wants to be wrong in, and it means the gates have been declining setups the model scored more harshly than reality warranted.

### A4. Why the two findings are consistent rather than contradictory

They are the same effect seen at two distances. The model over-disperses in the body — the 80% band contains 86% — which makes it conservative wherever real strikes sit. It under-disperses in the far tail. The crossover is at ~1.96σ overnight, and real spreads live at 0.7–1.0σ.

The band-coverage finding and the POP finding are one statement: **VEGA's lognormal is too wide where it trades and too narrow where it doesn't.**

---

## §B — The caveat that limits this

**This is a 2021–2026 sample with no 2008- or March-2020-scale event.** A "conservative at the 0.5% quantile" reading is exactly the reading a benign sample produces, and the far-tail bins are thin by construction — at 15,520 observations the 0.5% quantile rests on ~78 points, clustered in time.

So the honest form of the conclusion is: **the fat tail does not reach POP under the volatility regimes in this sample, and the model's body conservatism gives real margin.** It is not a proof that a genuine crisis gap cannot exceed a defined-risk spread's max loss — and it does not need to be, because max loss on a defined-risk spread is bounded and known in advance. That is what "defined risk" buys, and it is why this finding cannot become a risk-of-ruin problem the way an undefined-risk position could.

## §C — One finding I did not expect, and would not build on

The **upside** tail is fatter than the downside at every horizon, and stays fat where the downside normalizes:

| horizon | downside 1% (emp − norm) | upside 1% (emp − norm) |
|---|---|---|
| overnight | −0.538 | −0.476 |
| 1w | −0.266 | −0.291 |
| 1m | −0.048 | **−0.283** |
| 35d | **+0.067** | **−0.335** |

That inverts the usual equity-skew story, and it is the benign direction for a put seller — the fat tail is the profitable one. I predicted the opposite and was wrong.

**I would not build on it.** The sample covers the 2023–2026 megacap melt-up with NVDA, AMD, TSLA and MSFT in it, and a fat upside tail is the signature of that specific period rather than a durable property. Recorded because it is the honest output; flagged because it is the kind of result that becomes a thesis if nobody labels it sample-specific.

---

## §D — What this changes

**Nothing about sizing, and that is the result.** No gate, floor, delta band, or POP calculation should move on this. The specific chain — fat tail → understated max-loss probability → optimistic POP → sizing implication — breaks at the second link.

**The overnight band finding stands unchanged and stays scoped to the instrument.** The 99% gap band still under-covers, `GAP_MEASURED_COVERAGE` still records the real curve, and the 0.95 default is still calibrated only on a coincidence. That was always a statement about a measurement channel; it is now measured not to be a statement about the book.

**The quantile mapping's status improves.** It was held back partly because shipping a body fix while the tail was open would be unsafe. The tail is now measured not to reach the trade horizon, so a body fix is not papering over a live risk — it is fixing the only part that was broken. The two conditions you set still hold: test it at 99% before calling it tail-safe, and audit the 250-window's far-tail thinness. But the reason to wait has weakened.

---

## §E — Open items by consequence

1. **The next scan's `quote_absent` vs `quote_spread_too_wide` split.** Now the only live drought question. Readings **pre-registered in `VEGA_METHODOLOGY.md`** before the numbers arrive, per your note: wide → the market is thin and selection is correctly refusing, and the follow-on is the 0.80-vs-0.35 threshold mismatch in the health metric; absent → a one-sided book, a fetch-path question first; crossed → a broken feed. No floor gets loosened on any outcome.
2. **Log the CRWD and QQQ trades.** Unchanged, and now the only thing standing between the project and its first fill-verified ground truth.
3. **Empirical quantile mapping** — costed, body-only, tail condition now partially discharged.
4. **Chain-quality predicate decision** (0.80 health vs 0.35 selection).
5. **Confirm production overnight coverage** from 2026-09-07.
6. Everything else unchanged.

---

## §F — What I would not do

**1. I would not have run this measurement first if I had checked the geometry first.** Ten minutes reading the ledger's OTM distribution would have shown the max-loss boundary sits at ~0.8 sigma, and the entire escalation would have been visibly misdirected before either of us wrote a paragraph about risk of ruin. The Level 0 question — *does the thing I am worried about live where I am worried about it?* — was available and cheap, and neither of us asked it. That is the same failure you named on the HAR blend, one level up: reasoning about a mechanism's consequences before confirming the mechanism operates in the relevant region.

**2. I would not describe the tail finding as retracted.** It is correct and correctly scoped: the overnight band's 99% level under-covers, and that matters for anything that reads the band as a probability statement. What is retracted is the *inference* from it to POP and sizing — mine, and yours, made independently and in the same direction.

**3. I would not let the conservatism become an argument for loosening.** "The model overstates breach probability by 3 points" is exactly the shape of finding that becomes "so we can sell closer to the money." It should not. The conservatism is measured on a benign sample, it is the margin that makes a defined-risk book survivable, and the drought is not caused by strikes being too far out — it is 68% quote and liquidity failures at the strikes the strategy already wants.
