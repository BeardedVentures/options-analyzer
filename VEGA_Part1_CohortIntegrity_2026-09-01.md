# VEGA — Part 1, cohort-integrity findings
**Date:** 2026-09-01 (post-close; all seven of today's scans were already on disk)
**Method:** validate → improve → revalidate → execute, per the session handoff.
**Headline:** three of the five items rested on premises that are false. Two produced real fixes.

---

## 0. The premise correction that reframes everything below

The handoff opens with "cohort status: ravens_v1, ~12/30 clean trades" and item 1.1 asks for a
cross-reference against "25 currently open positions". Both numbers are wrong, and the real ones
change what Part 1 is for.

Closed-trade counts by `outcome_logger.cohort()`, computed from the live ledger:

| cohort key | closed |
|---|---|
| `natural / mid / credit_stop_1.5x_natural / pre_caps / unrecorded` | 44 |
| `mid / mid / credit_stop_1.5x_natural / pre_caps / unrecorded` | 18 |
| `natural / mid / ravens_v1 / pre_caps / unrecorded` | 8 |
| **`natural / natural / ravens_v1 / pre_caps / unrecorded`** | **4** |

The target cohort holds **4** closed trades, not 12. And all four are `pre_caps`: they were opened
2026-08-10, before `ENTRY_RULES_EPOCH = 2026-08-20`. The next trade VEGA opens will key as
`caps_v1`, which is a **different cohort starting from zero**. The forward-looking count is
therefore **0 of 30**, exactly as `config.py`'s own entry-diversification block predicted when it
said the caps were landing "at the cheapest moment in this cohort's life".

The open book is **5 positions**, not 25:

| ticker | filled | expiration |
|---|---|---|
| NKE | 2026-08-04 | 2026-09-18 |
| META | 2026-08-06 | 2026-09-18 |
| AMGN | 2026-08-06 | 2026-09-18 |
| SMH | 2026-08-07 | 2026-09-18 |
| NEE | 2026-08-07 | 2026-09-18 |

**No paper trade of any status — open or closed — has been entered since 2026-08-10.** That single
fact resolves items 1.1 and 1.4 outright, and it means the thing standing between VEGA and its
validation gate is not data integrity. It is that the pipe has been shut for three weeks.

---

## 1.1 — Truncated-chain vs. open-book cross-reference

### Validated

The count is exactly right. `run.log` holds 683 `RATE_LIMITED` events, which split cleanly:

```
  85  get_option_instruments   <- the walk-truncating path
 598  get_option_quotes        <- the batch-dropping path
```

The 85 instrument-path events span **2026-08-27 13:38:36 → 2026-08-31 12:56:05**: 1 on 08-27,
53 on 08-28, 31 on 08-31. Every one predates the retry/truncation fix, whose files carry mtimes
of 2026-08-31 14:33–14:38. Nothing about the ~85 figure needed correcting.

### Result: ZERO affected positions

The join is empty, and not marginally so. The latest ledger entry of any kind is 2026-08-10;
the earliest truncation event is 2026-08-27. The two sets are **seventeen days apart** and cannot
overlap. Three of the five open positions (NKE, META, NEE) do appear among the tickers that later
fell back to yfinance, but those readings are all dated 08-27 or after — weeks after the positions
were opened.

**No `band_completeness: unknown` flag has been applied to anything, because nothing qualifies for
one.** Writing a flag here would have recorded a concern the evidence does not support.

### What the cross-reference did surface

The exercise was worth the time for a reason the handoff did not anticipate. Grepping for
truncation found 16 `TRUNCATED_WALK` events — 14 of them **today**, after the fix, on every single
scan:

```
2026-09-01 08:37:11  TRUNCATED_WALK SPY  puts DTE 5-120 (page_budget_exhausted): kept 1205
2026-09-01 08:38:03  TRUNCATED_WALK QQQ  puts DTE 5-120 (page_budget_exhausted): kept 1130
   ... and the same pair on all seven scans of the day
```

This is a live truncation with a **different cause**: not a rate limit, but
`_MAX_INSTRUMENT_PAGES = 12` being exhausted with a cursor still outstanding. SPY and QQQ — the two
most liquid names on the watchlist — have had their chains flagged incomplete on every scan since
the fix landed. See §1.3, because the cause is the term-structure fetch.

---

## 1.2 — Falsifier check

### Verdict: PASS on the stated threshold, with one named miss and one attribution caveat

**Coverage.** Against a 2026-08-31 baseline of 53.6%–67.9%:

| scan | reported coverage | put-side skips | call-side skips | truncated |
|---|---|---|---|---|
| 08:52 | 96% (54/56) | 7 | 10 | 2 |
| 09:51 | 100% (56/56) | 1 | 2 | 2 |
| 10:52 | 100% | 1 | 1 | 2 |
| 11:50 | 100% | 2 | 2 | 2 |
| 12:52 | 100% | 1 | 2 | 2 |
| 13:51 | 100% | 0 | 1 | 2 |
| 14:52 | 100% | 0 | 2 | 2 |

The first scan lands at 96% — one point under the 97% "confirms" bar and far above the 90%
failure line. Every scan after it reads 100%. On the threshold as written, this passes.

> **CORRECTED 2026-09-02.** These reported figures were inflated. `chain_coverage()` does
> `skipped -= passed`, and the DTE 5-120 term-structure fetch was a *third* window that passed
> for tickers which had failed **both** trading chains — silently crediting them as covered. True
> coverage (failed every window fetched) was **89%** on the 08:52 scan, not 96%: ABBV, CLF, NEE,
> PFE, PLD and USB were rescued by the wide window. Later scans were 98%, not 100%. Disabling
> term structure on 2026-09-02 removed the third window, and the first scan after it read 88%
> reported = 88% true. So the honest before/after is **89% → 88%, flat**, with a metric that can
> no longer be inflated by adding a window. The delta-band gate's win over the 53.6–67.9%
> whole-grid baseline stands; only these headline percentages move.

**Canary tickers {GE, RCL, LMT, BAC, JNJ, PFE}** — reported as a miss, not rationalised:

- **GE — cleared.** Never skipped, on either side, on any scan. This was the headline case for
  the delta-band gate change and it is fixed.
- **BAC — cleared.** Never skipped.
- **PFE — cleared after the first scan.** Skipped at 08:43 only.
- **JNJ — cleared after the second.** Skipped at 08:42 and 09:41.
- **RCL — cleared after the first.** One call-side skip at 08:46.
- **LMT — DID NOT CLEAR.** Skipped on **5 of 7 scans** (10:52, 11:50, 12:52, 13:51, 14:52), the
  call side every time and the put side three times, at band ratios of 32–47% against a 50% floor.

Per the handoff's own rule — "any of {…} still skipping means the root-cause diagnosis needs
revisiting" — **LMT is a miss.** It is one ticker rather than a systemic failure, and its ratios
sit just under the floor rather than at the 0–20% of a broken fetch, which reads more like genuine
thin quoting than like retrieval failure. But it did not clear, and this section is not the place
to explain that away.

**Skip counts, NOT pooled** (pooling them would reproduce the original review's error):

```
SKIP_TRUNCATED_CHAIN : 14   <- all SPY/QQQ, all page_budget_exhausted, 2 per scan
SKIP_DATA_QUALITY    : 34   <- genuine thin-quote skips
```

### The attribution caveat — read this before crediting the retry fix

**There were zero `RATE_LIMITED` events today.** Not a reduced number: zero, across seven full
scans and ~135,000 contract fetches. The retry logic therefore **never fired even once**, and
cannot be responsible for any part of the coverage improvement.

The coverage gain belongs to the *other* change that shipped in the same batch — the chain-quality
gate moving from the whole strike grid to the tradeable delta band. The baseline file says both
landed together; the log says only one of them ran.

So the honest verdict is split:

- **The delta-band gate: validated live.** 60% → 96–100%, and GE cleared.
- **The retry/backoff logic: unexercised in production.** Unit-tested, live-untested. Today
  proved nothing about it either way, and this is precisely why item 1.5 stopped being optional.
- **The truncation *detection*: validated, and it immediately earned its keep** by exposing the
  SPY/QQQ page-budget truncation that no prior scan had ever reported.

### One measurement caveat worth knowing

`chain_coverage()` keys `_chain_gate_passed` / `_chain_gate_skipped` on the bare ticker, both the
put and call paths write to the same sets, and the function then does `skipped -= passed`. A
ticker whose **call** chain was skipped while its **put** chain passed therefore counts as covered.
LMT sits inside today's "100%" for exactly this reason. The design is deliberate — a ticker seen in
any window has been seen — and truncated tickers are named separately in `truncated_walks` so they
are not lost. But the headline percentage is a put-side number in practice, and bear calls and
condors are built off the side it does not report. **Recommendation only; not changed here**,
because `healthy` gates JARVIS ingest and that guard has been jammed shut once already.

---

## 1.3 — Term structure: the load math, for an operator decision

**Not flipped. This is Josh's call, and the recommendation now cuts harder than when it was
written.**

### Re-derived, not inherited

`config.py` states 52% (8,491 of 16,290 contracts, measured 2026-08-31). Independently re-derived
from `data/data_quality_log.json` over today's seven full scans:

```
total contracts fetched      : 135,398   (19,342 per scan)
surface fetch, DTE 5-120     :  81,318   (11,616 per scan)
                             =    60.1%
```

Higher than the documented 52%, and for a benign reason: coverage improved, so more tickers now
reach the surface fetch at all. Method: three quality readings per ticker per scan (puts 25-45,
surface 5-120, calls 25-45); the wide-window read was correctly the largest in 380 of 392
ticker-groups. Call it **~60%, ±3**.

### The argument that changed

The handoff's rationale — "shrink the population at risk of truncation" — is **weaker** than when
written, because today produced zero rate limits. There is no active bleeding to stanch.

But a **stronger and more concrete** argument appeared in its place:

> The DTE 5-120 surface fetch is what exhausts the 12-page instrument budget on SPY and QQQ. It
> truncates on both names on every scan.
>
> ~~And because `_truncated_walks` is keyed by **ticker** rather than by (ticker, window), that
> truncation then blocks the ticker's *subsequent* call-chain fetch as well — SPY's and QQQ's call
> chains return empty for the rest of the scan.~~ **WRONG, corrected 2026-09-02.**
> `_truncated_walks` is read at exactly one site, in `get_options_chain` (the put path);
> `get_call_options_chain` never consults it. SPY and QQQ were scored and rejected normally on
> 09-01 — SPY on a news block, QQQ on IV Rank 38.5 — with both trading chains intact. The real
> cost was narrower: the surface signal was unavailable for the two names it is most expensive to
> compute. Term structure was disabled anyway on 2026-09-02, on the cost argument alone.

So the term-structure read is not merely expensive. It is, right now, the reason VEGA cannot see
the call side of the two most liquid underlyings on its watchlist, in exchange for an advisory
signal that never hard-blocks and has never been graded.

### The three options

1. **`TERM_STRUCTURE_ENABLED = False`.** Cuts ~60% of request volume in one flip and very likely
   restores SPY/QQQ. Costs an ungraded advisory input. Its sibling `SKEW_SCORING_ENABLED` is
   already off for the same class of reason.
2. **Narrow the window** (e.g. 5-120 → 15-75). Keeps the signal, cuts most of the cost. Untested;
   changes what the signal measures.
3. **Raise `_MAX_INSTRUMENT_PAGES`.** Fixes SPY/QQQ *and increases* request volume — the opposite
   trade, defensible only because there is currently no rate-limit pressure to spend.

**My recommendation: option 1**, and it does not touch the cohort contract — term structure feeds
`edge_score` as a bonus component, never a gate. But it is a signal-removal decision, so it is
yours to make, not mine to make quietly.

---

## 1.4 — yfinance silent-fallback labeling

### Validated: the premise is false, in a good way

Vendor mix from `data/data_quality_log.json`:

```
today (2026-09-01)   : robinhood 1,772   coinbase 38   yfinance 0
since 2026-08-27     : robinhood 4,819   coinbase 85   yfinance 90   none 6
```

**Zero yfinance fallbacks today**, across all 56 tickers and all seven scans. The 17 tickers named
in the prior review (ABBV, AMT, AMZN, BLK, CLF, JPM, KO, META, MU, NEE, NKE, PLD, PSX, TLT, TSLA,
UNH, XLE) are confirmed as the union across 08-27→08-31, and that union has now stopped growing.

The "7 of them open positions" claim does not survive either. The open book is the 5 names in §0.
Three of them (NKE, META, NEE) are in the yfinance list, but their entries are 08-04→08-07 and
every yfinance reading is 08-27 or later. **No open position was priced off yfinance.**

### The forward fix — and the real gap it was hiding

Most of the plumbing already existed in the working tree: `fetcher._stamp_chain_source` stamps
records, `main.screen_ticker` carries it onto bull-put candidates, `auto_paper_cycle` passes it to
`log_trade`, and `outcome_logger.vendor_basis` reads it as the fifth cohort dimension.

**`build_iron_condor` did not stamp it.** A condor reaching the ledger keyed as
`vendor_basis='unrecorded'` while a bull put built from the *identical Robinhood chain in the same
scan* keyed as `'robinhood'` — two cohort keys from one fetch. The split was invisible because
`'unrecorded'` is also the correct label on every row written before 2026-08-31, so it looks like
ordinary history rather than a live defect.

**Fixed**, via a shared `multi_strategy._vendor_of()` helper. A condor draws from two independent
chain fetches (`get_call_options_chain` and `get_options_chain` each choose their own source and
each can fall back to yfinance alone), so its wings genuinely can come from different vendors.
Returning either wing's label would file a half-yfinance trade under `'robinhood'` and walk it past
the exact dimension `vendor_basis` exists to enforce. Disagreement therefore gets its own label —
`mixed:robinhood+yfinance` — which can never be pooled with either. A missing source stays `None`
and renders `'unrecorded'`; guessing would be inventing a measurement.

`build_bear_call` was already correct and is now routed through the same helper so the two
call-side paths cannot drift apart on label format.

---

## 1.5 — Unrecognized-error-body counter

### Promoted from "if time allows" to "do it", by today's own result

`_is_retryable_error()` defaults an unrecognised body to **permanent**, which is right — it fails
fast instead of burning four attempts and 15s of backoff on a request that can never succeed. The
cost of that default is a silent mode: if Robinhood rewords its rate-limit response, every marker
stops matching, retries quietly collapse to a single attempt, and the only symptom is a coverage
regression with nothing in the log naming the cause.

That was hypothetical when the handoff was written. Today made it concrete: **seven scans, zero
rate limits, so the matcher never ran.** It is load-bearing code that production has never once
exercised — and a matcher that is never exercised is a matcher nobody can tell has broken. Same
shape as the Polygon probe that sat at `healthy=False` for 29 consecutive runs.

### Implemented

- `robinhood_mcp._unrecognized_errors` — `{signature: count}`, counted at **both** non-retryable
  branches (instruments and quotes). Signatures rather than raw bodies, so 56 tickers hitting one
  server-side change read as one line with a count instead of 56 near-identical lines.
- Reset per scan from `fetcher.clear_cache()`; surfaced in `chain_coverage()` as
  `unrecognized_errors`, alongside `truncated_walks` as specified.
- `main.run_scan` logs it as `UNRECOGNIZED_ERROR_BODY` with the remedy in the message. Counted but
  unreported is the exact failure mode this item exists to close, and `scan_latest.json` is not
  read by a human between scans.

`run_scan` now also logs `truncated_walks` explicitly — SPY and QQQ had been truncating on every
scan today while only ever appearing inside a JSON blob.

---

## Revalidation

Full suite: **1310 passed** (1296 before, 14 added).

The added tests were then checked against the requirement that a metric must be able to read
non-zero. With today's four source edits reverted, **11 of the 14 fail**, including
`test_every_call_side_generator_stamps_the_vendor[build_iron_condor]` — the specific gap. The three
that pass without the patch are the three that should: the bear-call and bull-put stamps, which
already existed, and `_is_retryable_error`, which is unchanged.

---

## Scoreboard

| item | premise | outcome |
|---|---|---|
| 1.1 | 85 events correct; 25 open positions wrong (it is 5) | **Zero affected.** No flag applied. Found live SPY/QQQ truncation instead |
| 1.2 | — | **Pass** at 89→100% *true* (96→100% as reported was inflated — see the correction) vs 53.6–67.9% baseline. **LMT is a named miss.** Retry logic unexercised — credit belongs to the gate change |
| 1.3 | 52% of volume | **~60.1%**, re-derived. Recommend disabling; new argument is SPY/QQQ, not rate limits. **Not flipped — your call** |
| 1.4 | 17 tickers, 7 open — wrong | **Zero yfinance today, zero ever touched an open position.** Real gap found and fixed: iron condor never stamped `chain_source` |
| 1.5 | optional | **Done**, and promoted — the matcher is unexercised in production |

## What Part 1 could not do

Nothing here moves the cohort forward, and the reason is not data quality. **VEGA has not opened a
trade since 2026-08-10**, and the forward-looking clean-cohort count is 0 of 30, not 12 of 30. Data
integrity was the right thing to secure first — a trade opened on a truncated chain would poison
the count it joins. That is now secured. The next question is the drought, and Part 1 does not
touch it.
