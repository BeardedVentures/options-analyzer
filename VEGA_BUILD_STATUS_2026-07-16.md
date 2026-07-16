# VEGA — build status after the Claude Code tower session (2026-07-16)

Answers the `CLAUDE_CODE_HANDOFF_2026-07-16.md` handoff. Blockers cleared, work committed, and the
live path validated on the tower. **The headline: none of the multi-strategy work could run at all.**
Five commits, all local — **nothing has been pushed**.

## The headline

The handoff's step 1 was "run `python main.py`, confirm bear-call and iron-condor objects exist in
`logs/scan_latest.json`, spot-check vs a broker". That was unreachable. `python main.py` was a **silent
no-op**: the sandbox edit had truncated the tail off `main.py`, destroying the `if __name__ == "__main__"`
block that commit `26eb747` still has. It parsed, exited 0, printed nothing, and did nothing.

`Launch_VEGA.bat` — the LIVE launcher — checked no exit codes, so its "engine scan (LIVE Yahoo)" step
did nothing and fell straight through to the cockpit, **which served the previous artifact under a LIVE
banner**. Everything downstream looked healthy because every failure exited 0.

Behind that were four more failures, each individually total and silent:

| # | Bug | Effect |
|---|-----|--------|
| 1 | `multi_strategy` called `technicals.calculate_all()` without `current_iv` (defaults 0.0) | `iv_rank` was **always 0** → the `iv_rank ≥ 35/45` gates could never pass |
| 2 | `strategies.py` matched `up/flat/down`; technicals emits `STRONG_UP\|UP\|NEUTRAL\|DOWN\|STRONG_DOWN` | `"flat"` is never produced → **iron condor could never qualify under any market condition** |
| 3 | Iron condor (4 strikes, no `short_strike`) hit `format(trade.short_strike)` in the tip-sheet template | `UndefinedError` killed `run_scan` **before** `write_scan_latest` → no artifact, ever |
| 4 | `lottery_scanner` called `technicals.compute()` / `news.get_sentiment()` — **neither exists** — behind `hasattr` guards | silently fell back to `{}` forever → `_build_call` returns `None` without `rsi` → "wrote 0 lottery calls" |

Bug 2 also reached the **proven bull-put path**: `main.py:857` enriches bull puts through the same
`evaluate()`, so a `STRONG_UP` stock that legitimately qualified was shown a criteria panel reading
*"Regime fits thesis: FAIL"* — the cockpit contradicting its own recommendation. Gating there is
config-driven, so only the display was wrong.

## Live validation (the handoff's step 1, now actually done)

Live close scan, real Yahoo data, tower:

```
qualified 8 | rejected 45   →  5 bull put, 2 iron condor, 1 bear call
MSFT  Iron Condor       true_pop 0.8941  p_max_profit 0.8814  implied 0.679  edge 72  needs_val True
NFLX  Bear Call Spread  true_pop 0.8035  p_max_profit 0.7991  implied 0.780  edge 60  needs_val True
FCX   Iron Condor       true_pop 0.8432  p_max_profit 0.8199  implied 0.677  edge 58  needs_val True
verify_numbers: 8/8 rows reconcile, age 5 min, exit 0
cockpit: HTTP 200, all 8 trades render, 3 verify flags
lottery: 0 → 12 live calls
```

These are the **first bear-call and iron-condor objects the engine has ever produced.**

## Probability math — the call side was overstating POP

`_pop_above()` fed the upside distance into `calculate_true_pop`, a **downside** function. That is not a
harmless approximation. Under `TRUE_POP_DRIFT_MODE="risk_free"` the series carries a deliberate
`+r/252` drift, so a mirrored downside POP applies that drift *favorably to both sides*.

Measured: **+4.8 POP points** overstated at 30 DTE / 25% vol; **+10.5 points** on a real-shaped 32 DTE
bear call (reported 92.5% where the truth was 82.0%). At the 0.70 `min_pop` gate this manufactures
qualifying trades — a measured 0.7052 passes where the truth 0.6570 fails — and since
`edge_points = true_pop − implied_pop`, it also ranks them higher.

`edge_calculator.calculate_pop_between()` now measures P(price ends inside a signed band) **directly on
the empirical windows** — no symmetry assumption, drift points the one true way. It reproduces the
bull-put path **bit-for-bit** (0.7052 = 0.7052, same 726 windows), so the proven path is numerically
untouched.

Second, compounding bug: `multi_strategy` compared **breakeven**-POP against **strike**-implied POP
(apples to oranges — breakeven is further out), inflating edge on top of the mirror bias. It now follows
the proven bull-put convention: **edge at the short strike, POP gate at breakeven**.

## SECURITY — rotate the NewsAPI key

`requests` embeds the full URL in exception messages, so a NewsAPI 429 carries the live key. Those
strings were persisted verbatim to `logs/scan_log.json` → **a tracked file**.

**The key `9d73fe...8927c` is in git history and appears 23× in `origin/main` on GitHub.**

- I fixed the leak at its chokepoint (`fetcher.redact_secrets`), so it stops *now*.
- I did **not** rewrite history — that is destructive, needs a force-push, and **would not un-publish an
  already-pushed secret.**
- **Rotate the key.** That is the actual remedy. Then decide whether `logs/scan_log.json` should be
  tracked at all.

## Other things fixed

- **Staleness guards never fired.** `verify_numbers` *and* the cockpit both compared tz-aware ET
  timestamps against naive local time → age came out **negative** (`-55 min`), never `> 20`. The cockpit's
  `if age<0: age=0` clamp then rendered that as *"0 min old (within 15-min feed)"*. The guards meant to
  catch exactly the stale-data problem above were structurally incapable of firing.
- **Demo data was indistinguishable from live.** `seed_demo.py` writes fabricated trades to the *same*
  `scan_latest.json` with a current timestamp, and the cockpit never read `session_type`. A demo board
  rendered as a fresh live one. It now says so, loudly.
- **`verify_numbers` was passing rows it never checked.** Iron condors have no `short_strike`, so `width`
  was `None` and the max-loss check was skipped — printing `OK` for a row it never verified. Also `be` was
  computed and discarded, so breakeven was never checked despite the docstring. Now strategy-aware;
  unknown shapes fail loudly. (Negative control: a corrupted IC max-loss is now caught.)
- **A cosmetic template bug could destroy the scan.** `renderer.render` is now wrapped — the tip sheet is
  presentation; `scan_latest.json` is the cockpit's source of truth.
- **`Launch_VEGA.bat`** now halts on scan failure and prompts before showing a board that failed integrity.
- **Lottery quality:** it picked "nearest 0.32 delta" across the whole chain, so when the $400 budget bound
  on a high-priced underlying it surfaced **0.08-delta tickets labelled HIGH conviction**. Now confined to
  the spec's delta band and gated through `strategies.evaluate` (delta/IV-rank/budget/news): 19 → 12, all
  within 0.25–0.42, each carrying a criteria panel.
- `--session` now defaults to the clock (it was `required`, which would have made the launcher's bare
  `main.py` exit 2 even after the entry point was restored).
- `estimate_current_iv` moved to `technicals.estimate_atm_iv` — one IV number for every strategy path.

## Still open

1. **Broker spot-check — needs you.** Every call-side trade still carries `needs_validation=True` and shows
   an amber *verify* flag. I validated the math, the plumbing, and internal consistency; I cannot compare
   strikes/credit/greeks against a real broker chain. Do that on the next market day, then drop the flag in
   `multi_strategy._base()`. **The flags are correct as-is — don't remove them on my say-so.**
2. **True-POP calibration.** The call side is now measured, not mirrored, but it's still *historical
   frequency*, not a validated forecast. Confirm against realized outcomes as `vega_outcomes.jsonl` grows.
3. **NewsAPI is rate-limited (429 on every ticker).** Sentiment silently fell back to keyword analysis for
   all 50 tickers this run. Since news validation can *block* a trade, a degraded feed quietly weakens
   every thesis check. Worth surfacing in the cockpit.
4. **Gate 1 / ORATS backtester** — still gated on your paid-data decision (build doc §0.5 A3).
5. **Fuller mockup tabs** — unchanged, still your call.
6. Confirm the scheduled auto-paper task still runs (`vega_scheduler_status.ps1`).
7. Strategy naming is inconsistent (`bull_put_spread` vs `Iron Condor`). Normalized at the read points;
   worth unifying at the source.

## Commits (local, unpushed)

```
5c19d78  fix(vega): flag demo data in the cockpit and repair the freshness stamp's timezone math
fcbd4dc  fix(vega): redact credentials from API error strings before they are logged or persisted
b9c10a3  fix(vega): restore main.py entry point and stop the scan silently serving stale data
7f6cf17  fix(vega): two silent gate bugs that made the call-side engine unable to emit any trade
b1ac785  feat(vega): multi-strategy live engine + strategy-aware cockpit + criteria/news validation
```

Left alone as the handoff instructed: `analysis/outcome_logger.py`, `analysis/synthesizer.py`,
`data/news.py`, `logs/scan_log.json`.

## Tests

`python strategies.py` → **13/13** (extended to the real technicals vocabulary, so the mismatch that
killed the condor cannot silently return). `python verify_numbers.py` → **8/8**, exit 0.
All modules compile; both templates parse; the offline demo path still works.
