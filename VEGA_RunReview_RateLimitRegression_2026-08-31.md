# VEGA Run Review — 2026-08-31

> **The title of this file is wrong and is kept only so existing links resolve.**
> There was no rate-limit regression. The filename asserts the original conclusion; this
> document records what the evidence actually showed. See §0.
>
> **Status:** original review written from a console log; corrected 2026-08-31 against live
> code and `data/data_quality_log.json` on the JARVIS tower. Original claims struck through
> and left visible, per this project's convention — the point is that the next session
> inherits the **method**, not the conclusion.

---

## 0. What this document is for

The original review named "Robinhood MCP rate limiting" as the root cause of a 46% ticker skip
rate, under a confident title, with a numbered fix list. Three of its central claims were
wrong. It reached the system of record before anyone re-derived it, which is the failure mode
this project already knows it has — see `VEGA_INDEPENDENT_REVIEW_2026-08-13.md` and the
standing note that external reviews of VEGA run ~50% factual accuracy.

The corrected finding is that **two independent defects** were producing the symptom, neither
of them throughput-related, and both inside VEGA's own gating logic.

---

## 1. Corrections to the original review

### ~~"Roughly 26 of 56 tickers never got scored, and the cause was Robinhood MCP rate limiting."~~
**WRONG — the mechanism is structurally impossible.**

`data/fetcher.py::_parse_robinhood_options` builds records with `for q in quotes` — only from
quotes the server actually returned — and drops any contract with no two-sided market
(`if bid <= 0 and ask <= 0: continue`) **before** `measure_chain_quality` ever sees it.

A dropped quote batch therefore *lowers `raw_count`*. It cannot lower the quotable ratio. The
original review's arithmetic — "BAC came back 24/49 quotable = ~2 batches lost" — describes an
outcome the code cannot produce.

### ~~"441 chain fetches for 57 tickers in this single launch. A median of 8 per ticker."~~
**WRONG — that sums four scans, and the log is not a fetch counter.**

The manual launch (`scan_id 20260831T124110`, 12:41–12:56) **overlapped the scheduled hourly
task** (`20260831T123504`, 12:35–12:51). Adding both engine scans plus two lottery scans gives
468 rows and a median of 8. Per scan it is **3**, and `_quality_recorded` deliberately dedupes
on `(ticker, min_dte, max_dte)`, so the log undercounts fetches by design.

| scan_id | rows | tickers | median/ticker |
|---|---|---|---|
| 20260831T123504 (scheduled) | 168 | 56 | 3 |
| 20260831T124110 (manual) | 168 | 56 | 3 |

The real request budget is dominated by quote batches, not sessions: **~815
`get_option_quotes` calls per scan** (16,290 contracts ÷ 20).

### ~~"Chain quotability came back at 0–49% against the 91% measured on 2026-08-27."~~
**WRONG, and backwards. 08-31 was the best day in the log.**

| day | mean quotable (robinhood) | below 50% floor |
|---|---|---|
| 2026-08-27 | **57.8%** | 42% |
| 2026-08-28 | 62.4% | 36% |
| 2026-08-31 | **69.1%** | 25% |

There was no regression to explain. The "91%" figure traces to a **comment** in
`data/fetcher.py` (`get_call_options_chain`, "bull puts ran on a ~91% one"), not to data.
Treat it as unverified.

### ~~"17 tickers ran on yfinance today, 7 of them open positions."~~
**OVERSTATED, and wrong about which.** 8 in the launch, not 17 (17 is the union across the
whole day). More importantly, **AMT and PLD were never priced off yfinance at all** — see §4.
Five open positions did get real yfinance chains: AMZN, META, MU, NKE, TLT, XLE.

### "IV-rank history poisoning" — the original review's own self-correction
**CONFIRMED CORRECT.** Zero outlier observations on or after 2026-08-27; all cluster in
07-08→07-24 and 08-05→08-07. Legacy dual-writer contamination, not live. The review's
correction of itself stands.

### "chain_source is never stamped on records, and is absent from the cohort key"
**CONFIRMED CORRECT.** This was the review's most valuable finding. Fixed — see §3.3.

### ~~"The Polygon entitlement guard fired correctly and is the only reason this run didn't feed the ledger."~~
**WRONG, and inverted.** The guard was jammed shut, not holding. See §3.2.

---

## 2. The discriminating test (the method worth inheriting)

The competing hypotheses were *intermittent retrieval failure* vs *stable property of the
underlying*. They make opposite predictions about variance, which is cheap to measure from the
existing quality log across the six hourly scans of one day:

```
if dropped-batches : ratio is random per scan   -> HIGH within-ticker stdev
if illiquidity     : ratio is ~fixed per ticker -> LOW  within-ticker stdev
```

Result:

```
median WITHIN-ticker stdev   :  7.2%
       BETWEEN-ticker stdev  : 21.7%   <- 3x larger
MU 97.4% (sd 1.3)    PLTR 99.2% (sd 1.6)   NVDA 98.8% (sd 1.7)
LMT 30.8% (sd 11.4)  GE 46.2% (sd 21.2)    RCL 49.5% (sd 14.9)
```

Quotability is three times more a property of *which ticker* than of *when it was measured*.
That refutes the rate-limit hypothesis without needing a single new API call.

**Generalise this:** before accepting an intermittent-failure explanation for a VEGA metric,
partition the existing logs by entity and by time and compare the two variances. The data to
do it is already on disk.

---

## 3. What was actually wrong

### 3.1 The quality floor judged tickers on strikes the engine would never sell

`CHAIN_QUALITY_MIN_RATIO = 0.50` was applied to the **whole listed chain**. VEGA only sells
the 0.12–0.30 delta band. Measured live, 2026-08-31:

| ticker | whole grid | 0.12–0.30 band | was |
|---|---|---|---|
| GE | 31/54 = 57% | **11/11 = 100%** | skipped every scan |
| RCL | 23/42 = 55% | **11/11 = 100%** | skipped every scan |
| LMT | 38/90 = 42% | 14/19 = 74% | skipped every scan |
| MU | 153/153 = 100% | 58/58 = 100% | passed |
| SPY | 461/461 = 100% | 112/112 = 100% | passed |

The strikes dragging the grid down are **one-sided markets far from the money** — LMT's 425
put quoted bid 0.00 / ask 8.30, which fails the 80%-of-mid spread rule. That is the normal
condition of a strike nobody bids on, not evidence the underlying cannot be traded.

Note this is *not* the volume/OI clause, which was the first hypothesis after the rate-limit
one was refuted: removing it moved LMT only 37%→42%. The dominant term is the spread rule
applied to no-bid strikes. **Recorded because it was wrong** — the first replacement
hypothesis was also wrong, and only the per-record failure-reason breakdown settled it.

**Fixed:** the gate now reads `measure_tradeable_band_quality()`. The whole-grid number is
still measured and still logged, so the cockpit tile and historical comparisons are unchanged.
`CHAIN_QUALITY_REQUIRES_ACTIVITY` restores the old predicate in one line if needed.

### 3.2 The JARVIS ingest guard could not pass

`main.py` gated ingest on `fetcher.validate_polygon_connection("SPY")`. Polygon has been
**Tier 2 and unentitled for option quotes since 2026-08-26**; Robinhood has been Tier 1 since
08-27. So the probe correctly returned `healthy=False` forever, about a source no scan uses:

```
$ grep -c "Skipping JARVIS ingest" logs/run.log   ->  29
$ grep -c "post_to_jarvis"          logs/run.log  ->   0
```

**29 skips since 2026-08-26. Zero successes.** A guard that cannot pass is not a guard.

**Fixed:** replaced with `fetcher.chain_coverage()` — scored/attempted tickers derived from
the run itself, floor `SCAN_COVERAGE_MIN_RATIO = 0.70`, zero extra requests. The board
artifact now carries `scan_coverage` and a `degraded` flag, and `verify_numbers.py` reports
`NO DATA` instead of the vacuous `0/0 rows reconcile`.

### 3.3 Vendor provenance was unrecoverable (the original review was right)

`chain_source` was a local variable. Fixed: stamped onto every record, threaded through the
candidate builders to `open_paper_trade`, and added as a **fifth cohort dimension**
(`fill_model | gate_basis | close_logic | entry_epoch | vendor`). Pre-existing rows read
`unrecorded` rather than being back-filled — same contract as `gate_basis`. Provenance for
already-open trades is recoverable by joining `data_quality_log.json` on `(ticker, date)`.

### 3.4 An empty chain was attributed to yfinance

The Tier-3 branch set `chain_source = "yfinance"` unconditionally, even when yfinance returned
nothing. AMT and PLD logged **21 such `raw=0, usable=0` rows each** on 2026-08-31 and were
reported as "living on yfinance". They were never priced off yfinance. Now labelled `none`.
This mattered more after 3.3: a false vendor claim is now a false cohort.

---

## 4. AMT and PLD — not a vendor problem

They are **not** yfinance-dependent and should **not** be dropped from the watchlist.
Robinhood serves them fully — 189/189 and 191/191 contracts quoted, 100%.

Their expirations are **monthly-only**: 2026-09-18, 10-16, 11-20, 12-18. Against the 25–45 DTE
window on 2026-08-31:

```
2026-09-18 -> DTE 18   below the window
2026-10-16 -> DTE 46   ONE DAY above MAX_DTE = 45
```

A monthly-only underlying has **no expiration inside a 25–45 window on 31% of days**. On those
days VEGA gets no chain from any tier — which is why the yfinance rows are `raw=0`.

**Open decision for the operator, deliberately not taken here:** `MIN_DTE`/`MAX_DTE` are
described in `config.py` as part of the cohort contract, so widening `MAX_DTE` to 46–50 to
catch monthlies is a cohort-affecting change, not a bug fix. The alternative is accepting that
monthly-only names are scannable ~69% of days. No yfinance contamination results either way,
now that 3.4 is fixed.

---

## 5. The retry, and what it actually protects

The original review ranked "retry rate-limited quote batches" as fix #1 on a mechanism that
does not exist (§1). It is implemented — 3 attempts, exponential backoff — but the reasoning
in the original was wrong, and the *correct* reasoning makes it **more** load-bearing after
the band-gate change of 3.1, not less:

- Under the old whole-grid gate, a dropped batch was mostly far-OTM strikes nobody would sell.
- Under the band gate, a hole **inside** the band is a strike `select_bull_put_pair` could have
  used. It would silently pick a second-best pair with no signal the better one was absent.

**And the band ratio is structurally blind to it**: dropped contracts never become records, so
they never enter the denominator. This is a weaker defect than the yfinance tautology caught
during implementation — records that *do* exist can still fail the predicate, and LMT measured
74% not 100% — but it is the same family, and it is why the denominator question is worth
asking of every ratio in this system.

**Fixed:** `afetch_chain` now returns the *strikes* of dropped instruments, not a bare count.
`_parse_robinhood_options` classifies them against the band's strike range and logs
`BAND_DROP` (selection chose from an incomplete set) distinctly from `GRID_DROP` (harmless).
Band holes surface in `chain_coverage()["band_holes"]` on tickers that **passed** the gate —
the one place a healthy-looking number could otherwise hide an incomplete chain.

---

## 5b. Rate limiting was real — it just wasn't the mechanism

The original review's *conclusion* was wrong (§1); its *premise* was not. `run.log` carries
**683 `RATE_LIMITED` responses**, split across two paths:

| path | count | effect |
|---|---|---|
| `get_option_quotes` | 598 | loses 20 contracts from a known population |
| `get_option_instruments` | **85** | **ends the page walk** |

The instrument path had **no retry at all** until 2026-08-31 — only the quote loop did. Its
failure is strictly worse, for three reasons:

1. **It is a `break` inside a page walk, not a skip of one unit.** Rate-limit on page 3 of 12
   and pages 1–2 are kept and treated as the chain. Pagination is **ascending by strike**, so
   what survives is systematically the low strikes — for puts, the deep-OTM tail *below* the
   0.12–0.30 band. The result is a plausible partial chain missing exactly the sellable region.
2. **`band_holes` cannot see it.** That instrument is keyed off the instrument list, which is
   the correct denominator for a dropped *quote* batch — the one population a quote drop cannot
   shrink. An instrument-page drop shrinks that population itself. The mechanism built to stop
   a healthy ratio hiding an incomplete chain is blind to the one failure that shrinks its own
   denominator.
3. **It was silent.** The `no <type> instruments` warning fires only when a walk returns
   *nothing*. Of 122 such lines, 72 are PLD/AMT structural (§4) and ~50 are rate-limit-emptied
   — leaving roughly **35 walks that truncated and reported nothing at all**.

**Fixed:** the instrument page fetch retries (4 attempts, shared backoff). A walk that still
fails, or that exhausts `_MAX_INSTRUMENT_PAGES` with a cursor outstanding, is marked
`truncated` and the chain is **refused** rather than scored — `SKIP_TRUNCATED_CHAIN`, distinct
from `SKIP_DATA_QUALITY`, because a truncated chain is an *unknown* chain, not a thin one.
Surfaced in `chain_coverage()["truncated_walks"]`. Config: `SKIP_TRUNCATED_CHAINS`.

**A defect introduced by the first version of that fix, and caught in review:** it retried
*any* non-JSON body, including `Request entity too large` — a 413 caused by request shape,
which no amount of waiting improves. It spent 4 attempts and 15s of backoff to reach the same
answer. Retries are now gated on `_is_retryable_error()`, which matches known-transient markers
only and treats **unrecognised errors as permanent**, so a new server-side failure mode costs
one request rather than four across all 56 tickers.

---

## 6. Still open

- **Term structure is 52% of the request budget** — 8,491 of 16,290 contracts per scan, for an
  advisory signal that never hard-blocks, whose sibling `SKEW_SCORING_ENABLED` is already off.
  One config flip. Still an operator decision, but **two independent arguments now point the
  same way**: it is not only a cost question, it is the bulk of the load producing the 683
  `RATE_LIMITED` responses in §5b — and the instrument-path share of those is the failure the
  retry can only mitigate, not eliminate. Turning it off shrinks the population at risk.
  *Recommendation: flip it.* The signal is advisory and its sibling is already off for a
  weaker version of this same argument; the read it produces under rate limiting is a read of
  the survivors, which is the exact reasoning in `SKEW_SCORING_ENABLED`'s own config comment.
- **`MAX_DTE` vs monthly-only expirations** (§4) — **resolved: leave at 45.** The census settles
  it. PLD and AMT log 36 empty walks each against a 1–6 tail that is all rate-limit noise, so
  the structural monthly-only population is 2 names of 56, ≈0.6 ticker-days per scan. That does
  not justify forking a cohort-defining constant. The one counter-argument is timing — with the
  clean cohort at roughly one trade, a fork is cheaper now than it will ever be again — but two
  names is not the reason to spend it. Noted in the `config.py` watchlist entries so it is not
  re-derived in six months.
- Book concentration: 25 open positions ≈ 5–6 independent bets. Unaffected by any of this.
- Carried from 08-28, untouched: commit/push the Tier-1 client and `durable_write.py`; rotate
  leaked credentials; cockpit pooled headline number.

---

## 7. Baseline for measuring the change

Two large behavioural changes land in the same scan (3.1 and 3.2). Pre-change state is
captured in **`logs/coverage_baseline_2026-08-31.json`** so the delta is measurable rather
than inferred:

- mean coverage across the six hourly scans of 2026-08-31: **62.8%**
- persistently skipped (5+ of 6 scans): ABBV, AMGN, BA, BAC, BLK, CLF, COP, CVX, GE, JNJ, JPM,
  LMT, MAR, NEE, PFE, PSX, RCL, SCCO, USB, XBI, XLV
- ingest: 29 skips, 0 successes

The metric is defined identically either side of the change — `coverage = (56 − below_floor)
/ 56` — so the delta is real and not an artifact of a redefinition.

**Expected ceiling is ~96–98%, not 100%.** Two known deductions, both correct behaviour:

- **AMT and PLD** on a monthly-gap day (§4) — ~31% of days, ≈0.6 ticker-days per scan. They are
  *not* in the persistently-skipped list, so they do not cap the pre-change baseline, but they
  will still fail on a gap day.
- **`SKIP_TRUNCATED_CHAIN`** (§5b) — now a deliberate skip where the old code silently scored a
  partial chain. Rarer with the instrument retry in place, but non-zero under load.

**Falsifier, stated so it can actually fail:**

- **97% tomorrow confirms the diagnosis.** Do not read it as a miss.
- **Below ~90%, or any of GE / RCL / LMT / BAC / JNJ / PFE still skipped**, means the band-gate
  diagnosis is not the whole story and §3.1 needs revisiting.
- **`truncated_walks` non-empty and large** means the instrument retry is under-provisioned and
  §5b needs a larger `_INSTRUMENT_RETRIES` or the term-structure flip (§6).
- Check `SKIP_TRUNCATED_CHAIN` vs `SKIP_DATA_QUALITY` counts separately. Pooling them would
  reproduce the original review's exact error: attributing a retrieval failure to chain quality.

---

## 8. Method note

Of the original review's two highest-ranked findings, one was wrong and one was understated.
Of the corrections, the *first* replacement hypothesis (the volume/OI clause) was **also**
wrong and is recorded as wrong in §3.1. One real bug was introduced during the fix — the
yfinance gate measuring an already-filtered chain, arithmetically unable to fire — caught in
review, fixed, and pinned by a test that was then verified to **fail** when the bug is
reintroduced, so it cannot be a tautology.

A second review pass then found that the retry fixed only `get_option_quotes` and left the
instrument page walk unguarded (§5b) — the strictly worse of the two, and the one the new
`band_holes` instrument is structurally blind to. And the first version of *that* fix retried a
non-retryable 413. Every layer of this has needed a second look; none has been finished by the
session that wrote it.

Verification state of this document: every numeric claim was derived from live code or from
`logs/run.log` / `data/data_quality_log.json` on the tower. Tests: **1296 passing**, from a
1267 baseline. Not yet verified by a full live scan — coverage 62.8% → ~96-98% remains a
prediction until §7's falsifier runs.
