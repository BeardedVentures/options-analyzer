"""
analysis/edge_calculator.py — VRP edge scoring, IV rank, and delta probability analysis.

Core theory: the volatility risk premium (VRP) means implied vol consistently
overstates realized vol. Options sellers exploit this systematic overpricing.

The true POP calculation mirrors the sharp sports-betting model:
  - Market pays us for an implied probability
  - We calculate the ACTUAL historical frequency
  - Edge = True POP - Implied POP
  - Positive edge = we are being overpaid for the risk we're taking
"""

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# VOLATILITY RISK PREMIUM
# ─────────────────────────────────────────────

def calculate_vrp(current_iv: float, realized_vol_30d: float) -> Dict:
    """
    Volatility Risk Premium = Implied Volatility - Realized Volatility.

    VRP > 0 → options overpriced → seller has edge.
    Both inputs should be decimals (e.g., 0.25 for 25%).

    Returns:
        vrp_value: decimal difference
        vrp_pct: percentage points
        edge_exists: True if VRP >= config threshold
    """
    if current_iv <= 0 or realized_vol_30d <= 0:
        return {"vrp_value": 0.0, "vrp_pct": 0.0, "edge_exists": False}

    vrp_value = current_iv - realized_vol_30d
    vrp_pct = round(vrp_value * 100, 2)
    edge_exists = vrp_value >= config.VRP_MIN_THRESHOLD

    return {
        "vrp_value": round(vrp_value, 4),
        "vrp_pct": vrp_pct,
        "edge_exists": edge_exists,
    }


# ─────────────────────────────────────────────
# TRUE HISTORICAL PROBABILITY OF PROFIT
# ─────────────────────────────────────────────

def calculate_true_pop(
    strike_distance_pct: float,
    expiration_days: int,
    historical_prices: pd.Series,
) -> Dict:
    """
    Calculate the true historical probability of profit for a bull put spread.

    Methodology (mirrors sharp sports betting edge model):
      1. Get 2+ years of daily price history
      2. For each rolling window of [expiration_days] length in history:
         - Start price = price at window start
         - Strike = start_price * (1 - strike_distance_pct)
         - Success = end price > strike at window end
      3. True POP = successes / total windows

    This gives the ACTUAL historical frequency, not the market's implied probability.

    Args:
        strike_distance_pct: decimal (e.g., 0.05 for 5% below current price)
        expiration_days: DTE
        historical_prices: pd.Series of close prices (2+ years preferred)

    Returns:
        true_pop: float 0-1
        windows_tested: int
        confidence: "HIGH" | "MEDIUM" | "LOW"
    """
    prices = historical_prices.dropna().values
    n = len(prices)

    if n < expiration_days + 30:
        logger.warning("[edge] Insufficient history for true POP calculation")
        return {
            "true_pop": None,
            "windows_tested": 0,
            "confidence": "LOW",
        }

    successes = 0
    total = 0

    for i in range(n - expiration_days):
        start_price = prices[i]
        if start_price <= 0:
            continue

        # Strike equivalent for this historical window
        strike = start_price * (1 - strike_distance_pct)

        # Check end-of-window price (European-style — final price only)
        end_price = prices[i + expiration_days]
        if end_price > strike:
            successes += 1
        total += 1

    if total == 0:
        return {"true_pop": None, "windows_tested": 0, "confidence": "LOW"}

    true_pop = successes / total

    # Confidence based on sample size
    if total >= 400:
        confidence = "HIGH"
    elif total >= 150:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "true_pop": round(true_pop, 4),
        "windows_tested": total,
        "confidence": confidence,
    }


def calculate_edge_points(true_pop: Optional[float], implied_pop: float) -> Dict:
    """
    Calculate the edge in probability points.

    Edge = True POP - Implied POP
    Positive edge = seller is being overpaid for the risk.

    Example: True POP 84% vs Implied POP 80% → 4 edge points
    Like finding -180 (45% win probability) priced at -150 (40% win prob) in sports betting.

    Args:
        true_pop: historical frequency (decimal)
        implied_pop: market's implied probability = 1 - |delta of short put|

    Returns:
        edge_points: percentage points of edge (can be negative)
        edge_exists: True if edge_points > 0
    """
    if true_pop is None:
        return {"edge_points": 0.0, "edge_pct": 0.0, "edge_exists": False}

    edge_decimal = true_pop - implied_pop
    edge_points = round(edge_decimal * 100, 2)

    return {
        "edge_points": edge_points,
        "edge_pct": edge_points,
        "edge_exists": edge_points > 0,
    }


# ─────────────────────────────────────────────
# COMPOSITE EDGE SCORE
# ─────────────────────────────────────────────

def calculate_edge_score(
    ticker: str,
    strategy: str,
    technical_score: float,
    vrp_pct: float,
    edge_points: float,
    news_sentiment: str,
    earnings_days_away: int,
) -> Dict:
    """
    Composite 0-100 edge score combining all factors.

    Components:
      VRP component         (30 points max)
      True POP Edge         (25 points max)
      Technical Score       (25 points max)
      News Sentiment        (10 points max)
      Earnings Safety       (10 points max)

    Returns:
        total_score: int 0-100
        component_breakdown: dict of each component's score
        qualified: True if total >= MIN_EDGE_SCORE
        disqualification_reason: str if disqualified early
    """
    breakdown = {}
    disqualification_reason = None

    # ── VRP Component (30 points max) ──
    if vrp_pct < 0:
        # Negative VRP = options cheap = no edge for sellers
        breakdown["vrp"] = 0
        disqualification_reason = f"Negative VRP ({vrp_pct:.1f}%) — options underpriced, no seller edge"
    elif vrp_pct >= 30:
        breakdown["vrp"] = 30
    elif vrp_pct >= 20:
        breakdown["vrp"] = 22
    elif vrp_pct >= 10:
        breakdown["vrp"] = 15
    else:
        breakdown["vrp"] = 5

    # ── True POP Edge Component (25 points max) ──
    if edge_points < 0:
        breakdown["true_pop_edge"] = 0
        if disqualification_reason is None:
            disqualification_reason = f"Negative edge ({edge_points:.1f} pts) — market implied POP exceeds historical"
    elif edge_points > 8:
        breakdown["true_pop_edge"] = 25
    elif edge_points >= 5:
        breakdown["true_pop_edge"] = 18
    elif edge_points >= 2:
        breakdown["true_pop_edge"] = 12
    else:
        breakdown["true_pop_edge"] = 5

    # ── Technical Score Component (25 points max) ──
    breakdown["technical"] = round(min(technical_score * 0.25, 25))

    # ── News Sentiment Component (10 points max) ──
    sentiment_upper = news_sentiment.upper() if news_sentiment else "NEUTRAL"
    if sentiment_upper == "BLOCKING":
        breakdown["news"] = 0
        if disqualification_reason is None:
            disqualification_reason = "News BLOCKING event detected — do not sell premium"
    elif sentiment_upper == "NEGATIVE":
        breakdown["news"] = 0
        # Don't disqualify, but flag — the validator handles blocking
    elif sentiment_upper == "NEUTRAL":
        breakdown["news"] = 5
    else:  # POSITIVE
        breakdown["news"] = 10

    # ── Earnings Safety Component (10 points max) ──
    if earnings_days_away < config.EARNINGS_BLACKOUT_DAYS:
        breakdown["earnings_safety"] = 0
        if disqualification_reason is None:
            disqualification_reason = (
                f"Earnings in {earnings_days_away} days — "
                f"within {config.EARNINGS_BLACKOUT_DAYS}-day blackout window"
            )
    elif earnings_days_away <= 14:
        breakdown["earnings_safety"] = 2
    elif earnings_days_away <= 30:
        breakdown["earnings_safety"] = 7
    else:
        breakdown["earnings_safety"] = 10

    total = sum(breakdown.values())
    total = min(100, max(0, total))

    qualified = (
        total >= config.MIN_EDGE_SCORE
        and disqualification_reason is None
        and vrp_pct >= 0
        and edge_points >= 0
    )

    return {
        "total_score": total,
        "component_breakdown": breakdown,
        "qualified": qualified,
        "disqualification_reason": disqualification_reason,
    }


# ─────────────────────────────────────────────
# STRATEGY SELECTION HELPER
# ─────────────────────────────────────────────

def select_best_strategy(
    account_balance: float,
    trend: str,
    iv_rank: float,
    vix_level: float,
) -> str:
    """
    Select the most appropriate strategy given market conditions and account size.
    Respects account balance constraints.
    """
    max_spread = config.MAX_SPREAD_WIDTH

    if account_balance < 1000:
        # Very small account — only bull put spreads and bear call spreads
        if trend in ("STRONG_UP", "UP", "NEUTRAL"):
            return "bull_put_spread"
        else:
            return "bear_call_spread"

    if account_balance >= 2500 and trend == "NEUTRAL" and iv_rank >= 55:
        return "iron_condor"

    if trend in ("STRONG_UP", "UP"):
        return "bull_put_spread"
    elif trend in ("STRONG_DOWN", "DOWN"):
        return "bear_call_spread"
    else:
        return "bull_put_spread"  # default neutral


# ─────────────────────────────────────────────
# STRIKE SELECTION
# ─────────────────────────────────────────────

def find_target_put(
    options: list,
    current_price: float,
    ticker: str,
    target_delta: float = None,
) -> Optional[dict]:
    """
    From an options chain list, find the put closest to the target delta.
    Only considers puts that meet the minimum OTM buffer.

    Returns the best option dict or None.
    """
    if target_delta is None:
        target_delta = config.SHORT_STRIKE_TARGET_DELTA

    is_spy_like = ticker.upper() in config.SPY_BUFFER_TICKERS

    candidates = []
    for opt in options:
        if opt.get("type") != "put":
            continue

        strike = opt.get("strike", 0)
        delta = opt.get("delta")
        if delta is None:
            continue

        abs_delta = abs(delta)

        # Enforce absolute max delta
        if abs_delta > config.SHORT_STRIKE_MAX_DELTA:
            continue

        # Enforce OTM buffer
        if is_spy_like:
            if (current_price - strike) < config.MIN_STRIKE_BUFFER_SPY:
                continue
        else:
            min_buffer_pct = config.SHORT_STRIKE_MIN_OTM_PCT
            if (current_price - strike) / current_price < min_buffer_pct:
                continue

        # Must have meaningful premium
        mid = opt.get("mid", 0)
        if mid * 100 < config.MIN_CREDIT_USD:
            continue

        candidates.append((abs(abs_delta - target_delta), opt))

    if not candidates:
        return None

    # Return candidate closest to target delta
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def calculate_spread_metrics(
    short_put: dict,
    long_put_strike: float,
    current_price: float,
) -> Dict:
    """
    Calculate spread credit, max loss, and position sizing for a bull put spread.

    Args:
        short_put: option dict (the one we're selling)
        long_put_strike: strike of the protective long put
        current_price: underlying price

    Returns:
        credit_per_share, credit_usd, max_loss_usd, spread_width,
        contracts_allowed, profit_target_usd, stop_loss_usd,
        strike_distance_usd, strike_distance_pct
    """
    short_strike = short_put["strike"]
    short_mid = short_put.get("mid", 0)

    spread_width = round(short_strike - long_put_strike, 2)
    if spread_width <= 0:
        return {}

    # Estimate long put mid at ~0.30 of short put mid (typical)
    # In production this would use actual long put price from options chain
    long_put_mid = short_mid * 0.30
    credit_per_share = round(short_mid - long_put_mid, 2)
    credit_usd = round(credit_per_share * 100, 2)  # per contract

    max_loss_per_share = spread_width - credit_per_share
    max_loss_usd = round(max_loss_per_share * 100, 2)

    # How many contracts can we trade within risk limit?
    if max_loss_usd > 0:
        contracts_allowed = max(config.MIN_CONTRACTS, int(config.MAX_RISK_PER_TRADE_USD / max_loss_usd))
    else:
        contracts_allowed = config.MIN_CONTRACTS

    profit_target_usd = round(credit_usd * config.TARGET_PROFIT_PCT, 2)
    stop_loss_usd = round(credit_usd * config.STOP_LOSS_MULTIPLIER, 2)

    strike_distance_usd = round(current_price - short_strike, 2)
    strike_distance_pct = round(strike_distance_usd / current_price * 100, 2)

    return {
        "credit_per_share": credit_per_share,
        "credit_usd": credit_usd,
        "spread_width": spread_width,
        "max_loss_usd": max_loss_usd,
        "contracts_allowed": contracts_allowed,
        "profit_target_usd": profit_target_usd,
        "stop_loss_close_price": round(credit_per_share * (1 + config.STOP_LOSS_MULTIPLIER), 2),
        "stop_loss_usd": stop_loss_usd,
        "strike_distance_usd": strike_distance_usd,
        "strike_distance_pct": strike_distance_pct,
    }
