# VEGA Dashboard — Build Doc v3.1

**Date:** 2026-07-16
**Owner:** Josh Adams
**Target executor:** VS Code / Claude Code (agentic, in-repo)
**Repo root:** `options_intelligence/`
**Primary file to change:** `vega_app.py` (the live local cockpit)
**Supersedes/merges:** ChatGPT "VEGA Product Design Specification v3.0" (product/screen model) + `VEGA_Dashboard_UX_Spec_2026-07-15.md` (perceptual rules) + this doc's **signal-confidence corrections**.

---

## 0. How to read this document

This is a **build doc**, not a philosophy doc. It assumes the reader (a coding agent or a developer in VS Code) has the repo open and will make edits. Every work item names:

- **What** to build,
- **Where** in the codebase it lives (real file + function/anchor),
- **Why** (the principle or the audit finding it satisfies),
- **Done-when** (an acceptance check you can verify).

The product vision (the five-screen model, the "find/rank/explain/educate — never execute" boundary) comes from the v3.0 spec and is **accepted as-is**. The perceptual rules (tabular figures, one-color-per-meaning, gate dots, confidence gating) come from the 2026-07-15 UX spec and are **also accepted**. What this doc adds — and what must not be skipped — is section **3: Signal-Confidence Gating**, which reconciles the confident green UI with the engine's actual validation state.

---

## 0.5 Resolved assumptions (decided 2026-07-16 — these are no longer open)

Three architectural questions were open in the first draft. All are now resolved against the actual code and are binding for implementation.

**A1 — Data source: the cockpit switches to the main-engine artifact (single source of truth).**
Evidence: `vega_app.py` currently calls `vega_candidates.build_candidates()`, a *separate, lighter* scanner whose `score` is a local premium-efficiency/delta/liquidity ranking (`vega_candidates.py:195`), with `pop_implied = 1 − delta` and `vrp_pp`. It computes **no** `edge_score`, `component_breakdown`, or `true_pop` — those exist only in `main.py`'s engine path (`edge_calculator.calculate_edge_score` → `main.py:550-551`; `true_pop` at `544`). Running two engines means the cockpit and the tipsheets/emails can disagree, and the "no black box" Scanner Diagnostics tab is impossible without `component_breakdown`.
**Decision:** `main.py` persists a stable **`logs/scan_latest.json`** containing full qualified-trade objects (`edge_score`, `component_breakdown`, `true_pop` + `confidence` + `drift_mode`, `pop_implied`, `gates`). The cockpit reads that as its canonical board. Retire `vega_candidates`' independent `score`. Keep the fast live "Rescan" button, but a fast yfinance-only refresh lacks `edge_score` and therefore renders **PROVISIONAL/amber** (§3) — the official board is always the engine artifact.

**A2 — Board POP: headline engine `true_pop`, not `1 − delta`.**
The engine already computes both (C2 fix): `pop_implied` (market's implied) and `true_pop` (drift-removed historical frequency at breakeven, with a `confidence` field). **Decision:** show `true_pop` as the headline POP, `pop_implied` beside it, and **`edge = true_pop − pop_implied`** as the actual decision metric (the core thesis of `edge_calculator`). Never label raw `1 − delta` as "POP" unqualified. `true_pop.confidence == LOW` → render amber under the §3 tier rules.

**A3 — Gate 1 = hybrid (backtest gates, live confirms).**
Gate 1 is already defined in-repo as the outcome-logging flywheel (`outcome_logger` → `vega_outcomes.jsonl` → `log_outcome.py report`); status is zero outcomes, cron stalled since 2026-05-20 (`VEGA_FULL_AUDIT_2026-07-07.md`). **Decision (objective flip criterion for `SIGNAL_TIER`):**
- **Flip PROVISIONAL → VALIDATED** when an **ORATS historical backtest (2022–2025)** of the edge model passes: (a) calibration — realized win-rate within **±8 pp** of modeled `pop` across buckets; (b) directionality — higher `edge_score` bucket → higher realized expectancy (Spearman rank corr > 0); (c) top `edge_score` tercile expectancy **> 0 net of a slippage + commission haircut** (add the haircut before believing any P/L — `VEGA_DEVELOPER_HANDOFF_2026-07-06.md:257`).
- **Live safety net:** even after the flip, the diagnostics tab shows a **`live-confirmation: k/30`** counter fed by `vega_outcomes.jsonl`. If live realized calibration diverges beyond ±8 pp once `k ≥ 30`, the tier **auto-reverts to PROVISIONAL** (amber) and logs why. Encode this as a function, not a manual toggle:

```python
# vega_app.py — Gate 1 tier resolution (replaces the static SIGNAL_TIER['vrp'] literal)
def gate1_tier(backtest_passed: bool, live_calib_pp: float | None, n_live: int) -> str:
    if not backtest_passed:
        return "PROVISIONAL"
    if n_live >= 30 and live_calib_pp is not None and abs(live_calib_pp) > 8.0:
        return "PROVISIONAL"   # live fills contradict the backtest — revert to amber
    return "VALIDATED"
```

Record each backtest/live-confirmation result (date, n, calibration error, decision) in `VEGA_FULL_AUDIT_*` and surface it in the diagnostics tooltip. One function, one paper trail.

---

## 1. Product context (why VEGA looks the way it does)

VEGA is a **market opportunity engine**, not a broker. Its job is to *find, rank, explain, and educate* — and to end at the moment the user understands the trade. Execution lives elsewhere. Any feature that doesn't help discover a better opportunity, explain why it ranked where it did, or raise justified confidence does not belong on the primary workflow. Keep this test in the PR description for every change.

The interface target is **Bloomberg Terminal calm + Apple HIG clarity + Garmin-aviation legibility**. Dark, quiet, one anchor number per view, progressive disclosure. The information spine never changes:

```
Market  →  Best Opportunities  →  Trade Summary  →  Trade Detail  →  Research
(worth trading today?)   (what's best?)   (why?)   (what are the risks?)   (show me everything)
```

Every click adds **depth**, never **complexity**.

---

## 2. Current state of the codebase (what you're building on)

The live cockpit is **`vega_app.py`** — a stdlib-only local web app (no pip installs), served from `127.0.0.1:8765`, rendering HTML/CSS/inline-SVG from Python. Do not introduce a frontend framework; the constraint is deliberate (portability, zero-build, runs by double-clicking `Launch_VEGA.bat`).

Relevant modules:

| File | Role |
|---|---|
| `vega_app.py` | The cockpit UI + local HTTP server. **This is where 90% of this doc's work lands.** |
| `main.py` | Scan orchestration / pipeline entry. |
| `analysis/edge_calculator.py` | VRP, true-POP, edge-points, edge-score. **Source of the signals the UI displays.** |
| `analysis/strike_validator.py` | Gate checks (the 8 gates). |
| `analysis/synthesizer.py` | Assembles candidate objects the UI reads. |
| `analysis/outcome_logger.py` (`ol`) | Closed-trade outcomes → the History/confidence stats. |
| `paper_desk.py` | `compute_stats`, `_latest_candidates` — feed the board. |
| `output/candidates/` | JSON candidate files the UI loads. |

Key structures already in `vega_app.py`:

- `GATE_ORDER` — the canonical 8 gates in fixed order: `IV-Rank, Delta cap, OTM buffer, Credit/Width, Min credit, Liquidity, POP, DTE`. **Use this exact order everywhere** (dots, tooltips, diagnostics) so the pattern is learnable.
- `GRADE_RANK` / `GRADE_CLASS` — STRONG / SOLID / MARGINAL / SKIP verdicts.
- `_scan_status` — `{running, msg, at}`, already tracks live-scan state for the rescan spinner.

**Engine validation state (critical — read `analysis/edge_calculator.py` before touching any volatility UI):**

- **C1 (drift) — FIXED in code.** `calculate_true_pop(...)` now removes realized drift (log-return demean + `TRUE_POP_DRIFT_MODE`, default `risk_free`). The old bug (true-POP dominated by the sample period's directional drift) is closed. Historical true-POP is now defensible *for the zero/risk-free drift assumption* — surface that assumption in the UI, don't hide it.
- **H1/H2 (VRP band + delta/credit-width calibration) — recalibrated but NOT yet gate-validated.** `calculate_edge_score(...)` VRP bands were recalibrated to the real S&P VRP distribution (H1 FIX comment in code), but **Gate 1 backtest sign-off has not been recorded**. Until it is, the *edge score* and any "volatility edge: positive" claim are **provisional**, not proven.

This is the crux of section 3. The UI must not render a provisional signal with the same visual confidence as a validated one.

---

## 3. Signal-Confidence Gating (the correction — do this first, it's load-bearing)

**Problem being fixed:** the current/mockup UI paints "Positive volatility edge" and "Volatility Edge: Positive" as **solid green passed gates**, and prints VRP (IV−RV) as fact. But per the audit, the edge model is only *recalibrated*, not *validated* (Gate 1 unrecorded). A product whose defining promise is **"no black box"** cannot show an unvalidated signal at full confidence. This contradicts its own philosophy and over-promises to the user.

**Rule (make this a first-class concept in the code):** every displayed signal carries a **confidence tier**, and the tier drives its color and label. Add a single source of truth:

```python
# vega_app.py — add near GATE_ORDER
# Confidence tiers for any displayed signal/score.
# VALIDATED  -> green,  shown as fact
# PROVISIONAL-> amber,  shown as "estimate / under calibration"
# UNPROVEN   -> grey,   shown but explicitly not to be trusted yet
SIGNAL_TIER = {
    "vrp":              gate1_tier(...),  # resolved by §0.5 A3 hybrid, not a static literal
    "edge_score":       gate1_tier(...),  # same source of truth as vrp
    "true_pop":         "VALIDATED",    # C1 fixed; valid under stated drift assumption
    "implied_pop":      "VALIDATED",
    "liquidity":        "VALIDATED",
    "iv_rank":          "VALIDATED",    # if from real IV history; else PROVISIONAL (see data/iv_history)
    "credit_to_width":  "VALIDATED",
}
TIER_STYLE = {
    "VALIDATED":  {"class": "tier-ok",   "note": ""},
    "PROVISIONAL":{"class": "tier-prov", "note": "estimate · under calibration"},
    "UNPROVEN":   {"class": "tier-unp",  "note": "not yet validated"},
}
```

**Color mapping (do not violate — it's the whole point):**

- `tier-ok` → the green semantic (`#00C97A`). Fact.
- `tier-prov` → the yellow semantic (`#F0B429`), which per the palette means **"use additional judgment"**, *not* negative. This is exactly what yellow was reserved for.
- `tier-unp` → muted grey ink, with a tooltip.

**Where it must show up:**

1. **Overview tab / quality gates:** the "positive volatility edge" gate renders **amber** with a tooltip: *"VRP edge recalibrated; awaiting Gate 1 backtest sign-off (see Scanner Diagnostics)."* Not green.
2. **Volatility tab:** VRP (IV−RV) value shown in amber with an "estimate" pip until Gate 1 is recorded. Include the drift assumption: *"True-POP computed with zero/risk-free drift (C1 fix)."*
3. **Score composition / Scanner Diagnostics:** show the VRP/edge sub-scores with a small "provisional" flag and a one-line status: `Gate 1: not yet recorded`. This is the honest, trust-building move the "no black box" principle demands.
4. **Confidence Scorecard:** wire `Signal Strength / Historical Confidence / Data Completeness` to real numbers (see §7), and let low values read honestly — a 58% is more trustworthy than a fake 95%.

**Flip-to-green procedure — now automated (§0.5 A3).** The tier is computed by `gate1_tier(backtest_passed, live_calib_pp, n_live)`, not hand-toggled: it returns VALIDATED once the ORATS backtest passes (±8 pp calibration, positive rank-correlation, top-tercile expectancy > 0 net of costs), and auto-reverts to PROVISIONAL if live fills (`k ≥ 30`) contradict it beyond ±8 pp. Record every result (date, n, calibration error, decision) in `VEGA_FULL_AUDIT_*` and surface it in the diagnostics tooltip. One function, one paper trail.

**Done-when:** no signal that hasn't cleared its validation gate renders in the green semantic anywhere in the app; each provisional/unproven signal has a tooltip pointing to its diagnostics; flipping a tier in `SIGNAL_TIER` visibly recolors every place that signal appears.

---

## 4. Screen architecture — mapping the v3.0 model onto `vega_app.py`

The v3.0 spec defines five screens. `vega_app.py` today is essentially **Leaderboard + Trade Detail fused into one page**. Build the five screens as **routes/views within the same stdlib server** (query-param views, e.g. `/?view=dashboard`, `/?view=board`, `/?trade=<id>&tab=overview`). No SPA router needed.

| v3.0 Screen | One question | Build target | Status vs today |
|---|---|---|---|
| **1. Market Dashboard** | "Is today worth trading?" | New top view: regime, premium environment, scanned/analyzed/qualified/elite counts, hero "top setup". | **New** — mostly missing today. |
| **2. Leaderboard** | "What's best?" | Existing board, restyled per §5 (4 column groups, gate dots, priority hero, tabular nums). | **Exists**, needs restyle. |
| **3. Trade Detail** | "Explain everything about one." | Fixed header (ticker/strategy/score/one-liner) + tabbed body. | **Partially exists** (row expand). Formalize as a view with tabs. |
| **4. Research tabs** | "Show me every angle." | Tabs: Overview, Payoff, Greeks, Volatility, Option Chain, Technical, News, History, Scanner Diagnostics. | Payoff/metrics exist; most tabs new or stubbed. |
| **5. History** | "Can I trust it?" | Closed-trade outcomes, win rate, avg ROC/duration/drawdown, sample-size progress. | `outcome_logger` data exists; view needs building. |

**Empty/negative-day states are first-class (v3.0 under-specifies these — build them deliberately):**

- **Poor day:** `0 Elite, 0 Great, below-average premium` must render a calm, explicit *"Not a strong day to sell premium — X of Y spreads qualified"* message. The product earns trust by telling the user **not** to trade.
- **Thin history:** any stat with `n_closed < 10` renders grey with an `n=<k> — not yet meaningful` tooltip and a "closed k of 30" progress bar (already flagged as code finding S2).
- **Pending scan / stale data:** amber banner, timestamped, using `_scan_status`.

---

## 5. Leaderboard & Trade Detail restyle (from the 2026-07-15 UX spec — condensed to build items)

These are accepted verbatim from `VEGA_Dashboard_UX_Spec_2026-07-15.md`. Implement in this order (1–2 are ~30 min of CSS and give the biggest feel change):

1. **Tabular figures, right-aligned numerics.** Every numeric `<td>` → `text-align:right; font-variant-numeric:tabular-nums;`. Text columns stay left. *(Biggest single scannability win.)*
2. **One meaning per color.** Reserve green/red **exclusively** for realized outcome & live P/L. Price, credit, POP become neutral ink. Combine with §3 tiers (amber = provisional signal). Double-encode everything: never hue alone.
3. **8-dot gate matrix** in `GATE_ORDER` sequence — filled = pass, hollow = fail — replacing the comma-joined failure text. Keep the `6/8` count as a label; full names in tooltip.
4. **Group the 15 columns into 4 labeled clusters:** Trade (what is it?) · Edge (worth taking?) · Greeks/Risk (what am I holding?) · Quality (did it pass?) · Action (take it). Group headers + subtle vertical separators.
5. **Priority = the hero number:** largest type, a 0–100 micro-bar behind the value, and a ▼ glyph in the header to show it's the sort key.
6. **Hero card** for the single top setup above the grid — bigger payoff, plain-English one-liner (`"SPY 722/721, 38 DTE — 80% POP, 19% ROC, passes 6/8 gates"`).
7. **Confidence layer:** progress bar for "closed k of 30"; grey premature stats; POP shown as an estimate (pip/shaded bar), never a hard fact.
8. **Micro-interactions** (stdlib CSS + vanilla JS only): row hover lift, Log toast + green pulse, rescan spinner wired to `_scan_status`, payoff hover readout, tasteful win pulse on **closed wins only**. Gate all motion behind `prefers-reduced-motion`.
9. **Type scale** (hero/Priority 20px, cell 14px, sub-line 11px, group headers 10px uppercase), single emphasis weight (600) used once per row, sticky header + group header, contrast ≥ 4.5:1 (bump `#999` → `#6b7280`).

**Leaderboard reordering — override the v3.0 "live continuous reorder" idea.** A board that auto-reshuffles while the user is reading row 4 destroys focus. Build **snapshot updates with a visible "New scan available — refresh" pill**, not live auto-reorder. This also matches how the scan pipeline actually runs (discrete scans, not a stream). Animate the reorder **only** on explicit refresh, 300–500 ms, so the change is perceived rather than jarring.

---

## 6. Trade Detail tabs — content contract per tab

Fixed header (never scrolls): ticker · strategy · score · one-line rationale. Body is tabbed. Each tab answers exactly one question. Data sources noted so the agent knows where to pull.

- **Overview** — *why did this rank here?* Quality gates (with §3 tiers), score composition, trade profile, market quality, plain-English summary. Source: candidate JSON + `edge_calculator` breakdown. No raw math unless expanded.
- **Payoff** — *visualize the risk.* Expiration payoff (existing shaded SVG), break-even, max profit/loss, current-price marker, probability region. Hover readout of P/L at cursor price.
- **Greeks** — *translate to English*, with an advanced "show the number" toggle. `Theta=31` → *"≈ $31/day from time decay"* **and** the raw value on toggle. Don't force plain-English on advanced users.
- **Volatility** — *is vol expensive?* IV, HV, IV Rank, IV Percentile, term structure, VRP. **VRP + edge in amber (§3)** with the drift-assumption note. Source: `edge_calculator.calculate_vrp`, `data/iv_history`.
- **Option Chain** — *transparency.* Selected contracts: bid/ask/mid/volume/OI/spread width. Pure market data, no scoring, no opinions.
- **Technical** — *context, not primary evidence.* Price, support/resistance, MAs, RSI, trend, expected-move overlay. Source: `data/technicals.py`. Label clearly as secondary.
- **News** — *prevent surprises.* Upcoming earnings, FDA/dividends, headlines, econ releases. Source: `data/news.py`, `data/fundamentals.py`. If a feed is absent, **omit or label — never fake** (existing app convention; keep it).
- **History** — *can I trust it?* Similar past setups: win rate, avg ROC, duration, drawdown, with sample-size progress. Source: `outcome_logger` / `compute_stats`. Confidence grows visibly with n.
- **Scanner Diagnostics** — *no black box.* Exact score composition (Market/Premium/Liquidity/Risk/Historical/Technical → Final), **plus the Gate 1 validation status line** from §3. This is VEGA's defining trust feature — make it the best tab, not an afterthought.

**Define what a Score means (v3.0 omits this — do not ship without it).** Put an anchored definition in Overview + Diagnostics: state precisely whether e.g. `77` is a percentile of analyzed spreads, an expected win-rate band, or a weighted composite — and show the component weights. An unanchored number invites users to invent their own meaning.

---

## 7. Data wiring checklist (make the confidence numbers real)

The Confidence Scorecard and score composition must read live values, not placeholders:

- **Signal Strength** ← edge-score / gate-pass ratio for the candidate.
- **Historical Confidence** ← function of `n_closed` from `outcome_logger` (low n → low, honestly).
- **Volatility Confidence** ← IV-history completeness for that ticker (`data/iv_history/`).
- **Data Completeness** ← fraction of feeds present (news/technicals/fundamentals) for that candidate.
- **Score composition (Market/Premium/Liquidity/Risk/Historical/Technical)** ← the real sub-scores from `edge_calculator` / `strike_validator`, not hardcoded 18/20-style constants.

**Done-when:** no number on the scorecard or composition panel is a literal in the template; each traces to a function call, and thin data visibly lowers the displayed confidence.

---

## 8. Guardrails (keep VEGA VEGA)

- **No execution, ever.** No order placement, no "buy" button, no money movement. LOG/CLOSE are paper-desk journal actions only. Keep this explicit in code comments so no future contributor blurs the line.
- **No framework creep.** Stdlib server + inline CSS/SVG + vanilla JS. If a change seems to "need" React, it's the wrong change for this file.
- **Never fake a feed.** Missing data is omitted or amber-labeled, not invented. (Existing convention — preserve it.)
- **The philosophy test** in every PR: *does this help discover a better opportunity, explain a ranking, or raise justified confidence?* If no → cut it or defer to a future (isolated) portfolio/execution module.

---

## 9. Build order (phased, each phase independently shippable)

**Phase 0 — Signal-confidence gating (§3).** Add `SIGNAL_TIER`/`TIER_STYLE`, recolor VRP/edge to amber, add Gate-1 status line to diagnostics. *Load-bearing; do first.* Small diff, high trust payoff.

**Phase 1 — Readability pass (§5.1–5.2).** Tabular nums + right-align + one-meaning-per-color. ~30 min CSS, biggest immediate feel change.

**Phase 2 — Board structure (§5.3–5.6).** Gate dots, 4 column groups, Priority hero + micro-bar, hero card, snapshot-refresh pill.

**Phase 3 — Confidence & History (§5.7, §6 History tab, §7).** Progress bars, grey premature stats, wire real scorecard numbers, build History view.

**Phase 4 — Screen 1 Market Dashboard (§4).** Regime, premium environment, counts, negative-day state.

**Phase 5 — Trade Detail tabs (§6).** Formalize tabbed view; fill Volatility/Greeks/Chain/Technical/News/Diagnostics per the content contract.

**Phase 6 — Micro-interactions & hygiene (§5.8–5.9).** Hover lift, toasts, spinner, payoff hover, type scale, sticky headers, `prefers-reduced-motion`.

Each phase: run the app locally (`python vega_app.py` / `Launch_VEGA.bat`), verify against the Done-when checks, commit.

---

## 10. Acceptance checklist (definition of done for v3.1)

- [ ] No provisional/unproven signal renders in the green semantic anywhere; VRP/edge are amber with tooltips until Gate 1 is recorded.
- [ ] Flipping one `SIGNAL_TIER` entry recolors every occurrence of that signal.
- [ ] All numeric columns are right-aligned tabular figures; green/red used only for realized/live P/L.
- [ ] Gates shown as an 8-dot matrix in `GATE_ORDER` order with a `k/8` label + tooltip.
- [ ] Columns grouped into the 4 labeled clusters; Priority is the visual anchor with a sort glyph.
- [ ] Board updates via snapshot + "new scan" pill, not live auto-reorder.
- [ ] Every stat with `n_closed < 10` is greyed with a sample-size tooltip + progress bar.
- [ ] Confidence Scorecard and score composition read real values (nothing hardcoded).
- [ ] A concrete definition of "Score" appears in Overview and Diagnostics.
- [ ] Negative/empty-day state renders calmly and tells the user not to trade when appropriate.
- [ ] No execution surface added; stdlib-only constraint preserved; no faked feeds.
- [ ] All motion behind `prefers-reduced-motion`; text contrast ≥ 4.5:1.

---

## 11. Companion files in-repo (for the agent's context)

- `VEGA_Dashboard_UX_Spec_2026-07-15.md` — full perceptual rationale for §5 (Treisman/Miller/Von Restorff/Tufte references).
- `vega_dashboard_redesign_mockup.html`, `vega_dashboard_v3.html`, `vega_payoff_redesign.html` — visual targets; lift exact styles.
- `VEGA_Code_Review_2026-07-15.md`, `VEGA_FULL_AUDIT_2026-07-07.md` — engine findings (S1/S2 map to §5.7 and §7; C1/H1/H2 map to §3).
- `analysis/edge_calculator.py` — read before touching any volatility UI (drift/VRP truth).

*End of build doc v3.1.*
