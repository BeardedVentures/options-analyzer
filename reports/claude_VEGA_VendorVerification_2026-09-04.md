# VEGA — Data Vendor Verification

**Date:** 2026-09-04, run 19:34–19:40 CDT
**Scope:** Verification pass. Read-only except the one §4-permitted test fix.
**Discipline:** every claim below has a command and its output, or an explicit "not verifiable from disk."

---

## ⚠️ Handle first — I exposed a secret in this session

While inspecting the token cache I printed its contents with a filter that showed short string values in full. **The Robinhood `refresh_token` was printed in plaintext** into the session transcript.

It was not written to any file, log, or commit. But it is in the transcript, and it is a live credential that can mint access tokens until revoked.

**Recommended: re-authenticate to rotate it.** That invalidates the exposed token. This is my error — the filter should have redacted by key name, and `refresh_token` is exactly the key that needed it. No token values appear anywhere else in this report or in any file I touched.

---

## The two direct answers

**Is Polygon currently contributing anything VEGA doesn't get elsewhere? — NO.**
Zero of 6,576 chain records over the last 5 logged days were served by Polygon (Q4), and a fresh entitlement check right now returns `healthy: false — bid/ask empty on all 5 contracts` (Q3). It is not merely redundant; its tier-2 slot **never executes** in normal operation.

**Is Robinhood Tier 1 currently and actually working end-to-end? — YES.**
Token valid with 136.2h remaining (Q2), a live pull returns 505 contracts with **all 13 fields populated on all 505** including broker Greeks (Q5), and 6,165 live records across 5 days carry `chain_source = robinhood` (Q4). The parser demonstrably processes real authenticated responses.

**One qualification on each**, stated in Q3 and Q7 respectively: the Polygon check ran after hours, and unattended multi-day token refresh is **not** established by anything on disk.

---

## Q1 — Does the Robinhood parser populate `open_interest` and `volume`, and from what fields?

**Yes, from `q["open_interest"]` and `q["volume"]` on the quote record** (`data/fetcher.py:_parse_robinhood_options`, lines ~700–703):

```python
"volume": _i(q.get("volume")),
"open_interest": _i(q.get("open_interest")),
```

There is no fallback to another source. **But there is a silent default**, and it is the answer to the part of the question that matters:

```python
def _i(x):                          # fetcher.py:654
    try:
        return int(x) if x is not None else 0
    except (TypeError, ValueError):
        return 0
```

`_i` returns **0** when the field is absent or unparseable. `_f`, used for every Greek, returns **None**. So:

- A missing Greek is recorded as absent and stays distinguishable.
- **A missing `open_interest` or `volume` is recorded as `0`, indistinguishable from a strike that genuinely has none.**

That matters because the liquidity gate is `volume >= 25 or open_interest >= 100`. Absent data therefore reads as *illiquid* rather than as *unknown*, and fails the gate silently. Not a defect fixed here — out of scope — but it is the direct answer to "are they silently falling back to a default."

**Live evidence they are genuinely populated** (Q5 pull, SPY, 505 contracts):

| field | populated | null | zero | median |
|---|---|---|---|---|
| open_interest | 505/505 | 0 | 6 | **493** |
| volume | 505/505 | 0 | 72 | **50** |

A median OI of 493 is not a default-zero artifact. The 6 zero-OI and 72 zero-volume strikes cannot be distinguished from absent data by the code, but the distribution says they are real illiquid strikes.

---

## Q2 — Resolving the 08-27 / 08-28 contradiction

**The contradiction is not between two documents. It is inside one function, eighteen lines apart.**

`data/fetcher.py:_parse_robinhood_options`:

- line 578 (docstring): *"Field mapping is READ OFF A LIVE RESPONSE (2026-08-27), not inferred from tool names."*
- line 596 (comment): *"defence in depth for a Tier-1 path whose parser has never seen a real response."*

**Resolved empirically, in favour of the docstring.** The parser demonstrably processes real authenticated responses — Q5 pulls 505 contracts through it with every field populated, and 6,165 records in the quality log carry `chain_source = robinhood`. The line-596 comment is stale text that survived the transition from "not yet verified" to "verified," and it is the one that should be corrected. **Not corrected here** — out of scope — but it is the specific stale artifact that has been propagating.

**Token state**, from `data/.robinhood_mcp_tokens.json`:

| | |
|---|---|
| file mtime | 2026-09-02 22:10:11 |
| `obtained_at` | 2026-09-02T22:10:11 |
| age | **1.89 days** |
| `expires_in` | 653,985 s = **7.57 days** |
| expires | **2026-09-10 11:49:56** |
| valid now | **yes — 136.2h remaining** |

Note the stated lifetime is **7.57 days, not the 8.2 days** the 08-27 document claims.

**Has a real browser-approved OAuth session completed and been used?** Yes. `logs/vega_auth_events.jsonl`:

```
2026-09-02T15:17:13  preflight  critical  no_endpoint   authorization-server metadata did not name a token_endpoint
2026-09-02T15:17:27  preflight  refreshed  rotated=true  expires_in=602480  https://api.robinhood.com/oauth2/token/
2026-09-02T22:10:11  preflight  refreshed  rotated=true  expires_in=653985  https://api.robinhood.com/oauth2/token/
```

Two successful refreshes against the real Robinhood token endpoint, both rotating the token. The cache mtime equals `obtained_at`, so nothing has re-written it since — consistent with a 7.57-day token obtained 1.89 days ago. **The token in use today is a refreshed descendant of a real approved session, and it is live.**

---

## Q3 — Polygon Tier 2, re-verified today

Fresh run of `validate_polygon_connection("SPY")` at 19:35 CDT:

```json
{"enabled": true, "mode": "polygon_delayed_15m", "healthy": false,
 "reason": "HTTP 200/OK but bid/ask empty on all 5 contracts checked -- plan is very likely
            not entitled to options quotes (Starter/Developer lack this; Advanced tier includes it)"}
```

Key is configured (32 chars). The result reproduces the 08-26 finding **on a fresh run**, not by restatement.

**Caveat, stated because a single after-hours run cannot settle it:** the market closed at 15:00 CDT and this ran at 19:35. An empty bid/ask after hours is consistent with *both* "not entitled" and "no quotes outside session." Distinguishing them requires a rerun during market hours. It does not change the operational answer, because Q4 shows the path never runs at all — but the entitlement conclusion specifically is one run short of airtight.

---

## Q4 — Does the fallback ever reach Polygon? **No. Zero times.**

`data/data_quality_log.jsonl`, 6,576 records, 2026-08-31 → 2026-09-04:

| chain_source | records | share |
|---|---|---|
| robinhood | **6,165** | 93.8% |
| coinbase | 393 | 6.0% |
| yfinance | 12 | 0.2% |
| none | 6 | 0.1% |
| **polygon** | **0** | **0.0%** |

**Polygon has served zero chain records in the entire logged window.** This is the stronger finding the prompt anticipated: Polygon's uselessness is currently *moot rather than actively tested* — the tier-2 slot does not execute, so its output quality is not what makes it non-contributing. Even if its entitlement changed tomorrow, nothing would reach it while Robinhood succeeds.

*Coverage limit:* the log only reaches back to 2026-08-31 (retention), so this speaks to 5 days, not to the full period since the 08-26 finding.

---

## Q5 — Field manifest from one real live pull

`fetcher.get_options_chain("SPY", MIN_DTE, MAX_DTE)` at 19:36 CDT — the real entry point, no mock. **505 contracts returned.**

| field | populated | null/absent | zero | sample |
|---|---|---|---|---|
| bid | 505 | 0 | 0 | 0.24 |
| ask | 505 | 0 | 0 | 0.25 |
| mid | 505 | 0 | 0 | 0.245 |
| delta | 505 | 0 | 0 | −0.007765 |
| gamma | 505 | 0 | 0 | 0.00024 |
| theta | 505 | 0 | 0 | −0.028113 |
| vega | 505 | 0 | 0 | 0.050797 |
| rho | 505 | 0 | 0 | −0.005938 |
| open_interest | 505 | 0 | 6 | 61 |
| volume | 505 | 0 | 72 | 45 |
| iv | 505 | 0 | 0 | 0.394359 |
| rh_pop_short | 505 | 0 | 0 | 0.989319 |
| **chain_source** | **505** | 0 | — | **robinhood** |

**Every populated field on that pull came from Robinhood.** `chain_source` is `robinhood` on all 505 contracts — no mixed sourcing, no yfinance fallback, no Polygon.

The Greeks are broker-computed rather than Black-Scholes-derived from a mid, which is the stated reason to prefer this source at all, and `rh_pop_short` is present — a field neither other vendor provides.

---

## Q6 — Test suites, exact counts

**Before:**
```
tests/test_robinhood_mcp.py + test_polygon_entitlement.py + test_data_quality.py
128 passed, 2 warnings in 6.99s
```

Both warnings were `RuntimeWarning: coroutine 'afetch_chain' was never awaited`, in `test_fetch_put_chain_contains_a_cancellederror` and `test_operator_interrupts_are_not_swallowed`. **Still present as of this run** — not resolved since they were noted.

**After the §4 fix:**
```
128 passed in 7.27s          (0 warnings)
```

**Full suite:** `1538 passed in 75.37s` — **0 warnings**, previously `1538 passed, 2 warnings`.

---

## Q7 — Token reliability over time: **not established**

Robinhood-sourced records per day, with no gap in the logged window:

| date | robinhood | yfinance | coinbase | none |
|---|---|---|---|---|
| 2026-08-31 | 612 | 12 | 9 | 6 |
| 2026-09-01 | 1,772 | 0 | 38 | 0 |
| 2026-09-02 | 1,194 | 0 | 30 | 0 |
| 2026-09-03 | 1,168 | 0 | 159 | 0 |
| 2026-09-04 | 1,420 | 0 | 157 | 0 |

No gap across those 5 days, so **no evidence of a lapse in that window.**

**But the question as asked is not answerable from disk, and the answer is closer to "no" than "yes":**

1. `data_quality_log.jsonl` reaches back only to **2026-08-31**. It cannot speak to 08-27 → 08-30, which is the window the 08-27 claim covers.
2. `vega_auth_events.jsonl` contains **3 events, all on a single day (2026-09-02)**. Refreshes observed: 2. **Distinct days on which a refresh was observed: 1.**

So the 8.2-day expiry-with-refresh claim is **projected from the stated lifetime, not observed across multiple days**. Two refreshes 7 hours apart on one day is not evidence of unattended multi-day operation. The current token does not expire until 2026-09-10, so the first genuine unattended-refresh observation is still ahead.

**What would settle it:** a refresh event dated on a different day from 2026-09-02 — which will only appear if the preflight runs unattended near expiry on ~09-09.

---

## §4 — The one thing changed

`tests/test_robinhood_mcp.py`, two test doubles. Production calls `asyncio.run(afetch_chain(...))`; both tests monkeypatch `asyncio.run` with a function that raises *before* consuming the coroutine, leaving it unawaited.

**Before:**
```python
def cancelled(*a, **k):
    raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")
```

**After:**
```python
def cancelled(coro=None, *a, **k):
    if coro is not None and hasattr(coro, "close"):
        coro.close()
    raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")
```

Same shape applied to `interrupted` in `test_operator_interrupts_are_not_swallowed`.

This is not warning-suppression: the real `asyncio.run` consumes the coroutine it is given, and a stub that drops it is an **incomplete double**. Closing it is what a faithful stand-in does. No production code touched, no assertion changed, both tests still assert exactly what they did before.

---

## Not verifiable from disk

- **Polygon entitlement, cleanly.** Q3's run was after hours; separating "not entitled" from "no session quotes" needs a market-hours rerun.
- **Multi-day unattended token refresh.** Q7 — needs an auth event on a second calendar day.
- **Anything before 2026-08-31.** Both the quality log and the auth journal begin at or after that date, so the 08-26 → 08-30 claims that started this cannot be re-verified, only superseded by current evidence.
