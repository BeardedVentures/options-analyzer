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
# EXECUTION COST MODEL (Gate 1 realism)
# ─────────────────────────────────────────────
# Conservative defaults for a 1-contract vertical:
# - Commission assumed per contract per leg (open and close)
# - Slippage assumed per share at entry and exit (mark vs. fill friction)
COMMISSION_PER_CONTRACT_PER_LEG = 0.65  # USD
ASSUMED_ENTRY_SLIPPAGE_PER_SHARE = 0.02 # USD/share
ASSUMED_EXIT_SLIPPAGE_PER_SHARE = 0.02  # USD/share

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
# WATCHLIST
# ─────────────────────────────────────────────
WATCHLIST = [
    # ── Broad Market (existing) ──
    {"ticker": "SPY",  "type": "ETF",   "note": "S&P 500 Core"},
    {"ticker": "QQQ",  "type": "ETF",   "note": "Nasdaq 100"},
    {"ticker": "IWM",  "type": "ETF",   "note": "Russell 2000"},
    # ── Technology (existing + additions) ──
    {"ticker": "NVDA", "type": "Stock", "note": "NVIDIA — post-split, high IV"},
    {"ticker": "AMD",  "type": "Stock", "note": "Semiconductor — consistent high IV"},
    {"ticker": "PLTR", "type": "Stock", "note": "Palantir — highest IV large-cap"},
    {"ticker": "AAPL", "type": "Stock", "note": "Apple"},
    {"ticker": "MSFT", "type": "Stock", "note": "Microsoft"},
    # ── Consumer ──
    {"ticker": "TSLA", "type": "Stock", "note": "Tesla — extreme IV, top premium"},
    # ── Energy ──
    {"ticker": "XLE",  "type": "ETF",   "note": "Energy Sector ETF"},
    {"ticker": "OXY",  "type": "Stock", "note": "Occidental Petroleum — high IV"},
    # ── Financials ──
    {"ticker": "KRE",  "type": "ETF",   "note": "Regional Banks — elevated IV"},
    # ── Materials ──
    {"ticker": "GDX",  "type": "ETF",   "note": "Gold Miners — strong VRP"},
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
    # Broad market — diversified, exempt from cap
    "SPY":  "broad_market",
    "QQQ":  "technology_etf",
    "IWM":  "broad_market",
    "DIA":  "broad_market",
    # Technology
    "NVDA": "technology",
    "AMD":  "technology",
    "PLTR": "technology",
    "AAPL": "technology",
    "MSFT": "technology",
    "GOOG": "technology",
    "GOOGL": "technology",
    "META": "technology",
    "CRM":  "technology",
    # Consumer cyclical
    "TSLA": "consumer_cyclical",
    "AMZN": "consumer_cyclical",
    # Energy
    "XLE":  "energy",
    "OXY":  "energy",
    "XOM":  "energy",
    "CVX":  "energy",
    # Financials
    "KRE":  "financials",
    "JPM":  "financials",
    "BAC":  "financials",
    "GS":   "financials",
    # Materials / Gold
    "GDX":  "materials",
    "GLD":  "commodities",
    "SLV":  "commodities",
    # Rates / Fixed income
    "TLT":  "rates",
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
AUTO_OPEN_BROWSER = True          # Auto-open tip sheet in browser after generation

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
