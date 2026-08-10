# VEGA — Meta-Review of Three Reviews, Code Validation, and the Next-Generation Plan
**Date**: 2026-08-10
**Scope**: Adjudicates the GPT, Grok, and Claude reviews and the Claude improvement plan against the code and the ledger as they exist on disk today, then replaces the plan.
**Method**: Every claim below was checked against source. Code claims cite `file:line`. Empirical claims were computed from `logs/vega_outcomes.jsonl` (211 records) and `logs/vega_predictions.jsonl` (32 records).

---

## Part 0 — The one-paragraph verdict

All three reviews are arguing about the calibration of an instrument that is not currently taking readings. The improvement plan inherits that error and makes it structural: **its two Sprint-1 items — the ones it calls prerequisites for trusting anything the system reports — are respectively a no-op and unimplementable as written.** Item 1A wires `edge_score` into candidate ranking, but `edge_score` is `None` on 100% of real paper trades for a reason the plan never identifies, so the fix multiplies zero by fifty. Item 1B patches a field name that does not exist, in a function that is not the one enforcing the rule, while a correct fail-closed earnings gate already sits in the repo with thirteen passing tests and no production caller. Meanwhile the ledger's headline statistic — the 22% win rate all three reviews reason from — is an artifact of pooling two pricing regimes whose separation is total: **mid-fill trades are 13/18 (72%); natural-fill trades are 0/46 (0%).** No review noticed. The next-generation plan therefore has to start one sprint earlier than any of them proposed: before you can improve the selection engine, you have to make the system capable of recording what it selected on.

---

## Part 1 — Review of the reviews

### 1.1 ChatGPT — Grade: A− on framing, D on evidence

**What it got right, and it is the most valuable single contribution in the entire packet:** the reframe from "trading bot" to "opportunity discovery + validation engine." The §10 three-layer restructure (Validity → Safety → Edge) is the correct architecture and is genuinely better than what VEGA has. So is §11's core distinction: *can this invalidate the trade?* versus *can this make one opportunity better than another?* VEGA currently over-gates. `support_shelter` is a ranking signal wearing a gate's clothes. §20 (model disagreement as a first-class signal) and §22 (counterfactual tracking of rejected candidates) are both correct and both absent from Claude's plan entirely.

**Where it fails:** it never opened a file. Every technical assertion is inherited from the brief, so where the brief was stale or wrong, GPT is stale or wrong with confidence. §26 asserts liquidity/quote-quality is checked only on the short leg — half true; `vega_candidates._quote_spread_ok` (vega_candidates.py:188-192) checks **both** legs and has since it was written. The shared contract's `_quote_spread_ok` (assessment.py:230) checks one. Two implementations of the same rule, disagreeing, in a codebase whose stated first design constraint is "One definition. No rule may exist in two places." GPT flagged the symptom and missed that it is an instance of the repo's own documented failure mode.

**Net:** use GPT for the product architecture. Do not use it for a single factual claim about the system.

### 1.2 Grok — Grade: A− on adversarial instinct, C+ on accuracy

**Best single observation in the packet, and nobody followed it up:** *"That cohort therefore measures the old exit far more than the entry model."* This is exactly right and it is more right than Grok knew. The data (Part 3.5) shows the entry model has never been cleanly tested, and the reason is mechanical, not statistical.

Also correct and unique to Grok: the gate contract is structural hygiene with the single richness check (`MIN_IV_RANK`) living *outside* it. Verified — `MIN_IV_RANK` is enforced in `auto_paper_cycle._pick_new_trades` (auto_paper_cycle.py:347-352), not in `REQUIRED_GATES` (config.py:223). Grok is right that this is the same architectural shape that produced the four documented enforcement leaks. Its §4 "textbook constant" taxonomy and the observation that the 3× wolf truncates at ~35% of max loss on a structure whose edge lives in the right tail are both sound.

**Where it fails:** the specific claim *"support_shelter fails open; earnings_clear fails closed... no coherent principle"* is exactly backwards on the live path, and the principle is not only coherent, it is written down. `assessment._shelter_ok` (assessment.py:237-243) fails open **and documents why**: "The earnings gate fails closed by design; this one cannot, because a level read depends on price history that is routinely thin and a data gap must not empty the board." That is a coherent principle — the asymmetry is deliberate and correctly reasoned. Grok's error is that it attacked the *stated* design rather than checking whether the code implements it. It does not (Part 3.2), which is a far better criticism than the one Grok made.

Grok also asserts "n=5 ravens cohort." The ledger carries 15 `close_logic: ravens_v1` records, 5 closed.

### 1.3 Claude Independent Review — Grade: B+ on findings, C on verification discipline

This is the only review that read code, and it found the single most consequential defect in the system: **`_candidate_score` ignores `edge_score` and ranks on `pop_implied` rather than `true_pop`.** Verified exactly as described (auto_paper_cycle.py:292-300). That finding alone justifies the review.

Its architectural read is also the most accurate of the three: gate/edge/management separation, the DECLARED-vs-LEARNED split in `ticker_profile`, and Muninn's epistemic honesty are all correctly characterized and correctly graded.

**But it repeats the error it accuses the others of.** It corrects GPT and Grok on the earnings gate — "Both GPT and Grok reported this as 'safely rejecting' unknown cases; the code says the opposite" — and quotes:

```python
d = ctx.get("earnings_date")
if d is None:
    return True
```

**That code does not exist.** The actual function (assessment.py:289-298) reads `ctx.get("earnings_days")`. The review misquoted the field name of the function it was using to prove the other two reviewers hadn't read the code. And it drew the wrong conclusion: GPT and Grok said fail-closed because the *documented, tested* implementation is fail-closed (`vega_candidates._earnings_clear`, vega_candidates.py:128-155, with 13 tests in `tests/test_earnings_gate.py`). All three reviewers were describing different real functions and none checked which one runs. The truth is worse than any of them reported (Part 3.2).

Two further unverified claims:
- *"the 1.5x stop is a data-failure fallback, not a primary risk control... For trades where data is available (the common case), 3.0× is the only floor."* The ledger says the opposite: **45 closes by `auto-stop-loss` vs 5 by `wolf-stop`**, 9:1. The legacy path is the common case.
- *"the 59-trade credit_stop_1.5x_natural cohort"* and *"the 5-trade ravens_v1 cohort"* treated as ledger fields. **There is no `cohort` field.** All 211 records carry `cohort: None`.

### 1.4 The Improvement Plan — Grade: D on executability

The plan is well-organized, correctly prioritized *in the abstract*, and its two highest-priority items would both fail on contact with the code. Detailed in Part 4.

---

## Part 2 — Claim validation table

| # | Claim | Source | Verdict | Evidence |
|---|---|---|---|---|
| 1 | `_candidate_score` ignores `edge_score`, uses `pop_implied` | Claude | **TRUE** | auto_paper_cycle.py:292-300 |
| 2 | `evaluate_gates` is the single enforced contract, asserts on missing keys | all | **TRUE** | assessment.py:258-286 |
| 3 | `_earnings_clear` fails open on unknown | Claude | **TRUE but wrong field, wrong function, wrong reason** | assessment.py:294-296 reads `earnings_days`, not `earnings_date` |
| 4 | earnings gate fails closed | GPT, Grok | **TRUE of the dead implementation** | vega_candidates.py:145-146; zero production callers |
| 5 | `support_shelter` fails open with no coherent principle | Grok | **FALSE on the principle** | assessment.py:237-243 documents the asymmetry deliberately |
| 6 | Liquidity/quote-spread check only the short leg | all | **HALF TRUE** | assessment.py:225-234 short only; vega_candidates.py:188-192 checks **both** legs |
| 7 | `MIN_STRIKE_BUFFER_STOCK = 0.05` flat across all equities | Claude, Grok | **TRUE** | config.py:51; assessment.py:166-174 |
| 8 | `IV_HV_INFLATOR = 1.2` is a global constant with no per-ticker path | all | **STALE** | per-ticker hook already exists: `technicals.iv_hv_inflator(ticker)` (technicals.py:150-177) reading `ticker_profile.DECLARED`; deliberately unpulled |
| 9 | 3%-of-spot ATM window is a fixed textbook constant | Claude, Grok | **STALE** | already adaptive: widens 3→5→8→12% until `ATM_IV_MIN_CONTRACTS`, returns 0.0 rather than a wrong number (technicals.py:203-214) |
| 10 | `CHAIN_QUALITY_MIN_RATIO = 0.30` is too lenient | all | **TRUE on the value** — but it *is* hard-enforced, not advisory | fetcher.py:723-731 returns `[]` and skips the ticker |
| 11 | Two stops coexist (1.5× legacy, 3.0× wolf) | Claude | **TRUE** | config.py:128, config.py:834 |
| 12 | 1.5× is a rarely-firing data-failure fallback | Claude | **FALSE** | 45 `auto-stop-loss` vs 5 `wolf-stop` closes |
| 13 | Cohort fields `credit_stop_1.5x_natural` / `ravens_v1` exist in the ledger | Claude | **FALSE** | no `cohort` key on any of 211 records |
| 14 | Muninn is blind; no closed trade has a stress snapshot | all | **NOW FALSE** | 5 closed trades carry snapshots — still below `MUNINN_MIN_COMPARABLE`, so operationally blind |
| 15 | Prediction ledger has zero resolved claims | Grok | **TRUE** | 32 claims, all `status: open`, 0 resolved; only 3 of 6 claim types ever emitted |
| 16 | `ODIN_RECOVERY_THRESHOLD = 0.35` has never fired the CLOSE branch | Claude | **TRUE** | odin.py:58 unreachable while `sufficient: False` |
| 17 | `MIN_IV_RANK` richness gate lives outside `REQUIRED_GATES` | Grok | **TRUE** | config.py:223 vs auto_paper_cycle.py:347-352 |
| 18 | `TRUE_POP_DRIFT_MODE = "risk_free"` is live | Claude | **TRUE** | config.py:465 |
| 19 | Ravens architecture matches the described decision matrix | all | **TRUE** | odin.py:40-79 |
| 20 | 59 closed trades, 22% win rate | all | **WRONG NUMBERS, AND THE WRONG QUESTION** | 64 closed, 13W/51L = 20.3%; see Part 3.5 |

**Scorecard.** Of 20 checkable claims: 10 true, 3 false, 3 half-true or stale, 4 true-but-materially-mischaracterized. The reviews' aggregate accuracy on VEGA's actual state is roughly 50%.

---

## Part 3 — What all three reviews and the plan missed

These are the findings that change what should be built. Each was verified against source or computed from the ledger.

### 3.1 `edge_score` is structurally uncomputable on the path that opens trades

**This is the root cause under the plan's headline item, and the plan does not contain it.**

`assessment.assess()` computes the edge score only inside this guard (assessment.py:387-391):

```python
true_pop = spread.get("true_pop")
...
if true_pop is not None and implied is not None:
    # ... only here is edge_score computed
```

On the auto-open path, `vega_candidates.main()` calls `build_candidates()` at line 589 — which invokes `_assess_candidate` → `assess()` at line 383 — and calls `attach_true_pop()` at line 604. **`true_pop` is attached fifteen lines after the only code that would have used it.** Every fast-scan candidate is assessed with `true_pop = None`, so the guard never opens, so `edge_score` is `None`, permanently, by construction.

The ledger confirms it exactly: of 79 real paper records, **`edge_score` is non-null on 0**. (`vrp`: 0. `technical_score`: 0. The 132 records that *do* carry these are `status: modeled` — main.py's cockpit path, which never opens a trade.)

Consequence for the plan: `(edge * 50.0)` is `(0 * 50.0)` on every candidate. Item 1A ships, changes the ranking by a constant, and the operator concludes edge-weighted selection was tried and made no difference.

**The fix is an ordering fix, and it must land before 1A:** attach `true_pop` before assessment (or re-assess survivors after attaching), so the guard can open at all.

### 3.2 A correct, tested, fail-closed earnings gate exists in the repo and is dead code

`vega_candidates._earnings_clear` (vega_candidates.py:128-155) implements exactly the semantics all three reviews want — ETF-aware, fails closed for a non-ETF with no known date, blocks any print on or before expiry — and documents why. `attach_earnings_gate` (line 158-173) applies it to candidates and refreshes gate counts. `tests/test_earnings_gate.py` covers it with 13 tests including `test_unknown_earnings_fails_closed_for_equities`.

**Nothing in production calls either function.** The only callers are the tests themselves. When the gate consolidated into `assessment.evaluate_gates`, the live path became (vega_candidates.py:593-597):

```python
# earnings_clear is set by analysis.assessment.evaluate_gates during the build.
n_earn_blocked = sum(1 for _c in cands
                     if not (_c.get("gates") or {}).get("earnings_clear", True))
```

It now only *counts*. The enforcing implementation is `assessment._earnings_clear`, which returns `True` on unknown with the comment *"unknown — the caller's own gate decides"* — referring to a caller that no longer decides.

Three compounding holes in what actually runs:
1. `_earn_days` is computed only for non-ETFs, inside a bare `try/except` that swallows any lookup failure to `None` (vega_candidates.py:580-585).
2. `None` reaches `assessment._earnings_clear` and returns `True`.
3. `assessment._earnings_clear` has no `is_etf` awareness, so it cannot distinguish "no earnings exist" from "lookup failed" — the exact distinction the dead function was built to make.

**This is the plan's own lesson turned on the plan.** A green 13-test suite proving a function that never runs is precisely the "metric that cannot be non-zero" failure. It should be the template case in whatever regression discipline comes out of this.

### 3.3 Huginn's primary signal is blind on 100% of trades

`huginn.read_support` is documented as "The primary signal." It opens (huginn.py:170-176):

```python
sp = trade.get("support_level_at_entry")
...
if sp is None or price is None or not closes:
    return {"status": "UNKNOWN",
            "reason": "no support level recorded at entry — Huginn is blind to structure"}
```

**`support_level_at_entry` is null on all 79 real records.** `ol.open_paper_trade` is never called with it (auto_paper_cycle.py:470-507). The field is read but never written. Huginn's primary signal has returned `UNKNOWN` on every position the system has ever managed, and Muninn's `similarity()` weights `support_status_at_stress` at 0.25 — a quarter of the similarity function is permanently unfillable for the same reason.

No review caught this. It is a one-line write at open time and it unblocks two subsystems.

### 3.4 Two of the five wolves can never fire

`_ravens_close_check` constructs Huginn's data dict with (auto_paper_cycle.py:557-559):

```python
"news_sentiment": None,      # close-time news read is not wired yet
"earnings_check": {},        # nor is a close-time earnings check
```

`check_wolves` tests `ec.get("in_window")` (huginn.py:137-138) and `(data.get("news_sentiment") or "").upper() == "BLOCKING"` (line 143). Both are structurally unreachable. Of the five hard floors every review praised as "reasonable unconditional stops," **three are live** (gap ≥1.5 ATR, |delta| ≥ 0.55, mark ≥ 3× credit).

Combine 3.3, 3.4, and Muninn's blindness and the honest description of the ravens framework as it runs today is: *a three-condition mechanical stop with narration attached.* The architecture is as good as the reviews say. Approximately none of it is executing.

Note the compounding with 3.2: earnings risk is now unmanaged at **both** ends — the entry gate fails open on unknown, and the close-time earnings wolf cannot fire.

### 3.5 The win rate is a fill-model artifact, and the separation is total

Every review reasons from "59 trades, 22% win rate." Recomputed from the ledger:

| Population | n | Wins | Rate |
|---|---|---|---|
| All closed | 64 | 13 | 20.3% |
| `fill_model: mid` | 18 | 13 | **72.2%** |
| `fill_model: natural` | 46 | 0 | **0.0%** |

**Every win in the ledger is a mid-fill trade. Every natural-fill trade lost.** This is not a cohort that needs careful handling — it is two disjoint populations, and pooling them produces a number that describes neither.

The mechanism is arithmetic, not selection. Mid overstated achievable credit by ~75% (documented at auto_paper_cycle.py:441-445). A profit target at 65% of an inflated credit is easy; a stop at a multiple of an inflated credit is far away. Book the real credit and both flip: median natural credit is **$0.45/share ($45/contract)** against a **$2.16 round-trip cost**, so the 1.5× stop triggers on a **$22.50** adverse move — inside the bid-ask noise of the position itself.

**And the natural cohort is itself contaminated.** The five `ravens_v1` wolf-stops are GDX and AMGN trades opened 2026-08-06/07 at credits of **$0.07–$0.25/share**. A $9 credit on a $1-wide spread is 9% credit-to-width against a 15% floor, and $9 against a scaled minimum of ~$19. They should not have passed. The cause is documented in the code that fixed it (vega_candidates.py:329-335): the gate read the **mid** credit and the desk filled at **natural**. So `ravens_v1`'s 0/5 record — the number Grok called "not encouraging" and the plan treats as a live signal about the framework — **measures a since-fixed pricing leak, not the ravens.**

Bluntly: **VEGA has never recorded a clean trade.** Mid-fill trades were priced at a fill that could not be achieved. Natural-fill trades were selected under a gate reading the wrong price basis. There is no cohort in this ledger from which any conclusion about the selection model can be drawn, and every review drew one.

One residue of that leak is still in the code: `_candidate_passes_minimum` re-checks the credit floor against `c.get("credit_usd")` — the **mid** value (auto_paper_cycle.py:269-271). It is currently harmless because the contract's `min_credit_usd` gate checks `natural_credit_usd` (assessment.py:272), so the weaker check cannot leak past the stronger one. But it is the exact shape of the bug that just cost the system its only ravens-era data, sitting live in the file.

### 3.6 The entry-state instrumentation built for calibration is empty

The block at auto_paper_cycle.py:462-507 exists specifically so a calibration run "can ask whether a score was right... or wrong because the inputs were wrong." Coverage across 79 real records:

| Field | Non-null | Why |
|---|---|---|
| `edge_score`, `vrp`, `technical_score` | **0** | §3.1 ordering bug |
| `vix_at_entry` | **0** | reads `row["ctx"]["vix"]`; `vol_context` doesn't emit it — VIX lives in `meta` |
| `support_level_at_entry` | **0** | never written (§3.3) |
| `atm_iv_at_entry`, `rv_at_entry`, `pop_gap_at_entry` | **4** | pre-date the wiring |
| `true_pop` | 65 | the one that works |

`pop_gap` is described in the code as "the central claim of the whole system... the one number the ledger could never grade" (vega_candidates.py:198-200). It is populated on 4 of 79 records. The `vix_at_entry` gap also silently defeats Muninn's `vol_regime` fallback (muninn.py:150-151), which is written to fall back to the trade's entry VIX when the live read fails — a fallback that has never had a value to fall back to.

### 3.7 Smaller confirmed defects

- **`_candidate_score` defaults `gates_total` to 8; there are 11 gates** (auto_paper_cycle.py:294). Any candidate missing the key gets `q = passed/8`, which can exceed 1.0 and dominate the score.
- **`_quote_spread_ok` is implemented twice with different semantics** — both legs in `vega_candidates`, short leg only in the shared contract. The shared contract is supposed to be the single definition. GPT's finding was real; its scope was wrong.
- **`SKEW_SCORING_ENABLED = False`** (config.py:934), disabled pending exactly the chain-quality gating that fetcher.py:723 already implements. The stated re-enable condition is met; the flag was never flipped.
- **`stop_mult` defaults to 2.0** where config says 1.5 (auto_paper_cycle.py:~700 `getattr(config, "STOP_LOSS_MULTIPLIER", 2.0)`) — harmless while config defines it, live if the constant is ever removed.

---

## Part 4 — Why the improvement plan fails, item by item

| Item | Plan's claim | Reality |
|---|---|---|
| **1A** Wire `edge_score` into selection | "One function, one file. Low effort." | **No-op.** `edge_score` is `None` on 100% of auto-open candidates (§3.1). Also swaps in `true_pop` — correct, but `_candidate_passes_minimum` already prefers `true_pop` for the POP floor (auto_paper_cycle.py:281-283), so the plan is half-describing existing behavior. Real effort: ordering fix in `vega_candidates.main` **first**, then the scorer. |
| **1B** Fix `earnings_clear` to fail closed | Patch `ctx.get("earnings_date")` | **Unimplementable.** The field is `earnings_days` (assessment.py:294). Applying the plan's diff creates a read of a key that is never set → always `None` → always `False` → **every candidate blocked, every cycle.** The plan also never mentions that the correct implementation already exists and is orphaned (§3.2). |
| **1C** Document stops, add `effective_stop_multiplier` for cohort analysis | "Medium effort" | Documentation is right and cheap. The field addition targets "the cohort analysis," which **does not exist and neither does the cohort field** (§2, claim 13). And the premise — that the 1.5× is a rare fallback — is contradicted 9:1 by the ledger. |
| **2A** Vol-regime strike buffer | Sound | **Keep, and it is the plan's best item.** But `_otm_buffer_ok` (assessment.py:166-174) currently receives no `iv`; the plan says iv "is already available in ctx," which is true (`ctx["atm_iv"]`) but only populated on the BTC path (assessment.py:203-207). Real effort is higher than stated. |
| **2B** Density-aware IV inflate | Sound in principle | **Largely already built.** The ATM window is already adaptive (§2, claim 9) and `iv_hv_inflator(ticker)` already exists as the per-ticker lever (claim 8), deliberately unpulled pending measurement. The remaining work is *measuring IV/HV per name*, which the plan doesn't propose. |
| **2C** Raise chain quality 0.30 → 0.50 | "One config value" | Correct and cheap. Note it is a **hard skip**, not a warning (fetcher.py:723-731), so the cost is real; the plan understates it. |
| **3A** Long-leg liquidity/spread | Sound | **Already done in one of the two implementations** (vega_candidates.py:188-192). The correct framing is not "add a check" but "collapse two implementations to one" — which is the repo's stated first design constraint. |
| **4A** Retroactive partial Muninn snapshots | "High impact" | **Actively harmful right now.** Muninn is blind because 5 < `MUNINN_MIN_COMPARABLE`, and those 5 are the contaminated GDX/AMGN leak trades (§3.5). Backfilling approximations from 64 closes — 46 of which were selected under the wrong price basis and 18 under an unachievable one — manufactures a base rate from a corrupt population. This is exactly the fabrication `muninn.py:18-21` exists to forbid. **Reject this item.** |
| **4B** Grid-search `ODIN_RECOVERY_THRESHOLD` over 6 values at n≥20 | "Low effort, data-driven" | **Statistically invalid as specified.** Six trials on ~20 observations is textbook selection bias; Bailey & López de Prado's deflated-Sharpe / minimum-backtest-length work shows the required track length for a credible selection under multiple testing is far longer than the plan's trigger. Ship the harness, record the trial count, and gate the *use* of the result on a deflation test — don't gate it on n≥20. |
| **5A** Earnings data gap audit | Correct and necessary | Elevate — it is a prerequisite for 1B, not a Sprint-4 item. |
| **5B** Dual-engine alignment test | Correct | Elevate. §3.7 shows the engines have already diverged again on `_quote_spread_ok`. |
| **5C** Cohort purity assertion | "Low effort" | **Enforces a field that does not exist.** Replace with: *write* a cohort field, defined by the tuple that actually determines comparability — `(fill_model, gate_basis, close_logic)`. |

**Structural verdict on the plan:** it optimizes selection quality in a system that cannot currently record what it selected on, cannot manage what it opened, and has never produced a clean observation. Sprints 1–4 are, in the plan's own words, prerequisites for trusting what the system reports — and none of them makes the system able to report anything.

---

## Part 5 — Research findings that change the strategy

### 5.1 The core edge thesis has a live academic challenge

VEGA weights VRP at **30/100** in the edge score — the single largest component. Dew-Becker & Giglio, *The Decline of the Variance Risk Premium* (Chicago Fed WP 2025-17, Sept 2025), document that equity index options historically showed sharply negative returns and CAPM alphas, but **over the past ~15 years option alphas have become statistically indistinguishable from zero.** Their synthetic-option construction — which strips option-market frictions — shows *never* having had negative alpha over 100 years, implying the historical premium was an intermediary-frictions phenomenon that has been competed away.

This does not say premium selling cannot work. It says the specific thing VEGA weights most heavily is, at index level, a decaying premium. Three implications, none of which appear in any review or the plan:

1. **VRP's 30-point weight is a hypothesis with adverse recent evidence and must be the *first* thing the calibration engine tests**, not a component the system assumes and tunes around.
2. **The surviving premium is more likely single-name and dispersion-driven than index-level.** VEGA's watchlist is single-name-heavy — an advantage, if it measures per-name VRP realization rather than treating VRP as one number.
3. **Regime-conditioning is not optional.** A premium that decayed over 15 years is one whose historical base rates are not stationary, which directly undermines Muninn's design assumption that old comparables inform new situations.

### 5.2 VEGA's VRP measurement mixes two different premia

Papagelis, *The Variance Risk Premium Over Trading and Nontrading Periods* (Journal of Futures Markets, 2025), decomposes VRP into overnight and intraday components and finds them **opposite in sign**: significantly negative overnight, positive-to-insignificant intraday, with different predictive horizons (intraday predicts short, overnight predicts long).

VEGA computes realized vol from close-to-close 30-day log returns (`technicals._iv_rank_hv_approx`, technicals.py:134-141). Close-to-close **includes the overnight gap**, so VEGA's HV is the sum of two components whose premia have opposite signs, compared against an ATM IV spanning both. For a strategy whose defining risk is the overnight gap — `gap_event` is literally wolf #1 — this is not academic. Computing open-to-close and close-to-open RV separately is a few lines and turns one muddled number into two signals, one of which maps directly onto the system's own primary tail risk.

### 5.3 Raw Brier is the wrong instrument at VEGA's sample size

The plan and the ledger both aim at Brier scoring. Raw Brier conflates two things VEGA needs separated: **BS = Reliability − Resolution + Uncertainty.**

- **Reliability** — is 78% actually 78%? (calibration)
- **Resolution** — do the high-confidence calls differ from the low ones? (discrimination)
- **Uncertainty** — `ō(1−ō)`, fixed by the base rate, model-independent

A system that always predicts the base rate scores a respectable raw Brier with **zero resolution** — Grok's "claims whose stated probability matches the unconditional base rate look well-calibrated by construction; that is not skill," which the decomposition makes measurable rather than rhetorical. With 32 unresolved claims and a realistic horizon of dozens rather than thousands, VEGA should report all three terms with bootstrap confidence intervals from the first resolution, and treat **resolution > 0 with a CI excluding zero** as the actual bar for "the edge score adds information."

### 5.4 Competitive position

| Platform | Price | Strength | Gap VEGA can exploit |
|---|---|---|---|
| ORATS | ~$99/mo | Proprietary smoothed IV surface, 300M+ pre-computed backtests, realistic bid-ask fills | No falsifiable forward claim record on its own recommendations |
| Market Chameleon | $99/mo | Deepest IV/earnings research, 18+ strategy screeners | Delayed data; steep learning curve; research not decision |
| OptionStrat | Free / $20 Pro | Best-in-class payoff visualization and optimizer | **"Does not scan the market for you"** — visualizes a trade you already found |
| Option Samurai | Varies | 24 strategies, 170+ filters, broad universe | Firehose; no grading of its own output |
| Broker-native | Free | Real-time + execution | No edge model, no calibration |

The consistent, structural gap across the entire field: **nobody publishes non-cherry-picked calibration of their own recommendations.** Every platform's track record is either absent, selected, or unsized. VEGA's prediction ledger + Brier decomposition is the only component in this packet that no competitor has, and it is currently the component with zero resolved observations.

That is the whole strategic story. VEGA's moat is not better spread-finding — ORATS finds spreads on better data across 5,000 symbols. **VEGA's moat is being the only system that can be caught being wrong.** Everything in the plan should be ordered by how fast it gets the system to a first honest, gradeable observation.

### 5.5 Data acquisition is the plan's largest omission

The plan contains no data item. Every review named yfinance as the top infrastructure liability, and the plan's response is to raise a quality threshold on the same feed. Meanwhile the entire "prove the edge" program in §5.1/§5.3 requires historical chains with greeks and realistic bid-ask — which yfinance cannot provide at any threshold, because it has no history at all. ORATS runs ~$99/mo with a hosted backtester; ThetaData is materially cheaper for raw EOD if you write your own harness. At VEGA's stage the correct purchase is **history, not live quotes**: one historical chain dataset converts "we will know in 6-12 months of live paper trading" into "we can know this month," which is the difference between a project and a product.

---

## Part 6 — The next-generation plan

Reordered around one principle the original plan inverts: **an instrument that cannot take a reading cannot be calibrated.** Sprints 0 and 1 make VEGA able to record and manage a trade. Only then does selection quality mean anything.

### Sprint 0 — Make the instrument work (blocking; nothing else counts until this lands)

**0.1 — Fix the `true_pop` / assessment ordering.** *[unblocks the plan's #1 item]*
In `vega_candidates.main`, attach `true_pop` before assessment, or re-run `assess()` on gate survivors after `attach_true_pop`. The second is cheaper: only ~9 tickers survive, and `enrich_surface` is already structured for exactly this "analysis follows selection" pattern. **Acceptance: `edge_score` non-null on >90% of gate-passing candidates in a live scan.** Until that number moves, item 1A is inert.

**0.2 — Write `support_level_at_entry` at open time.** *[§3.3]*
It is already computed — `assess()` puts it in `analysis["shelter"]["level"]`. Pass it to `ol.open_paper_trade`. **Acceptance: Huginn's `read_support` returns a status other than `UNKNOWN` on a live open position.** One field write revives Huginn's primary signal and a quarter of Muninn's similarity function.

**0.3 — Populate `vix_at_entry`.** Read from scan `meta['vix']` (already fetched, vega_candidates.py:550) rather than `row['ctx']`. Restores Muninn's `vol_regime` fallback.

**0.4 — Write a real cohort key.** Not the plan's assertion on a nonexistent field — the field itself: `cohort = f"{fill_model}|{gate_basis}|{close_logic}"`, where `gate_basis` records whether the credit gate read mid or natural. This is the tuple that actually determines comparability, and it is what makes the pre-fix GDX trades separable from everything that follows. Backfill from `filled_at` against the known fix dates; mark backfilled rows as such.

**0.5 — Quarantine the existing ledger.** Mark all 64 closed records `analysis_eligible: false` with a reason. Not deletion — the record of the leak is itself evidence. But nothing selected under a mid-basis gate or filled at an unachievable mid may enter a base rate, a Brier score, or a Muninn stratum. **This is what makes §3.5 actionable instead of merely embarrassing.**

**0.6 — Regression test for the failure mode this whole review exposed.** A test that asserts every `REQUIRED_GATES` key is produced by a function reachable from the live scan entry point. `test_earnings_gate.py` was green for weeks while testing an unwired function; the codebase's own `AssertionError` contract catches *missing* gates but not *orphaned implementations*. Add the reachability assertion, then delete or rewire `attach_earnings_gate` so the repo stops carrying a tested lie.

### Sprint 1 — Correctness (the original plan's Sprint 1, corrected)

**1.1 — Earnings gate, done properly.** *[replaces plan 1B]*
Move the ETF/`has_earnings` awareness into the shared contract. `ctx` gains `has_earnings` (from `ticker_profile.DECLARED`, which already holds it) and `earnings_days_known: bool` distinguishing "no earnings exist" from "lookup failed":

```python
def _earnings_clear(spread: Dict, ctx: Dict) -> bool:
    if not _cfg("EARNINGS_GATE_ENABLED", True):
        return True
    if ctx.get("has_earnings") is False:
        return True                       # declared: no earnings (IBIT, SPY, TLT…)
    d = ctx.get("earnings_days")
    if d is None:
        return False                      # data gap on a name that DOES report — fail closed
    dte = spread.get("dte")
    return not (dte is not None and 0 <= d <= dte)
```

Note the field is `earnings_days`, not `earnings_date`. Ship §5A (the gap audit) **with** this, not two sprints later — you cannot know the cost of failing closed until you know which names have persistent gaps.

**1.2 — Candidate scorer.** *[plan 1A, now non-inert]* Ship the plan's weights as a starting point once 0.1 lands. Fix `gates_total` default 8 → `len(REQUIRED_GATES)` (§3.7). Log the score's component breakdown per pick so the weighting is auditable rather than asserted.

**1.3 — Collapse the duplicate rule implementations.** `_quote_spread_ok` and `_liquidity_ok` exist twice with different semantics (§3.7). Take the stricter (`vega_candidates`, both legs) into the shared contract and delete the other. This *is* plan item 3A, correctly scoped: not "add a long-leg check" but "stop having two answers to one question." Then ship the plan's 5B alignment test to keep it that way.

**1.4 — Remove the mid-basis credit check** in `_candidate_passes_minimum` (§3.5). Dead and dangerous.

**1.5 — Revive the two dead wolves.** *[§3.4]* Wire `earnings_check` into `_ravens_close_check`; the lookup already exists in `data.fundamentals`. Pair with 1.1 so earnings risk is covered at both ends. Leave `blocking_news` explicitly disabled with a config flag rather than a silently-empty dict — an unfireable wolf should be visibly off, not invisibly broken.

### Sprint 2 — Make the exit stop destroying the evidence

Grok's sharpest point, and it survives validation: the exit rule has been the dominant P&L driver and therefore the dominant confound. Median natural credit $45 against a $2.16 round trip means the 1.5× stop fires on a $22.50 move.

**2.1 — Retire the credit-multiplier stop as a primary rule.** A multiple of a *small* credit is not a risk measure; it is a measure of how little was collected. Replace with a stop expressed in the units the risk actually lives in — **percent of max loss** or **short-strike delta** — both scale-free across underlyings, which is the same defect class the plan's item 2A correctly identifies for the buffer.

**2.2 — Run the exit as an offline experiment, not a live change.** For every closed and open position, replay: hold-to-expiry, 1.5×/2×/2.5×/3× credit, 25%/50% of max loss, delta 0.40/0.50/0.55, and expected-move-based. Report risk-adjusted expectancy per rule **per cohort**. This is GPT §27, and it is the highest-information-per-dollar item in the entire packet because it needs no new trades.

**2.3 — Set `TARGET_PROFIT_PCT` from the same experiment.** 65% is as unvalidated as the stop and sits on the other side of the same distribution.

### Sprint 3 — Prove or kill the edge

**3.1 — Counterfactual ledger.** *[GPT §22; absent from the plan]* Record every candidate that was *rejected*, with its gate failures, and mark it forward exactly like an accepted one. Without this, VEGA can only ever learn whether its picks were good — never whether its **gates** were. Given §2's finding that `MIN_IV_RANK` is the only richness gate and it lives outside the contract, "does IV rank ≥ 45 actually improve expectancy?" is an open and answerable question that costs nothing but storage.

**3.2 — Value-of-information test per gate.** *[GPT §23]* For each of the 11 gates: expectancy of candidates that passed vs failed *only that gate*. Gates with no measurable contribution get demoted to ranking signals — which is GPT's §10/§11 restructure arrived at empirically rather than by assertion. Expect `support_shelter` and one of the near-redundant `pop`/`delta_cap` pair to be the first demotions.

**3.3 — Brier decomposition, not raw Brier.** *[§5.3]* Report reliability, resolution, and uncertainty separately with bootstrap CIs from the first resolution. **The bar for "the edge score works" is resolution significantly greater than zero** — not a good-looking aggregate. Resolve the 32 open claims; light up the 3 unused claim types only after the first 3 are grading.

**3.4 — Test VRP first and hardest.** *[§5.1]* It carries the largest weight and has the most adverse recent evidence. Split VRP into overnight and intraday components (§5.2) — cheap, and the overnight component maps directly onto `gap_event`, the system's own wolf #1.

**3.5 — Historical data. DEFERRED by decision, 2026-08-10.** *[§5.5]*

No paid data until the free-tier system is maxed out and paid data is the *only* remaining thing that would improve it. The standing test before this gets revisited:

1. Every field the calibration engine reads is populated on live trades (Sprint 0 — done).
2. `mark_history` has accumulated enough path to replay exit rules (Sprint 2.2b).
3. The counterfactual ledger has enough rejected candidates to run per-gate value-of-information (Sprint 3.1/3.2).
4. The prediction ledger has resolved claims and a Brier decomposition with resolution measurably above zero (Sprint 3.3).

If those four are done and a question still cannot be answered, the blocker is genuinely the data and the purchase is justified. Until then it would buy history the system is not yet instrumented to learn from — and the three stacked `edge_score` bugs found on 2026-08-10 are the argument: better data would have flowed into the same discarded field.

**What this costs:** §3.1–3.4 answer on live-forward time (months) rather than on replayed history (weeks). **What it buys:** the answers, when they arrive, are about a system whose instrumentation has been proven to work.

### Sprint 4 — Calibration (the plan's Sprint 2, mostly intact)

**4.1 — Vol-regime strike buffer** *[plan 2A]* — the plan's strongest item; keep the design, budget for plumbing `iv` into `_otm_buffer_ok`.
**4.2 — Chain quality 0.30 → 0.50** *[plan 2C]* — cheap; note it is a hard skip.
**4.3 — Per-ticker `iv_hv_inflator` from measured data** — the lever exists (§2, claim 8); this is the measurement that justifies pulling it, starting with the documented IBIT case (measured ~1.12 against a global 1.2, which zeroes its IV rank every day).
**4.4 — Re-enable `SKEW_SCORING_ENABLED`** — its stated re-enable condition is already met (§3.7).
**4.5 — `ODIN_RECOVERY_THRESHOLD` harness** *[plan 4B, corrected]* — build the grid search, record the trial count, and gate its *use* on a deflated-significance test rather than on n≥20 (§5.1, §4).

**Explicitly rejected: plan item 4A (retroactive partial Muninn snapshots).** It would build a base rate out of the contaminated population Sprint 0.5 quarantines.

### Sprint 5 — Differentiation

**5.1 — "Why VEGA likes it" / "Why VEGA might be wrong."** *[GPT §18-19]* The narrative machinery already exists (`assessment._narrate`) and already produces the right *kind* of sentence. Add the failure-mode half. No competitor ships this.
**5.2 — Model-disagreement as a first-class signal.** *[GPT §20]* Market-implied vs `true_pop` vs Muninn's historical analog. `pop_gap` is already defined for exactly this and is populated on 4 of 79 records (§3.6); Sprint 0 fixes the population, this makes it a headline.
**5.3 — Data-confidence score, surfaced.** *[GPT §25]* Chain quality ratio, IV-history sample count, and `true_pop_confidence` are all already computed and none is exposed. This is the honest counterweight to a recommendation and it is cheap.
**5.4 — Risk-tagged opportunity output.** Max risk / credit / POP / edge components per candidate, filterable by risk budget. This is the fractional-shares thesis made concrete, and it is a presentation layer over data the engine already produces.

---

## Part 7 — What changed versus the original plan

| | Original plan | This plan |
|---|---|---|
| **Sprint 1** | Wire `edge_score`; fix earnings | Both were blocked — one inert, one unimplementable. Now preceded by Sprint 0 (ordering fix, 3 unwritten fields, ledger quarantine, reachability test) |
| **Biggest risk addressed** | Selection criterion | The system cannot record or manage a trade, and has never produced a clean observation |
| **Ledger** | Treated as usable evidence | Quarantined; 0/46 natural-fill record shown to measure a since-fixed pricing leak |
| **Ravens** | Accelerate Muninn's learning | Muninn's data is corrupt; Huginn's primary signal and 2 of 5 wolves are dead — revive before accelerating |
| **Exit rule** | 1C: document it | Sprint 2: retire the credit-multiplier stop, replay all alternatives offline — it has been the dominant P&L driver |
| **Gates** | Assumed correct, tuned | Sprint 3: measured; gates with no measurable contribution demoted to ranking signals |
| **Counterfactuals** | Absent | Sprint 3.1 — the only way to learn whether the *gates* are right |
| **Brier** | Raw score | Decomposed; resolution-with-CI is the actual bar |
| **Data** | Absent | Sprint 3.5 — buy history; the "prove the edge" program is impossible on yfinance at any threshold |
| **VRP** | Assumed as the edge | Sprint 3.4 — 30-point weight with adverse 2025 evidence; test first, split overnight/intraday |

---

## Sources

- [The Decline of the Variance Risk Premium: Evidence from Traded and Synthetic Options — Dew-Becker & Giglio, Chicago Fed WP 2025-17](https://www.chicagofed.org/publications/working-papers/2025/2025-17) ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5525882))
- [The Variance Risk Premium Over Trading and Nontrading Periods — Papagelis, Journal of Futures Markets, 2025](https://onlinelibrary.wiley.com/doi/full/10.1002/fut.22589)
- [The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality — Bailey & López de Prado](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) · [The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- [Brier score decomposition — reliability, resolution, uncertainty (Stata reference)](https://www.stata.com/manuals/rbrier.pdf) · [Variance estimation for Brier Score decomposition](https://arxiv.org/pdf/1303.6182)
- [Tastytrade Credit Spreads, 11-Year Backtest — SJ Options](https://www.sjoptions.com/tastytrade-credit-spreads-do-they-work/)
- [Best Options Data APIs 2026 — FlashAlpha](https://flashalpha.com/articles/best-options-data-apis-2026) · [Options Data Pricing Compared](https://flashalpha.com/articles/options-data-pricing-comparison-flashalpha-thetadata-polygon-spotgamma-squeezemetrics)
- [Market Chameleon Review 2026](https://www.optionstrading.org/blog/market-chameleon-review/) · [7 Best Options Screeners of 2026](https://daytradingz.com/best-options-screener/) · [OptionStrat Alternatives](https://optionscout.ai/blog/optionstrat-alternatives)

---

## Part 8 — Implementation log (Sprints 0 and 1, shipped 2026-08-10)

Test suite: **652 → 661 passing**, 10.7s. Nine files modified, no new modules. Verified against a full 56-ticker live scan.

### Sprint 0 — the instrument

| # | Change | File | Verified by |
|---|---|---|---|
| 0.1 | `true_pop` attached per-candidate **before** the assessment that needs it; `attach_true_pop` made idempotent so the later sweep only fills gaps | `vega_candidates.py` | live scan shows `tPOP` on every row and `[tpop 0/3]` — the sweep now finds nothing left to do |
| 0.1b | **Second, independent cause found during verification**: `build_candidates` copied the assessment's `analysis` and `narrative` but dropped `edge_score`. Fixing either bug alone still yielded `None` | `vega_candidates.py` | `edge_score=20` on a live gate-passing GDX candidate |
| 0.1c | **Third cause**: `assess()` read `tech["vrp"]`, but `vol_context` emits `vrp_pp` — so VRP, the largest component at 30/100, was silently zero even once computed | `analysis/assessment.py` | live components show VRP flowing (GDX `vrp_pp = −2.4`) |
| 0.2 | `support_level_at_entry` written at open from `analysis.shelter.level` | `outcome_logger.py`, `auto_paper_cycle.py` | test asserts Huginn returns a status other than `UNKNOWN` once recorded |
| 0.3 | `vix_at_entry` read from scan `meta`, not the per-ticker ctx that never had it | `auto_paper_cycle.py` | — |
| 0.3b | `vrp` at open read `ctx["vrp"]`; producer emits `vrp_pp` | `auto_paper_cycle.py` | — |
| 0.4 | `mark_history` appended by `set_mark` instead of overwriting `current_mark` | `outcome_logger.py` | stores `(at, mark)` only — derived P&L stays a pure function |
| 0.5 | `cohort()`, `gate_basis()`, `analysis_eligible()` — **derived, not written**, extending the existing `close_cohort()` precedent. No ledger mutation | `outcome_logger.py` | Muninn now pools on `analysis_eligible`; 3 new tests |
| 0.6 | Reachability test: gate predicates must live in the contract's module, and the scanner must not regrow copies | `tests/test_candidate_gates.py` | fails loudly if an implementation is orphaned again |

### Sprint 1 — correctness

| # | Change | Verified by |
|---|---|---|
| 1.1 | Earnings gate fails **closed** on unknown, with `has_earnings` resolved from `ticker_profile.DECLARED` so declared no-earnings names pass without a lookup. `earnings_source` recorded so a passing gate is auditable | 12 rewritten tests now target the live implementation |
| 1.1b | **Near-miss caught by a test**: `days_until_earnings` returns `999` for unknown — "safe — won't block trade". The scan collapsed unknown to that sentinel *before* the gate saw it, which would have left the fail-closed branch unreachable. Call sites now preserve `None` | dedicated test asserts the sentinel cannot reach the gate |
| 1.2 | `_candidate_score` led by `edge_score`, `true_pop` replaces `pop_implied`, `gates_total` default 8 → `len(REQUIRED_GATES)` | — |
| 1.3 | `quote_spread` collapsed to one both-legs implementation; `leg_quote()` normalises flat and nested leg shapes | test asserts the **contract** rejects a wide long leg, not just the helper |
| 1.4 | Mid-basis credit re-check deleted from `_candidate_passes_minimum` | test asserts the floor gates natural and that a low mid alone is not a rejection |
| 1.5 | Earnings wolf revived — `earnings_check` was a hardcoded `{}`. Degrades to silence, not to a close: unknown at entry fails closed, unknown while open must not realise a loss on no evidence | 4 new ravens tests |
| — | Deleted `attach_earnings_gate`, `vega_candidates._earnings_clear`, `_quote_spread_ok`, `_leg_spread_pct` — all orphaned or duplicated | reachability test guards against their return |

### Measured impact

- **Full 56-ticker scan: 29 candidates pass all 11 gates.** The fail-closed earnings gate did not empty the board.
- **Earnings gap audit (plan item 5A, previously impossible to run): 11 declared no-earnings ETFs, 45 with known dates, 0 unknown.** Failing closed costs nothing today. Its value is on the day a calendar fetch breaks, when the gate now refuses instead of selling into a print.
- Earnings blocks fired on 5 names (NVDA, CRM, ADBE, WMT, CRWD).
- Live VRP readings are **negative** on several names (GDX −2.4pp, AAPL −14.4pp) — signal that was structurally invisible before 0.1c and which bears directly on §5.1.

### Three bugs, one shape

`edge_score` needed **three** independent fixes. `vrp` needed two. Each was a field name or an ordering that no test compared against its producer — the same shape as the `earnings_date`/`earnings_days` error in the original plan, and as the 999 sentinel caught mid-implementation. The reachability test covers orphaned *implementations*; it does not cover mismatched *field names* between a producer and its consumer. That gap is the natural next regression guard.

### Still blocked

- **2.2b exit replay** — needs `mark_history` to accumulate. 2.2a is now recording, so the clock has started.
- **3.1 counterfactuals** — pipeline buildable; 20 snapshots over 5 days is not enough to conclude.
- **3.5 historical data purchase — DECIDED: deferred.** No paid data until the free-tier system is maxed out and paid data is the only remaining improvement. See the four-point test in Sprint 3.5. This makes Sprints 2.2a, 3.1 and 3.3 the critical path: they are what turn live-forward time into answers, and they are all free.

---

## Part 9 — The field-name contract (2026-08-10, follow-up)

Part 8 closed by naming the gap the reachability test does not cover: it catches an *orphaned implementation* — a function nothing calls — but not a live call reading a key its producer never writes. `.get()` on a missing key is a successful call returning `None`, so nothing fails. Five bugs that day had exactly that shape. This closes it.

### The underlying design fault

Two producers fill the same `tech` slot on the assessment context:

| | `vega_candidates.vol_context` (fast scan / auto-open) | `data.technicals.calculate_all` (main.py engine) |
|---|---|---|
| VRP, in vol points | `vrp_pp` | `vrp` |
| IV rank | `iv_rank` | `iv_rank` |
| ATM IV | `atm_iv` (decimal) | `current_iv` (percent) |

`assess()` accepts either and reads `tech["vrp"]`. One producer matched; the other did not; the largest edge component was zero on every auto-opened trade. **Units were verified identical before unifying** — both are `(IV − RV) × 100`, which is what `calculate_edge_score`'s `>=9 / >=7 / >=5` thresholds are written against. So `vol_context` now emits `vrp`, and `vrp_pp` survives as a read-only fallback for the 20 snapshots already on disk.

### The guard — `tests/test_schema_contracts.py`

1. **Producer conformance.** `vol_context` emits exactly `VOL_CONTEXT_KEYS` on *every* return path, including the early ones. A key that appears only when the happy path completes is indistinguishable, to a consumer, from a key that was renamed.
2. **Consumer conformance.** An AST scan resolves every string-literal key read against that dict — handling both real shapes, `_ctx.get("x")` and the `(row.get("ctx") or {}).get("x")` idiom — and asserts each key is one the producer writes. Unresolvable nodes are skipped rather than guessed at, so it reports no false positives.
3. **The scanner is tested against a known-bad snippet**, so a guard that silently stopped resolving anything would fail rather than pass vacuously.
4. **The same check on the assessment context**, including an explicit test that `earnings_date` is not a field and `earnings_days` is — the exact error that would have made the original plan block every trade forever.

### It found two more bugs on its first run

- `_auto_open_from_candidates` still read `_ctx.get("vix")` as a fallback — a key `vol_context` has never emitted. Removed; `_meta_vix` is the only source.
- **`_entry_state` read `ctx["spot"]` and `ctx["price"]`. `vol_context` emits neither.** Spot was `None` on every trade, and with it `expected_move_at_entry`, which requires `atm_iv and spot and dte`. Spot lives on the candidate — `build_candidates` has recorded it since the credit floor started scaling with price. **Sixth instance of this shape, and the first caught by a test rather than a person.**

The `test_entry_state.py` fixtures had been passing `spot` inside the ctx — a shape production never produces. The tests were green because the fixture encoded the bug. Same failure as the orphaned earnings gate, one layer down.

Verified live: `expected_move_at_entry = 13.28` and `pop_gap_at_entry = −0.0398` now compute on a real candidate. `pop_gap` is described in the source as "the central claim of the whole system... the one number the ledger could never grade" — it was populated on 4 of 79 records.

**652 → 673 tests.**

### What this still does not cover

Value-domain mismatches, not name mismatches — the `999`-vs-`None` sentinel is the example, and it was caught by a hand-written test, not by this. A producer and consumer can agree on a field name and disagree about what the values mean. That is the next gap, and it is narrower than the one just closed.

---

## Part 10 — Counterfactuals (Sprint 3.1, shipped 2026-08-10)

The trade ledger can only ever say whether the *picks* were good. Eleven gates decide every entry and **not one has ever been measured against an outcome**, because a rejected candidate leaves no record of what it would have done.

### Nothing new had to be recorded

Every scan already writes `output/candidates/*.json` with each candidate's full gate results. On disk today: **2,590 candidates across 20 snapshots, 327 passing every gate, and 562 that failed exactly one gate** — the last being the only sample that can price a single gate's contribution. `analysis/counterfactuals.py` resolves them; no new write path was added to the scan, so no scan risk was taken.

### Two failures it produced on itself, both caught before they became conclusions

**1. A confident zero from an empty window.** The first real run resolved 639 spreads and reported **0% touched on every gate** — which reads as *"none of the eleven gates avoids anything."* The median spread had been observed for **two days**. A 39-DTE spread at 0.20 delta is not expected to be touched in two days; nothing had had time to happen. Worse, window length is confounded with scan date, so a gate whose blocked candidates came from the oldest scan would look worse than one whose came from today purely through exposure.

Fixed with a **fixed horizon**: every spread is judged over the same `HORIZON_DAYS` (10) trading days after its scan, and one that has not lived that long is excluded rather than counted as untouched. The honest output today:

```
  639 spreads on record · 0 have lived the full horizon · 639 still maturing
  baseline: NOT YET MEASURABLE — no qualified spread has lived the full horizon.
```

First answers arrive around **2026-08-20**, when the 08-06 cohort matures.

**2. The ledger would have destroyed its own history.** `output/candidates/` is gitignored under a comment describing it as a "regenerated artifact" directory — and it is not. A past scan cannot be re-run, so a pruned snapshot is a permanently lost observation, and the original wholesale-rewrite `build()` would have silently erased the counterfactual record the moment anyone cleaned that folder. `build()` now merges: rows whose snapshot survives are re-resolved, rows whose snapshot is gone are kept and flagged `source_snapshot_missing`, with a warning. **`logs/vega_counterfactuals.jsonl` is now the durable artifact** — ~200× smaller than the snapshots and the thing worth backing up.

### What it measures, and what it refuses to

`touched` — did the underlying trade at or through the short strike within the horizon. For a credit spread that is the event that drives a delta breach, a stop-out and a loss, and unlike expiry it answers in two weeks rather than forty days. Measured on the **low**, not the close: a spread does not care that price recovered by 4pm.

`held_at_expiry` is the cleaner measure and the slower one. It is `None` until contracts actually expire, and is never inferred from `touched`.

Per gate, it compares candidates whose **only** failure was that gate against candidates that passed everything. A gate earning its place blocks spreads that go on to be touched more often. A gate whose blocked candidates fare no worse is costing opportunity and buying nothing — it belongs in the ranking function, not the contract. Below `MIN_GATE_SAMPLE` (20) it reports `insufficient` rather than a number, the same refusal `muninn` makes.

Stated caveats travel with every report: the snapshot keeps only the top 3 candidates per ticker by natural credit-to-width, so this measures gates *within that band*; and touch is a leading indicator, not a loss.

**673 → 700 tests.**
