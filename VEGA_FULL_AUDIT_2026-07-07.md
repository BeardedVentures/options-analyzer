# VEGA — Full System Audit (2026-07-07)

**Scope:** whole-system audit of the `options_intelligence` scanner (VEGA), its model, its
runtime/CI state, and JARVIS integration. Verified against live code, git history, `scan_log.json`,
`config.py`, `.github/workflows/scan.yml`, and the `data/iv_history` cache — not just the handoff docs.

**One-line verdict:** the *code* is in good shape (all 07-06/07-07 fixes are committed and internally
consistent), but the *system* is stalled at the data layer: real IV history never accrued, the daily
cron has not run since May 20, and Gate 1 has zero logged outcomes. VEGA is currently **data-accrual-gated**,
not model-gated or feed-gated. Fix accrual first; only then is threshold tuning meaningful.

---

## 1. What's actually healthy (verified)

- **All audit fixes are committed and match `origin/main`.** `git log` confirms `1480e8c` (C1/C2/H1/H2/M1/M3),
  `cbc3a89` (execution-cost model + broad-index cap + IV cache), `7521d3b` (CI health checks),
  `3d4c108` (near-miss diagnostics), `8ba9b5d` (session doc). Working tree is clean except one stray
  `.gitignore` edit (see §3, O4).
- **Model config is internally consistent with the fixes.** `VRP_MIN_THRESHOLD=0.02`,
  `MIN_CREDIT_TO_WIDTH_PCT=0.15`, `NARROW_SPREAD_MIN_CREDIT_TO_WIDTH=0.20`, `TRUE_POP_DRIFT_MODE="risk_free"`
  all present in `config.py`. `calculate_true_pop` genuinely detrends (demean + risk-free drift).
- **Gate 1 plumbing works and is wired in.** `main.py:972` calls `record_modeled_trades()` every scan; the
  logger is idempotent and now tracks gross **and** net (commission + slippage) P/L. It's just starved of input.
- **Scanner degrades safely.** Anthropic credit exhaustion falls back to rule-based synthesis; scans still
  complete and produce a tipsheet. No hard failure.

---

## 2. Critical findings (fix these first)

### C1 — IV Rank is 100% APPROX across the entire watchlist
Every file in `data/iv_history/*.json` holds **1 sample**. `calculate_iv_rank` needs
`IV_HISTORY_MIN_SAMPLES=30` before it returns a real percentile; below that it returns the HV-based
approximation labeled `APPROX`. So **every IV-Rank decision VEGA makes today is a proxy**, including the
gate the 07-07 session update identified as the *dominant blocker*. The 10:08 run rejected 5+ names on
"IV Rank … below minimum 45" — all computed from the approximation, not real IV.

**Consequence:** the handoff's central question ("are IV_RANK / EDGE_SCORE thresholds too strict, or
correctly silent?") **cannot be answered yet.** You'd be tuning a threshold against a synthetic input.

### C2 — The workflow YAML was invalid; the daily cron could not run at all [FIXED on disk 2026-07-07]
`scan_log.json` jumps straight from `2026-05-20` to two manual `2026-07-07` runs. Root cause found:
**`.github/workflows/scan.yml` did not parse** — both "Validate … scan outputs" steps were mis-indented
(the `- name:`/`run:` block was nested 10 spaces deep inside the preceding step), producing a YAML
`ScannerError` at line 91. GitHub silently skips a workflow it cannot parse, so no scheduled run fired and the
self-bootstrapping IV history never grew. This is the concrete mechanism behind audit item L1.
**Fixed this session:** both validate steps re-indented to proper sibling steps; the file now parses with
both jobs carrying all 8 steps. *Change is on disk but NOT yet committed — see §5a.*

### C3 — The CI IV-history cache never saved a fresh copy [FIXED on disk 2026-07-07]
`restore-keys: vega-iv-history-` was already present (good — it restores the latest prior cache). The real
defect was the **static primary key** `vega-iv-history-${{ github.ref_name }}`: `actions/cache@v4` skips the
save step on an exact-key hit, so after the first save the cache froze at a 1-sample snapshot and never
persisted new samples. **Fixed this session:** primary key now rotates per run
(`…-${{ github.run_id }}`) with branch-scoped `restore-keys`, so every run loads the newest cache and saves an
updated one. Combined with C2, IV history should finally accrue once the workflow runs.

### C4 — Gate 1 has zero outcomes; the validation flywheel is fully stalled
`logs/vega_outcomes.jsonl` does not exist. `record_modeled_trades` only writes when `qualified_trades` is
non-empty, and recent live runs qualify **0**. So there are no modeled rows, no fills, no closes — the entire
empirical premise ("log 30 closed trades, then judge the edge") has not started. Nothing downstream (POP
calibration, realized-vs-modeled edge, slippage validation) can move until qualifiers exist.

**These four compound into one loop:** no daily runs → no real IV history → IV-Rank gate runs on a proxy and
stays silent → no qualifiers → no Gate 1 outcomes → no way to validate the edge. Breaking C2+C3 is the
highest-leverage move in the system.

---

## 3. Secondary findings

- **H1 — Extreme negative VRP on some names looks like a data glitch.** The 10:08 run logged `Negative VRP
  (-12.0pp)` on one ticker. A −12 vol-point VRP (RV 12pp above IV) is implausible in this regime and smells
  like a stale/garbage yfinance IV or an HV-window artifact. Worth a one-off sanity check on the IV and HV
  inputs for that name; the −1.7 / −2.4pp readings are believable low-vol, −12 is not.
- **M1 — `CLAUDE_MODEL = "claude-sonnet-4-6"` is a suspicious pin.** Combined with "credits exhausted," it's
  worth confirming the failure is truly billing and not `model_not_found`. Either way synthesis falls back
  safely, but a wrong pin would permanently disable AI narrative even after you top up credits.
- **M2 — Structurally few qualifiers by design.** Single strategy (`bull_put_spread` only) + 13 tickers +
  IV-Rank≥45 gate means a low-VIX bull market will be silent most days. That's *correct risk behavior*, but
  it also means the system produces little of value in the current regime. A second strategy for the opposite
  regime (bear-call, config stub exists) is the real usefulness unlock — but only after the edge is validated.
- **O4 — Stray `.gitignore` edit uncommitted.** The working-tree diff truncates the file and deletes the
  OS/IDE ignore block (`.DS_Store`, `.vscode/settings.json`, `*.swp`). Looks accidental — revert or fix and
  commit so the tree is clean.
- **Security (carryover, still valid).** Live API keys sit in plaintext `.env` inside a cloud-synced folder;
  JARVIS ingest is plain HTTP. Fine on a trusted LAN/tailnet, not off it. Rotate periodically.

---

## 4. Model-gated vs feed-gated? — the corrected answer

The 07-07 session update concluded VEGA is **model-gated** (IV_RANK / EDGE_SCORE dominate rejections, not
missing chains). That's directionally right but incomplete. The precise diagnosis is:

> **VEGA is data-accrual-gated.** The gate that dominates rejections (IV_RANK) is computed on an HV
> approximation because real IV history never accumulated. Until IV Rank is real, you cannot distinguish
> "thresholds are too strict" from "the proxy is miscalibrated" from "the regime is correctly silent."

So the A/B test proposed in the handoff (free vs paid *options* feed) is measuring the wrong axis first. Paid
options data fixes `NO_VALID_SPREAD` / quote quality — real, but secondary. What actually unblocks the core
question is **real IV history** (via reliable daily accrual, or by buying it).

---

## 5a. Fixed this session (on disk, pending commit)
- **Workflow YAML repaired (C2)** — both validate steps re-indented; file now parses, both jobs = 8 steps.
- **IV cache key rotation (C3)** — primary key now `…-${{ github.run_id }}` with branch-scoped restore-keys.
- **`.gitignore`** — stray truncating edit reverted; tree matches committed version.

**Blocker to committing:** a stale `.git/index.lock` (0 bytes) is present and cannot be removed from the
sandbox ("Operation not permitted" — it's on the Windows mount). Josh must clear it and commit locally:
```
cd "C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence"
del .git\index.lock
git add .github/workflows/scan.yml config.py vega_candidates.py VEGA_FULL_AUDIT_2026-07-07.md
git commit -m "fix(ci): repair invalid workflow YAML + rotate IV cache key; add candidate viewer; MIN_DTE 25"
git push
```
(This session changed: `scan.yml` (YAML repair + cache key), `config.py` (`MIN_DTE` 21→25),
and added `vega_candidates.py` + this audit doc.)

## 5. Next best actions (in priority order)

1. **Commit the CI fix above, then prove one run end-to-end (C2).** After pushing, confirm GitHub Actions is
   enabled and the required secrets exist (`TAILSCALE_AUTH_KEY`, `JARVIS_HOST` = JARVIS tailnet IP, plus the
   API keys). Trigger a manual `workflow_dispatch` morning run and confirm: tipsheet written, `scan_log.json`
   appended, and the JARVIS ingest line appears. Until a *scheduled* run lands in the log, treat "daily" as false.

2. **Let IV history accrue (C3 fix now in place).** With the rotating cache key, let it build ~30 trading days
   — **or** short-circuit the wait by buying real historical IV (ORATS / Market Chameleon), which makes IV Rank
   real on day one instead of after six weeks.

3. **Don't tune IV_RANK / EDGE_SCORE yet.** Any tuning now is against a proxy (C1). Revisit thresholds only
   after IV Rank reports `method: HISTORY`. Hold the safety floor (delta cap, OTM buffer, earnings blackout)
   regardless.

4. **Seed Gate 1 (C4).** The instant real scans start qualifying, place even 1–2 small real trades and log
   them (`log_outcome.py fill/close`) so the modeled-vs-real ledger has data. The net-of-cost P/L model is
   worthless until it's compared against actual fills.

5. **Sanity-check the −12pp VRP input (H1)** and confirm the Anthropic failure mode / model pin (M1). Both are
   quick and rule out silent data/AI corruption.

6. **Commit/revert the `.gitignore` change (O4)** to get back to a clean tree.

7. **Only then** run the free-vs-paid *options-feed* A/B from the handoff, and consider the second strategy
   (bear-call) — both are premature until IV Rank is real and Gate 1 has begun.

---

## 5b. New tool: `vega_candidates.py` — the validation viewer (added 2026-07-07)
The strict scanner (`main.py`) shows a blank tip sheet whenever nothing clears every gate, which in a low-vol
regime is most days — leaving nothing to eyeball. `vega_candidates.py` is the complementary lens and the fastest
path to Josh's stated goal: *run a scan and see real strategies, real tickers, real contracts and prices on free
~15-min-delayed data, then visually verify and adjust.*

What it does: pulls the **same** live chains the scanner uses (`data.fetcher.get_options_chain`, honoring the
25–45 DTE window), enumerates real bull-put spreads per ticker, and shows the best ones **even when they fail the
strict gates** — each annotated PASS/FAIL against IV-Rank, delta cap, OTM buffer, credit/width, liquidity, and POP.
Credit is shown two ways: mid-based (what VEGA models) and a conservative sell-bid/buy-ask "natural" fill. It is
read-only — never writes `scan_log.json` or the Gate-1 ledger, never touches `main.py`.

Run it (on the tower, where yfinance works):
```
python vega_candidates.py                       # 25–45 DTE, top 3/ticker, opens HTML
python vega_candidates.py --top 5 --tickers SPY,QQQ,AMD
python vega_candidates.py --delta-min 0.10 --delta-max 0.35 --no-open
```
Output: `output/candidates/candidates_<stamp>.html` (+ `.json`). Green row = passes all gates; red chips = failed
gates. Economics verified against `analysis.edge_calculator.calculate_spread_metrics` on a synthetic chain
(credit, width, max loss, break-even, credit/width, and every gate check out).

**The validation loop this enables:**
1. `python vega_candidates.py` — see the real spreads on offer and which gates they miss.
2. Pick 1–2 you'd actually take; verify the strikes/credit against your broker (delayed quotes are fine for 25–45 DTE).
3. If you place one, log it: `python log_outcome.py fill "<id>" <actual_credit>` … later `... close`.
4. After ~30 closed trades, `python log_outcome.py report` tells you whether the modeled edge/POP survives real fills.

Note: `config.MIN_DTE` was changed 21 → 25 this session to match the stated 25–45 target (so `main.py` and the
viewer agree on the window).

## 6. Verification notes
Cross-checked: commit hashes present in `origin/main` ✓; config values match documented fixes ✓;
`record_modeled_trades` wired into `run_scan` ✓; IV-Rank APPROX path confirmed in `data/technicals.py` ✓;
IV-history sample counts (1 each) and the May 20→Jul 7 log gap read directly from disk ✓. C3 (frozen cache)
is a strong inference from the workflow YAML — confirm on the live Actions cache before treating as certain.

*Not financial advice. VEGA is an educational screener; every trade decision and order is the user's own.*
