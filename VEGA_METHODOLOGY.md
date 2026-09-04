# VEGA — Analysis Methodology

Durable practice, not a checklist to be admired. Everything here exists because this project
already paid for it once.

---

## 1. The identity-vs-value test

Before inferring that a contaminated input produces a **directional** effect on a downstream
gate, ask one question:

> Is the downstream system operating on the **same object** with a changed **value**, or can the
> changed input cause it to select a **different object**?

If anything between the input and the gate performs an `argmax`, a threshold crossing, clustering,
nearest-neighbour selection, pivot re-identification, ranking, or a fallback branch — **do not
infer aggregate directionality without measuring it.**

**Where this was paid for.** `main.py:543` read technical levels in adjusted price space and
compared them against raw strikes. Adjusted lows genuinely sit below traded lows, and dividend
payers are most of the universe, so a one-directional squeeze on `support_shelter` looked
certain. Measured result: **+3 of 544**. Level *re-detection* swamped level *drift*, and
re-detection is roughly symmetric — a shifted series does not merely move each pivot, it changes
which bars are pivots at all. The mechanism was real and the prediction was still wrong.

---

## 2. Definition-first preflight — Levels 0-2

Mandatory before any analysis. Three levels, not seven: a longer checklist gets skipped as
ceremony, and these are the three this project has actually missed.

### Level 0 — Population
What rows are *supposed* to exist? What is the unit of observation? When does a row enter the
dataset? What exclusions occur, and **can an exclusion depend on information from the future?**

> "All enumerated candidate spreads" and "all rows where scoring happened to run" are different
> populations. Naming which one you mean is most of the work.

### Level 1 — Variable definition
Is the variable defined for **every** row? Is missingness structural? Is missingness correlated
with the dependent variable? And the question that matters most here:

> **Is the variable itself downstream of the gate being evaluated?**

**Where this was paid for.** `edge_score` was computed inside `if all(g.values())`. Measured
2026-09-02 over 2,726 counterfactual rows: 331 carried an `edge_score` and **every one of them
had passed all gates**; 2,341 rows that failed a gate carried none. `edge_score is present` was
a perfect proxy for `all gates passed`. A threshold test on that sample could not have answered
the threshold question, and would have produced a clean-looking number on a sample structurally
incapable of bearing it — which would then have moved a live gate.

Three rounds of guards preceded that test, each aimed at sample properties: clustering, proxy
labelling, range restriction. Right instinct, wrong layer. **None of them asked whether the
variable existed on both sides of the gate.**

### Level 2 — Execution-path validation
Does the code path that supposedly creates this measurement **actually run in production?**

Catches: dead subtrees (`_pick_new_trades` computes `slots` and looks exactly like the live gate;
it sits under a comment reading *"NOTHING BELOW IS REACHABLE from the cycle"*), config that
exists but is never consumed, gates fixed in an orphaned copy while the live path does the
opposite, and flags whose short-circuit position is assumed rather than traced.

Reading the code is not Level 2. Runtime evidence is: an artifact on disk, a log line with a
timestamp, a coverage profile, a counter that moved.

---

## 3. Self-validating instrumentation

**The anti-pattern:** a system using an internally contaminated population to verify its own
completeness. The numerator and the denominator come from the same fetch, so the metric
structurally cannot detect the failure it exists to detect.

The general defence is an **independent denominator** — a signal about the *process* rather than
about the *records the process returned* — and it must be consulted **before** the ratio, not
alongside it.

**Worked example, both halves.** `measure_chain_quality` computes `usable/raw` where both come
from the same `records` list, so a truncated page walk shrinks numerator and denominator together
and reads as healthy. That is real. The defence is `_truncated_walks`: the fetch layer observes
that *pagination ended early* — a fact about the walk, not about the rows — and
`get_options_chain` refuses the ticker outright before the ratio is ever computed. Independent
signal, consulted first, fails closed.

When auditing for this shape, ask: *what would this metric report if the fetch that produced it
had silently returned half the data?*

---

## 4. Recorded so a future session does not "correct" it back

- **The scheduler fix landed by removing `StopAtDurationEnd`, not by extending `Duration`.** The
  prescribed `PT7H` is moot. The empirical record is eight clean days.
- **A ledger's line count is not its contents.** Diff contents, back up first, pause the cycle.
- **Never trust a fixture-shaped value in production data** — single-letter tickers, `TEST`,
  `NEW`, `ZZZ`, and hostnames like `t.example` / `a.example`. Grep for the fixtures' own
  hostnames before believing a log that agrees with what you hoped.
- **Any new ledger, journal, or log file must be registered in the shared test-isolation helper
  and in the liveness rule in the same commit that creates it.** An autouse guard that enumerates
  known ledgers protects only the paths that existed when it was written.
- **Every build doc gets a "verify this is still true" pass as step one.** A doc written from
  session logs describes the repo at the moment those logs were written, not the repo you are
  about to change. Three of eight items in the 2026-09-03 build doc were already complete when
  it was written; acting on it unchecked would have paused a live grading channel to wait for a
  fix that shipped on 2026-08-10. Confirm before executing. Cost: minutes.
- **A figure quoted INTO a decision carries its effective N inline, and a figure under ~30
  independent blocks cannot be a load-bearing premise.** This is the consumption rule, and it is
  the one that was missing. `cluster_sample()` printed `n_effective = 6` on the same output line
  as the 86.1% coverage figure; the number was read, the effective N beside it was not, and a
  build doc then made "the vol forecast is biased high" its headline task on that basis.
  Producing the uncertainty correctly bought nothing, because the failure was entirely
  downstream of production. When you cite a number you did not just derive, look up its power
  before you build on it — and if you cannot find its power, that is the finding.
- **A coverage, hit rate, or bias figure is quoted with its interval or it is not quoted.** The
  same walk-forward of the same unchanged code reported 1-day band coverage at 78.8% one day
  and 89.7% the next, because 416 claims across 8 correlated names is ~6 independent blocks.
  Those numbers were then read as evidence of a biased volatility forecast and became the
  headline task of the next build doc. `predictions.cluster_sample()` exists to make the
  effective count visible; run the estimate at a sample size that can support the claim, and
  report the interval beside the point estimate.
- **Check that your YARDSTICK is unbiased before concluding the thing you measured is.** The
  "forecast is biased high by +2.33 vol points at one week" finding was 1.72 points of estimator
  artifact: realised vol computed as the square root of an unbiased variance is a DOWNWARD-biased
  estimate of sigma at small sample sizes (Jensen), by ~6% on 5 returns and ~1% on 21. The
  apparent bias decayed with horizon exactly as that correction does. It also explains why the
  bias test and the band-coverage test appeared to contradict each other: coverage never
  estimates a volatility at all, it compares a price to an interval, so it was the test with the
  intact yardstick. Two measurements of the same quantity disagreeing is a fact about one of the
  measurements until proven otherwise.
- **Correcting a measured mis-specification is not tuning; the difference is whether the number
  came from outside the thing being fixed.** The overnight band over-covered because it was
  charged a full session of sigma and graded on the close-to-open gap. The fix estimates the
  overnight share of variance *from prices*, and it was validated by PREDICTING the observed
  over-coverage (96.2% predicted, 97.4% observed) before being applied. Adjusting the same
  parameter until coverage read 80% would have been the curve-fit. Ask: was the correction
  derived from the outcome it is judged on?
- **Level 2 applies to code this process wrote 48 hours ago, and it was not applied.** The
  2026-09-03 session moved the quality log to JSONL append so retention could rise to 120 days,
  and deferred `compact()` to the `--mark-only` run to keep a whole-file rewrite off the hot
  path. The reasoning was right and the commit message argued it carefully. **Nothing schedules
  `--mark-only`** — one Windows task exists and it runs `run_auto_paper_cycle.ps1` — so
  `compact()` has exactly one caller and that caller has never executed. The retention policy
  that justified the format change has no execution path. Ask Level 2 of your own work, not only
  of the code you inherited: *does the path that supposedly runs this actually run in
  production?* Confirm it against the scheduler, not against the source.
- **`direction_overnight` and `direction_1d` were RETIRED on 2026-09-04, and they are absent on
  purpose.** Resolution 0.0000 at an effective N of 96 blocks each -- what shuffling the
  outcomes produces -- against a gradeability floor of 10. They had a fair test and failed it.
  A future session will see an obvious gap where a short-horizon direction forecaster should be;
  the numbers live in `analysis/direction_forecast.HORIZONS` so it is not rebuilt from scratch.
  This project has already paid once for orphaned-but-tested code (the earnings gate). Note the
  asymmetry honestly: what was retired is what is PROVEN worthless, what is kept (`1w`, `1m`) is
  UNPROVEN -- `1m` has never resolved a claim and cannot before 2026-09-24 -- so the channel has
  zero demonstrated skill at any horizon today.
- **Read a hit-rate gap only next to the decomposition.** The tilted and baseline direction
  variants scored 18.5% and 64.6%, which reads as "the signal is actively harmful" and is not:
  they predict different CATEGORIES (the baseline always says "flat", a wide target), Brier
  actually favours the tilt, and both have resolution 0.000. The correct verdict was "neither
  discriminates", and the hit rates alone would have supported a confident wrong conclusion.
- **PRE-REGISTERED, 2026-09-04: what the next scan's quote counters mean.** The 19-of-54
  enumeration block is 501 `quote_not_tradeable` + 240 `liquidity_below_floor` = 68% of all
  rejections, and `_quote_verdict` now splits the first into `quote_absent` / `quote_crossed` /
  `quote_spread_too_wide`. Deciding the reading BEFORE the numbers arrive, so this does not
  become a third round of interpretation:
    - **Mostly `quote_spread_too_wide`** -> the market is quoting wide books on those strikes
      and selection is CORRECTLY refusing them. The drought is the market. The follow-on is
      then the threshold mismatch, not the fetch: `_option_record_is_quotable` tolerates a
      spread up to 0.80 of mid while selection requires 0.35, so the health metric is set more
      than twice as loose as the gate it is supposed to describe, and reporting "54/54 healthy"
      while 15 of them enumerate nothing is the metric's fault, not the market's.
    - **Mostly `quote_absent`** -> a strike with a positive mid and a missing side. Strikes with
      no price at all are already caught one gate earlier as `short_missing_price_delta`, so
      this is specifically a one-sided book. That is a FETCH-PATH question first (does our
      chain carry both sides for these strikes?) and a market question second.
    - **Mostly `quote_crossed`** -> a broken feed. Neither market nor threshold; fix the source.
  Do not loosen `MAX_QUOTE_SPREAD_PCT` or the liquidity floors on any of these outcomes. The
  floors may be right and the metric wrong, which is the whole reason the split exists.
- **Before reasoning about a defect's consequences, check whether the defect LIVES where the
  consequence would be.** The overnight band's 99% level under-covers by 1.5pp, which looked
  like it must reach POP and sizing -- a defined-risk spread takes max loss on a large adverse
  move. It does not. The fat tail is a one-day property that aggregates away by 21 sessions, and
  real spreads take max loss at about -0.8 sigma: ZERO of 2,899 ledger spreads have a max-loss
  boundary beyond -2.576 sigma. Measured against real geometry the model OVERSTATES breach
  probability at every strike location (+3.00pp at the median long strike). Ten minutes reading
  the ledger's OTM distribution would have shown this before a word was written about risk of
  ruin. Level 0 for consequences, not just for definitions: *does the thing I am worried about
  live where I am worried about it?*
