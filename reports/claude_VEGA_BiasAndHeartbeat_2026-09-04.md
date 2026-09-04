# VEGA — Forecaster bias, band heartbeat, overnight horizon

**Date:** 2026-09-04
**Branch:** `session/2026-09-03-prediction-engine` (commit `e27ea57`)
**Tests:** 1455 passing
**Scope:** §0 premise pass run; Tasks 1–3 executed; carried items surveyed, not owned.

---

## §A — Refutations

### A1. The forecaster is **not** materially biased high at the horizon VEGA trades

This was the handoff's largest open question, and the answer is no — with one real qualification.

Signed forecast error, 16 names, 5 years, every session an anchor, 18,416 paired observations per horizon, 95% CI from a block bootstrap over (ticker, month) units. Positive = the forecast exceeded what subsequently realized.

| horizon | n | forecast error | 95% CI | trailing error | 95% CI |
|---|---|---|---|---|---|
| 1w (5d) | 18,416 | **+2.33** | [+1.93, +2.77] | +1.78 | [+1.36, +2.19] |
| 2w (10d) | 18,416 | **+1.19** | [+0.73, +1.63] | +0.64 | [+0.15, +1.12] |
| **1m (21d)** | 18,416 | **+0.50** | **[−0.08, +1.07]** | −0.05 | [−0.62, +0.55] |
| 2m (42d) | 18,416 | +0.06 | [−0.49, +0.60] | −0.49 | [−1.11, +0.15] |

**At one week the forecast is biased high by ~2.3 vol points and it is significant. At one month — the horizon `PREFERRED_DTE_TARGET = 35±7` actually trades — the bias is +0.50 with an interval straddling zero.**

The regime split (§B3 of the handoff) is where the hypothesis half-holds and half-dies:

| | 1w forecast err | 1m forecast err |
|---|---|---|
| quiet (VIX < 16.3) | **+3.11** [+2.47, +3.77] | +0.56 [−0.19, +1.34] |
| normal (16.3–20.4) | +2.39 [+1.70, +3.00] | +0.30 [−0.46, +1.02] |
| stressed (VIX ≥ 20.4) | +1.49 [+0.80, +2.16] | +0.64 [−0.24, +1.49] |

At 1w the predicted monotone pattern is there — quiet over-forecasts most. At 1m there is no pattern and nothing significant. And **the model never under-forecasts in stressed tapes**, which the handoff's stated hypothesis predicted it would: stressed is +1.49 and +0.64, both positive.

### A2. Substituting trailing vol would make the negative-VRP calls **worse**, not better

The handoff asked (§B4) how many of the five negative-VRP finalist deaths flip if trailing replaces forecast. The arithmetic — `VRP = implied − vol`:

| ticker | implied | trailing | forecast | VRP (forecast) | VRP (trailing) | |
|---|---|---|---|---|---|---|
| AAPL | 19.82 | 18.78 | 22.92 | −3.10 | **+1.04** | flips positive |
| META | 30.13 | 31.20 | 37.43 | −7.30 | −1.07 | still negative |

**One of two tickers flips (one of five spreads — META accounted for four).** Taken alone that looks like support for the hypothesis. It is not, for two reasons.

**First, META needs no forecaster at all.** Its implied of 30.13 is **below its own trailing realized of 31.20**. The market is charging less than the stock has actually been delivering over the last twenty sessions. No choice of volatility estimator rescues that trade.

**Second — and this is the decisive test — the forecast is right precisely when it disagrees most with trailing.** AAPL and META on 09-03 sat at forecast−trailing of **+4.14** and **+6.23**, far above the watchlist norm, because the mean-reversion term was pulling hard toward long-run levels (AAPL 27.74, META 44.63). Bucketing all 18,416 one-month observations by that same gap:

| forecast − trailing | n | forecast error | 95% CI | trailing error | better |
|---|---|---|---|---|---|
| < −1.30 | 4,600 | +3.78 | [+2.62, +4.88] | +7.95 | forecast |
| −1.30 … +0.61 | 4,608 | −0.51 | [−1.23, +0.21] | −0.21 | trailing |
| +0.61 … +2.57 | 4,599 | −1.17 | [−1.81, −0.55] | −2.69 | forecast |
| **+2.57 … +4.93** *(AAPL)* | 2,767 | **−0.71** | [−1.63, +0.13] | −4.25 | forecast |
| **> +4.93** *(META)* | 1,842 | **+0.82** | [−1.41, +2.95] | **−6.73** | forecast |

**In the exact state these names occupied, the forecast is unbiased and trailing under-forecasts realized vol by 4 to 7 volatility points.** AAPL's "flip" is not a recovered trade; it is a false positive produced by swapping in an estimator that is badly wrong in that regime. The mean-reversion term firing hard is the model working, not the model inflating.

**The drought's negative-VRP verdicts survive their most obvious alternative explanation, and are now considerably better supported than they were.**

### A3. The band over-coverage that motivated the whole hypothesis was sampling noise

The handoff's opening premise: *"1w covers 86.1%, 1m covers 86.1%, against a claimed 80%… the same upward bias produces both symptoms."*

Those figures came from a walk-forward of 8 names at every seventh session — 416 claims per horizon, which `cluster_sample()` scores at **~6 independent blocks**. Re-running that identical harness on identical code one day later, with one extra bar of data, moved **1d coverage from 78.8% to 89.7%**. No code changed. Six blocks cannot distinguish 79% from 90%.

Properly powered — 16 names, 5 years, every session an anchor, 15,552 observations per horizon, block bootstrap over 768 (ticker, month) units:

| horizon | coverage | 95% CI | verdict |
|---|---|---|---|
| 1m | **80.7%** | [78.9, 82.4] | calibrated |
| 1w | **81.2%** | [80.2, 82.2] | calibrated |
| 1d | 82.8% | [82.2, 83.4] | ~3pp wide |
| overnight | 86.2% | [85.5, 86.9] | ~6pp wide, after the fix in §B |

**1w and 1m are calibrated.** There is no over-coverage at those horizons to explain, so §B5 of the handoff ("does the bias magnitude quantitatively match the band over-coverage?") has no premise left: the bias is ~0 at 1m and the over-coverage is ~0 at 1m. Two symptoms that were supposed to be one defect turn out to be one artifact and one real, separate, construction error.

I produced the 86.1% numbers yesterday and quoted them without an interval in the module docstring and the report. They have been replaced there, and `VEGA_METHODOLOGY.md` now carries the rule.

### A4. Task 3's timing premise is off by one trading day

The handoff: *"First production settlement is 2026-09-04 — today. The evidence starts arriving now."*

Half true. The 09-03 overnight and 1d claims carry `score_on = 2026-09-04` — they settle today. But `resolves_on = 2026-09-07`, by deliberate design: `claim_dates()` defers resolution one trading day past settlement because the settling bar must be complete before resolution runs, and the desk's last cycle fires before the close. 09-05 and 09-06 are the weekend.

**No production overnight coverage can exist today, and none of the 432 open band claims is gradeable.** Nothing was lost — Task 3's substance was the interval mismatch, which needed no production rows to diagnose — but the calendar pressure the handoff put on the ordering was not real.

### A5. `--mark-only` has never run because nothing schedules it

Confirmed from the scheduler, not inferred: exactly one VEGA task exists.

```
VEGA_AutoPaper_2Weeks   Ready   Last 2026-09-03 14:35:35   Result 0   Next 2026-09-04 08:35:35
  powershell.exe … -WindowStyle Hidden -File …\run_auto_paper_cycle.ps1
```

`run_auto_paper_mark.ps1` exists on disk and **no scheduled task invokes it**. Per instruction I did not create one.

---

## §B — Task 3 result: the overnight band

### Diagnosis

`predictions._settle_price` settles the overnight claim on the **open** of the settling bar — deliberately and documented, because the claim is about the close-to-open gap. The band was drawn with a **full trading session** of sigma. Charged for one interval, graded on a shorter one.

This is not a mis-tuned parameter and it is not fixable by forecasting better.

### The fix, and why it is a correction rather than a tuning

`overnight_variance_share()` measures, per ticker, from prices alone:

```
var(log(open_t / close_t-1)) / var(log(close_t / close_t-1))
```

Across the 54-name watchlist this runs **0.172 (PFE) to 0.637 (TLT), median 0.382** — far too wide a spread to replace with a pooled constant. The volatility is scaled by its square root.

**The test that separates this from curve-fitting: the share was measured first and used to predict the defect's size before the fix was applied.** A band charged a full session but graded on a share `s` of it behaves like a band at `z/√s`. For the eight walk-forward names that is z = 1.28 → ~2.06, predicting **96.2%** coverage on an 80% claim. Observed uncorrected coverage: **97.4%**. Agreement to 1.2pp from a number derived only from prices, with no reference to any coverage statistic.

### Result

Controlled A/B, identical data and anchors, correction off then on:

| horizon | correction OFF | correction ON | delta |
|---|---|---|---|
| 1d | 89.7% | 89.7% | 0.0 |
| 1w | 85.3% | 85.3% | 0.0 |
| 1m | 84.4% | 84.4% | 0.0 |
| **overnight** | **97.4%** | **90.1%** | **−7.3** |

Close-settled horizons are bit-identical — asserted directly in `test_the_correction_touches_only_open_settled_horizons`. On the properly powered run the corrected overnight coverage is **86.2% [85.5, 86.9]**.

### The residual, left alone deliberately

86.2% is still ~6pp above the claim. That residual is **distributional shape, not scale**: gap returns are markedly leptokurtic, and a lognormal interval sized at ±1.28σ contains more than 80% of a fat-tailed, sharp-peaked distribution. Fixing it means changing the distributional assumption, which is a much larger change than this channel has earned and which would affect every horizon.

**I did not shrink sigma further to reach 80%.** The first correction earned its place by predicting the error in advance; a second adjustment chosen because 86.2 ≠ 80 would be exactly the curve-fit the first one avoided. The honest description of the overnight horizon today is *usable and known ~6pp conservative*, and that is what the module says.

Retirement was on the table (§D4 of the handoff) and is not warranted: at 86.2% with a measured, principled construction, the horizon carries information and is gradeable.

### Behavioural change worth knowing

**Without opens, the overnight horizon now abstains** rather than falling back to a full-session band. A silent fallback to a band known to be wrong is worse than a visible hole in the `abstained` count. `record_watchlist` supplies opens from the same fetch it already makes, so production is unaffected; the abstention shows up in the sweep line and in the liveness `horizons` list if a data path ever stops delivering opens.

---

## §C — Task 2 result: the heartbeat

### The staleness check is the half that matters

Every existing check asks whether the sweep produced anything **when it ran**. None of them can see the sweep **no longer being called** — a scheduler entry that stops firing, a flag flipped off, an exception swallowed one frame up. That failure leaves a ledger full of perfectly healthy rows that simply stop arriving, which is the precise shape of how this channel spent its entire life empty while `prediction_ledger` reported `OK`.

`_check_band_forecasts` now computes weekdays since the newest band claim and returns `CRITICAL` past a tolerance, **before any other verdict** — a stale channel cannot report `OK` on the strength of old resolved rows (`test_staleness_outranks_every_other_band_verdict`).

Counted in **weekdays with a tolerance of 2** (`BAND_STALE_MAX_WEEKDAYS`), not calendar days:
- a Friday write is not stale on Sunday;
- `next_trading_day` does not model holidays, and one market holiday costs one weekday, so the tolerance absorbs it. A CRITICAL that fires every Labor Day is a CRITICAL nobody reads by November — which is how the `shadow_book` message stopped being believed;
- a genuinely missed session still trips: Thursday write, dead Friday, holiday Monday, checked Tuesday = 3 weekdays.

### The per-sweep self-audit

Written to `output/paper_desk/auto_paper_cycle.log`, where the operator already reads:

```
BAND FORECAST recorded=432 claims  tickers=54/54 abstained=0 failed=0
  no_opens=0 with_implied=0  ledger_rows=432 newest=2026-09-04 non_finite=0
```

Three conditions raise a named `!!! MEASUREMENT CHANNEL CRITICAL` line at the same volume as the liveness banner:

1. **Zero claims with a populated watchlist** — broken, not starved. An empty watchlist reports `STARVED` with its reason instead.
2. **Any row carrying a non-finite value** — `json.dumps` emits a bare `NaN`, which is not valid JSON, so those rows are unreadable by any strict parser.
3. **The sweep raising at all** — previously this meant the channel stopped quietly while the rest of the cycle reported success.

Both directions are tested (7 new tests in `tests/test_liveness.py`), including that an unparseable `made_at` never produces a false CRITICAL.

### `--mark-only`

Status reported, **no scheduler change made**. One consequence is worth recording: `data_quality_log.compact()` has exactly one caller (`auto_paper_cycle.py:183`) and it is gated to the `--mark-only` path, so **retention has never executed and has no other enforcement route**. The file currently sits at 5,000 rows — residue of the *old* `MAX_ROWS = 5000` cap, not an active limit; the cap is now 400,000. So nothing is at risk today, and nothing will trim it either. This is an operator decision about the scheduler and the scheduler is this project's most expensive failure surface.

---

## §D — Task 1 result

Reported in full in §A1–A3. Summary of the four questions the handoff posed:

1. **Signed forecast error per horizon** — +2.33 (1w), +1.19 (2w), +0.50 (1m, CI straddles zero), +0.06 (2m).
2. **Trailing as null model** — +1.78 (1w), −0.05 (1m). Trailing is *less* biased pooled, and **much worse conditionally** (−6.73 in the high-gap bucket where the disputed calls sat).
3. **Regime split** — real and monotone at 1w (quiet +3.11 → stressed +1.49), absent at 1m. The model never under-forecasts in stress, contrary to the stated hypothesis.
4. **VRP recomputation** — AAPL flips to +1.04, META stays −1.07. The flip is a false positive; META's implied is below its own trailing realized.
5. **Quantitative match to band over-coverage** — no match possible: with a properly powered estimate there is no over-coverage at 1w or 1m to match.

**Effective sample sizes.** Every figure above is bootstrapped over (ticker, month) blocks — 896 units for the bias tables, 768 for coverage — not over the raw 18,416. The raw counts are reported beside the intervals throughout, never alone.

**Not tuned.** No change was made to `vol_forecast`. The 1-week bias is real and significant, and adjusting the reversion coefficient to remove it on the sample that measured it is exactly the failure this task was constrained against.

---

## §E — New findings

1. **A 1-day forecast-error test is not constructible and silently returns nothing.** Annualized realized vol over a single session needs at least two returns; the first run of the bias harness produced zero rows for the 1d bucket and crashed on the empty mean. Any future "1d vol accuracy" measurement is a category error — the 1d *band* is gradeable (one price, one interval), the 1d *vol forecast* is not.

2. **The forecast is far better conditioned than trailing, which the pooled numbers hide.** Across the five gap buckets at 1m, forecast error spans −1.17 to +0.82; trailing spans +7.95 to −6.73. Pooled, trailing looks *less* biased (−0.05 vs +0.50). That pooled comparison is close to meaningless and would have supported exactly the wrong conclusion.

3. **Long-run vol was far above trailing across the watchlist on 09-03** (AAPL 27.74 vs 18.78; META 44.63 vs 31.20; SPY 13.87 vs 7.38). This is what made the mean-reversion term fire so hard, and §A2 shows it fired correctly. It also means the current tape is unusually quiet *relative to these names' own two-year history*, which independently corroborates the low-vol reading.

4. **The overnight variance share is a stable, strongly ticker-specific quantity** (0.172 PFE → 0.637 TLT) with no obvious sector logic — WMT 0.29 against KO 0.39, MSFT 0.50 against AAPL 0.40. Worth knowing before any future work assumes a pooled gap constant.

5. **`data_quality_log.jsonl` sits at exactly 5,000 rows**, the fingerprint of the retired `MAX_ROWS = 5000` cap rather than of any live limit (§C).

---

## §F — Open items, by consequence of failure

1. **Log the CRWD and QQQ trades.** Still zero rows with `source: "manual"`. The only fill-verified ground truth in the project, still unrecorded. Highest value, zero risk, unblocked.
2. **`"No valid same-expiration credit spread found"` — 19 of 54 tickers.** Unchanged and unowned; still the largest unexplained block in the funnel and the next investigation.
3. **Confirm the corrected overnight coverage on production rows** from 2026-09-07, when the first claims resolve. Target is the walk-forward's 86.2%; a materially different production number means the live data path differs from history, which is the thing no backtest can see.
4. **The direction channel needs a decision.** Resolution ≈ 0.000 at every horizon, 3,000+ claims deep. Task 1 removes the excuse for deferring it — the vol model underneath it is sound at the trade horizon, so the direction channel's lack of skill is its own, not inherited.
5. **`MIN_IV_RANK` under `APPROX`** — still a warning rather than a block below the 30-clean-sample floor, on a ~40-sample base with ~2.5pp of resolution. Needs a decision, not a change.
6. **Two `iv_rank` fields share a name and are not the same quantity.** Unowned.
7. **`--mark-only` / retention** (§C) — operator scheduler decision.
8. **`n_effective` is not variance-corrected.** This session leaned on it hard and it earned its keep — it correctly predicted that the 86.1% figures were noise before the powered run confirmed it. Replace before any decision boundary rests on it.
9. **Rotate the NewsAPI key**; **delete the three stale branches**; **decide `ENTRY_HOLD`**.
10. Standing: the 09-15 resolution-agreement check; the chain-size absolute floor.

---

## §G — What I would not do

**1. I would not have ordered Task 3 first.** The handoff put it ahead of Task 1 on a calendar argument that turns out not to hold (§A4) — nothing was gradeable today by design. Task 1 was both more important and unblocked, and it retroactively dissolved a third of Task 3's stated motivation: the "1w/1m over-cover by 6pp" premise was noise, so the only real over-coverage was the overnight one, which was a construction error diagnosable in an hour without any production rows at all. Correct order was Task 1, then Task 3, then Task 2.

**2. I did not tune the forecaster, and I would resist doing so on the 1-week bias.** +2.33 vol points at 1w is real and significant. But VEGA does not trade a one-week horizon, the bias is ~0 where it does trade, and the conditional test shows the reversion term earning its keep in exactly the states where it looks most aggressive. Shrinking reversion to fix a horizon the system does not trade would degrade the horizon it does.

**3. I did not shrink the overnight band further to reach 80%.** §B explains why: the first correction earned its place by predicting the error before being applied, and a second adjustment justified only by the remaining gap would be the curve-fit the first one avoided.

**4. I did not create a scheduled task for `--mark-only`,** as instructed, and I agree with the instruction.

**5. I would treat the 09-03 report's walk-forward numbers as retracted, not merely superseded.** I generated them, quoted them without intervals, and they became the headline premise of this handoff. They are corrected in the module docstring, in `VEGA_METHODOLOGY.md`, and here. The general lesson is stronger than the specific fix: `cluster_sample()` was already in the codebase and already reported `n_effective = 6` next to those numbers *on the same line*, and I quoted the raw figure anyway.

**6. One thing in this handoff I think is mis-framed.** §E5 says the JARVIS/local store divergence *"suppresses entries through the operator"* and should outrank its "display-only" classification. I agree it belongs on the list and that the priority argument is right. But the fix framing is not: the divergence is that book-awareness reads the JARVIS tower's 399-row phantom book while the cohort reads the local ledger. Reconciling the stores is the larger job; **the immediate correctness fix is to make book-awareness read the same store the cohort counts from**, which is one function (`main.py:1340`) and removes the phantom rows from the operator's view entirely. That is a small, safe change I did not make only because it was not in the three scoped tasks — and I would put it above item 2 in §F.
