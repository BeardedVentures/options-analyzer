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

## 4. When a finding may act, and when it may only be written down

**Measurements that would change BEHAVIOUR get recorded. Measurements that change what a NUMBER
MEANS get acted on immediately.**

This replaces "do not change gates mid-drought", which was the working rule for a month and is a
worse one: it states a prohibition without a reason, so it cannot tell you what the exception
looks like. The rule above explains itself, and it decides cases that the prohibition leaves
ambiguous — including several where the right answer is to act.

The test is not "is this risky" or "am I confident". It is **what does the change alter?**

- If it alters which trades the system would take, the population changes, and a population
  change mid-experiment destroys the experiment. Record the measurement, size the decision, and
  hand it to the operator. A gate is an entry rule and entry rules define the cohort.
- If it alters what an already-recorded number means — a field that was mislabelled, a cost that
  was omitted, a basis that was implied and never stated — then NOT acting is the corruption.
  Every day it waits produces more rows measured on a definition nobody can reconstruct later.

Worked, 2026-09-04, seven findings in one day and every one decided by that single test:

    ACTED ON — changed what a number means
      net P/L omitted the exit cross              -> measured it, added net_basis
      the ledger stored no leg quotes             -> persisted them, and leg liquidity
      chain_coverage implied tradeability         -> renamed to say quotability

    RECORDED ONLY — would have changed behaviour
      the liquidity floors differ 10-25x          -> decision table, constants untouched
      the two paths run different quote gates     -> exposure sized, predicates untouched
      the exit cross can eat a profit target      -> projected onto candidates, gates nothing
      the ranker opposes the POP gate             -> rate measured, ranking key untouched

Two corollaries that fall out of it, and both were load-bearing today:

**A measurement that cannot be redone later is not optional.** Instrumentation gaps are permanent
in a way analysis gaps are not: analysis can be re-run against the same rows, but the rows for a
period are created by the code running during that period. 101 recommendations between 08-11 and
09-04 can never be audited for fill quality because their quotes were never stored. That is why
persisting leg quotes was urgent while gating on them was not.

**Absence is informative or it is not, and which one decides how to record it.** The same day
produced two opposite treatments of a missing value, both correct: an unreadable closing book
records `None` for the exit cross, because the cost is genuinely unknown and a zero would read
as "the exit was free" — and a missing `net_basis` counts as `commissions_only`, because every
row written before that field existed was priced that way and there is no other possibility.
Ask whether the absence carries information before deciding whether to preserve it.

**Prefer derived to stored.** A stored copy of a computable value is a second field that can
disagree with the first. `gate_basis`, `entry_vendor_basis`, `cohort` and the projected exit
cross are all derived, and derived fields have a property stored ones do not: they cannot drift.
Store the RAW inputs — quotes, liquidity, dates — and compute everything else on read.

**And make comparisons commensurable by construction.** The projected exit cross and the realised
one use the same function against different books, so the eventual disagreement between them is
model error and nothing else. Had they been computed two ways, the first divergence would have
been uninterpretable — a real difference or a definition mismatch, with no way to tell which.

---

## 5. Recorded so a future session does not "correct" it back

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
- **THE TWO ENUMERATION PATHS APPLY DIFFERENT QUOTE STANDARDS, AND IT IS THE PREDICATE, NOT THE
  MARKET.** Measured 2026-09-04 on six names, both predicates run over both chains at the same
  moment:

        predicate                       admits of call band   admits of put band
        multi_strategy._tradeable            95%                   99%
        main.py (put path)                   69%                   75%

  `main.py` requires bid>0 AND ask>0 AND mid>0 AND (ask-bid)/mid <= MAX_QUOTE_SPREAD_PCT (0.35)
  AND volume>=25 OR OI>=100. `multi_strategy._tradeable` requires **mid>0 AND (volume>=1 OR
  OI>=10)** -- no two-sided quote, NO SPREAD THRESHOLD AT ALL, and a liquidity floor 10-25x
  looser. Of the strikes the call path admits and the put path refuses, two thirds fail on
  liquidity and one third on spread.

  **Put chains are NOT wider.** Median relative spread in band: put 0.008, call 0.028, and the
  put band carries twice the strikes (181 vs 91) despite reaching deeper OTM (0.12-0.30 vs
  0.16-0.30), which biases against this conclusion rather than toward it. So bull puts going to
  exactly zero while bear calls kept producing 0-3/day is substantially the PREDICATE, not a
  worse market on the put side.

  **The operator consequence is the point:** the bear calls and condors recommended since 08-11
  -- the entire board -- were selected under a standard that never checks whether the book can
  be crossed. The call path prices at the natural credit and requires credit-to-width > 0, which
  catches the worst cases economically, but "positive natural credit" is a far weaker condition
  than "spread under 35% of mid". Nothing here says those recommendations are unfillable; it
  says nothing has checked, and the one path that would check is the one that stopped producing.

  Sample limits: six tickers, one moment, skewed liquid (SPY and NVDA sit near 0.01). The 22
  tickers that enumerate nothing are the illiquid tail, where KO and WMT show 33% and 60% of the
  put band wider than 0.35. Re-measure across the tail before treating the ratio as general.

  Also corrected here: `MIN_OPTION_VOLUME` is **25** and `MIN_OPTION_OPEN_INTEREST` is **100**.
  Earlier notes in this session quoted 100/500 -- those are the `getattr` DEFAULTS in main.py,
  not the configured values. Read the config, not the fallback.
- **READ THE CONFIG, NOT THE DEFAULT.** `main.py` reads liquidity floors as
  `getattr(config, "MIN_OPTION_VOLUME", 100)` and `getattr(config, "MIN_OPTION_OPEN_INTEREST",
  500)`. The CONFIGURED values are 25 and 100. Quoting the fallbacks as if they were live
  produced a 4-5x overstatement of a gate's strictness inside this session, in an argument about
  that very gate. Same shape as MEASURED_COVERAGE carrying the close-to-close figure on a gap
  claim and as the two `iv_rank` fields: a value that exists in two places, read from the wrong
  one. When a number matters, print it from the running config rather than reading it out of a
  call site.
- **LEG QUOTES ARE NOW PERSISTED (2026-09-04), AND THEIR ABSENCE WAS THE MISSING INSTRUMENT.**
  All three builders put leg bid/ask on the trade dict; `record_modeled_trades` dropped every
  one, so the ledger recorded a credit with no record of the book it came from and 101 call-side
  recommendations cannot be audited for fill quality even in principle. Stored RAW under
  `leg_quotes`, never as a derived ratio -- a stored verdict bakes in today's definition of "too
  wide", and the whole lesson of 0.35-vs-0.80 is that one threshold can mean two things. Raw
  quotes can be re-judged under any threshold later.
- **THE LOOSE PREDICATE'S EXPOSURE, SIZED 2026-09-04 — and it is a DIFFERENT defect from
  mid-vs-natural, not the same one wearing a mask.** Measured on the 18 reachable tickers that
  enumerated nothing on the 09-04 09:35 scan:

        population        band strikes   loose admits   strict admits
        illiquid tail          164        119 (73%)       48 (29%)
        liquid sample (6)       91         86 (95%)       63 (69%)

  The gap is **2.5x in the tail against 1.4x in the liquid sample** -- the earlier six-name
  reading understated it because SPY and NVDA sit near a 0.01 relative spread. Of the structures
  `_best_wing` builds on those names right now, **7 of 15 survive the strict test**; of the 8
  refusals, 4 are liquidity alone and 4 involve a too-wide leg.

  **The recorded credits are NOT fiction, and saying so was my error.** The call path prices at
  the NATURAL credit -- sell the short bid, buy the long ask -- which is the fillable price by
  construction. A wide book does not make it unreachable; it makes it smaller, and the credit
  floors then judge it. mid-vs-natural recorded a price obtainable in principle by nobody. This
  records a real price whose SIZE, EXIT and LIVENESS are untested.

  **And the tail failures are thin markets, not wide ones.** GE 350C: spread 8.2%, volume 1,
  OI 26. BAC 65C: spread 13.1%, volume 6, OI 78. USB 66C: volume 0, OI 13. Most refused legs are
  quoted more tightly than strikes that pass; what catches them is the liquidity floor. So the
  live risk is not the entry price -- it is that the exit crosses the same spread again on a
  strike with single-digit volume, and that a multi-contract order has no demonstrated
  counterparty. The strict predicate was protecting the ROUND TRIP.

  Not fixed: bringing `_tradeable` to parity changes what the board builds, mid-drought, on the
  only path still producing -- and "no volume today" is not "cannot be traded". The liquidity
  floors differ 10-25x (`volume>=1 or OI>=10` against `volume>=25 or OI>=100`) and neither was
  chosen against the other; that is a decision, not a bug fix.
  Full measurement: `reports/claude_VEGA_PredicateExposure_2026-09-04.md`.
- **`estimated_round_trip_cost_per_contract` IS COMMISSIONS ONLY, AND NET P/L SUBTRACTS NOTHING
  ELSE.** `_round_trip_cost_per_contract()` returns `per_leg * legs * 2` = **$2.16**. The exit
  cross is not in it, and `outcome_logger:614` computes net P/L as `gross - that figure` -- so
  every net number this project has reported assumes closing the position is free. Measured
  2026-09-04 against the real books on the open positions:

        ticker  short rel  long rel  35% gate  exit cross   entry credit   cross as % of credit
        NKE        3.7%     28.6%     PASS       $ 2.00        $38             5%
        NEE       25.6%     54.5%     FAIL       $ 4.00        $36            11%
        SMH        9.9%      7.7%     PASS       $28.50        $95            30%
        AMGN      90.9%    175.0%     FAIL       $57.50        $65            88%

  On the three LIVE-marked positions the exit cross is **$34.50 against $6.48 modelled -- 5.3x**.
  AMGN is excluded from that ratio deliberately: the engine already flags its mark unusable, and
  measuring off a book it refuses to trust would be the same error as trusting a stale mark.

  **THE GATE IS RELATIVE AND THE COST IS ABSOLUTE, AND THAT IS THE STRUCTURAL POINT.** SMH clears
  MAX_QUOTE_SPREAD_PCT comfortably on both legs -- 9.9% and 7.7% -- and still costs $28.50 to
  cross, because the legs are $3+ each. NKE at 3.7% costs $2.00. **A 10% spread on a $3.50 leg
  costs more to cross than a 35% spread on a $0.50 leg**, and neither the ratio gate nor a flat
  $2.16 constant can see the difference. Any future work on exit economics starts here, not at
  the spread cap.

  Not changed: the estimator feeds net P/L on every row, and rewriting it re-prices the whole
  ledger. That is a decision about trade economics, not a bug fix.
- **101 RECOMMENDATIONS CAN NEVER BE AUDITED, AND THAT IS A PERMANENT HOLE.** Every structure
  recommended between 2026-08-11 and 2026-09-04 was written without its leg quotes, so no
  retrospective fill-quality check on them is possible even in principle -- and that window is
  exactly the period the board has been exclusively call-side, produced by the path with no
  crossability test. The 7-of-15 survival rate measured 2026-09-04 is the best available proxy
  for what those 101 looked like, and it is a proxy: a different day, a fresh builder run, not
  the recommendations that were actually made. Do not read 47% as a property of them.
  `leg_quotes` and `leg_liquidity` close this going forward only.
- **THE EXIT-COST CORRECTION IS HYGIENE, NOT A COHORT PROBLEM -- MEASURED, NOT ASSUMED.**
  Cannot be measured directly (no closed row carries leg quotes; historical option books are not
  retrievable), so it was run as a sensitivity: how much exit cross each closed row absorbs
  before its verdict changes. The asymmetry that decides it: a stop-loss exit only gets WORSE
  with added cost, so a loss cannot become a win and only the 24 wins can flip.

        exit cross applied to every row      wins flipped to loss
        $2.00 / $4.00 / $10.00 flat                 0 / 24
        $28.50 flat (the SMH worst case)            6 / 24
        5% / 11% / 30% OF CREDIT                    0 / 24

  The proportional row is the one that matters, because the cross scales with leg price and leg
  price scales with credit. Every win closed at 57-95% of its entry credit -- they exited at a
  profit target -- so a cross of 30% of credit still leaves all 24 positive. **No verdict flips.**

  **Magnitude is a different question and it is material:** cohort net goes -3,163 as recorded to
  **-3,913** at $10/contract and **-5,300** at $28.50. The direction was never in doubt; the size
  of the loss is understated by 24-68%. Fix the estimator for accounting, not because the
  win/loss record is wrong.
- **STALE MEMORY CORRECTED: the natural-fill cohort is no longer 0-win.** The standing finding
  was "mid 13/18, natural 0/46". Measured 2026-09-04: **mid 13/18 (72.2%) -- still exact -- and
  natural 11/57 (19.3%)**. All eleven natural wins closed between 2026-08-12 and 2026-09-03, all
  at `auto-target-profit`, after that finding was written. So the natural basis DOES reach a
  profit target; the claim that it never had was true when made and is not now.

  It does not rescue the cohort. Natural expectancy is **-$56.00/contract** against a breakeven
  win rate of 63%, and mean loss (-80.74) is 1.7x mean win (+47.45). The mid cohort's +$10.61
  expectancy at a 72% win rate remains the artifact -- it needs 68% to break even and got 72% on
  prices no fill could achieve. Two cohorts, still never pooled.
- **THE -$56/CONTRACT EXPECTANCY DESCRIBES A POPULATION THAT NO LONGER EXISTS.** Split on ENTRY
  date, because that is when the gates applied:

        natural cohort, entered <= 2026-08-10 (pre-fix)   n=57   exp -$56.00
        natural cohort, entered 08-11 .. 08-17            n= 0
        natural cohort, entered >= 2026-08-18 (post-fix)  n= 0

  **ALL 57 were entered between 08-04 and 08-10.** So were all 75 closed rows, and so were all
  four open positions (NKE 08-04, AMGN 08-06, SMH 08-07, NEE 08-07). **Zero trades entered under
  post-fix pricing have closed or are open.** Entries stopped the day the fix landed, so the
  ledger's entire outcome record predates it.

  The consequence is the opposite of the obvious reading: **caps_v1 is NOT starting from a
  measured negative.** It has no prior at all under current rules. "The same rules on the same
  basis produced -$56/contract over 57 trades" is wrong -- the basis fed to those gates was the
  MID until 2026-08-10 17:46, so the trades were selected by gates reading prices no fill could
  achieve, then recorded at the natural credit on the way in. Honest fill model, dishonest
  selection. The 30-trade target really is "find out whether this works", not "find out whether
  the fixes moved a measured negative".

  This is the entry-epoch boundary the cohort contract already names, made concrete: an entry
  rule change defines a new population, and 100% of the outcome record sits on the far side of
  one. Do not quote the -$56 as a prior for anything caps_v1 opens.
- **ANY RECORDED COUNT THAT CAN STILL GROW CARRIES THE DATE IT WAS MEASURED, and gets
  re-measured before it is used as an argument. THIS APPLIES TO DOCSTRINGS WITH MORE FORCE THAN
  TO REPORTS.** A report is superseded by the next report; a code comment is read as current
  indefinitely, by everyone, forever. The fourth instance this week was `cohort()`'s own
  docstring arguing for the fill_model dimension on "natural-fill trades won 0 of 46" -- a
  figure that had been wrong for three weeks, sitting in the justification for a cohort key.
  The argument survived its number being wrong (fill model matters because mid and natural are
  different PRICES, not because one won zero times), which is luck rather than design. Put the
  measurement date inside the docstring or do not put the number there. Three times this week a stale figure directed
  reasoning: "zero qualified since 08-10" (the board qualified 76 more through 09-01), the
  100/500 liquidity floors (configured 25/100 -- those were getattr defaults), and "natural
  0/46" (now 11/57, and every win closed after that note was written). Each was correct when
  written and consulted later as current. "natural 0/46 AS OF 2026-08-11" would have been safe.
  Correct such a figure IN PLACE rather than adding a second record beside it.
- **THE LIQUIDITY FLOOR IS A TAIL INSTRUMENT AND NOTHING ELSE.** Measured with the spread cap
  held constant, so the floor is the only variable:

        floor                      tail call   tail put   liquid call   liquid put
        >=1 vol OR >=10 oi  (call)   87/89      161/170     130/130       291/291
        >=5 vol OR >=25 oi           77/89      147/170     129/130       288/291
        >=10 vol OR >=50 oi          67/89      127/170     124/130       277/291
        >=25 vol OR >=100 oi (put)   52/89       92/170     118/130       247/291
        >=50 vol OR >=250 oi         42/89       60/170     103/130       208/291

  On liquid names every candidate admits 91-100%: the constant is irrelevant there. On the tail
  it moves admission from 98% to 58% between the two constants currently live. The tail's median
  OI is 158 (call) / 114 (put), so `>=25 vol OR >=100 oi` cuts almost exactly at the median --
  the highest-variance place a threshold can sit. `>=10 vol OR >=50 oi` admits ~75% of the tail
  on both sides and costs ~5% on liquid names, which is the natural meeting point if one
  constant has to serve both paths. NOT CHANGED: picking it is a selection decision.
- **`gate_basis` CHECKED AND CORRECT — a negative result worth recording, because the hazard is
  real and only timing closed it.** `GATE_BASIS_FIX_DATE = "2026-08-08"` is one constant
  describing two paths fixed on different dates: vega_candidates on 08-07, main.py on 08-10
  17:46. A single 08-08 boundary would mislabel any trade selected by main.py's board between
  08-08 and 08-10 17:46 as `natural` while its gates still read the mid.

  **No such trade exists.** The desk did not open from main.py's board until `e088e3e` landed at
  2026-08-10 **19:14** — 88 minutes after the gates it reads were fixed. Every position opened on
  or before 08-10 came from the vega_candidates snapshot; the four opened that day are stamped
  09:38:52, hours before either commit. The field means what its name says.

  Do NOT "correct" the constant to 08-10 or 08-11: that would mislabel the
  vega_candidates-selected population, which is all 57 natural-fill closed rows. The reasoning
  is now pinned in the constant's own comment so the next reader does not re-derive it and get
  it backwards.
- **`commissions_plus_exit_cross` IS A NEW BASIS WITH NO ROWS, AND THE BOUNDARY IS ONE-WAY.**
  All 75 closed rows are `commissions_only` and can never be otherwise -- the closing books they
  were priced from are gone. So the first cohort gradeable on true net is one that has not
  started, and every comparison across the boundary compares two definitions of `net` rather
  than two populations. The new basis is STRICTLY MORE EXPENSIVE, which makes two opposite
  errors available: "net got worse" is trivially true the moment the first
  commissions_plus_exit_cross row closes, and "net improved" is trivially arguable by comparing
  a new gross against an old net. `outcome_logger.net_basis_note()` returns the warning and the
  paper-desk cohort report prints it; any new reporter that aggregates net should call it.
- **THE EXIT CROSS IS MEASURED AND DELIBERATELY NOT GATED.** Nothing at selection time has ever
  seen it, so a candidate whose exit would eat its own profit target passes every gate -- SMH at
  30% of credit and AMGN at 88% both did. `projected_exit_cross_per_contract` and
  `..._pct_of_credit` now ride on every candidate, computed from the entry book by the same
  formula the realised cross uses. They gate nothing, and a test asserts they gate nothing.
  Turning the ratio into a floor changes what the board builds and belongs WITH the
  liquidity-floor decision, not ahead of it. Same shape as the chain-size instrumentation of
  2026-09-03: measure first, decide once there are rows to decide against.

  Not persisted, on purpose. The leg quotes are, so the projection is recomputable from the
  ledger at any time, and storing it too would be a second field that can disagree with the
  first -- exactly what `set_mark`'s docstring refuses for gross and net.
