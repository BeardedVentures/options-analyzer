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
ENABLED_STRATEGIES = [
    "bull_put_spread",
    "bear_call_spread",
    "iron_condor",
    "pmcc",
    "csp",
]

# ─────────────────────────────────────────────
# SCAN CRITERIA
# ─────────────────────────────────────────────
MIN_PROBABILITY_OF_PROFIT = 0.72  # 72% minimum — true probability not just delta
MIN_IV_RANK = 45                  # Only trade when IV rank >= 45
MIN_CREDIT_USD = 25               # Minimum premium worth collecting (per contract)
MIN_DTE = 21                      # Minimum days to expiration
MAX_DTE = 45                      # Maximum days to expiration
TARGET_PROFIT_PCT = 0.50          # Close winners at 50% of max profit
STOP_LOSS_MULTIPLIER = 2.0        # Stop if spread reaches 2x credit received

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

# EDGE FILTER THRESHOLDS
# ─────────────────────────────────────────────
MIN_EDGE_SCORE = 60               # 0-100 composite score required to appear on tip sheet
VRP_MIN_THRESHOLD = 0.15          # Implied vol must exceed realized vol by at least 15%
NEWS_SENTIMENT_BLOCK = True       # Block trades on tickers with strong negative news
EARNINGS_BLACKOUT_DAYS = 7        # Never sell premium within 7 days of earnings (unless volatility crush mode is enabled)

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
TRADIER_API_KEY   = os.environ.get("TRADIER_API_KEY", "")
TRADIER_SANDBOX   = os.environ.get("TRADIER_SANDBOX", "true").lower() == "true"

# AI Model settings
CLAUDE_MODEL = "claude-sonnet-4-6"
OPENAI_MODEL = "gpt-4o-mini"     # Cheapest GPT-4 class model for news batch

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
