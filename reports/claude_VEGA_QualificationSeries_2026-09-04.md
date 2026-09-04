# VEGA — The drought as a series

**Date:** 2026-09-04
**Branch:** `session/2026-09-03-prediction-engine`
**Source:** `logs/scan_log.json`, 363 scan entries. One query.

**The drought splits 91 / 9 between a corrected measurement and the market, and the corrected measurement is by far the larger half.**

---

## §A — The series

Qualified trades per scan, by day, against the fill-model commit and SPY's realized volatility.

| date | scans | total | mean/scan | max | VIX | SPY rv20 | |
|---|---|---|---|---|---|---|---|
| 2026-08-04 | 26 | 168 | **6.46** | 8 | 16.5 | 14.5 | |
| 2026-08-05 | 30 | 128 | 4.27 | 7 | 15.8 | 14.5 | |
| 2026-08-06 | 45 | 177 | 3.93 | 6 | 15.1 | 14.3 | |
| 2026-08-07 | 2 | 3 | 1.50 | 3 | 14.9 | 14.4 | *multi_strategy moves to natural credit* |
| 2026-08-08 | 1 | 2 | 2.00 | 2 | — | — | |
| 2026-08-10 | 16 | 59 | **3.69** | 6 | 15.5 | 14.0 | **`d6255b9` lands 17:46, after the last cycle** |
| 2026-08-11 | 12 | 8 | **0.67** | 1 | 15.3 | 14.1 | **first cycle on natural credit** |
| 2026-08-12 | 31 | 3 | 0.10 | 2 | 14.6 | 14.0 | |
| 2026-08-13 | 17 | 15 | 0.88 | 1 | 14.6 | 13.9 | |
| 2026-08-14 | 27 | 12 | 0.44 | 1 | 14.2 | 13.3 | |
| 2026-08-17 | 10 | 5 | 0.50 | 2 | 15.2 | 13.5 | |
| 2026-08-18 | 26 | 3 | 0.12 | 1 | 15.8 | 13.6 | |
| 2026-08-19 | 6 | 4 | 0.67 | 1 | 14.9 | 13.6 | |
| 2026-08-20 | 5 | 1 | 0.20 | 1 | 16.0 | 13.1 | |
| 2026-08-21 | 6 | 5 | 0.83 | 2 | 15.1 | 13.1 | |
| 2026-08-24 | 5 | 5 | 1.00 | 2 | 15.9 | 13.2 | |
| 2026-08-25 | 7 | 2 | 0.29 | 1 | 15.4 | 13.3 | |
| 2026-08-26 | 7 | 9 | 1.29 | 2 | 15.2 | **11.6** | |
| 2026-08-27 | 10 | 2 | 0.20 | 1 | 14.5 | **10.5** | |
| 2026-08-28 | 9 | 0 | 0.00 | 0 | 14.4 | **10.4** | |
| 2026-08-31 | 8 | 0 | 0.00 | 0 | 14.9 | **9.4** | |
| 2026-09-01 | 7 | 2 | 0.29 | 1 | 16.3 | **7.2** | last qualified trade |
| 2026-09-02 | 7 | 0 | 0.00 | 0 | 15.2 | 7.4 | **true zero begins** |
| 2026-09-03 | 7 | 0 | 0.00 | 0 | 14.3 | 8.3 | |
| 2026-09-04 | 2 | 0 | 0.00 | 0 | 14.0 | 8.0 | |

*Scan cadence changed over the window (26–45/day in early August, 7/day now, after the scheduler consolidation). Compare the **mean per scan** column; the totals are not comparable across the change.*

## §B — The split

| regime | scans | mean/scan | share of the original rate |
|---|---|---|---|
| mid-credit gating (… 08-10) | 120 | **4.47** | 100% |
| natural-credit gating (08-11 … 09-01) | 193 | **0.39** | 9% |
| natural credit + quiet tape (09-02 …) | 16 | **0.00** | 0% |

- **91% of the drought is the fill-model correction.** 4.47 → 0.39 on the first cycle after `d6255b9`.
- **9% is the market.** 0.39 → 0.00, three trading days ago, as SPY's 20-day realized vol fell from 13.3 to 7.2 and the quote split came back 79.2% *too wide to cross*.

## §C — What the two columns on the right show

The VIX column barely moves across the whole window — 16.5 to 14.0, a range of 2.5 points. It explains nothing about the 08-11 step, and it is why "quiet tape" was never a sufficient account of it.

**SPY's realized vol is the column that moves**, and it moves late: flat around 13–14.5 through 08-21, then 11.6 → 10.5 → 9.4 → **7.2** across the last week of August. That collapse lines up with the final 0.39 → 0.00, not with the 08-11 step. Two different causes at two different dates, and the series separates them cleanly where a single "since 08-10" number could not.

---

## §D — The correction, and where it propagated

**"Zero qualified since 2026-08-10" is wrong.** The board qualified **76 trades across 193 scans** between 08-11 and 09-01 — 1–2 per scan on most days. True zero began **2026-09-02**.

That claim was load-bearing this week and it is in documents I wrote. Corrected in place, with a pointer to this report:

- `reports/claude_VEGA_StoreFix_and_Reconciliation_2026-09-04.md` §G5
- `reports/claude_VEGA_PredictionEngine_2026-09-03.md` §E
- `analysis/liveness.py` module docstring

**Why the correction changes the shape of the problem.** Four weeks of total failure invites structural explanations, and it got nine. Three days of zero at the tail of a declining rate invites a much smaller one — and the smaller one is what the pre-registered quote split found. The wrong number was not just inaccurate; it was sized to justify the wrong class of hypothesis.

---

## §E — What this means for the standing conclusion

"The drought is the market" needs qualifying, not retracting:

1. **The 08-11 step is the system becoming honest.** VEGA stopped counting credit it could not collect. The prior rate of 4–6 qualified per scan was priced on mids, and the mid-vs-natural split is already known to be cohort-invalidating from the outcome side — 72% win rate on unachievable prices against 8% on achievable ones. This is that same defect measured at entry instead of at exit. **A system qualifying 6 spreads a day on prices no fill could achieve was not working better in July.**
2. **The 09-02 step is the market.** Wide books, 79.2% `quote_spread_too_wide`, SPY realized vol at 7.2.

Both are correct behavior. Neither is a bug. The honest one-line summary is: *the qualification rate is honest for the first time since July, and at that honest rate the current tape produces nothing.*

---

## §F — Open

1. **Log the CRWD and QQQ trades.** Now sharper than before: they are the only fill-verified ground truth, and they are the only thing that could validate the natural-credit fill model **from the outcome side**. The 08-11 step is a measured entry-rate change; nothing yet confirms the new basis is achievable in practice.
2. **The ranker/gate opposition** — recorded as a defect with a measured rate (§ methodology), deferred deliberately.
3. **Quantile mapping** — body-only until tested at 99%.
4. **Two origin branches** — operator's, public repo.
5. **The unpinned checkout** — fifth recurrence, now with a working demonstration attached: a diagnostic written this morning answered a question in the 09:35 scheduled scan. Valuable in that direction, which is exactly what makes an untested edit dangerous in the other.
