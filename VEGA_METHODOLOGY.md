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
- **A coverage, hit rate, or bias figure is quoted with its interval or it is not quoted.** The
  same walk-forward of the same unchanged code reported 1-day band coverage at 78.8% one day
  and 89.7% the next, because 416 claims across 8 correlated names is ~6 independent blocks.
  Those numbers were then read as evidence of a biased volatility forecast and became the
  headline task of the next build doc. `predictions.cluster_sample()` exists to make the
  effective count visible; run the estimate at a sample size that can support the claim, and
  report the interval beside the point estimate.
- **Correcting a measured mis-specification is not tuning; the difference is whether the number
  came from outside the thing being fixed.** The overnight band over-covered because it was
  charged a full session of sigma and graded on the close-to-open gap. The fix estimates the
  overnight share of variance *from prices*, and it was validated by PREDICTING the observed
  over-coverage (96.2% predicted, 97.4% observed) before being applied. Adjusting the same
  parameter until coverage read 80% would have been the curve-fit. Ask: was the correction
  derived from the outcome it is judged on?
