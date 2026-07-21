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
# POSITION SIZING — auto-scales with balance
# ─────────────────────────────────────────────
# At $500:  only 1-3 wide spreads valid
# At $1000+: 1-5 wide spreads valid
# At $2500+: iron condors on SPY/QQQ viable
# At $5000+: full strategy suite

def get_max_spread_width():
    if ACCOUNT_BALANCE >= 5000:
        return 10
    else:
        return 5  # $5 wide at all levels — oversized warning flags risk at small accounts

MAX_SPREAD_WIDTH = get_max_spread_width()
MIN_CONTRACTS = 1                 # Always show at least 1-contract setup, even if oversized

# ─────────────────────────────────────────────
# STRIKE PLACEMENT — HARD RULES, NON-NEGOTIABLE
# ─────────────────────────────────────────────
SHORT_STRIKE_MIN_OTM_PCT = 0.03   # Short strike must be minimum 3% OTM from current price
SHORT_STRIKE_TARGET_DELTA = 0.20  # Target delta for short leg (range 0.15-0.25)
SHORT_STRIKE_MAX_DELTA = 0.30     # Absolute maximum delta — reject anything above this
MIN_STRIKE_BUFFER_SPY = 10.00     # SPY/QQQ: short strike must be $10+ below current price
MIN_STRIKE_BUFFER_STOCK = 0.05    # Individual stocks: 5% minimum buffer

# Tickers that use the SPY buffer rule
SPY_BUFFER_TICKERS = {"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT"}

# ─────────────────────────────────────────────
# STRATEGY PREFERENCES
# ─────────────────────────────────────────────
# L2 fix: the engine only implements bull_put_spread (main.py hard-forces it, and
# select_best_strategy's result is overridden). The previous 5-item list was aspirational and
# misleading. Keep the roadmap in a comment; enable only what actually runs.
#   Roadmap (NOT yet implemented): bear_call_spread, iron_condor, pmcc, csp
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
MIN_DTE = 25                      # Minimum days to expiration (targets the 25–45 DTE window)
MAX_DTE = 45                      # Maximum days to expiration
PREFERRED_DTE_TARGET = 35         # Prefer contracts near this DTE when multiple are valid
PREFERRED_DTE_TOLERANCE = 7       # Within +/- this range is considered ideal
TARGET_PROFIT_PCT = 0.50          # Close winners at 50% of max profit
STOP_LOSS_MULTIPLIER = 2.0        # Stop if spread reaches 2x credit received
ALLOW_OVERSIZED_TRADES = True     # Account-agnostic output — risk tiers handle sizing
MAX_QUOTE_SPREAD_PCT = 0.35       # Reject option legs with (ask-bid)/mid above this threshold
MIN_SPREAD_WIDTH_SPY_LIKE = 1.0   # Minimum spread width for SPY-like tickers (flat — not account-size dependent)
MIN_SPREAD_WIDTH_OTHER = 1.0      # Allow 1-point width on non-index symbols
ALLOW_NARROW_SPREAD_EXCEPTION = True
NARROW_SPREAD_MIN_CREDIT_TO_WIDTH = 0.20  # H2 fix: was 0.30 — a 0.20Δ spread pays ~13–20% of width
MIN_OPTION_VOLUME = 100
MIN_OPTION_OPEN_INTEREST = 500
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
EARNINGS_BLACKOUT_DAYS = 7        # Never sell premium within 7 days of earnings (unless volatility crush mode is enabled)

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
SKEW_SCORING_ENABLED = True        # Compute per-ticker put/call skew and add a skew_score
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
