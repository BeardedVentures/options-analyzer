# VEGA Independent Review — 2026-09-04

Read-only audit. Production code, config, logs, and data were not modified.

## §1 — My findings before reading their reports

This list was written from the implementation and on-disk data paths before opening `reports/*.md` or `VEGA_METHODOLOGY.md`.

1. `set_close()` appears to fail open: when `actual_fill_credit` is absent it falls back to modeled credit, allowing modeled P/L to be stored as realized P/L. Check with a synthetic modeled row closed without a fill.
2. `_read_all()` detects damaged outcome-ledger lines but returns only parseable rows; the next full rewrite can drop the damaged records despite the stated preservation intent. Check with one malformed line followed by a write.
3. Ledger writes are atomic per process but not concurrency-safe: read-modify-write has no inter-process lock, so two writers can each replace the other’s additions. Check with concurrent appenders or inspect the write contract against scheduler/process topology.
4. The test isolation fixture does not cover every live data artifact: its data-quality entry names `data_quality_log.json`, while the protected live file is `data_quality_log.jsonl`. Check module constants and run a focused writer test under the fixture.
5. `gate_basis()` derives a time-sensitive selection regime from an open date, while the documented fix occurred at a commit time; trades on the boundary day may be mislabeled. Check commit timestamps and all ledger opens around 2026-08-08 through 2026-08-10.
6. Counterfactual deduplication keys omit strategy/side, so distinct put and call observations with the same ticker, strikes, and expiration can collapse. Check `dedup_key()` with two such records.
7. Call-side and condor paths likely do not satisfy the same gate/gradeability contract as the bull-put path, creating recommendations that either bypass quote/liquidity gates or cannot be resolved. Check emitted fields and live modeled rows.
8. Stored prediction fields may be semantically inconsistent across builders: one `credit_per_share`/POP field is populated from different bases, and modeled rows may lose the source needed to distinguish them. Compare main and multi-strategy emitters with ledger rows.
9. Chain-quality health may be self-validating or misleading when pagination truncates the source list, because the ratio denominator is the returned list rather than an independently known expected population. Check truncation handling and quality-log records.
10. The live board opener and the older candidate-selection helpers are separate paths; tests may primarily exercise the orphaned helpers, leaving the production opener insufficiently covered. Check call graph, scheduler entry point, and test references.

These hypotheses are deliberately provisional. Each is either confirmed or rejected below with a command, output, or focused executable check.

## §2 — Where I agree with the prior agent

The prior reports correctly identified and measured several important defects:

- The qualification drought is not one population: the reported cadence-adjusted split is 88–92% fill-model correction and 8–12% remaining market effect. The series also correctly retracts the earlier “zero since 2026-08-10” wording: the last nonzero date in the supplied series is 2026-09-01 and true zero starts 2026-09-02.
- The chain-quality ratio can be self-validating under silent pagination truncation. `SKIP_TRUNCATED_CHAINS` and `_truncated_walks` are a real mitigation, not proof that historical readings are clean.
- The chain-health predicate and selection predicate disagree: health tolerates a relative spread of 0.80 while selection uses `MAX_QUOTE_SPREAD_PCT = 0.35`; call-side `_tradeable()` also uses volume >= 1 or OI >= 10 rather than the configured 25/100 floors.
- The `--mark-only` path is unscheduled. The live task action is `run_auto_paper_cycle.ps1` with no `--mark-only`; `data_quality_log.compact()` therefore has no observed production caller.
- The direction channel has no demonstrated skill at the retained horizons. The source tuple now retains only `direction_1w` and `direction_1m`, while historical 1d/overnight rows remain gradeable.
- The store divergence and dead `open_positions.json` reader were real operational defects. I agree with the prior measurement, but the present code should still be checked rather than inferred from the report’s claimed fix.

## §3 — Where I disagree, and the evidence

### 3.1 Modeled P/L can still be recorded as realized

The prior reports describe realized P/L and exit-cross handling as fixed. The current implementation contradicts that claim.

Command:

```text
python -c "... set_close() on a temp modeled row with actual_fill_credit=None ..."
```

Output:

```text
[CLOSE] ... has no actual_fill_credit; falling back to modeled credit 1.25.
set_close_return= True
realized_gross= 100.0 realized_net= 97.84 status= closed
```

`analysis/outcome_logger.py:set_close` explicitly substitutes `modeled_credit_per_share` or `0.0` when the actual fill is absent. This is a confirmed root-level defect, not a test gap. A “closed” row can therefore look like achieved performance despite never having an achieved entry price.

### 3.2 The live ledger does not show the claimed net-basis migration

Command:

```text
python -c "load logs/vega_outcomes.jsonl; count status, net_basis, exit_leg_quotes, chain_source"
```

Output:

```text
status: modeled 178, closed 75, open 4
net_basis: None 257
exit_leg_quotes: None 257
chain_source: None 257
```

The new code path has unit tests for `net_basis`, but none of the current 75 closed rows carries that field. The reports’ statement that rows “now” distinguish commissions-only from commissions-plus-exit-cross is true only for future closes, not for this ledger. Existing rows default to commissions-only in `net_basis_note()` without an explicit stored fact. This is a semantic boundary, so historical performance comparisons must not treat the field as present.

### 3.3 The entry-date claim is false as stated

Command:

```text
python -c "count outcome opened_at by status and date"
```

Output:

```text
closed: n=75, min=2026-07-09, max=2026-08-10, before_0804=18
open:   n=4, min=2026-08-04, max=2026-08-07, before_0804=0
```

The four open rows do fall in the claimed Aug 4–7 window, but 18 of 75 closed rows predate Aug 4. The assertion “all 75 closed and 4 open rows entered 08-04 → 08-10” is not supported by the ledger.

The commit timing itself is verified:

```text
d6255b9 2026-08-10 17:46:42 -0500 fix: board was quoting prices no fill could achieve
e088e3e 2026-08-10 19:14:34 -0500 feat: desk opens the board it was shown
```

The ordering coincidence may explain the four Aug 10 rows, but it cannot repair the date-derived `gate_basis()` ambiguity for all historical rows. The current ledger has only four rows on or after 2026-08-08 by the function’s derived rule; 253 derive as `mid`.

### 3.4 Counterfactual identity remains incomplete

Command:

```text
python -c "print(counterfactuals.dedup_key(put)); print(counterfactuals.dedup_key(call))"
```

Output:

```text
X-100/95-2026-09-18
X-100/95-2026-09-18
same=True
```

The current 3,009-row counterfactual ledger has 3,009 unique keys, but that only proves no collision occurred in this stored sample. It does not prove the identity function is safe: the ledger stores no `strategy` field at all, and the key omits strategy/side. A future same-strike put/call pair would collapse at first sighting.

### 3.5 Test isolation is still incomplete

Command:

```text
python -c "print(conftest._PRODUCTION_LEDGERS); print(data_quality_log.LOG_FILE)"
```

Output:

```text
fixture ... 'data_quality_log.json'
actual data quality log = .../data/data_quality_log.jsonl
```

The full suite did not alter the protected JSONL quality log on this run, but the fixture does not structurally guarantee that. It redirects the legacy `.json` name, not the live `.jsonl` name. This is exactly the stale-enumeration failure pattern the audit brief warns about.

### 3.6 The current prediction ledger shows production band claims, not production grading

Command:

```text
python -c "group prediction rows by claim_type and status"
```

Output:

```text
band_contains_*: 108 rows per type, 0 resolved, 108 open
2026-09-03: 8 band types x 54 = 432 rows
2026-09-04: 8 band types x 54 = 432 rows
```

There are 216 forecast/baseline band claims per day, but none has graded yet. Therefore the reports’ historical walk-forward coverage figures are not live calibration evidence. The channel writer is active; its grading result remains unmeasured in production.

## §4 — What neither of us checked

1. **Modeled-close fallback is a money-path violation.** The contract enforces `actual_fill_credit` for `open_paper_trade`, but `set_close` accepts arbitrary existing rows and replaces missing actual fill with modeled credit. The write-boundary contract is therefore bypassable through the close transition.
2. **Atomic replacement is not multi-process safe in every ledger.** `outcome_logger._write_all()` uses a per-process temp name and `os.replace`, but the surrounding read-modify-write has no exclusive lock. Two processes can read the same rows and replace one another’s additions. Atomic replacement protects file truncation, not lost updates.
3. **Counterfactual identity does not preserve strategy.** The current rows cannot be retroactively separated because `strategy` was never stored. Any strategy comparison from this ledger is structurally unavailable, not merely statistically underpowered.
4. **The actual quality-log test gap is still live.** The suite’s passing result is not evidence that a chain writer cannot reach production JSONL; it is evidence only that the tests exercised no such unredirected write in this run.
5. **Call-side strategy evaluation still fails open in the main path.** `main.py` catches `strategies.evaluate()` exceptions and proceeds toward qualification. A broken advisory/criteria evaluator can therefore preserve a trade rather than reject or mark it errored. This was visible in the code and was not disproved by the suite.
6. **The configured liquidity contract is not universal.** `multi_strategy._tradeable()` uses `mid > 0` and 1/10 activity thresholds, while the bull-put path uses configured 25/100 plus quote-spread rules. The reports measured this, but no runtime contract prevents the divergence from widening again.
7. **The full suite has an unresolved async warning.** `1519 passed, 2 warnings` includes two `RuntimeWarning: coroutine 'afetch_chain' was never awaited` warnings in `tests/test_robinhood_mcp.py`. Green status does not mean warning-free broker-path tests.

## §5 — Inventory

### Entry gates and thresholds

The enforced bull-put gate contract is `config.REQUIRED_GATES` (11 keys): `delta_cap`, `otm_buffer`, `credit_to_width`, `min_credit_usd`, `liquidity`, `pop`, `dte_window`, `quote_spread`, `natural_credit_positive`, `earnings_clear`, and `support_shelter`. The code-level values and enforcement surfaces are:

| Rule | Current value | Enforcement | Coverage / empirical status |
|---|---:|---|---|
| POP | 0.72 | `assessment.evaluate_gates`, board opener re-check | A priori; calibration is not live-closed |
| IV rank | 45 | `main.py` ticker screen; hard only for `HISTORY` | Not in REQUIRED_GATES; `APPROX` below-floor path is warning-only |
| Credit floor | $25, scaled to [$15,$25] by spot | `config.min_credit_usd_for`, assessment, strike validation, board path | Comment cites measurements; no current closed natural cohort validates it |
| DTE | 25–45 | candidate enumeration and gates | A priori strategy window |
| Quote spread | 0.35 of mid | bull-put selection/gate | Call path does not enforce the same value |
| OTM buffer | 3% stock; $10 SPY-like | strike selection/gates | A priori, partly documented from prior failures |
| Delta band | 0.12–0.30 search; target 0.20 | enumeration and strike selection | Search bounds, not validated as optimal |
| Min spread width | 1.0 | strike validation | A priori |
| Credit/width | 0.15; narrow exception 0.20 | gate/selection | Comment cites expected 0.20-delta economics, not cohort validation |
| Leg liquidity | volume 25 OR OI 100 | bull-put path | Measured threshold change; call path uses 1/10 |
| Shelter | enabled | assessment / selection | Comment cites five-entry observation, not independent validation |
| Earnings | enabled, fail closed for unknown non-ETF entry | strategy/fundamental path | Policy constant; no calibration |
| Edge score | 60 | final ticker qualification | Explicitly described as a starting/unvalidated ordering |
| VRP | 2 vol points | strategy/edge qualification | A priori threshold; reports show current negative VRP but no decision test |
| Entry caps | 2/run, 3/day, 4/expiration, 15 total | `_auto_open_from_board` | Policy; `ENTRY_HOLD=True` means caps have no current live-entry observations |

The highest-blast-radius constants are `ENTRY_HOLD`, `MIN_PROBABILITY_OF_PROFIT`, `MIN_IV_RANK`, `MIN_CREDIT_TO_WIDTH_PCT`, `MAX_QUOTE_SPREAD_PCT`, the liquidity floors, `MIN_DTE/MAX_DTE`, and `VRP_MIN_THRESHOLD`: each changes the candidate population or whether a trade can be opened. None should be moved based on the present drought without a new cohort contract.

### Ledgers and logs

| Path | Writers | Readers / purpose | Isolation / retention / runtime evidence |
|---|---|---|---|
| `logs/vega_outcomes.jsonl` | outcome logger, board opener, manual tools | P/L, book awareness, calibration | Isolated; append history; 257 rows observed |
| `logs/vega_predictions.jsonl` | prediction and band engines | resolution, Brier/calibration/liveness | Isolated; no explicit retention; 4,688 rows observed |
| `logs/vega_counterfactuals.jsonl` | counterfactual rebuild | gate value-of-information | Isolated; 3,009 rows observed |
| `logs/vega_shadow_book.jsonl` | shadow book | modeled recommendation grading | Listed in fixture; runtime file was not present in the basic artifact listing |
| `logs/vega_auth_events.jsonl` | auth preflight and tests historically | auth liveness | Isolated; 3 rows observed |
| `data/data_quality_log.jsonl` | fetcher/data-quality logger | chain coverage and provenance | **Fixture mismatch**; 6,576 rows observed |
| `logs/scan_latest.json` | `main.py` | cockpit and board opener | Redirected in tests; current artifact is production board |
| `logs/scan_log.json` | scan append path | historical qualification series | Large tracked artifact; current file is a JSON array, not JSONL despite line-oriented append assumptions |
| `logs/run.log` / `output/paper_desk/auto_paper_cycle.log` | scheduler and cycle | liveness and operator evidence | Runtime evidence exists; retention not established |

Gitignore status, retention, and historical reader coverage were not uniformly documented in code. The quality JSONL has a 120-day policy and 400,000-row backstop, but its compaction caller is only the unscheduled mark-only path.

### Automated entry points

The live Windows task is `VEGA_AutoPaper_2Weeks`, last observed successful at 2026-09-04 14:35 and next scheduled for 2026-09-05 08:35. Its action invokes `run_auto_paper_cycle.ps1` with no `--mark-only`. The normal cycle runs the engine, candidate scan, marking/closing, prediction resolution, and board opener. `--mark-only` exists in `auto_paper_cycle.py` but has no scheduler evidence. The GitHub Actions workflow is a second potential scan surface; whether it is enabled was not verified.

### Stored versus derived fields

Stored raw measurements include entry/exit prices, credits, strikes, expiration, quote and liquidity fields where present, prediction probability/context, and marks. Derived fields include `gate_basis`, `entry_epoch`, `entry_vendor_basis`, `cohort`, exit-cross cost from exit quotes, and several P/L aggregates. Risk remains where raw fields are absent: the outcome ledger has no chain source or exit quotes on existing rows, and counterfactual rows have no strategy identity. Stored copies that can disagree include `modeled_credit_per_share` versus natural/mid credit fields, `modeled_pop` versus `true_pop`/`implied_pop`, and the historical `realized_pl_per_contract` versus the newer net-basis fields.

## §6 — QC and stress results

### Suite and ledger isolation

Command: `python -m pytest -q` from `options_intelligence`.

Output: `1519 passed, 2 warnings in 84.13s`; both warnings were un-awaited `afetch_chain` coroutines. Protected SHA-256 hashes were identical before and after:

```text
vega_outcomes.jsonl        8CAD43C5...30E9
vega_predictions.jsonl     C24893E1...F113
vega_counterfactuals.jsonl BFB4B1B7...D4B
vega_auth_events.jsonl     FF7017A2...E669
data_quality_log.jsonl     71CCB8F2...BFEC
```

This is a clean observed run, not proof of complete isolation, because of the `.json`/`.jsonl` fixture mismatch.

### Boundary probes

The shared contracts reject `None`, NaN, infinity, and negative values for non-negative fields. That is a good boundary. The close path is weaker: missing actual fill becomes modeled credit. Counterfactual `resolve()` returns an empty result on missing/empty history; this is explicit `None` for horizon outcome, but callers must not interpret it as untouched. The call-side `_tradeable()` uses falsy defaults for missing volume/OI and can accept a positive mid without any two-sided quote.

### Idempotence and reproducibility

The focused 76-test run passed, and the full 1519-test run passed. On-disk counterfactual keys are currently unique (3,009/3,009), but the identity function is not strategy-complete. I did not run a live counterfactual rebuild or band sweep because both may rewrite production ledgers and the brief prohibits production mutations. Therefore claims of identical rebuild output were not independently established here.

### Failure simulations not run against production

No broker quote calls were issued. No live ledger was truncated, concurrently written, or rebuilt. Static inspection finds no inter-process lock around outcome-ledger read-modify-write; DST handling uses timezone-aware scan timestamps in some paths but date-derived cohort labels in others. Auth failure handling and truncated-walk tests exist, but their production behavior was not exercised against the broker in this audit.

## §7 — Red team

The most direct bad-advice path is the book-awareness path: a stale or divergent store can mark a ticker “already held” or report wrong exposure in display/verdict text. The prior report measured 25 phantom tickers versus four local positions; the current code’s ledger is now the intended source, but no live tower reconciliation was performed here.

The more damaging unclosed path is P/L provenance. A modeled row with no actual fill can be closed into realized P/L, and the existing ledger’s 75 closed rows do not carry explicit `net_basis`. A motivated reader can conclude that net performance is comparable across all closed rows when the definitions are not stored uniformly.

The strategy thesis is also not falsifiable from the current cohort: only 20 of 257 rows carry natural-credit fields, 178 are modeled, and no current band claim has graded. “No edge available” and “our forecast is broken” are not yet cleanly separated by a sufficiently populated, fill-verified outcome sample.

The cohort can be contaminated by different strategy shapes, entry basis, vendor basis, close logic, expiration clustering, and call-side liquidity predicates. `caps_v1` at zero is currently policy-driven by `ENTRY_HOLD`, not evidence that caps work or fail.

## §8 — Overclaims

These are the material statements in the prior reports that the current code/data do not fully support:

1. **“Net P/L now includes the exit cross”** — only future rows with readable exit quotes do. All 257 current outcome rows have `net_basis=None` and `exit_leg_quotes=None`; the close fallback also allows modeled entry credit.
2. **“All 75 closed and 4 open rows entered 08-04 → 08-10.”** The ledger says 18 closed rows opened before 08-04; closed dates span 2026-07-09 through 2026-08-10.
3. **“The band forecaster writes ~216 claims/day and grades them.”** It writes 216 forecast/baseline band claims per day on 09-03 and 09-04, but all 864 are still open and zero are resolved.
4. **“The current ledger distinguishes vendor/gate/net populations.”** Existing outcome rows have no `chain_source`, no explicit `gate_basis`, and no `net_basis`; those dimensions are derived or defaulted for legacy data.
5. **“1519 tests pass” as a complete health statement.** The count is verified, but two un-awaited coroutine warnings remain, and isolation is incomplete for `data_quality_log.jsonl`.
6. **“Direction 1d and overnight were retired”** is true for future generation (`HORIZONS` contains only 1w/1m), but historical rows remain and are still graded. That is not an orphan, but it is a mixed-generation population.
7. **“The counterfactual ledger is deduplicated by full spread identity.”** It is unique by the current key, but the key omits strategy/side and the stored rows do not preserve strategy, so it is not full identity across structures.
8. **“No code changes were needed for the modeled P/L boundary.”** The unit tests cover the desired close behavior, but the direct probe shows the missing-fill fallback remains in production code.

## §9 — What I could not verify

- I could not independently reproduce the 88–92/8–12 drought split from the full `scan_log.json` because the 53 MB artifact is stored as a formatted JSON array with append history and does not parse as one JSON document; I accepted the report’s displayed series only where it is consistent with the ledger and commit timestamps.
- I could not recompute the 80%/99% band coverage, drift geometry, or tail calibration from production claims because the live band claims have not matured. Those figures are backtest/report evidence, not independently verified production measurements here.
- I did not run a counterfactual rebuild or band sweep twice: doing so can rewrite production ledgers, violating the read-only and live-ledger constraints.
- I did not query the JARVIS tower or issue broker quote calls, so current remote-store reconciliation and live Robinhood schema behavior remain unverified.
- I did not verify GitHub Actions enablement, gitignore status for every artifact, or complete retention history for every log.
- I did not perform a concurrent writer stress test against the production ledger. Static code evidence is sufficient to flag the missing lock, but not to quantify collision frequency.

