# VEGA — Third-Party Review Brief
## Everything an outside reviewer needs to critique this system
**Prepared:** 2026-08-09 (Sunday, market closed) · **For:** independent AI/engineer review
**Repo:** `options-analyzer` · **Suite:** 652 tests passing · **Python 3.14, Windows**

---

## 0. How to use this document

You are being asked for an adversarial, independent review. The author of this system has a
strong prior that **the system is probably wrong in ways it cannot see**, and the most valuable
thing you can do is find those ways rather than validate the ones already known.

**Please prioritise, in this order:**

1. **Falsify the core thesis** (§2). Is this strategy sound at this account size at all?
2. **Attack the gate contract** (§5). Which gates are theatre? Which are missing?
3. **Attack the close logic** (§7). The current close logic has stopped out 5 of 5 trades.
4. **Find the next instance of the recurring defect class** (§10). Three have been found; there
   are almost certainly more.
5. **Tell us what to stop building.** Scope discipline is a bigger risk than any single bug.

**What is NOT useful:** restating what §11 already lists as known. Praise. Suggestions to add
indicators or data vendors (see §12 on why).

**Ground truth caveat:** every "live" number in this document was read on a Sunday, so option
quotes are Friday's closes. Treat all chain-derived figures as end-of-Friday marks.

---

## 1. What this system is

A **bull-put-spread screener and paper-trading system** for a single retail operator (Josh).
It scans ~56 US equity/ETF tickers, identifies credit spreads that meet a hard rule set, opens
them as **paper** positions, manages them to a close, and — the newest and most important part —
**grades its own predictions over time**.

It has never placed a real order. No broker is connected. That guarantee is currently
**structural** (no integration exists), not configurational.

**Strategy:** short put vertical (bull put credit spread). Sell a put at ~0.20 delta, buy a
further-OTM put for defined risk, collect the credit, profit if the underlying stays above the
short strike. This is a **short-volatility, high-win-rate, negative-skew** strategy: many small
wins, occasional large losses.

**Account context:** `ACCOUNT_BALANCE = 500.0`, `MAX_RISK_PER_TRADE_PCT = 0.2`. This is a
$500 paper account. **This matters enormously and is under-examined — see §2.3.**

---

## 2. The track record — read this before anything else

This is the honest, un-spun state as of 2026-08-09.

```
TOTAL 207 ledger records | 64 closed | 11 open

COHORT                      n    win%      net $   stop-outs
credit_stop_1.5x_natural   59   22.0%    -3,277    45/59
ravens_v1                   5    0.0%      -384     5/5

Open positions: 11, unrealized net -$487
Realized total: -$3,661
```

**Exit reasons across all 64 closes:**
```
auto-stop-loss        45
auto-target-profit    12
wolf-stop              5
trust-loop-check       1
manual-earnings-risk   1
```

### 2.1 What the two cohorts mean

Cohorts are **not poolable**. The system enforces this in `outcome_logger.close_cohort()`.

- **`credit_stop_1.5x_natural` (n=59)** — legacy. Closed by a 1.5× credit stop, marked
  natural-in/natural-out. This stop fired at t=0 on bid-ask spread alone on many trades: the
  natural exit debit immediately exceeded 1.5× the natural entry credit before the underlying
  moved at all. **These 59 trades largely measure the exit rule, not the selection model.**
- **`ravens_v1` (n=5)** — current. Closed by the thesis framework in §7. **0 wins, 5 stop-outs.**

### 2.2 The honest reading

A 22% win rate on a strategy designed for ~78% win rate (POP floor is 0.72) is not a bad run —
it is evidence something is structurally wrong. The leading hypothesis has been that the *exit
rule*, not the *selection*, produced it. That hypothesis is **untested**, because the
replacement exit rule has 5 observations and has lost on all 5.

**Open question for the reviewer:** is 5/5 stop-outs on `ravens_v1` (a) noise at n=5,
(b) evidence the new stop is also too tight, or (c) evidence the selection model is the problem
and the exit was never the issue? What would distinguish these?

### 2.3 The account-size question nobody has answered

At `ACCOUNT_BALANCE = $500`:
- Round-trip commission is **$2.16/contract** (4 legs × $0.54).
- The credit floor is **$15–25/contract**.
- So fees are **9–14% of gross credit** before the underlying moves at all.
- A $5-wide spread at the 0.15 credit floor collects $75 and risks **$425 — 85% of the account
  on a single position.** The system permits `MAX_TRADES_PER_SECTOR = 2` and currently holds 11
  open positions, so aggregate risk far exceeds the notional account.

**Reviewer question:** Is a defined-risk credit spread strategy viable at all at $500 with these
commissions? If not, what is the minimum account size at which the math works, and should the
system say so out loud instead of generating signals it cannot economically trade?

### 2.4 Position sizing is not enforced at all — found while writing this brief

`config.py` defines:
```python
ACCOUNT_BALANCE        = 500.00
MAX_RISK_PER_TRADE_PCT = 0.20                       # -> $100 per trade
MAX_RISK_PER_TRADE_USD = ACCOUNT_BALANCE * 0.20
```
**`MAX_RISK_PER_TRADE_USD` is never read by any enforcement code.** `config.py:302` states
outright: *"The scanner no longer gates on ACCOUNT_BALANCE."* The only position limit anywhere
is a **count**: `VEGA_MAX_OPEN_TOTAL = 15`.

The current open book:
```
QCOM $360  NKE $212  CVX $413  KO  $214  META $415  MSFT $430
PSX  $460  AMGN $435 SMH $405  NEE $214  COP  $416

AGGREGATE MAX LOSS : $3,974
ACCOUNT_BALANCE    : $500
                     7.9x the account
```
Every one of the 11 positions individually exceeds the $100 per-trade cap by 2.1–4.6×. At the
15-position ceiling the book could carry roughly **$6,000 of defined risk — 12× the account.**

**This reframes §2.2.** The −$3,661 realised loss is not a drawdown a $500 account could have
sustained; that account would have been wiped out several times over. **The track record is not
achievable at the stated account size**, which means it cannot be read as "what would have
happened to Josh's money" — it is closer to an unsized signal backtest.

**Reviewer questions:** Is this the single most important defect in the system? Should
`MAX_RISK_PER_TRADE_USD` become a hard gate, should contract sizing be computed from it, or
should `ACCOUNT_BALANCE` simply be removed as a misleading fiction until sizing is real?

---

## 3. Architecture map

```
ENTRY POINTS
  vega_app.py        3279  Cockpit web UI (localhost:8765), 7 views, own scheduler
  main.py            1537  "Engine" scan — full edge scoring, writes scan_latest.json
  vega_candidates.py  679  "Fast scan" — enumerate spreads, gate them, write snapshot JSON
  auto_paper_cycle.py 940  THE TRADER. Scheduled every 2h in session. Opens/marks/closes.
  vega_status.py      343  Read-only terminal status board (6 sections)

SHARED CONTRACT
  analysis/assessment.py   461  evaluate_gates() — THE gate contract. assess() — analysis+narrative
  config.py               1013  Every threshold, heavily commented with rationale

SCORING
  analysis/edge_calculator.py  764  0-100 edge score + true POP (drift-removed)
  data/technicals.py           751  Indicators, ATM IV, IV rank, realised vol
  analysis/vol_surface.py      248  Term structure, skew
  analysis/horizon.py          239  Expected move, sigma distances
  analysis/levels.py           205  Support/resistance detection
  analysis/structure.py        568  Chart pattern detection
  analysis/entry_timing.py     546  Timing readiness (advisory)

POSITION MANAGEMENT — "the ravens" (Norse: Odin's two ravens, Thought and Memory)
  analysis/huginn.py    421  THOUGHT. Is the thesis still intact? Hard "wolf" floors.
  analysis/muninn.py    243  MEMORY. Have comparable stressed positions recovered before?
  analysis/odin.py       80  SYNTHESIS. Combines both into a recommendation.

LEARNING LOOP
  analysis/predictions.py        413  Falsifiable claim ledger + Brier scoring
  analysis/calibration_engine.py 256  Correlates entry beliefs against outcomes
  analysis/outcome_logger.py     467  THE LEDGER (JSONL). Every trade, every field.

DATA
  data/fetcher.py          1160  yfinance (Polygon key is UNSET — see §9)
  data/crypto.py            243  Deribit DVOL + Coinbase (free, unauthenticated)
  data/data_quality_log.py  ~150 Per-ticker per-scan chain usability
  data/news.py              350  News sentiment

TICKER SPECIFICITY (new, 2026-08-09)
  analysis/ticker_profile.py 254  Declared + learned per-asset character
  analysis/btc_signal.py     ~140 BTC DVOL vs IBIT IV cross-venue read
  analysis/btc_forecast.py   197  Daily BTC directional claim
```

**652 tests** across 27 files. Test philosophy: each test docstring states *the bug it prevents*,
usually with the live incident that motivated it.

---

## 4. The two-engine problem (important context)

There are **two scan paths** and they diverged historically:

| | `main.py` (engine) | `vega_candidates.py` (fast scan) |
|---|---|---|
| Full edge score | yes | no |
| true_pop | computed | attached separately via `attach_true_pop()` |
| Feeds the trader | no | **YES** — `auto_paper_cycle` reads its snapshot |
| Gate implementation | `assessment.evaluate_gates` | `assessment.evaluate_gates` |

**Four separate enforcement leaks** were caused by rules existing in one path and not the other
(IV rank 2026-07-25, POP floor 2026-08-02, quote spread 2026-08-02, mid-vs-natural credit
2026-08-07). The fix was to make `analysis/assessment.evaluate_gates()` the single
implementation, which now **raises `AssertionError`** if it fails to emit any key in
`config.REQUIRED_GATES`.

**Reviewer question:** is one shared function sufficient, or does having two scan entry points at
all guarantee future divergence? Should `main.py` be deleted?

---

## 5. The gate contract — complete

`config.REQUIRED_GATES` (all must pass; `qualified = all(gates.values())`):

| Gate | Rule | Value | Comment |
|---|---|---|---|
| `delta_cap` | \|short delta\| ≤ max | **0.30** | target is 0.20 |
| `otm_buffer` | spot→strike distance | **5%** (stock), **$10** (SPY) | |
| `credit_to_width` | natural credit / width | **≥ 0.15** | |
| `min_credit_usd` | natural credit $ | **$25**, scaled ↓ to **$15** floor below $100 spot | see §10.1 |
| `liquidity` | volume OR open interest | **≥100 vol** or **≥500 OI** | short leg only |
| `pop` | probability of profit | **≥ 0.72** | |
| `dte_window` | days to expiry | **25–45** | |
| `quote_spread` | (ask−bid)/mid | **≤ 0.35** | short leg only |
| `natural_credit_positive` | natural credit > 0 | **> 0** | ~25% of candidates fail this |
| `earnings_clear` | no earnings ≤ expiry | binary | **fails closed** |
| `support_shelter` | a defended level above the strike | — | **fails OPEN** by design |

**Enforced separately in `auto_paper_cycle._pick_new_trades` (NOT in the gates dict):**

| Check | Value | Note |
|---|---|---|
| `iv_rank_below_floor` | `MIN_IV_RANK = 45` | **This is the richness gate.** |
| `iv_rank_unknown` | — | **fails closed** as of 2026-08-09 |
| `already_open_ticker` | — | one position per ticker |
| `credit_usd` re-check | same scaled floor | independent re-enforcement |
| `pop` re-check | 0.72 | prefers `true_pop`, falls back to `pop_implied` |

### 5.1 Critical observation about the gates

**Every gate in `REQUIRED_GATES` tests STRUCTURE, not RICHNESS.** They answer "is this spread
well-formed?" Only `MIN_IV_RANK` — which lives outside the contract — asks "is the premium
actually rich right now?"

**Reviewer questions:**
- Should richness be *in* the contract rather than bolted onto the trader?
- `support_shelter` fails OPEN when level data is missing. Is that defensible, given
  `earnings_clear` fails closed? What is the principle for choosing?
- `liquidity` and `quote_spread` check the **short leg only**. The long leg is unchecked. Is
  that a real hole? (The long leg is bought, so a wide spread there costs real money.)
- `MIN_PROBABILITY_OF_PROFIT = 0.72` combined with `SHORT_STRIKE_MAX_DELTA = 0.30` — are these
  redundant? (1 − 0.30 = 0.70, so delta cap nearly implies the POP floor.)

---

## 6. The scoring model

`analysis/edge_calculator.calculate_edge_score()` → 0–100 (+ bonuses), `MIN_EDGE_SCORE = 60`.

| Component | Max points | Basis |
|---|---|---|
| **VRP** (implied − realised vol) | **30** | largest single component |
| True POP edge | 25 | `true_pop − implied_pop` |
| Technical score | 20 | composite of indicators |
| Fundamentals | 10 | |
| News sentiment | 10 | |
| Earnings safety | 5 | uses `EARNINGS_BLACKOUT_DAYS = 7` |
| IV skew (additive) | 0–15 | **DISABLED** (`SKEW_SCORING_ENABLED = False`) |
| Term structure (additive) | −8 to +5 | |

VRP bands: `≥8pp → 30`, `6–8 → 26`, `4–6 → 22`, `2–4 → 17`, `0–2 → 8–12`, `<0 → 0`.

**Reviewer questions:**
- Is VRP at 30/100 correctly weighted for a short-vol strategy, or should it dominate more?
- `true_pop_edge` at 25 points assumes the drift-removed POP model is good. It has **never been
  validated** — see §8.
- Skew scoring is off pending data quality. Is that the right call or an over-correction?

---

## 7. The close logic — "the ravens"

This is the highest-risk subsystem and has the worst record (0/5).

### 7.1 Structure

```
HUGINN (thought)  -> thesis_status in {WOLF, INTACT, EXCEEDED, UNDER_PRESSURE, VIOLATED}
MUNINN (memory)   -> recovery_probability from comparable historical stressed positions
ODIN  (synthesis) -> recommendation in {WOLF_CLOSE, CLOSE, HOLD, HOLD_TENSION, MUNINN_BLIND}
```

**Odin's decision matrix:**
- `WOLF` → `WOLF_CLOSE` (nothing overrides)
- `INTACT`/`EXCEEDED` → `HOLD`
- `VIOLATED` + memory insufficient → `MUNINN_BLIND` (human decision)
- `VIOLATED` + memory agrees (recovery < 0.35) → `CLOSE`
- `VIOLATED` + memory disagrees → `HOLD_TENSION` (**the ravens disagree — surface it, don't act**)
- `UNDER_PRESSURE` + memory insufficient → `MUNINN_BLIND`
- `UNDER_PRESSURE` + memory sufficient → `HOLD`

Only `WOLF_CLOSE` and `CLOSE` actually close. `HOLD_TENSION` and `MUNINN_BLIND` persist to the
trade record and surface in the cockpit as alert cards.

### 7.2 The hard floors ("wolves")

Fire without synthesis, nothing overrides:
- **Gap event** — gapped through the short strike by > 1.5 ATR overnight
- **Delta breach** — short delta ≥ **0.55** (market now prices the strike ITM-likely)
- **Earnings in window**
- **Blocking news**
- **Hard floor loss** — mark ≥ **3.0× credit** (`WOLF_STOP_MULTIPLIER`)

Also unconditional: **DTE ≤ 7 closes** regardless of thesis. Profit target closes at
`TARGET_PROFIT_PCT = 0.65` of max profit.

`CLOSE_DECISION_MARK_BASIS = "mid"` — the close decision reads the MID mark, while entry books
the NATURAL credit.

### 7.3 Why this is the highest-risk area

- **5 of 5 ravens trades stopped out.** All 5 hit `wolf-stop`.
- Muninn is **structurally blind**: it needs historical stress snapshots to compute a recovery
  rate, and those only started being written recently. It reports `sufficient: False` and
  declines to give a number. So Odin is effectively running on Huginn alone.
- On a spread at the 0.15 credit-to-width floor, max loss is **5.67× credit**. The wolf fires at
  a mark of 3× credit, i.e. a realised loss of 2× credit — **35% of max loss**. So the position
  is cut roughly a third of the way into its defined risk, and the remaining 65% of the risk
  that was underwritten is never actually used.

**Reviewer questions:**
- Is a 3× credit hard stop correct for a 0.15 credit-to-width spread? Prior analysis in this
  repo concluded stop distance (~3.5× credit) was "the real killer", not entry selection. Does
  3× repeat that error?
- Entry books NATURAL credit but the close decision reads MID. Is this inconsistency a bug?
  It was a deliberate choice — natural-in/natural-out fired stops at t=0 on spread alone.
- Muninn cannot function until stress snapshots accumulate. Should the ravens be **disabled**
  until then, falling back to a simple rule, rather than running a 3-tier framework where 2
  tiers are inoperative?
- `ODIN_RECOVERY_THRESHOLD = 0.35` — arbitrary. Never validated.

---

## 8. The learning loop

The system's distinguishing feature and its main claim to eventual credibility.

### 8.1 Prediction ledger (`analysis/predictions.py`)

Every trade records **falsifiable claims** with a probability and a resolution date. Scored by
**Brier score** (mean squared error of probability vs outcome), not just accuracy — because a
predictor saying 95% and being right 60% of the time is more dangerous than one saying 60%.

Claim types: `strike_holds`, `strike_untouched`, `level_holds`, `timing_improves`,
`event_realised`, `direction`.

**Current state:**
```
total 23 claims | open 23 | resolved 0 | unresolvable 0
by type: strike_holds 11, strike_untouched 11, direction 1
PREDICTION_MIN_FOR_GRADE = 10 (per type, before a verdict is issued)
```

**Nothing has resolved yet.** First grades in ~30 days.

### 8.2 What is recorded at entry (for later correlation)

`edge_score`, `vrp`, `technical_score`, `term_slope`, `skew_steepness`, `vix_at_entry`,
`true_pop`, `implied_pop`, `iv_rank`, plus (added 2026-08-08/09) `atm_iv_at_entry`,
`rv_at_entry`, `expected_move_at_entry`, `pop_gap_at_entry`, `btc_iv_gap_pp`, `btc_vrp_pp`.

`pop_gap_at_entry = true_pop − implied_pop` is **the model's core edge claim** and was not
recorded at all until 2026-08-08.

### 8.3 Milestone gates (self-imposed)

| Gate | Requirement | Status |
|---|---|---|
| First honest close-logic read | 30 closed ravens_v1 trades | **5/30** |
| Calibration per claim type | 10 resolved of that type | **0/10** |
| IV/RV refinement | 20 closed with score components | not met |
| Portfolio Greeks | real funded account | not met |

**Reviewer questions:**
- Is 30 trades enough to conclude anything about a strategy with ~75% expected win rate? (Naive
  binomial says no — the confidence interval at n=30 is enormous.) What n is actually needed?
- Brier scoring on `strike_holds` where the claim probability is ~0.78 and the base rate is
  ~0.78 — does this measure anything, or is it guaranteed to look well-calibrated?

---

## 9. The data layer — and why it is the biggest liability

**`POLYGON_API_KEY` is UNSET.** Everything comes from **yfinance**, free and unauthenticated.

### 9.1 Chain quality (instrumented 2026-08-08)

A per-ticker per-scan usable ratio is logged. Live sample:
```
SPY 0.970 | QQQ 0.920 | NEE 0.638 | GDX 0.589 | XLE 0.367
Floor: CHAIN_QUALITY_MIN_RATIO = 0.30 (below this the ticker is skipped entirely)
```
Typical yfinance quality-filter rejections observed in one scan: **31–67%** of records discarded
as stale/unquoted (AMGN 67%, SMH 44%, KO 47%).

### 9.2 The IV history corruption (found 2026-08-09)

`iv_history/*.json` feeds **IV RANK**, the only richness signal. It was written by **two
different definitions of ATM IV**:
- `vega_candidates.vol_context` — IV of the *single contract nearest spot*
- `technicals.estimate_atm_iv` — *median* of contracts within 3% of spot

The single-contract version ran during market hours. Result:
```
SPY stored ATM IV: weekdays 34-68%, weekends 12-14%
101 of 1026 stored observations (10%) exceed 3x that ticker's own realised vol
  AMD 507% vs 83% realised | AAPL 255% vs 36% | IWM 188% vs 14%
```
Errors are always **high**, which biases IV percentile **down**, so `MIN_IV_RANK` rejected
setups that deserved to pass. Now unified, and bad observations are filtered at read time.

### 9.3 IV readiness right now

```
23 of 58 tickers have >= 20 clean IV observations
Thinnest: KRE(1), OXY(1), XBI(1), CRWD(2), SMH(2), TLT(2), COIN(3), IBIT(3)
IV_HISTORY_MIN_SAMPLES = 30 for a real percentile; below that a HV-based APPROX is used
```

**Reviewer questions:**
- Can any conclusion be drawn from a system whose primary data source discards 30–67% of the
  chain? Should it stop trading until data quality is fixed?
- The HV-based IV-rank approximation uses `IV_HV_INFLATOR = 1.2` — see §10.3.
- Is `CHAIN_QUALITY_MIN_RATIO = 0.30` far too permissive? A chain that is 31% quotable passes.

---

## 10. The recurring defect class — "textbook constant applied to a specific asset"

**Three instances found. This is the most productive bug pattern in the system's history and
there are probably more.** Finding a fourth is the single highest-value thing a reviewer can do.

### 10.1 `MIN_CREDIT_USD = 25`
A flat dollar floor is **not price-neutral**. $25 is 0.03% of spot on SPY ($773) and 0.68% on
IBIT ($36.80) — the same rule, 20× stricter, purely because of share price. IBIT appeared in
**0 of 207 ledger records** for its entire life. Fixed 2026-08-09: floor scales with price,
capped at $25, floored at $15.

### 10.2 `estimate_atm_iv` 3%-of-spot window
138 contracts on SPY, 10 on IBIT. When it caught nothing it fell back to the **whole-chain
median** — a smile-weighted number running ~7 vol points high. Fixed: widens in steps
(3→5→8→12%), returns 0.0 rather than a wrong number.

### 10.3 `IV_HV_INFLATOR = 1.2` — **NOT FIXED, live today**
One global constant assumed for all 56 names. Measured for IBIT:
```
IBIT ATM IV 32.72%, realised 29.2%  -> genuinely positive VRP (+3.5pp)
Inflated HV distribution MINIMUM: 33.8%
-> IV rank 0.0 -> rejected by MIN_IV_RANK=45 EVERY DAY, permanently
IBIT structural IV/HV ~= 1.12; the constant assumes 1.20
```
**The name is not cheap; the ruler is wrong.** A per-ticker override mechanism now exists
(`ticker_profile.DECLARED['iv_hv_inflator']`) but **every ticker still resolves to the global
default** — the lever ships, deliberately unpulled, because setting it is a strategy decision.

**Reviewer question: where is the fourth instance?** Candidates to examine:
`MIN_STRIKE_BUFFER_STOCK = 5%` (same 5% for a 12-vol utility and an 80-vol semi?),
`MIN_OPTION_VOLUME/OI` (absolute counts across wildly different chain depths),
`MAX_QUOTE_SPREAD_PCT = 0.35`, `VRP_MIN_THRESHOLD`, the DTE window, `WOLF_DELTA_THRESHOLD`.

### 10.4 The generalised fix: `analysis/ticker_profile.py`

Separates **DECLARED** knowledge (facts about the world: IBIT holds spot BTC, so no earnings, no
business risk, and its IV belongs against Deribit's DVOL; COIN is an operating company, *not* a
BTC tracker) from **LEARNED** knowledge (its own IV distribution, realised vol, chain
readability, with sample size attached). They fail differently — a declared fact is wrong when
the world changes; a learned fact is wrong when the sample is thin.

Its output is **cautions**, which reach the trade narrative:
> *"IV rank is unreliable here — 3 of 20 observations."*
> *"No earnings, so the earnings gate can never bind — it is not evidence of safety on this
> name, just an inapplicable rule."*

**Advisory only.** Never enters the gates dict.

---

## 11. Known defects and open items (do not re-report these)

1. **`IV_HV_INFLATOR` is global** (§10.3) — IBIT permanently rejected. Lever exists, unpulled.
2. **Muninn is blind** — needs stress snapshots that only recently started accruing.
3. **`ravens_v1` is 0/5.** Too few to conclude, too few to ignore.
4. **Nothing in the prediction ledger has resolved.** 23 open claims, first grades ~30 days out.
5. **Skew scoring disabled** pending data quality.
6. **`POLYGON_API_KEY` unset** — yfinance only, 30–67% chain discard.
7. **23/58 tickers** have enough clean IV history to rank.
8. **`CLOSE_DECISION_MARK_BASIS = "mid"` vs natural entry** — deliberate asymmetry.
9. **`support_shelter` fails open**, `earnings_clear` fails closed — inconsistent philosophy.
10. **Long leg is unchecked** for liquidity and quote spread.
11. **Two scan entry points** (`main.py` and `vega_candidates.py`) still exist.
12. **$500 account vs $2.16 round-trip** economics never formally assessed (§2.3).
13. **Position sizing is unenforced** (§2.4). `MAX_RISK_PER_TRADE_USD` is defined and never
    read. Open book carries $3,974 of defined risk against a $500 account — 7.9x. Found while
    compiling this brief, not previously known.

---

## 12. Engineering principles in force (why changes look the way they do)

These are the working discipline. Critique them too.

1. **One definition of a rule.** Four enforcement leaks came from the same rule implemented
   twice. `evaluate_gates()` raises if it drifts from `REQUIRED_GATES`.
2. **A metric must be able to be wrong.** Chain quality was nearly implemented as
   `len(after_filter)/len(before_filter)`, which is pinned at 1.000 on the unfiltered Polygon
   path — arithmetically incapable of reporting a problem on the primary source.
3. **Absence ≠ neutral.** `None` means "no claim made"; `0.0` means "claimed no edge". The
   calibration engine must distinguish them.
4. **Never fabricate a base rate.** Muninn reports `sufficient: False` rather than inventing a
   recovery probability from 3 samples. "A fabricated base rate would launder a guess into a
   number that looks like evidence."
5. **Store the raw measurement, never the label.** Thresholds are provisional; the number is
   permanent.
6. **Advisory by construction, not by flag.** A signal that never enters the gates dict cannot
   block a trade regardless of what it says.
7. **Never rewrite a ledger in place.** A dedup script once reverted a day of closes while the
   line count still looked right. Filtering happens at read time.
8. **Cohorts are not poolable.** Trades closed under different logic cannot be averaged.
9. **Do not add indicators.** The system's problem has never been too few signals.
10. **Tests document the incident.** Each docstring names the live bug it prevents.

**Reviewer question:** which of these is actually counter-productive? Principle 9 in particular
may be over-applied — is the system now under-powered rather than over-fit?

---

## 13. Operational state (as of 2026-08-09, ready for 2026-08-10 open)

```
Scheduler : VEGA_AutoPaper_2Weeks — DAILY trigger 09:35, repeat 2h for 7h, no end boundary
            NextRun 2026-08-10 09:35. (Was a one-off repeating "for P14D" expiring 08-13 —
            it would have died silently mid-week.)
Cockpit   : vega_app.py on 127.0.0.1:8765, 7 views (Today/Brief/Track/Open/Bitcoin/History/Lottery)
Ledger    : logs/vega_outcomes.jsonl, 207 records
Predictions: logs/vega_predictions.jsonl, 23 open claims
Cost      : $0/month. All data free/unauthenticated. Claude is the only paid input.
```

**Last dress rehearsal (throwaway ledger, Friday's marks):**
`opened=0, marked=11, closed=0` — rejections `{iv_rank_below_floor: 3, already_open_ticker: 1}`.

All four tickers scanned were below `MIN_IV_RANK = 45`: IBIT 0.0, COIN 10.0, SPY 16.0, NEE 44.0.
With VIX at 14.9 and SPY IV 12.7% vs 13.4% realised, **declining to sell is probably correct** —
it is a calm tape and premium is cheap. But IBIT's 0.0 is the §10.3 artifact, not a judgement.

---

## 14. The Bitcoin extension (newest, least proven)

Added 2026-08-09 on free data only. Deliberately advisory.

- **`data/crypto.py`** — Deribit `DVOL` (BTC 30-day implied vol, published as an index) and spot
  index; Coinbase daily candles. Live: DVOL 34.4%, realised 28.0%, **BTC VRP +6.4pp**.
- **`analysis/btc_signal.py`** — cross-venue gap: BTC's own options market vs IBIT's. Live:
  IBIT 32.72% vs DVOL 34.4% = **+1.7pp, "aligned"**. **IBIT only** — COIN's gap to BTC was 31
  vol points, which measures the company, not a mispricing of Bitcoin.
- **`analysis/btc_forecast.py`** — one daily `DIRECTION` claim into the **existing** prediction
  ledger (cohort `btc_forecast_v1`), not a separate table, so BTC grades on the same scale as
  everything else. Model is deliberately tiny (20/50 SMA + spot vs 50d, vol regime as a
  *confidence* modifier never a direction). Confidence capped at **0.62**.
- **Robinhood is deliberately NOT wired as a data source.** Its documented read surface is
  accounts/holdings/orders/products/quotes — it is an execution venue. Signal and execution on
  one free retail API means a rate-limit costs the ability to decide and the ability to exit
  simultaneously.

**Reviewer questions:**
- Is a 20/50 SMA crossover worth logging at all, or does it pollute the ledger with a known-weak
  claim type that will drag the aggregate Brier score?
- Cross-venue IV gap: is DVOL (30-day constant maturity) even comparable to IBIT's ~40 DTE ATM
  IV? **This may be an apples-to-oranges comparison and nobody has checked the maturity match.**

---

## 15. Specific questions we most want answered

1. **Is unenforced position sizing (§2.4) the most important defect here?** The open book
   carries 7.9x the account in defined risk and the per-trade cap is dead code. Everything else
   in this document may be a rounding error next to it.
2. **Is this strategy viable at $500 with $2.16 round-trips?** If not, say so plainly.
3. **Is the 3× credit wolf stop repeating the mistake the 1.5× stop made?** It cuts at 35% of
   max loss on a 0.15 credit/width spread, leaving two-thirds of the underwritten risk unused.
4. **Where is the fourth "textbook constant" defect?** (§10)
5. **Should the ravens be disabled** until Muninn has data, given 2 of its 3 tiers are inert?
6. **Is `MIN_IV_RANK = 45` on an HV-approximation defensible**, or is the system gating on noise?
7. **What n is actually required** to conclude anything about a ~75%-win-rate strategy?
8. **Is the DVOL-vs-IBIT-IV comparison maturity-matched?** (§14)
9. **What should we stop building?** Name the subsystem with the worst value-to-complexity ratio.
10. **Does the prediction ledger measure anything real**, or do claims like `strike_holds` at
   p≈0.78 against a base rate of ≈0.78 guarantee a flattering Brier score?
11. **Is there a fundamental reason a retail short-vol system cannot work**, independent of
    implementation quality?

---

## Appendix A — Reproducing the state

```bash
cd options_intelligence
python -m pytest -q                    # 652 tests
python vega_status.py                  # 6-section operator view
python vega_candidates.py --tickers SPY,IBIT --no-open
python vega_app.py                     # cockpit on 127.0.0.1:8765
```

## Appendix B — Key files to read first

For a reviewer with limited budget, in priority order:
1. `analysis/assessment.py` — the gate contract (461 lines)
2. `config.py` — every threshold with its rationale (1013 lines, heavily commented)
3. `analysis/huginn.py` + `odin.py` — the close logic that has lost 5/5
4. `analysis/predictions.py` — the learning loop
5. `auto_paper_cycle.py` — the actual trader
6. `analysis/ticker_profile.py` — the newest idea, least tested

---

*VEGA / BeardedVentures · personal beta · not financial advice · no orders are placed*
*This brief was compiled from live system state on 2026-08-09; all figures are reproducible
via Appendix A.*
