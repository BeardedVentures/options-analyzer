# VEGA — Sizing the loose-predicate exposure

**Date:** 2026-09-04
**Branch:** `session/2026-09-03-prediction-engine`
**Question:** the call path applies no crossability test. Is that harmless in practice, or is the board recommending structures at credits that cannot be reached?

**Answer: neither, and the distinction matters.** Roughly half the board's structures sit on strikes the strict path would refuse — but the refusals are dominated by **thin markets, not mispriced ones**, and the recorded credits are not fictional. What is untested is size, exit, and whether the quote is live.

---

## §A — Correcting my own framing first

In my previous message I wrote that these recommendations may sit "at credits that may not be reachable," and called it the 72%-on-mid defect wearing a different mask. **That is wrong, and the leg data shows why.**

The call path prices at the **natural** credit — sell the short at its bid, buy the long at its ask. That is the fillable price *by construction*. A wide book does not make the natural credit unreachable; it makes the credit **smaller**, and the credit floors then judge it. The mid-vs-natural defect recorded a price that could not be obtained in principle. This does not.

So this is a **different** defect, and a less severe one:

| | mid-vs-natural (fixed 08-10) | the loose predicate |
|---|---|---|
| the recorded price | unachievable in principle | achievable right now, for one contract |
| what is wrong | the number is fiction | the number is real but untested for **size, exit, and liveness** |

## §B — The tail is where the ratio lives, and it is 2.5×

Population: the 18 reachable tickers of the 22 that enumerated no valid spread on the 09-04 09:35 scan (JNJ and PLD were skipped by the chain-quality floor mid-measurement, which is itself a data point).

| | band strikes | loose admits | strict admits |
|---|---|---|---|
| **tail (18 names)** | 164 | **119 (73%)** | **48 (29%)** |
| liquid sample (6 names, earlier) | 91 | 86 (95%) | 63 (69%) |

**The predicate gap is 2.5× in the tail against 1.4× in the liquid sample.** The earlier six-name reading understated it, exactly as predicted, because SPY and NVDA sit near a 0.01 relative spread and drown the effect.

Per-ticker, the tail spreads are genuinely wide in places — XLV 0.540 median, AMGN 0.461, NEE 0.449, BLK 0.435, COP 0.428, XBI 0.411 — and BLK admits **zero** strikes under the strict test.

## §C — The survival count: 7 of 15

Of the structures `multi_strategy._best_wing` would actually build on these names *right now*:

| ticker | structure | survives strict? | why refused |
|---|---|---|---|
| AMGN | 470/480 | ✅ | |
| WMT | 112/114 | ✅ | |
| KO | 92.5/95 | ✅ | |
| AMT | 185/190 | ✅ | |
| XLE | 68/70 | ✅ | |
| XBI | 175/180 | ✅ | |
| TLT | 84/84.5 | ✅ | |
| BAC | 65/67 | ❌ | liquidity |
| GS | 1110/1120 | ❌ | liquidity |
| GE | 350/360 | ❌ | liquidity |
| XLV | 178/181 | ❌ | liquidity |
| USB | 66/68 | ❌ | spread (36%, 55%) |
| PEP | 144/147 | ❌ | liquidity short, spread long (43%) |
| COP | 145/155 | ❌ | spread on the long |
| NEE | 87/89 | ❌ | spread on the long |

**7 of 15 survive (47%).** Of the 8 refusals: **4 on liquidity alone, 4 involving a too-wide leg.**

## §D — What the refused legs actually look like

This is the part that changes the interpretation:

| ticker | leg | strike | bid | ask | spread | volume | OI |
|---|---|---|---|---|---|---|---|
| BAC | short | 65.0 | 0.57 | 0.65 | **13.1%** | **6** | **78** |
| GS | short | 1110.0 | 10.30 | 12.30 | 17.7% | **20** | **12** |
| GE | short | 350.0 | 4.10 | 4.45 | **8.2%** | **1** | **26** |
| XLV | short | 178.0 | 1.19 | 1.38 | 14.8% | **7** | **40** |
| USB | short | 66.0 | 0.43 | 0.62 | 36.2% | **0** | **13** |
| PEP | long | 147.0 | 0.48 | 0.74 | 42.6% | 7 | 250 |

**Most of the refused legs are tightly quoted.** GE's short is an 8.2% spread — better than many strikes that pass — on a strike with **volume 1 and open interest 26**. BAC's is 13.1% on volume 6.

The dominant tail failure is **not that the market is wide. It is that there is barely a market.** These are quotes on strikes nobody is trading, and the strict predicate's liquidity floor (volume ≥ 25 **or** OI ≥ 100) is what catches them; the spread cap catches a smaller second group.

## §E — What follows, and what does not

**Does not follow:** that the recorded credits are wrong. They are natural-basis prices and obtainable for a contract.

**Does follow:**

1. **Size is untested.** GE 350C carries OI 26 and volume 1. A multi-contract order has no demonstrated counterparty. Nothing in the pipeline models fill size, and the paper ledger assumes one contract fills at the natural.
2. **Exit is untested, and it is the larger half.** Entry crosses the spread once; the 50%-of-max-profit target crosses it again on a strike with single-digit volume. The strict predicate was protecting the **round trip**; the loose one screens neither leg of it.
3. **Liveness is untested.** USB 66C shows volume 0 and OI 13 with a 36% spread — that is plausibly a market-maker placeholder rather than a market.

**And the one genuinely reassuring number:** 7 of 15 structures survive, and those seven (AMGN, WMT, KO, AMT, XLE, XBI, TLT) are on strikes that clear both tests. Roughly half the board is fine by the strict standard. This is not a board built entirely on air.

## §F — Recommended, not done

**I did not tighten `_tradeable`.** Bringing it to parity would change which structures the board builds, mid-drought, on a path that is currently the only one producing — and the measurement above says the right answer is not obvious: 4 of the 8 refusals are liquidity calls on strikes with *tight* quotes, where "no volume today" and "cannot be traded" are not the same claim.

What the measurement does support, in order of confidence:

1. **Record the strict verdict alongside each call-side recommendation** without acting on it — one field, no selection change, and it turns this from a one-off audit into a standing series. The `leg_quotes` field committed earlier today already makes this computable after the fact.
2. **Decide the liquidity floor deliberately.** `volume >= 1 or OI >= 10` against `volume >= 25 or OI >= 100` is a 10–25× gap that nobody chose; one of the two was picked and the other inherited.
3. **The exit problem deserves its own measurement.** Round-trip cost on the strikes actually recommended is unmeasured, and `estimated_round_trip_cost_per_contract` exists on the ledger — comparing it against the real spreads on those legs is now possible with persisted quotes.

## §G — Sample limits

One moment, one session, 18 tickers, call side only. The survival count is of structures `_best_wing` would build *now*, not of structures actually recommended since 08-11 — those cannot be re-tested because their leg quotes were never persisted, which is the gap closed earlier today and only closed going forward. The first genuinely retrospective version of this measurement is possible on recommendations written from the next scan onward.
