# VEGA — Session Log, 2026-08-18 / 2026-08-19

Operator: Josh · Agent: Claude Opus 5 (Claude Code)
Repo: `options-analyzer` @ `main` · start `87fe6ac` → end `12b89f3` (4 commits, all pushed)
Tests: 1,031 → **1,096** (65 added, all green)

Companion document: `VEGA_System_Audit_2026-08-19.md` — the full third-party audit.
Every figure below was read from the live system at the time stated, not carried over from a
prior document. Where a claim in an earlier doc turned out to be wrong, that is called out.

---

## 1. What this session was asked to do

It began as a verification pass on an external (Codex) review, and turned into four pieces of
work. Only the first was planned.

| # | Work | Trigger |
|---|---|---|
| 1 | Answer two open questions from the Codex verification doc | Asked |
| 2 | Build the shadow book — grade what the board recommends, not just what it opens | Asked, after the answer to #1 |
| 3 | Fix the cycle's exit code and consolidate two racing schedulers | Found while verifying #2 |
| 4 | Build the directional forecast engine (Stages 0–1) | Asked |

---

## 2. The two questions, answered

**Q1 — how many trades has the frozen cohort accumulated toward 30?**

**Zero, at the time of asking.** Not "a few". All 66 closed trades carried `gate_basis: mid`,
meaning every one was selected before the 2026-08-08 fix. The clean `natural|natural` cohort
had exactly four members and all four were still open, opened in a single batch on 2026-08-10
at 09:38 (GDX, XLE, PEP, ARKK).

The prior doc framed this as "the count hasn't been checked". The count was zero.

**Q2 — is scheduler consolidation still the top priority?**

The answer changed twice, and the second change was my error.

Initially: the Windows task looked healthy (`EndBoundary` blank, so the 14-day expiry documented
in earlier sessions was genuinely fixed), so I advised **dropping** it from priority 1. That was
wrong. The cockpit's own scheduler was not running when I first looked — it started at 10:21:45
on 08-18, mid-session — and once it was up, two schedulers were firing the same cycle:

```
cockpit      hourly at :21
Windows task every 2h at :35     ← lands inside the cockpit's 13-16 min run, every time
```

On 08-18 three of the task's four fires did no work: one hit `Another cycle appears active`,
one hit the market-closed guard, and only 13:35 got through — because the 13:21 cockpit run had
finished five seconds earlier.

---

## 3. The finding that reframed everything

The desk had opened **nothing since 2026-08-10**. The cause was not the scheduler.

Every one of the eleven board recommendations between 08-11 and 08-17 was **call-side** — bear
call or iron condor — and the desk refuses 100% of call-side. The bull-put path produced nothing
in that window because of upstream data quality. So the only board the desk was given was one it
was structurally unable to act on.

Underneath that sat a one-word defect:

> `multi_strategy._base()` emitted the expiry as `expiration_display`.
> Every consumer reads `expiration`.

Result: **81 of 158 modeled rows carried `expiration: null`**, with trade ids containing the
literal string `None` (`META-610.0/620.0-None-2026-08-17`). A position with no expiration cannot
be marked, expired or graded even in principle. Nothing raised, because a missing key reads as
`None` and `None` renders as a blank cell.

This is precisely the FIELD-NAME MISMATCH class that `analysis/contracts.py` was written for —
and that module had never been wired into anything.

Two further defects found in the same area:

- **`modeled_credit_per_share` meant two different things** — the MID on the bull-put path, the
  NATURAL on the call-side path, with nothing to tell them apart after the fact. Pricing P/L
  from it would have reproduced the exact defect that made the ledger's first 18 trades unusable.
- **Width was recorded negative on all 49 bear calls.** `short − long` is negative on the call
  side; the fallback assumed a put spread.

---

## 4. What was built and fixed

### 4.1 Shadow book (`analysis/shadow_book.py`, commit `96d635d`)

Grades every board recommendation, opened or not. 0 of 158 modeled rows had ever been graded;
the ledger's own comment says `modeled → filled → closed` and nothing implemented the second
arrow.

Direction-aware by construction: breach is measured on the **Low** for put-side structures and
the **High** for call-side ones, and a condor holds only if neither wing is touched. This is the
load-bearing part — `counterfactuals.resolve` is put-side only, and run over a bear call it does
not error, it reports that every one held, forever. The test asserting a call-side and a
put-side trade grade *differently on identical bars* is the one that matters.

Refuses rather than guesses: `held` stays `None` until expiry, bars after expiry cannot breach a
settled contract, P/L is declined when no natural credit was recorded, and a cohort below
`MIN_SAMPLE` reports a count instead of a rate. Writes its own ledger and its own
`shadow|<strategy>|<basis>` cohort — it never touches `vega_outcomes.jsonl`, so the live cohort
count is unaffected.

### 4.2 Cycle exit code (`0616ecc`)

`cand_path` was referenced but never assigned — a leftover from the deleted candidates opener.
**Every full cycle had been raising `NameError` on its final statement**, after all work was
done and after the `finally` released the lock. Six runs reported exit 1 to Task Scheduler while
having actually succeeded.

This is the worst possible shape of failure here: every postmortem about a dead re-mark loop
turned on telling a run that died from one that finished, and the exit code was lying in both
directions.

`tests/test_cycle_names_resolve.py` catches the class, not the instance — it walks the AST and
asserts every name a function *reads* will exist. Against the shipped pre-fix file at `87fe6ac`
it reports exactly one finding at the exact line. My first two attempts at that detector
produced false positives on closures and comprehensions; a check that cries wolf gets deleted,
so both properties are now asserted.

### 4.3 Scheduler consolidation (`21212bb`)

One driver. `INTRADAY_SCHEDULER_ENABLED = False`; the Windows task survives — the reverse of what
`auto_paper_cycle.py`'s own docstring recommends. That advice assumes the cockpit is always up,
but it is a UI process that stops silently when the window closes or the box reboots, and paper
execution must not be conditional on a dashboard being open.

The task's window was independently wrong: it started 09:35 CDT (an hour after the 08:30 open)
and its last fire at 15:35 CDT was 16:35 ET, after the close — that fire had never done anything.

```
was:  09:35 CDT, every 2h for 7h   → 4 fires, 1 always dead, rest colliding
now:  08:35 CDT, every 1h for 6h   → 7 fires, all inside market hours
```

Three dead tasks deleted (`VEGA_DailyMorningScan`, `VEGA_Checkpoint_1200/1500`). All four task
definitions exported to `backups/scheduled_tasks/` and committed before any change.

### 4.4 Directional forecast (`analysis/direction_forecast.py`, commit `12b89f3`)

A calibration flywheel, explicitly **not** an alpha engine. Nothing it produces reaches
selection, sizing or execution, and `price_projection` still draws zero-drift bands.

The rationale is measurement speed: every existing VEGA claim matures at a 30–45 day expiry
(39 claims, 0 resolved, first maturing 08-23), so calibration currently takes quarters. One-day
claims mature in one day.

Probabilities are derived from the band rather than assigned by a score, so climatology is the
natural baseline and is recorded as a twin claim beside every live one.

**Two errors caught on real data before commit**, both of which would have invalidated the
exercise:

1. **Tilt was constant across horizons.** Drift accumulates linearly, sigma with √t, so a fixed
   tilt in sigmas implied 0.2σ of drift in one session — past 100% annualised — and was most
   wrong where the signal is weakest. Now an annualised information ratio scaled by √t.
2. **Every claim came out "flat."** At a half-sigma band, flat carries 38.3% against 30.8% a
   side and the capped tilt cannot close that gap at any horizon. A constant forecast has zero
   resolution *by construction* — it would grade as beautifully calibrated having measured
   nothing. Band set to 0.4307σ, the value that makes the three outcomes equally likely.

---

## 5. Verified outcomes

Confirmed against the live system on 2026-08-19:

- **Six consecutive clean cycles.** Every run today: `START → END`, exit 0, ~13 min. Zero lock
  skips, zero `NameError`. Last `NameError` in any log: 2026-08-17 14:39:01.
- **The expiration fix works in production.** Three META bear calls recorded 08-18 with a real
  expiration, positive width, legs, natural credit and no `None` in the id. Shadow book
  `graded` went 77 → 80 once today's bar existed.
- **The first clean-cohort trade closed, and it won.** PEP 130/125, opened 08-10, closed
  2026-08-19 18:48 at target profit: entry $0.77, exit $0.23, **+$51.84 net**, 9 days held,
  `true_pop` 0.8113. The cohort is now **1 of 30**.

---

## 6. Corrections to earlier claims

Stated plainly because this project has a documented history of handoff docs being wrong on
checkable facts.

| Earlier claim | Correction |
|---|---|
| "Scheduler consolidation should drop from priority 1" (mine, 08-18) | Wrong. Two schedulers were racing; the cockpit simply wasn't running when I first checked. |
| "Watch the 11:35 run to confirm the fix" (mine, 08-18) | Wrong run. It exited 0 in two seconds on the lock. The 13:35 run was the real proof. |
| "The count toward 30 hasn't been checked" (prior doc) | The count was **zero**, and the sample was a single day's batch. |
| "Re-mark loop has died silently four times" (prior doc) | The re-mark loop was running. The *exit code* was lying. |

---

## 7. Open at session end

Full detail in the audit. The short list:

1. **XLE has not been marked since 2026-08-14** — 5 days stale, while GDX and ARKK marked today.
   It is 1 of only 3 remaining clean-cohort positions, and an unmarked position cannot stop out.
2. **Data quality is the binding constraint** — 22 of 56 watchlist tickers skipped today; 41 of
   92 recorded scans produced zero qualified trades.
3. 81 pre-fix call-side shadow rows are permanently unresolvable (fixed forward only).
4. The orphaned candidate-selection subtree is still present.
5. Two stale comments in `config.py` still reference deleted functions.
6. The IV-rank soft-fail path for approximate-history tickers is unchanged.
7. `analysis/counterfactuals.py` has not been rebuilt since 2026-08-10.

---

## 8. Next dates that matter

| Date | Event |
|---|---|
| 2026-08-20 | First directional claims record (14:35 CDT), ~448 claims |
| **2026-08-21** | **29 bull puts expire — the shadow book prices its first trades** |
| 2026-08-23 | First existing prediction claims mature |
| ~2026-09-03 | Stage 2 gate: read *resolution*, not hit rate; compare live vs baseline |
| 2026-09-18 | 44 more shadow settlements |
