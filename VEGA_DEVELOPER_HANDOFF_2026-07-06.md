# VEGA — Developer Handoff & Master Reference

**Version:** 2026-07-06 · **Repo:** `options_intelligence` · **Audience:** whoever picks this up in VS Code next (you, or a future Claude/Copilot session).

This single document is meant to be opened first. It gives you the project's purpose, the full map of the code, a red-team of where it breaks, the options-trading theory you need to read the code critically, and an honest account of what paid tools would buy you. Companion docs: `VEGA_Audit_Report_2026-07-06.docx` (findings) and `VEGA_FIX_PUNCHLIST_2026-07-06.md` (the fixes applied on 2026-07-06).

---

## 0. Quick start in VS Code

```bash
# From the repo root:
cd "C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence"

python -m venv .venv && .venv\Scripts\activate      # optional but recommended
pip install -r requirements.txt

python smoke_test_data.py            # data viability across the 13-ticker watchlist
python main.py --session morning     # run a real scan -> writes tipsheet + logs modeled trades
python log_outcome.py report         # Gate 1 calibration (once you have fills)
```

Verified 2026-07-06 on the tower (Python 3.14.3): all modules compile; smoke test 13/13 PASS on free
yfinance data. **Uncommitted** working-tree changes exist — `del .git\index.lock`, then
`git add analysis/outcome_logger.py log_outcome.py VEGA_FIX_PUNCHLIST_2026-07-06.md` and commit.

---

## 1. What VEGA is and why it exists

**One line:** a daily options-income scanner that screens a fixed watchlist for **bull put credit spreads**
that look statistically favorable, produces an HTML "tipsheet," and pushes results to the JARVIS tower —
you execute manually.

**The thesis.** VEGA is built on the *volatility risk premium* (VRP): implied volatility (what options
buyers pay) is, on average, higher than the volatility that actually shows up. Selling defined-risk premium
(credit spreads) is a way to harvest that gap. The system's original framing borrowed from sharp sports
betting: the market gives you an *implied* probability (via the option's delta), and you compare it to the
*actual historical* probability; a positive gap is your "edge."

**What it is NOT:** it is not an execution/trading bot (you place every order by hand), not financial advice,
and not a directional predictor. It is a screener + risk-framer. It deliberately runs on free data.

**Design values (in priority order):**
1. **Never crash a scan.** Every data path degrades to a safe default.
2. **Hard risk rules are non-negotiable** (delta cap, OTM buffer, earnings blackout, liquidity floor).
3. **Account-agnostic output** — risk tiers show contract counts for several account sizes.
4. **Explainability** — every qualifier carries the reasons it passed; every rejection carries a category.

**History (from `project_state.md`):** started 2026-03 on Tradier live data; renamed WOLF→VEGA; migrated to
Polygon-capable code but runs on yfinance (no paid options plan); a 2026-05-20 remediation fixed 7 structural
faults; a 2026-07-06 audit found the *edge model itself* was measuring drift, not VRP, and fixed it (see §6).

**Where VEGA sits in the wider system:** it is the scanner. **JARVIS** (the tower at `192.168.0.222`) is the
brain/store — VEGA POSTs each scan to `JARVIS_HOST/vega/ingest`. **BV content formatter** turns scans into
social posts. n8n + Buffer handle distribution. This repo is only the scanner; the tower work is tracked in
`project_state.md`.

---

## 2. Repository structure & key locations

```
options_intelligence/
├── main.py                     # ORCHESTRATOR — run_scan(), screen_ticker(), pair selection, gates
├── config.py                   # SINGLE SOURCE OF TRUTH for every threshold & knob
├── requirements.txt            # yfinance, pandas, numpy, scipy, requests, anthropic, openai, jinja2 …
│
├── data/                       # ── ACQUISITION LAYER (all external I/O, all degrade-gracefully) ──
│   ├── fetcher.py              #   price history, options chains, VIX, earnings, news; Polygon/yfinance/Tradier
│   ├── technicals.py           #   SMA/EMA/RSI/MACD/BB/ATR, realized vol, IV-Rank, VRP, support/resistance, trend
│   ├── fundamentals.py         #   yfinance fundamentals + days_until_earnings()
│   ├── news.py                 #   headline sentiment (GPT-4o w/ keyword fallback)
│   └── iv_history/{TICKER}.json#   self-bootstrapping IV samples for real IV-Rank (grows daily)
│
├── analysis/                   # ── DECISION LAYER (pure functions, no I/O) ──
│   ├── edge_calculator.py      #   VRP, true_pop (detrended), edge points, composite 0-100 edge score, spread metrics
│   ├── strike_validator.py     #   HARD rules — delta, OTM buffer, earnings, credit, liquidity, credit/width
│   ├── synthesizer.py          #   builds the tipsheet narrative/structure from qualified trades
│   └── outcome_logger.py       #   NEW (Gate 1) — records modeled trades; set_fill/set_close
│
├── output/                     # ── PRESENTATION LAYER ──
│   ├── renderer.py             #   renders tipsheet HTML from Jinja templates
│   ├── emailer.py              #   SMTP send (disabled unless SMTP_* set)
│   ├── templates/*.html        #   morning.html, close.html
│   └── tipsheets/*.html        #   dated output artifacts
│
├── log_outcome.py              # NEW (Gate 1) CLI — list / fill / close / report
├── smoke_test_data.py          # data-viability probe across the watchlist (no trades, just coverage)
├── vega_ingest.py              # POSTs scan payload to JARVIS_HOST/vega/ingest (non-blocking)
├── send_vega_email.py          # standalone email helper
├── bv_content_formatter.py     # scan -> 3 social posts (BeardedVentures)
│
├── logs/
│   ├── scan_log.json           # append-only history of every scan (qualified, rejected, source health, regime)
│   ├── run.log                 # runtime log
│   └── vega_outcomes.jsonl     # NEW — Gate 1 modeled-vs-actual ledger (created on first scan)
│
├── .github/workflows/scan.yml  # GitHub Actions cron: 13:50 UTC (9:50 ET) morning, 19:10 UTC (15:10 ET) close
├── .env / .env.example         # secrets (gitignored) / template
└── VEGA_*.md, verify_vega.bat   # audit/patch docs + the verify script
```

**"Where do I change X?" cheat-sheet**

| I want to… | Edit |
|---|---|
| Add/remove a ticker | `config.WATCHLIST` **and** `config.TICKER_SECTORS` (for the sector cap) |
| Loosen/tighten a gate | `config.py` (MIN_EDGE_SCORE, MIN_IV_RANK, MIN_PROBABILITY_OF_PROFIT, MIN_CREDIT_TO_WIDTH_PCT, delta/buffer, DTE) |
| Change how "edge" is scored | `analysis/edge_calculator.py` → `calculate_edge_score` (component weights) and `calculate_true_pop` |
| Change strike selection | `main.py` → `select_bull_put_pair`; hard rules in `analysis/strike_validator.py` |
| Change a data source | `data/fetcher.py` → `get_options_chain` / `get_price_data` |
| Change the tipsheet look | `analysis/synthesizer.py` + `output/templates/*.html` + `output/renderer.py` |
| Add a new strategy | New selector in `main.py`, metrics in `edge_calculator.py`, rules in `strike_validator.py`, enable in `config.ENABLED_STRATEGIES` |

---

## 3. Scan pipeline (end-to-end)

`main.run_scan(session_type)`:

1. **Setup** — timestamp (ET), watchlist, data-source health probe (`yfinance_only` unless a Polygon key exists).
2. **Market context** — `build_market_context()`: VIX level+trend, SPY day change, macro headlines, RISK-ON/OFF bias.
3. **Regime gate** — `_compute_regime_context()`: classifies VIX into LOW_VOL / NORMAL / ELEVATED / HIGH and
   emits a note (e.g. "VIX below edge threshold — expect few qualifiers; do not chase").
4. **Per ticker** `screen_ticker()`:
   - price history (2y) → current price;
   - fundamentals + fundamentals score (shadow mode: logs, doesn't block);
   - options chain 21–45 DTE (`get_options_chain`, quality-filtered);
   - `select_bull_put_pair()` — find a same-expiration short/long put pair that clears delta, OTM buffer,
     liquidity, quote-width, min-credit, and credit/width;
   - technicals (`calculate_all`) → IV-Rank, VRP, trend, composite technical score, support;
   - **gates in order:** IV-Rank ≥ 45 → news not BLOCKING → (fundamentals strict, off by default) →
     POP at breakeven ≥ 72% → composite edge score ≥ 60 → hard strike validation;
   - build the trade dict (credit, max loss, risk tiers, POP, edge, greeks, technicals, warnings).
5. **Sector cap** — `_apply_sector_limit()`: at most 2 qualifiers per sector (broad-market ETFs exempt), highest edge wins.
6. **Synthesize + render** — narrative + HTML tipsheet to `output/tipsheets/`.
7. **Persist** — append full entry to `logs/scan_log.json`; record modeled trades to `logs/vega_outcomes.jsonl` (Gate 1).
8. **Ingest** — POST full payload to JARVIS (non-blocking); email if configured.

---

## 4. Configuration reference (the important knobs)

All in `config.py`. Current values in **bold**; rationale where non-obvious.

**Account / sizing** — `ACCOUNT_BALANCE=500`, `MAX_RISK_PER_TRADE_PCT=0.20`, `MAX_SPREAD_WIDTH=5` (10 if ≥$5k),
`RISK_TIERS` (<$100 / <$500 / <$1,000) drive the per-tier contract table.

**Strike placement (hard)** — `SHORT_STRIKE_TARGET_DELTA=0.20`, `SHORT_STRIKE_MAX_DELTA=0.30`,
`MIN_STRIKE_BUFFER_SPY=$10`, `MIN_STRIKE_BUFFER_STOCK=5%`. `SPY_BUFFER_TICKERS = {SPY,QQQ,IWM,DIA,GLD,TLT}`.

**Scan criteria** — `MIN_PROBABILITY_OF_PROFIT=0.72`, `MIN_IV_RANK=45`, `MIN_CREDIT_USD=25`,
`MIN_DTE=21 / MAX_DTE=45 / PREFERRED_DTE_TARGET=35`, `TARGET_PROFIT_PCT=0.50`, `STOP_LOSS_MULTIPLIER=2.0`,
`MAX_QUOTE_SPREAD_PCT=0.35`, liquidity `MIN_OPTION_VOLUME=100` OR `MIN_OPTION_OPEN_INTEREST=500`.

**Edge filters** — `MIN_EDGE_SCORE=60`, `VRP_MIN_THRESHOLD=0.02` *(2026-07-06: was 0.15)*,
`MIN_CREDIT_TO_WIDTH_PCT=0.15` *(was 0.25)*, `NARROW_SPREAD_MIN_CREDIT_TO_WIDTH=0.20` *(was 0.30)*.

**Regime** — `VIX_MIN_FOR_EDGE=16`, `VIX_ELEVATED_THRESHOLD=25`, `VIX_MAX_FOR_TRADES=30`, `VRP_HV_WINDOW=35`.

**IV history / true_pop (2026-07-06)** — `IV_HISTORY_MIN_SAMPLES=30`, `IV_HV_INFLATOR=1.2`,
`TRUE_POP_DRIFT_MODE="risk_free"` (set to `"raw"` to A/B the old drift-inclusive edge model).

**Fundamentals** — `FUNDAMENTALS_ENABLED=True`, `FUNDAMENTALS_SHADOW_MODE=True` (score/log only),
`FUNDAMENTALS_STRICT_BLOCK=False`. **Strategy** — `ENABLED_STRATEGIES=["bull_put_spread"]` only.
**Earnings** — `EARNINGS_BLACKOUT_DAYS=7`, `ENABLE_VOL_CRUSH_MODE=True`. **Rates** — `RISK_FREE_RATE=0.04`.

---

## 5. Options strategy education (read this before editing the model)

### 5.1 The bull put spread (what VEGA trades)
Sell a put at strike **K_short**, buy a cheaper put at a lower strike **K_long** (same expiration). You collect a
net **credit**. You want the stock to stay **above K_short** so both expire worthless and you keep the credit.
- **Max profit** = net credit (if price ≥ K_short at expiry).
- **Max loss** = (K_short − K_long) − credit = "width − credit" (if price ≤ K_long).
- **Breakeven** = K_short − credit. You profit as long as price > breakeven.
- It's a **defined-risk, bullish-to-neutral, positive-theta** trade: you're short volatility and long time decay.

### 5.2 Volatility Risk Premium (the whole thesis)
VRP = Implied Vol − Realized Vol. Historically IV > RV ~85% of the time; the S&P average VRP is **~4.2 vol
points** (1990–2018) and **~6.5** since 2020. That premium is the seller's structural edge — you're paid to
insure others against moves that, on average, don't fully materialize. **Caveat:** the premium is compensation
for *tail risk*. It pays a little most months and loses a lot occasionally (short-vol is "picking up pennies in
front of a steamroller" if unmanaged). Defined-risk spreads + position sizing are how you survive the steamroller.

### 5.3 The Greeks (what each sensitivity means for a credit spread)
- **Delta** — sensitivity to price. A short put's |delta| ≈ risk-neutral probability it finishes in-the-money. A
  0.20-delta short strike ≈ 80% chance of expiring OTM. VEGA targets 0.20 and hard-caps at 0.30.
- **Theta** — time decay. Positive for a credit spread; your daily tailwind. Peaks in the last ~30–45 DTE, which
  is why VEGA lives in the 21–45 DTE band.
- **Vega** — sensitivity to IV. A credit spread is short vega: you *want* IV to fall after you sell. Selling when
  IV-Rank is high (VEGA requires ≥45) means you sell rich and benefit from mean reversion.
- **Gamma** — how fast delta changes. Short options are short gamma; risk accelerates as price nears the short
  strike and as expiration approaches. This is why VEGA sizes down / stands aside in high-VIX regimes.

### 5.4 Probability of profit vs delta (why VEGA computes both)
- **Implied POP** ≈ 1 − |delta| (market's risk-neutral view).
- **True/historical POP** — how often price *actually* stayed above the level over rolling windows of history.
- **P(max profit)** — probability above the **short strike** (used to score *edge* vs implied POP).
- **P(profit)** — probability above the **breakeven** (used for the 72% gate). Since breakeven < short strike,
  P(profit) > P(max profit). Conflating these was audit finding C2 (now fixed).
- **Edge** = historical P(OTM) − implied P(OTM). Positive = you're being overpaid for the risk.

### 5.5 IV Rank vs IV Percentile
- **IV Rank** = where current IV sits between its 52-week min and max: `(IV−min)/(max−min)`.
- **IV Percentile** = % of days over the lookback that IV was *below* today's. VEGA approximates a percentile
  from stored IV history once ≥30 samples exist; before that it uses an HV-based approximation (see §6, M1).
- Rule of thumb: **sell premium when IV Rank is high** (rich options, room to mean-revert down).

### 5.6 Managing the trade (VEGA's defaults)
Take profit at **50% of max credit** (`TARGET_PROFIT_PCT`); stop if the spread doubles (`STOP_LOSS_MULTIPLIER=2`);
avoid earnings inside the trade (7-day blackout) unless explicitly in vol-crush mode; exit/roll before gamma
spikes in the final week. These are industry-standard tastytrade-style mechanics.

---

## 6. Red Team — flaws, break points, failure modes

Grouped by severity. Items marked **[FIXED 07-06]** were addressed in the punch-list; the *residual* risk is
still described because none of these are fully "solved."

### Model risk (the edge might not be real)
- **C1 — drift vs VRP [FIXED 07-06, residual].** The historical-probability engine used to inherit the sample
  period's price drift, so in a bull market every bull-put spread showed "edge." Now detrended to a risk-free
  drift. **Residual:** (a) it's still a *backward-looking* empirical estimate — the last 2 years of dispersion
  may not predict the next 35 days; (b) removing drift is itself a modeling choice — a stock genuinely can have
  persistent drift, and you've deliberately thrown that signal away to isolate vol; (c) small underlyings have
  short/noisy histories. **Break point:** regime change (a low-vol bull flips to a high-vol selloff) — the
  detrended history won't have seen it.
- **European-style POP (path independence).** `calculate_true_pop` checks only the **end-of-window** price. Real
  American options can be assigned early, and a spread can be stopped out mid-life even if it would've recovered
  by expiry. So POP overstates the probability you *hold to a win*. **Fix path:** Monte Carlo / path-dependent
  simulation with the stop-loss modeled (Phase 3 in `project_state.md`).
- **Overlapping windows [partially FIXED 07-06].** Confidence now scales with independent (non-overlapping)
  windows, but the point estimate still uses overlapping windows (autocorrelated). Fine for a rough number, not
  for a p-value.
- **VRP bands + credit/width recalibrated [FIXED 07-06].** Old VRP bands never rewarded realistic 4–6pp VRP; old
  25% credit/width floor was mutually exclusive with the 0.20-delta target. **Residual:** the new 0.15 floor
  admits thinner credits — safety now leans harder on the OTM buffer and the POP gate, so don't weaken those.

### Data risk (garbage in)
- **M2 — yfinance is delayed & unofficial.** 15–20 min delayed, no native Greeks (VEGA computes delta/theta via
  Black-Scholes from yfinance's IV, which itself can be stale/wrong), can silently return empty chains or break
  when Yahoo changes their site. The smoke test shows heavy stale-quote filtering on thin names (e.g. **KRE 57%,
  AAPL 35% dropped**). **Consequence:** modeled credit ≠ your real fill. This is the #1 reason Gate 1 exists.
- **IV-Rank still APPROX [FIXED-ish 07-06].** Until 30 real IV samples accrue per ticker, IV-Rank is an inflated
  HV approximation (now de-biased with `IV_HV_INFLATOR`, but still an approximation). Treat the IV-Rank gate as
  provisional for the first ~6 weeks of daily scans. The `data/iv_history/*.json` files must be allowed to grow.
- **Liquidity on thin names.** KRE, OXY, GDX chains are thin; even after filtering, a "valid" quote may have a
  wide real spread. The liquidity floor helps but isn't a fill guarantee.

### Financial / execution risk (real money)
- **Tiny-account economics.** At `ACCOUNT_BALANCE=500` with `MIN_CREDIT_USD=25`, commissions + bid/ask slippage
  can eat a large fraction of the edge. No **slippage or commission model** exists anywhere in the code —
  modeled P/L is optimistic. Add one before believing the Gate 1 P/L numbers.
- **Assignment / dividend risk.** American options on dividend-paying underlyings (and ETFs like XLE/KRE/GDX) can
  be assigned early around ex-div. Not modeled.
- **Correlation in a selloff.** The sector cap limits names per sector, but in a real crash *all* short-put
  spreads lose together — diversification across correlated equity names is illusory when correlations → 1.
- **No portfolio-level risk.** Each trade is validated in isolation; there's no aggregate max-drawdown or
  total-capital-at-risk gate across simultaneously open positions.

### Operational risk
- **L1 — the cron is unverified.** No scan since **2026-05-20**. GitHub Actions may be disabled, or `JARVIS_HOST`
  unset. Confirm a manual run succeeds and the ingest log line appears. Until then the "daily" system isn't daily.
- **Stale `.git/index.lock`.** Present now; `del .git\index.lock` before committing.
- **13 `[DEBUG]` prints** in the scan path (noise in CI) — route through `logger.debug`.
- **News via GPT-4o** — cost per scan + model-deprecation risk (there's a keyword fallback, good). NewsAPI key
  optional.

### Security
- **[FIXED] leaked GitHub PAT** removed from the git remote; `.env` is gitignored and untracked. **Residual:**
  live Anthropic/OpenAI/NewsAPI/Tradier keys sit in plaintext `.env` inside a cloud-synced folder — move to a
  secrets manager or exclude from sync, and rotate periodically.
- **JARVIS ingest over plain HTTP** to `192.168.0.222:8000` on the LAN — fine on a trusted network, not off it.

### "If you change one thing, don't break these"
The **hard rules** in `strike_validator.py` (delta cap, OTM buffer, earnings blackout, liquidity, credit,
credit/width) are the safety floor. The gates are intentionally strict; loosening `MIN_PROBABILITY_OF_PROFIT`,
the delta cap, or the OTM buffer trades away the margin that lets a free-data system be safe at all.

---

## 7. Key indicators reference (what VEGA computes and how it uses them)

**Technical (`data/technicals.py`)**
- **SMA 20/50/200, EMA 9/21** — trend structure. Feed `_classify_trend` (STRONG_UP…STRONG_DOWN). Composite: +10 if price > SMA50.
- **RSI(14)** — momentum/overbought-oversold. Healthy sell zone 40–65 (+15 composite); >70 overbought warning; >72 kills the "not overbought" points.
- **MACD(12/26/9)** — trend momentum; histogram >0 = +10 composite; crossover labeled bullish/bearish.
- **Bollinger Bands(20,2)** — volatility envelope; price > lower band = +15; band width < 3% flags a squeeze.
- **ATR(14)** — average true range, absolute volatility (for context/sizing).
- **Realized vol (HV)** — annualized stdev of log returns; `VRP_HV_WINDOW=35` matches the DTE target so VRP is horizon-relevant.
- **IV-Rank** — percentile of current IV vs stored history (or HV-approx while bootstrapping). Gate ≥45; >50 = +20 composite, 45–50 = +10.
- **VRP (pp)** = IV − RV in vol points. Drives 30 pts of the edge score. Realistic range 2–10pp.
- **Support/Resistance** — swing extrema + round numbers; a short strike above nearest support = +20 composite; strike within 10% of support = warning.
- **Trend** — count of MAs price is above + RSI → STRONG_UP/UP/NEUTRAL/DOWN/STRONG_DOWN. Steers (nominal) strategy choice.
- **Volume ratio** — today vs 20-day avg; used by EOD mean-reversion and morning-signal logic.

**Fundamentals (`data/fundamentals.py`, shadow mode)** — debt/equity, current ratio, profit & operating margin,
revenue & earnings growth, free cash flow, analyst recommendation → a 0–10 *stability* score (a risk filter, not
a value model). Currently logged, not blocking. ETFs get a baseline 8.

**Composite edge score (0–100, `edge_calculator.calculate_edge_score`)**:
VRP 30 · true-POP edge 25 · technical 20 · news 10 · fundamentals 10 · earnings safety 5. Qualify ≥ 60 **and**
VRP ≥ 0 **and** edge ≥ 0 **and** no disqualifier (negative VRP, negative edge, BLOCKING news, earnings blackout).

---

## 8. What premium tools do best (and when VEGA should pay)

VEGA runs free by design. Here's honestly what money buys, grounded in current (2026) offerings.

**Data APIs (raw + Greeks)**
- **ThetaData** — cheapest credible real-time options + Greeks (~$25/mo Standard); excellent price-to-data.
- **Polygon / Tradier** — raw chains; *you* compute Greeks. Polygon's options is a **paid add-on** (its free tier
  is EOD-only, which is why VEGA's Polygon path is dormant). Tradier needs a funded brokerage account.
- **ORATS** — deep **historical** options data with Greeks + a hosted backtester (300M+ pre-computed backtests,
  ~$99/mo). Best if your value-add is strategy, not data plumbing.
- **CME Group** — institutional, liquidity-pre-filtered Greeks/IV from two-sided markets only (no stale quotes).
- **FlashAlpha / Unusual Whales** — pre-computed dealer positioning (GEX, gamma flip, call/put walls) in one call.

**Analytics / research platforms**
- **Market Chameleon** — best all-round volatility research: IV rank/percentile, earnings-vol history, flow,
  screeners. Directly better than VEGA's IV-Rank bootstrap and news sentiment.
- **tastytrade** — options-first execution + probability metrics, backtesting, multi-leg builders, liquidity
  ratings, auto P/L on rolls. A natural execution home for what VEGA screens.
- **OptionStrat** — best payoff-diagram visualization + strategy optimizer + flow.

**Where paying would most improve VEGA, in order of bang-for-buck:**
1. **Real-time options + real Greeks** (ThetaData ~$25/mo) — kills the delayed/BS-derived-Greek problem (audit M2)
   and makes modeled credit ≈ real fill. Biggest single upgrade.
2. **Real historical IV** (ORATS/Market Chameleon) — replaces the IV-Rank approximation (M1) immediately, no
   6-week bootstrap wait.
3. **A backtester** (ORATS) — validates the edge model against 2022–2025 far faster than Gate 1's live logging.

**Don't pay yet if:** the smoke test says free data supports your watchlist (it does — 13/13 PASS), and you
haven't yet logged ~30 Gate 1 outcomes. Let real fills tell you whether there's an edge worth sharpening before
spending. *Decide with evidence, not assumption.*

---

## 9. Prioritized backlog

1. **Verify the cron + `JARVIS_HOST`** (L1) — make "daily" actually daily.
2. **Run Gate 1 to 30 closed trades** — the empirical test of everything above. Add a **slippage/commission
   model** so the P/L is honest.
3. **Commit the 07-06 fixes** (uncommitted working tree).
4. **Exit IV-Rank APPROX** — let `data/iv_history` accrue, or buy real IV history.
5. **Path-dependent POP** (Monte Carlo w/ modeled stop) — replaces the European-style estimate.
6. **Portfolio-level risk gate** — cap total capital-at-risk across open positions.
7. **Second strategy** — bear-call spread for overbought/bearish regimes (config stub exists).
8. **Cleanups** — remove `[DEBUG]` prints; secrets out of synced `.env`.

---

## 10. Appendix

**Commands**
```bash
python main.py --session morning|close     # run a scan
python smoke_test_data.py                  # data viability
python log_outcome.py list|fill|close|report
python -m py_compile config.py main.py analysis\*.py data\*.py   # syntax check
verify_vega.bat                            # compile + smoke in one shot -> verify_output.txt
```

**Gate 1 loop**
```
scan  ->  auto-records "modeled" rows in logs/vega_outcomes.jsonl
place trade  ->  python log_outcome.py fill  "<id>" <actual_credit>
close trade  ->  python log_outcome.py close "<id>" <exit_price> <win|loss|scratch> "reason"
weekly        ->  python log_outcome.py report   # credit gap, POP calibration, realized P/L
```

**Glossary** — VRP volatility risk premium · POP probability of profit · IV/RV implied/realized vol · DTE days to
expiration · OTM out-of-the-money · credit premium received · width strike distance · assignment being forced to
buy/sell the underlying · theta/vega/gamma/delta the Greeks.

**Key files to open first in VS Code:** `config.py` (the knobs) → `main.py::screen_ticker` (the flow) →
`analysis/edge_calculator.py` (the model) → `analysis/strike_validator.py` (the safety floor).

---
*Not financial advice. VEGA is an educational screener; every trade decision and order is the user's own.*
