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

## 2. Definition-first preflight — Levels 0-3

Mandatory before any analysis. Four levels, not seven: a longer checklist gets skipped as
ceremony, and these are the four this project has actually missed.

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

### Level 3 — Locality of the consequence
**Does the thing you are worried about LIVE where you are worried about it?** Geometry first,
consequence second. Added 2026-09-04 after this check caught two confidently-argued escalations
in forty-eight hours, both of which had reasoned from a mechanism to its consequences without
confirming the mechanism operates in the relevant region.

- **The HAR blend that was not there.** An argument that a 5-day realised-vol component was
  biasing the equity forecast, built on a model VEGA does not use. The equity forecaster is a
  20/120 mean-reversion blend; HAR windows exist only in `crypto_vol_forecast`, which computes
  on w=30. One grep would have ended it.
- **The tail that spreads never reach.** The overnight band understates the far gap by ~53% at
  the 99% level, which looked like it must reach POP and sizing. Real spreads take max loss at
  about **-0.8 sigma**: zero of 2,899 ledger spreads have a max-loss boundary beyond -2.576
  sigma. Ten minutes reading the ledger's OTM distribution would have ended it.

The check is cheap and it is nearly always available before the first paragraph is written:
locate the defect on the same axis as the decision, then ask whether the two overlap.

**And measure where the BOOK is, not where the reasoning started.** The put-side POP result was
established on bull-put geometry while every modeled recommendation since 2026-08-11 has been a
call structure — 17 iron condors and 14 bear calls, zero bull puts. Running the mirrored test
found the opposite sign (below). A conclusion established on the half of the book you happened
to start with is a conclusion about that half.

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
- **ZERO DRIFT IS NOT NEUTRAL BETWEEN THE TWO SIDES OF THE BOOK.** `price_projection.project()`
  drops drift deliberately and the reasoning is sound -- drift estimated from a sample measures
  the sample, and sector relative strength was tested as an input and rejected at rank
  correlations of +0.01 to -0.04. But the CONSEQUENCE is asymmetric and was never recorded.
  Measured 2026-09-04 over 14,076 observations at 21 sessions, the sample drifted **+0.19 sigma**
  (positive on 11 of 12 names), and modelled-vs-actual breach at real ledger geometry came out:

        put  long median (-7.56%)   modelled 16.53%  actual 13.55%   +2.97  conservative
        call short p10   (+6.77%)   modelled 20.12%  actual 24.25%   -4.13  OPTIMISTIC
        call short median(+10.25%)  modelled 12.50%  actual 14.94%   -2.44  OPTIMISTIC
        call long median (+11.47%)  modelled 10.67%  actual 12.45%   -1.78  OPTIMISTIC

  Drift accounts for the sign and most of the size on both sides; body over-dispersion partially
  offsets it (residuals +1.3 to +1.7 on puts, -0.8 to -2.2 on calls). This is NOT a fat upside
  tail -- that hypothesis was tested and drift explains the asymmetry.

  The rule that follows: **in an up-trending sample a zero-drift model is conservative on puts
  and optimistic on calls, and the sign flips with the trend.** Do not read "conservative at
  every real strike" from a put-side measurement onto a call-side book. Do not add a drift term
  to fix it either -- that reintroduces the rejected estimator. Record the asymmetry, size it,
  and let it inform which side of the book carries unmodelled risk in a given regime.
- **RESOLVED 2026-09-04, on the pre-registered reading: the drought is the market.** The
  09-04 09:35 scan is the first to carry the split counters. Across the 22 tickers that
  enumerated no valid spread: `quote_spread_too_wide` **683 (79.2%)**, `quote_absent` **179
  (20.8%)**, `quote_crossed` **0**. Per the reading fixed in advance, that is wide books on the
  strikes the strategy wants and selection correctly refusing to cross them -- not a fetch-path
  fault. The same scan reported `scan_coverage` at 87% healthy (47/54) while 22 of those 54
  produced nothing, so the follow-on stands: the health predicate tolerates a spread up to 0.80
  of mid where selection requires 0.35, and the metric is more than twice as loose as the gate
  it purports to describe. Fix the metric or rename it; do not touch MAX_QUOTE_SPREAD_PCT.
- **The working tree IS production.** The scheduled task names a path, not a branch, so an edit
  on a session branch is live on the next cycle. Confirmed 2026-09-04: split counters written
  that morning appeared in the 09:35 scan from an uncommitted-to-main branch. Useful for getting
  a diagnostic answered same-day; dangerous for anything else. Know which one you are doing.
- **Selection ranks on credit-to-width and then gates on POP, and the two are inversely
  ordered**, so a ticker can die at the POP floor while a sibling spread that would have cleared
  it was discarded upstream. GDX, 2026-09-03: the selected 93/90 carried true_pop 0.713 against
  a 0.72 floor, while 90/84 (0.7543) and 91/89 (0.7435) both cleared it and both passed every
  other gate. Structural rather than incidental -- more credit means closer to the money means
  lower POP -- so it recurs whenever a ticker's surviving spreads straddle the floor. Recorded,
  NOT fixed: changing the ranking key is a selection change and belongs to a decided cohort.
- **`MIN_PROBABILITY_OF_PROFIT = 0.72` was set in the INITIAL COMMIT** (2026-03-31, comment
  "true probability not just delta") and has never been calibrated against the scale it gates.
  That matters now that the scale is measured: modelled breach is ~3pp PESSIMISTIC on the put
  side and ~2-4pp OPTIMISTIC on the call side, so one constant is effectively stricter on puts
  and looser on calls. This is a units question of the same family as decimal-vs-points and
  MEASURED_COVERAGE carrying the close-to-close figure on a gap claim -- not an argument for
  moving the floor.
- **THE DROUGHT'S START DATE IS A COMMIT, NOT A MARKET MOVE.** `d6255b9` landed 2026-08-10 at
  17:46 CDT -- after that day's last cycle -- and made main.py gate bull puts on the NATURAL
  credit instead of the MID. The first cycle to run it was 08-11 08:35. From the scan log,
  qualified trades per scan:

        08-05  0,0,0,6,5,5,3,5,5,7,4,5,5,6,5,5,4,4,4,5,...   (mid-priced gating)
        08-10  3,4,3,3,4,5,6,4,0,0,2,5,5,5,5,5
        08-11  1,1,1,1,1,0,0,0,0,1,1,1                        (natural-credit gating)
        08-12+ mostly 0, occasional 1-2
        09-02+ 0 on every scan

  The multi_strategy call side had already been fixed on 2026-08-07, so it was already
  producing at its honest rate; main.py's bull-put path was still quoting mids until 08-10.
  That is the whole explanation for the board going 100% call-side on 08-11 -- the put path
  lost an inflated pass rate, the call path had already taken the hit. It is NOT a market
  signal and NOT a delta-band artifact. **The drought is substantially the system becoming
  honest about fills**, which is the same finding as the 72%-on-mid vs 8%-on-natural split, seen
  from the entry side instead of the outcome side.

  Correction to the standing framing while here: "zero qualified since 08-10" is wrong. The
  board qualified 1-2 per scan on most days through 2026-09-01. TRUE zero began 2026-09-02 --
  three trading days before this was written.
- **A ranking key inversely ordered with a gate manufactures rejections that are not
  rejections.** `select_bull_put_pair` ranks on natural credit-to-width and the POP gate is
  applied to the winner, but more credit means closer to the money means lower POP -- so the
  ranker hands the gate the lowest-POP member of each family by construction. Measured over 593
  ticker-days (2026-08-07 .. 09-03): **33 (5.6%)** are cases where the ranked winner failed a
  post-selection gate while a sibling that also cleared enumeration passed everything -- 36.3%
  of all ticker-days where the winner failed one. Killers: pop 27, support_shelter 9. So a
  funnel line reading "GDX failed POP" sometimes means "VEGA chose the one GDX spread that
  fails POP", and roughly a third of POP-labelled deaths are of that kind. Enumeration deaths
  (64.6% of ticker-days, "nothing cleared enumeration") and ticker-level gates (IV rank, news,
  VRP) are NOT affected, so the funnel's large buckets stand. Recorded, not fixed: changing the
  ranking key is a selection change and belongs to a decided cohort.

  A first pass at this measurement reported **18.5%** by treating delta_cap and otm_buffer as
  post-selection gates when main.py rejects them DURING enumeration. Spreads failing them were
  never selectable, so counting their failure as a ranking artifact was a category error. Draw
  the enumeration/selection boundary where the code draws it before counting across it.
- **THE DROUGHT SPLITS 91/9, AND THE LARGE HALF IS A CORRECTED MEASUREMENT.** Qualified trades
  per scan: **4.47** under mid-credit gating (120 scans, through 08-10), **0.39** under
  natural-credit gating (193 scans, 08-11 .. 09-01), **0.00** since 09-02 (16 scans). So 91% of
  the fall is `d6255b9` -- VEGA no longer counting credit it cannot collect -- and 9% is the
  tape. VIX moves 2.5 points across the entire window and explains none of the step; SPY's
  20-day realised vol is the column that moves, and it moves LATE (13.3 -> 7.2 across the last
  week of August), lining up with the 0.39 -> 0.00, not with 08-11. Two causes, two dates.

  **State it plainly whenever the drought is discussed: the prior rate was fiction.** A board
  qualifying six spreads a day on prices no fill could achieve was not working better in July,
  and the mid-vs-natural defect that produced a 72% win rate against a real 8% is the same
  defect, measured at entry instead of at exit. "No trades" is the correct output of a correct
  system in this tape; it only looks like failure if the reason is not written down.

  **Deduplicated, the split is 88/12 (92/8 excluding two 1-2 scan days), not 91/9.** The raw
  per-scan measure overstates the fall by ~30%, because at 26-45 scans/day one candidate can
  qualify repeatedly and at 7/day it cannot. The modeled ledger is keyed on
  ticker+strikes+expiration+date and is therefore already deduplicated: 16.67/day -> 1.94/day
  -> 0.00/day, an 8.6x drop rather than 11.5x. Quote a cadence-free measure whenever a rate is
  compared across a scheduler change.

  **There is no 08-07 control, and the claim that multi_strategy was "already fixed on 08-07"
  is WRONG** -- that date belongs to `vega_candidates`, a different module. Bear calls run
  7/8/11 through 08-06, read 1 and 2 on 08-07 and 08-08 (two scans and one scan -- a sampling
  artifact), bounce to 12 on 08-10, and go to 0 on 08-11 alongside bull puts. Both paths drop
  together, exactly as d6255b9's message says ("on every strategy"). One commit, one date, both
  paths -- a stronger claim than two dates, but resting on a single step.

  **Why the board is 100% call-side is therefore NOT "the call path was pre-adjusted."** After
  08-11 bull puts go to exactly zero and stay; bear calls keep producing 0-3/day. The put path
  dies at ENUMERATION -- 79.2% `quote_spread_too_wide` -- while multi_strategy runs its own
  enumeration and survives more often.

  **SPY rv20 is an index backdrop, not the per-name mechanism.** Its series low of 7.2 falls on
  09-01, the only productive day of the final week -- because that day's single qualifier was
  META, whose own rv20 was 29.9. Do not read a per-name, per-strike decision against an
  index-level series; the quote split is measured at the level the decision is made and is what
  carries the final step.

  Full series and the three checks: `reports/claude_VEGA_QualificationSeries_2026-09-04.md` §G.
- **A RANKING KEY THAT OPPOSES A GATE IS A DEFECT, NOT AN OPEN QUESTION.** Recorded here with a
  mechanism and a rate so it is not re-litigated: `select_bull_put_pair` sorts on
  `natural_credit_to_width` and the POP gate is applied to the winner; credit-to-width is
  monotonically anti-correlated with POP, so the ranker hands the gate the worst-POP member of
  every family, on every ticker, by construction. Measured: 33 of 593 ticker-days (5.6%), and
  **36.3% of the ticker-days where the winner failed a post-selection gate** had a sibling that
  cleared everything. `pop` is 27 of 36 killers.

  **The fix has a shape, for whoever takes it:** either rank on something the gates do not
  oppose, or evaluate the post-selection gates BEFORE ranking rather than after -- which is what
  the enumeration path already does for `delta_cap` and `otm_buffer`, and is the cheaper of the
  two. Deferred deliberately: it changes selection, and selection changes belong to a decided
  cohort, not to a drought.
