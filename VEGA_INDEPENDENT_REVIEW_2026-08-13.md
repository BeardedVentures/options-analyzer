# VEGA — Independent Review Brief
**2026-08-13 · BeardedVentures · prepared for third-party technical and quantitative review**

---

## 0. How to read this document, and how to attack it

This brief exists to be **falsified**, not agreed with. Every prior handoff document in this
repository has been wrong on facts that a `grep` would have settled, so the standing rule here
is: **treat every claim below as a hypothesis and check it.** Where a claim is verifiable, the
file and symbol are named. Where it is not, it is marked `UNVERIFIED`.

Reviewers are specifically asked to attack, in priority order:

1. **The fill model.** Everything else is downstream of whether a quoted credit is achievable.
2. **The ledger's ability to grade anything.** See §6 — we believe it currently cannot.
3. **The single global constants** (`VOL_REVERSION_PHI`, `IV_HV_INFLATOR`) fitted across all names.
4. **Whether the gate set is complete** — §4.3 documents a known hole nobody has closed.

**Verified as of this document (2026-08-13, on `main`):**

| Fact | Value |
|---|---|
| Test suite | **986 passing**, 0 failing |
| Production code | 56 files, 25,193 lines |
| Test code | 41 files, 9,058 lines |
| Watchlist | 56 tickers |
| Hard gates | 11 (`config.REQUIRED_GATES`) |
| Ledger | 231 rows — 152 modeled, 14 open, 65 closed |
| Closed outcomes | **14 wins / 51 losses (21.5%)** |
| Account basis | `ACCOUNT_BALANCE = 500.0` |

---

## 1. What VEGA is

A single-operator, paper-only options **premium-selling** engine. It scans a 56-name watchlist
for defined-risk credit spreads (bull put, bear call, iron condor), grades them, and presents
them in a local web cockpit. A scheduled cycle can open and close paper positions
autonomously.

**It places no orders.** No broker integration exists. That guarantee is currently
*structural* — true because no order-routing code exists at all — not merely configured.

**Design thesis:** implied volatility persistently exceeds subsequently-realised volatility
(the variance risk premium). VEGA attempts to harvest that, gated on defined risk.

### 1.1 Data sources — all free, all unauthenticated

| Source | Used for | Reliability observed |
|---|---|---|
| yfinance | equity prices, option chains | **poor** — see §5.1 |
| Deribit | BTC/ETH DVOL, spot index | good |
| Coinbase | crypto daily candles | good |
| FRED | VIX, VXN, GVZ (dated series) | good |

No paid data is used. This is a standing constraint, not an oversight.

---

## 2. Architecture

```
main.py            engine scan → logs/scan_latest.json  (the canonical board artifact)
vega_candidates.py fast yfinance rescan → output/candidates/*.json  (what the AUTO-TRADER opens from)
auto_paper_cycle.py scheduled open/close against the candidates snapshot
vega_app.py        local cockpit (stdlib http.server), 6 views
analysis/          assessment, edge_calculator, vol_forecast, price_projection, hedge,
                   verdict, cross_venue, structure, levels, predictions, outcome_logger,
                   calibration_engine, ticker_profile, decisions
data/              fetcher, technicals, crypto, vol_indices, news, data_quality_log
```

### 2.1 The two-engine hazard — the single most important architectural fact

There are **two independent paths that produce tradeable candidates**:

- `main.py` → `screen_ticker()` → the engine board
- `vega_candidates.py` → `build_candidates()` → the snapshot the auto-trader opens from

**These have diverged four separate times**, each time silently, each time in a way that let
the desk trade on different numbers than the board displayed. The repository contains explicit
scar-tissue comments at each site. Most recently (fixed 2026-08-13): the forecast-VRP
correction landed only on the engine path, leaving the auto-trader selecting on the biased
number for several commits.

> **REVIEWER PRIORITY:** any divergence between these two paths is a live trading defect, not
> a code-style issue. We consider this the highest-risk structural feature of the codebase.

---

## 3. What was measured, and what the measurements say

Everything in this section is reproducible from free data. Methods and sample sizes are given
so a reviewer can disagree with the conclusion rather than the arithmetic.

### 3.1 The core thesis — VALIDATED over 36 years

VIX (FRED `VIXCLS`, 1990–2026) against subsequently-realised SPX vol over 24 trading days:

| Metric | Value |
|---|---|
| Mean VRP | **+3.99 vol points** |
| Positive | **84.9%** of 9,166 days |
| Worst | **−73.0** vol points |
| Every 5-year block | positive, including **2005–09 at +2.35** |

The premise is real and regime-robust. The `−73.0` tail is the entire risk of the strategy.

### 3.2 Entry rules — backtested, mixed results

Simulated 0.20-delta 5%-wide bull put, 24 trading days, 1991–2026:

| Rule | n | win% | Sharpe | 5th pct |
|---|---|---|---|---|
| always sell (baseline) | 8,939 | 94.3% | 4.12 | −0.33% |
| **IV rank ≥ 45** *(current gate)* | 4,485 | 94.2% | **4.34** | −0.49% |
| IV rank ≥ 70 | 2,635 | 94.2% | **4.57** | −0.46% |
| forecast VRP > 0 | 7,868 | 94.7% | 4.25 | −0.13% |
| vol EXPANDING | 2,042 | 93.8% | **3.95** | −0.94% |
| **vol COMPRESSING + IVrank ≥ 45** | 1,146 | **95.2%** | **5.17** | **+0.14%** |

**Findings:**
- The `MIN_IV_RANK = 45` gate adds measurable value; raising it adds more.
- **Selling into COMPRESSING vol at a decent IV rank is the best-supported rule found.** On raw
  VRP it caps the worst case at **−18.8 vs −73.0**. This contradicts the intuition that high
  vol means rich premium — vol expands *into* crashes and keeps expanding.
- The forecast-VRP correction is a better *measurement* but a near-neutral *entry filter*.

> **CAVEAT, LOAD-BEARING:** no historical option chains exist in free data, so credits are
> **modelled from VIX**, not read from a book. Any Sharpe above ~4 is a tell that something is
> unmodelled — there is no bid/ask, no slippage, no commission, no assignment risk. **Treat
> the rankings as meaningful and the levels as fiction.** This is also index data standing in
> for single names, whose VRP is smaller and less stable.

### 3.3 Volatility forecasting — validated out-of-sample

35,774 observations, 20 names, 8 years. VRP was computed against **trailing** realised vol
while the trade is paid against **forward** realised vol. The gap is directional:

| Vol state | Trailing error vs actual |
|---|---|
| COMPRESSING (<0.85×) | **−5.54pp** (understates → VRP read too positive) |
| EXPANDING (>1.15×) | **+10.39pp** (overstates → VRP read too negative) |
| Unconditional | −0.13pp |

The unconditional bias is ~zero, which is why this survived — the errors are large, opposite,
and cancel in any average. Replacement is mean reversion (`forecast = long_run + φ(recent −
long_run)`, φ=0.55 fitted on a 60% split). Held-out MAE **13.07 → 12.29**; state biases
collapse to +0.65 / +5.43.

### 3.4 Price projection — coverage-tested

14,900 held-out observations. Does a claimed X% window contain the price X% of the time?

| Target | lognormal+trailing | **lognormal+forecast** | empirical quantiles |
|---|---|---|---|
| 50% | 50.9% | **52.7%** | 55.6% |
| 80% | 79.5% | **81.5%** | 83.0% |
| 90% | 87.9% | **89.7%** | 92.0% |

A units bug (calendar days into a trading-day formula) made every band **20% too wide** — an
"80%" window actually covering 88%. Fixed and re-validated at 81.4%.

### 3.5 Sector rotation — TESTED AND REJECTED

Sector relative strength, 11 SPDR sectors, 8 years: rank correlation with forward returns is
**+0.01 to −0.04 at every horizon** from 21 to 252 days, **none significant** (p > 0.18),
top3-minus-bottom3 spread ≈ 0. **It is not used anywhere.** Sector *volatility* persists
strongly (+0.62 at 1m, +0.78 at 3m, p ~ 1e-207) and IS used, as a vol-forecast input only.

---

## 4. Known defects and open holes

### 4.1 No trade has been both selected AND filled on an executable basis

`analysis_eligible()` rejects any trade selected or filled on the mid. **0 of 65 closed trades
pass it.** The cohort that could validate this system did not exist until 2026-08-13, when
`fill_basis`/`gate_basis` began being recorded at open rather than derived from dates.

### 4.2 The win rate is a fill-model artifact

| Cohort | n | win rate | calibration gap |
|---|---|---|---|
| `mid \| mid \| credit_stop` | 18 | **72.2%** | **−5.4pp** |
| `natural \| mid \| credit_stop` | 41 | **0.0%** | −77.3pp |
| `natural \| mid \| ravens_v1` | 5 | 0.0% | −73.8pp |

Pooled, the ledger reports a −56.8pp calibration miss. **Split, the POP model is within 5.4pp
of calibrated on the cohort that filled where it was priced.** The catastrophe is entirely in
cohorts selected on mid prices and filled at natural ones. **45 of 65 closes were
`auto-stop-loss`.**

> **REVIEWER PRIORITY:** we believe the 21.5% headline win rate measures a *selection/fill
> mismatch*, not the strategy. Please attack this interpretation — it is the most consequential
> claim in this document and we have a clear motive to believe it.

### 4.3 Eleven gates, and none tests the edge

`REQUIRED_GATES` contains 11 entries. **None checks `true_pop − pop_implied`.** The `pop` gate
tests absolute probability against a floor. A spread can therefore pass 11/11 while VEGA's own
model rates it *worse* than the market prices it — observed live on IBIT at −12.6pp. This is
now surfaced on the card and in sort order, but **is not gated**. Making it a gate is an open
decision; the repo has scar tissue from criteria that silently emptied strategy boards.

### 4.4 Stop-loss logic is unvalidated

45 of 65 closes fired `auto-stop-loss`. The stop replay cannot be run: **1 of 65 closed trades
carries a `mark_history` path.** All 14 open positions are accumulating one (6–16 marks), so
the replay becomes possible as they close. **This is the single question most likely to move
P&L and it is currently unanswerable.**

### 4.5 Single global constants fitted across all names

- `VOL_REVERSION_PHI = 0.55` — fitted on 20 names, applied to 56.
- `IV_HV_INFLATOR = 1.2` — already documented in-repo as the reason IBIT could never trade.

Both are the same class of defect: a textbook constant pretending to be universal. Per-ticker
overrides exist as a mechanism but are deliberately unset.

### 4.6 Hedge long-leg estimation

`analysis/hedge.py` assumed the long leg at 60% of the short's delta. `long_delta` is now
stored, but **all historical candidates lack it** and cannot be backfilled.

---

## 5. Data quality

### 5.1 yfinance is stripping 33–68% of option chains DURING MARKET HOURS

Measured 2026-08-13, market open: JNJ 61%, AMGN 68%, XLV 88%, XLI 94% of records discarded as
stale or invalid. Five of twelve sector ETFs fail the 30% quotability floor entirely.

**This is the binding constraint on board size**, larger than any threshold. The liquidity
floor was lowered 100/500 → 25/100 after measuring that the old floor passed only 20% of legs
on mega-caps and **zero on AMGN** — but that change did not by itself produce more qualified
trades, because the chain data is the limit.

### 5.2 The MOVE index has no free source

FRED publishes no MOVE series (`BAMLMOVE`, `MOVE`, `ICEBOFAMOVE` all 404). Yahoo's `^MOVE`
returns HTTP 200 with a plausible value **stale since 2026-07-17**. TLT's cross-venue block
ships declared-but-disabled with the reason recorded. GVZ (`GVZCLS`) is genuinely free and
current.

---

## 6. What we believe, and our confidence

| Claim | Confidence | Basis |
|---|---|---|
| The variance premium is real and harvestable | **High** | 36 years, every regime |
| IV-rank gating adds value | **Medium-high** | 36-year backtest, index only |
| Selling into compressing vol is safer | **Medium** | backtest; modelled credits |
| The POP model is roughly calibrated | **Low-medium** | one cohort, n=18 |
| VEGA's live results measure the strategy | **Very low** | see §4.1, §4.2 |
| Forecast VRP improves *selection* | **Low** | near-neutral in backtest |
| Forecast VRP improves *measurement* | **High** | held-out MAE and bias |

---

## 7. Specific questions for the reviewer

1. **Is §4.2's interpretation self-serving?** We claim the 0/46 losing cohort reflects a
   fill/selection mismatch rather than a broken strategy. What evidence would falsify that?
2. **Should negative POP-gap be a hard gate** (§4.3), advisory, or auto-trader-only?
3. **Is a 0.20-delta short strike correct** for a $500 account, or is the position sizing
   fundamentally mismatched to the risk?
4. **Is `φ = 0.55` overfit?** 20 names, one split.
5. **Is the modelled-credit backtest (§3.2) worth anything at all**, or should its conclusions
   be discarded until chain history is available?
6. **Is the 2-engine architecture (§2.1) salvageable**, or should the fast path be deleted?
7. **What is the minimum viable clean cohort** before any calibration claim is honest? We have
   assumed ~30 closed trades; that number is not defended.

---

## 8. Reproducing everything here

```bash
cd options_intelligence
python -m pytest -q                 # 986 tests
python main.py                      # engine scan → logs/scan_latest.json
python vega_app.py                  # cockpit at 127.0.0.1:8765
python vega_status.py               # read-only health + record (needs PYTHONUTF8=1)
python clv_tracker.py               # CLV + per-cohort calibration
python verify_numbers.py            # artifact reconciliation
```

Git history is intact and unrewritten; every behavioural change carries its measurement in the
commit message.

---

## 9. Bottom line

VEGA is pointed at a **real, 36-year-durable phenomenon**, and its analytical layer measures
what it claims to measure — that has been checked out-of-sample rather than asserted.

**It has not yet demonstrated that it can capture the premium.** Its live record is 14/65, and
we believe that number describes an execution defect rather than the strategy, on evidence we
have a clear motive to believe. **No closed trade yet exists in a cohort clean enough to
settle it.** That is the honest state of the project: a validated thesis, a measured engine,
and an unproven desk.

*Not financial advice. No orders are placed. Personal beta.*
