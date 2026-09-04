# VEGA — The tail test, the c4 audit, and two dead readers

**Date:** 2026-09-04
**Branch:** `session/2026-09-03-prediction-engine`
**Scope:** 99% falsification, c4 audit across every realized-vol site, `open_positions.json` reader sweep, direction-channel retirement, and a costing of the empirical quantile mapping.

---

## §A — The tail test confirms fat tails, and the corrected prediction held

Your reformulation was the right test. Predictions stated in the script before the numbers existed; both held.

| claimed | n | actual | 95% CI | error | verdict |
|---|---|---|---|---|---|
| 80.0% | 7,936 | 85.71% | [84.79, 86.66] | +5.71 | over-covers |
| 90.0% | 7,936 | 92.07% | [91.44, 92.73] | +2.07 | over-covers |
| 95.0% | 7,936 | 95.02% | [94.52, 95.52] | +0.02 | calibrated |
| **97.5%** | 7,936 | **96.52%** | [96.11, 96.91] | **−0.98** | **UNDER-covers** |
| **99.0%** | 7,936 | **97.49%** | [97.14, 97.78] | **−1.51** | **UNDER-covers** |

And the quantile table shows exactly the structure:

| quantile | 50% | 68% | 80% | 90% | **95%** | 97.5% | 99% | 99.5% |
|---|---|---|---|---|---|---|---|---|
| empirical `\|z\|` | 0.494 | 0.779 | 1.071 | 1.491 | **1.959** | 2.586 | **3.952** | **5.484** |
| normal | 0.674 | 0.994 | 1.282 | 1.645 | **1.960** | 2.241 | 2.576 | 2.807 |
| emp − norm | −0.180 | −0.216 | −0.211 | −0.154 | **−0.001** | +0.345 | **+1.376** | **+2.677** |

Sharp peak, thin shoulders, crossover at **1.959 vs 1.960** — the 95% level to three decimal places — then a tail that runs 53% wider at 99% and 95% wider at 99.5%. Textbook leptokurtosis, and my original mechanism survives; only my claim about *where it inverts* was wrong, and only because the crossover happens to land on the level I chose as the test.

### Two consequences that matter more than the calibration question

**1. The 95% band being calibrated is a coincidence, not a property.** It sits precisely where the body's over-coverage and the tail's under-coverage cancel. That is a fragile place to operate: any change to the variance-share estimate, the vol forecast, or the underlying mix moves the crossover and the calibration goes with it. It is a further argument against defaulting to 0.95 — the level is right today for a reason that is not robust.

**2. The tail under-covers, and for a premium seller that is the direction that matters.** A band claiming 99% delivers 97.5%: the "1-in-100" adverse gap arrives roughly **1-in-40**. Empirical `|z|` at 99% is 3.95 against an assumed 2.58, so the lognormal understates the far gap by ~53%. That bears directly on short-strike selection and on POP calibration, both of which are built on the same distributional assumption — and it is the first result in this sequence that argues the model is *unsafe* rather than merely miscalibrated.

### The empirical quantile mapping, costed as asked

Estimated on that ticker's prior gaps only (rolling 250, minimum 150), applied to size the band for the **next** open. Out of sample by construction — the estimation window never sees the row it scores.

| level | method | n | coverage | 95% CI | error |
|---|---|---|---|---|---|
| 80% | normal z | 15,872 | 86.27% | [85.60, 86.94] | **+6.27** |
| 80% | **empirical q** | 13,472 | **79.90%** | [78.96, 80.77] | **−0.10** |
| 95% | normal z | 15,872 | 95.12% | [94.71, 95.47] | +0.12 |
| 95% | empirical q | 13,472 | 94.66% | [94.24, 95.09] | −0.34 |

**It fixes the 80% band completely and out of sample** — +6.27pp to −0.10pp, CI straddling 80 — and leaves 95% essentially unchanged, which is what you would expect where the normal was already right.

**Cost:** ~40 lines in `band_forecast`, one rolling deque per ticker, no new dependency, no change to `project()`. **Caveats:** it needs 150 prior gaps, so ~15% of anchors abstain during warm-up (fine in production, which fetches a year); and I did **not** test it at 99%, so whether it also repairs the tail is unmeasured — that is the test to run before treating it as a tail fix rather than a body fix.

**Built: no.** You asked for a costing and the number is the whole answer: this is cheap, principled, and clearly better than the default change it replaces.

---

## §B — c4 audit: no production site is exposed

Every annualized realized-vol computation in the codebase, with its window:

| site | window (obs) | c4 | bias | used for |
|---|---|---|---|---|
| `technicals._historical_vol(close, 20)` | 20 | 0.9869 | 1.31% | rv_20 |
| `technicals._historical_vol(close, 30)` | 30 | 0.9914 | 0.86% | rv_30, IV ceiling, ticker_profile |
| `technicals` rolling rv_series | 30 | 0.9914 | 0.86% | IV plausibility filter |
| `vega_candidates` `VRP_HV_WINDOW` | **35** | 0.9926 | 0.74% | **the VRP calculation** |
| `technicals._historical_vol(close, 126)` | 126 | 0.9980 | 0.20% | HV percentile |
| `band_forecast` recent / long-run | 20 / 120 | 0.9869 / 0.9979 | 1.31% / 0.21% | the band, the forecast |
| `predictions` crypto_vrp scorer | 30 | 0.9914 | 0.86% | grades crypto claims |
| `crypto_vol_forecast` | 30 | 0.9914 | 0.86% | IBIT/BTC fit |

**The shortest window anywhere in production is 20 observations — 1.31% bias, 0.26 vol points on a 20-vol base.** The severe small-df cases (4.04 pts at n=2, 2.28 at n=3, 1.20 at n=5) exist only in my analysis harness, which is where the +2.33 artifact came from.

### Your HAR hypothesis does not apply to the equity path

**The equity forecaster is not HAR.** `vol_forecast.forecast_rv` is a two-component mean-reversion blend of a 20-day recent and a 120-day long-run. HAR-style 1/5/21 windows appear only in `crypto_vol_forecast`, and that module computes its realized vol on **w = 30**, not on a 5-day component. So there is no short-window component feeding the equity blend, and the regime-dependent distortion you hypothesised has no mechanism here.

### Your VRP concern is real, tiny, and points the safe way

Both inputs are biased **down** (recent by 1.31%, long-run by 0.21%), so de-biasing raises the forecast — which pushes VRP **further negative**, strengthening the calls rather than weakening them:

| ticker | recent | long-run | forecast | c4-corrected | Δ | VRP | VRP corrected | flips? |
|---|---|---|---|---|---|---|---|---|
| AAPL | 18.78 | 27.74 | 22.81 | 22.97 | +0.16 | −3.10 | **−3.26** | no |
| META | 31.20 | 44.63 | 37.24 | 37.51 | +0.27 | −7.30 | **−7.57** | no |

**Boundary width is 0.07–0.27 vol points**, so a name flips only if its VRP already sits inside that sliver of zero. Nothing in the 09-03 scan does. The correction is worth having for correctness but changes no verdict, and it cannot rescue a trade.

---

## §C — Two dead readers, both closed; your predicted third does not exist

`open_positions.json` has never existed. It had exactly **two** readers, not three:

1. **`_get_open_position_tickers`** — fixed earlier today. It never actually reached the fallback anyway, because the JARVIS path returned phantom rows first.
2. **`compute_decay_alerts`** — read it on every close scan, got `[]`, and therefore **has never fired**. Silently, because an empty alert list is indistinguishable from "nothing has decayed yet." Exactly the fail-open shape you named: missing input → empty → reads as safe.

Now fed from the outcome ledger, with the field mapping (`entry_credit` → `actual_fill_credit` / natural credit; `current_price` → `current_mark`) and **a guard the old path never had: only a LIVE `mark_status` can alert.** AMGN currently sits at `DATA_UNAVAILABLE` with a five-day-stale mark of 0.91; alerting off that would tell you to close a position on a price nobody is quoting, and would contradict `_reprice_and_close_open`, which already refuses to evaluate stop/target in that state.

Live result: 4 open rows, **0 alerts** — correct. Best position is NEE at +44% of max against a 65% target. The path now computes instead of returning empty by construction.

`load_open_positions` is deleted. A test asserts, against the parsed AST with docstrings excluded, that neither the function nor a code reference to the filename can come back.

**Worth stating plainly, because it sharpens the original finding:** the two *dashboard* consumers (`vega_app._reprice_open_positions`, `hedge.book_delta`) were already reading `outcome_logger.load_records()` correctly. **The scan was the odd one out** — it and the dashboard disagreed about what you held, for weeks, and the dashboard was right.

---

## §D — Direction channel retired, with its numbers

`direction_overnight` and `direction_1d` removed from `direction_forecast.HORIZONS`. The sweep drops from 432 to **216 claims/day**.

The numbers are recorded **in the tuple itself**, immediately above the surviving entries, so a future session sees why the gap exists before it fills it — the earnings-gate lesson. Also added to `VEGA_METHODOLOGY.md`.

Recorded verbatim in both places, including the asymmetry:

> **1w and 1m are kept, and the asymmetry is deliberate and uncomfortable:** the two horizons being retired are the two that are PROVEN worthless, and the two being kept are UNPROVEN. `direction_1w` sits at effective N 16 with resolution 0.0013; `direction_1m` has never resolved a single claim and cannot before 2026-09-24. So the honest statement is that this channel currently has zero demonstrated skill at ANY horizon, and the horizon closest to the 35-DTE trade window has not spoken yet.

Claim types and scorers stay in `predictions`, so the 1,344 historical rows remain gradeable and restoring a horizon is a one-line change.

A second methodology rule came out of this: **read a hit-rate gap only next to the decomposition.** 18.5% vs 64.6% reads as "the tilt is actively harmful" and is not — the two predict different categories, Brier favours the tilt, and both have resolution 0.000. The hit rates alone would have supported a confident wrong conclusion, and nearly did.

---

## §E — The chain-quality mismatch, diagnosed as you called it

Your reading is right and the mechanism is one notch more specific than "different predicates."

`_option_record_is_quotable` rejects only: both sides zero; crossed *when both are positive*; or spread > **0.80** of mid. Selection's `_quote_is_tradeable` requires bid > 0 **and** ask > 0 **and** spread ≤ `MAX_QUOTE_SPREAD_PCT` = **0.35**.

So the divergence is **two different spread thresholds on the same strike — 0.80 for health, 0.35 for selection** — plus health tolerating a missing bid. A strike quoting 0.40/0.90 is "healthy" and unsellable. `measure_tradeable_band_quality` already restricts the metric to the 0.12–0.30 delta band, so the strike *sets* now largely agree; it is the *predicate* that does not.

The codebase names the exact case in its own docstring — *"one-sided markets (bid 0.00 / ask 8.30 on LMT 425)"* — as the reason the whole-chain ratio was abandoned for the band ratio. The band ratio fixed the strike set and left the threshold gap untouched.

**You are right that this makes it a choice, not an investigation.** Either the quality predicate becomes the selection predicate, or the metric is renamed so it stops implying tradeability. I did neither: it is a selection-adjacent config decision mid-drought, and my `quote_absent` / `quote_spread_too_wide` split measures which of the two thresholds is actually doing the damage before anyone picks. **The next scan answers it** — if the 501 land mostly in `quote_spread_too_wide`, the answer is the 0.35/0.80 gap; if mostly in `quote_absent`, it is the missing-bid clause.

---

## §F — Open items by consequence

1. **Log the CRWD and QQQ trades.** Unchanged.
2. **Read the next scan's `quote_absent` vs `quote_spread_too_wide` split** — the last live drought candidate, and it resolves by observation.
3. **Decide the chain-quality predicate** (§E): unify with selection, or rename the metric.
4. **The tail under-coverage is a risk finding, not a calibration one** (§A2). Short-strike selection and POP rest on the same lognormal that understates the 99% gap by 53%. This is now the most consequential open item in the modelling stack.
5. **Empirical quantile mapping** — costed, clearly worth it, not built. Test it at 99% before treating it as a tail fix.
6. **Confirm production overnight coverage** from 2026-09-07.
7. **Retire `direction_1w`/`1m` on the same rule** once they reach effective N past the floor.
8. `MIN_IV_RANK` under `APPROX`; two `iv_rank` fields; `--mark-only` / retention; `n_effective` not variance-corrected.
9. Rotate the NewsAPI key; delete the three stale branches; decide `ENTRY_HOLD`.

---

## §G — What I would not do

**1. I would not adopt 0.95 as the overnight default even now that it measures calibrated.** §A1 is the argument: it is calibrated because it sits exactly on the crossover where two opposite errors cancel. That is not a property to build on, and the quantile mapping in §A3 gets 80% right for a reason that will survive a change in the underlying mix.

**2. I did not build the quantile mapping.** You asked for a costing and I think the instruction was right — but I want to be explicit that the number came back strong enough that "cost it" and "do it" are close, and I stopped at the boundary you set rather than because the evidence was ambiguous.

**3. I did not unify the chain-quality predicate with the selection predicate,** despite §E making the mismatch concrete. It changes which underlyings the scan will look at, mid-drought, on the basis of a threshold argument rather than a measurement. The split I shipped measures which threshold is responsible first. That ordering is deliberate.

**4. I would flag one thing in your framing.** You wrote that the c4 finding "is written up as a yardstick correction for one analysis" and is bigger than that. It is bigger — but the audit came back with no production site under 20 observations, so the honest conclusion is that it was **contained to the analysis harness**. That is a real result and I would not want it read as "audited and found a systematic tilt" when it is "audited and found nothing exposed." The finding's value is the *rule* it produced, not a defect it uncovered.

**5. The drought hypothesis space is now closed except for one item, and I would resist reopening it by argument.** Seven causes have died to measurement. What remains is §F2, which the next scheduled scan answers on its own. If that comes back "the market", the correct conclusion is that there is no premium and the band channel is the evidence engine — and I would rather record that plainly in September than have it discovered in October.
