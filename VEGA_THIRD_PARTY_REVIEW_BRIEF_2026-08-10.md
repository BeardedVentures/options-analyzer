# VEGA — Third-Party Review Brief
## Everything an outside reviewer needs to critique this system

**Date**: 2026-08-10 (supersedes the 2026-08-09 brief)
**Test suite**: 750 passing, ~25s
**Verification**: every number, field name and line reference below was queried from the running code and the live ledgers while this document was written. Nothing here is from memory.

---

## 0. How to use this document — read this first

**The previous brief was wrong often enough to send three reviewers down the wrong path.** On 2026-08-10 the GPT, Grok and Claude reviews it produced were adjudicated against source. Of 20 checkable claims: 10 true, 3 false, 3 stale, 4 materially mischaracterised. The improvement plan built on them had two Sprint-1 items, and both failed on contact with the code — one was a no-op, the other patched a field name (`earnings_date`) that does not exist.

So: **treat this document as a hypothesis list too.** Every claim has a file and a line. Grep before you build on it. The one thing you should trust more than the prose is the test suite.

The single most useful pattern we learned: **when reviewers disagree about a rule, each is usually describing a different real function**, because the rule was implemented more than once. Before believing any claim about behaviour, check which implementation the live entry point actually reaches.

---

## 1. What this system is

VEGA is a **curated tip sheet for defined-risk premium selling** — research and reasoned opportunities, aimed at giving a 30-minute-a-day investor the read a seasoned options professional would have.

Three things follow from that, and they are not negotiable framing:

1. **The board is the product.** The dashboard is the deliverable, not a debug view.
2. **Trades are opened and closed manually.** This is not a bot that touches a brokerage account.
3. **The paper bot exists solely to validate the board.** It opens what the operator was shown, so the learning history grades the thing being published.

Point 3 was **false until 2026-08-10** and is the largest change in this brief. See §4.

---

## 2. The track record — read this before anything else

Queried live from `logs/vega_outcomes.jsonl`:

```
records=226   real=79   closed=64   open=15
  fill_model=mid     : n=18  wins=13   (72%)
  fill_model=natural : n=46  wins=0    ( 0%)
  strategy: {'bull_put_spread': 79}
```

**Do not compute a pooled win rate from this.** The separation is total and mechanical:

- **Mid-fill trades were booked at a price that could not be achieved.** The mid overstated the collectable credit by ~75% (measured across the 2026-07-31 snapshot: $92.33 mean vs $21.85 natural). A profit target at 65% of an inflated credit is easy; a stop at a multiple of one is far away.
- **Natural-fill trades were selected under a gate reading the wrong price basis.** The gate checked the *mid* credit while the desk filled at *natural*. GDX 82/81 opened twice on 2026-08-07 for $9 and $7 of real credit against a $19 floor because the mid said $31 and $29.

**Conclusion an outside reviewer should hold onto: VEGA has never recorded a clean trade.** There is no cohort in this ledger from which a conclusion about the selection model can be drawn. Both defects are fixed forward; neither is fixable backward.

Comparability is derived, not stored — `analysis/outcome_logger.py`:

| Function | Line | Purpose |
|---|---|---|
| `cohort(record)` | ~322 | `fill_model \| gate_basis \| close_logic` |
| `gate_basis(record)` | ~305 | derived from open date vs `GATE_BASIS_FIX_DATE` |
| `analysis_eligible(record)` | 336 | may this row inform a base rate at all? |

The ledger is **never rewritten**; closed records stay exactly as written. `muninn` pools on `analysis_eligible`, so the five contaminated ravens-era wolf-stops can no longer teach it anything.

---

## 3. Architecture map

```
                    main.py  (THE ENGINE — builds the board)
                      │  screen_ticker per ticker
                      │  ├─ select_bull_put_pair       main.py:185
                      │  └─ multi_strategy.scan_extra  (bear call, iron condor)
                      │        └─ _best_wing           multi_strategy.py:117
                      ▼
              logs/scan_latest.json   ◄── THE BOARD. One list.
                      │
        ┌─────────────┴──────────────┐
        ▼                            ▼
   vega_app.py                auto_paper_cycle.py
   (what you review)          _auto_open_from_board   auto_paper_cycle.py:529
                                     │
                                     ▼
                        logs/vega_outcomes.jsonl  (the learning history)

   vega_candidates.py  ── DEMOTED 2026-08-10 ──►  output/candidates/*.json
                                                   │
                                                   ▼
                                      analysis/counterfactuals.py
                                      (measures the gates; opens nothing)
```

**The shared contract** — `analysis/assessment.py` is the single definition of "is this a good trade". Both the engine and the scanner call it.

| Function | Line |
|---|---|
| `evaluate_gates(spread, ctx)` | 435 |
| `natural_credit(short, long, width)` | 237 |
| `fill_basis(short, long, width, live)` | 296 |
| `quotes_are_live(now)` | 269 |
| `_earnings_clear(spread, ctx)` | 466 |
| `_spread_quote_ok(spread)` | 392 |

---

## 4. The two-engine problem — RESOLVED 2026-08-10, verify this

Until this date the board came from `main.py` and the paper bot opened from the `vega_candidates` snapshot. Different search, different strikes, different strategies. Measured that morning: the board showed 6 trades, the bot could trade 29 candidates across 18 names, and the overlap was 3 names — of which only one was even the same structure.

**The learning history was grading trades nobody had been shown.**

Now: `auto_paper_cycle.main()` runs `main.py`, then `_auto_open_from_board()` opens from `scan_latest.json`. `vega_candidates.py` still runs but opens nothing — it is the counterfactual recorder (§8.3).

`_auto_open_from_board` refuses three things, deliberately:

1. **Modelled credits** — the whole cycle, not just one trade (§5.2).
2. **Unmanageable structures** — `is_manageable()`, `auto_paper_cycle.py:489`.
3. **Ungated trades** — re-checks `REQUIRED_GATES` even though the board already enforced them.

**What to attack here:** the candidates-path selectors (`_pick_new_trades`, `_candidate_passes_minimum`, `_candidate_score`, `_missing_gates`, `_min_credit_floor`, `_entry_state`) are now **unreachable but still present and still tested**. That is the exact shape that inverted the earnings gate. They are marked with an `ORPHANED` block and held by `test_orphaned_selection_subtree_is_unreachable_and_labelled`. They should be deleted and their tests repointed; it was left for a separate change on purpose.

---

## 5. Pricing — the defect class that dominated this session

### 5.1 A credit spread cannot be filled at the mid

You sell the short leg at its **bid** and buy the long at its **ask**. `natural_credit()` is the one definition.

This was fixed in `vega_candidates` on 2026-08-07 and **never ported to `main.py`**, which fed the shared gate contract its mid credit under the key `natural_credit_per_share`. So every "passes all 11 gates" badge on the board — including `min_credit_usd` and `credit_to_width`, the two gates that decide whether a spread pays enough to be worth its risk — was judged against a price nobody could execute.

Fixing the put side revealed that `multi_strategy` (bear calls, iron condors) **never had a fill basis at all**. A condor crosses four bid-ask spreads.

All three generators now go through one definition. `test_no_strategy_path_still_gates_on_the_mid` asserts every generator reaches it.

### 5.2 Quotes go stale, and then the fillable price is meaningless

Measured the same day on GOOG 335/330:

| | short bid/ask | long bid/ask | natural credit |
|---|---|---|---|
| 14:47 (open) | 5.55 / 5.80 | 4.35 / 4.55 | **$100** |
| 18:03 (closed) | 4.90 / 5.80 | 4.00 / 4.60 | **$30** |

Spreads roughly tripled; the fillable credit fell 70% with no move in the underlying.

Left alone, **any scheduled scan after 16:00 ET would reject the whole watchlist and report an empty board as though the market were paying nothing.** `fill_basis()` therefore returns the natural credit when `quotes_are_live()`, and otherwise a **modelled** credit — the mid haircut by `MODELLED_FILL_RATIO` — labelled as such.

`MODELLED_FILL_RATIO = 0.78` is **measured, not assumed**: across the 158 positively-priced candidates in the 14:47 intraday scan, natural ran a median 78% of mid. It is a global constant of exactly the kind §9 warns about, and is only defensible while it stays explicitly provisional. **Re-measure it.**

The board shows a "markets closed — prices are indicative" banner whenever a modelled credit is on it, and the desk refuses to open at all.

**⚠️ Everything in §5.2 and §6 was verified against AFTER-HOURS quotes.** The intraday verification run had not happened when this brief was written. That is the single largest caveat in this document.

---

## 6. The search — changed 2026-08-10, unverified intraday

`select_bull_put_pair` used to sort short candidates by nearness to a 35-DTE / 0.20-delta target. That preference pre-empted the gates and the edge score, which are the things meant to filter.

It now sweeps every short strike in `[SHORT_STRIKE_MIN_DELTA=0.12, SHORT_STRIKE_MAX_DELTA=0.30]` across every expiration in the window, prices each pair on the fillable basis, and ranks on natural credit-to-width. Structural shelter survives as a **tie-break inside a tolerance**, never as a filter.

`multi_strategy._best_wing` does the same for calls, replacing `_pick_short`. `_pick_long` no longer returns the *nearest* strike (the narrowest spread, usually the worst credit-to-width) — it returns the one maximising the fillable ratio.

**Current board** (after hours, therefore modelled):

```
generated=2026-08-10T21:05 qualified=5 rejected=56
  strategies: {'Bear Call Spread': 5}
  fill_basis: {'modelled': 5}
```

**Questions for a reviewer:** is ranking on credit-to-width the right objective, or should it rank on edge score (more expensive: several full analyses per ticker) or expected value? Is a 0.12–0.30 delta band too wide for a "high probability" product? Is the shelter tolerance (`LEVEL_STRIKE_ROC_TOLERANCE`) doing anything measurable?

---

## 7. The gate contract — complete

`config.REQUIRED_GATES`, enforced once in `evaluate_gates` (`analysis/assessment.py:435`), which raises `AssertionError` if any key is missing:

```python
['delta_cap', 'otm_buffer', 'credit_to_width', 'min_credit_usd', 'liquidity',
 'pop', 'dte_window', 'quote_spread', 'natural_credit_positive',
 'earnings_clear', 'support_shelter']
```

Two asymmetries a reviewer should interrogate:

- **`earnings_clear` fails CLOSED** on an unknown date, and short-circuits to True for a name DECLARED to have no earnings. Restored 2026-08-10: a correct fail-closed implementation existed in `vega_candidates` with 13 passing tests and **no production caller**, while the live path passed unknown earnings open. Watch for the sentinel trap: `days_until_earnings()` returns **999** for unknown, documented as "safe — won't block trade", and collapsing unknown to it makes the fail-closed branch unreachable.
- **`support_shelter` fails OPEN.** Deliberate and documented: a level read depends on thin price history and a data gap must not empty the board.

**Richness still lives outside the contract.** `MIN_IV_RANK` is enforced in the desk, not in `REQUIRED_GATES`. Grok flagged this in the last round and it is still true. Structure is gated; "is the premium actually rich?" is not.

**Earnings data gap audit** (previously impossible to run, now instrumented via `ctx["earnings_source"]`): 56 watchlist names → **11 declared no-earnings ETFs, 45 with known dates, 0 unknown.** Failing closed costs nothing today.

---

## 8. The learning loop

### 8.1 Prediction ledger — `analysis/predictions.py`

```
claims=32  open=32  resolved=0   next resolves 2026-08-23
```

`grade()` now reports **Murphy's decomposition**, not just raw Brier:

> **BS = reliability − resolution + uncertainty**

Raw Brier cannot distinguish a model that knows something from one that memorised the base rate: a forecaster who always predicts the base rate scores a respectable Brier and has *exactly zero* resolution. `decompose()` (`analysis/predictions.py:326`) reports all three terms plus a `residual` so the identity is checkable on the published numbers.

**Discrimination is judged by a permutation test, not a bootstrap CI.** Resolution is a sum of squares, so noise scores above zero — 40 random forecasts gave resolution 0.019 with a bootstrap CI of [0.004, 0.055], an interval "excluding zero" for a model with no signal. Shuffling outcomes against forecasts builds the correct null.

**A finding worth a reviewer's attention:** the 32 open claims have a forecast spread of **sd 0.065**, nearly all between 0.70 and 0.85. Resolution is capped by how much forecasts vary, so even under perfect calibration the achievable resolution is ~0.034 against uncertainty 0.132 — a skill ceiling near 0.25. **The ledger's ability to demonstrate skill is limited less by sample size than by the engine saying nearly the same number about every trade.** Waiting will not fix that.

### 8.2 What is recorded at entry

Fields that were read-but-never-written until 2026-08-10, all now populated by `_auto_open_from_board`: `edge_score`, `vrp`, `support_level_at_entry`, `vix_at_entry`, `atm_iv_at_entry`, `rv_at_entry`, `expected_move_at_entry`, `pop_gap_at_entry`.

`edge_score` was `None` on all 79 real trades for **three independent reasons**, each sufficient alone: `true_pop` was attached after the assessment that gated on it; `build_candidates` computed the score and never copied it onto the candidate; and `assess()` read `tech["vrp"]` while the producer emitted `vrp_pp`, zeroing the largest component (30 of 100).

`set_mark` now **appends to `mark_history`** instead of overwriting `current_mark`. The ledger recorded where a trade started and ended and nothing between, which makes every "would a wider stop have done better?" question unanswerable — and the exit rule, not the entry model, has been the dominant driver of realised P&L.

### 8.3 Counterfactuals — `analysis/counterfactuals.py`

Eleven gates decide every entry and **none has ever been measured against an outcome**. The scan snapshots turn out to be that record: 2,590 candidates on disk, 562 of which failed exactly one gate — the only sample that can price a single gate.

```
spreads=639   horizon_complete=0
```

Every spread is judged over the same `HORIZON_DAYS = 10` trading days. The first real run reported **0% touched on every gate**, which reads as "no gate avoids anything" and actually meant the median spread had been observed for **two days**. It now reports `NOT YET MEASURABLE` until the window closes. First answers ~2026-08-20.

`build()` **merges** rather than rewrites: `output/candidates/` is gitignored under a comment calling it a regenerated artifact directory, and it is not — a past scan cannot be re-run. `logs/vega_counterfactuals.jsonl` is the durable record.

---

## 9. The recurring defect class

Two distinct shapes, both live in this repo.

### 9.1 Textbook constants
A global scalar correct for one regime, applied to all 56 names. Still live: `MIN_STRIKE_BUFFER_STOCK = 0.05` (5% is ~5 annualised sigma on a 12-vol name and ~0.75 on an 80-vol name); `IV_HV_INFLATOR = 1.2` (the per-ticker lever `technicals.iv_hv_inflator(ticker)` **exists and is deliberately unpulled**); `CHAIN_QUALITY_MIN_RATIO = 0.30`; and now `MODELLED_FILL_RATIO = 0.78`.

### 9.2 Field-name mismatches — the one that actually bit
A consumer reads a string-literal key its producer never writes. `.get()` succeeds and returns `None`, so nothing fails. **Six instances found in one day**: `tech["vrp"]` vs `vrp_pp` (twice), `ctx["vix"]` (never emitted), `ctx["spot"]`/`ctx["price"]` in `_entry_state` (never emitted — which silently nulled `expected_move_at_entry` on every trade), `earnings_date` vs `earnings_days`, and the 999-vs-None sentinel.

`tests/test_schema_contracts.py` now declares producer key contracts and AST-scans consumers. It found two of those six on its first run. **It does not cover value-domain mismatches** — producer and consumer agreeing on a name and disagreeing on meaning. That gap is open.

---

## 10. Known defects and open items — do not re-report these

1. **Intraday verification has not happened.** Everything in §5.2/§6 was measured after the close.
2. **Orphaned candidates-path subtree** in `auto_paper_cycle.py` — unreachable, still tested, marked.
3. **Iron condors cannot be represented.** The ledger holds `short_strike`/`long_strike`; a condor has four legs. `is_manageable()` refuses them, so the engine can recommend a structure the desk will never validate.
4. **Richness (`MIN_IV_RANK`) is outside the gate contract.**
5. **`SKEW_SCORING_ENABLED = False`** — its documented re-enable condition (chain-quality gating) is already met.
6. **yfinance is the only data source**; 30–50% of chain records are discarded per scan. No paid data by explicit decision until the free-tier system is maxed out.
7. **`blocking_news` wolf is unreachable** — `news_sentiment` is hardcoded `None` at close time. Left visibly off rather than silently broken.
8. **Two of five wolves were dead** until 2026-08-10 (earnings, news); the gap wolf was **wrong-sided for calls** and would have sat silent through the event it exists to catch on every bear call — which is currently 5 of 5 board trades.

---

## 11. Engineering principles in force

1. **One definition.** No rule may exist in two places. Most defects this session were second implementations diverging from the first.
2. **A metric must be able to come out wrong.** The Brier identity test caught three real bugs, including binning that manufactured resolution 0.16 from a forecaster with zero information.
3. **Unknown ≠ safe.** Earnings fails closed; an unknown gate basis is excluded from base rates; "too early" reports as `NOT YET MEASURABLE`, never as zero.
4. **Degrade, never break.** Advisory layers fail to empty, not to an exception.
5. **Never rewrite history.** The ledger is append-only; comparability is derived.
6. **A tested function with no caller is a liability**, not documentation.

---

## 12. What we most want reviewed

1. **Is ranking on fillable credit-to-width the right selection objective** for a "high probability, efficient use of capital" product — or should it be expected value, or edge score?
2. **Is `MODELLED_FILL_RATIO` defensible at all?** It keeps the evening board useful and it is a global haircut on a distribution we have measured exactly once.
3. **The forecast-spread ceiling (§8.1).** If the engine can only ever say 0.70–0.85, can this ledger ever demonstrate skill? What would widen it honestly?
4. **Iron condors** — represent four legs in the ledger, or drop the structure from the board so the desk can validate everything shown?
5. **Is the 3.0× wolf stop truncating the edge?** A high-POP structure's payoff lives in the right tail; cutting at 3× credit caps at roughly 35% of max loss. No data yet — `mark_history` started recording today.
6. **Attack §5.2.** If the freshness policy is wrong, every off-hours board is wrong.

---

## Appendix A — Reproducing this state

```bash
cd "AI_OS/projects/Stock Market Tools/options_intelligence"
python -m pytest -q                      # 750 passing, ~25s
PYTHONUTF8=1 python main.py              # build the board -> logs/scan_latest.json
PYTHONUTF8=1 python vega_candidates.py --no-open --no-html   # counterfactual record
python analysis/counterfactuals.py       # per-gate value of information
python vega_app.py                       # the board itself
```

`PYTHONUTF8=1` is required on Windows — the console is cp1252 and the scan prints `Δ`.

## Appendix B — Files to read first, in order

1. `analysis/assessment.py` — the contract, the fill basis, the freshness policy
2. `main.py:185` `select_bull_put_pair` — the sweep
3. `auto_paper_cycle.py:529` `_auto_open_from_board` — what the desk will and will not open
4. `analysis/counterfactuals.py` — how the gates get measured
5. `analysis/predictions.py:326` `decompose` — how the forecasts get graded
6. `VEGA_Meta_Review_and_NextGen_Plan_2026-08-10.md` — the adjudication of the last review round, the plan, and Parts 8–12 logging what shipped

## Appendix C — This session's commits

```
e088e3e  the desk opens the board it was shown, and the fast scan becomes a ruler
5a27c05  the desk could not have managed the board it is about to be given
adcf8c5  the call side sweeps and prices too, and its wing selector cannot pick blind
e8df2be  one search, and a price the board is honest about
916db7a  scope the one-list consolidation
d6255b9  the board was quoting prices no fill could achieve, on every strategy
93cad91  drop the Brief tab, and make Bitcoin a board instead of a readout
5a375a8  put the measurement work on the page that is supposed to show it
e05fc92  tell a model that knows something from one that memorised the base rate
f9ffe18  measure the gates, using the record the scans were already keeping
2bc9cd5  a key the producer never writes is a successful call returning None
68dc1ab  meta-review of three reviews, and the plan they could not execute
2f2562a  the edge score never existed, and three separate bugs kept it that way
```
