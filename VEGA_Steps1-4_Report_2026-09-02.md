# VEGA — Steps 1–4 report
**Date:** 2026-09-02
**Cohort:** `caps_v1`, 0 of 30 — clean slate, and still unopened by design.

---

## Step 1 — Term structure disabled ✅ COMPLETE, revalidated live

### Validated before flipping

The 60.1% figure was re-derived and the causality traced through code, not assumed:

- `get_chain_by_expiry()` → `get_options_chain(ticker, 5, 120)` is the only DTE 5-120 fetch, and
  both its call sites are gated on `TERM_STRUCTURE_ENABLED`.
- The quality log keys readings `(ticker, min_dte, max_dte[, "call"])`, so exactly three readings
  exist per ticker per scan and the wide one is identifiable. The method holds in 380 of 392
  ticker-groups.
- **Why it costs 60%:** term structure is a *scoring* input. `edge_calculator` adds
  `TERM_STRUCTURE_SCORE_ADJ` as a bonus, and the bonus must exist before
  `total >= MIN_EDGE_SCORE` is evaluated — so the wide pull happens in `screen_ticker` for all 56
  tickers ahead of every gate, including the ~47 about to be rejected. `assessment.load_context`
  argues exactly this and offers `enrich_surface()` ("nine tickers rather than fifty-six") — but
  that path is post-selection *analysis* and cannot serve a pre-selection *score*. The eager fetch
  is not a bug to fix; it is what scoring on this signal costs. That is what made the decision
  clean.

### One claim of mine corrected

My 2026-09-01 Part 1 findings said the truncation "blocks the ticker's subsequent call-chain fetch
as well." **That is wrong.** `_truncated_walks` is read at exactly one site, in `get_options_chain`
(the put path); `get_call_options_chain` never consults it. On 09-01 SPY and QQQ were scored and
rejected normally — SPY on a news block, QQQ on IV Rank 38.5 — with both trading chains intact.
The real cost was narrower: the surface signal was unavailable for SPY and QQQ, i.e. it failed on
the two names it is most expensive to compute.

### Revalidated against the live 08:35 scan

| | 09-01 08:35 (ON) | 09-02 08:35 (OFF) |
|---|---|---|
| contracts fetched | 19,286 | **7,595** (−60.6%) |
| scan wall clock | 17m 41s | **10m 24s** (−41%) |
| `SKIP_TRUNCATED_CHAIN` | 2 (SPY, QQQ) | **0** |
| readings per ticker | 3 | 2 |
| rate limits | 0 | 0 |
| unrecognized error bodies | 0 | 0 |

**The 60.1% prediction landed at 60.6% measured — within 0.5pp.** SPY dropped its 1,205-contract
wide reading; both names now fetch cleanly. A config comment records the cost, the corrected
causality, and the two conditions for re-enabling.

### The coverage number moved — and it is not a regression

Reported coverage read 88% today against 96% yesterday. That looked like a regression, so I
checked rather than explained it:

| scan | reported | failed BOTH sides | true coverage | rescued by the 5-120 window |
|---|---|---|---|---|
| 09-01 08:52 | 96% | 6 | **89%** | ABBV, CLF, NEE, PFE, PLD, USB |
| 09-01 09:51 | 100% | 1 | 98% | JNJ |
| 09-01 10:52–12:52 | 100% | 1 | 98% | LMT |
| 09-01 13:51, 14:52 | 100% | 0 | 100% | — |
| **09-02 08:45** | **88%** | 7 | **88%** | — (none possible) |

`chain_coverage()` does `skipped -= passed` — a ticker seen in *any* window counts as covered. The
wide 5-120 window was a third window, and it was passing for tickers that had failed **both**
trading chains, silently crediting them. Removing it removed the false credit.

**Like for like: 89% true yesterday → 88% true today. Flat.** Coverage did not drop; the metric
stopped inflating, and it can no longer be inflated by adding a window. My Part 1 falsifier
verdict of "96→100%" was itself partly this artifact and should be read as 89→100%.

---

## Step 2 — Crypto boundary enforced, not extracted ✅ COMPLETE

**Your call, after I corrected the premise I had supplied.** The brief said crypto "ran inside all
seven scans" — my wording, and wrong. It came from 38 Coinbase readings whose `scan_id` was `None`;
`crypto._record_quality` simply never passes one, so that field proved nothing.

Measured properly, from the 09-01 14:35 cycle:

```
14:35:02  main.py (run_scan)     17m41s   <- SUBPROCESS; exits before any crypto runs
14:52:43  vega_candidates.py      8m23s
15:01:33  _record_btc_forecast       ~7s  <- crypto
15:02:00  _record_crypto_premium     ~5s  <- crypto
          total 27m40s
```

**12 seconds of 27m40s (0.7%), in a different process, spending zero options request budget.** The
one in-scan hook is `assessment._btc_cross_venue`, which short-circuits before any I/O unless the
ticker is in `BTC_PROXY_TICKERS` (`{"IBIT"}`) — and IBIT is on the watchlist deliberately for
uncorrelated VRP, with DVOL-vs-IBIT-IV being a premium read that serves the mission.

Delivered instead of an extraction:
- **`tests/test_crypto_boundary.py`** — 12 tests holding the line *behaviourally*: the crypto
  reading is driven to both extremes and `qualified`/`failed_gates` must not move; the gate set
  must still equal `config.REQUIRED_GATES`; only declared proxies may reach an endpoint; a dead
  Deribit must not break the equity scan; the two ledgers stay distinct; the cohort key grows no
  crypto dimension; and the scan subprocess still runs *before* any crypto phase.
- The measured boundary documented in `config.py` above `BTC_SIGNAL_ENABLED`.
- `VEGA_Part2_Crypto_Research_Brief` §0 rewritten, with the error marked as mine.

---

## Step 3 — Watchlist audit ✅ DELIVERED as a proposal — **awaiting your sign-off**

Full artifact: **`VEGA_Watchlist_Audit_2026-09-02.md`**. Nothing has been cut; the watchlist
config is untouched.

**The premise was wrong in both directions.**

The brief's 16-name chronic list is the **whole-grid** list — the metric that skipped GE for weeks
while GE was 100% quotable in the band it actually sells. `data_quality_log` stores that metric;
the gate uses the delta band. Ranking on the log file would have repeated the GE error at scale.

On the correct metric, over 7 scans:
- **42 of 56 names never fail.** Only 14 have ever been skipped.
- **Two are chronic**: LMT (71%) and ABBV (43%).
- **Five names the brief calls chronic have a 0% skip rate: GE, BAC, BLK, AMGN, XBI** — four of
  them sitting *below* the old whole-grid floor while passing the real gate every scan.
- CLF is the inverse trap: 87% whole-grid, third-best on the board, and it still failed 29%.

So there is no third of the watchlist to reclaim on chain quality. There are two names, worth
~3.5% of fetch. **The budget win was Step 1 (60.6%), not this.**

**The real dead weight is a different question.** From 1,557 spreads formed over 8 trading days,
**31 of 56 tickers produced zero qualified spreads** — and the regime excuse does not hold:

```
zero-qualifier tickers (n=31) : median IV rank 26.9
producing tickers      (n=25) : median IV rank 25.4
```

Six zero-qualifiers sat in exactly the premium-rich conditions the strategy wants and still
produced nothing — XBI 49 spreads at median IVR 56 → 0; also BAC, PEP, XOM, TLT, CRWD.

**Recommendation: approve Tier B now (LMT, ABBV — chain quality, clean call on 7 scans); hold
Tier A (XBI, CRWD, PEP, XOM, TLT, BAC) for one more week.** Tier A is a claim about *edge* on 8
trading days, and the asymmetry favours waiting: a cut name produces nothing forever, while a kept
name costs only budget Step 1 already freed 60% of.

---

## Step 4 — The gate ✅ BUILT, because there wasn't one

### The brief said not to assume a soft convention. It was right to.

I checked what was actually preventing the first `caps_v1` trade from opening. **Nothing was.**
Entry was not gated; it merely was not happening, because the board has qualified almost nothing
since 2026-08-10. The moment one board qualifies, `_auto_open_from_board` opens up to
`MAX_NEW_OPENS_PER_RUN` positions with no further approval. Relying on that is relying on the
drought to keep holding the door.

Today's 08:35 cycle ingested `qualified=0`, so nothing opened — by luck, not by control.

### Built and proven

- **`config.ENTRY_HOLD = True`** with `ENTRY_HOLD_REASON`, checked first thing in
  `_auto_open_from_board`, before the board file is even read.
- Deliberately *not* `VEGA_MAX_OPEN_TOTAL=5`, which would have the same effect today but reads as
  a capacity limit, stops holding the moment a position closes, and records no reason.
- **Cohort-safe:** it gates entry *timing*, not selection. Nothing keyed by `cohort()` moves.
- **`tests/test_entry_hold.py`** — 6 tests. The one that matters uses a board carrying a live,
  fully-gated, fillable trade; with the hold removed, that test **fails**, proving the trade would
  otherwise have opened.

> Worth recording: my first version of that fixture put the gates under `gates` when the open path
> reads `assessment_gates`. The trade was rejected as "not fully gated" and the hold test passed
> for the wrong reason — proving only that a malformed board opens nothing. Caught by removing the
> hold and finding the test still green.

A `conftest` fixture now sets `ENTRY_HOLD = False` for every other test, so the per-run, per-day and
per-expiration caps stay under test while the hold is deployed on. Without it, twelve
entry-diversification tests went red at once and those caps would have been untested for as long
as the hold stayed up.

**Full suite: 1328 passing.**

### To lift the hold

Set `config.ENTRY_HOLD = False`. `test_the_hold_carries_a_reason_and_is_currently_ON` reads the
committed source and will go red — that is the intended notification. Delete it in the same commit,
so releasing entry is a reviewed act rather than a flag flip.

---

## Not in the brief, found while doing Step 4 — please read

`main._get_open_position_tickers()` queries the **JARVIS tower** (`/vega/outcomes?status=open`)
and treats it as authoritative, falling back to the local ledger only if that returns nothing.

**JARVIS holds 399 rows with `status='open'`, across 33 tickers, dated 2026-07 through 2026-09.**
The local ledger holds **5**. Every qualified trade the scan ingests becomes an `open` row on the
tower and nothing ever closes it — they are board candidates, not positions. This is where the
original brief's "25 open positions" came from; I called that number simply wrong, and it was
actually a real number from a different, phantom book.

**It does not block anything** — `_annotate_book_awareness` only flags (`ALLOW_SAME_TICKER=False`
means "flag, do not drop"), and `_auto_open_from_board` never reads the flag. But it does corrupt
decision assistance, which under the mission is not cosmetic:

- Candidates on **33 of 56 tickers** are annotated *"ALREADY IN POSITION — open exposure already
  exists in this underlying"*, and `verdict.py` renders that to you as *"You already hold X. This
  doubles down rather than spreading risk."* For 28 of those tickers, you don't.
- `book_risk_usd` in the scan artifact sums `max_loss` over 399 phantom rows.

**Not fixed, deliberately** — it is outside the four steps, it touches the scan path, and the fix
is a judgement call between reconciling the tower table and demoting it below the local ledger.
Flagging it for a decision.

---

## Where this leaves the scanner

| | before | after |
|---|---|---|
| fetch volume per scan | 19,286 contracts | **7,595** (−60.6%) |
| scan wall clock | 17m 41s | **10m 24s** (−41%) |
| chains truncated per scan | 2 (SPY, QQQ) | **0** |
| true coverage | 89% | 88% (flat, now non-inflatable) |
| crypto boundary | assumed | **enforced by 12 tests** |
| entry control | none | **`ENTRY_HOLD`, proven to stop a live board** |
| tests | 1310 | **1328** |

**Steps 1, 2 and 4 are complete and revalidated. Step 3 is delivered and awaiting your decision** —
which is exactly why the hold is on. The scanner is now spending its budget correctly and the
coverage metric no longer flatters itself, but the watchlist it spends that budget on is still
under review, and the brief's own sequencing says the first `caps_v1` trade should be selected by a
scanner whose watchlist has already been decided.

**Ready for the first `caps_v1` trade once you decide Tier A/Tier B — and I am not proposing to
lift the hold before then.**
