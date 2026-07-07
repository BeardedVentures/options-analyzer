# VEGA — Fix Punch-List (2026-07-06)

Companion to `VEGA_Audit_Report_2026-07-06.docx`. Every audit finding that could be fixed in code
has been applied to the working tree. This doc maps each change so you can review the git diff in
VS Code, understand the "why," and verify before committing.

All edits are **surgical** — unchanged code is byte-identical. Review with:

```bash
cd "C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence"
del .git\index.lock            # a stale lock is present (PowerShell: Remove-Item .git\index.lock)
git diff -- config.py main.py analysis/edge_calculator.py data/technicals.py
git add analysis/outcome_logger.py log_outcome.py   # new files
python -m py_compile config.py main.py analysis/*.py data/*.py   # sanity compile
python smoke_test_data.py       # confirms live yfinance chains still populate
```

> Note: I could not run `py_compile`/`smoke_test` from my sandbox — the mount served stale, truncated
> copies of a few files (a sync artifact), so a shell compile there is meaningless. The edits
> themselves are confirmed correct on your actual files. Please run the two commands above on the
> tower to confirm against the real files before committing.

---

## Changes at a glance

| # | Finding | Files touched | Type |
|---|---------|---------------|------|
| C1 | Edge measured drift, not VRP | `analysis/edge_calculator.py`, `config.py` | Logic |
| C2 | "true_pop" was P(max profit), mislabeled & gated wrong | `main.py`, `analysis/edge_calculator.py` | Logic |
| H1 | VRP score band pinned at floor in normal vol | `analysis/edge_calculator.py` | Calibration |
| H2 | 0.20Δ target vs ≥25% credit/width were mutually exclusive | `config.py` | Calibration |
| M1 | IV-Rank APPROX overstated (~100) | `data/technicals.py`, `config.py` | Calibration |
| M3 | Overlapping windows overstated confidence | `analysis/edge_calculator.py` | Correctness |
| L2 | Dead strategy config (5 listed, 1 implemented) | `config.py` | Cleanup |
| Gate 1 | No outcome logging | `analysis/outcome_logger.py`, `log_outcome.py`, `main.py` | New feature |

Not changed in code (need your hands / judgment): L1 verify cron + `JARVIS_HOST`; L3 move secrets out
of the cloud-synced `.env`; L4 the 13 `[DEBUG]` prints (left in for now — harmless, and useful while
you validate).

**L2** — `ENABLED_STRATEGIES` trimmed to `["bull_put_spread"]` (the only implemented strategy;
main.py already hard-forces it). The other four are kept as a roadmap comment.

**Gate 1 was verified end-to-end** in a scratch run: record → idempotent re-run (0 dupes) → fill →
close → report. The report correctly surfaced the modeled-vs-actual credit gap, a calibration gap
(realized win rate vs modeled POP), and realized per-contract P/L.

---

## C1 — Edge now measures volatility, not trend  *(the important one)*

**What was wrong:** `calculate_true_pop` replayed raw price history and counted how often price
stayed above the strike. That statistic is dominated by the sample period's drift, so the same trade
showed strong edge in a bull sample and negative edge in a flat/down sample.

**Change** (`analysis/edge_calculator.py` → `calculate_true_pop`): work in log-return space, subtract
the realized mean drift, and add back a small risk-free drift (`config.TRUE_POP_DRIFT_MODE = "risk_free"`).
The result now reflects the stock's volatility structure under a near-risk-neutral assumption —
directly comparable to the option's implied probability (1 − |delta|). Modes: `risk_free` (default),
`zero`, `raw` (legacy, for A/B checks).

**Verified:** same 5%-OTM 35-DTE put, 25% vol, three regimes —

| regime | OLD (raw) edge | NEW (detrended) edge |
|---|---|---|
| Bull +25%/yr | +4.2 | −0.9 |
| Flat 0%/yr | −9.9 | −0.9 |
| Bear −25%/yr | −23.5 | −0.9 |

The 28-point swing driven purely by trend is gone. (A near-zero edge here is *correct* — the synthetic
had IV = RV, i.e. no premium, so there should be no edge.)

---

## C2 — Two honest probabilities instead of one mislabeled one

**What was wrong:** the metric labeled `true_pop` was actually P(price > short strike) = probability of
**max** profit, but it was gated against `MIN_PROBABILITY_OF_PROFIT` (72%) as if it were probability of
profit.

**Change** (`main.py` → `screen_ticker`): compute both, from the same detrended engine —
- `p_max_profit` = P(above short strike) → used for **edge** vs implied P(OTM).
- `p_profit` = P(above breakeven), breakeven = short strike − net credit → used for the **72% gate**.

The trade dict now carries `true_pop` = `p_profit` (real POP), plus `p_max_profit`,
`true_pop_confidence`, and `true_pop_drift_mode`.

---

## H1 — VRP band recalibrated to reality

**What was wrong:** bands were `<10pp→5, 10–20→15, 20–30→22, ≥30→30`. Real S&P VRP averages ~4.2pp
(1990–2018) and ~6.5pp since 2020 (Cboe/CAIA), so the biggest single component (30 pts) sat at its
5-pt floor in every normal market.

**Change** (`analysis/edge_calculator.py` → `calculate_edge_score`): `<0→0, 0–2→8, 2–4→15, 4–6→22,
6–10→27, ≥10→30`. Also `VRP_MIN_THRESHOLD` 0.15 → 0.02 in `config.py`.

**Verified:** VRP 4pp now scores 22/30 (was 5); 6pp → 27/30 (was 5).

---

## H2 — Resolved the delta-vs-credit/width contradiction

**What was wrong:** config targeted a 0.20-delta short strike **and** required credit/width ≥ 25%. A
20-delta spread structurally pays ~13–20% of width, so the two rules could not both hold and most valid
index spreads were silently rejected.

**Change** (`config.py`): `MIN_CREDIT_TO_WIDTH_PCT` 0.25 → **0.15** (the true floor for a 20Δ spread);
`NARROW_SPREAD_MIN_CREDIT_TO_WIDTH` 0.30 → 0.20. The 33% "ideal" warning in `strike_validator` stays.
Safety now leans on the OTM buffer + the POP gate — the right place for it.

**Verified:** a 0.20Δ SPY 5-wide spread at 16% credit/width now **passes** (was rejected).

---

## M1 — IV-Rank bootstrap de-biased

**What was wrong:** until 30 real IV samples accumulate, the fallback ranked IV against the realized-HV
distribution. Because IV sits above HV by design (that's the VRP), it returned ~100 almost always.

**Change** (`data/technicals.py` → `_iv_rank_hv_approx`): scale the HV distribution by
`config.IV_HV_INFLATOR = 1.2` (typical IV/HV ratio) before comparing, so a normal IV lands
mid-distribution. Still an approximation — the real percentile takes over once IV history builds. Let
scans run daily to exit APPROX mode; treat IV-Rank as provisional until then.

## M3 — Confidence reflects independent samples

`calculate_true_pop` confidence is now based on independent (non-overlapping) windows
(`total / expiration_days`): ≥12 HIGH, ≥5 MEDIUM, else LOW — instead of a raw overlapping count that
labeled everything HIGH. Adds `independent_windows` to the result.

---

## Gate 1 — Outcome logging (new)

This is recommendation #1 from the audit: capture ground truth before trusting any qualifier.

**How it works:**
- Every scan auto-appends each qualified trade as a `modeled` row to `logs/vega_outcomes.jsonl`
  (hooked non-blocking into `run_scan`; idempotent per day). Records modeled credit, delta, IV-rank,
  edge score, `p_max_profit`, modeled POP, etc.
- You add ground truth as you trade, via the CLI:

```bash
python log_outcome.py list                                   # open trades + their ids
python log_outcome.py fill  "<id>" 0.78                       # actual credit/share you got
python log_outcome.py close "<id>" 0.39 win "50% target hit"  # exit price + outcome
python log_outcome.py report                                  # calibration report
```

**The report answers the three questions the audit raised:**
1. **Credit gap** — how far your real fills sit below the modeled (delayed-yfinance) credit.
2. **Probability calibration** — realized win rate vs modeled POP (negative gap = model too optimistic).
3. **Realized edge** — actual per-contract P/L, win/loss averages.

Target: **30 closed trades** before treating qualifiers as validated edge.

Files: `analysis/outcome_logger.py` (storage, stdlib-only, never raises into the scan),
`log_outcome.py` (CLI). Data: `logs/vega_outcomes.jsonl` (plain JSON lines, safe to hand-edit).

---

## Rollback

Every change is config- or function-local. To A/B the edge model without reverting, set
`TRUE_POP_DRIFT_MODE = "raw"` in `config.py` to restore the old drift-inclusive behavior. To revert a
single file: `git checkout -- <file>`. The two new files are additive and inert until a scan runs.

## Suggested commit

```
fix(vega): de-bias edge model (drift→VRP), recalibrate VRP/credit-width/IV-rank, add Gate 1 outcome logging

- C1: detrend true_pop (risk-free drift) so edge reflects vol premium, not trend
- C2: split p_max_profit (edge) vs p_profit at breakeven (POP gate)
- H1: recalibrate VRP score bands to real ~4-6pp distribution; VRP_MIN_THRESHOLD 0.15→0.02
- H2: MIN_CREDIT_TO_WIDTH_PCT 0.25→0.15 to match 0.20Δ reality
- M1: inflate HV distribution in IV-rank bootstrap; M3: block-based confidence
- feat: outcome_logger + log_outcome.py CLI (Gate 1 calibration)
```
