# VEGA Master Brief — Validation & Execution Record
**2026-08-11** · reviewing `VEGA_Master_Brief_Final_2026-08-11.docx`

Every claim in the brief was treated as a hypothesis and checked against the code before
anything was built. That was the right call: **four of the brief's instructions were wrong**,
and two of them would have made the system worse if executed as written.

Test baseline confirmed before touching anything: **750 passed**. After this work: **858 passed**.

---

## 1. Findings — where the brief is wrong

### 1.1 P0-A would have REGRESSED live code  ⚠ most serious

The brief says to check whether `_candidate_score()` uses `edge_score` and, if absent, paste in
a replacement. It is already there ([auto_paper_cycle.py:296](auto_paper_cycle.py#L296)) — and the
brief's snippet is a strictly worse version of it, reintroducing two bugs the current code
carries explicit comments about fixing:

| | Brief's snippet | Live code |
|---|---|---|
| edge scaling | `float(c.get('edge_score') or 0.0)` — 0–100 scale, then `× 50` | `/100.0` first, so it shares the 0–1 scale of every other term |
| gates denominator | `c.get('gates_total') or 8` | `len(config.REQUIRED_GATES)` |

With the brief's version, `edge` contributes up to **5000** against `q`'s 30 and `true_pop`'s 20 —
the score becomes edge_score and nothing else. The hardcoded `8` is the exact literal the live
comment records as having been wrong: three gates were added after it was written, so a
candidate missing the key scored `gates_passed/8 > 1.0`.

**Action: not applied.** The existing implementation is correct.

### 1.2 P0-B is already complete

`_earnings_clear()` ([analysis/assessment.py:466](analysis/assessment.py#L466)) already fails closed on
`None` and already short-circuits on `has_earnings is False`, with 13 tests in
`tests/test_earnings_gate.py` pinning exactly the behaviour the brief asks for.

The brief's suggested DECLARED short-circuit list (`IBIT, TLT, SPY, QQQ, IWM, GLD, GDX`) is also
based on a misreading — `DECLARED` contains only 5 tickers, but ETFs are covered by an `_is_etf`
check at [vega_candidates.py:620](vega_candidates.py#L620), not by DECLARED membership. GLD/GDX/IWM
are **not** blocked. Adding them to DECLARED would have been harmless but pointless.

**Action: not applied.** Already correct.

### 1.3 P1-4's `PATTERN_DIRECTION` map keys a vocabulary that does not exist

This is the subtlest and most damaging error. The brief supplies a 16-entry map of textbook
pattern names. VEGA's detector (`analysis/structure.py`) emits **nine** labels, and only four
of the brief's keys correspond to any of them:

| Brief's map (16 keys) | Reality |
|---|---|
| `bull flag`, `bear flag`, `double top`, `double bottom` | ✅ real |
| `cup and handle`, `inverse head and shoulders`, `ascending triangle`, `falling wedge`, `head and shoulders`, `descending triangle`, `rising wedge`, `evening star`, `symmetrical triangle`, `pennant`, `rectangle`, `consolidation` | ❌ no detector produces these — 12 dead keys |
| — | ❌ **missing**: `RANGE`, `UPTREND_EXTENDED`, `DOWNTREND`, `PULLBACK`, `UNREADABLE` |

`DOWNTREND` is missing from a *directional* map. Had this shipped, the most obviously bearish
reading VEGA produces would have returned no direction, every live setup would have read
NEUTRAL, and the contradiction check would have passed 100% of the time — a green test suite
over a function that cannot fire. That is the same failure mode as
[claude-validating-against-invented-schemas]: a metric that cannot come out non-zero.

**Action: rewritten** against the nine real labels, with a test that asserts
`set(PATTERN_DIRECTION) == {the nine emittable labels}` so the map cannot drift from the detector
again.

### 1.4 Three P-items target files that do not exist or do not do the job

| Brief's target | Reality |
|---|---|
| `analysis/technical_score.py` (P1-4) | does not exist — technicals are `data/technicals.py`, patterns are `analysis/structure.py` |
| `vega_status.py` (P1-2, P1-3, A2-1) | a **read-only CLI health report**. It has no regime classifier and does not feed the dashboard. The Today's-call logic is `vega_app._mc_status_cards` |
| `vega_candidates.py` for pop_gap (P1-1) | already done — `set_pop_gap()` exists and is tested |

### 1.5a Two more ticker errors — verified live

| Brief says | Reality (checked 2026-08-11) |
|---|---|
| `CRYPTO_TICKERS = ['IBIT','ETHA','SOLI']` | **`SOLI` is delisted** — Yahoo returns no price data at all. `SOLZ` is the Solana ETF that exists and has options. |
| "SOL cross-venue data fetch — native DVOL not verified" | Verified: Deribit returns **HTTP 200 with zero data points** for `currency=SOL`. The endpoint exists, the index does not — the shape most easily mistaken for a working feed. |

ETH DVOL, by contrast, is real and live (49.4 against BTC's 35.9), so **P3 was buildable as
specified**.

### 1.5 P2-4's MOVE data path does not exist — verified live

The brief says to fetch the ICE BofA MOVE index from **FRED series `BAMLMOVE`**. Checked
against FRED directly:

```
GVZCLS     HTTP 200   observation_date,GVZCLS  2008-06-03,22.89   ← real
BAMLMOVE   HTTP 404                                               ← does not exist
VIXCLS     HTTP 200   (control)
```

GVZ is genuinely free and usable. MOVE is licensed by ICE and is not on FRED — FRED's `BAML*`
series are credit spreads, not the volatility index. Yahoo's `^MOVE` answers, but its last print
is **2026-07-17**, 25 days stale, while `^GVZ` was current to the day.

So **P2-3 (TLT cross-venue) has no free data source** under
[vega-no-paid-data-until-free-maxed]. TLT ships fully declared with the switch off and the
reason recorded (§5) rather than omitted — "blocked on a licensed feed" and "nobody thought
about it" must not look the same in config.

### 1.6 A2-1's premise is half right

The brief says "STAND ASIDE is being used to mean 'the environment is suboptimal'". The code
already reserves it for two genuine no-trade cases, and already has a `Selective` tier. The real
gap was the **missing middle** between "Selective" and "Sell Premium" — not the misuse the
brief describes. Implemented as a four-rung ladder rather than the brief's relabelling.

---

## 2. What was built

All backend items are wired to something that renders; none are config-only.

### Backend

| Item | Where | Note |
|---|---|---|
| **P1-2** opportunity-density funnel | `main.build_scan_summary()`, emitted into `scan_latest.json` | counts **real enumerated short/long pairs**, not tickers |
| **P1-3** four-state Today's call | `vega_app._mc_status_cards` | Stand Aside → Selective → **Cautious** (new) → Sell Premium |
| **P1-4** pattern direction | `analysis/structure.py` | `PATTERN_DIRECTION`, `get_pattern_direction()`, `check_thesis_contradiction()` |
| **P1-4** contradiction flag | `vega_app._thesis_contradiction` | surfaces in the Why section via the concentration-warning mechanism |

Two honesty constraints were enforced in the counting, because a proof-of-work number is
exactly the kind that invites inflation:

- `total_scanned` counts **only** the bull-put enumeration that `select_bull_put_pair` walks.
  Bear-call and condor structures come from `multi_strategy.scan_extra`, which reports no count,
  so they are **excluded rather than estimated**. The figure understates the work done and can
  never overstate it.
- The UI bar renders **nothing** when the board carries no `scan_summary` (fast rescan, or an
  artifact written before the field existed) rather than inferring a denominator from the row
  count. Pinned by a test.

### UI (Part A)

| Item | Change |
|---|---|
| A1 | `Lottery` → **Asymmetry**, `Bitcoin` → **Research** (Decisions 1 & 2, brief's own recommendations) |
| A2-1 | four-state call ladder |
| A2-2 | density funnel bar between KPI cards and the table |
| A2-3 | stars → **0–10 numbers** + permanent colour-matched legend; VRP value on the Premium card |
| A2-4 | `Win prob` → **VEGA POP**, with **Market POP** and **Edge (VEGA − market)** beside it |
| A2-6 | regime note → full-width coloured band (`.regband`) |
| A2-12 | `Recommended action` → **Recommended setup** (Decision 3) |
| A2-5 | edge-score decomposition on the card; `#1 of 27 qualified` rank context |
| A2-7 | open-risk exposure bar moved above the opportunity table |
| A2-8 | Why split into **Why it works / What can break it / What VEGA doesn't like**; model-confidence badge moved under the evidence it grades |
| A2-9 | secondary ideas name the component they lost on |
| A2-10 | max-risk **preset bands** (`<$100 / <$500 / <$1K / <$5K / Any`) beside the typed box |
| A2-11 | **WATCH / REJECT** with a gradeable decision ledger (see below) |
| A2-13 | exposure summary bar, hidden below two open positions |
| A3-3 | **Edge** column on the crypto Tradeable-now table |
| A3-4 | directional-claims **learning-period** banner below 5 resolutions |
| A4-2 | `HOME RUN 3x` → **TARGET $1,167** with the multiple as subtitle |
| A4-3 | non-varying `HIGH` conviction chip → **IV rank chip** (green ≤30 / amber / red ≥70) |
| A4-4 | direction tag: **momentum vs counter-trend**, not a fixed `BULLISH` |
| A4-5 | WATCH / REJECT on the asymmetry cards |
| A5-2 | phone breakpoints — breakpoints only, no separate mobile document |

### The decision ledger (A2-11) — `analysis/decisions.py`

The paper ledger is a censored sample: it holds trades that were **taken** and knows nothing
about the ones waved through. WATCH and REJECT write to `logs/vega_decisions.jsonl` with the
**full entry state** (strikes, expiry, credit, delta, `true_pop`, `pop_implied`, `pop_gap`,
`edge_score`) — because a row saying "rejected WMT on the 11th" cannot be graded against
anything once the chain moves. `pop_gap` is derived in the recorder rather than trusted from
the form, so the one number the ledger exists to grade can't go missing.

`summary()` compares mean edge score on each side of the operator's judgement. If rejects
score no lower than watches the overrides are noise; if they score *higher*, something is
wrong with either the score or the operator — worth catching early.

Both POST routes verified end-to-end against a live server, not just at module level.

**Tab renames change display labels only.** Route keys (`?view=lottery`, `?view=bitcoin`) are
untouched, so bookmarks and POST targets keep working.

### Decisions 1–3

The session was non-interactive, so the brief's own recommendations were adopted: **Asymmetry**,
**Research**, **Recommended setup**. Each is a one-line change (`nav()`'s `labels` dict and two
page titles) — trivially reversible if you want Momentum/Crypto instead.

---

## 3. Deliberately not built

**P2 / P3 / P4 — now built.** See §5.

Not built, and flagged here rather than silently dropped:

- **A2-7's "remove Today's Playbook — duplicates the table".** It does not duplicate it. The
  table is a sortable list; the playbook is role-based selection (safest / most aggressive /
  best EV), a different access path with its own tests. Kept.
- **A3-2 cross-venue simplification.** Depends on the P4 multi-asset refactor to be worth doing
  once rather than twice.
- **A5-1 progressive disclosure, A5-3 calibration table, A5-4 nav restructure.** The brief
  already defers these; A5-3 is gated on resolved Brier predictions (~2026-08-23).

---

## 4. Recommendation on the brief itself

The UI reasoning in this brief is strong — A2-4 (VEGA POP vs Market POP) and A2-2 (density
funnel) are the two highest-value changes in it, and both were worth building. The **code**
sections are where it goes wrong, and the pattern is consistent: it describes a plausible
version of VEGA rather than the one on disk. Three P-items were already complete, one was a
regression, one targets a nonexistent module, one targets the wrong module three times, and one
depends on a 404.

For the next brief: state the file:line the claim was verified against, or mark it explicitly as
unverified. Per [vega-audit-docs-need-reverification], "code-validated" in a handoff header has
not yet meant the code was read.

---

## 5. P2 / P3 / P4 — cross-venue architecture (built)

Every source claim checked against the live feed before any config was written:

| Signal | Source | Verdict |
|---|---|---|
| BTC DVOL | Deribit | ✅ 35.9 |
| ETH DVOL | Deribit | ✅ 49.4 — **P3 is real** |
| SOL DVOL | Deribit | ❌ HTTP 200, **zero data points** |
| GVZ | FRED `GVZCLS` | ✅ current to the day |
| MOVE | FRED | ❌ `BAMLMOVE` / `MOVE` / `ICEBOFAMOVE` all 404 |
| MOVE | Yahoo `^MOVE` | ❌ HTTP 200, plausible 75.46, **stale since 2026-07-17** |

The `^MOVE` case is the one worth remembering: reachable, plausible, and 25 days dead while
`^GVZ` was current. Nothing in the response says so.

**Shipped enabled:** IBIT (BTC DVOL), ETHA (ETH DVOL), GLD + GDX (GVZ).
**Shipped declared-but-off, with the reason recorded:** TLT (no free MOVE feed), SOLZ (no SOL
index). Those are not stubs — the renderer shows the reference, the venue, and why it is not in
service. Leaving them undeclared would make an asset blocked on a licensed feed
indistinguishable from one nobody had considered.

**Nothing is shared between assets.** Per-asset noise floors (GLD 0.5 · IBIT 2.0 · ETHA 4.0 ·
GDX 6.0), per-asset driver lists, per-asset independence. A shared floor reports gold noise as
signal several times a week and hides every real move in GDX behind an unreachable bar.

`data/vol_indices.py` exists rather than a two-line yfinance call because **every read is dated
and a stale read is not returned** — absent, not flagged. Callers are gap calculations, and a
subtraction will happily produce a confident number from a month-old operand.

### Bugs caught in my own work before merge

| Found by | Defect |
|---|---|
| rendering the page | Defaulting the currency to BTC for every non-ETH asset put **Bitcoin's spot, DVOL and variance premium on GLD's card** under gold's heading. Every value was individually true. |
| reasoning about GDX | `bool(derived_from)` filed an 11pp **miner-beta spread as "the likelier number to be wrong is ours"** — GVZ is derived from GLD's options, not GDX's. |
| review | `estimate_atm_iv` returns **0.0 as a sentinel**; accepting it made a failed reconstruction into a full-width gap (`+27.9pp ETF CHEAP` off an IV of zero). |
| review | ETHA was **permanently unavailable in the scan path** — only published indices were routed, so a working Deribit feed was one call away and never made. |
| review | GDX's structural gap got a **green seller's-edge badge every day**, beside a note saying it was not a mispricing. |
| review | Failed fetches were **never cached**, so a dead source was re-hit on every call. |
| review | A **NaN close** survived every downstream comparison and rendered as `nanpp`. |

All fixed and pinned by regression tests. Two full-system runs: **858 passed**, all six views
render, `vega_status.py` clean under `PYTHONUTF8=1`.
