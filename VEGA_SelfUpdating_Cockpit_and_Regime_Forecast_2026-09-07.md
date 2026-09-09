# VEGA — self-updating cockpit + market regime forecast
**2026-09-07**

Two changes. The first makes the cockpit stop lying about how current it is. The second gives
VEGA a written-down, graded opinion about the market it has been selling premium into.

---

## 1. The cockpit updates itself

### What was actually wrong

The board refresh was **already built and switched off**. `vega_app._scheduler_loop` has run
`main.py` every `BOARD_REFRESH_MIN` since it was written; `config.py` carried
`INTRADAY_SCHEDULER_ENABLED = False` and a long comment explaining why.

That comment is correct and was not overridden. The scheduler was disabled on 2026-08-19
because **two drivers were firing the same job**: the cockpit's paper cycle hourly at :21, and
the Windows task `VEGA_AutoPaper_2Weeks` every two hours at :35. A cycle takes 13–16 minutes,
so the task's fire landed inside the cockpit's run every time and was turned away by the lock.
On 2026-08-18, three of four task fires did no work at all.

So there were two defects, not one, and they pointed in opposite directions:

* the **board** never refreshed, and an open page was never told when it had — the operator's
  only way to see current numbers was to close the engine and relaunch it;
* the **paper cycle** must *not* be driven by the cockpit, because paper execution and the
  re-mark loop the cohort depends on cannot be conditional on a dashboard window being open.

Turning the one master switch back on would have fixed the first and re-created the second.

### What changed

**The scheduler is armed, and its jobs are now gated separately.**

| job | switch | state | why |
|---|---|---|---|
| `board` — re-runs `main.py` | `INTRADAY_BOARD_REFRESH_ENABLED` | **ON**, every 15 min, market hours | this is the one that makes the cockpit self-updating |
| `paper` — `auto_paper_cycle.py` | `INTRADAY_PAPER_CYCLE_ENABLED` | **OFF** | the half that collided; the Windows task remains its sole owner |
| `forecast` — the daily regime claim | `MARKET_FORECAST_ENABLED` | **ON**, daily after 14:00 local, **regardless of market state** | crypto trades weekends; gating it on the equity clock would drop two claims in seven for BTC/ETH and bias their record toward weekdays |

**The residual race is closed.** `auto_paper_cycle` spawns `main.py` as a subprocess of its own,
so the Windows task and a cockpit board scan could still put two `main.py` processes on
`scan_latest.json`, and the loser would write a board assembled from the winner's half-written
state — a board that looks fine and reconciles against nothing. `_spawn_job` now refuses to
start a board scan while `logs/auto_paper_cycle.lock` is fresh. It only *reads* that lock:
taking it would make the cockpit capable of blocking paper execution, which is the exact
dependency the scheduler was disabled to avoid. A deferred scan is **not** stamped, so it waits
out the cycle and starts on the next 30-second tick rather than losing its whole 15-minute slot.

**The open page reloads itself when the board it is showing has been superseded.**

* New endpoint `GET /api/freshness` — a few `stat()` calls, no scan. A test asserts it can
  never reach `run_scan_now`, `subprocess`, or the price feed, because a page polling it every
  20 seconds would otherwise quietly run the engine hundreds of times a day.
* Every page ships the artifact mtime it was rendered from. The poller reloads when the server's
  stamp has *moved* — never on a plain timer. A meta-refresh would reload a weekend board that
  cannot have changed and would take the page out from under a half-typed contract count.
* The reload **refuses to interrupt**: it holds while an input is focused, while a candidate
  drawer is open, or while a form is submitting, and it counts down visibly with a
  *Keep this view* button. Losing typed work is a worse failure than being one scan behind.
* The nav carries a live **age readout** (`Board 14:32 · 3m ago`), updated on every poll without
  reloading, so an idle screen still tells the truth about what is on it. It turns amber past
  twice the refresh interval while the market is open.

Cadence knobs: `COCKPIT_AUTOREFRESH_ENABLED`, `COCKPIT_POLL_SECONDS` (20),
`COCKPIT_RELOAD_GRACE_SECONDS` (8).

> **The scheduler runs inside the cockpit process.** Closing the launcher window stops the
> 15-minute refresh. That is the trade for not re-creating the collision.

---

## 2. Forecast tab — the market regime call

New nav tab between **Open** and **Research**. New module `analysis/market_forecast.py`.

### What it shows

One **BULL / BEAR / NEUTRAL** call per market at four horizons — 24 hours, 1 week, 1 month,
6 months — for `SPY`, `QQQ`, `IWM`, `BTC-USD`, `ETH-USD` (declared in
`config.MARKET_FORECAST_ASSETS`).

**Read the lean, not the percentage.** The flat band is fixed at ±0.4307σ — the width that makes
bull, bear and neutral *equally likely* under zero drift — so all three start tied at 33.3%.
A 38% call is a real tilt; 34% is close to no opinion. Every cell therefore shows the call, the
probability, the lean in points over the runner-up, and a three-segment bar of the full
distribution, because the raw percentage on its own reads as feeble to anyone who does not know
the base rate.

### The model, and what is genuinely new in it

The probability comes from the band the asset's own volatility implies, not from a signal score:

```
p_up = 1 − Φ(b − μ)      b = 0.4307σ      μ = IR·√t      |IR| ≤ 0.25
```

The tilt is `direction_forecast.tilt()` **unchanged** — 20/50 trend, position against the slow
average, realised vol against its own past — reused rather than reimplemented so a second set of
constants cannot drift from the first. Three things differ, and each one matters:

* **Calendar.** Equities compound over 252 sessions, crypto over 365 days. Annualising a crypto
  series on 252 understates its horizon sigma by `√(365/252)` = 1.20 — a flat band 20% too
  narrow, making NEUTRAL that much harder to hit on exactly the assets that move most. Each
  asset declares its own calendar; a week is 5 days for SPY and 7 for BTC.
* **Volatility window scales with the horizon** (20 → 120 periods). A six-month band set from
  twenty sessions of vol is a reading of last month wearing a half-year label.
* **The lean grows with the horizon** and this is correct, not a bug: drift accumulates in `t`
  and sigma in `√t`, so the same edge is worth `μ = IR·√t`. The 24-hour call is nearly pure
  climatology; the six-month call carries the most tilt.

Prices come from `data/fetcher` (yfinance) for **both** stocks and crypto — deliberately, not
from the Coinbase reader in `data/crypto`. The resolver grades every claim off
`fetcher.get_price_data`, so a claim anchored to a Coinbase close and settled against a yfinance
bar would carry a venue basis in **every single grade**.

### The live grid and the ledger are separate, on purpose

* The **grid** recomputes on every page load and writes nothing.
* A **dated claim** is written once a day, after 14:00 local, and that copy is the only one
  graded. Anchoring in the morning would hand the 24-hour call several hours of the move it is
  supposed to be predicting.

If the live board also recorded, a day's opinion would be written once per refresh at drifting
anchors, and the ledger's hit rate would be measuring how often the page was refreshed.

### A correctness fix that would have been invisible

`data/fetcher._cache` is keyed per (ticker, period) and **has no expiry** — it is sized for a
two-minute scan process that exits. The cockpit is a long-lived server. Serving the "live" board
straight from it would have shown *the first frame ever fetched* for as long as the window
stayed open — the original bug, one layer down, on a page whose whole claim is that it is
current. It would have looked perfect.

`market_forecast._default_lookup` therefore keeps its own TTL (`MARKET_FORECAST_CACHE_MIN`, 10
min) and evicts fetcher's entry for the key it is about to request. On a vendor failure it
returns the stale series and the page labels its age rather than blanking the board.

### Grading

Claims land in the existing ledger (`logs/vega_predictions.jsonl`) under cohort
`market_forecast_v1`, as claim types `direction_mkt_24h / _1w / _1m / _6m`. The `direction`
prefix is load-bearing — `predictions.is_direction_claim` matches by prefix, so these are scored
by the existing direction scorer the moment they are written and cannot fall through to
"no scorer for claim type" and mark a whole population unresolvable while the ledger still looks
healthy. The `_mkt_` segment keeps them out of the 56-ticker watchlist sweep's buckets.

**Every claim is written alongside its climatology twin** — same band, same horizon, mean pinned
at zero, graded and not displayed. The signal is only worth something to the extent it beats
that row.

Per horizon the tab reports: claims written, in flight, resolved, **independent after clustering**,
hit rate, Brier, **resolution**, and skill against the twin. Resolution leads, because raw Brier
cannot tell a model that knows something from one that has memorised the base rate. A card goes
green only when the type is gradeable *and* discriminates. **Nothing is green today.**

### The 24-hour horizon is carried under protest

`direction_forecast` retired `direction_1d` and `direction_overnight` on **2026-09-04** after
measuring, over 96 effective samples:

```
direction_overnight   hit 18.5%   Brier 0.174   RESOLUTION 0.0000
direction_1d          hit 29.8%   Brier 0.211   RESOLUTION 0.0000
```

Resolution 0.0000 is what shuffling the outcomes produces: the forecasts did not distinguish one
day from another. This is indices and crypto rather than single names, so it is a different
population and worth its own measurement — but it is **not** a rehabilitation of that result. It
is labelled UNPROVEN on the page and graded in its own bucket.

**If it comes back at resolution ~0 past the gradeability floor, retire it and record the
numbers in the module docstring**, the way `direction_forecast` did.

The six-month horizon cannot say anything before **March 2027**, by construction.

---

## Measuring instrument, not a signal

Nothing in the Forecast tab reaches selection, sizing or execution. No gate reads it, no strike
moves because of it, and no order can be placed from it. VEGA has never had a written-down
opinion about the market it sells premium into — only about individual spreads — so there has
never been anything to grade. Now there is.

---

## Review pass — three defects found after the build

Everything above was re-read against the code on disk and exercised end to end. Three things
were wrong, in ascending order of how much they mattered.

**1. A test that could not fail.** `test_price_cache_expires` monkeypatched `sys.modules` only.
`from data import fetcher` resolves the ATTRIBUTE on the package first and falls back to
`sys.modules` only when it is absent, so the patch worked in a bare interpreter and stopped
working the moment anything else imported `data.fetcher` — which `conftest` does. The test
reached the real vendor, its call counter stayed at zero, and it failed. Fixed by patching both
bindings. The suite's earlier "tests pass" was not true for this one.

**2. The daily claim was written only by the cockpit.** `record_daily` was reachable from the
cockpit's scheduler and from nowhere else. The cockpit is a window Josh opens when he wants to
look at something, so the ledger would have recorded *when the desk was staffed*, and every
horizon's hit rate would have been conditioned on that with nothing on the page hinting at it.
`_record_market_forecast()` is now wired into `auto_paper_cycle` beside the direction and band
sweeps, in both the full and mark-only paths, so the Windows task writes the claim whether
anyone is at the machine or not. Both drivers firing is safe — `predictions.record` is
idempotent per (asset, horizon, day). *Accepted gap:* the cycle exits early when equities are
closed, so weekend BTC/ETH anchors still depend on the cockpit being open.

**3. The grader would have called the forecast worthless precisely when it worked.**
`_verdict` carried `if brier > 0.25: "worse than always guessing 50%. This claim type is not
adding information."` That constant is the Brier of "always say 50%" — the right yardstick for
a **two-outcome** claim. These are three-outcome claims built so bull, bear and flat start tied
at a third, so stated confidence never leaves ~0.33–0.45. On that scale:

```
correct call  → (1 − 0.35)² = 0.42        Brier = 0.12 + 0.30 × hit_rate
wrong call    → (0 − 0.35)² = 0.12        crosses 0.25 at 42.5% correct
```

Against a 33% base rate, **every horizon forecasting better than chance would have been
declared "worse than a coin flip — not adding information", and the better it got the louder
the page would have said so.** The tab would have printed that about its own best horizon. The
constant is now applied only where it is defined (average confidence ≥ 50%); below that,
resolution and the permutation test — which are scale-free and already implemented, and which
the verdict's own docstring says it leads with — do the work. Two regression tests were added,
and both were confirmed to fail against the old logic.

Measured effect on a 70%-correct three-way fixture:

```
before   70% correct over 20 with a Brier of 0.32 — worse than always guessing 50%.
         This claim type is not adding information.
after    70% correct while only claiming 35% — underconfident by 35pp. This signal
         deserves more weight. But it does NOT discriminate: resolution 0.000 is what
         shuffling the outcomes produces 100% of the time.
```

The same defect applies to the existing single-name direction and band sweeps, which share the
scorer and the same three-outcome construction; the fix covers them too.

Two smaller things were corrected: the footnote's tilt cap now reads `direction_forecast.MAX_TILT_IR`
(the value the model actually ran with) rather than re-reading a config name that module only
falls back to, and the Brier figure on each grade card carries a tooltip saying that on a
three-way call **lower is not better**, because a reader trained on binary forecasts would rank
the channel exactly backwards.

**Verified end to end, not just by unit test:** the live board fetches real prices for all five
assets (BTC 30-day vol window and a 5.81% flat band against SPY's 21-day and 1.02% — the crypto
calendar is genuinely doing something); claims record, resolve and grade through the real
scorer, 40/40 in a synthetic rising market with the signal at 100% and the climatology twin at
0%; the Forecast tab renders in both its empty and populated states; `/api/freshness` returns
valid JSON. The live ledger is clean — `grep -c mktfc logs/vega_predictions.jsonl` is 0, and
`conftest`'s autouse fixture redirects `PREDICTIONS_FILE`, so the new tests cannot repeat the
2026-08-20 contamination.

Full suite: **1585 passed** (1579 before the review, +6 tests).

---

## Files

| file | change |
|---|---|
| `analysis/market_forecast.py` | **new** — model, live board, daily claim, grading, CLI |
| `vega_app.py` | Forecast view; `/api/freshness`; self-refresh script; nav age readout; scheduler split + paper-lock deferral |
| `config.py` | scheduler re-enabled and split; autorefresh cadence; `MARKET_FORECAST_*` block |
| `Launch_VEGA.bat` | launch banner notes the window must stay open |
| `auto_paper_cycle.py` | **review fix** — `_record_market_forecast()` wired into both cycle paths |
| `analysis/predictions.py` | **review fix** — the coin-flip verdict is scoped to two-outcome claims |
| `tests/test_market_forecast.py` | **new** — 20 tests |
| `tests/test_cockpit_autorefresh.py` | **new** — 17 tests |
| `tests/js/autorefresh_harness.js` | **new** — executes the reload script under node |
| `tests/test_predictions.py` | +2 regression tests for the Brier scale |

Backups written beside the originals as `config.py.bak-*` / `vega_app.py.bak-*`.

## CLI

```
python analysis/market_forecast.py            # print the live board
python analysis/market_forecast.py --record   # write today's claims (hour-gated; --force to override)
python analysis/market_forecast.py --grade    # print the graded track record
```

---

# Corrections found in review, same day

Three defects in the above, found by exercising the build end to end rather than by reading it.
All three are fixed; the third is the one that matters and it reaches beyond this work.

## 1. `_verdict`'s Brier bar is a TWO-OUTCOME constant applied to THREE-OUTCOME claims

`predictions._verdict` carried `if brier > 0.25 → "worse than always guessing 50%. This claim
type is not adding information."` 0.25 is the Brier of "always say 50%" — correct for a
two-outcome claim on a 50–100 confidence scale, which is every claim the scorer originally
graded.

The directional claims are three-outcome and constructed so bull/bear/flat start tied at a
third, so stated confidence never leaves ~0.33–0.45. On that scale, with p ≈ 0.35, a call that
is RIGHT scores (1−0.35)² = 0.42 and one that is WRONG scores 0.12:

```
Brier = 0.12 + 0.30 × hit_rate        crosses 0.25 at 42.5% correct
```

against a **33% base rate**. So every horizon forecasting better than chance would have been
declared worse than a coin flip, and the better it got the louder the page would have said so.
The Forecast tab would have printed that verdict about its own best horizon. Measured on a
70%-correct fixture:

```
before   70% correct ... Brier 0.32 — worse than always guessing 50%.
                         This claim type is not adding information.
after    70% correct while only claiming 35% — underconfident by 35pp ...
                         But it does NOT discriminate: resolution 0.000 ...
```

**Fix:** the constant now applies only where it is defined — `avg_p >= 0.5`. Below that,
resolution and the permutation test do the work; both are scale-free and are what the verdict's
own docstring says it leads with. Two regression tests, confirmed failing against the old logic.

**This is a pre-existing defect in the shared scorer, not one introduced by the regime tab.** It
affects every three-outcome type already in the ledger. Measured against the live ledger today:

| claim type | n | avg conf | hit | Brier | old verdict |
|---|---|---|---|---|---|
| `direction_overnight_baseline` | 444 | 33.3% | 59.9% | 0.311 | **mislabelled "not adding information"** |
| `direction_1d` / `_1w` / `_overnight` | 444/224/444 | ~34% | 19.6–30.8% | 0.177–0.243 | under the bar, unaffected |
| `band_contains_*` | 54 | 80% | 81–87% | 0.118–0.218 | two-outcome scale, unaffected |
| `direction` | 12 | 51.9% | 50.0% | 0.251 | correctly flagged |

**Consequence for the archive: Brier figures quoted for `direction_*` types in earlier audit
docs are not interpretable against 0.25 and should not be read that way.**

**What this does NOT change:** the 2026-09-04 retirement of `direction_1d` and
`direction_overnight` was decided on RESOLUTION, not Brier. Those numbers are unchanged and
still decisive — resolution 0.0001 and 0.0000 at n_effective 128 against a floor of 10. The
retirement stands.

*Open cosmetic wart, not fixed:* `direction_overnight_baseline` now reads "underconfident by
27pp. This signal deserves more weight." on a climatology control with resolution 0.000. The
very next clause contradicts it, so the row is not misleading in full, but the phrasing is
pre-existing `_verdict` text and reads oddly on a control.

## 2. The daily claim was only written when the dashboard happened to be open

`record_daily` was reachable from the cockpit scheduler and nowhere else. The cockpit is a
window the operator opens when they want to look at something, so the ledger would have
recorded **when the desk was staffed, not what the market did** — and every hit rate would have
been conditioned on that with nothing on the page hinting at it. That is a worse failure than
the staleness this whole piece of work was meant to remove, because staleness is visible and
this is not.

**Fix:** `_record_market_forecast()` wired into `auto_paper_cycle` beside the existing direction
and band sweeps, on both cycle paths, so the Windows task writes the claim whether or not anyone
is at the machine. Both drivers firing is safe — `predictions.record` is idempotent per
(asset, horizon, day) — and the redundancy is the point, since either driver alone can fail to
run. A zero-write with assets configured now logs `MEASUREMENT CHANNEL CRITICAL`, matching the
band sweep.

**Known and accepted gap:** the cycle exits early when equities are closed, so on weekends only
the cockpit path can write, costing BTC/ETH their Saturday and Sunday anchors when the dashboard
is shut. Left visible rather than papered over.

## 3. A test that could not fail

`test_price_cache_expires` patched `sys.modules` only. `from data import fetcher` resolves the
**package attribute** first, so the patch worked in a bare interpreter and silently stopped
working once `conftest` had imported that module — the test reached the real vendor and its call
counter never moved. It was failing, so the earlier claim that the suite passed was not true for
that one. Fixed by patching both bindings.

## 4. Advice was being handed to the row defined to know nothing

Found on the follow-up pass, in the same function. Both bias branches ended in a
**prescription** — "the direction is useful", "this signal deserves more weight" — issued on the
strength of the gap between stated confidence and hit rate alone. That gap says nothing about
whether a channel knows anything: state a third about everything in a market that rose 60% of
the time and you are right 60% of the time.

Which is exactly what the live ledger showed. `direction_overnight_baseline` is the
**climatology control**, the one row defined to carry no information, and its verdict read:

```
60% correct while only claiming 33% — underconfident by 27pp.
This signal deserves more weight.          ← the recommendation
But it does NOT discriminate: resolution 0.000 ...   ← the retraction
```

444 claims, resolution 0.000, and a recommendation to weight it up. Read in full the row was not
misleading, because the next clause contradicted it — but **a verdict whose first sentence has to
be walked back by its second is one that will be quoted by its first sentence.**

The prescriptive clause is now gated on discrimination (the same permutation p < 0.05 the next
sentence already uses). Where a channel discriminates the advice stands; where it does not, the
verdict says what the numbers actually support — the probabilities sit below the base rate, which
is a calibration to correct, not a signal to weight up. The gate is a real distinction rather than
a way to delete the sentence: a fixture that is underconfident **and** genuinely discriminates
(p=0.011) still gets the recommendation, and there is now a test holding that open.

The existing `test_underconfidence_is_named_too` failed against this change, correctly — its
fixture states one probability about everything and so cannot discriminate by construction, and
its assertion conflated *naming* the bias with *prescribing* action on it. It was split into
three: the bias is still named on that fixture, the recommendation is asserted **absent** there,
and a new discriminating fixture asserts it present.

## Smaller corrections

* The Forecast footnote quoted `DIRECTION_MAX_TILT_SIGMAS`, a config name `direction_forecast`
  only falls back to; it now reports the cap the model actually ran with.
* Each grade card's Brier figure carries a tooltip saying that on a three-way call
  **lower is NOT better** — a correct call scores ~0.42 and a wrong one ~0.12 — and to
  read resolution instead. (This bullet previously said "lower is better", which is the
  inversion the tooltip exists to prevent, written into the note describing it.)

## Verified beyond unit tests

Live board fetches real prices for all five assets and the crypto calendar is demonstrably doing
something (BTC 30-day window, 5.81% flat band; SPY 21-day, 1.02%). Claims record → resolve →
grade through the real scorer, 40/40 in a synthetic rising market with the signal at 100% and
the climatology twin at 0%. The tab renders in both empty and populated states.
`/api/freshness` returns valid JSON. The live ledger is untouched — 0 `mktfc` rows, 5,338 lines
unchanged — and `conftest`'s autouse fixture redirects `PREDICTIONS_FILE`, so the new tests
cannot repeat the 2026-08-20 contamination. **Full suite: 1585 passed.**

## 5. The opt-out erased its own warning

The auto-reload script had never been executed — only grepped. Its tests asserted that the
source *mentioned* `f.stamp` and `activeElement`, which is the coverage that let the cache test
pass while exercising nothing: it proves a line was written, never that it runs.

`tests/js/autorefresh_harness.js` now runs the real script under node against a stub DOM and a
virtual clock. It found a defect on its first execution.

Clicking **Keep this view** sets the pill to "Auto-refresh paused". The next poll, twenty
seconds later, calls `setAge` unconditionally and overwrites it with an ordinary age readout —
so within one poll the pill was **indistinguishable from the armed state while auto-refresh was
permanently off for that page**. The operator opts out once, the notice disappears, and they go
on reading a board that will never update itself again, with nothing on screen saying so. That
is precisely the failure this whole feature exists to remove, re-entering through its own
opt-out.

Fixed by making `paused` outlive the banner: the age keeps updating (knowing *how* old the board
is stays useful), and the pill now reads `Board 14:45 · just now · auto-refresh paused` and keeps
saying it. Removing that one line was confirmed to fail the harness.

Nine behaviours are now executed rather than asserted about: unchanged stamp never reloads; the
age pill updates without reloading; an advanced stamp banners and reloads once after the grace
period; the reload is not repeated; typing holds the countdown and releases it on blur; an open
drawer holds it; "Keep this view" is permanent and says so; a disabled config polls zero times.

**This is still not a browser.** No rendering, layout or styling, so a CSS or event-wiring bug
would get through. The control flow is covered; **one real page left open across a scan boundary
is the remaining check**, and it is Josh's to make.

## Standing observation, not fixed

The tilt saturates at its cap for four of the five assets, so the model is largely reporting
"trend up, capped" and barely distinguishes SPY from BTC. That is the design working as
specified — the cap exists precisely so a 20/50 crossover cannot claim more than it is worth —
and it is why the 24h leans are ~0pp. It is also the reason **the graded record, not the grid,
is the part that will say whether any of this is worth reading.**

---

# 2026-09-08 — the board was dead, and every health field said it was fine

Found while grading the day's runs. This is the most serious defect in this document and it
predates the work above.

## What happened

`main.py` ends with:

```
if __name__ == "__main__":          # line 1943
    ...
    run_scan(session)               # the entire scan runs HERE

def _exit_cross_proj(...)           # line 1952   <- never reached in time
def _exit_cross_pct(...)            # line 1963
def _spread_ratio(...)              # line 1973
def _quantiles(...)                 # line 1983
```

`python main.py` executes the module top to bottom. The guard fires at 1943 and runs the whole
scan; the four helpers below it do not exist yet. `select_bull_put_pair` calls `_spread_ratio`
(lines 288, 359) and `_quantiles` (line 457), so **every ticker raised NameError**. The
per-ticker `except Exception` at main.py:1680 caught each one and recorded it as an ordinary
rejection.

## What the board reported while this was happening

```
2026-09-08 scan_latest.json
  total_qualified     0
  rejected           54      ALL category: ERROR
  reasons            45x  name '_spread_ratio' is not defined
                      9x  name '_quantiles' is not defined
  degraded          False
  scan_coverage     healthy: True   ratio 1.0   54/54   band_holes {}
  regime            LOW_VOL — "expect fewer qualifiers and smaller credits"
```

Every health field agreed the scan was fine, and the regime note supplied a plausible story for
the zero. **`scan_coverage` measures quotability — whether chain data arrived — not whether
anything was evaluated.** It was 100% correct and completely beside the point.

## Blast radius

| date | NameErrors in run.log | note |
|---|---|---|
| 2026-09-01 … 09-04 | **0** | board healthy |
| 2026-09-07 | 343 | ~6 scans; Labor Day, market closed |
| 2026-09-08 | 1,883 | full session, 26 scans, every ticker |

Commit `4627215` ("rejection counters record BY HOW MUCH") landed 2026-09-04 19:05, after
Friday's close, and appended the helpers below the guard. The first trading session it could
break was 2026-09-08 — **one full session of boards read "0 qualified" as market conditions.**

The scheduler work in this document did not cause it. It did raise the scan count from a
handful a day to 26, and the resulting empty board was graded as a healthy low-vol drought in
the first version of this file. That grade was wrong; the rejection reasons were one field away
in the same artifact and were not opened.

## Why 1,585 passing tests could not catch it

`import main` runs the module body with `__name__ != "__main__"`. The guard is skipped, the body
runs to the last line, and all four helpers exist. `tests/test_reason_margins.py` calls
`main._spread_ratio(...)` directly — **it passed on every one of the 1,899 logged NameErrors.**

Every import-based test in this repo is blind to script-execution order by construction. The
only observation that separates the two worlds is the order of the source, so that is what the
new test asserts. Minimal reproduction of the mechanism, including the blindness:

```
BEFORE-fix ordering -> NameError: name 'helper' is not defined
AFTER-fix ordering  -> ok
import ord_before   -> ok        <-- passes either way
```

## Fix

Pure code motion: the four helpers moved above the guard, which is now the last statement in the
file. No semantic change; `git diff` is 26 insertions / 9 deletions, all of it the move plus the
comment recording why. CRLF and BOM preserved. Backup at `backups/main.py.bak-20260908-225156`;
the broken artifact is kept as `backups/scan_latest.BROKEN-2026-09-08.json`.

`tests/test_main_entrypoint_ordering.py` — 7 cases, all confirmed to FAIL against the pre-fix
file and pass against the fixed one:

* no definition of any kind below the guard (whole-file rule, not a list of four names)
* the guard is the last statement in the file (an assignment below it is the same bug)
* each of the four named explicitly, so deleting them cannot turn the general test green
* any top-level name *called* before the guard must be *defined* before it — catches the next
  one without knowing its name

## Still open, not changed

**A scan where 100% of tickers ERROR must not report `degraded: false`.** The health contract
currently has no channel for "the engine ran and evaluated nothing": `degraded` and
`scan_coverage.healthy` both describe data arrival. An ERROR-category share above a floor should
degrade the scan and the cockpit should band it, the way a stale board is banded. That is a
change to the health semantics, not a bug fix, and is left for a decision rather than taken.

---

# Correction: the board was NOT empty, and the fix that followed

## What I reported wrong

I said the board showed nothing all day. It showed **162 candidates across all 54 tickers**.

`load_board()` fell back to the legacy fast-scan artifact whenever `qualified_trades` was empty
— for any reason. The NameError emptied it, so the cockpit **silently swapped boards**. What was
on screen for two sessions was `vega_candidates.py`'s enumeration: no `true_pop` (`None` by
design on that path), no edge score, and 128 of the 162 rows carrying a `"Blocked by …"`
narrative that `vega_app.py` never renders at all.

I read `scan_latest.json` and called it "the board." The board is what `load_board()` returns.
The fallback is described in vega_app.py's own module docstring, which I quoted on day one.

So the real defect was never an empty board. It was a **silent downgrade from the gated engine
board to an ungated enumeration**, wearing the same amber "provisional" strip the cockpit shows
when a scan merely hasn't run yet.

## Both halves fixed

**1. `main.py` — an errored scan is no longer healthy.** New `scan_errors` block counts
ERROR-category rejections, and `degraded` is now `coverage_unhealthy OR error_share >= floor`
(`config.SCAN_ERROR_DEGRADE_SHARE`, 0.20). Distinct exception messages are collapsed and
counted — one repeated NameError across 54 tickers is one defect, reported in the exception's
own words. A degrading scan also logs at ERROR level.

**2. `vega_app.py` — three states, not two.** New `_engine_artifact()` returns:

| state | when | behaviour |
|---|---|---|
| `trust` | ran, reached verdicts, current | engine board **even at zero** — a real drought looks like one |
| `failed` | ERROR share past the floor | fast scan still shown (only usable data) under a **red** banner naming the exception |
| `absent` | missing, unparseable, or stale while market open | ordinary amber fallback, unchanged |

Staleness applies **only while the market is open** (`ENGINE_ARTIFACT_STALE_MIN`, 45 min): after
the close the session's last scan *is* the board, and expiring it would drop the cockpit to the
fast scan every evening.

A trusted zero now renders a green *"No qualifying trade — the engine reached a verdict on every
ticker it could quote and qualified none of them. This is a real zero, not a fallback."* An
empty table with no explanation reads as a dead feed.

## Verified against the real artifact

The broken 2026-09-08 artifact is kept at `backups/scan_latest.BROKEN-2026-09-08.json` and is the
fixture, so the tests are anchored to what happened rather than a reconstruction:

```
BROKEN ARTIFACT -> source=legacy  engine_state=failed  rows=162
  red banner present : True      names the exception  : True
  warns rows ungated : True      amber strip suppressed: True

CLEAN ZERO      -> source=engine  engine_state=trust   rows=0
  green "real zero"  : True      no false ENGINE FAILED: True
```

`tests/test_engine_failure_is_not_a_quiet_market.py` — 10 cases, including that the *original*
artifact carries `degraded: false` with `coverage.healthy: true` (the fixture must be the thing
that fooled us), that pre-fix artifacts without a `scan_errors` block are still judged rather
than trusted by default, and that one bad chain in fifty-one is noise rather than a failure.

## A test contract changed, deliberately

`test_board_fallback.py::test_the_market_read_survives_a_board_with_nothing_on_it` asserted
`source == "legacy"` on an empty board — it encoded the old fallback rule, which is the rule
being removed. Its **intent** (a board with nothing on it must not lose the market read) is
unchanged and still asserted; only the path changed, and it now arrives by the better one.

The coverage it used to provide over the *legacy* path is not lost: a new
`test_the_market_read_survives_the_REAL_fallback_too` exercises a stale-while-open artifact,
which is the ordinary way to reach the fallback with an artifact still on disk.

**This conflict was masked by the hand-rolled shim** (it passes `None` for pytest fixtures, so
the test errored instead of failing on its assertion). It was found by re-running with a real
fixture rather than by assuming the failure was environmental — which is the second time in this
engagement that assumption would have been wrong.

## Test-run provenance

`pytest` cannot be installed in this environment, so counts below are from a hand-rolled harness
that does **not** load `conftest.py` and does not support fixtures or `parametrize`. The suite
is unrun here; these are not equivalent claims.

* harness: `test_engine_failure_is_not_a_quiet_market` 10/10, `test_cockpit_autorefresh` 17/17,
  `test_market_forecast` 20/20, `test_predictions` 66/66
* verified individually with real fixtures (harness cannot): all three `test_board_fallback`
  market-read cases, and all four `test_main_entrypoint_ordering` parametrized cases
* **not run here:** the full suite. Run it on the tower before trusting these numbers.
  *(2026-09-09: done. 1634 passed, 0 failed on the working tree. Also run against `git archive
  HEAD` extracted to a clean directory — 1630 passed, 4 skipped, 0 failed — which is the check
  that catches a committed tree referencing files that were never committed. It found exactly
  that: `vega_app.py` and `config.py` shipped references to `analysis/market_forecast.py` while
  it was still untracked, so the Forecast tab was dead and the 14:00 job spawned a missing
  script silently. Fixed by `71e5a36`. Note a bare `import vega_app` does NOT catch it — the
  import sits inside a function and degrades politely; the views have to be rendered.)*

---

# 2026-09-09 — NEWS_BLOCK is removing 15% of the watchlist on bad evidence

> **RESOLVED 2026-09-09 in `d67026a`. This section is kept as the diagnosis, and it is written in
> the present tense about code that no longer exists — read it as "what was true when found".**
>
> Shipped: (1) evidence on the rejection row, (3) word-boundary matching, (4) `"earnings"`
> dropped, (5) ticker relevance required. Also removed bare `"approval"` and `"rejection"`, which
> the diagnosis did not flag — they carry no event meaning and everything real is already caught
> by `fda` or the M&A terms.
>
> **(2) was withdrawn: its premise was wrong.** There is no silent LLM fallback to surface.
> `config.DISABLE_AI = True` is a deliberate, documented cost control, so the keyword path is not
> a degraded fallback — it is production, and always was. The finding this replaced it with is
> worse, not better, and is recorded at `config.py`'s `DISABLE_AI`: the comment claiming
> keyword screening was "fully sufficient for screening" was never measured, and was false.
>
> Two corrections to the numbers below. The headline "15% of the watchlist" counts what reached
> the board; the sentiment cache held **18 of 54** tickers marked BLOCKING, a third of the
> universe. Re-scoring those 18 under the fixed gate frees **14** and keeps 4, each naming its
> own ticker (GE's acquisition, JNJ and PFE on FDA decisions, NEE's shareholder merger vote).
>
> Regression tests: `tests/test_news_block_gate.py`. Six of the seven false-positive cases below
> block under the old rule and pass under the new one, so they are not vacuous; the seventh
> (ADBE "Fiscal Q3") trips no old keyword either, meaning ADBE's real block came from a
> different headline in its feed than the one recorded here.

Found by following up a "why did NEWS_BLOCK go 5 → 8 in half an hour?" watch item.

## The rule

`data/news._keyword_sentiment` concatenates **all** of a ticker's headlines and substring-matches
a keyword list:

```python
text = " ".join(headlines).lower()
for kw in BLOCKING_KEYWORDS:
    if kw in text:   return BLOCKING
```

Three failure modes stacked: **substring not word**, **no relevance-to-ticker test**, and
**whole-feed concatenation**. What it actually blocked on 2026-09-09:

| ticker | keyword | the string that matched |
|---|---|---|
| QQQ | `fire` | "Wall Street **Fires** Back With 'Activist Treasury' Lab" |
| JPM | `acquisition` | "GSK Commences Bond Sale to Help Fund Nuvalent **Acquisition**" — *GSK's* deal |
| ADBE | `earnings` | "S&P 500 Second Quarter **Earnings** Outpace Forecast…" — market commentary |
| SPY, IWM, NKE, CRWD, UNH | `approval` / `earnings` / `merger` | general market wraps |

**SPY, QQQ and IWM were blocked together** because they share market-wrap headlines. A signal
that removes the three most liquid underlyings on the board simultaneously is not detecting
single-name gap risk, which is what the gate exists for.

Also: `"fire"` matches "wildfire"/"misfired", `"hack"` matches "hackathon", `"default"` matches
"by default", `"recall"` matches "he recalls", `"approval"` matches "approval rating".

## The keyword path is running while the LLM path is configured

Every blocked entry carries `confidence: 0.8`, `key_themes: ["fire"]`,
`"Potential blocking event detected: 'fire'"` — the `_keyword_sentiment` fallback signature —
although `OPENAI_API_KEY` is set. `_gpt4o_batch_sentiment` is failing over **silently**, and
nothing on the artifact records which path scored a ticker.

## The artifact records no evidence

`rejected_trades` carries only `reason: "News BLOCKING event detected"`. No headline, no
keyword, no source, no path. **The one gate that disqualifies outright is the only one with no
audit trail** — which is exactly why nothing cross-checked it until now.

## `"earnings"` is redundant with a dated gate that already works

VEGA already has a dated earnings blackout (`EARNINGS_BLACKOUT_DAYS` in `strike_validator`,
`_earnings_clear` in `assessment`, surfaced as the `earnings_clear` gate). On the same session it
gets the right answers where the keyword gets the wrong ones:

```
ADBE  earnings_clear=False   <- correct, fiscal Q3 report imminent
JPM   earnings_clear=False   <- correct
SPY   earnings_clear=True    <- correct; an index ETF has no earnings date
```

The keyword blocked SPY. The dated gate cleared it. The keyword is a strictly worse proxy for a
risk that is already measured properly.

## THE DIRECTION OF THIS ERROR IS SAFE, AND THAT IS WHY IT SURVIVED

Unlike every other defect in this document, this one over-blocks. It costs opportunity, not
money — 8 names skipped, never a bad trade taken. That is also why nothing caught it: a gate
that errs toward standing aside produces no incident, no loss, and no complaint. It just quietly
removes the most liquid third of the board and looks like discipline.

## Proposed, not taken — this changes what is tradeable

1. **Record the evidence** (headline, keyword, path) on the rejection row. Pure observability;
   should happen regardless of the rest.
2. **Surface the silent LLM fallback** — if the GPT path fails, say so rather than degrading
   into keywords invisibly.
3. **Word-boundary match** instead of substring. Fixes "Fires", "hackathon", "by default".
4. **Drop `"earnings"`** from `BLOCKING_KEYWORDS` — the dated gate already covers it, correctly.
5. **Require ticker relevance** — the keyword and the ticker must appear in the *same* headline.
   Fixes JPM/GSK and every market-wrap block.

(1) and (2) are safe in any case. (3)–(5) change which trades reach the board and are the
operator's call.
