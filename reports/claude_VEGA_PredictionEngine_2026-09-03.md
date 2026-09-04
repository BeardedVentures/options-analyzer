# VEGA — Prediction & Grading Engine, session report

**Date:** 2026-09-03
**Branch:** `session/2026-09-03-prediction-engine` (commit `6709f2a`)
**Tests:** 1439 passing
**Scope:** §1–§3 diagnostics run; §4–§6 built; §7 and §8 investigated and found already closed.

---

## §A — Refutations

Four of the build doc's load-bearing claims did not survive measurement. These are the headline.

### A1. The IV-rank discontinuity is real in the raw store and has **no material effect on the live gate**

§1 called this "the highest information-per-minute test available." It was worth running, and it dies.

The discontinuity exists — dramatically. Every ticker's pre-08-09 IV history carries readings that are physically impossible: AMD 507%, AAPL 255%, PLTR 268%, PSX 232%, IWM 188%. Post-08-09 maxima are all sane. So the level shift is confirmed as a **data fact**.

But the consequence claimed does not follow, for three reasons:

1. **The codebase already knows, and already fixed it.** `technicals._plausible_iv_samples` filters implausible stored observations at *read* time, judging each against the realised vol of its own date. Today's cycle logged the exact drop counts per ticker (SPY 8, IWM 9, AAPL 8, XOM 8, MAR 7, PEP 7, JPM 6…). The contaminated readings do not vote.

2. **"Two years of old-method readings" is wrong by a factor of ~12.** `IV_HISTORY_MAX_SAMPLES = 504` is a cap that has never been approached. History begins **2026-07-07**. Tickers hold 38–42 samples, split roughly **20 pre / 19 post** the boundary — not a long old-method history with a short new tail, but a near-even split.

3. **Quarantining pre-08-09 samples would move almost nothing, and would move more of it the wrong way.** Reconstructing each rejection exactly (see method below) and re-ranking against a post-08-09-only base:

| ticker | live rank | post-08-09-only | | ticker | live rank | post-08-09-only |
|---|---|---|---|---|---|---|
| PSX | 44.1 | 44.4 | | MSFT | 19.5 | 15.8 |
| AMGN | 42.1 | **61.1 → PASS** | | MU | 17.9 | 10.5 |
| GOOG | 33.3 | 31.6 | | CVX | 17.9 | 15.8 |
| CLF | 30.8 | **47.4 → PASS** | | FCX | 16.2 | 5.3 |
| QQQ | 29.3 | 36.8 | | QCOM | 13.5 | 5.3 |
| SPY | 26.5 | 25.0 | | ARKK | 13.2 | 26.3 |
| CRM | 25.6 | 10.5 | | AMD | 7.9 | 0.0 |
| RCL | 22.9 | 44.4 | | SCCO | 5.3 | 5.3 |
| ADBE | 21.1 | 0.0 | | NVDA | 20.5 | 21.1 |

**2 of 18 flip to PASS. Nine move DOWN.** The proposed remedy is close to a coin flip in direction and would have been reported as a fix.

*Method, and why it is trustworthy:* the stored sample for today is written by the **first** cycle of the day, while the rejection came from the 15:35 close scan, so `current_iv` differs from the last history entry — a naive reconstruction disagrees with the live numbers and I discarded it. Instead I inverted `current_iv` from the reported rank. Validation: for all **18 of 18** tickers the live rank is exactly `k / (n_samples − n_dropped) × 100` for integer `k`, using the drop counts logged by the live cycle. With ~34–41 samples the achievable ranks are ~2.5 apart, so 18/18 landing on grid points confirms both the sample-count and drop-top-N models.

*Corroboration:* VIX is **14.32** today, against a post-08-09 mean of 15.13 and a pre-08-09 mean of 16.87 — and 14.32 sits within 0.07 of the 46-day low. An index IV rank in the single digits is the *correct* reading. SPY's live 26.5 is, if anything, generous. **The universe is at IV rank 15–32 because the tape is quiet.** The low-vol regime reading stands.

**No gate was changed. Recommended action: none.**

### A2. The CRWD and QQQ trades are absent — but the writer is not broken

§2 predicted "the writer is the fault" or "the grading loop is the fault." Neither.

- **CRWD:** 6 rows, every one `status: modeled`, none filled or closed — and all are **Iron Condor or Bear Call**. The bull put spread that was actually traded and closed near 80% of max profit has no row of any kind.
- **QQQ:** 8 rows, all `closed`, all auto-paper from 2026-07-14 → 08-05 with `exit_reason` `auto-stop-loss` / `auto-target-profit`. None is the manual trade closed at the 50% target.

A manual-entry path **exists and works** — `outcome_logger.open_position(source="manual")`, reachable from the dashboard's `/open_manual` and `paper_desk.py manual`, plus `log_outcome.py fill` / `close` for adding ground truth to an existing modeled row. The ledger's `source` field reads `{auto-paper: 72, candidate: 7, None: 178}` — **not one row has `source: "manual"`.** The path has never been used.

This is an operator-side gap, not a code fault, and no amount of tracing the writer would have found it. **It is also the single highest-value unblocked action available:** two real, manually-executed, closed trades are the only fill-verified ground truth this project has, and they are currently unrecorded.

### A3. `caps_v1` at 0 of 30 is announced policy, not silent failure

The liveness rule already states it, on every cycle, in plain language:

> `caps_cohort: starved, executed: 0` — *"ENTRY_HOLD is ON, so no trade can open. This zero is a policy, not a fault. It becomes CRITICAL the moment the hold is lifted and the count stays at zero while the board is qualifying."*

And today's cycle log states it again: `ENTRY HELD — no new positions will be opened.` The grading loop is alive: today's 14:35 cycle ran `marked=3, closed=0`, resolved 2,899 counterfactual spreads, graded 97/178 shadow rows, and recorded 432 direction claims. **Last confirmed grading execution: 2026-09-03 14:51:47, exit=0.**

### A4. §8 item 6 — "fix the shadow-book writer, stop grading until this lands" — **it landed three weeks ago**

The doc's own §7 numbers are exactly right (`mid_credit_per_share` 0/178, `natural_credit_per_share` 20/178, `credit_basis` null on all 178). The inference from them is wrong.

`main.py:385` merges the natural-credit block into `metrics` (`metrics = {**metrics, **nat}`), and `git log -S` dates that line to **2026-08-10** (`d6255b9`). The `multi_strategy` call side followed on **2026-08-18**. Checking every modeled row by write date:

- **bull_put_spread: 0 of 77 carry natural credit — and the last one was written 2026-08-10**, the day the fix landed and the day entries stopped.
- Rows written after 2026-08-10 that still lack it: **11**, dated 08-11 → **08-17** — all before the multi_strategy fix.
- **Rows written on or after 2026-08-18 with no natural credit: zero.**

The 158 unpriceable rows are a **closed historical set**. Nothing is producing new ones. "Stop grading until this lands" would have paused a channel to wait for a fix already in production.

---

## §B — §1 result: the IV history

Full per-ticker table, 58 files, 2,063 samples, boundary 2026-08-09 (medians, vol as decimal):

| ticker | n | pre | post | med pre | med post | **max pre** | max post |
|---|---|---|---|---|---|---|---|
| SPY | 42 | 22 | 20 | 0.165 | 0.145 | 0.678 | 0.551 |
| AMD | 41 | 22 | 19 | 0.800 | 0.537 | **5.074** | 0.596 |
| MSFT | 41 | 22 | 19 | 0.380 | 0.269 | 1.013 | 0.304 |
| PLTR | 41 | 22 | 19 | 0.659 | 0.476 | **2.682** | 0.558 |
| QQQ | 41 | 22 | 19 | 0.234 | 0.210 | 0.736 | 0.369 |
| AAPL | 40 | 21 | 19 | 0.319 | 0.246 | **2.547** | 0.265 |
| IWM | 40 | 21 | 19 | 0.575 | 0.197 | **1.875** | 0.211 |
| NVDA | 40 | 21 | 19 | 0.428 | 0.384 | 1.474 | 0.430 |
| XOM | 39 | 20 | 19 | 0.507 | 0.297 | 1.649 | 0.373 |
| PSX | 38 | 20 | 18 | 0.408 | 0.394 | **2.322** | 0.460 |
| UNH | 39 | 20 | 19 | 0.321 | 0.295 | 1.882 | 0.333 |

(Pattern holds across all 50 tickers with ≥30 samples; full data in `data/iv_history/`.)

**Reading:** the pre-08-09 maxima are not a smile-weighted whole-chain median running "7+ vol points high" as the doc describes. A 507% reading on AMD against ~83% realised is a **bad single quote**, which is what `technicals.estimate_atm_iv`'s own docstring says the pre-08-09 path was — the IV of the single contract nearest spot, taken from live quotes. The doc's "whole-chain median" story and the code's "single-contract quote" story are different mechanisms, and the code's is the one the data supports.

**Boundary decision required of the operator: none.** The read-time filter already implements the quarantine, non-destructively, with the files intact as audit trail.

**One residual worth knowing:** the filter is one-sided (it drops only highs) and falls back to the HV approximation when fewer than 30 clean samples remain — and under `APPROX`, `main.py:672` makes `MIN_IV_RANK` a *warning rather than a hard block*. On 2026-09-01, IWM, MAR and XOM sat at exactly 29/30 clean samples and the IV-rank gate was silently not enforced on them. Histories have since grown past the boundary and no such warning appears in today's log — but the failure mode is one sample wide and will recur if the filter's drop count ever rises.

---

## §C — §2 result: stores, and which one is which

### The divergence is real, and it is not what suppresses anything

`main.py:1340` `_get_open_position_tickers` reads, in order:

1. **JARVIS tower** `http://192.168.0.222:8000/vega/outcomes?status=open&limit=200` — the store holding the 399-row phantom book;
2. falling back to the local `open_positions.json` paper-desk ledger.

The cohort counts from **`logs/vega_outcomes.jsonl`**. These are **different stores** — §2's suspicion is confirmed on the facts.

**But book-awareness cannot suppress anything.** `_annotate_book_awareness` sets `already_in_position` and appends a warning string; that flag is consumed only by `analysis/verdict.py:147` and two display paths in `vega_app.py`. `ALLOW_SAME_TICKER` appears in `config.py:1302` and in one docstring — **it gates no code**. Nothing drops a candidate for being in an existing name.

So the divergence corrupts **advice only**, exactly as the existing finding recorded. §4's guardrail-removal instruction ("disable book-awareness suppression for the prediction layer") is a **no-op against a suppression that does not exist**, and executing it would have removed a display annotation while appearing to unblock predictions. Not done — see §H.

### Local book, for the record

4 open rows: NKE 37.5/35.0, AMGN 380/375, SMH 535/530, NEE 80/77.5, all exp 2026-09-18, filled 08-04 → 08-07. Today's cycle marked 3 of 4; AMGN logged `MARK-UNAVAILABLE` (legs present but not quotable, bid/ask 0.02/0.87 and 0.05/0.85, last good mark 5d ago) and correctly declined to treat a stale 0.91 as a hold decision.

---

## §D — §3 result: the funnel, and which gate actually binds

**The funnel did not need building.** `logs/vega_counterfactuals.jsonl` already records the whole enumeration with a per-gate boolean map — 173 rows for 2026-09-03 across 45 tickers, 2,899 rows resolved today. It is written from the chain payload the scan already fetched, so the §3 constraint "issue no new quote calls" is satisfied by construction and the runtime delta is **zero**.

### Stage 1 — ticker level (54 scanned, 0 qualified)

| deaths | first gate hit |
|---|---|
| **19** | **no valid same-expiration credit spread enumerated** |
| 18 | IV rank |
| 7 | news blocking |
| 6 | POP floor |
| 2 | negative VRP |
| 1 | shared gates (credit_to_width, min_credit_usd, natural_credit_positive, earnings_clear, support_shelter) |
| **1** | **edge score** |

### Stage 2 — spread level (173 enumerated spreads)

| gate | failed | fail % | sole failure |
|---|---|---|---|
| pop | 78 | 45.1% | 4 |
| otm_buffer | 75 | 43.4% | 3 |
| **credit_to_width** | **61** | **35.3%** | **29** |
| delta_cap | 58 | 33.5% | — |
| quote_spread | 22 | 12.7% | — |
| liquidity | 17 | 9.8% | 1 |
| support_shelter | 46 | 26.6% | 6 |
| min_credit_usd | 11 | 6.4% | 1 |
| earnings_clear | 6 | 3.5% | 1 |
| natural_credit_positive | 3 | 1.7% | — |
| dte_window | 0 | 0.0% | — |

**21 spreads cleared all 11 spread-level gates.**

### Stage 3 — what killed the 21 finalists

| deaths | cause | names |
|---|---|---|
| 5 | **negative VRP** | META (−7.3pp) ×4, AAPL (−3.1pp) |
| 5 | news blocking | IWM ×4, CRWD |
| 7 | POP floor | TSLA ×3, GDX ×3, COIN |
| 4 | IV rank | ARKK ×2, FCX, QQQ |
| **0** | **edge score** | — |

### Resolving the five-cause contradiction

- **`MIN_EDGE_SCORE = 60`** kills **1 ticker of 54** and **0 of the 21 finalists**. It is **not** the binding constraint. Refuted.
- **`MIN_IV_RANK = 45`** kills 18 tickers but only **4 of 21 finalists**, and §A1 shows those readings are correct for a VIX-14 tape. Real, second-order, and **not an instrumentation artifact**.
- The two were never in conflict; they operate at different funnel depths, which is why placing them in one funnel dissolves the contradiction rather than adjudicating it.
- **`credit_to_width` is the largest single "one gate away" blocker: 29 of 45 sole failures.** This corroborates the standing finding that credit floors are first and IV rank second.

**Interpretation caveat, as required:** the gates *are* the strategy. Two of the five finalist deaths are **negative VRP** — the system declining to sell volatility that is cheaper than its own forecast of realised. The crypto view independently reads `STAND_ASIDE, expected_vrp = −12.57pp`. VIX is at a 46-day low. **The most parsimonious explanation of the drought is that there is currently no volatility risk premium to harvest**, which is precisely when a premium-selling strategy is supposed to produce nothing. This funnel shows *where* scarcity enters. It does not show the scarcity is wrong, and nothing here justifies loosening a floor.

---

## §E — Build status

### Item 4 — one prediction, end to end ✅

A prediction-and-grading loop already existed and demonstrably closes: 3,606 claims, 1,578 resolved, four horizons (`direction_overnight` / `_1d` / `_1w` / `_1m`), each with a baseline twin. **§4 substantially describes something already built.**

The genuine gap was narrower and sharper: **`price_projection` draws a confidence band, `predictions.BAND_CONTAINS` has had a working scorer since the band was drawn, and nothing had ever written one.** Zero band rows in 3,606. The coverage table the UI quotes (80% → 81.5%) is a **backtest**, and a backtest cannot see a live vol forecast that has stopped updating or a spot that has gone stale.

Proven end to end on a temp ledger, anchored 2026-07-22 so the horizons had already settled — 8 claims written, **8 resolved, 0 unresolvable**:

```
band_contains_overnight   correct=False  band 739.88-755.02  settled 739.37 (open)
band_contains_1d          correct=False  band 739.88-755.02  settled 738.18 (close)
band_contains_1w          correct=False  band 730.68-764.52  settled 729.46
band_contains_1m          correct=True   band 713.53-782.90  settled 762.60
```

(The overnight and 1d claims share a band and settle on different fields — the distinction works.)

### Item 5 — scaled to all horizons and the full watchlist ✅

`analysis/band_forecast.py`, wired into both cycle paths beside the direction sweep, same gate hour, same settlement calendar (so the two channels can be **joined on the day**, not merely compared in aggregate). First production sweep: **432 claims, 54 tickers, 0 abstained, 0 failed, 0 rows containing NaN**. Ledger backed up to `logs/vega_predictions.jsonl.pre-bandforecast-2026-09-03` first.

Each horizon gets its **own claim type**, so `grade()` buckets them separately and they can never be pooled. Each is written twice — forecast vol and trailing-vol baseline — because a coverage of 79% on an 80% band means nothing without a null model.

**Two defects caught during the build, both silent-failure class:**

1. **Unit mismatch.** `direction_forecast.realised_vol` returns a *decimal* (0.284); `vol_forecast.forecast_rv` and `price_projection.project` take *points* (28.4). Passing the decimal raises nothing — it is positive, clears every guard, and `forecast_rv` clamps it up to `MIN_VOL_PP = 1.0`. The 80% band would collapse to about ±0.3%, every claim would resolve OUTSIDE, and the channel would report a healthy row count while measuring only its own unit error.
2. **NaN propagation.** `nan <= 0` is False, `not nan` is False, and `max(1.0, min(400.0, nan))` is `nan`. A single padded bar in a live yfinance pull drove **all four horizons to a `nan`–`nan` band on the first run**. `json.dumps` writes a bare `NaN` token, which is not valid JSON, so this would have surfaced days later in a strict reader of the *ledger* with nothing pointing back at the pull.

Both are now guarded (`_finite`, `_usable`) and both have named tests.

**Walk-forward validation of the writer** — 8 names, ~51 anchors each over two years, 3,328 claims, 0 unresolvable, against a claimed 80%:

| horizon | coverage | baseline twin | verdict |
|---|---|---|---|
| 1d | **78.8%** | 77.2% | well calibrated |
| 1w | 86.1% | 83.2% | ~6pp wide |
| 1m | 86.1% | 83.9% | ~6pp wide |
| overnight | **95.4%** | 94.5% | **mis-specified** |

The overnight band is charged a full session of sigma but graded on the **gap alone**, so it over-covers badly. **Left as written rather than tuned** — shrinking a horizon until its coverage hits the target is fitting to the validation sample. It is recorded in the module docstring as the first calibration question this channel owes.

### Item 6 — ticker-day clustering (§6) ✅

`predictions.cluster_sample()` reports, beside every raw count: distinct ticker-days, independent time blocks (calendar days ÷ **longest** horizon present — averaging would credit the sample with independence the long claims destroy), and cross-sectional clusters keyed on `vol_forecast.sector_proxy_for` so SPY/QQQ/IWM collapse to one market bet. `n_effective = min(raw, blocks × clusters)`, capped by raw and deliberately conservative.

**`gradeable` now keys off the effective count, not the raw one.** Live effect on the existing direction channel:

| claim type | raw | **effective** | ticker-days | blocks | clusters |
|---|---|---|---|---|---|
| direction_overnight | 336 | **96** | 336 | 6 | 16 |
| direction_1d | 336 | **96** | 336 | 6 | 16 |
| direction_1w | 112 | **16** | 112 | 1 | 16 |

And on the walk-forward above: **416 raw claims per horizon are ~6 independent observations.** That is the honest number, and it is the number the doc asked for.

### Item 6 (doc numbering) — shadow-book writer

**Already fixed** (§A4). No code change was needed or made. What *was* wrong is that the liveness rule reported `CRITICAL` naming a cause that no longer exists — a message that had already misdirected reviews. `CRITICAL` is now reserved for a row written **after** the fix with no basis (the writer actually being at fault); a set that entirely predates it reports `STARVED` and says plainly that it is unrecoverable and correctly refused. Both branches have tests.

### Item 7 — projection overlay

**Already built.** `analysis/price_projection.py` provides `project()`, `implied_band_from_chain()`, `compare_bands()` and `strike_position()`, coverage-tested on ~14,900 held-out observations, and is already consumed by `vega_app.py` and `main.py:634`. The three legs of §5 are now **recorded on every band claim**: `forecast_vol_pp`, `trailing_vol_pp`, `long_run_vol_pp`, and `implied_vol_pp` with its derived band on the identical horizon.

The strike-selection hook was **not built** — see §H.

### Item 8 — `/vega/ingest` writer

`vega_ingest.post_to_jarvis` forwards `scan_entry` verbatim; `_enrich_qualified_trades` is an explicit no-op. It writes no credit basis of its own and adds no fields, so it cannot be a source of bad rows independent of the scanner — and the scanner has carried a natural credit since 2026-08-10. It has also posted very few qualified trades through the drought [see correction below]. No live defect found; no change made.

### Registration (§0)

**No new ledger was created.** `band_forecast` writes to the already-registered `logs/vega_predictions.jsonl` through `predictions.record()`, which `tests/conftest.py` already redirects — so there is no new hole in the isolation list, which is where the sixth ledger has been missed before. A test asserts the module names no ledger file and holds no path constant.

It **is** registered as its own **liveness channel**, because sharing a file is exactly the hazard: `prediction_ledger` read `OK` on 3,000 direction claims while the band scorer had never been fed once. A starved channel hiding inside a healthy one is invisible to anything but an explicit list. The registry test is pinned as an exact set.

---

## §F — New findings not in the document

1. **The largest single ticker-level rejection bucket is one the document never mentions.** 19 of 54 tickers die at *"no valid same-expiration credit spread found"* — more than IV rank (18) and more than everything else. Whether that is thin chains, the DTE window, or the expiration-alignment logic is unanswered, and it is the biggest unexplored block in the funnel.

2. **Every direction horizon has resolution ≈ 0.000 — the channel carries no discriminating information.** `direction_overnight`: hit 18.5%, Brier 0.174, skill **−0.154**, resolution 0.000, *"what shuffling the outcomes produces 100% of the time."* Its baseline twin: hit 64.6%, Brier 0.326, skill **−0.427**.

   The hit-rate gap is **not** evidence the tilt is harmful, and I nearly reported it as such. The two predict different categories — the baseline always says "flat" (a wide, easy target) while the tilted version commits to up/down. Brier actually *favours* the forecast (0.174 vs 0.326). The correct reading is the decomposition: **both have zero resolution.** The direction channel is a calibration exercise with no skill, which strengthens rather than weakens the case for measuring the *band* — the quantity the strategy actually depends on.

3. **`iv_rank` in the counterfactual ledger varies by strike within one ticker on one day** (GDX 64.9 / 64.9 / 45.9; ARKK 15.8 / 13.2), and disagrees with the ticker-level IV rank in the rejection record. Two fields share a name and are not the same quantity. Not chased; flagged because that is the exact shape of the `credit_per_share` defect.

4. **`--mark-only` has never run.** No mark-only line appears in `output/paper_desk/auto_paper_cycle.log` at all. The normal cycle reprices anyway (`marked=3` today), so nothing is broken — but the end-of-day run the 2026-07-21 runbook describes is not executing, and `data_quality_log.compact()` is documented as deliberately deferred to it.

5. **The `APPROX` fallback disables `MIN_IV_RANK` entirely** (`main.py:670–675` — hard block only under `HISTORY`). Combined with the plausibility filter's 30-clean-sample floor, the gate stops being enforced at exactly the moment the data is worst. On 2026-09-01 this fired on IWM, MAR and XOM at 29/30. Currently dormant; one sample wide.

6. **SPY 20-day realised vol is 7.38%** against a 120-day of 13.87% and a forecast of 10.56%. This is an extreme low-vol reading and is the single most economical explanation of the drought.

---

## §G — Open items, ordered by consequence of failure

1. **Log the CRWD and QQQ trades.** `python log_outcome.py list`, then `fill` / `close`, or the dashboard's manual form. These are the only fill-verified ground truth in the project and they are currently unrecorded. Every calibration conclusion is weaker until they exist. **Highest value, zero risk, operator-side, unblocked.**
2. **Confirm the overnight band's over-coverage on production rows** before deciding whether it gets a gap-variance estimate or gets retired. First settlement is 2026-09-04.
3. **Investigate "no valid same-expiration credit spread" (19 of 54).** Largest unexplained block in the funnel.
4. **Decide `ENTRY_HOLD`.** Not the drought, but while it is on, "zero opens" is over-determined and `caps_cohort` cannot leave `STARVED`.
5. **Rotate the NewsAPI key** (`sha256[:12] 0176a2ea1371`, exposed 2026-05-27 → 2026-07-21). The repo is public.
6. **Verify the OAuth preflight** ran under the 09-03 08:35 cycle. Today's log shows `Token OK — 165.2h remaining`; expiry 09-10, margin fires 09-09.
7. **Delete the three stale branches** — `feat/entry-timing`, `feat/opportunity-density-and-pop-framing`, `fix/vega-mark-availability-2026-08-20`. Confirmed present; `feat/entry-timing` is local-only.
8. **Reconcile the two `iv_rank` fields** (§F3).
9. **The `APPROX` gate hole** (§F5) — decide whether `MIN_IV_RANK` should block under `APPROX` too, or whether the plausibility floor should be lowered.
10. **Standing:** the 09-15 resolution-agreement check on real matured rows; the chain-size absolute floor as a v2 boundary decision.

---

## §H — What I would not do

**1. I did not disable book-awareness on the prediction path, because there is nothing to disable.** §4 describes suppression that does not exist: `_annotate_book_awareness` sets a flag consumed only by display and verdict text, and `ALLOW_SAME_TICKER` gates no code. Executing the instruction would have removed a genuine display annotation while producing a plausible "unblocked the prediction layer" line in this report. The band sweep runs over the full watchlist unconditionally, which is what was actually wanted.

**2. I did not build the strike-selection hook, even flagged off.** §5 is right that an ungraded projection must not move a strike. But an unused hook is not free: it is a code path nothing exercises, sitting next to a flag whose only purpose is to be turned on later. The honest sequencing is to accumulate graded rows first, then build the hook against a horizon that has *demonstrated* calibration. Nothing is lost by waiting — the projection is recorded and displayed today, which is all §5 asks for before calibration.

**3. I did not tune the overnight horizon to hit its coverage target.** 95.4% against a claimed 80% is a real mis-specification and I could have made the number look right by shrinking the sigma. That would be fitting to the validation sample and would destroy the evidence. It ships known-wide and documented.

**4. I would reorder the document.** §1 is placed first as "the highest information-per-minute test," and it cost real time to refute. The funnel (§3) was **already sitting in a ledger**, needed no new quote calls, and answered two of the five contested causes outright. §3 should have been first. The pattern is worth naming: §1 was reasoned from a code comment, and §3 from data already on disk — and the doc's own §0 says measurement beats availability-driven diagnosis.

**5. Three of the eight ordered items were already complete before the session started** (item 6's writer fix, item 7's projection overlay, most of items 4–5's prediction engine). The build doc was written against a repo state older than the repo. Given its own session-history caveat, I would treat "verify this is still true" as a required first step on any future doc of this kind — the cost of checking is minutes and the cost of not checking, this time, would have been a paused grading channel waiting for a fix that shipped on 2026-08-10.

**6. One claim I am not confident in.** The `n_effective` estimate is deliberately conservative and is *not* a variance-corrected effective sample size — it is `blocks × clusters`, capped by the raw count. It cannot flatter a sample, which is the property that matters here, but it should not be quoted as though it came from a proper cluster-robust variance estimate. If the band channel ever reaches a decision boundary on these numbers, that estimator deserves replacing with a real one.

> **CORRECTION, 2026-09-04.** The claim above that the board has qualified zero trades since 2026-08-10 is WRONG, and it was load-bearing in several documents this week. From `scan_log.json`: the board qualified **1-2 per scan on most days through 2026-09-01** (76 qualified across 193 scans). TRUE zero began **2026-09-02**. What happened on 2026-08-10 was commit `d6255b9`, which moved bull puts from mid to natural credit and cut the rate from 4.47/scan to 0.39/scan -- 91% of the drop. The remaining 9% is the market. See `reports/claude_VEGA_QualificationSeries_2026-09-04.md`.
