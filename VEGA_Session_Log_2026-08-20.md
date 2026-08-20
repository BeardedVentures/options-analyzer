# VEGA Session Log — 2026-08-20

Follows: VEGA Session Brief 2026-08-20, VEGA_System_Audit_2026-08-19.md
Scope: brief items 1 (fix) and 2 (diagnostic). Item 3 is **not** implemented — findings and a
recommendation are below, awaiting sign-off.

Every claim here was checked against code or the ledger this session. Where the brief and the
system disagreed, the system is reported and the disagreement is called out.

---

## 0 · Four corrections to the brief, up front

The brief's framing was right about *what* to fix and wrong about several specifics. These
matter because two of them change the urgency.

| Brief says | Actually |
|---|---|
| XLE's 29 bull puts expire **2026-08-21** — hard deadline today | The open XLE position expires **2026-09-18** (39 DTE). The 08-21 rows are `status: modeled` shadow-book projections, not open positions. **There is no expiry deadline this week.** |
| XLE is the urgent unmarked position | **Three positions, not one.** PSX unmarked longest (since 08-12, 12 skipped cycles), AMGN 08-13 (10), XLE 08-14 (9). But the audit was right that XLE is the one that matters most: XLE is clean-cohort (`natural\|natural\|ravens_v1`), PSX and AMGN are not (`natural\|mid\|ravens_v1`). Verified — the audit's "1 of only 3 remaining clean-cohort positions" (GDX, XLE, ARKK) is **correct**. |
| The failure is in the reprice path and may be a yfinance outage | It is neither an outage nor a strike-lookup bug. It is the **entry-side chain-quality gate being applied to the exit path** — see §1. Deterministic, reproducible, code-side. |
| Item 3a is urgent if entries are still clustering | **Nothing has opened since 2026-08-10** — seven sessions, zero entries, zero since the freeze. But the reason is not selectivity: half of all cycles since go-live never ran to completion. See §2.2, which is the finding of this session. |

The 08-21 point, traced to source: audit §10 line 144 reads *"Upcoming settlements: 29 on
2026-08-21"* — that line is in the **shadow-book** section and counts modeled recommendations,
not ledger positions. Reading it as an open-position deadline is what produced "before 08-21
expiry." The defect was real and worth fixing today; the deadline was not.

Worth separating cleanly, because the audit deserves the credit it earned: it was **right about
severity** (XLE genuinely is 1 of only 3 open clean-cohort positions — a third of the live
validation sample was unmanaged) and **wrong about deadline and mechanism**. The P0 ranking
stands on the severity alone.

---

## 1 · Item 1 — the unmarked-position defect. Root cause and fix.

### 1.1 Root cause (code-side, not data-side)

`_reprice_and_close_open()` marks open positions using `fetcher.get_options_chain()`. That
function enforces the **selection** contract (`data/fetcher.py`, the `SKIP_DATA_QUALITY`
branch): if fewer than `CHAIN_QUALITY_MIN_RATIO` of the chain's contracts are quotable, it
returns `[]` so that no signal is built on a chain that is mostly absent.

`CHAIN_QUALITY_MIN_RATIO` was raised 0.30 → **0.50 on 2026-08-14** — the same day XLE and AMGN
stopped being marked.

That gate is correct for choosing a new trade and is a category error applied to a position
already held: marking a vertical needs **two specific strikes to quote**, not a healthy chain.
Reproduced live this session:

```
PSX : only  1/22 ( 5%) of the yfinance chain is quotable, floor is 50% → returns []
AMGN: only  3/41 ( 7%) of the yfinance chain is quotable, floor is 50% → returns []
XLE : only 25/54 (46%) of the yfinance chain is quotable, floor is 50% → returns []
```

`[]` → the index is empty → `chain depth: 0 strikes` → skip. PSX at 5% and AMGN at 7% could
never clear a 50% floor again; XLE at 46% is marginal, which is why it drifted in and out
before failing permanently. Same mechanism, three severities.

The non-zero-depth variant (`NEE ... chain depth: 120 strikes`) is the same predicate acting
one contract at a time: `_quality_filter_options` drops individual records that have no volume
*and* no open interest, so a specific leg can vanish from a chain that passed the floor.

### 1.1a This is a known defect class in this codebase, not a new one

The shape: **a threshold built for one purpose, reused in a context that needs a different
bar.** `config.py`'s own GATE ENFORCEMENT CONTRACT block records the prior instances —
"three enforcement leaks in one week (IV-rank 07-25, POP floor 08-02, quote-spread 08-02) shared
one shape: a rule defined here, annotated by the scanner, then omitted from the path that
actually opens trades."

Those leaks were a rule reaching *too few* paths. This one is the mirror image: a rule reaching
*too many*. `CHAIN_QUALITY_MIN_RATIO` answers "is this chain trustworthy enough to build a new
signal on?" and got asked "may I price two strikes I already own?" — a question with a much
lower legitimate bar.

`REQUIRED_GATES` exists precisely to stop the first version. Nothing structural stops the second,
and the fix here is a local one: a keyword-only opt-out scoped to a single caller. The general
form — *every shared predicate should state which question it answers* — is worth carrying into
the next review rather than rediscovering as a fourth incident.

### 1.2 A correction to the source audit, not a rephrasing

`VEGA_System_Audit_2026-08-19.md:261-262` states: *"Its stop and target are evaluated against a
mark that does not exist, so the close logic silently does not run."* The conclusion is right and
**the mechanism is wrong**, and the difference is load-bearing for the fix.

Nothing is evaluated against anything. Target, stop **and the mechanical DTE-window close** all
live inside the `if s and l:` branch of the chain lookup (`_ravens_or_legacy_close` is called from
exactly one place in the file). When the lookup fails the branch does not execute — no evaluation
occurs, against a stale mark or any other.

Why it matters: "evaluated against a bad number" implies the fix is *supplying a better number*.
"The branch never runs" means the fix must also make the **absence** a state the rest of the
system can see and act on — which is why §1.3(b) and the `UNMANAGED-AT-EXPIRY` escalation exist
at all. Fixing only the first reading would have left a position able to reach expiry unmanaged
while looking healthy.

Flagged here with the same discipline applied to the brief's 08-21 error above: a correction to
the audit is worth exactly as much as a correction to the brief.
Target, stop **and the mechanical DTE-window close** all live inside the `if s and l:` branch of
that same lookup (`_ravens_or_legacy_close` is called from exactly one place). When the lookup
fails, none of them run at all, and nothing raises.

So a position that goes unpriceable is not mismanaged — it is **unmanaged**, and it stays
unmanaged through expiration, because the DTE close that would mechanically settle it is behind
the same failed lookup. PSX, AMGN and XLE have been in that state for 9–12 cycles each.

(Mitigating fact, checked: all 11 open positions are comfortably OTM right now — XLE spot 63.58
vs short 56.00, PSX 242.29 vs 185.00, AMGN 442.36 vs 380.00. Nothing was silently lost. The
defect is that the system could not have known that.)

### 1.3 The fix

Two halves, matching the brief's two asks.

**a. Give the mark path an ungated view.** `get_options_chain()` and `get_call_options_chain()`
take a new keyword-only `apply_quality_gate: bool = True`. Every existing caller is byte-for-byte
unaffected. `_reprice_and_close_open()` is the only caller passing `False`. The two views cache
under separate keys so they can never serve each other, and the quality *reading* is still
computed and logged from the filtered count on both paths — an ungated read must not launder a
bad chain into a clean data-quality record.

**b. Make an unpriceable position an explicit state.** New in `outcome_logger`:
`MARK_LIVE` / `MARK_UNAVAILABLE`, `set_mark_unavailable()`, and `mark_is_stale()`. A position
that cannot be priced now carries `mark_status`, `mark_unavailable_since`,
`mark_unavailable_reason`, `mark_skips_consecutive`, `mark_last_attempt_at`. A successful
`set_mark()` clears them, so recovery is automatic and the flag cannot become permanent noise.

The stale `current_mark` is deliberately **left in place** — blanking it would destroy the last
real observation. The point is not to forget where the trade was; it is to stop that number
reading as current. `mark_status` is what downstream code consults.

Marking now fails in four named ways, each stamped and logged, none silent:

| Reason | Meaning |
|---|---|
| `chain fetch failed: …` | the fetch raised |
| `strike not in chain (chain depth N)` | legs absent from the chain |
| `legs present but not quotable (…)` | no market / crossed / absurdly wide |
| `implausible mark X outside [0, W]` | a broken print — a vertical can only be worth 0…width |

The last one is new and exists *because* of half (a): reading an ungated chain means occasionally
reading a bad quote, and acting on one is the same mistake as ignoring the position, pointed the
other way.

Leg-quotability for **marking** deliberately does not reuse `fetcher._option_record_is_usable`.
That predicate also demands volume or open interest, which is the right question when deciding
whether a market is liquid enough to *sell into* and the wrong one for a position already held —
a leg that hasn't traded today still has a quote. Reusing it would have re-imported the entry
gate through the back door.

**c. Visibility.** `paper_desk.py list` gains a `MARK` column plus a footer naming every stale
position and its reason; the dashboard gains a `Mark` column and a red banner when any position
could not be re-priced. `_mark_unavailable()` logs `MARK-UNAVAILABLE … Stop/target NOT evaluated
this cycle` every time.

**d. The escalation.** A position that goes dark *inside the 7-DTE close window* gets a distinct
`UNMANAGED-AT-EXPIRY … needs a human before expiry` line. Target and stop can wait for a quote;
the DTE close cannot, because no later cycle rescues it — expiration arrives on schedule. None of
the three current positions is in this window (all 39–43 DTE), which is precisely why it should be
wired now rather than the week it matters.

### 1.4 Verification

`tests/test_mark_availability.py` — 17 tests (13 at first writing; see §1.6). Against the **unfixed** code, **12 of 13 fail**.

Exactly one passes unfixed — `test_a_stale_mark_never_reaches_the_close_rules` — and it is
behavioural, not display. It asserts `_ravens_or_legacy_close` is never reached when the mark
could not be refreshed. The old code satisfied it by skipping everything; the new code satisfies
it by pausing deliberately. It is a ratchet against over-correcting this fix into "mark it off
whatever quote exists and run the stop anyway," which would trade a silent-skip bug for a
false-stop bug. Correctly not-failing.

**Test gap, stated rather than glossed:** none of the 13 cover the `paper_desk.py` display
changes (MARK column, stale footer, dashboard banner). Those were smoke-tested by hand —
`paper_desk.py list` renders, the dashboard emits `<th>Mark</th>` and 11 `live` cells. Display
only, no behaviour behind it, but it is untested and should not be described otherwise.

Full suite: **1109 passed**, no regressions.

One second-order defect found and fixed while writing those tests: splitting the chain cache by
view made it possible for one process to log the same chain-quality reading twice, double-counting
that ticker in every aggregate built on `data_quality_log.json`. The reading is now de-duplicated
per (ticker, DTE window) per process — restoring the invariant the single cache key used to give
for free — and `tests/conftest.py` clears that state between tests for the same order-dependence
reason the fixture already clears `ticker_profile`.

### 1.5 Cohort impact — none

Checked against the contract's own reset clause. `outcome_logger.cohort()` keys on
`fill_model | gate_basis | close_cohort`. This change touches none of the three: no gate moved,
no fill basis moved, no close *rule* changed. `get_options_chain`'s default is still `True`, so
selection sees exactly the chain it saw yesterday — pinned by
`test_the_entry_path_still_gets_the_gate`.

Spot-checked three ways before commit rather than asserted:

1. **Field-level.** The set of fields the mark path can write (`mark_status`,
   `mark_unavailable_since`, `mark_unavailable_reason`, `mark_skips_consecutive`,
   `mark_last_attempt_at`, `current_mark`, `unrealized_gross`, `unrealized_net`, `marked_at`,
   `mark_history`) intersected with the set `cohort()` reads (`fill_model`, `opened_at`,
   `logged_at`, `close_logic`) is **empty**.
2. **Diff-level.** `analysis/outcome_logger.py` has **zero deleted lines** — purely additive.
   `cohort()`, `gate_basis()`, `close_cohort()` and `GATE_BASIS_FIX_DATE` are untouched, and the
   diff modifies no config value in any file.
3. **Data-level.** Recomputing `cohort()` over all 79 graded rows under the patched module
   reproduces the existing distribution: 46 `natural|mid|credit_stop_1.5x_natural`, 18
   `mid|mid|…`, 11 `natural|mid|ravens_v1`, 4 `natural|natural|ravens_v1`.

A useful fact that fell out of (3): **3 of the 11 open positions are already in the target
cohort** (GDX, XLE, ARKK — the 08-10 batch). The clean count goes 1 → 4 as they close, with no
new entries required. That is most of what the cohort will gain in the near term, which raises
the stakes on §2 rather than lowering them.

The honest nuance, stated so it isn't discovered later: the fix does change *when* the existing
close rules get to run, because they currently do not run at all on three positions. That is
restoring the documented close logic, not altering it — but it is the one judgement call in this
change, and it's yours to confirm rather than mine to assume.

---

### 1.6 A regression this fix nearly shipped, caught by running it live

Worth recording because the mechanism is instructive. The first version of the leg-quotability
check required the specific side the natural basis needs — the short leg's ask, the long leg's
bid. It passed all 13 tests. The live 08:46 cycle then logged:

```
MARK-UNAVAILABLE NEE-80.0/77.5-… — legs present but not quotable
                                   (short bid/ask=0.36/0.0, long bid/ask=0.13/0.37)
```

NEE's short leg had no ask at that moment. But the file already contains a considered answer
for exactly that — *"Quote gap: degrade to mid rather than mark at zero, but say so — this mark
is optimistic relative to the position's own natural basis."* Requiring the side pre-empted a
documented degradation and replaced it with a refusal. **The fix for a silent-skip bug had
quietly made marking stricter than the bug it was fixing.**

Relaxed to "is there a market here at all, and is it not obvious nonsense," and the one-sided
case now degrades to mid as it always did.

The general form is the same defect class as §1.1a — a predicate written for one question
answering a different one — which is what made it easy to write and hard to see. So the
invariant is now pinned rather than argued: `test_the_marking_bar_is_a_strict_superset_of_the_
entry_filter` enumerates 243 quote combinations and asserts that **every** leg the old entry
filter accepted is still markable. Zero violations, 43 legs newly accepted. This fix is
arithmetically incapable of making marking rarer than it was.

Note what did *not* catch this: 13 passing tests, a full 1108-test suite, and a code review of
my own reasoning. Running the thing against a live market did.

---

## 2 · Item 2 — why entries stopped. Substantially revised.

Read-only diagnostic. The first version of this section said "the board qualifies nothing."
That is true of 4 cycles out of 34 and was the wrong headline.

### 2.1 The headline

**Zero entries since 2026-08-10** — the freeze was 08-14, so the frozen cohort has never
accumulated an entry. Every cycle logs `Board has no qualified trades this cycle`.

Except that most of them do not, and that is the finding.

### 2.2 Every market-hours cycle since 08-04, classified

34 cycles (69 more were correctly skipped as market-closed):

| Outcome | Count | What it means |
|---|---:|---|
| opened | 11 | all on or before 08-10 |
| **ran, open path SILENT** | **11** | cycle completed, open path logged *nothing at all* |
| **DIED — no `Finished` line** | **9** | the run stopped mid-scan and left its lock behind |
| blocked by a concurrent cycle | 7 | the two-scheduler race — see the correction below |
| board genuinely empty | 4 | the only cause the first draft reported |
| book full (15 open) | 1 | `No slots free` |
| board trade carried no gates | 1 | `SKIP TLT … missing=[all 11]` |

**"Board qualifies nothing" explains 4 of 34 cycles.**

**Two corrections to my own first pass at this table**, both caught by checking rather than by
reasoning:

- *I attributed the 7 blocked cycles to locks left behind by deaths.* Wrong. They are the
  cockpit-vs-task race already recorded in project memory as fixed. `logs/intraday_paper.log`
  holds **51 cockpit-spawned cycles** that are invisible in the task log, ending 2026-08-18
  14:35:49, and every blocked task cycle falls on 08-12, 08-14, 08-17 or 08-18 — inside that
  window, none after it. `INTRADAY_SCHEDULER_ENABLED = False`. Those cycles were also not lost
  work: the cockpit had just done it (`marked=10` on its final run). **Already fixed; not a live
  problem.**
- *I counted 10 deaths.* One is today's 08:35 run, which was mid-scan when I classified it and
  finished normally at 08:46:53. `run_auto_paper_cycle.ps1` pipes Python through `Out-File`,
  which flushes in chunks, so a healthy running cycle is indistinguishable from a dead one until
  it ends. Nine are genuine — historical runs with a later run after them.

So "half of every cycle was lost" was overstated. The honest claim is **9 genuine deaths out of
34**, and those are the live problem; the lock blocks are a solved one.

Corroboration for the deaths, independent of the classifier: a clean exit calls `_release_lock()`
and deletes the lock. The lock found at the start of this session held PID 35580 from the 08-19
14:35 run, and that process was gone. The 08-19 09:35 run logged `Removed stale automation lock`,
meaning the 08:35 run before it died holding one. The lock's existence is the death certificate.

### 2.2a The deaths have a strong lead — which should not be called the answer yet

Both 08-19 deaths stop mid-scan, and both carry this immediately before their output ends:

```
python.exe : 2026-08-19 08:35:03,390 [INFO] [main] Running morning scan ...
At ...\run_auto_paper_cycle.ps1:44 char:1
+ & $PythonExe "auto_paper_cycle.py" 2>&1 | Out-File -FilePath $logFile ...
    + FullyQualifiedErrorId : NativeCommandError
```

That is the exact mechanism the wrapper's own comment says was fixed: PowerShell 5.1 wraps every
stderr line from a native exe in a `NativeCommandError`, which under
`$ErrorActionPreference = "Stop"` aborts the script mid-run and leaves the lock behind. The
wrapper drops to `"Continue"` for the native call — and the error record is *still being
emitted*.

**Superseded — see §9.3.** This lead is a red herring for every death after 2026-08-09. The
`NativeCommandError` mechanism was real, but its fix landed in commit `bfffd32` on 08-09, and
only the 08-04 and 08-05 deaths predate it. Today's healthy cycle emits the identical record.
§9 has the actual root-cause work: three distinct causes, one of them live and daily.

### 2.3 The silent-open-path bug, and what it cost the diagnosis

Those 11 silent cycles were `_auto_open_from_board` reaching `if not tk or tk in open_tickers:
continue` — an unlogged branch. The board kept re-qualifying names the desk already held, every
one hit that `continue`, and the cycle wrote nothing.

In the log that is **identical to an empty board**. "The market is offering nothing" and "our
book is saturated on the names that qualify" are opposite diagnoses with opposite fixes, and
they produced the same blank line for two weeks. Fixed in commit `7c75484`; both states now say
which they are.

Direct evidence of the mechanism, from yesterday's final board: the single qualified trade was a
**META bear call** — and META was already open. It also carried `assessment_gates: MISSING`, and
`bear_call` is the structure audit §P2 flagged as gated by a contract the desk cannot read.
Three independent reasons it could not open; the ticker check fires first, silently.

### 2.4 The threshold change is not the cause — the timeline rules it out

You flagged this and it holds. The last entry was **08-10**; `CHAIN_QUALITY_MIN_RATIO` moved
0.30 → 0.50 on **08-14**. Four days and two entry-less sessions separate them, and the
proximate causes in that window were a full book (08-11), a stale lock (08-12) and the silent
ticker check (08-13). The threshold hike arrived into an already-stopped pipe.

### 2.5 Clustering — real, dormant, and now capped

65 of 79 entries (82%) share a minute with at least one other; 36 distinct entry minutes;
largest batch 5. Not an artifact of go-live day — the normal shape of this system's entries.

Batching is a function of **scan cadence, not watchlist size**: the cycle opens up to
`MAX_NEW_OPENS_PER_RUN` from one board snapshot in one loop, all stamped identically.

### 2.6 Regime coverage — not measurable, for a benign reason

`vix_at_entry` is `None` on all 79 paper trades, so "how many distinct regimes are represented"
**cannot be answered from the ledger**. Not a bug: all 79 came from the retired
`auto-paper`/`candidate` openers. The current opener threads it from
`board.market_context.vix.current` (verified populated). It will populate from the next entry on.

### 2.7 The concentration nothing had surfaced

**10 of 11 open positions share expiration 2026-09-18.** 91% of the book resolves on one date.
Distinct tickers and distinct entry days do not make those independent. Now capped.

### 2.8 Cohort status

```
 46  natural|mid|credit_stop_1.5x_natural
 18  mid|mid|…
 11  natural|mid|ravens_v1
  4  natural|natural|ravens_v1     ← the frozen cohort (1 closed + 3 open)
```

**1 of 30 closed** — PEP, a win, 08-19. The 3 open clean-cohort positions are GDX, XLE and ARKK,
so the count reaches 4 without a single new entry. That is the near-term ceiling, and it is why
§2.2 matters more than the entry caps do.

---

## 3 · Item 3 — implemented (3a, 3b), per the sign-off addendum

### 3a. Entry diversification — implemented, not deferred

Your reasoning is right and mine was wrong: the reset cost is paid whenever it is paid, and it
is cheapest at a count of 1. "Nothing to test it against" applies to behaviour, not to
implementation. Shipped in `7c75484`:

| Cap | Value | Why this one |
|---|---:|---|
| `MAX_NEW_OPENS_PER_RUN` | 2 | was an *undocumented env default of 5* — the 08-10 batch of four never hit it |
| `MAX_NEW_OPENS_PER_DAY` | 3 | a per-run cap alone permits 14/session on an hourly cadence; one day ≈ one regime |
| `MAX_OPEN_PER_EXPIRATION` | 4 | §2.7 — with 15 open max, forces at least four settlement dates |

Forward-looking only, and tested to be: the live book is already at 10 on one expiration against
a cap of 4 and is left untouched. `test_the_caps_never_touch_an_existing_position` exists because
a cap that reached backwards would close six positions to satisfy a rule they were never opened
under — destroying the cohort it was added to protect.

**These are entry gates. They restart the 30-trade count.** Deliberately, now, at count 1.

The tension, stated rather than hidden: tightening throughput while the pipe is clogged looks
perverse. It is not, because §2.2 shows the clog is operational — but if unblocking entry flow
shows these numbers are too tight, they are three integers in one config block.

### 3b. Stale comments — fixed, and the IV-rank finding stands

`config.py`'s GATE ENFORCEMENT CONTRACT block named `_candidate_passes_minimum()` and
`_auto_open_from_candidates()` as the enforcers. The latter is deleted; the former is in the
orphaned unreachable subtree. Corrected to name `_auto_open_from_board()`, which genuinely does
enforce `REQUIRED_GATES`. Comment-only — every changed line in that diff begins with `#`.

The IV-rank finding is unchanged and verified at `main.py:647-653`: below-threshold is a hard
reject **only** when `iv_rank_method == "HISTORY"`; on the approximated path it writes
`tech["iv_rank_warning"]` — and a repo-wide search returns exactly one hit, the line that writes
it. Nothing reads it. The thinnest-data case is also the silent one. Documented in the comment,
**not changed**: promoting it into `REQUIRED_GATES` is a gate change and belongs with the §4
decision.

---

## 4 · The `CHAIN_QUALITY_MIN_RATIO` diagnostic — run, segmented, and it deflates the story

Read-only. Computed from measured chain ratios rather than by mutating the config.

### 4.0 First, a correction to my own §4 figure

The original §4 said *"54% of the watchlist is unscannable for DATA reasons alone,"* citing
buckets of 16 and 14. **Both numbers were wrong** — an extraction bug in my tally, reading
`reasons` where the field is `reason`, so most rows fell into a catch-all. The true split from
today's 08:35 board is **31 and 25, totalling 56 of 56 tickers**. Exactly the class of
unverified arithmetic this session opened by correcting in someone else's document.

### 4.1 The two buckets have different causes — confirmed, not assumed

| Bucket | n | Cause |
|---|---:|---|
| `NO_VALID_SPREAD` | 31 | chain present; **zero** short-strike candidates survived selection |
| `NO_OPTIONS` | 25 | chain fetch returned `[]` — the quality floor, or no chain at all |

`pair_selection_diagnostics` settles the first bucket. Across all 31, every one had
`short_candidates_count: 0`, and the reasons are:

```
234 occurrences across 30 tickers   short_liquidity_below_floor
 38 occurrences across 14 tickers   short_delta_too_high
  5 occurrences across  3 tickers   short_quote_not_tradeable
```

**A per-strike liquidity floor, not the chain-quality ratio.** Your instinct was right and the
answer is stronger than "not yet confirmed": these 31 are confirmed to fail on a *different
gate*, so `CHAIN_QUALITY_MIN_RATIO` cannot recover any of them.

### 4.2 How much would 0.50 → 0.30 actually recover? Six tickers.

Per-ticker measured ratios for the 25 `NO_OPTIONS` names:

```
would pass at the CURRENT 0.50 floor : 1/25   (NEE 61%)
would pass at a 0.30 floor           : 7/25   (+ ABBV 40%, GDX 49%, IWM 33%,
                                                RCL 44%, XBI 36%, XLE 45%)
NET RECOVERED                        : 6  =  11% of the 56-ticker watchlist
```

The rest are not threshold-blocked at all. Fourteen of the 25 have raw chains of **twelve
contracts or fewer** — AMD 2, BAC 1, GS 5, JNJ 4, and FCX/KO/PFE/WMT at **zero**. yfinance is
returning essentially nothing for those names, and no floor recovers a chain that does not
exist. Four more sit below 0.30 anyway (AMGN 23%, PSX 27%, PEP 25%, SCCO 20%).

**So: 54% → 25% → 11%, and the last figure is measured.** The "one 08-14 change explains both
symptoms" story does not survive. It explains the marking failure (§1, now fixed) and about a
ninth of the scanning failure.

One point in its favour, worth keeping: **XLE and GDX are two of the six**, and they are two of
the three open clean-cohort positions.

### 4.3 If the floor does get lowered, it is a gate change

Flagged before anyone reaches for it. `CHAIN_QUALITY_MIN_RATIO` decides which tickers are
eligible — `config.py` says so in its own comment — so changing it needs the same verification
§1.5 got, against all three components of `outcome_logger.cohort()`, and it restarts the count.
"It started as a read-only diagnostic" does not carry over to the fix.

**Recommendation: not yet.** Six tickers is a poor trade for a cohort reset on its own. If it
ships, it should ship bundled with something that earns the reset — most plausibly whatever
comes out of §2.2, since that is where the actual entry throughput went.

---

## 5 · What actually blocks entries — the next diagnostic, unranked by anyone so far

§2.2 is the finding of this session and it is not root-caused. **Nine of 34 cycles since 08-04
died mid-run**, two of them after every previously-known cause had been fixed, and this sits
upstream of every gate question in this document: a cohort cannot accumulate at any threshold if
a quarter of the cycles never finish.

Two threads:

1. **Why do cycles die?** Nine since 08-04, including 08-19 08:35 and 14:35. §2.2a has the lead
   — `NativeCommandError` is still emitted from `run_auto_paper_cycle.ps1:44` — and its
   limitation: today's healthy cycle emitted the same record. The question is what makes it fatal
   sometimes. Two prior root causes are documented in that file (cp1252 stdout, and this same
   error under `$ErrorActionPreference = "Stop"`), both fixed; this is a third. Note that two of
   the nine produced **zero** log lines, dying before the market-hours check prints — those may
   be a different failure from the mid-scan ones.
2. **`board.qualified_trades` carrying `assessment_gates: MISSING`.** Seen on TLT (08-14) and on
   the 08-19 META bear call. The board publishes a trade as qualified while omitting the
   annotations the desk requires in order to open it — the leak shape `REQUIRED_GATES` exists to
   catch, pointed the other way. Cheap to check, and currently a hard blocker on board trades
   that do reach the desk.

---

## 6 · Commits

Branch `fix/vega-mark-availability-2026-08-20`, off `main` at `5a3cd90`. **Not pushed.**

```
12bcd4a  fix(vega): the selection gate was deciding whether open positions could be managed
         data/fetcher.py, analysis/outcome_logger.py, auto_paper_cycle.py, paper_desk.py,
         tests/conftest.py, tests/test_mark_availability.py (17 tests)

7c75484  feat(vega): cap how far entries may cluster, and stop a saturated book logging as
         an empty one
         config.py, auto_paper_cycle.py, tests/test_entry_diversification.py (12 tests)
```

Split deliberately: item 1 is a restoration and stays independently revertible; the caps are a
deliberate gate change and reset the cohort count. Full suite **1125 passed**. Ledger backed up
to `logs/vega_outcomes.pre-markfix-2026-08-20.jsonl` before anything ran.

---

## 7 · Live verification, 2026-08-20 08:35 cycle

First cycle with the fix. Completed `exit=0`. Every previously-silent skip is now stamped:

```
MARK-UNAVAILABLE PSX  — legs present but not quotable (short 0.0/0.8, long 0.0/3.0), last good mark 7d ago
MARK-UNAVAILABLE XLE  — legs present but not quotable (short 0.01/0.18, long 0.05/0.12), last good mark 5d ago
MARK-UNAVAILABLE AMGN — legs present but not quotable (short 0.61/2.09, long 0.23/1.73), last good mark 6d ago
```

**XLE's reason changed from `chain depth: 0 strikes` to a specific quote.** The ungated chain now
contains the 56/55 legs; they are simply unpriceable at 08:46 (a 179%-of-mid spread on the short
leg). That is the fix working: the position went from invisibly stale to visibly paused with a
reason, an age and an explicit note that its stop and target did not run.

**Stated plainly: this does not by itself recover XLE, PSX or AMGN.** It converts an unexplained
silence into a diagnosis. Whether they re-mark depends on their quotes tightening later in the
session — which is now observable rather than guessed at.

`marked=2` on that cycle, against 9 on a comparable cycle yesterday. Most of that is time of
day: 08:46 CDT is earlier in the session than any of yesterday's successful marks (08:47–12:48
CDT), and chains fill out as the session goes on. The leg predicate itself cannot be responsible
— §1.6 proves by enumeration that it is a strict superset of the old filter. The 09:35 cycle is
the like-for-like comparison and should be checked before this is called settled.

---

## 8 · Open for you

1. **Push the branch / merge to main?** Two commits, not pushed.
2. **§4 — hold the floor at 0.50?** My recommendation: yes, for now. Six tickers does not earn a
   reset alone.
3. **§5 — root-cause the cycle deaths and the lock next?** This is where the entry throughput
   went, and it outranks every threshold question in this document.

---

## 9 · Root cause of the cycle deaths

Asked for directly. Three of nine are explained, one of those recurs **daily and is still
happening**, and the remaining six cannot be closed out from here — for a reason that is itself
the most actionable finding.

### 9.1 First, the deaths are real — confirmed independently of the log

The wrapper log cannot prove a death, because `run_auto_paper_cycle.ps1` pipes Python through
`Out-File`, which flushes in chunks: a killed process loses its buffered tail, so "the log stops"
and "the process stopped" are different events. Two independent sources settle it:

- **The ledger.** All seven post-fix dead cycles produced **zero marks**. Every finished cycle in
  the same window produced 9–15. The work genuinely did not happen.
- **`logs/run.log`**, which `main.py` writes directly rather than through the pipeline, so it is
  unbuffered and gives true lifetimes.

### 9.2 What the true lifetimes show — two distinct populations

| dead cycle | main.py ran | died at |
|---|---:|---|
| 2026-08-10 13:35 | 1491s | 13:59:51 |
| 2026-08-11 13:35 | 1455s | 13:59:15 |
| 2026-08-11 09:35 | 37s | 09:35:37 |
| 2026-08-17 11:35 | 66s | 11:36:06 |
| 2026-08-19 14:35 | 15s | 14:35:15 |
| 2026-08-19 08:35 | 3s | 08:35:03 |
| 2026-08-13 09:35 | — | wrote nothing at all |

*(control: healthy cycles run main.py for 489–540s)*

Group A dies at ~24 minutes; Group B inside 66 seconds. And Group A's two deaths land at
**13:59:51 and 13:59:15** — the same wall-clock minute on different days, which is a coincidence
worth noticing and not one I can currently explain.

None of the nine ends on a traceback or a PowerShell error. They stop on ordinary mid-scan
output, mid-work, with `Finished` never written. **That is an external kill, not a crash.**

### 9.3 Explained: 2 of 9 — the pre-08-09 wrapper bug

`2026-08-04 08:37` and `2026-08-05 08:37` predate commit `bfffd32` (2026-08-09), which introduced
the `$ErrorActionPreference = "Continue"` guard around the native call. The 08-05 death's final
log line is literally:

```
    + FullyQualifiedErrorId : NativeCommandError
```

That is the documented mechanism, firing before its fix existed. **Closed — and it means the
`NativeCommandError` lead from §2.2a is a red herring for everything after 08-09:** today's
healthy cycle emits the identical record. Withdraw it as an explanation.

### 9.4 Explained: 1 of 9 — and this one is live, daily, and hitting the close scan

The task's trigger:

```
StartBoundary : 2026-08-19T08:35:00-05:00
Repetition    : Interval PT1H   Duration PT6H   StopAtDurationEnd TRUE
```

`08:35 + 6h = 14:35`. The **final repetition fires at exactly the instant the repetition window
expires**, and `StopAtDurationEnd` means "stop all running tasks at the end of the duration." The
14:35 instance is started and then immediately stopped by the scheduler.

The 2026-08-19 14:35 cycle died **15 seconds in with zero work done**, which is exactly that
signature. 08-19 is the only complete day under this trigger, and its 14:35 fire is one of only
two that died.

**This is the daily close-scan cycle** — the end-of-day resolution run, the one that matters most
for marking and closing positions before the session ends. Under the current schedule it has
never completed.

**Testable prediction: today's 14:35 CDT fire will die the same way**, in seconds, with zero
marks. That is the check to run before accepting this.

Fix, when you want it — two settings, neither touching the repo:

```
Duration PT6H  ->  PT7H        (window ends 15:35; the 14:35 fire gets a full hour)
                               a 15:35 fire is 16:35 ET and exits on the market-closed guard
ExecutionTimeLimit PT72H -> PT30M    a real runaway guard; a healthy cycle takes ~13 min
```

Alternatively `StopAtDurationEnd = false`, but only *with* the `ExecutionTimeLimit` change — at
PT72H there would be nothing left to stop a genuinely hung instance.

**Do not apply either while a cycle is running:** re-registering a task terminates its running
instances, which is plausibly what happened to 08-19 08:35 (the old task XMLs in
`backups/scheduled_tasks/` are timestamped 2026-08-19 08:06, the morning the schedule was
rewritten; today's 08:35 fire ran fine).

### 9.5 Unexplained: 6 of 9 — and why they cannot be closed from here

**`Microsoft-Windows-TaskScheduler/Operational` is DISABLED** (`IsEnabled: False`). There is no
record of a single task start, stop or termination on this machine. Every conclusion above had to
be reconstructed from application logs, and the six remaining deaths cannot be reconstructed at
all, because the one artifact that would name the terminator does not exist.

**Enabling it is the single highest-value action here**, and it needs one elevated command:

```
wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
```

This session is not elevated, so I could not run it. With it on, the next death names its own
cause — including today's predicted 14:35 one.

### 9.6 Ruled out, so nobody re-treads them

| hypothesis | verdict |
|---|---|
| `ExecutionTimeLimit` timeout | **No.** PT72H. |
| Machine sleep / hibernate / reboot | **No.** Zero Kernel-Power 41/42/107/109/6008 or shutdown events at any death time since 08-04; the only System events in range are evening clock syncs. |
| Overlapping task instances | **No.** `MultipleInstancesPolicy: IgnoreNew`, and a cycle takes ~13 min against a 60-min interval. |
| `JARVIS_Watchdog` killing it | **No.** Runs every 10 min, but its only process check is Docker Desktop and it kills nothing. Its trigger also starts 2026-08-18, after five of the nine. |
| Cockpit / task collision | **Only 1 of 6.** A cockpit cycle ended at 13:42:59 during the 08-10 13:35 run; the other five have no cockpit activity within ±20 min. |
| Two wrapper instances running at once | **No** — and this one was my own error. A process query filtering on `CommandLine -match 'run_auto_paper_cycle'` matches *the query process itself*, because the pattern appears in its own command line. The "duplicate wrapper" was my `Where-Object` finding itself. |
| `StopOnIdleEnd: true` | **Unlikely, not cleared.** It is set on every VEGA task, but `RunOnlyIfIdle` is false, and Windows only honours idle-stop when the task is idle-triggered. Worth clearing anyway since it costs nothing. |

### 9.7 Where this leaves it

- **2 of 9** — a fixed wrapper bug. Closed.
- **1 of 9** — `StopAtDurationEnd` killing the daily close scan. Live, recurring, fixable in two
  task settings, and testable at 14:35 today.
- **6 of 9** — open, and they stay open until task history is enabled. Nothing in the application
  logs can name what stopped those processes.

The honest summary is that the deaths are **not one bug**. They are at least three, one of which
is still running daily against the most important cycle of the day.
