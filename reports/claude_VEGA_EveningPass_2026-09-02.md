# VEGA — Evening Pass Report, 2026-09-02

**Branch:** `session/2026-09-02-oauth-preflight`
**Suite:** 1357 passed, 0 failed. Live ledgers (`vega_auth_events`, `vega_outcomes`, `vega_counterfactuals`) hash-identical before and after the run.
**Scope completed:** Tasks 1–5, 11, 12 in full. Tasks 6–10 not started (droppable per §0); Task 10's shadow-book item was *measured* but not fixed.

---

## §A — Refutations first

### A1. The OAuth "hard deadline 2026-09-04 16:25" did not exist

The token was never going to expire Friday. Measured before touching anything:

```
obtained_at : 2026-09-02T15:17:27      expires_in : 602480s
expiry      : 2026-09-09T14:38 local   remaining  : 160.5 h
```

The brief's 48-hour window was ~161 hours. `TOKEN_REFRESH_MARGIN_S` is 24h, so the *unattended* refresh branch would first have fired on the **09-08** cycles, not Friday afternoon. After tonight's forced production-invocation refresh the expiry is **2026-09-10 11:49 local**.

This does not make the task wasted — 2a/2b were still worth doing, and doing them found things. But the urgency framing was wrong, and any plan built on "48 hours out" should be re-costed.

### A2. Chain-quality denominator contamination is real, already known, and already mitigated

The brief flags this as a "LIKELY REAL DEFECT… not previously checked."

**The mechanism is real.** `measure_chain_quality(records)` computes `raw = len(records)` and `usable = sum(predicate(o) for o in records)` — numerator and denominator are the **same list**. A truncated page walk shrinks both together, so a chain truncated to its quotable head reads as ~1.0. The denominator is *not* independent of the fetch that produced the numerator.

**But it was checked, and it was fixed.** The defect is documented in four places (`robinhood_mcp.py:861-868`, `fetcher.py:64-68`, `fetcher.py:1196-1200`, `config.py:304-308`), in the brief's own language — *"invisible to every ratio computed downstream: those are measured over the instrument list, and this shrinks the instrument list itself."*

The mitigation is exactly the independent denominator the brief asks for. `_truncated_walks` is a fact about the **walk** (pagination ended early), not about the **records** the walk returned, and `get_options_chain` refuses a truncated ticker **before** the ratio is computed:

```
fetcher.py:1201   if (apply_quality_gate and ticker in _truncated_walks
                          and config.SKIP_TRUNCATED_CHAINS):
                      ... _cache[cache_key] = []; return []      # never reaches the ratio
```

`SKIP_TRUNCATED_CHAINS = True`. **While that holds, the contamination cannot occur.**

Two honest caveats: the guard only fires on truncation it *detects*, and pre-fix truncations were silent — so historical data remains contaminated even though the live path is sound. And a truncation mode that fails to set the flag would restore the blindness.

**No recalibration performed.** `CHAIN_QUALITY_MIN_RATIO` untouched at 0.50.

### A3. ENTRY_HOLD cannot short-circuit enumeration or scoring — and is not the drought

`ENTRY_HOLD` is read at **exactly one line in the entire codebase**:

```
auto_paper_cycle.py:667   if getattr(config, "ENTRY_HOLD", False):
```

`main.py`, `vega_candidates.py`, and `strategies.py` contain **zero** references to it, and they run as separate subprocesses that exit before the desk is reached. Runtime confirmation from today's cycle and its artifact, not from reading:

```
14:35:02  RUN main.py                       (9m53s)
14:44:55  RUN vega_candidates.py            (4m37s)
14:49:32  ENTRY HELD — no new positions…    <- hold reached only after both completed

logs/scan_latest.json:  tickers_scanned 54 | total_scanned 2586 | total_qualified 0
                        rejected_trades 54 | qualified_trades 0
```

This is the brief's **third branch**: 2,586 structures were enumerated across 54 tickers and **zero qualified, with the hold never consulted.** The drought is upstream of `ENTRY_HOLD` entirely. Gate calibration is not exonerated and `ENTRY_HOLD` is not implicated.

### A4. The push did not propagate hold state — it was already there

Task 1 asks to confirm the push propagates `ENTRY_HOLD` to main. `git show main:config.py` before the push already had `ENTRY_HOLD = True` at line 700; it arrived with `4ee8d68` this morning. Nothing propagated. Confirmed rather than discovered, as instructed — the answer was just "no change."

### A5. The unrecognized-error-body counter already exists

Task 2c asks to add it if missing. `_note_unrecognized` / `_error_signature` / `unrecognized_errors()` are implemented and wired at **both** call sites (`robinhood_mcp.py:848, 936`), and read by `fetcher.chain_coverage`. Today's scan reports `unrecognized_errors: {}`. Nothing to add.

### A6. The shadow-book defect is worse than described, and differently shaped

The brief describes "158 of 178 rows with `modeled_credit_per_share` as mid on the bull-put path and natural on the call-side path." Measured:

```
modeled_credit_per_share  present on 178 of 178
mid_credit_per_share      present on   0 of 178
natural_credit_per_share  present on  20 of 178
current_mark              present on   0 of 178

comparable basis :  20  (9 Bear Call, 11 Iron Condor — modeled == natural exactly)
UNVERIFIABLE     : 158  (77 bull_put, 49 Bear Call, 32 Iron Condor)
```

It is not a mid-vs-natural *split*. On 158 rows **neither comparison field was recorded at all**, so the fill basis cannot be determined from the ledger in either direction. That is a harder problem than an inconsistency: there is no evidence to audit. It is also itself an instance of §A2's anti-pattern — the shadow book records the number it produced but not the inputs that would let anyone check it. `priced=0` in the cycle log follows directly: no marks exist.

---

## §B — Hard-deadline status

### B1. Deployment pinning — pushed, **NOT pinned**

```
git push origin d6591ed:refs/heads/main    ->  4ee8d68..d6591ed  (fast-forward)
```

**What the scheduled task invokes: a PATH, not a branch or commit.**

```
Task   : VEGA_AutoPaper_2Weeks   State Ready   LastResult 0   Next 2026-09-03 08:35
Action : powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass
         -WindowStyle Hidden -File "…\options_intelligence\run_auto_paper_cycle.ps1"
Script : $projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path ; Set-Location $projectRoot
```

Whatever is checked out at that path is production. The push protects the work and closes the `main` revert path — but **the risk is reduced, not closed.** Three stale local branches remain live hazards:

```
BRANCH                                     ENTRY_HOLD  RETRY  DELTA_BAND  LAST COMMIT
main                                            1        5        3       2026-09-02
session/2026-09-02-oauth-preflight              1        5        3       2026-09-02
session/2026-09-01-cohort-integrity             1        5        3       2026-09-02
feat/entry-timing                               0        0        0       2026-08-06
feat/opportunity-density-and-pop-framing        0        0        0       2026-08-13
fix/vega-mark-availability-2026-08-20           0        0        0       2026-08-20
```

A checkout of any of the bottom three silently reverts production to a scanner with **no ENTRY_HOLD, no retry layer, and no delta-band fix**. That is the exact failure named in the brief, still reachable.

**Minimal proposal (NOT implemented — brief says propose).** Add a non-fatal assertion at the top of `run_auto_paper_cycle.ps1`, after `Set-Location`:

```powershell
$expected = "main"
$actual   = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($actual -ne $expected) {
    "[$ts] !!! DEPLOYMENT DRIFT: on '$actual', expected '$expected'. Running it anyway; " +
    "verify this is intentional. !!!" | Out-File -FilePath $logFile -Encoding utf8 -Append
}
```

Deliberately **logs and continues** rather than aborting. An aborting guard would convert a branch mistake into a missed cycle, and missed cycles are this project's most expensive failure class (21 market-hours deaths). Deleting the three stale branches is the cheaper half of the fix and is an operator call.

### B2. OAuth — refresh **executed under production invocation**, auth failure is **permanent**

**2a — the 15:17:27 event was interactive, not production.** Timeline settles it: the scheduled task ran 14:35–14:51; the branch was created 15:13:33; the token was written 15:17:27; the commit landed 15:22:38. It happened at a developer terminal between a checkout and a commit. The original finding therefore stood, and 2b applied.

**2b — executed.** Run through the exact task command line (`-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden`), token file backed up first:

```
_interactive_auth_allowed() : False        -> browser flow BLOCKED (production shape)
normal margin  -> {"status": "ok", "seconds_remaining": 577740}          (no rotation)
forced margin  -> {"status": "refreshed", "rotated": true,
                   "expires_in": 653985,
                   "endpoint": "https://api.robinhood.com/oauth2/token/"}
access_token changed: True   refresh_token changed: True
expiry 2026-09-09T14:38  ->  2026-09-10T11:49 local   (181.7 h)
obtained_at persisted and read back verified
```

The unattended path is now proven end-to-end against the real endpoint, with verified persistence — not modelled, not unit-tested, executed. Journal after: 3 real events, 0 fixture lines.

**2c — auth failures classify as PERMANENT.** Tested, not read:

```
False <- 401 Unauthorized                False <- 403 Forbidden
False <- {"error":"invalid_token",…}     False <- Authentication failed
False <- invalid_grant                   False <- unauthorized
False <- Token has expired
True  <- RATE_LIMITED    True <- 429 Too Many Requests    False <- Request entity too large
```

`_RETRYABLE_ERROR_MARKERS` contains only `rate_limited`, `too many requests`, `429`, `timeout`, `timed out`, `503`, `service unavailable`, `temporarily unavailable`. **Implication: the SKIP branch, not the backoff branch.** A dead token does not burn the retry budget looping; every ticker fails fast and Robinhood falls back a tier to yfinance — whose wide quotes collapse natural credit toward zero, which would present as a *credit-floor* drought. That is the specific false signature to watch for, and it is distinguishable: `logs/vega_auth_events.jsonl` would carry a non-`ok` preflight line for that cycle.

---

## §C — Task-by-task

| # | Task | Status | Result |
|---|------|--------|--------|
| 1 | Deployment pinning | **done** | Pushed `d6591ed`→main. Production **not** pinned; 3 stale branches remain hazards. Guard proposed, not implemented. §B1 |
| 2 | OAuth | **done** | Prior refresh was interactive; production-invocation refresh executed and persisted. Auth failure = permanent/SKIP. §B2 |
| 3 | NewsAPI scope | **done** | Exactly one credential, one host, window bounded. Below. |
| 4 | ENTRY_HOLD position | **done** | Hold is at the open action only; 2,586 structures enumerated, 0 qualified. §A3 |
| 5 | Chain-quality denominator | **done** | Non-independent denominator confirmed; already documented and mitigated by `_truncated_walks`. §A2 |
| 6 | Quarantine / archival | not started | Droppable. §F |
| 7 | Decision ledger | not started | Droppable. §F |
| 8 | Liveness monitoring | not started | Droppable. §F |
| 9 | Funnel diagnostic design | not started | Droppable. §F |
| 10 | Remaining items | partial | Shadow book **measured** (§A6), not fixed. Others not started. |
| 11 | Methodology | **done** | `VEGA_METHODOLOGY.md` — Levels 0–2, identity-vs-value, self-validating instrumentation. |
| 12 | Suite / audit / report | **done** | 1357 passed; ledgers hash-verified; this document. |

### Task 3 — NewsAPI exposure, bounded

```
INTRODUCED  e4757c4  2026-05-27   (logs/scan_log.json tracked, carrying error payloads)
CLOSED      19448ad  2026-07-21   (chore: untrack scan_log.json)
Window      26 commits, ~8 weeks, public repo
```

Scanned every historical blob of `logs/scan_log.json` and every tracked file in history:

- **46** secret-shaped query params, **all** `apiKey=`, **all** on host `newsapi.org`.
- **Exactly one distinct value**, 32 chars, `sha256[:12] = 0176a2ea1371`. (Fingerprint so the operator can confirm they are rotating the right key; the value is not reproduced here.)
- **No other credential took this route.** The only other `api_key=` hits in history are `analysis/synthesizer.py` and `data/news.py` in the initial commit, both of the form `api_key=config.ANTHROPIC_API_KEY` / `config.OPENAI_API_KEY` — code, not values.
- `redact_secrets` **does** cover the error-payload path: `fetcher.py:123` applies it to `"error"` inside `_log_api_call`, which is the `api_calls[].error` field that leaked. Also applied at the two logger sites (1578, 1645). HEAD is clean.

**Operator action:** rotate one NewsAPI key; tell the provider the window is 2026-05-27 → 2026-07-21. Nothing else is implicated.

### Task 4 — before-state recorded, and the confounding proven

The brief requires the before-state be recorded before behaviour changes. For the unconditional-scoring fix (`b8e7f52`), over `logs/vega_counterfactuals.jsonl`, 2,726 rows:

```
with edge_score      331   ->  failed >=1 gate:    0     passed all: 331
without edge_score  2395   ->  failed >=1 gate: 2341     passed all:  54
=> "edge_score is present" is a PERFECT proxy for "all gates passed"

rows carrying edge_score_basis (written only by b8e7f52):  0
```

**The unconditionally-scored sample has not begun accumulating.** `b8e7f52` was committed at 20:47; today's only cycle ran 14:35. The first cycle producing `edge_score_basis` rows is tomorrow 08:35. This is the empty-dataset trap the brief anticipated, arriving by a different route than expected — not because the hold short-circuits, but because the commit post-dates the last cycle. **Do not read an empty `edge_score_basis` population before ~09-03 as "the fix didn't land."**

Single-gate killers across the same 2,726 rows:

```
 377  credit_to_width     84  earnings_clear    82  otm_buffer    72  min_credit_usd
  49  pop                 32  liquidity         16  support_shelter    7  quote_spread
   6  natural_credit_positive                 2001  (multiple gates / none)
```

`credit_to_width` + `min_credit_usd` = 449 credit-related sole failures, corroborating the credit-floor reading rather than the caps or chain quality.

**Anomaly, unexplained:** 54 rows passed all gates yet carry no `edge_score`. 331 + 54 = 385 gate-passers, but only 331 scored. Probably rows predating edge scoring; not verified. Flagged rather than assumed.

---

## §D — Drought status

With Task 4 resolved, the drought restates as: **a qualification failure, not an entry-blocking one, and not a data-quality one.**

Today's cycle, measured:

```
tickers attempted 54 | scored 54 | skipped []        ratio 1.00 (floor 0.70)
truncated_walks {}   | band_holes {}   | unrecognized_errors {}
structures enumerated 2586  ->  qualified 0
```

Chain quality is **not** the constraint today: every ticker cleared, nothing truncated, nothing skipped. Ticker-level rejection reasons:

```
16  No valid same-expiration credit spread found
14  IV Rank below minimum 45   (8.1, 15.8, 18.4, 20.0, 21.1, 21.6, 22.2, 22.5, 23.5,
                                26.3, 30.0, 31.6, 40.5, 42.5)
 8  News BLOCKING event detected
 3  POP below minimum 0.72     (0.62, 0.65, 0.69)
 1  Negative VRP (-5.8pp)
```

Eliminated as causes: the phantom book (measured, does not gate — see prior session), chain truncation (zero today, guarded), `ENTRY_HOLD` (downstream of all of it), `main.py:543` adjusted prices (+3 of 544).

Still live: **genuine gate miscalibration** and **a real low-vol regime**. The IV-rank distribution is the strongest available evidence and it points at regime — a universe clustered in the 15–32 band against a floor of 45 is what a low-vol tape looks like, and a VRP strategy declining it is the strategy working, not failing. Two names at 42.5 and 40.5 sit just under the floor, which is the only observation here that would bear on calibration, and two names is not a basis for moving a constant.

**Unit-of-analysis caveat, as instructed.** Ten trading days of zero qualification is **not ten independent failures.** It is plausibly one regime, sampled daily. The unit is cycle state or regime state, not ticker-days, and **no hypothesis here gets stronger merely because the drought ran longer.** The 2,586-structure enumeration from a single cycle is worth more than the ten-day streak, because it has a denominator.

---

## §E — New findings not in the brief

1. **A second deployment surface tracks `main` independently of the checkout.** `.github/workflows/scan.yml` has `schedule:` triggers, and GitHub Actions runs scheduled workflows from the **default branch** regardless of which branch the workflow file sits on. Default branch is `main`. Until tonight's push, those runs executed `4ee8d68` — without the five fixes — no matter what was checked out locally. Now aligned. *Not verified:* whether the workflow is currently enabled; `gh` is unauthenticated on this machine.

2. **The TTY half of `_interactive_auth_allowed()` is weaker than its docstring.** Under `-NonInteractive` with redirected stdout, `sys.stdin.isatty()` still returned **True** in my harness. The browser flow was correctly blocked — but by the `ROBINHOOD_MCP_ALLOW_BROWSER` config flag alone, not by the TTY backstop the docstring describes as catching "the flag left set in a profile that the scheduled task inherits." Under the real task there is no console at all, so the backstop probably works there; my harness leaked a console stdin and could not exercise it. Worth knowing the belt is doing the work and the braces are untested.

3. **A `no_endpoint` critical event 14 seconds before the first successful refresh.** `{"at":"2026-09-02T15:17:13","status":"no_endpoint","severity":"critical","reason":"authorization-server metadata did not name a token_endpoint"}`. Discovery failed once, then succeeded. If it fails in production the preflight logs and continues by design, and the scan silently falls back a tier — the same signature as an auth failure and as a credit drought.

4. **158 of 178 shadow-book rows have no recorded fill basis at all** (§A6) — not a split, an absence.

5. **54 counterfactual rows passed all gates but carry no `edge_score`** (§C).

6. Carried from earlier today: the auth journal was being written by its own test suite — 12 fixture lines around 1 real event, in the file whose docstring says "a non-empty tail is itself the signal." Fixed in `d6591ed`; the isolation held across two full suite runs tonight.

---

## §F — Open items, ordered by consequence of failure

1. **Production is not pinned.** Three stale branches revert `ENTRY_HOLD`, the retry layer, and the delta-band fix on checkout. Highest consequence, lowest effort: delete the three branches, or add the logging guard in §B1.
2. **Rotate the NewsAPI key** (`sha256[:12] 0176a2ea1371`), window 2026-05-27 → 2026-07-21. Operator-only.
3. **`/vega/ingest` writes candidates as `open`** (Task 6b). The writer fix is the real remediation; archival preserves forensics. **Not verified tonight:** whether AMGN, SMH and NEE are absent from the JARVIS open list — the false-negative direction that creates real risk. Requires querying the tower.
4. **Quarantine flag on the 08-20/08-21 batches** (Task 6a). Until the metadata flag exists, only this report and a commit message record the prohibition — and commit messages are the artifact nobody reads. Rising consequence as the batches mature.
5. **Liveness rule** (Task 8). Three instruments with zero grading were found three separate times; the fourth channel (prediction ledger, 1,498 of 2,739 open) is unwatched.
6. **Decision ledger** (Task 7). Will work with the hold in place — §A3 proves enumeration runs.
7. **Shadow-book fill basis** (§A6). Record `mid_credit_per_share` and `natural_credit_per_share` on every modeled row before grading any of them; 97 are already graded on an unverifiable basis.
8. **`auto_paper_cycle.py:862`** ravens price basis; **`price_basis` field** alongside `resolution_method`; raw-vs-adjusted as a per-field schema property.
9. **Truncation audit** (Task 10), with the caveat written into its conclusion: pre-fix truncations were **silent and unlogged**, so a null result is *not* evidence that no position was selected off a truncated grid.
10. **Funnel diagnostic** (Task 9) — design only, next session.

---

## §G — What I would not do

**G1. I would not treat Task 5's audit list as six defects to fix.** The list is a good lens, but §A2 shows the flagship case was already found and fixed. Auditing the remaining five for *shape* is right; assuming they are broken because the pattern exists is the same error as the `main.py:543` prediction — a real mechanism is not a measured effect. Each should get its own before-state.

**G2. I would not implement the deployment guard tonight**, and I would not make it abort. Any change to the production driver is a change to the thing whose failure mode is missed cycles. Deleting three stale branches achieves most of the benefit with none of that risk.

**G3. I would not have forced the token refresh if the deadline had been real.** The brief's urgency (2a/2b) and its risk profile pointed opposite ways: a forced rotation kills the old refresh token server-side the instant the server answers 200, and a persist failure there is unrecoverable. It was safe to do *because* §A1 showed 160 hours of runway and the access token would have survived to 09-09 even if the write failed. Had the 48-hour framing been accurate, the right move would have been to test the path and let the scheduled cycle perform the real rotation, not to hand-rotate a credential under time pressure.

**G4. I would not read today's IV-rank rejections as a case for lowering `MIN_IV_RANK`.** Two names at 40.5 and 42.5 are the entire body of evidence near the boundary. The other twelve are at 8–32, which no defensible floor admits. This is the precise situation the funnel diagnostic's own caveat warns about — the gates *are* the strategy, and loosening them until trades appear is the standard way a premium-selling thesis is destroyed.

**G5. I would not grade any more of the shadow book until §A6 is fixed.** 97 of 178 rows are already graded against a `modeled_credit_per_share` whose basis is unrecorded. Grading the remaining 81 adds rows to a cohort that cannot be audited later — the exact defect that invalidated the two cohorts this instrument was built to replace. It should be stopped, not completed.

**G6. On the brief's "expect 1357+ passing":** the count is 1357, unchanged. I added no tests tonight, because every task was measurement or investigation and the one code change (the auth journal isolation, `d6591ed`) was made earlier today with its own coverage. A rising test count was not evidence of anything here.
