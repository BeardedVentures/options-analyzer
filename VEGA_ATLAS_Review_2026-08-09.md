# ATLAS / Crypto Add-On — Code-Grounded Review
## BTC specialist engine · Robinhood crypto screener · BTC→IBIT signal path
**Reviewed:** 2026-08-09 · **Source doc:** ATLAS Architecture & Build Plan, v1.0, 2026-07-13
**Method:** every claim checked against the working tree and against live chains pulled during the review.

---

## Verdict in one paragraph

The architecture is sound and the discipline in it — prove standalone, forecast don't trade,
separate schema, gated integration — is the right instinct and matches how VEGA earned its own
credibility. Three things change the plan materially. **One**, the spec is four weeks old and
predates the ravens, the prediction ledger and the shared gate contract, so roughly a third of
what it proposes building already exists and should be reused rather than duplicated. **Two**,
IBIT has never traded — not once in 207 ledger records — and the reason is arithmetic, not
signal, so ATLAS as sequenced would improve strike placement on an instrument the engine
rejects before strike placement is reached. **Three**, the Robinhood crypto API is an execution
venue and not a data source, which flips the natural build order: the spot bot is closer to the
automation north star than the forecaster is, and it can start on free data today.

---

## 1. The blocking finding: IBIT has never traded, and ATLAS does not fix why

**IBIT appears in 0 of 207 ledger records.** It has been on the watchlist since well before the
spec was written, tagged as its own `crypto` sector with a 2-position cap. It has never produced
a position.

Run live during this review, against today's chain:

```
--- IBIT spot=36.80 chain=105 cands=17 QUALIFIED=0
      min_credit_usd             blocks 17/17     ← every candidate
      credit_to_width            blocks 12/17
      liquidity                  blocks 10/17
--- COIN spot=153.60 chain=153 cands=17 QUALIFIED=1
--- SPY  spot=773.26 chain=581 cands=103 QUALIFIED=5
```

The best spread available anywhere on the IBIT board:

```
    short/long   wid   natCr$   midCr$    ctw  delta
     35.0/34.0   1.0    23.00    26.00  0.230  -0.30      ← $23 against a $25 floor
```

That candidate **passes** `credit_to_width` at 0.230 against a 0.15 floor. It is at the delta
cap. It is a perfectly reasonable spread. It is excluded by **two dollars**.

This is a scale artifact, not a judgement. `MIN_CREDIT_USD = 25` is an absolute dollar floor
written for underlyings in the $100–$800 range. IBIT trades at $36.80 with $0.50–$1.00 strike
spacing, so a 25–45 DTE put at the target delta simply cannot carry $25 of natural credit at a
width the engine accepts. The floor is doing exactly what it was built to do — it just encodes
an assumption about share price that IBIT violates.

**Why this matters for ATLAS.** The spec's stated purpose is to "justify tighter strikes and
larger size than VEGA's generic rules would ever allow." Neither of those can be expressed
through a gate that rejects on absolute dollar credit before delta, strike or size are
considered. A perfect BTC forecast changes nothing about a $23-versus-$25 comparison.

**The cheap diagnostic first.** Before any of ATLAS is built, decide whether IBIT should be
tradeable at all under the current contract. Three honest options:

1. **Scale the credit floor to the underlying.** Replace the flat `MIN_CREDIT_USD` with a floor
   expressed as a fraction of width — which `MIN_CREDIT_TO_WIDTH_PCT` already does — and keep
   the dollar floor only as a commission-viability check against
   `estimated_round_trip_cost_per_contract`. That is the intellectually consistent version: the
   real question is "does this credit survive fees", not "is it $25".
2. **Trade IBIT in multiples.** Two contracts on the 35/34 clears $46 of credit against the same
   fee base. This is a sizing decision, and it is the one that actually matches "larger size."
3. **Accept that IBIT is not a bull-put underlying** at this price and get crypto exposure
   through COIN, which qualified today, or through the spot bot in §3.

All three are hours of work with an immediate, measurable answer. None require an on-chain data
vendor. **Do this before A0.**

> One consequence worth stating plainly: if option (3) is chosen, the BTC→IBIT signal path in §4
> has no consumer, and ATLAS's justification narrows to feeding the spot bot. That is still a
> good reason to build it — but it is a different project than the one the spec describes, and
> the decision should be made deliberately rather than discovered in A5.

---

## 2. What has changed under the spec since 2026-07-13

The doc says "reuse VEGA's `technicals.py` logic where it transfers." That was the right advice
for a system that, at the time, had little else worth reusing. Considerably more exists now.

| Spec proposes building | Already exists | Recommendation |
|---|---|---|
| `atlas_forecast_outcomes` + §5 validation gate | `analysis/predictions.py` — claim ledger with resolution, hit rate, **Brier score**, confidence-bias and a plain-English verdict | **Do not build.** See §2.1 — this is the big one |
| §7 A4 "60 cycles logged, accuracy measured" | `predictions.grade()` with `PREDICTION_MIN_FOR_GRADE`, already surfaced in `vega_status.py` §4 | Reuse; ATLAS becomes another claim type |
| Outcome logger for forecasts | `analysis/outcome_logger.py`, JSONB-backed so new fields need no migration | Reuse |
| Vendor data-quality assumptions | `data/data_quality_log.py` + a per-scan usable-ratio floor (built 2026-08-08) | Reuse — see §5 on vendors |
| Position management for any bot | `analysis/huginn.py` / `muninn.py` / `odin.py` — thesis, memory, synthesis | Transfers to spot almost directly |
| A gate contract | `analysis/assessment.evaluate_gates` — one implementation, raises if it drifts from `REQUIRED_GATES` | Model ATLAS's gates on this |

### 2.1 The prediction ledger already contains ATLAS's core output

`analysis/predictions.py` defines six claim types. Five are recorded on every trade. The sixth:

```python
DIRECTION = "direction"   # the regime call was right about which way price went
```

It has a scorer:

```python
if ct == DIRECTION:
    entry_px = ctx.get("price_at_claim")
    expect = (ctx.get("expected") or "").lower()
    ...
    got = "up" if chg > 1 else ("down" if chg < -1 else "flat")
    return got == expect, f"expected {expect}, price went {got} ({chg:+.1f}%)"
```

It has tests. **Nothing has ever recorded one.** It is a fully built, fully graded, dormant
claim type whose semantics are precisely ATLAS's forecast object: a direction, a probability, a
resolution date.

The spec's entire §5 is therefore already implemented:

| §5 criterion | Where it already lives |
|---|---|
| 60 forecast cycles logged with actual move recorded | `predictions.record()` + `resolve(price_lookup)`; the caller owns data fetching, so a BTC lookup drops straight in |
| Direction accuracy above coin-flip | `grade()['by_type']['direction']['hit_rate']` |
| Magnitude/confidence calibration | `brier`, `bias_pp`, and a verdict that says *"70% correct but claiming 85% — overconfident by 15pp. The direction is useful; the certainty is not earned."* |
| Unexplained blind spots | `context` is a free-form dict per claim — stamp the regime and slice `grade()` by it |

**Recommendation.** ATLAS emits `DIRECTION` claims into the existing ledger with a cohort tag,
and its validation gate is a `grade()` call. This deletes most of A4's infrastructure and buys
something better than the saving: ATLAS is graded by *the same machinery, on the same scale,* as
VEGA's own claims. A separate `atlas_forecast_outcomes` table would make the two systems
incomparable by construction, which is the failure the cohort separation in `close_cohort()`
exists to prevent.

Two small changes are needed and both are cheap:

- `record()` keys on `trade_id`; a forecast is not a trade. Either pass a forecast id (the field
  is opaque — nothing parses it) or rename the parameter to `subject_id`. Prefer the rename.
- `grade(cohort=...)` filters on `context.close_logic`, which is trade-shaped. Generalise to a
  `context.cohort` key so `grade(cohort="atlas_v1")` works without abusing the close-logic field.

### 2.2 Where the spec is now understated rather than wrong

§3.1 says "reuse VEGA's technicals.py where it transfers." The larger transfer is the *contract
pattern*, not the indicators: one place that defines what a good setup is, that raises if it
drifts from its config list, and that both the screener and the executor call. Four enforcement
leaks in VEGA came from re-implementing the same rules in two places. A crypto system built with
two copies of its entry rules will reproduce that, and it will do so with real money attached.

---

## 3. The Robinhood crypto screener

### 3.1 The central architectural fact

The Robinhood Crypto Trading API is real, free to use, US-only, and gated behind an API key you
generate in crypto account settings. Its documented read surface is: **crypto accounts, crypto
holdings, crypto orders, crypto products, crypto quotes.**

That is an execution and account surface. There is no historical OHLCV/candles endpoint in the
documented read actions. Verify this at build time against the current docs — but design as
though it is true regardless, because:

> **Never put your signal and your execution on the same free retail API.**
> If Robinhood rate-limits, degrades, or changes an endpoint, a system that reads its signal
> from Robinhood loses its ability to *decide* and its ability to *exit* in the same instant.
> Separating them is a safety property, not tidiness.

**So:** signal from a free public market-data source (Coinbase Exchange and Binance public
endpoints both serve OHLCV without auth; Polygon may already cover crypto on the current plan —
check before adding a vendor). Execution on Robinhood. The screener never calls Robinhood for
anything except account state and order placement.

### 3.2 The constraint that shapes the whole strategy

Robinhood crypto is **spot, long-only.** No shorting, no leverage. A bot there can express
exactly two states: long, or flat.

This has a consequence the spec doesn't anticipate. ATLAS produces a directional forecast with
both halves populated — but **only the bullish half is monetizable through the spot bot.** The
bearish half is monetizable only through IBIT options, via a bear call spread. The two consumers
use the same signal asymmetrically, which is a good argument for keeping them as separate
consumers of one forecast rather than fusing them into one system.

### 3.3 What is missing from the ask, and it is the important part

"A crypto screener for possible bot trading" does not yet state **what edge the bot trades.** A
screener with no thesis is a data pipeline. The three plausible candidates, in ascending order of
how much they lean on ATLAS's fundamental layer:

1. **Trend/regime following** — pure technical, needs no on-chain vendor, testable on free data
   this week. The honest v1.
2. **Funding-rate carry / crowded-positioning fade** — needs derivatives data, and it is the
   signal in the spec most likely to be genuinely predictive, because perp funding has no equity
   analogue and is not arbitraged away by the same participants.
3. **On-chain flow-driven positioning** — the CryptoQuant/Glassnode tier, and the most expensive
   and slowest to validate.

Recommend (1) as the v1 thesis precisely because it can be falsified cheaply. If a trend model on
free data cannot beat buy-and-hold on BTC after fees, no amount of on-chain data is the reason.

### 3.4 The safety point, stated loudly

Every VEGA page footer currently ends: *"personal beta · not financial advice · **no orders are
placed**."*

Today that is a **structural** guarantee — it is true because no broker integration exists. The
moment a Robinhood key with order permissions enters this codebase, that guarantee downgrades to
a **configuration** guarantee, true only as long as a flag is set correctly. That is a different
kind of promise and it deserves engineering that matches:

- **Paper mode is the default**, and live mode requires an explicit env var that is not in any
  committed file.
- **A notional cap enforced in the order path itself**, not in the caller — the same "one
  implementation of the contract" lesson as `evaluate_gates`.
- **A kill switch** that closes to flat and refuses new orders, reachable without the cockpit.
- **A test asserting the paper path cannot reach the order endpoint.** VEGA's suite has tests
  that assert enforcement lives in one place; this needs the equivalent.
- **The API key scoped to the minimum actions needed.** Robinhood lets you select which actions a
  key enables at creation — do not enable order placement on the key used for the read-only
  screener.
- Retain the "no orders are placed" footer on every surface that is still paper, and change it
  only where it stops being true.

Kept separate, the read-only screener can ship and run for weeks under exactly today's safety
properties while the execution path is still unbuilt. That is the right seam.

---

## 4. BTC as a signal for IBIT options

This is the cheapest and highest-confidence piece of the three, and it can start on free data
immediately. Two points on how it should attach.

**It must be advisory, not gating.** The spec gets this right in §6 ("reference … not a gating
input"), and the codebase already has the exact mechanism. `strategies.py` supports:

```python
def _chk(label, ok, detail="", advisory=False):
    """A criterion row. `advisory=True` marks a row that INFORMS but never disqualifies"""
...
qualified = all(c["ok"] for c in crit if not c.get("advisory"))
```

A row added to `strategies.evaluate()` **without** `advisory=True` is a hard block that silently
deletes candidates. So the BTC signal attaches as an advisory row, and that is a one-line
distinction with a large blast radius if it is got wrong.

**The genuinely differentiated input is the IV relationship, not the direction.** BTC options IV
on Deribit and IBIT's own listed-options IV are two prices for nearly the same risk, quoted by
different participants in different venues with different hours. The spread between them is a
real, measurable, free-to-obtain quantity — and it is exactly the kind of thing VEGA's VRP
component is already built to reason about, since VRP is the largest single scoring band at 30
points. A direction forecast has to beat a coin flip to be worth anything; a cross-venue IV
spread only has to be *measurable* to be informative. Prioritise it.

That also gives the 24/7 problem a use rather than a nuisance: BTC trades through the weekend and
IBIT does not, so Monday's IBIT open has a weekend of BTC information already priced into
Deribit and not yet into IBIT. Whether that is exploitable is an empirical question — but it is a
cheap one to log a `DIRECTION` claim against every Friday and grade after ten weekends.

---

## 5. Vendor and cost review

The spec recommends starting with CryptoQuant (~$39/mo) + Farside + Deribit. **Defer the paid
tier.** The reasoning comes from what was learned on 2026-08-08:

VEGA ran for months on chains where the free data source was discarding 30–45% of records as
stale, and nothing measured it. Instrumentation added yesterday shows live ratios from 0.367
(XLE) to 0.970 (SPY) — a real, wide spread that was invisible the entire time. The lesson is not
"free data is bad." It is that **you cannot tell whether your data is the binding constraint
until you measure it**, and paying for a second source before measuring the first buys a feeling
rather than a signal.

Concretely:
- **A0 on free sources only:** Deribit (free, and the standard venue for BTC options IV), Farside
  scrape or wrapper for IBIT flows, Coinbase/Binance public endpoints for OHLCV and derivatives.
- **Instrument every one of them** with `data_quality_log.record()` from day one. It already
  takes `(ticker, source, raw_count, usable_count)` and needs no changes to serve a crypto feed.
- **Pay only when a specific free signal is demonstrably the limiting factor** on a graded claim
  type. That is a decision the prediction ledger can actually make for you, which is the whole
  point of having built it.

One correction to the vendor table's framing: Farside having no official API makes it a
*scraping dependency*, which is a fragility, not a cost saving. A scraped table that silently
changes shape produces plausible wrong numbers rather than an error. If IBIT flow becomes a
weighted input, it needs the same usable-ratio treatment as an options chain, plus a schema
assertion that fails loudly.

---

## 6. Revised sequencing

The spec's phase order is A0 data → A1 technical → A2 fundamental → A3 fusion → A4 dry run → A5
integration. That is a sound shape, but it front-loads the most expensive and slowest-to-falsify
work. Reordered so each step can kill the next one cheaply:

| # | Step | Cost | Kills what, if it fails |
|---|---|---|---|
| **0** | **IBIT tradeability diagnostic** (§1) — decide the credit-floor question | Hours | Kills the entire IBIT-options justification before a dollar is spent |
| **1** | **Free BTC data plumbing + quality instrumentation** — Deribit, Coinbase, Farside into `data_quality_log` | Days | Kills vendor choices; tells you which feeds are actually usable |
| **2** | **BTC/IBIT cross-venue IV spread as an advisory row** — measurable, not predictive; no forecast required | Days | Cheapest possible real integration; ships value with no validation gate |
| **3** | **`DIRECTION` claims into the existing ledger** — daily BTC call, graded by `grade()` | Days | This *is* A4. Runs in the background costing nothing while everything else proceeds |
| **4** | **Read-only Robinhood screener** — account/holdings/quotes, no order permissions on the key | Days | Proves the integration without changing the "no orders are placed" guarantee |
| **5** | **Technical composite + trend thesis** (spec A1) | Weeks | If a free-data trend model can't beat buy-and-hold after fees, on-chain data isn't the reason |
| **6** | **Paper execution path + safety rig** (§3.4) | Weeks | The first real risk in the system. Gate on step 3 having ≥10 resolved DIRECTION claims |
| **7** | **Fundamental layer / paid vendors** (spec A2) | $ + weeks | Only if step 5 shows technical alone is the ceiling |
| **8** | **Live crypto execution, capped notional** | — | Gate on step 6 paper record |

Steps 0–3 need no vendor spend, no broker key, and no new schema. Step 3 starts the validation
clock immediately, in parallel — which is the single highest-leverage change to the plan, because
the spec's 60-cycle gate is 3 months of wall-clock that currently doesn't start until after A3.

---

## 7. Answers to the spec's open questions

1. **Forecast cadence.** Daily, and log it as a `DIRECTION` claim with a 14-day resolution. Daily
   claims with a longer resolution window gives a 60-sample validation in 60 days rather than
   60 × 14. Noise is handled by the Brier score, not by forecasting less often.
2. **Backtestability.** Yes, and it is more valuable than the spec suggests — but note the
   ledger's own precedent: cohorts closed under different logic are not poolable
   (`close_cohort()`). A backtest is its own cohort and must be graded separately from live
   claims, never merged to reach a sample threshold faster.
3. **Vendor cost trigger.** Answered in §5: when a graded claim type is demonstrably limited by a
   free input. Not before.
4. **Naming.** ATLAS is fine and the spec is right that renaming is cheap. Note the house
   convention has two layers already — agent personas (JARVIS, VEGA, MIKE, FOX) and Norse
   internals (Huginn, Muninn, Odin, the wolf floor). A BTC forecaster feeding a decision system
   sits naturally in the Norse layer if you want the continuity.
5. **Reusable pattern or one-off.** Build it as a one-off and let the pattern emerge. The
   generalisable asset is already extracted — the prediction ledger, the gate contract, the
   ravens, the quality log are all asset-agnostic today. A second specialist would reuse those,
   not ATLAS's BTC-specific code, so there is nothing to gain from generalising in advance.

---

## 8. What I'd push back on hardest

**"Read by Josh, not auto-executed"** (§4) is stated as a virtue, and against the north star of a
fully automated trading system it is a deferral of the hard part. It is the correct posture for
the *validation* phase and the wrong one as a permanent design goal. Worth deciding now which it
is, because a forecast object designed to be read by a person and one designed to be consumed by
an executor diverge quickly — the first optimises for narrative, the second for calibrated
probabilities and explicit abstention. The current spec's object leans narrative. If automation
is the destination, `direction_confidence` and an explicit "no call" state matter more than
`primary_catalysts` prose.

**The sequencing risk.** VEGA has 5 closed trades under the current close logic against a 30-trade
milestone, and the whole thrust of the last two weeks was to stop trusting the track record and
fix the measurement underneath it. Starting a second system that makes a *harder* claim
(magnitude forecasting) before the first has an honest record is the scope expansion the
three-model brief specifically warned against. That is a sequencing observation, not an argument
against the project — and steps 0–3 above are chosen so that most of ATLAS's clock can run in
parallel without competing for the attention VEGA's 30-trade cohort still needs.

---

*Reviewed against the working tree at 536 passing tests. Live chain reads taken 2026-08-09.*
*VEGA / BeardedVentures · personal beta · not financial advice · no orders are placed*
