# VEGA — Claude Code handoff (2026-07-16)

Hand this file to Claude Code running on the tower (where live Yahoo + git work). It summarizes the
current state, what's blocked, and the ordered next steps. Built in a remote Cowork sandbox that could
NOT reach live Yahoo or write git — hence the handoff.

## Current state (all on disk, compiles, NONE committed yet)
- Cockpit `vega_app.py` (~1470 lines): dark UI, 4 nav tabs (Today / Open / History / Lottery), Today =
  verdict strip + hero + expandable leaderboard (rows open full detail inline). Strategy-aware: bull-put,
  bear-call, iron-condor (geometry/payoff/breakevens/EV/reconcile per type). Sort + max-loss filter.
  Gambler-edge EV column. Per-row reconcile ✓/⚠. Freshness stamp. Criteria + news-validation panel per trade.
  `needs_validation` → amber "verify" flag on bear-call/iron-condor.
- `strategies.py`: single source of truth for per-strategy criteria + news validation. `evaluate()` returns
  criteria list + news_check. Self-test 8/8 (`python strategies.py`).
- `multi_strategy.py`: LIVE bear-call + iron-condor generators (reuse edge_calculator + strategies). Every
  emitted trade `needs_validation=True`.
- `data/fetcher.py`: NEW `get_call_options_chain()` (yfinance calls, BS greeks) — the previously-missing calls path.
- `main.py`: scan loop calls `multi_strategy.scan_extra()` (ADDITIVE; bull-put path untouched) and enriches
  bull-put trades with strategies.evaluate criteria/news.
- `config.py`: `ENABLED_STRATEGIES` = bull_put_spread + bear_call_spread + iron_condor.
- `lottery_scanner.py`: single-call speculative scanner (live via get_call_options_chain; `--demo` for sample).
- `seed_demo.py`: offline demo seeder (criteria-compliant data from strategies.py) → Launch_VEGA_BETA.bat.
- `verify_numbers.py`: artifact reconcile + freshness check.
- `Launch_VEGA.bat` = LIVE launcher; `Launch_VEGA_BETA.bat` = offline demo review.

## BLOCKERS to clear first (on the tower)
1. `del .git\index.lock`  (stale lock from a crashed session — blocks all commits).
2. Delete leftover empty temp files: `vega_app_new.py`, `_probe_new.py`.
3. Commit the work:
   ```
   git add vega_app.py main.py config.py data/fetcher.py strategies.py multi_strategy.py \
           lottery_scanner.py seed_demo.py verify_numbers.py Launch_VEGA.bat Launch_VEGA_BETA.bat
   git commit -m "feat(vega): multi-strategy live engine + strategy-aware cockpit + criteria/news validation"
   ```
   (Leave unrelated pre-existing modified files as-is unless you know they belong.)

## NEXT STEPS (ordered)
1. **Validate the live calls path** (the whole point of the verify flags). Run `python main.py` on a live
   market day → open `logs/scan_latest.json` → confirm bear-call and iron-condor objects exist. Open the
   cockpit, expand one of each, and **spot-check strikes / credit / greeks against a real broker chain**.
   If they match, remove the `needs_validation=True` in `multi_strategy.py` (or gate it behind a config flag).
2. **True-POP for call side**: `multi_strategy._pop_above()` uses a *symmetric-mirror approximation* of the
   downside `calculate_true_pop`. Validate this against realized frequency once outcomes log; if biased, add a
   proper upside/`between` probability to `edge_calculator`.
3. **Fuller mockup tabs (optional, per Josh's latest note)**: the GPT mockup has more surfaces than the app
   (Dashboard, Leaderboard, Trades, Scanner, Analytics, Learn + research sub-tabs Greeks/Volatility/Option
   Chain/Technical/News/History/Scanner). The app currently uses 4 tabs + an expandable drawer (the approved
   "marriage" design). Decide with Josh whether to expand to the full tab set; several need new data wiring
   (live Greeks, option chain table, IV term structure).
4. **Gate 1 / ORATS backtester** (still gated on Josh's paid-data decision) to flip the edge tier
   Provisional → Validated. See build doc §0.5 A3.
5. Confirm the scheduled auto-paper task is still running (`vega_scheduler_status.ps1`) so `vega_outcomes.jsonl`
   keeps growing (it had ~12 entries; the cron had stalled once in May).

## Companion docs in-repo
- `VEGA_Dashboard_BuildDoc_v3.1_2026-07-16.md` — architecture + signal-confidence tiering decisions.
- `VEGA_MultiStrategy_Engine_Spec_2026-07-16.md` — strategy generation detail + test plan.
- `VEGA_Dashboard_UX_Spec_2026-07-15.md` — perceptual UX rationale.

## Known environment note
Everything above was authored in a sandbox with a flaky file mount (stale reads, blocked deletes) and no
Yahoo/network to Yahoo or git write. On the tower none of those constraints apply — run, validate, commit natively.
