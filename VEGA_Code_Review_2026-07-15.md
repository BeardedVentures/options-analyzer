# VEGA — Comprehensive Code Review (2026-07-15)

**Scope:** the code that makes up the running VEGA system — the scanner (`main.py`,
`analysis/edge_calculator.py`, `analysis/strike_validator.py`), the candidate viewer
(`vega_candidates.py`), the paper desk (`paper_desk.py`, `analysis/outcome_logger.py`),
the live cockpit (`vega_app.py`), the data layer (`data/fetcher.py`), and `config.py`.
Read line-by-line against the source, cross-checked against the 2026-07-06 and 2026-07-07
audits so this complements rather than repeats them.

**One-line verdict:** the code is clean, well-annotated, and the 07-06/07-07 fixes (C1/C2/H1/H2/M1/M3)
are all present and internally consistent. What's new here — findings the earlier audits did **not**
cover — is that the system now runs **three different probability-of-profit models** that don't agree,
the scanner feeds **calendar DTE into a trading-day return model**, and the composite score **double-counts
the volatility-risk-premium signal**. None are fatal; all three change which trades rank highest and how
much confidence the numbers deserve. Fix the POP unification and the DTE unit first.

The earlier audits were about *why the flywheel was stalled* (data accrual, CI). This one is about
*whether the math the flywheel measures is coherent*. The dashboard screenshot shows the paper desk is now
live (117 candidates, 1 closed trade), so the accrual blocker has broken — which makes model coherence the
thing that matters next.

---

## 1. What's healthy (verified against source)

- **The edge model detrends correctly.** `calculate_true_pop` works in log-return space, demeans, and adds
  back a risk-free drift (`config.py:261`, `edge_calculator.py:114-127`). The C1 fix is real — the statistic
  now measures dispersion, not the sample period's trend.
- **Two probabilities are cleanly separated (C2).** `main.py:406-431` computes `p_max_profit`
  (P > short strike, used for edge scoring against `1−|delta|`) and `p_profit` (P > breakeven, the real POP
  the `MIN_PROBABILITY_OF_PROFIT` gate uses). This distinction is correct and many retail tools get it wrong.
- **The ledger is crash-safe.** `outcome_logger._write_all` serializes to a temp file then `os.replace`
  (`outcome_logger.py:59-67`) — an interrupted write can't truncate the ledger. This directly fixes the
  failure mode that corrupted `scan_log.json`.
- **Spread economics are sound.** `calculate_spread_metrics` prefers the real long-leg mid, flags
  `spread_invalid` when credit ≥ width, and sizes per risk tier (`edge_calculator.py:426-504`). Max loss,
  break-even, and credit/width all check out.
- **The data layer degrades safely.** Polygon → yfinance fallback, a price-glitch sanity check
  (`fetcher.py:204-224`), and a stale-quote quality filter (`fetcher.py:538-585`). Nothing crashes the scan.
- **Fees are honest.** Robinhood round-trip commission (`0.54 × 2 legs × 2 = $2.16`) is subtracted from every
  paper P/L (`config.py:95-111`, `outcome_logger.py:70-74`), matching the dashboard's "$2.16 round-trip".

---

## 2. New findings (fix these first)

### N1 — Three disagreeing POP models are live at once  *(highest-leverage)*
The system now computes probability-of-profit three different ways, and the number the user acts on depends
on which surface they're looking at:

| Surface | POP method | Source |
|---|---|---|
| Scanner (`main.py`) | Historical detrended dispersion at break-even | `edge_calculator.calculate_true_pop`, `main.py:425` |
| Candidate viewer (`vega_candidates.py`) | Implied only: `1 − \|delta\|` | `vega_candidates.py:161` |
| Live dashboard (`vega_app.py`) | Zero-drift **lognormal** from blended ATM IV/RV | `_model_pop_estimate`, `vega_app.py:258-288` |

The dashboard's "POP score" column (the 80%/78%/71% in the screenshot) is the **lognormal** number, and the
dashboard **ranks trades by it** (`Priority = 65% POP + 35% ROI`, `vega_app.py:291-298`). But the disciplined
scanner gates on the **historical** POP, and the viewer annotates with the **delta** POP. These three can
disagree by 5–15 points on the same spread, because they make different assumptions (historical realized paths
vs. a smooth lognormal vs. the market's risk-neutral delta). Nothing reconciles them.

**Consequence:** the "best" candidate on the dashboard is chosen by a model the scanner doesn't use to qualify
trades. A spread can look like the top pick on the cockpit and be a marginal qualifier in the engine. When you
later check calibration (realized win rate vs. "POP"), it's ambiguous *which* POP you're calibrating.

**Fix:** pick one POP as canonical (the historical break-even POP is the most defensible given the whole VRP
thesis) and show the other two only as labeled reference columns. At minimum, rank the dashboard by the same
POP the scanner gates on, so "top of the board" and "passes the gate" mean the same thing.

### N2 — Calendar DTE is fed into a trading-day return model
`main.py:414` sets `dte_val = short_put.get("dte")`, which `fetcher.py:299` defines as
`(exp_date - today).days` — **calendar** days. That value is passed straight into `calculate_true_pop` as
`expiration_days` (`main.py:422, 427`), where it indexes a walk over **daily close-to-close log returns**
(`edge_calculator.py:135`) — i.e. **trading** days. A 38-calendar-day option (as in the screenshot) is ~27
trading days, so the model rolls a window ~40% too long, which widens the modeled price dispersion and
**understates** the historical POP. That makes the POP gate systematically stricter than intended and biases
`edge_points` conservative.

**Fix:** convert to trading days before the call — `trading_days ≈ round(dte * 252/365)` — or pass calendar
days and resample the return series to calendar frequency. It's a one-line change with a real effect on which
trades clear `MIN_PROBABILITY_OF_PROFIT`. (Note: the dashboard's lognormal model uses `dte/365` with
annualized vol, which **is** correct calendar-time convention — so only the scanner has the mismatch, and
fixing it also brings the two models closer together, reinforcing N1.)

### N3 — The composite score double-counts the VRP signal
`calculate_edge_score` (`edge_calculator.py:268-389`) allots **30 points to VRP** and **25 points to True-POP
Edge** — 55 of 100. But `edge_points` is `p_max_profit − implied_pop` (`main.py:436`), and `p_max_profit`
is the risk-free-drift historical dispersion probability while `implied_pop` is `1 − |delta|`. The gap between
those two *is* the volatility risk premium expressed as a probability. So VRP-in-vol-points and
True-POP-Edge-in-probability-points are two views of the **same underlying phenomenon** (IV richer than RV),
and the model counts it twice. Two correlated components inflate the apparent conviction of names that are
merely rich on one axis.

Meanwhile 45 of 100 points (technical 20, news 10, earnings 5, fundamentals 10) are **not** VRP at all — so a
high-technical, thin-VRP name can cross the 60 threshold on momentum rather than premium richness, which
quietly dilutes the "we harvest VRP" thesis the docstring states.

**Fix:** either collapse VRP + True-POP-Edge into a single premium-richness component (~35 pts) and reallocate,
or keep both but down-weight so the correlated pair can't exceed, say, 40 combined. Document the intended
signal budget so it's a deliberate choice, not an accident of two independently-added components.

---

## 3. Secondary findings

### S1 — "Est net θ/day" is actually short-leg gross theta
`portfolio_strip` computes `utheta += -(r["short_theta"]) * 100 * ct` (`vega_app.py:243-244`) — only the
**short leg's** theta. A bull-put spread's *net* theta is the short-leg decay **minus** the long-leg decay, so
the displayed figure overstates daily decay income (the long put you bought also decays against you). It's
labeled "Est net θ/day" but it's gross. For a $5-wide 0.20Δ spread the long leg is a meaningful fraction, so
this can overstate by 20–40%. Either subtract the long-leg theta (the chain has it) or relabel to
"short-leg θ (gross)".

### S2 — Small-sample stats are displayed with full confidence
With one closed trade the dashboard shows **Win rate 100%**, **Profit factor ∞**, **Calibration +24pp**
(screenshot). These are arithmetically correct and the "of 30" subtitle hints at immaturity, but the
20px bold treatment invites over-reading a sample of n=1. `compute_stats` (`paper_desk.py:70-128`) has no
confidence guarding. Recommend suppressing or greying calibration/profit-factor/expectancy until
`n_closed >= ~10`, and showing a Wilson interval or "n=1 — not yet meaningful" note. (This is also a UI
finding — see the UX spec.)

### S3 — Reprice can silently strand an expired position
`_reprice_open_positions` (`vega_app.py:127-145`) looks up held strikes in a fresh chain fetched with
`get_options_chain(tk, 0, 200)`. Once a held option is past expiration it won't appear in the chain, so
`set_mark` is never called and the position keeps its last stale mark forever with no flag. Low frequency for
a 25–45 DTE book, but worth an "expired — close me" state so the portfolio strip can't quietly drift.

### S4 — `select_best_strategy` is dead code
`edge_calculator.select_best_strategy` (`edge_calculator.py:396-423`) can return `iron_condor`,
`bear_call_spread`, etc., but `main.py:398-399` hard-forces `strategy = "bull_put_spread"` and `config`
enables only that one (correctly noted in the `config.py:59-65` comment). The function is unreachable in the
live path. Harmless, but delete it or gate it behind the roadmap flag so a future reader doesn't assume
multi-strategy selection is active.

### S5 — Broad, silent `except Exception: continue` blocks
The data and reprice paths swallow exceptions to keep scans alive (e.g. `vega_app.py:144-145`,
`fetcher.py` throughout) — the right instinct for an unattended screener. The cost is that a persistent data
problem (a ticker whose chain always errors) is invisible unless you read debug logs. Consider a per-run
counter of swallowed errors surfaced in the scan-complete banner ("117 candidates · 3 names errored") so
silent degradation becomes visible without changing the fail-open behavior.

### S6 — Security carryover (still valid from prior audits)
Live API keys sit in plaintext `.env` inside a cloud-synced folder; JARVIS ingest is plain HTTP over the LAN.
Fine on a trusted tailnet, not off it. Rotate periodically. Unchanged from 07-07.

---

## 4. Architecture notes

- **Two parallel dashboards exist.** `vega_app.py` (the live stdlib server, the screenshot) and
  `paper_desk.py::_dash_html` (a static one-page render). They duplicate the stat-card and table logic with
  slightly different formatting and column sets. This is drift waiting to happen — a fix to one (e.g. the θ
  relabel in S1) won't reach the other. Consider extracting the shared render helpers
  (`stat_cards`, payoff SVG, row formatting) into one module both import.
- **The candidate JSON is the contract between engine and UI.** `vega_candidates.build_candidates` writes the
  JSON; `vega_app` and `paper_desk` read it. That's a clean seam. Worth a tiny schema/version stamp in `meta`
  so a UI reading an older file fails loudly rather than `KeyError`-ing mid-render.
- **`config.py` is genuinely the single source of truth** and the inline fix-annotations (H1/H2/M1/C1) are
  excellent institutional memory. Keep that discipline.

---

## 5. Priority order

1. **Unify POP (N1)** — make the dashboard rank by the same POP the scanner gates on; demote the other two to
   labeled reference. Highest leverage: it aligns "what looks best" with "what qualifies."
2. **Fix the DTE unit (N2)** — convert calendar → trading days before `calculate_true_pop`. One line; changes
   which trades pass the POP gate.
3. **De-duplicate the score (N3)** — collapse or cap the VRP + True-POP-Edge pair; document the signal budget.
4. **Relabel/fix net theta (S1)** and **guard small-sample stats (S2)** — both are quick and both are also in
   the UX spec.
5. **Expired-position state (S3)**, **swallowed-error counter (S5)**, **delete dead strategy code (S4)**.
6. **Shared render module** for the two dashboards; schema version stamp on candidate JSON.

None of these block paper trading. N1/N2/N3 should be settled *before* you read much into calibration, because
they determine what the calibration is measuring.

---

## 6. Verification notes

Cross-checked directly against source: C1 detrend path (`edge_calculator.py:114-161`) ✓; C2 dual-probability
split (`main.py:406-431`) ✓; calendar-DTE definition (`fetcher.py:299`) fed into trading-day walk
(`edge_calculator.py:135`) ✓ — this is the N2 mismatch, confirmed; three POP call sites
(`edge_calculator.calculate_true_pop`, `vega_candidates.py:161`, `vega_app.py:258`) ✓; composite weights
sum to 55 VRP-correlated / 45 other (`edge_calculator.py:298-372`) ✓; atomic ledger write
(`outcome_logger.py:59-67`) ✓; θ sign in `portfolio_strip` (`vega_app.py:243`) ✓; fee math
($0.54 × 4 = $2.16) ✓ against the screenshot.

*Not financial advice. VEGA is an educational screener; every trade decision and order is the user's own.*
