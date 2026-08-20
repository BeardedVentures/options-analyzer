# VEGA — System Analysis & Audit

**Prepared for third-party review · 2026-08-19**
Repository: `BeardedVentures/options-analyzer` @ `main` `12b89f3`
Subject: an automated options premium-selling research system, paper-traded, single operator.

---

## How to read this document

Every quantitative claim was read from the live system on 2026-08-19 and the command that
produced it is recoverable from the repo. This matters because **this project has a documented
history of internal review documents being wrong on checkable facts** — a prior handoff asserted
gate enforcement lived in a function that had already been deleted, and a "code-validated" audit
described a cohort field that was never written. A reviewer should treat prior VEGA documents as
hypotheses and this one as no different: the figures are current, the interpretations are
arguable, and §9 lists what I would attack first if I were reviewing it.

**The single most important thing to understand before evaluating anything else:** VEGA has
**one** trade in its history that is scientifically usable. Not 68. One. Everything in §4
explains why, and no performance claim in this system should be read without it.

---

## 1. What the system is

VEGA screens a 56-ticker watchlist for defined-risk options credit spreads, publishes a ranked
"board", paper-trades from that board automatically, and grades the results. It does not place
real orders.

| Dimension | Value |
|---|---|
| Python files | 106 |
| Lines of Python | 36,898 |
| Analysis modules (`analysis/`) | 29 |
| Test files / tests | 46 / **1,096 passing** |
| Commits | 98 |
| Watchlist | 56 tickers |
| Strategies enabled | bull put spread, bear call spread, iron condor |
| Market data | yfinance (free, ~15-min delayed). No paid feed. |
| Execution | Paper only. Nothing places orders. |
| Deployment | Single Windows machine, one scheduled task, no CI |

### Architecture

```
  Windows Task (hourly 08:35–14:35 CDT)
        │
        └── auto_paper_cycle.py ── the only driver
              ├── main.py               build the BOARD  → logs/scan_latest.json
              ├── vega_candidates.py    enumerate + record gate results (measuring only)
              ├── _auto_open_from_board  open ONLY what the board recommended
              ├── _reprice_and_close     mark open positions, apply stops/targets
              ├── _resolve_predictions   score matured falsifiable claims
              ├── _record_direction_forecasts   NEW — dated directional claims
              └── _grade_shadow_book     NEW — grade recommendations, opened or not
```

Ledgers (JSONL, append-oriented, `logs/` is git-ignored):

| Ledger | Rows | Purpose |
|---|---|---|
| `vega_outcomes.jsonl` | 244 | trades: modeled / open / closed |
| `vega_shadow_book.jsonl` | 165 | every board recommendation, graded |
| `vega_predictions.jsonl` | 39 | falsifiable claims with resolution dates |
| `vega_counterfactuals.jsonl` | — | rejected candidates (**stale since 2026-08-10**) |

---

## 2. What is genuinely good here

Stated first because the rest of this document is critical, and a reviewer should know what
would be lost by dismissing the system.

**Cohort discipline is real and enforced in code.** `outcome_logger.cohort()` keys every trade
as `fill_model | gate_basis | close_logic` and `analysis_eligible()` refuses to let a trade
inform a base rate unless it was both selected and filled on prices the desk could achieve. The
justification is empirical, not aesthetic — see §4. Most retail-scale systems pool everything and
report one number.

**The system refuses to guess, in many places, deliberately.** Unknown earnings date fails
closed at entry. A board built on stale quotes cannot be opened from. A structure the desk cannot
mark cannot be opened. An unexpired trade has no outcome rather than an inferred one. A cohort
below its minimum sample reports a count instead of a rate.

**Forecast grading uses Brier *decomposition*, not accuracy.** `predictions.grade()` reports
reliability, resolution, uncertainty and skill, and its verdict leads with **resolution** — the
question raw Brier cannot answer. The docstring states the trap explicitly: a forecaster that
says the base rate about everything scores well-calibrated and has measured nothing.

**Defects are documented at the site of the fix.** Inline comments record what broke, when, and
what evidence forced the change. This is unusual and it materially accelerated this audit.

**Volatility forecasting is empirically grounded.** `vol_forecast.py` corrects trailing realised
vol using a measured bias table (35,774 observations, 20 names, 8 years). `price_projection.py`
chose lognormal-plus-forecast-sigma over three alternatives on ~14,900 held-out observations by
coverage test, and **rejected drift** on evidence (sector relative strength: rank correlation
+0.01 to −0.04 at every horizon, none significant).

---

## 3. Current state of the evidence

### 3.1 The trade ledger

244 rows: 68 closed, 11 open, 165 modeled (recommendations).

| Cohort (`fill_model \| gate_basis \| close_logic`) | Closed | Wins | Net P/L |
|---|---:|---:|---:|
| `natural \| mid \| credit_stop_1.5x_natural` | 43 | 2 (4.7%) | −$3,342.88 |
| `mid \| mid \| credit_stop_1.5x_natural` | 18 | 13 (72.2%) | +$152.12 |
| `natural \| mid \| ravens_v1` | 6 | 1 (16.7%) | −$328.96 |
| **`natural \| natural \| ravens_v1`** | **1** | **1 (100%)** | **+$51.84** |

**Analysis-eligible closed trades: 1.** Target: 30.

Exit reasons across all 68 closes: `auto-stop-loss` 45, `auto-target-profit` 16, `wolf-stop` 5,
manual 2. **The stop accounts for 74% of all exits** — the system is overwhelmingly being taken
out by its stop rather than reaching expiry, which is the single most important structural fact
about its realised results.

### 3.2 The first usable trade

```
PEP 130/125 put spread · opened 2026-08-10 · closed 2026-08-19 18:48
entry $0.77 → exit $0.23 · +$51.84 net · 9 days held · true_pop 0.8113 · IV rank 61
exit reason: auto-target-profit
```

One trade. It is a win. It supports no conclusion whatsoever, and the system's own
`MIN_SAMPLE`/`COHORT_TARGET_CLOSED_TRADES = 30` machinery says so.

### 3.3 The shadow book (new this session)

165 recommendations graded; **1 expired, 0 priced.**

| Cohort | Recommended | Unresolvable | Reason |
|---|---:|---:|---|
| `shadow\|bull_put_spread\|unpriced` | 77 | 4 | recorded today, no forward bar yet |
| `shadow\|bear_call_spread\|unpriced` | 54 | 49 | **no expiration recorded** (pre-fix) |
| `shadow\|iron_condor\|unpriced` | 34 | 32 | **no short strike recorded** (pre-fix) |

81 rows are permanently unresolvable: the data was never captured and cannot be reconstructed.
Fixed forward only. Upcoming settlements: **29 on 2026-08-21**, 4 on 08-28, 44 on 09-18.

### 3.4 Falsifiable claims

39 claims, **0 resolved**, first maturing 2026-08-23. Types: `strike_holds` 15,
`strike_untouched` 15, `direction` 9 (all BTC). The grading machinery is complete and has
never produced a graded result.

---

## 4. The central finding

**VEGA's headline win rate is an artefact of a pricing defect, not a measurement of its model.**

Split by fill model, the ledger's two populations are almost perfectly inverted:

- `mid` fill: **13 of 18 wins (72%)**, +$152
- `natural` fill: **4 of 50 wins (8.0%)**, −$3,620.00

The mechanism is arithmetic. `mid` overstated achievable credit by ~75%, so a target set at 65%
of an inflated credit was easy to reach and a stop set at a multiple of it sat far away. Median
natural credit is $0.47/share (n=50, range $0.07–$1.40) against a median $2.16 round-trip cost,
so a 1.5× stop fires on a $23.50 move per contract — inside the position's own bid-ask noise.

Two independent defects, both since fixed, contaminate everything before 2026-08-08:

1. **Fill model** — trades booked at mid, a price no fill could achieve.
2. **Gate basis** — the credit floor and ranking score read the *mid* credit while the desk
   filled at *natural*, so candidates cleared a $19 floor on $31 of mid credit and opened for $9.

A trade with either defect measures the leak, not the strategy. Excluding them is not discarding
data; it is declining to average a thermometer's readings with the readings it took while broken.

**Consequence for a reviewer:** no statement about VEGA's edge, win rate, calibration or
profitability is currently supportable. The instrument to answer those questions exists and is
running. It has one data point.

---

## 5. The binding constraint: data quality

The system is starved of inputs, and this — not model quality — is what currently limits it.

**Today (2026-08-19): 22 of 56 watchlist tickers (39%) were skipped** for failing the 50%
chain-quotability floor. Skip events by day:

```
08-10   45      08-14  794      08-18  681
08-11   30      08-17  264      08-19  183
08-12  202      08-13   80
```

Typical log line:

```
SKIP_DATA_QUALITY PSX: only 11/44 (25%) of the yfinance chain is quotable, floor is 50%
  -- skipping this ticker rather than scoring a chain that is mostly absent.
```

**Across 92 recorded scans, 41 (45%) produced zero qualified trades.** Many collapse to
`no candidates — chain 0 → priced 0 → OTM 0`.

The refusal behaviour is correct — scoring a mostly-absent chain would be worse. But the
downstream effect is that the validation cohort accumulates at a rate set by a free data feed,
and the 30-trade target is months away at the current rate.

The project has a standing decision not to buy paid data until the free path is demonstrably
maxed out. That decision is defensible, and a reviewer should note that it also **caps how fast
any of the open questions can be answered.**

---

## 6. Defects found and fixed this session

Each was live in production, each is verified fixed, each has a regression test.

**1. Call-side recommendations were structurally ungradeable.** `multi_strategy._base()` emitted
the expiry as `expiration_display`; every consumer reads `expiration`. 81 of 158 modeled rows
carried `expiration: null` and trade ids containing the literal string `None`. Nothing raised —
a missing key reads as `None`.

**2. `modeled_credit_per_share` held two different prices.** MID on the bull-put path, NATURAL
on the call-side path, indistinguishable after the fact. Pricing P/L from it would have
reproduced the defect in §4.

**3. Spread width was negative on all 49 bear-call rows.** `short − long` is negative on the call
side; the fallback assumed a put spread.

**4. Every full cycle reported exit 1 while succeeding.** `NameError: cand_path` on the final
statement, after all work completed and after the lock was released. Six runs. Every postmortem
about a "dead re-mark loop" was reasoning from an exit code that was lying in both directions.

**5. Two schedulers were racing.** The cockpit fired hourly at `:21`, the Windows task every two
hours at `:35`, and a cycle takes 13–16 minutes. Three of four task fires on 08-18 did no work.

**6. The desk refuses 100% of call-side trades.** `assessment_gates` is attached at exactly one
place, inside the bull-put path. Call-side trades reach the board with no gate evidence and are
refused with `missing=[all 11] failed=[]`. **This remains open by design decision** — see §7.

---

## 7. Open issues, ranked

### P0 — a validation-cohort position is running unmanaged

**XLE has not been marked since 2026-08-14 09:42.** Five days stale. GDX and ARKK, opened in the
same batch, were both marked today at 13:48.

```
GDX   82/81   marked 2026-08-19T13:48
XLE   56/55   marked 2026-08-14T09:42   ← 5 days stale
ARKK  75/70   marked 2026-08-19T13:48
```

Cause: `Reprice: strike not found in chain for XLE ... (chain depth: 0 strikes)` — 5 occurrences
today, 7 on 08-18, recurring since 08-12. Same failure on AMGN, PSX and NEE.

**Why this is P0:** an unmarked position cannot stop out. Its stop and target are evaluated
against a mark that does not exist, so the close logic silently does not run. XLE is **1 of only
3 remaining clean-cohort positions** — a third of the validation sample is currently unmanaged,
and the cohort is the entire point of the exercise.

### P1 — data quality (see §5)

39% of the watchlist unscannable today; 45% of scans produce nothing. Everything else is
downstream of this.

### P2 — the call-side gate contract

`bear_call_spread` is enabled in config and structurally unopenable by the desk. The trades are
not ungated — `strategies.evaluate` gates them — but by a *different contract the desk cannot
read*. `REQUIRED_GATES` contains put-side concepts (`support_shelter`, `otm_buffer`), so this
needs a per-strategy contract rather than a straight application. Deliberately deferred: the
operator chose shadow-grading over opening, so these are now measured without being traded.

### P3 — housekeeping with real teeth

| Item | Detail |
|---|---|
| Orphaned subtree | `_pick_new_trades`, `_candidate_passes_minimum`, `_candidate_score` still present, guarded by a reachability test, "deliberately for one release" — that release has passed |
| Stale comments | `config.py:243` references `_auto_open_from_candidates()` (deleted); `config.py:249` claims IV rank is gated in `_pick_new_trades()` (orphaned) |
| IV-rank soft-fail | `main.py:649` — when IV rank is *approximated* rather than from history, a below-threshold reading becomes a warning, not a block. Thin-data tickers pass with IV rank under 45 and no gate catches it, because enforcement sits upstream of the gate contract |
| Counterfactuals stale | Ledger not rebuilt since 2026-08-10; 20 candidate snapshots on disk unprocessed |
| `MODELLED_FILL_RATIO` | One global 0.78 constant, measured once on 158 candidates (2026-08-10). Not bucketed by ticker, width or liquidity |
| No CI | 1,096 tests, run manually. Equivalence tests exist but are not automated |

---

## 8. Governance and method

**The cohort contract** (`config.py`, frozen 2026-08-14) declares what is being validated —
*"Sell a defined-risk credit spread on the natural basis, hold to stop or expiry"* — explicitly
excludes rolls and 21-DTE management, sets a 30-trade target, and states that changing any gate,
fill basis or close rule mid-run restarts the count. This is unusually disciplined and it is
enforced socially, not mechanically: nothing prevents an edit.

**Additions this session were designed not to disturb it.** The shadow book keys its own cohort
and never writes to the trade ledger. The direction forecast writes only claims. Neither reaches
selection, sizing or execution.

**A note on method.** During this session, two errors in new code were caught only by running it
against real market data, after unit tests passed:

1. A directional tilt held constant across horizons — implying >100% annualised drift at the
   one-day horizon, because drift accumulates linearly while sigma grows with √t.
2. A flat band that made *every* claim come out "flat" — a constant forecast, which has zero
   resolution by construction and would have graded as beautifully calibrated having measured
   nothing.

Both passed the test suite. Neither would have been visible without a smoke test on live data.
A reviewer should weight this: **the test suite is large and did not catch either.**

---

## 9. What I would attack first, as a reviewer

1. **Challenge the single-trade result.** PEP is a win at 81% modelled POP. Ask what the other
   three cohort members are doing and whether the sample is a batch effect — all four were opened
   in one 09:38 batch on one day, in one market regime, and three of the four (GDX, XLE, ARKK)
   are ETFs rather than single names. Batch-opened cohorts are correlated samples, and 30 such
   trades are not 30 independent observations. **This is the weakest link in the validation design and it is not addressed
   anywhere in the codebase.**
2. **Interrogate the stop.** 74% of all exits are stops. If the stop is mis-sized relative to
   credit and spread noise (§4 suggests it is), the cohort may be measuring the exit rule again
   rather than selection — the exact failure the calibration engine already hit once.
3. **Verify the shadow book's put/call asymmetry independently.** It is new, it has 34 tests, and
   its central claim — that call-side breaches are measured on the High — is the kind of thing
   that silently reports "everything held" if wrong.
4. **Check whether `MODELLED_FILL_RATIO = 0.78` still holds.** Measured once, on one day, on 158
   candidates, and used globally.
5. **Ask what happens when the free data feed degrades further.** 39% of the watchlist was
   unscannable today. There is no fallback source.
6. **Audit the gap between enabled and tradeable.** Three strategies are enabled; one can open.

---

## 10. Assessment

VEGA is a **well-engineered measurement instrument that has not yet produced a measurement.**

Its epistemics are better than its results — the cohort discipline, the refusal-to-guess
defaults, the Brier decomposition and the habit of documenting defects at the fix site are all
above what the trading results currently justify. The system is unusually honest with itself:
most of the significant findings in this audit came from evidence the codebase had already
written down about its own failures.

The risks are concentrated in three places. **One:** the evidence base is a single trade, and the
validation cohort is a correlated batch that nothing currently accounts for. **Two:** the input
data is free, degrading, and already the binding constraint on how fast anything can be learned.
**Three:** the system has repeatedly shipped defects that unit tests could not see — field-name
mismatches, sign errors, a lying exit code — and the ones found this session were found by
looking at production data, not by the 1,096 tests.

Against that: nothing here is trading real money, every defect found this session was fixed with
a regression test that provably catches the original, and the machinery needed to answer the open
questions is now built and running. The next four weeks are decisive — 29 shadow settlements on
2026-08-21, the first graded forecasts around 09-03, and 44 more settlements on 09-18.

**Recommended reading order for a reviewer:** `config.py` (the COHORT CONTRACT block),
`analysis/outcome_logger.py` (`cohort` / `analysis_eligible`), `analysis/assessment.py` (the
gate contract), `auto_paper_cycle.py` (`_auto_open_from_board` and its three refusals), then
`analysis/shadow_book.py` and `analysis/direction_forecast.py` for what was added this session.

---

*Companion: `VEGA_Session_Log_2026-08-19.md`. Figures current as of 2026-08-19 18:50 CDT.*
