"""
config.py — Single source of truth for all user settings.
Never hardcode any of these values elsewhere in the system.

API keys and secrets are loaded from environment variables.
Locally: put them in a .env file (never commit .env).
GitHub Actions: store them as repository Secrets.
"""

import os
from pathlib import Path

# Load .env file if present (silently ignored if dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

# ─────────────────────────────────────────────
# ACCOUNT
# ─────────────────────────────────────────────
ACCOUNT_BALANCE = 500.00          # Update this as account grows
MAX_RISK_PER_TRADE_PCT = 0.20     # 20% of account per trade
MAX_RISK_PER_TRADE_USD = ACCOUNT_BALANCE * MAX_RISK_PER_TRADE_PCT

# ─────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────
# MAX_SPREAD_WIDTH was computed by get_max_spread_width(), which branched on ACCOUNT_BALANCE:
# 5 below $5,000 and 10 above. That was the ONE place account size genuinely reached the board
# — build_candidates refuses any pair wider than this, so at $500 a 10-wide spread was never
# enumerated and could not be seen, let alone sized.
#
# That is the wrong shape for a discovery tool. A 10-wide SPY spread is a real opportunity
# whether or not this account can carry it; RISK_TIERS already exists to say what it costs and
# let the operator decide. Declared directly at the wider value on 2026-08-14 so the board is
# balance-independent in fact and not only in intent.
#
# Note this WIDENS what gets enumerated and therefore raises max loss per spread. It is a
# cohort-affecting change, made on day 0 of the frozen cohort with zero trades accumulated —
# see the COHORT CONTRACT block below. Changing it later restarts the count.
#
# edge_calculator.select_best_strategy() was deleted alongside it. That one really was dead,
# and it caused a false claim on 2026-08-14 that iron condors were unreachable at $500. The
# ledger disproves it: 31 condors and 45 bear calls, produced by multi_strategy.scan_extra(),
# which contains no reference to account balance at all.
MAX_SPREAD_WIDTH = 10
MIN_CONTRACTS = 1                 # Always show at least 1-contract setup, even if oversized

# ─────────────────────────────────────────────
# STRIKE PLACEMENT — HARD RULES, NON-NEGOTIABLE
# ─────────────────────────────────────────────
SHORT_STRIKE_MIN_OTM_PCT = 0.03   # Short strike must be minimum 3% OTM from current price
SHORT_STRIKE_TARGET_DELTA = 0.20  # Target delta for short leg (range 0.15-0.25)
# Search floor. The engine sweeps every short strike in [MIN, MAX] delta and picks the
# winner on fillable credit rather than on nearness to TARGET -- the gates and the edge
# score are what should filter, not the search. Below this a strike carries so little
# premium it cannot clear the credit floors anyway, so it is only enumeration cost.
SHORT_STRIKE_MIN_DELTA = 0.12

# When quotes are STALE (outside 09:30-16:00 ET) the bid-ask blows out and the natural credit
# stops meaning anything -- GOOG 335/330 read $100 fillable at 14:47 and $30 at 18:03 with no
# move in the underlying. Off-hours the engine models the fill as the mid haircut by this
# ratio and marks the board PROVISIONAL. Measured, not assumed: across the 158 positively
# priced candidates in the 2026-08-10 14:47 intraday scan the natural ran a median 78% of mid.
# Re-measure as the ledger grows.
MODELLED_FILL_RATIO = 0.78
SHORT_STRIKE_MAX_DELTA = 0.30     # Absolute maximum delta — reject anything above this
MIN_STRIKE_BUFFER_SPY = 10.00     # SPY/QQQ: short strike must be $10+ below current price
MIN_STRIKE_BUFFER_STOCK = 0.05    # Individual stocks: 5% minimum buffer

# Tickers that use the SPY buffer rule
SPY_BUFFER_TICKERS = {"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT"}

# ─────────────────────────────────────────────
# STRATEGY PREFERENCES
# ─────────────────────────────────────────────
# main.py's own screen_ticker path hard-forces bull_put_spread. Bear call and iron condor are
# NOT unimplemented — they are produced by multi_strategy.scan_extra() and appended to the same
# qualified_trades list, and the ledger carries 45 and 31 of them respectively. The comment
# here previously said the engine "only implements bull_put_spread" and cited
# select_best_strategy as the override; both were wrong and that function is now deleted.
#   Genuinely not implemented: pmcc, csp
ENABLED_STRATEGIES = [
    "bull_put_spread",
    "bear_call_spread",   # live via multi_strategy.py — spot-check vs broker on first run
    "iron_condor",        # live via multi_strategy.py — spot-check vs broker on first run
]

# ─────────────────────────────────────────────
# SCAN CRITERIA
# ─────────────────────────────────────────────
MIN_PROBABILITY_OF_PROFIT = 0.72  # 72% minimum — true probability not just delta
MIN_IV_RANK = 45                  # Only trade when IV rank >= 45
MIN_CREDIT_USD = 25               # Minimum premium worth collecting (per contract)

# ── The credit floor scales with the underlying's price ──────────────────────
# A flat dollar floor is not price-neutral. $25 against SPY at $773 is 0.03% of spot; against
# IBIT at $36.80 it is 0.68% — the same rule, twenty times stricter, purely because the share
# price is lower. That is not a risk judgement, it is an artifact.
#
# It is what has kept IBIT out of the book entirely: on 2026-08-09 every one of the 17 IBIT
# candidates was blocked by min_credit_usd, and the best spread on the board carried $23 of
# natural credit against the $25 floor while PASSING credit_to_width at 0.230. Excluded by two
# dollars, on an underlying the watchlist deliberately added for its uncorrelated VRP.
#
# So the floor scales down for cheap underlyings and is CAPPED at MIN_CREDIT_USD — it can never
# loosen the rule for anything at or above the reference price. Nothing that qualifies today
# stops qualifying, and nothing above $100 changes at all.
#
# MIN_CREDIT_USD_FLOOR is the hard bottom: the credit must still be worth collecting after fees.
# Round-trip commission on a vertical is ~$2.16 (4 legs x $0.54), so $15 is roughly 7x fees —
# genuinely a fee-viability test, which the $25 never was at 11.6x.
CREDIT_FLOOR_SCALES_WITH_PRICE = True
CREDIT_FLOOR_REFERENCE_PRICE = 100.0   # At or above this, the floor is exactly MIN_CREDIT_USD
MIN_CREDIT_USD_FLOOR = 15              # Hard bottom; never scale below this


def min_credit_usd_for(spot=None) -> float:
    """The credit floor for one underlying, in dollars per contract.

    ONE definition. Four places enforce this floor — assessment.evaluate_gates,
    strike_validator, auto_paper_cycle._candidate_passes_minimum and main.py — and every prior
    enforcement leak in this system came from the same rule being re-implemented rather than
    shared. Callers pass the spot; callers that do not know it get the flat floor unchanged,
    which is the conservative direction.
    """
    base = float(MIN_CREDIT_USD)
    if not CREDIT_FLOOR_SCALES_WITH_PRICE or not spot:
        return base
    try:
        s = float(spot)
    except (TypeError, ValueError):
        return base
    if s <= 0:
        return base
    scaled = base * (s / float(CREDIT_FLOOR_REFERENCE_PRICE))
    return round(max(float(MIN_CREDIT_USD_FLOOR), min(base, scaled)), 2)
MIN_DTE = 25                      # Minimum days to expiration (targets the 25–45 DTE window)
MAX_DTE = 45                      # Maximum days to expiration
PREFERRED_DTE_TARGET = 35         # Prefer contracts near this DTE when multiple are valid
PREFERRED_DTE_TOLERANCE = 7       # Within +/- this range is considered ideal
# Stop geometry (revised 2026-07-31). The 0.50 / 2.0 pair was structurally EV-negative:
# every realized loss stopped out near 3.5x credit collected while wins took only 50%,
# giving avg win $69.84 vs avg loss $241.66 (profit factor 0.36). That needs a ~78% win
# rate to break even against a 76.3% modeled POP — negative before selection quality even
# matters. Tightening the stop AND raising the target fixes both sides of the ratio.
TARGET_PROFIT_PCT = 0.65          # Close winners at 65% of max profit (was 0.50)
STOP_LOSS_MULTIPLIER = 1.5        # Stop if spread reaches 1.5x credit received (was 2.0)
ALLOW_OVERSIZED_TRADES = True     # Account-agnostic output — risk tiers handle sizing
MAX_QUOTE_SPREAD_PCT = 0.35       # Reject option legs with (ask-bid)/mid above this threshold

# ── ATM IV integrity (2026-08-09) ────────────────────────────────────────────
# iv_history feeds IV RANK, which is the system's only answer to "is premium rich right now".
# Two functions used to write to it — a median over a near-ATM band, and the IV of the single
# contract nearest spot — and the single-contract one ran during market hours, where one bad
# live quote decides the number. SPY's stored history reads 34-68% on weekdays and 12-14% on
# weekends as a result, and 10% of every observation on the watchlist is more than 3x that
# ticker's realised vol. Errors are always HIGH, which biases the percentile DOWN and makes
# MIN_IV_RANK reject setups that deserved to pass.
ATM_IV_MIN_CONTRACTS = 3          # Near-ATM contracts needed before a median is trustworthy.
                                  # The window widens (3%→5%→8%→12% of spot) until it has this
                                  # many, instead of falling back to the whole-chain median —
                                  # a smile-weighted number that is not ATM IV at all.
PROFILE_MIN_OBSERVATIONS = 20     # Usable IV observations before a ticker's own IV range is
                                  # described as known. Below it, analysis/ticker_profile.py
                                  # says so rather than reporting a percentile — IBIT has 3.
IV_PLAUSIBLE_MAX_MULT = 3.0       # Stored IV above this multiple of the ticker's OWN realised
                                  # vol is a bad quote, not a regime. Filtered at READ time;
                                  # the history files are never rewritten, so the audit trail
                                  # survives and nothing can be silently lost.

# ── BTC cross-venue signal (2026-08-09) ──────────────────────────────────────
# IBIT options and BTC options price the same underlying risk in two venues. Deribit publishes
# DVOL (BTC 30-day implied vol) for free; IBIT's ATM IV comes off a chain already being fetched.
# The gap between them costs nothing to measure and needs no forecast to be informative.
#
# ADVISORY BY CONSTRUCTION: this never enters the gates dict, so it cannot block a trade
# regardless of what it reads. See analysis/btc_signal.py.
BTC_SIGNAL_ENABLED = True
# Only underlyings that track BTC ~1:1. Measured 2026-08-09: DVOL 34.24 vs IBIT 32.72 (a 1.5pt
# gap between two prices for the same risk) but vs COIN 65.23 (a 31pt gap). COIN is an operating
# company with its own equity risk, so comparing its IV to BTC's measures the difference between
# the assets, not a mispricing of one. Do not add it.
BTC_PROXY_TICKERS = {"IBIT"}
BTC_IV_GAP_WIDE_PP = 3.0          # Vol points before the venues are called meaningfully apart.
                                  # PROVISIONAL — reasoned, not fitted. Nothing has graded it
                                  # yet; the raw gap is what gets persisted for calibration.
BTC_RV_WINDOW = 30                # Days of BTC realised vol, annualised on 365 (BTC never closes)

# ── BTC directional forecast (2026-08-09) ────────────────────────────────────
# One dated, probability-carrying claim per day, written to the SAME prediction ledger as
# VEGA's own claims. predictions.DIRECTION was already built, scored and tested — and had never
# been recorded once. This starts the validation clock now rather than after a fusion engine
# exists, and grades ATLAS on the same scale as everything else instead of in a private table.
#
# The model is deliberately small and the confidence deliberately timid: a 20/50 crossover has
# no business claiming 80%, and the Brier score punishes overconfidence twice. Expect the first
# verdict to read "underconfident — this deserves more weight". That is the correct place to
# start, and it is the baseline any later on-chain layer has to beat.
BTC_FORECAST_ENABLED = True
BTC_FORECAST_HORIZON_DAYS = 14    # Claims resolve in 14 days; a daily cadence still yields 60
                                  # samples in 60 days rather than 60 x 14.
BTC_FLAT_BAND_SIGMAS = 0.5        # "Flat" = within half a 14-day sigma of BTC's OWN vol. At 34
                                  # vol that is ~±3.4%; the ledger's equity default of ±1% would
                                  # be 0.15 sigma and make flat unreachable.
BTC_TREND_FLAT_PCT = 0.5          # 20d/50d SMA gap under this calls flat rather than a side
BTC_FORECAST_BASE_PROB = 0.52     # Confidence floor for a directional call
BTC_FORECAST_PROB_STEP = 0.03     # Added per extra unit of agreement between the drivers
BTC_FORECAST_MAX_PROB = 0.62      # Hard ceiling — this model cannot earn more than this
BTC_HIGH_VRP_PP = 8.0             # Implied-over-realised above this trims conviction (crowded
                                  # protection is a confidence signal, never a direction)

# How long a claim may stay unreadable before it is written off. A failed price lookup used to
# mark a claim `unresolvable` FOREVER — one network blip on the day it came due deleted it from
# the record, and because the sample only ever shrinks the loss is invisible. Retry instead.
PREDICTION_RESOLVE_GRACE_DAYS = 5

# ── Chain data quality (2026-08-08) ──────────────────────────────────────────
# Every signal VEGA produces is a statement about an options chain, and nothing used to record
# how much of that chain was actually there. The yfinance fallback discards 30-45% of records
# as stale or unquoted; an IV rank, a skew read or a delta measured over the remainder is a
# read of the survivors. It looked identically confident either way.
CHAIN_QUALITY_LOG_ENABLED = True  # Write a per-ticker per-scan reading to data/data_quality_log.json
CHAIN_QUALITY_GATE_ENABLED = True # Below the floor, return an empty chain instead of a thin one
# Raised 0.30 -> 0.50 on 2026-08-14, BEFORE activating a better chain source. The threshold
# decides which tickers are eligible, so it is part of the cohort contract and cannot be moved
# once the count starts. 0.30 was calibrated to yfinance's degraded chains (33-68% of records
# discarded during market hours); scoring a ticker off a third of its chain is how a gate ends
# up evaluating a name it cannot see. Set to where it should be for good data, accepting fewer
# names in the interim.
CHAIN_QUALITY_MIN_RATIO = 0.50    # Floor. Under 50% quotable, skip the ticker for this scan.
CHAIN_QUALITY_GOOD_RATIO = 0.70   # At or above this the chain reads green in the cockpit tile.
# How wide a window counts as "the last scan" when the cockpit summarises chain quality.
# scan_id is per PROCESS, and one cycle runs several — the engine scan, vega_candidates, then a
# mark loop over only the tickers already holding positions. Summarising the newest scan_id
# alone therefore described the mark loop, which is healthy by construction, and the tile read
# green on 2026-08-25 while that cycle skipped ten tickers. 30 minutes covers one cycle
# (they run 11-16 min) without reaching the previous hour's.
CHAIN_QUALITY_SCAN_WINDOW_MIN = 30

# ─────────────────────────────────────────────
# GATE ENFORCEMENT CONTRACT
# ─────────────────────────────────────────────
# Three enforcement leaks in one week (IV-rank 07-25, POP floor 08-02, quote-spread 08-02) shared
# one shape: a rule defined here, annotated by the scanner, then omitted from the path that actually
# opens trades. REQUIRED_GATES is the contract between the two.
#
# WHERE ENFORCEMENT ACTUALLY LIVES (corrected 2026-08-20). This block used to name
# _candidate_passes_minimum() and _auto_open_from_candidates() as the enforcers. Neither is the
# enforcer any more: _auto_open_from_candidates() was DELETED when the desk was rewired to open
# from the board, and _candidate_passes_minimum() survives only inside the orphaned, unreachable
# selection subtree (see the banner above _pick_new_trades in auto_paper_cycle.py). A reader
# following the old wording would audit code that cannot run — the exact failure this project
# has been bitten by before.
#
# The live enforcer is auto_paper_cycle._auto_open_from_board(): it reads each board trade's
# `assessment_gates` dict, refuses to open when any REQUIRED_GATES key is missing or False, and
# logs "SKIP <ticker> — board trade is not fully gated". Every key must therefore be EMITTED onto
# that dict by the board builder; a gate dropped upstream fails loudly here instead of silently
# widening what gets traded.
#
# NOTE: these are the scanner's gate KEYS, not the config knob names above — they must match the
# board's `assessment_gates` dict exactly. One rule is deliberately NOT here:
#   - IV rank : enforced in main.py:647-653 during screening, per TICKER, because iv_rank is a
#               property of the underlying rather than of an individual spread. Be aware that
#               enforcement there is CONDITIONAL: a below-threshold reading is a hard reject only
#               when iv_rank_method == "HISTORY". On the approximated path (thin IV history) it
#               writes tech["iv_rank_warning"] and lets the ticker through — and nothing reads
#               that field, so the softest case is also the silent one. Sitting outside
#               REQUIRED_GATES, it is invisible to the contract above. See MIN_IV_RANK.
REQUIRED_GATES = [
    "delta_cap",
    "otm_buffer",
    "credit_to_width",
    "min_credit_usd",
    "liquidity",
    "pop",
    "dte_window",
    "quote_spread",
    "natural_credit_positive",
    "earnings_clear",
    "support_shelter",
]

# Structural shelter gate. The auto-open path placed strikes on delta and OTM percentage
# alone and never consulted a support level, so a short strike could sit in open air with
# nothing to break on the way down.
#
# Evidence, 2026-08-07 — the first full day under the new close logic. Five entries:
#   SMH  strike -0.55 sigma, nearest support -0.43  (support ABOVE strike)  survived
#   NEE  strike -0.65 sigma, nearest support -0.24  (support ABOVE strike)  survived
#   COP  strike -0.58 sigma, nearest support -0.41  (support ABOVE strike)  survived
#   GDX  strike -0.59 sigma, nearest support -1.26  (strike in OPEN AIR)    died same day
#   GDX  strike -0.72 sigma, nearest support -1.54  (strike in OPEN AIR)    died same day
# The two failures were exactly the two with no defended level above the strike.
#
# A same-day stop on a 30+ DTE thesis is an entry problem, not an exit one. Cost measured on
# the 2026-08-07 14:39 snapshot: 67 candidates passing the credit gates -> 36 with shelter.
# Fails OPEN when levels are unavailable, so a data gap cannot empty the board.
SUPPORT_SHELTER_GATE_ENABLED = True

# Kill switch for the earnings gate. It fails CLOSED for a non-ETF whose earnings date cannot be
# resolved, so a mass yfinance calendar outage would block every equity candidate (ETFs still
# trade). Set False to fall back to the prior behaviour of ignoring earnings entirely.
EARNINGS_GATE_ENABLED = True
MIN_SPREAD_WIDTH_SPY_LIKE = 1.0   # Minimum spread width for SPY-like tickers (flat — not account-size dependent)
MIN_SPREAD_WIDTH_OTHER = 1.0      # Allow 1-point width on non-index symbols
ALLOW_NARROW_SPREAD_EXCEPTION = True
NARROW_SPREAD_MIN_CREDIT_TO_WIDTH = 0.20  # H2 fix: was 0.30 — a 0.20Δ spread pays ~13–20% of width
# Leg liquidity floor. A leg passes on EITHER test (volume OR open interest), so these are
# not "and" conditions — see select_bull_put_pair.
#
# Lowered 100/500 -> 25/100 on 2026-08-11, after measuring what the old floor actually did.
# It was the single largest filter in the system: 1,037 of ~1,530 leg rejections in the
# 2026-08-11 scan, and 28 of 56 tickers produced NO valid spread at all. Measured against live
# 25-45 DTE chains it passed only 20% of legs on mega-caps — MU 20%, UNH 24%, WMT 25%, and
# AMGN exactly ZERO — because open interest concentrates in front-month and round strikes
# while the median leg in this DTE window carries OI of 21-38. The board was not reading a
# thin market; it was reading a filter calibrated for a different part of the chain.
#
# Loosening it is affordable because THIS SYSTEM ALREADY MEASURES FILLABILITY DIRECTLY.
# _quote_is_tradeable rejects any leg with a crossed book, a missing side, or a bid-ask wider
# than MAX_QUOTE_SPREAD_PCT, and every credit is gated and ranked on the NATURAL basis (sell
# the bid, buy the ask). Open interest is a proxy for exactly the thing those two already
# check, so a heavy OI floor stacked on top was double-counting the same risk and paying for
# it in coverage. 25/100 still excludes genuinely dead strikes; the quote test remains the
# binding constraint, which is the right place for it.
MIN_OPTION_VOLUME = 25
MIN_OPTION_OPEN_INTEREST = 100
# H2 fix: hard floor lowered 0.25 → 0.15. A 0.20-delta short strike structurally collects
# ~13–20% of width in normal vol (Cboe/industry), so a 25% floor was mutually exclusive with
# the 0.20Δ strike target and silently rejected most valid index spreads. 0.15 is the true floor;
# 0.33 remains the "ideal" warning threshold in strike_validator. Safety now leans on the OTM
# buffer + the probability-of-profit gate, which is the correct place for it.
MIN_CREDIT_TO_WIDTH_PCT = 0.15

# ─────────────────────────────────────────────
# EXECUTION COST MODEL (Gate 1 realism) — Robinhood-accurate
# ─────────────────────────────────────────────
# Robinhood options pricing (verified 2026-07): $0.50/contract (non-Gold) or $0.35 (Gold),
# PLUS ~$0.04/contract combined regulatory + exchange fees, charged on BOTH open and close.
# So per-leg-per-direction ≈ $0.54 (non-Gold) / $0.39 (Gold). A vertical = 2 legs, round trip
# = 4 leg-fills. These are the MEASUREMENT baseline for honest paper P/L — not something to
# optimize yet. Set ROBINHOOD_GOLD=True if you carry Gold.
ROBINHOOD_GOLD = False
_RH_CONTRACT_FEE = 0.35 if ROBINHOOD_GOLD else 0.50
_RH_REG_EXCH_FEE = 0.04
COMMISSION_PER_CONTRACT_PER_LEG = round(_RH_CONTRACT_FEE + _RH_REG_EXCH_FEE, 2)  # ≈0.54 / 0.39
# Slippage is only used by the scanner's *modeled* estimate. Paper P/L captures real friction
# through the actual entry/exit prices you log, so paper trades are commission-only by default.
ASSUMED_ENTRY_SLIPPAGE_PER_SHARE = 0.02 # USD/share (modeled estimate only)
ASSUMED_EXIT_SLIPPAGE_PER_SHARE = 0.02  # USD/share (modeled estimate only)
# Legs per vertical spread (bull put = short leg + long leg).
LEGS_PER_SPREAD = 2

# ─────────────────────────────────────────────
# PAPER / CREDIT-FREE MODE
# ─────────────────────────────────────────────
# DISABLE_AI hard-stops every paid LLM call (news GPT sentiment + tipsheet synthesis) so paper
# validation never burns Anthropic/OpenAI credits. The system falls back to rule-based/keyword
# logic, which is fully sufficient for screening and paper tracking. Flip to False only when you
# deliberately want AI narrative and have credits to spend.
DISABLE_AI = True

# ─────────────────────────────────────────────
# RISK TIERS — account-size-agnostic position sizing
# Each qualified trade is presented with contracts-per-tier so the output
# serves accounts of any size. The scanner no longer gates on ACCOUNT_BALANCE.
# ─────────────────────────────────────────────
RISK_TIERS = [
    {"label": "< $100",   "max_risk": 100},
    {"label": "< $500",   "max_risk": 500},
    {"label": "< $1,000", "max_risk": 1000},
]

# ─────────────────────────────────────────────
# WATCHLIST — 50 tickers across all major sectors for maximum coverage
# ─────────────────────────────────────────────
WATCHLIST = [
    # ── Broad Market Indices (3) ──
    {"ticker": "SPY",  "type": "ETF",   "note": "S&P 500"},
    {"ticker": "QQQ",  "type": "ETF",   "note": "Nasdaq 100"},
    {"ticker": "IWM",  "type": "ETF",   "note": "Russell 2000"},
    
    # ── Mega-Cap Technology (6) ──
    {"ticker": "NVDA", "type": "Stock", "note": "NVIDIA — chip design, extreme IV"},
    {"ticker": "AAPL", "type": "Stock", "note": "Apple — tech leader"},
    {"ticker": "MSFT", "type": "Stock", "note": "Microsoft — cloud/AI"},
    {"ticker": "GOOG", "type": "Stock", "note": "Alphabet — search/cloud"},
    {"ticker": "META", "type": "Stock", "note": "Meta — social/AI, high IV"},
    {"ticker": "AMD",  "type": "Stock", "note": "AMD — semiconductor competitor"},
    
    # ── Mid-Cap Technology (2) ──
    {"ticker": "PLTR", "type": "Stock", "note": "Palantir — data/analytics, high IV"},
    {"ticker": "MU",   "type": "Stock", "note": "Micron — memory chips"},
    
    # ── Semiconductors (1) ──
    {"ticker": "QCOM", "type": "Stock", "note": "Qualcomm — mobile/wireless"},
    
    # ── Software & Services (2) ──
    {"ticker": "CRM",  "type": "Stock", "note": "Salesforce — enterprise CRM"},
    {"ticker": "ADBE", "type": "Stock", "note": "Adobe — creative software"},
    
    # ── Communications (1) ──
    {"ticker": "NFLX", "type": "Stock", "note": "Netflix — streaming, growth"},
    
    # ── Healthcare (5) ──
    {"ticker": "JNJ",  "type": "Stock", "note": "Johnson & Johnson — diversified health"},
    {"ticker": "PFE",  "type": "Stock", "note": "Pfizer — pharma giant"},
    {"ticker": "UNH",  "type": "Stock", "note": "UnitedHealth — insurance/healthcare"},
    {"ticker": "ABBV", "type": "Stock", "note": "AbbVie — biopharm"},
    {"ticker": "AMGN", "type": "Stock", "note": "Amgen — biotech"},
    
    # ── Financials (5) ──
    {"ticker": "JPM",  "type": "Stock", "note": "JPMorgan — banking giant"},
    {"ticker": "BAC",  "type": "Stock", "note": "Bank of America"},
    {"ticker": "GS",   "type": "Stock", "note": "Goldman Sachs — investment banking"},
    {"ticker": "BLK",  "type": "Stock", "note": "BlackRock — asset management"},
    {"ticker": "USB",  "type": "Stock", "note": "U.S. Bancorp — regional bank"},
    
    # ── Consumer Discretionary (6) ──
    {"ticker": "TSLA", "type": "Stock", "note": "Tesla — EVs, extreme IV"},
    {"ticker": "AMZN", "type": "Stock", "note": "Amazon — e-commerce/cloud"},
    {"ticker": "RCL",  "type": "Stock", "note": "Royal Caribbean — cruise/cyclical"},
    {"ticker": "MAR",  "type": "Stock", "note": "Marriott — hospitality"},
    {"ticker": "NKE",  "type": "Stock", "note": "Nike — apparel/athletic"},
    {"ticker": "WMT",  "type": "Stock", "note": "Walmart — retail leader"},
    
    # ── Consumer Staples (2) ──
    {"ticker": "KO",   "type": "Stock", "note": "Coca-Cola — beverages"},
    {"ticker": "PEP",  "type": "Stock", "note": "PepsiCo — food/beverage"},
    
    # ── Energy (4) ──
    {"ticker": "XOM",  "type": "Stock", "note": "ExxonMobil — integrated energy"},
    {"ticker": "CVX",  "type": "Stock", "note": "Chevron — oil/gas major"},
    {"ticker": "COP",  "type": "Stock", "note": "ConocoPhillips — exploration"},
    {"ticker": "PSX",  "type": "Stock", "note": "Phillips 66 — refining"},
    
    # ── Materials & Metals (3) ──
    {"ticker": "FCX",  "type": "Stock", "note": "Freeport-McMoRan — copper/gold"},
    {"ticker": "CLF",  "type": "Stock", "note": "Cleveland-Cliffs — steel"},
    {"ticker": "SCCO", "type": "Stock", "note": "Southern Copper — mining"},
    
    # ── Industrials (3) ──
    {"ticker": "BA",   "type": "Stock", "note": "Boeing — aerospace/defense"},
    {"ticker": "GE",   "type": "Stock", "note": "General Electric — diversified"},
    {"ticker": "LMT",  "type": "Stock", "note": "Lockheed Martin — defense"},
    
    # ── Utilities (1) ──
    {"ticker": "NEE",  "type": "Stock", "note": "NextEra Energy — utilities/renewable"},
    
    # ── Real Estate & REITs (2) ──
    {"ticker": "PLD",  "type": "Stock", "note": "Prologis — industrial REIT"},
    {"ticker": "AMT",  "type": "Stock", "note": "American Tower — tower REIT"},
    
    # ── Sector & Commodity ETFs (4) ──
    {"ticker": "XLE",  "type": "ETF",   "note": "Energy Sector ETF"},
    {"ticker": "GDX",  "type": "ETF",   "note": "Gold Miners ETF — strong VRP"},
    {"ticker": "XLV",  "type": "ETF",   "note": "Healthcare Sector ETF"},
    {"ticker": "ARKK", "type": "ETF",   "note": "ARK Innovation — disruptive tech, high IV"},

    # ── Crypto (2) ──
    # IBIT/COIN trade on crypto sentiment, not equity macro — zero equity correlation,
    # structurally elevated IV, and a new VRP dimension the rest of the book doesn't touch.
    # Sector cap keeps combined crypto exposure to 2 positions max.
    {"ticker": "IBIT", "type": "ETF",   "note": "iShares Bitcoin ETF — crypto exposure, high IV, zero equity correlation"},
    {"ticker": "COIN", "type": "Stock", "note": "Coinbase — crypto proxy, very high IV, liquid options"},

    # ── Semiconductor ETF (1) ──
    # SMH gives broad-sector semi coverage with better VRP stability than individual names.
    # Complements the existing NVDA/AMD/QCOM/MU individual positions without duplicating them.
    {"ticker": "SMH",  "type": "ETF",   "note": "VanEck Semiconductor ETF — sector VRP, complements individual semi names"},

    # ── Biotech ETF (1) ──
    # XBI has among the highest structural VRP of any ETF. The existing healthcare names
    # (JNJ, PFE, UNH, ABBV, AMGN) are pharma/insurance — XBI fills the biotech gap.
    {"ticker": "XBI",  "type": "ETF",   "note": "SPDR S&P Biotech ETF — chronically high VRP, best premium-selling ETF in healthcare space"},

    # ── Fixed Income / Macro (1) ──
    # TLT is the only non-equity, non-crypto name on the watchlist — complete diversification.
    # Rates vol has been structurally elevated since 2022. Already in SPY_BUFFER_TICKERS.
    {"ticker": "TLT",  "type": "ETF",   "note": "iShares 20+ Year Treasury — rates vol, macro diversifier, zero equity correlation"},

    # ── Cybersecurity (1) ──
    # CrowdStrike is the sector leader with consistently high IV and liquid options.
    # Fills the cybersecurity gap — a major growth sector absent from the current watchlist.
    {"ticker": "CRWD", "type": "Stock", "note": "CrowdStrike — cybersecurity leader, high IV, growing sector"},
]

# ─────────────────────────────────────────────
# MARKET REGIME GATES
# ─────────────────────────────────────────────
# VIX gates enforce that we only sell premium when VRP edge is real.
# Below MIN: premium is cheap — IV Rank gate will naturally block most trades,
#   but we also inject a regime note so the output explains the silence.
# Above MAX: gamma risk dominates; spreads breach rapidly even at 0.20 delta.
VIX_MIN_FOR_EDGE = 16            # Below this: premium too cheap, inject LOW_VOL regime warning
VIX_MAX_FOR_TRADES = 30          # Above this: inject HIGH_VOL aggressive size-down warning
VIX_ELEVATED_THRESHOLD = 25      # Above this: inject standard size-down caution

# ─────────────────────────────────────────────
# VRP CALCULATION WINDOW
# ─────────────────────────────────────────────
# HV lookback should match expected DTE so VRP is relevant to the holding period.
# Default matches PREFERRED_DTE_TARGET = 35.
VRP_HV_WINDOW = 35               # HV lookback days — set equal to PREFERRED_DTE_TARGET

# ── VRP measured against FORECAST realised vol, not trailing ──────────────────
# The trade is paid against the vol of the NEXT VRP_HV_WINDOW days; the trailing window
# measures the LAST ones. Over 35,774 observations the trailing figure overstated future vol
# by 10.4pp on names whose vol had just expanded and understated it by 5.5pp on names that had
# gone quiet — so the engine refused rich premium after a shock and sold into a lull right
# before it ended. Held-out MAE improves 13.07 -> 12.29 and the state biases collapse
# (-7.25 -> +0.65 compressing, +13.11 -> +5.43 expanding). See analysis/vol_forecast.py.
#
# Set False to restore the trailing behaviour exactly; `vrp_trailing_pp` is emitted either way
# so the two can be compared on any row.
# Declared rather than defaulted, because outcome_logger stamps it onto every trade at open
# and a phantom getattr default would write an unverifiable claim into the ledger. True since
# the credit floor and the ranking score moved to the natural basis (sell the bid, buy the
# ask) — the basis the desk actually fills at.
# ═══════════════════════════════════════════════════════════════════════════
# COHORT CONTRACT — FROZEN 2026-08-14.  Read this before changing anything below it.
# ═══════════════════════════════════════════════════════════════════════════
# outcome_logger.cohort() keys a trade as `fill_model | gate_basis | close_logic`, and a
# calibration number is only meaningful within one key. The ledger already proves what happens
# when that is ignored: pooled it reports a -56.8pp miss, split it reports -5.4pp on the cohort
# that filled where it was priced.
#
# We are now accumulating the FIRST cohort that can validate anything: natural|natural|<close>.
# Every setting in this block defines which trades are eligible or how they are managed, so
# changing ANY of them mid-run splits the cohort and restarts the count. That includes the
# close logic — a roll rule or a 21-DTE exit added at trade 10 splits it exactly as surely as
# moving a gate would, because close_logic is part of the key.
#
# WHAT THIS COHORT IS VALIDATING — stated so it cannot be quietly redefined:
#     "Sell a defined-risk credit spread on the natural basis, hold to stop or expiry."
# NOT rolls. NOT 21-DTE management. Those are deliberately deferred to a v2 cohort with its
# own key, because the open question is whether SELECTION has edge, and 45 of the last 65
# exits went through the stop — adding a second unvalidated mechanism now makes that question
# unanswerable rather than answering it.
#
# Target: 30 closed trades under one key. Deferred work that does NOT touch this block
# (scoring refinements, display, calibration analysis) may proceed in parallel.
COHORT_FROZEN_AT = "2026-08-14"
COHORT_STRATEGY_LABEL = "sell_natural_hold_to_stop_or_expiry"
COHORT_TARGET_CLOSED_TRADES = 30

# ─── ENTRY DIVERSIFICATION (added 2026-08-20; part of this contract, see below) ──────────────
# The cohort's premise is 30 INDEPENDENT observations. The ledger says it was not getting them:
# 65 of 79 paper entries (82%) were opened in the same MINUTE as at least one other, in batches
# of up to 5, and 10 of the 11 currently-open positions share a single expiration (2026-09-18).
# Four spreads opened from one board snapshot are four readings of one market moment, not four
# samples — they share the volatility regime, the day's news, the same scan's data quality and,
# increasingly, the same settlement date. Correlated observations inflate apparent sample size
# without adding information, so a 30-trade count assembled that way validates less than it
# claims to.
#
# These caps are FORWARD-LOOKING ONLY. They never close, re-open or re-classify an existing
# position, and they do not redefine what counts as a valid observation — that would reopen the
# contract's own definition of what is being measured, which is a larger change than the problem
# needs. They gate NEW opens and nothing else.
#
# Yes, these are entry gates, and yes, changing them restarts the 30-trade count. That is why
# they land NOW: the count is 1, which is the cheapest moment in this cohort's life to pay it.
# Deferring until entries resume pays the identical cost at a strictly higher count.
#
# The tension, stated rather than hidden: nothing has opened since 2026-08-10, so tightening
# throughput while the pipe is already clogged could look perverse. It is not — the drought is
# operational (see VEGA_Session_Log_2026-08-20.md §4a) and will be fixed separately. But if
# unblocking entry flow shows these numbers are too tight, they are three integers in one place,
# deliberately.
MAX_NEW_OPENS_PER_RUN = 2        # was an undocumented VEGA_MAX_NEW_PER_RUN default of 5
MAX_NEW_OPENS_PER_DAY = 3        # the batch that matters most: one day is ~one regime
MAX_OPEN_PER_EXPIRATION = 4      # with 15 open max, forces at least four settlement dates

# The date those three caps took effect. outcome_logger.cohort() reads it so a trade opened
# under the caps cannot be pooled with one opened before them.
#
# This block says plainly that "changing them restarts the 30-trade count", but nothing in the
# code enforced that: cohort() keyed on fill_model|gate_basis|close_logic, none of which move
# when an ENTRY rule changes. The four closed trades in natural|natural|ravens_v1 were all
# opened in the same minute on 2026-08-10 — precisely the clustering the caps now forbid — so
# the first trade opened under the new rules would have joined them under an identical key and
# the count would have read 5 of 30 as though nothing had changed. Entry rules select the
# population; a change to them starts a new one, and the key has to say so.
ENTRY_RULES_EPOCH = "2026-08-20"
ENTRY_RULES_EPOCH_LABELS = ("pre_caps", "caps_v1")

# Negative pop_gap = VEGA's own model rates the trade WORSE than the market prices it. Eleven
# gates and none of them tested this; observed live on IBIT at -12.6pp while passing 11/11.
# Decision 2026-08-14: HARD block for the auto-trader, ADVISORY for the manual desk. The robot
# must not open a trade its own model rates negatively; the operator may, with the warning in
# front of them. Frozen for the cohort.
POP_GAP_GATE_AUTO_TRADER = True
POP_GAP_MIN = 0.0

USE_NATURAL_CREDIT = True

VRP_USE_FORECAST = True
VOL_REVERSION_PHI = 0.55         # fitted on a 60% train split, held out. 1.0 = no reversion.
VOL_SECTOR_WEIGHT = 0.30         # how far a cooling/heating SECTOR nudges a name's forecast

# Sector RELATIVE STRENGTH is deliberately not used anywhere. Tested over 8 years on the 11
# SPDR sectors: rank correlation between RS today and forward returns is +0.01 to -0.04 at
# every horizon from 21 to 252 days, none significant (p > 0.18), and the top3-minus-bottom3
# forward spread is ~0. Sector VOLATILITY persists strongly (+0.62 at 1m, +0.78 at 3m,
# p ~ 1e-207); sector DIRECTION does not. Only the former is wired in.

# ─────────────────────────────────────────────
# IV HISTORY TRACKING — proper IV Rank calculation
# ─────────────────────────────────────────────
# Per-ticker IV samples stored in IV_HISTORY_DIR/{ticker}.json.
# System self-bootstraps: starts with HV-based approximation (labeled APPROX),
# transitions to real IV percentile once IV_HISTORY_MIN_SAMPLES are collected.
IV_HISTORY_DIR = "data/iv_history"   # Relative to options_intelligence root
IV_HISTORY_MIN_SAMPLES = 30          # Minimum IV samples for reliable percentile
IV_HISTORY_MAX_SAMPLES = 504         # ~2 years of daily samples (rolling window cap)
# M1 fix: while bootstrapping (< MIN_SAMPLES real IV points) the fallback ranks current IV against
# the realized-HV distribution. Because IV structurally sits ABOVE realized vol (that IS the VRP),
# the raw comparison returned ~100 almost every time. We inflate the HV distribution by this factor
# (typical IV/HV ratio ≈ 1.2) so a normal IV lands near the middle of the distribution, not the top.
IV_HV_INFLATOR = 1.2

# ─────────────────────────────────────────────
# TRUE-POP DRIFT HANDLING (C1 fix)
# ─────────────────────────────────────────────
# The historical probability-of-profit backtest must NOT inherit the sample period's directional
# drift, or every trade looks like edge in a bull market and none in a flat/down market. We remove
# the realized mean drift and replace it with a small risk-free drift so the statistic reflects the
# stock's VOLATILITY structure under a near-risk-neutral assumption — directly comparable to the
# option's implied probability (1 − |delta|). Modes: "risk_free" (default), "zero", "raw" (legacy).
TRUE_POP_DRIFT_MODE = "risk_free"

# ─────────────────────────────────────────────
# SECTOR CORRELATION LIMITS
# ─────────────────────────────────────────────
# Prevents over-concentration when multiple tickers share macro factor exposure.
# When more than MAX_TRADES_PER_SECTOR qualify from the same sector, only the
# highest-edge-scoring ones are kept.
MAX_TRADES_PER_SECTOR = 2        # Max qualified trades surfaced per sector group
SECTOR_LIMIT_EXEMPT = {"broad_market"}  # These sector keys are never capped

# Additional macro correlation guard: cap simultaneous broad-market exposures
# across highly correlated index ETFs.
MAX_CORRELATED_BROAD_MARKET_TRADES = 1
CORRELATED_BROAD_MARKET_TICKERS = {"SPY", "QQQ", "IWM"}

TICKER_SECTORS: dict = {
    # ── Broad Market ──
    "SPY":  "broad_market",
    "QQQ":  "broad_market",
    "IWM":  "broad_market",
    
    # ── Mega-Cap Technology ──
    "NVDA": "technology",
    "AAPL": "technology",
    "MSFT": "technology",
    "GOOG": "technology",
    "GOOGL": "technology",
    "META": "technology",
    "AMD":  "technology",
    
    # ── Mid-Cap Technology ──
    "PLTR": "technology",
    "MU":   "technology",
    
    # ── Semiconductors ──
    "QCOM": "technology",
    
    # ── Software ──
    "CRM":  "technology",
    "ADBE": "technology",
    
    # ── Communications ──
    "NFLX": "communications",
    "DIS":  "communications",
    
    # ── Healthcare ──
    "JNJ":  "healthcare",
    "PFE":  "healthcare",
    "UNH":  "healthcare",
    "ABBV": "healthcare",
    "AMGN": "healthcare",
    "VRTX": "healthcare",
    
    # ── Financials ──
    "JPM":  "financials",
    "BAC":  "financials",
    "GS":   "financials",
    "BLK":  "financials",
    "AIG":  "financials",
    "USB":  "financials",
    "KRE":  "financials",
    
    # ── Consumer Discretionary ──
    "TSLA": "consumer_cyclical",
    "AMZN": "consumer_cyclical",
    "RCL":  "consumer_cyclical",
    "MAR":  "consumer_cyclical",
    "NKE":  "consumer_cyclical",
    "WMT":  "consumer_cyclical",
    
    # ── Consumer Staples ──
    "KO":   "consumer_staples",
    "PEP":  "consumer_staples",
    "PG":   "consumer_staples",
    
    # ── Energy ──
    "XLE":  "energy",
    "OXY":  "energy",
    "XOM":  "energy",
    "CVX":  "energy",
    "COP":  "energy",
    "PSX":  "energy",
    "MPC":  "energy",
    
    # ── Materials / Mining ──
    "GDX":  "materials",
    "FCX":  "materials",
    "CLF":  "materials",
    "SCCO": "materials",
    
    # ── Industrials ──
    "BA":   "industrials",
    "GE":   "industrials",
    "LMT":  "industrials",
    "DAL":  "industrials",
    
    # ── Utilities ──
    "NEE":  "utilities",
    "DUK":  "utilities",
    
    # ── REITs ──
    "PLD":  "reits",
    "AMT":  "reits",
    "O":    "reits",
    
    # ── Commodities ──
    "GLD":  "commodities",
    "SLV":  "commodities",
    
    # ── Sector/Broad ETFs ──
    "XLV":  "healthcare_etf",
    "ARKK": "tech_etf",
    "SMH":  "tech_etf",

    # ── Biotech ──
    "XBI":  "biotech",

    # ── Fixed Income / Macro ──
    "TLT":  "fixed_income",

    # ── Crypto ──
    # Separate sector so crypto correlation is capped independently of equities.
    "IBIT": "crypto",
    "COIN": "crypto",

    # ── Cybersecurity ──
    "CRWD": "cybersecurity",
}

# ─────────────────────────────────────────────
# EDGE FILTER THRESHOLDS
# ─────────────────────────────────────────────
MIN_EDGE_SCORE = 60               # 0-100 composite score required to appear on tip sheet
# H1 fix: was 0.15 (15 vol points) — ~3.5x the historical average VRP, so it essentially never
# triggered. Real S&P VRP averages ~4.2pp (1990–2018) and ~6.5pp since 2020 (Cboe/CAIA). 0.02 =
# require IV to exceed RV by at least 2 vol points, a realistic minimum edge.
VRP_MIN_THRESHOLD = 0.02          # Implied vol must exceed realized vol by at least 2 vol points
NEWS_SENTIMENT_BLOCK = True       # Block trades on tickers with strong negative news
EARNINGS_BLACKOUT_DAYS = 7        # Never sell premium within 7 days of earnings
# This knob is SCORING/ADVISORY only. Two places read it:
#   analysis/edge_calculator.py  — zeroes the earnings_safety component inside the window
#   analysis/strike_validator.py — refuses a strike inside the window
# It does NOT control the hard gate. assessment._earnings_clear() enforces a strictly stronger
# rule: any earnings on or before EXPIRY blocks the candidate, however many days away it is,
# because a spread held through a print is an event bet no matter how it looked at entry.
# So this value can only ever tighten the soft paths — never loosen the contract. Do not delete
# it as "unused"; zeroing it silently disables the earnings_safety score component.

# ─────────────────────────────────────────────
# SCORE DISPLAY & ROC QUALITY CONTROLS
# ─────────────────────────────────────────────
# The base scoring components sum to 100 max; the additive bonuses (skew +15, post-earnings +5)
# can push a trade above 100 before the cap clips it. SCORE_DISPLAY_UNCAPPED lets the true raw
# score run up to 120 so you can see which trades are genuinely elite vs. which were lifted by
# timing / skew bonuses. The capped score (0–100) is still used for MIN_EDGE_SCORE qualification
# — only the display value changes.
SCORE_DISPLAY_UNCAPPED = True     # If True, expose raw_score (0–120) alongside total_score (0–100)

# ROC sanity flag — gross ROC on very narrow spreads can exceed 100%.  A $1-wide spread that
# collects $0.60 shows 150% ROC but only pays $60/contract. Flag any trade where gross ROC
# exceeds this threshold for manual review; the system does NOT block on it.
ROC_SANITY_FLAG_THRESHOLD = 0.50  # 50% gross ROC — flag but do not block

# Net & EV-adjusted ROC fields added to spread metrics output.
# net_realized_roc = (credit × TARGET_PROFIT_PCT − round_trip_commission) / max_loss
#   This is the ROC you'll actually book when managing at 50% and accounting for commissions.
# ev_roc = (net_realized_roc × p_profit) − (1.0 × (1 − p_profit))
#   Expected-value ROC — negative means the trade destroys capital in expectation even when
#   POP is above the minimum gate. Requires p_profit to be passed in.
EV_ROC_ENABLED = True             # Compute and surface net_realized_roc and ev_roc in spread metrics

# Environment gauge gate — wire environment.py heat_assessment() output into the scan output.
# When ENVIRONMENT_GATE_ENABLED is True and a trade's heat band is "hot", the trade is flagged
# with a WARNING tag in the tip sheet and its recommendation is surfaced. It does NOT hard-block
# (that integration lives in main.py — see VS Code handoff doc). Set to False to keep advisory-only.
ENVIRONMENT_GATE_ENABLED = True   # Surface heat band + recommendation on every qualified trade

# ─────────────────────────────────────────────
# ENTRY TIMING (pattern phase) — analysis/entry_timing.py
# ─────────────────────────────────────────────
# For a delta-targeted premium-selling structure, the credit collected varies with WHERE in
# the short-term move you sell. A bull put pays most late in a pullback (put skew steepened);
# a bear call pays most late in a bounce. This surfaces that as a cockpit chip + warning.
#
# ADVISORY ONLY — the timing criterion is appended with advisory=True in strategies.evaluate()
# and is excluded from the `qualified` computation. It must stay that way: multi_strategy.py
# and lottery_scanner.py return None on `not ev["qualified"]`, so a non-advisory timing row
# would silently delete every bear call / condor / lottery candidate with mid-range RSI.
ENTRY_TIMING_ENABLED = True

# Bull put — pullback maturity. Lower RSI = later in the pullback = richer put premium.
BULL_PUT_OVERSOLD_RSI     = 38    # RSI at or below this = deep oversold, OPTIMAL
BULL_PUT_OPTIMAL_RSI_MAX  = 52    # below this (with SMA20/support context) = OPTIMAL
BULL_PUT_EARLY_RSI_MIN    = 58    # at/above this = EARLY_PULLBACK warning
BULL_PUT_EXTENDED_RSI_MIN = 68    # at/above this = EXTENDED, thinnest premium

# Bear call — bounce maturity. Higher RSI = later in the bounce = richer call premium.
BEAR_CALL_OPTIMAL_RSI_MIN  = 58   # above this = OPTIMAL
BEAR_CALL_EXTENDED_RSI_MIN = 65   # above this = extended bounce, peak call premium
BEAR_CALL_EARLY_RSI_MAX    = 45   # at/below this = EARLY warning

# Iron condor — wants a range-bound tape, no directional extreme.
CONDOR_RANGE_RSI_MIN = 44
CONDOR_RANGE_RSI_MAX = 57

# How close price must sit to nearest_support to count as "at support" (fraction of price).
ENTRY_TIMING_SUPPORT_PROXIMITY_PCT = 0.03

# ── Chart structure reader (analysis/structure.py) ──
# RSI says how stretched momentum is; it cannot tell a shallow pause inside an advance from
# the second peak of a double top. This reads the SHAPE — "early in a bull flag", "late —
# second peak of a double top", "at support, third touch" — and how far through it we are.
# Advisory like everything else in the timing stack; heuristics misread charts, so each read
# carries a confidence and UNREADABLE is a legitimate answer.
STRUCTURE_ENABLED = True
STRUCTURE_LOOKBACK_DAYS = 180     # price history requested for the structure read
STRUCTURE_MIN_BARS = 40           # below this, return UNREADABLE rather than guess
STRUCTURE_PIVOT_K = 2             # fractal width, used only for support/resistance touch counts

# Swing detection is a volatility-scaled zigzag, NOT k-bar fractals. A fractal marks every
# two-day wiggle as a pivot, which made "the last two swing highs" meaningless — QQQ at
# all-time highs classified as a DOWNTREND, and every flag reported the same 2-bar age
# because a k-bar fractal cannot confirm a pivot closer than k bars to the last candle.
# A pivot here needs a countermove of max(MIN_PCT, ATR_MULT x ATR%) to confirm.
STRUCTURE_ZIGZAG_ATR_MULT = 2.5   # swing size in ATRs — adapts to each instrument
STRUCTURE_ZIGZAG_MIN_PCT = 0.03   # floor, so placid ETFs don't produce noise swings
STRUCTURE_ZIGZAG_MAX_PCT = 0.10   # ceiling, so violent names still produce some swings

# Flag detection — an impulse leg followed by a shallow, orderly counter-drift.
STRUCTURE_FLAG_MIN_IMPULSE_PCT = 6.0    # a move smaller than this is noise, not an impulse
STRUCTURE_FLAG_MAX_BARS = 30            # older than this and it is no longer "the" flag
STRUCTURE_FLAG_MAX_RETRACE_PCT = 70.0   # beyond this the flag has failed, not matured
STRUCTURE_FLAG_DEEP_RETRACE_PCT = 62.0  # beyond this, report the flag but don't trade off it
# Stage thresholds are retracement-based: <25% EARLY, 25-50% MID, >=50% LATE.

# Double top / bottom — two comparable extremes with a real trough between them.
STRUCTURE_DOUBLE_TOLERANCE_PCT = 0.025  # how close the two peaks must be to count as a pair
STRUCTURE_DOUBLE_MIN_BARS = 8           # minimum separation, else it is one peak with noise
STRUCTURE_DOUBLE_MIN_MIDDLE_PCT = 0.03  # the trough between must be this deep

# A one-way move never produces a countermove big enough to mark a swing, so the swing test
# reads FLAT. Net displacement over the window is the fallback: this is the "extended, no
# pullback yet" state, which is the thinnest-premium moment there is for a put seller.
STRUCTURE_TREND_MIN_NET_PCT = 8.0       # net move over the window to call it a trend

STRUCTURE_RANGE_WINDOW = 30             # bars examined for a sideways band
STRUCTURE_RANGE_MAX_BAND_PCT = 8.0      # band narrower than this reads as range-bound
STRUCTURE_CONTRACTION_WINDOW = 10       # bars per half when comparing range/volume contraction
STRUCTURE_LEVEL_TOLERANCE_PCT = 0.02    # "at" a level means within this fraction of it

# ── Support / resistance detection (analysis/levels.py) ──
# The old inline 2-bar-fractal scan in data/technicals.py marked every two-day wiggle as a
# level, so nearest_support was systematically a micro-low under spot (measured 2026-08-05:
# QQQ 0.0% away, XLE 0.3%, WMT 0.6%) and the three "levels" it kept were usually one price
# area. Levels are now zigzag pivots CLUSTERED by price and ranked by strength, so a level is
# something the market tested more than once.
LEVELS_LOOKBACK_BARS = 180        # history examined for levels
LEVELS_MIN_BARS = 40              # below this, return no levels rather than guess
LEVELS_KEEP_PER_SIDE = 3          # levels retained above and below spot
LEVELS_CLUSTER_ATR_MULT = 0.8     # cluster width in ATRs — one level on QQQ is three on AMD
LEVELS_CLUSTER_MIN_PCT = 0.010    # floor on cluster width
LEVELS_TOUCH_WEIGHT = 22.0        # strength per touch (capped at 70 before bonuses)
LEVELS_FLIP_BONUS = 12.0          # old ceiling now acting as floor (or vice versa)
LEVELS_RECENCY_HALFLIFE_BARS = 60 # strength halves every N bars since the last touch
# nearest_support/nearest_resistance skip anything weaker than this, so a one-touch accident
# cannot become the invalidation price printed on the order ticket.
LEVELS_MIN_STRENGTH = 12.0

# ── Level-aware strike selection ──
# Selling beneath a level the market has defended more than once is the core structural edge
# available to a premium seller, and until 2026-08-05 strike selection ignored levels
# entirely — it was delta, DTE and credit only, on every strategy.
#
# Deliberately a PREFERENCE, not a filter. Bull put: among pairs whose ROC is within
# LEVEL_STRIKE_ROC_TOLERANCE of the best, take the most sheltered. Call side: among strikes
# within LEVEL_STRIKE_DELTA_TOLERANCE of the delta target, same idea. Structure must never
# cost meaningful ROC, never override delta as the risk control, and never empty the board on
# names where no level happens to sit in the right place.
LEVEL_AWARE_STRIKES = True
LEVEL_STRIKE_ROC_TOLERANCE = 0.10    # bull put: ROC within 10% of best stays a contender
LEVEL_STRIKE_DELTA_TOLERANCE = 0.04  # call side: delta within 0.04 of target stays a contender
LEVEL_TARGET_BUFFER_PCT = 0.02       # a shielding level this far from the strike earns full credit
# A level closer than this to the strike shields nothing — breaking it lands on the strike.
# Live SPY 2026-08-05 offered a 3-touch support 0.16% above a candidate strike; without this
# floor, selection paid 1.4 points of ROC for a shelter that was not there. Applied only in
# selection (where ROC is spent), not in scoring (where partial credit is still informative).
LEVEL_MIN_BUFFER_PCT = 0.005

# Every order ticket says "exit if it breaks support on volume", but trade management was
# purely price/DTE based, so nothing ever watched for it. The auto paper cycle now logs a
# LEVEL-ALERT when an open position loses a level that stood above its short strike.
# ADVISORY — it never closes a position. Auto-closing on a support break would cut winners
# that dip and recover, which is a strategy decision rather than a gap fix.
LEVEL_MANAGEMENT_ALERTS = True

# ─────────────────────────────────────────────
# VOLATILITY SURFACE (analysis/vol_surface.py)
# ─────────────────────────────────────────────
# VEGA read IV as one scalar per trade. The surface has two axes that both change what a
# premium seller should do: term structure across expirations, and skew depth across strikes.
# Independent of SKEW_SCORING_ENABLED so either can be switched off alone.
TERM_STRUCTURE_ENABLED = True
TERM_STRUCTURE_MIN_DTE = 5        # ignore weeklies — not the curve a 25-45 DTE seller trades
TERM_STRUCTURE_MAX_DTE = 120      # ignore LEAPS
TERM_STRUCTURE_FLAT_BAND_PTS = 2.0   # |back - front| inside this reads as flat
# A dated catalyst is an expiration priced this many vol points above the front-to-back line
# at its own DTE. Measured against the interpolated line, NOT a standard deviation: the spike
# is part of the sample, so it inflates the mean and sigma it would have to clear — a
# 0.24/0.52/0.28 curve is an obvious catalyst and still sits at only 1.42 sigma.
TERM_STRUCTURE_EVENT_EXCESS_PTS = 5.0
# Term-structure points added to / removed from the edge score. Advisory during calibration —
# event_spike is a heavy penalty but must never hard-block.
TERM_STRUCTURE_SCORE_ADJ = {
    "upward": 5,        # back months richer — general nervousness, good to sell into
    "flat": 2,
    "downward": -5,     # front richest — imminent event risk, weakest entry
    "event_spike": -8,  # dated catalyst inside the window — selling a binary, not variance
    "unknown": 0,
}

# Skew depth — a curve at 20/30/40 delta rather than the single 30-delta point.
SKEW_DELTA_TOLERANCE = 0.08       # how far from the target delta a contract may sit
SKEW_STEEP_PTS = 4.0              # put-minus-call vol points that count as steep
SKEW_FLAT_PTS = 1.0

# ─────────────────────────────────────────────
# CALIBRATION ENGINE (analysis/calibration_engine.py)
# ─────────────────────────────────────────────
# Every scoring weight in this file was hand-set from reasoning about how premium selling
# should work, and none has ever been tested against VEGA's own closed trades. This watches
# outcomes against the components that predicted them and PROPOSES adjustments.
#
# It never edits config.py. Proposals are surfaced for human approval and applied by hand —
# scoring weights are the engine's beliefs about edge, and a bug in the calibration engine
# must never be able to silently corrupt them.
CALIBRATION_ENGINE_ENABLED = True
CALIBRATION_MIN_TRADES = 30       # below this, report the sample size and propose nothing
CALIBRATION_MIN_COMPONENT_N = 20  # per-component minimum before testing predictiveness
CALIBRATION_FLAT_SPREAD = 0.08    # <8pp win-rate spread across terciles = not discriminating
CALIBRATION_REGIME_VIX = 20.0     # high-vol / low-vol split point
CALIBRATION_REGIME_ALERT_PP = 10.0  # win-rate divergence across regimes worth flagging
# When this share of losses are stop-outs, the calibration gap is measuring the EXIT rule
# rather than the selection logic — modelled POP is a probability of finishing profitable at
# expiration, and a stopped position never got to answer that. Flag it loudly, because the
# naive reading of a big negative gap is "cut the scoring weights", which would be the wrong
# fix entirely.
CALIBRATION_STOP_DOMINANCE = 0.70

# ─────────────────────────────────────────────
# RAVENS FRAMEWORK — thesis-based close logic
# ─────────────────────────────────────────────
# Replaces the credit-multiplier stop as the primary close rule. Huginn (thought) asks
# whether the structure that justified the trade still holds; Muninn (memory) asks how often
# comparable situations recovered; Odin synthesises. The wolves are hard floors that fire
# without either raven.
#
# WHY THIS EXISTS, measured on 2026-08-06 across 45 stop-outs:
#   median hold before the stop fired ....... 0 days (mean 1.3)
#   median DTE remaining when stopped ....... 44 days
#   underlying now back above the strike .... 44 of 45 (98%)
#   realised loss on those ................... $3,897
# The stop was not reading the market. It was reading the bid-ask spread — see
# CLOSE_DECISION_MARK_BASIS below.
RAVENS_FRAMEWORK_ENABLED = True

# ── The wolves ──
# 3.0x, not 1.5x, and a BACKSTOP rather than a trigger. Do not lower this without first
# checking that the decision mark is not double-counting the spread.
WOLF_STOP_MULTIPLIER = 3.0
WOLF_DELTA_THRESHOLD = 0.55      # short delta at which the spread is a directional bet
WOLF_GAP_ATR_MULT = 1.5          # overnight gap through the strike, in ATRs

# ── Huginn ──
HUGINN_SUPPORT_BREACH_CONFIRMS = 2   # closes below support before a breach counts as broken
# Below this DTE the clock dominates the chart: a strike still clear is nearly safe, and one
# breached has little time to recover. Above 3x this, an under-water position still owns most
# of its life and the same chart is less damning.
HUGINN_LATE_DTE = 10

# ── Muninn ──
MUNINN_MIN_COMPARABLE = 5        # below this, report insufficient_data — never invent a rate
MUNINN_MIN_SIMILARITY = 0.45     # similarity floor for a historical trade to count

# ── Odin ──
ODIN_RECOVERY_THRESHOLD = 0.35   # below this recovery rate, a violated thesis closes

# Which price basis decides whether to CLOSE. This is the fix for the finding above.
#
# Entry natural = short_bid - long_ask; exit natural = short_ask - long_bid. Both are the
# worst side of the book, so a position must overcome the full bid-ask spread on both legs
# TWICE before it shows a profit. Measured live on 2026-08-06, GDX 76/75 quoted an entry
# natural of +$0.03 against an exit natural of +$0.49 — a 16.3x apparent loss at t=0, with no
# price movement at all. Against a 1.5x stop the position was dead the moment it opened, and
# 40 of 45 stop-outs fired within 24 hours.
#
# "mid" marks the close DECISION at the midpoint, which is how every broker computes
# unrealised P&L. Realised P&L on an actual close still books the natural price, so the
# record stays honest about slippage — only the trigger stops being fiction.
CLOSE_DECISION_MARK_BASIS = "mid"   # "mid" | "natural"

# ─────────────────────────────────────────────
# HORIZON CALIBRATION (analysis/horizon.py)
# ─────────────────────────────────────────────
# Every structural module analysed a fixed 180 days with fixed indicator periods, and none of
# structure / levels / entry_timing / huginn contained a single reference to DTE. A 25-day
# spread and a 45-day spread received an identical technical read, and a support level six
# months out was weighted the same as one price will touch next week.
#
# The principle is already stated for volatility right above (VRP_HV_WINDOW: "HV lookback
# should match expected DTE so VRP is relevant to the holding period"). This extends it to
# structure: distance is measured in expected moves over the REMAINING life of the trade, and
# a pattern is only actionable if it can resolve before expiry.
HORIZON_CALIBRATION_ENABLED = True
HORIZON_LOOKBACK_DTE_MULT = 4.0   # history examined = DTE x this, clamped below
HORIZON_LOOKBACK_MIN_BARS = 60    # floor — the swing detector needs pivots to find
HORIZON_LOOKBACK_MAX_BARS = 250

# ─────────────────────────────────────────────
# PREDICTION LEDGER (analysis/predictions.py)
# ─────────────────────────────────────────────
# VEGA recorded what HAPPENED and never what it CLAIMED, so nothing it asserts was ever
# marked. "The strike is 0.52 sigma away", "EARLY: premium improves below RSI 50", "event
# spike at Sep 11", "3-touch support holds" — all falsifiable inside a known window, all
# discarded the moment they were printed. That is why the calibration engine could only grade
# modelled POP, the one prediction that happened to be stored.
PREDICTION_LEDGER_ENABLED = True
PREDICTION_MIN_FOR_GRADE = 10       # resolved claims of a type before it is graded at all
PREDICTION_TIMING_HORIZON_DAYS = 14 # how long an EARLY timing claim gets to prove itself

# ─────────────────────────────────────────────
# DIRECTIONAL FORECAST (analysis/direction_forecast.py)
# ─────────────────────────────────────────────
# A MEASUREMENT INSTRUMENT, not a signal. Nothing it produces reaches selection, sizing or
# execution, and price_projection still draws its bands with zero drift — that rejection stands
# (sector relative strength: rank correlation +0.01 to -0.04 at every horizon, none significant).
#
# It exists because every other claim VEGA makes matures at a 30-45 day expiry, so the question
# "do these probabilities mean anything?" takes quarters to answer. One-day claims answer it in
# a fortnight, and a null result is a perfectly good result.
DIRECTION_FORECAST_ENABLED = True
DIRECTION_RECORD_AFTER_HOUR = 14   # local hour; claims anchor near the close, not at the open

# Counterfactual recording — the only measurement of whether the eleven gates earn their place.
# The hourly cycle has always produced the snapshots (vega_candidates.py --no-open exists for
# exactly this) but nothing ever called the recorder, so the ledger's last write was 2026-08-10
# and the cockpit told the operator to run it by hand. Wired into the cycle 2026-08-24.
# DAILY, on the same gate hour as the direction forecast: a counterfactual resolves against
# daily bars, so re-resolving hourly costs ~56 price-history fetches to produce the same answer.
COUNTERFACTUAL_RECORD_ENABLED = True
COUNTERFACTUAL_RECORD_AFTER_HOUR = 14
DIRECTION_MAX_TILT_SIGMAS = 0.25   # hard cap on how far the signal may move the mean
# Record a climatology twin (same band, mean pinned at zero) beside every live claim. Without it
# the signal gets credited with the base rate and there is nothing to charge it against.
DIRECTION_RECORD_BASELINE = True

# ─────────────────────────────────────────────
# INTRADAY REFRESH SCHEDULER (runs inside vega_app.py cockpit)
# ─────────────────────────────────────────────
# The cockpit runs a market-hours-only background scheduler so the board tracks the free
# (≈15-min delayed) Polygon/yfinance data without any Windows scheduled tasks. All cadences
# are minutes; the scheduler only fires while US equity options are open (weekdays 9:30–16:00 ET).
# DISABLED 2026-08-19. Two schedulers were firing the same cycle: this one hourly at :21, and
# the VEGA_AutoPaper_2Weeks task every two hours at :35. A cycle takes 13-16 minutes, so the
# task's fire landed INSIDE the cockpit's run every time and was turned away by the lock —
# 2026-08-18 recorded "Another cycle appears active; skipping this run" at 11:35 and got 13:35
# through only because the 13:21 run had ended five seconds earlier. Three of that day's four
# task fires did no work at all.
#
# The Windows task is the surviving driver rather than this one, despite the note above and in
# auto_paper_cycle's docstring recommending the reverse. That advice assumes the cockpit is
# always up; it is a UI process, so it stops when the window closes or the box reboots, and it
# stops silently. Paper execution and the re-mark loop the cohort depends on must not be
# conditional on a dashboard being open.
#
# Re-enabling this REQUIRES disabling the task, or the collision returns. The flag is read once
# when the scheduler thread starts (vega_app._scheduler_loop), so the cockpit must be restarted
# for a change here to take effect.
INTRADAY_SCHEDULER_ENABLED = False  # Master switch for the in-cockpit market-hours scheduler
BOARD_REFRESH_MIN = 15              # Full local re-scan (main.py, no JARVIS post) cadence
PAPER_CYCLE_MIN = 60               # Auto-open + mark paper positions (auto_paper_cycle.py) cadence
NEWS_CACHE_TTL_MIN = 60            # Sentiment disk-cache lifetime — news re-scrapes ~hourly, and the
                                   # 15-min board scans in between reuse the cache instead of re-scraping.

# Fundamentals controls
FUNDAMENTALS_ENABLED = True       # Fetch and score fundamentals in screening flow
FUNDAMENTALS_SHADOW_MODE = True   # Score/log only; do not hard-block when True
FUNDAMENTALS_STRICT_BLOCK = False # If True (and shadow mode False), block severe deterioration
MIN_FUNDAMENTALS_SCORE = 4        # Minimum score required when strict blocking is enabled
FUNDAMENTALS_WEIGHT = 10          # Component weight in composite score (0-10)

# ─────────────────────────────────────────────
# VOLATILITY CRUSH MODE
# ─────────────────────────────────────────────
# If enabled, system will surface trades with earnings inside the DTE window as volatility crush plays.
# These will be tagged and flagged with special warnings in the output.
ENABLE_VOL_CRUSH_MODE = True

# ─────────────────────────────────────────────
# BETA BUILD — signal-quality components (spec §3.2–§3.5)
# ─────────────────────────────────────────────
# P0 dedup / book awareness
ALLOW_SAME_TICKER = False          # If True, don't flag trades whose underlying is already held

# IV skew scoring (spec §3.3) — additive 0–15 component
# DISABLED until chain data quality is instrumented and gated per scan.
# Skew is measured across the whole put/call surface, so it is the component most exposed to a
# thin chain: the yfinance quality filter routinely discards ~40% of records, and a skew read
# taken over what survives is a read of the survivors, not of the market. Feeding that into
# scored trades poisons the prediction ledger's baseline before it has one.
# RE-ENABLE when data/data_quality_log.json exists and CHAIN_QUALITY_MIN_RATIO gates the scan
# (see fetcher.get_options_chain / data/data_quality_log.py).
SKEW_SCORING_ENABLED = False       # Compute per-ticker put/call skew and add a skew_score
SKEW_SCORE_MAX_POINTS = 15         # Max points the skew component can contribute
SKEW_SCORE_CAP_VOL_PTS = 10.0      # Favorable skew (vol points) that maps to the max score
SKEW_TARGET_DTE = 30               # Expiration (DTE) at which skew is measured

# Post-earnings IV-crush mode (spec §3.5) — additive +5 bonus
POST_EARNINGS_MODE_ENABLED = True  # Flag names that reported 1–3 days ago with IVR still high
POST_EARNINGS_IVR_MIN = 55         # IV Rank must exceed this to qualify as a crush candidate
POST_EARNINGS_DAYS_WINDOW = (1, 3) # Trading days since the earnings report (inclusive)
POST_EARNINGS_BONUS = 5            # Edge-score bonus applied to a qualifying crush candidate

# EOD Mean Reversion thresholds (close session)
EOD_MIN_DROP_PCT = 1.5            # Minimum % down on day to flag for mean reversion
EOD_MAX_DROP_PCT = 4.0            # Maximum % down (beyond this = potential fundamental break)
EOD_MIN_VOLUME_RATIO = 1.5        # Volume must be 1.5x 20-day average

# ─────────────────────────────────────────────
# API KEYS — set via .env file or environment variables
# Never hardcode keys here. See .env.example.
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
NEWS_API_KEY      = os.environ.get("NEWS_API_KEY", "")
POLYGON_API_KEY   = os.environ.get("POLYGON_API_KEY", "")   # Free tier — 15-min delayed options data

# ── Tradier (legacy — inactive; kept for reference) ──────────────────────
# Tradier requires a funded brokerage account for live API access.
# VEGA now uses Polygon.io as primary data source.
TRADIER_API_KEY   = os.environ.get("TRADIER_API_KEY", "")
TRADIER_SANDBOX   = os.environ.get("TRADIER_SANDBOX", "true").lower() == "true"

# AI Model settings
CLAUDE_MODEL = "claude-sonnet-4-6"
# Pinned model string — update this when OpenAI deprecates the model.
# If OPENAI_MODEL is deprecated, GPT calls will raise a model_not_found error.
# The news.py module catches this and falls back to keyword sentiment, logging
# a CRITICAL warning so it is visible in GitHub Actions logs.
OPENAI_MODEL = "gpt-4o"          # Pinned; update on deprecation
OPENAI_MODEL_FALLBACK = "gpt-4o-mini"  # Used if primary model returns model_not_found

# ─────────────────────────────────────────────
# EMAIL DISTRIBUTION — set via .env or GitHub Secrets
# Leave SMTP_HOST empty to disable email (tip sheet still saves to disk).
# ─────────────────────────────────────────────
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
EMAIL_USER      = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECIPIENTS = [
    e.strip()
    for e in os.environ.get("EMAIL_RECIPIENTS", "").split(",")
    if e.strip()
]
EMAIL_ENABLED = bool(
    EMAIL_SMTP_HOST and EMAIL_USER and EMAIL_PASSWORD and EMAIL_RECIPIENTS
)

# ─────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────
OUTPUT_DIR = "output/tipsheets"
LOG_DIR = "logs"
AUTO_OPEN_BROWSER = False         # Consolidated into the VEGA dashboard "Brief" tab — no separate page. Email is off (EMAIL_ENABLED gated on unset SMTP env).

# ─────────────────────────────────────────────
# RISK-FREE RATE (for Black-Scholes)
# ─────────────────────────────────────────────
RISK_FREE_RATE = 0.04             # 4% — update with current 3-month T-bill rate

# ─────────────────────────────────────────────
# LEGAL DISCLAIMER — included in every output
# ─────────────────────────────────────────────
DISCLAIMER = (
    "This tool is for educational and informational purposes only. "
    "Nothing generated by this system constitutes financial advice, investment advice, "
    "or a recommendation to buy or sell any security. All trading decisions are made "
    "solely by the user. Options trading involves significant risk of loss. "
    "Past performance does not guarantee future results."
)
